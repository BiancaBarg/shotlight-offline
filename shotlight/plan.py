"""The complete boundary between lighting plans and scene mutations.

The planner returns data, never executable code or Maya commands. Native adapters
must accept only plans validated here before creating any nodes.
"""

import math
import re


class PlanError(ValueError):
    pass


EXPOSURE_LIMITS = {"area": (-12, 32), "directional": (-12, 20), "skydome": (-12, 20)}


def _vector_schema(length):
    return {"type": "array", "items": {"type": "number"},
            "minItems": length, "maxItems": length}


LIGHT_FIELDS = {
    "name": {"type": "string"},
    "role": {"type": "string", "enum": ["key", "fill", "rim", "environment", "practical"]},
    "type": {"type": "string", "enum": ["area", "directional", "skydome"]},
    "position": _vector_schema(3),
    "target": _vector_schema(3),
    "size": _vector_schema(2),
    "color": _vector_schema(3),
    "exposure": {"type": "number", "minimum": -12, "maximum": 32},
}

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "look_description": {"type": "string"},
        "confidence": {"type": "number"},
        "lights": {"type": "array", "minItems": 1, "maxItems": 6,
                   "items": {"type": "object", "additionalProperties": False,
                             "properties": LIGHT_FIELDS,
                             "required": list(LIGHT_FIELDS)}},
    },
    "required": ["summary", "look_description", "confidence", "lights"],
}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanError("%s must be a finite number." % name)
    return float(value)


def _vector(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise PlanError("%s must have exactly %s values." % (name, length))
    return [_number(v, name) for v in value]


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlanError("%s must be a nonempty string of at most %s characters." % (name, maximum))
    return value.strip()


def validate_plan(plan, shot):
    """Validate and normalize a plan; reject unsupported instructions completely."""
    if not isinstance(plan, dict) or set(plan) != set(PLAN_SCHEMA["required"]):
        raise PlanError("The lighting engine returned an incomplete or unsupported lighting plan.")
    radius = _number(shot.get("radius"), "Shot radius")
    if radius <= 0:
        raise PlanError("The subject has no usable size.")
    center = _vector(shot.get("center"), 3, "Shot center")
    normalized = {
        "summary": _text(plan["summary"], "Summary", 4000),
        "look_description": _text(plan["look_description"], "Look", 2000),
        "confidence": _number(plan["confidence"], "Confidence"),
        "lights": [],
    }
    if not 0 <= normalized["confidence"] <= 1:
        raise PlanError("Confidence must be between zero and one.")
    lights = plan["lights"]
    if not isinstance(lights, list) or not 1 <= len(lights) <= 6:
        raise PlanError("A lighting plan needs one to six lights.")
    seen_names = set()
    for index, light in enumerate(lights):
        if not isinstance(light, dict) or set(light) != set(LIGHT_FIELDS):
            raise PlanError("Light %s contains missing or unsupported settings." % (index + 1))
        name = _text(light["name"], "Light name", 64)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise PlanError("Light names must be simple identifiers.")
        if name in seen_names:
            raise PlanError("Light names must be unique.")
        seen_names.add(name)
        if light["role"] not in LIGHT_FIELDS["role"]["enum"] or light["type"] not in LIGHT_FIELDS["type"]["enum"]:
            raise PlanError("Unsupported light role or type.")
        item = {"name": name, "role": light["role"], "type": light["type"]}
        item["position"] = _vector(light["position"], 3, "Position")
        item["target"] = _vector(light["target"], 3, "Target")
        item["size"] = _vector(light["size"], 2, "Size")
        item["color"] = _vector(light["color"], 3, "Colour")
        item["exposure"] = _number(light["exposure"], "Exposure")
        # Positions are world coordinates, constrained by the shot's actual size.
        for field in ("position", "target"):
            if math.dist(item[field], center) > radius * 25:
                raise PlanError("%s is too far from the shot." % field.title())
        if item["type"] != "skydome" and math.dist(item["position"], item["target"]) < radius * .001:
            raise PlanError("A light needs a distinct position and aiming target.")
        if any(v <= 0 or v > radius * 10 for v in item["size"]):
            raise PlanError("Light size is outside the subject-relative range.")
        if any(v < 0 or v > 1 for v in item["color"]):
            raise PlanError("Light colour components must be between zero and one.")
        minimum, maximum = EXPOSURE_LIMITS[item["type"]]
        if not minimum <= item["exposure"] <= maximum:
            raise PlanError("%s light exposure must be between %s and %s stops." % (item["type"].title(), minimum, maximum))
        normalized["lights"].append(item)
    return normalized
