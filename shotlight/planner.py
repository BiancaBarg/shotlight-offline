"""Offline shot-aware lighting. No accounts, network clients or external processes.

Geometry, camera basis, native units and material cues choose a fixed Arnold rig.
Qt (bundled with Maya) measures local preview pixels for bounded exposure tuning.
These are technical exposure checks, never semantic or artistic approval.
"""
import copy
import itertools
import json
import math
from pathlib import Path

from .plan import EXPOSURE_LIMITS, validate_plan


class PlannerError(RuntimeError):
    pass


def _check(config):
    if config is not None and not isinstance(config, dict):
        raise PlannerError("Lighting configuration must be a dictionary.")
    if config and config.get("control") is not None:
        config["control"].check()


def _v(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PlannerError("The shot needs three-dimensional camera and subject measurements.")
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value):
        raise PlannerError("Shot measurements must be finite numbers.")
    return list(value)


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _mul(a, n):
    return [x * n for x in a]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _norm(a):
    length = math.sqrt(_dot(a, a))
    if length < 1e-9:
        raise PlannerError("The camera orientation has no usable direction.")
    return _mul(a, 1 / length)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _basis(shot):
    center = _v(shot.get("center"))
    radius = shot.get("radius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or radius <= 0:
        raise PlannerError("The subject has no usable size.")
    if shot.get("up_axis", "y") not in ("y", "z"):
        raise PlannerError("Unsupported scene up axis.")
    world_up = [0, 0, 1] if shot.get("up_axis") == "z" else [0, 1, 0]
    samples = shot.get("samples") or []
    camera = (samples[len(samples)//2].get("camera") if samples else None) or shot["camera"]
    forward = _norm(_v(camera.get("forward", _add(center, _mul(_v(camera["position"]), -1)))))
    camera_up = _norm(_v(camera.get("up", world_up)))
    right = _norm(_cross(forward, camera_up))
    toward = _mul(forward, -1)
    horizontal = _add(toward, _mul(world_up, -_dot(toward, world_up)))
    if _dot(horizontal, horizontal) < 1e-6:
        horizontal = _cross(world_up, right)
    toward = _norm(horizontal)
    right = _norm(_cross(world_up, toward))
    return center, float(radius), toward, right, world_up


def _material_offset(shot):
    luminances, roughness = [], []
    seen = set()
    for subject in shot.get("subjects", [])[:150]:
        for material in subject.get("materials", []):
            name = material.get("name", json.dumps(material, sort_keys=True))
            if name in seen:
                continue
            seen.add(name)
            props = material.get("properties", {})
            color = props.get("baseColor", props.get("color"))
            if isinstance(color, (list, tuple)) and len(color) == 3:
                try:
                    lum = _dot(_v(color), [.2126, .7152, .0722])
                    if 0 <= lum <= 1:
                        luminances.append(lum)
                except PlannerError:
                    pass
            r = props.get("specularRoughness")
            if isinstance(r, (int, float)) and math.isfinite(r):
                roughness.append(r)
    # Dark materials stay dark. Modest compensation avoids whitening them.
    mean = sum(luminances)/len(luminances) if luminances else .45
    offset = _clamp(.35 * math.log2(.45 / max(mean, .06)), -.4, .65)
    if roughness and sum(roughness)/len(roughness) < .2:
        offset -= .25
    return offset


def _placement(center, radius, offset, shot):
    """Avoid emitter centers inside known environment boxes, approximately.

    Environment group boxes are conservative; this is not a visibility solver.
    Try closer positions before retaining the original candidate with a warning.
    """
    obstacles = [item["bounds"] for item in shot.get("environment", []) if len(item.get("bounds", [])) == 6]
    def blocked(p):
        return any(all(b[i] + radius*.015 < p[i] < b[i+3] - radius*.015 for i in range(3)) for b in obstacles)
    for factor in (1, .8, .6, .4):
        position = _add(center, _mul(offset, radius*factor))
        if not blocked(position):
            return position, factor, False
    return _add(center, _mul(offset, radius)), 1, True


def validate_versions(bundle, shot):
    """Validate all three alternatives before any scene mutation."""
    try:
        if not isinstance(bundle, dict) or set(bundle) != {"versions"}:
            raise ValueError("Invalid bundle")
        if not isinstance(bundle["versions"], list) or len(bundle["versions"]) != 3:
            raise ValueError("Expected three versions")
        result, labels, plans = [], set(), set()
        for version in bundle["versions"]:
            if not isinstance(version, dict) or set(version) != {"label", "plan"}:
                raise ValueError("Invalid version")
            label = version["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 60 or any(ord(c) < 32 for c in label):
                raise ValueError("Invalid label")
            plan = validate_plan(version["plan"], shot)
            signature = json.dumps([{k:v for k,v in light.items() if k not in ("name", "role")} for light in plan["lights"]], sort_keys=True)
            if label.casefold() in labels or signature in plans:
                raise ValueError("Duplicate version")
            labels.add(label.casefold())
            plans.add(signature)
            result.append({"label": label.strip(), "plan": plan})
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        raise PlannerError("Invalid lighting versions; no lights were applied.") from None


def generate_versions(shot, previews=None, config=None):
    _check(config)
    center, radius, front, right, up = _basis(shot)
    units = {"mm": .1, "cm": 1, "m": 100, "in": 2.54, "ft": 30.48, "yd": 91.44}
    if shot.get("unit", "cm") not in units:
        raise PlannerError("Unsupported scene length unit.")
    base = 19.0 + 2*math.log2(radius*units[shot.get("unit", "cm")]/100) + _material_offset(shot)
    # Only untouched artist lighting can influence the initial balance.
    if previews and all(p.get("analysis_usable", True) for p in previews) and not any(light.get("owned") for light in shot.get("existing_lights", [])):
        metrics = measure_previews(shot, previews, config)
        if metrics and max(m["p70"] for m in metrics) > .3:
            base -= 1.5
    # Camera-relative directions; all positions cover the motion union, fixed in time.
    looks = [
        ("Soft sculpted", "Broad neutral key, gentle fill and restrained separation.", -1, 2.2, -2.6, -.8),
        ("Directional contrast", "A smaller side key, deeper shadows and a clean edge.", 1, 1.0, -3.8, -.4),
        ("Warm studio", "Warm soft key from the opposite side with a neutral fill.", 1, 2.8, -1.9, -1.3),
    ]
    versions = []
    for index, (label, description, side, size, fill_stops, rim_stops) in enumerate(looks):
        _check(config)
        lights, obstructed = [], False
        key_direction = _add(_add(_mul(right, side*(2.8 if index == 1 else 2.2)), _mul(front, 2.5 if index == 1 else 3)), _mul(up, 2.3))
        entries = [
            ("Key", "key", key_direction, size, 0, [1, .91, .8] if index == 2 else [1, .98, .94]),
            ("Fill", "fill", _add(_add(_mul(right, -side*2), _mul(front, 2.8)), _mul(up, .8)), 3.0, fill_stops, [.92, .96, 1]),
            ("Edge", "rim", _add(_add(_mul(right, -side*1.8), _mul(front, -2.4)), _mul(up, 2)), 1.8, rim_stops, [1, .97, .91]),
        ]
        target = _add(center, _mul(up, radius*.18))
        for name, role, offset, width, stops, color in entries:
            position, factor, blocked = _placement(center, radius, offset, shot)
            obstructed |= blocked
            # Moving an emitter closer reduces flux to preserve the initial balance.
            exposure = _clamp(base + stops + 2*math.log2(factor), *EXPOSURE_LIMITS["area"])
            lights.append({"name": name, "role": role, "type": "area", "position": position,
                           "target": target, "size": [radius*width, radius*width],
                           "color": color, "exposure": exposure})
        summary = "Local camera-relative lighting for the selected animation range. " + description
        if obstructed:
            summary += " Some emitter positions may intersect environment geometry; inspect the preview."
        versions.append({"label": label, "plan": {"summary": summary, "look_description": description,
                         "confidence": .5, "lights": lights}})
    return validate_versions({"versions": versions}, shot)


def projected_region(shot, frame, width, height):
    """Conservative screen box around sampled subject bounds (not segmentation)."""
    sample = next((s for s in shot.get("samples", []) if math.isclose(s["frame"], frame, abs_tol=.001)), {})
    camera = sample.get("camera", shot["camera"])
    bounds = sample.get("bounds", shot.get("bounds"))
    if not bounds or len(bounds) != 6:
        raise PlannerError("Missing subject bounds for the preview.")
    pos = _v(camera["position"])
    forward = _norm(_v(camera.get("forward", _add(shot["center"], _mul(pos, -1)))))
    up = _norm(_v(camera.get("up", [0, 0, 1] if shot.get("up_axis") == "z" else [0, 1, 0])))
    right = _norm(_cross(forward, up))
    up = _norm(_cross(right, forward))
    aspect = width / max(height, 1)
    if camera.get("orthographic"):
        half_x = float(camera.get("orthographic_width", 30))/2
    else:
        half_x = float(camera.get("horizontal_film_aperture", 1.417))*25.4/(2*float(camera.get("focal_length", 35)))
        if half_x <= 0 or not math.isfinite(half_x):
            raise PlannerError("Invalid camera aperture or focal length.")
        half_x *= float(camera.get("lens_squeeze_ratio", 1))
        vertical = float(camera.get("vertical_film_aperture", .945))*25.4/(2*float(camera.get("focal_length", 35)))
        fit = camera.get("film_fit", 1)
        if fit == 2:
            half_x = vertical*aspect
        elif fit == 0:
            half_x = min(half_x, vertical*aspect)
        elif fit == 3:
            half_x = max(half_x, vertical*aspect)
    half_y = half_x/aspect
    points = []
    for corner in itertools.product(*[(bounds[i], bounds[i+3]) for i in range(3)]):
        delta = _add(_v(corner), _mul(pos, -1))
        depth = _dot(delta, forward)
        if depth <= 1e-6 and not camera.get("orthographic"):
            raise PlannerError("The subject bounds cross or sit behind the camera; exposure cannot be measured reliably.")
        divisor = 1 if camera.get("orthographic") else depth
        points.append((.5 + _dot(delta, right)/(2*half_x*divisor), .5 - _dot(delta, up)/(2*half_y*divisor)))
    left, top = max(0, min(p[0] for p in points)), max(0, min(p[1] for p in points))
    right_edge, bottom = min(1, max(p[0] for p in points)), min(1, max(p[1] for p in points))
    if right_edge-left < .01 or bottom-top < .01:
        raise PlannerError("The subject is outside the shot camera; exposure cannot be measured.")
    return (int(left*width), int(top*height), min(width-1, int(right_edge*width)), min(height-1, int(bottom*height)))


def pixel_metrics(pixels):
    """Bounded preview-space luminance statistics; retain opaque black pixels."""
    values, clipped = [], 0
    for r, g, b, a in pixels:
        if a < .02:
            continue
        if not all(math.isfinite(x) and 0 <= x <= 1 for x in (r, g, b, a)):
            raise PlannerError("Invalid preview pixel values.")
        values.append(.2126*r + .7152*g + .0722*b)
        clipped += max(r, g, b) >= .985
    if len(values) < 16:
        raise PlannerError("Too few visible preview pixels to check exposure.")
    values.sort()
    def percentile(p):
        return values[round((len(values)-1)*p)]
    return {"pixels": len(values), "p50": percentile(.5), "p70": percentile(.7), "p90": percentile(.9),
            "clipped": clipped/len(values), "dark": sum(v < .025 for v in values)/len(values)}


def measure_previews(shot, previews, config=None):
    _check(config)
    if not isinstance(previews, (list, tuple)) or not 1 <= len(previews) <= 8:
        raise PlannerError("A local exposure check needs one to eight rendered previews.")
    try:
        from PySide6.QtGui import QImage
    except ImportError:
        try:
            from PySide2.QtGui import QImage
        except ImportError:
            raise PlannerError("Preview analysis needs Qt bundled with Maya. Run ShotLight inside Maya.") from None
    metrics = []
    for preview in previews:
        _check(config)
        if not preview.get("analysis_usable", True):
            raise PlannerError("Arnold marked this preview with a license watermark. Exposure tuning needs an unwatermarked Maya render; the editable lights are available.")
        path = Path(preview["path"])
        if not path.is_file() or path.stat().st_size > 8*1024*1024:
            raise PlannerError("A local preview is missing or exceeds 8 MB.")
        image = QImage(str(path))
        if image.isNull() or image.width()*image.height() > 16_000_000:
            raise PlannerError("The local preview could not be decoded safely.")
        left, top, right, bottom = projected_region(shot, preview["frame"], image.width(), image.height())
        pixels = []
        for y in range(top, bottom+1, max(1, (bottom-top)//40)):
            _check(config)
            for x in range(left, right+1, max(1, (right-left)//40)):
                c = image.pixelColor(x, y)
                pixels.append((c.redF(), c.greenF(), c.blueF(), c.alphaF()))
        item = pixel_metrics(pixels)
        item.update(frame=preview["frame"], region=[left, top, right, bottom])
        metrics.append(item)
    return metrics


def exposure_correction(metrics):
    """One conservative global correction; never erase deliberate dark materials."""
    if not metrics:
        raise PlannerError("No rendered exposure measurements are available.")
    level = sorted(m["p70"] for m in metrics)[len(metrics)//2]
    if max(m["clipped"] for m in metrics) > .04:
        return -.75
    if level < .16:
        return _clamp(math.log2(.27/max(level, .012)), 0, 2.5)
    if level > .62:
        return _clamp(math.log2(.4/level), -1.5, 0)
    return 0


def analyze(shot, previews=None, previous_plan=None, feedback=None, config=None):
    _check(config)
    if previous_plan is None:
        return generate_versions(shot, previews, config)[0]["plan"]
    metrics = measure_previews(shot, previews, config)
    result = copy.deepcopy(validate_plan(previous_plan, shot))
    stops = exposure_correction(metrics)
    for light in result["lights"]:
        light["exposure"] = _clamp(light["exposure"] + stops, *EXPOSURE_LIMITS[light["type"]])
    if stops:
        result["summary"] += " Local preview exposure adjustment: %+.2f stops." % stops
    _check(config)
    return validate_plan(result, shot)


def assess(shot, previews, plan, config=None):
    validate_plan(plan, shot)
    metrics = measure_previews(shot, previews, config)
    if not metrics:
        raise PlannerError("No rendered exposure measurements are available.")
    expected = set(shot.get("frames", []))
    actual = {m["frame"] for m in metrics}
    if expected and actual != expected:
        raise PlannerError("The exposure check does not cover every requested sampled frame.")
    problems = []
    for m in metrics:
        if m["p90"] < .07:
            problems.append("frame %s is very dark" % m["frame"])
        if m["clipped"] > .04:
            problems.append("frame %s has clipped highlights" % m["frame"])
    return {"acceptable": not problems, "scope": "technical_exposure", "method": "local_preview_pixels",
            "reason": "; ".join(problems) if problems else "No major dark-frame or clipping warnings in the sampled subject regions. Artist review is still required.",
            "metrics": metrics}
