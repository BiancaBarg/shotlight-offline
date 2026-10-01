"""Host-independent orchestration: see the shot, decide, render, refine."""

import json
import copy
import math
import threading
import time
from pathlib import Path

from . import planner
from .runtime import Budget, Cancelled, TimedOut


def scene_fingerprint(shot):
    """Only input data, excluding generated lights and temporary render settings."""
    value = {key: shot.get(key) for key in
             ("scene_identity", "unit", "up_axis", "camera", "bounds", "frames",
              "samples", "subject_paths", "subjects", "environment",
              "render_resolution", "display_aspect_ratio")}
    # The overview camera describes the current playhead, which an artist may
    # scrub while local analysis runs. Guard camera animation with the fixed sampled frames;
    # keep camera identities and any non-sampled metadata authoritative.
    sampled = (shot.get("samples") or [{}])[0].get("camera", {})
    camera = dict(shot.get("camera") or {})
    for key in camera.keys() & sampled.keys() - {"name", "transform"}:
        camera[key] = sampled[key]
    value["camera"] = camera
    value["artist_lights"] = [light for light in shot.get("existing_lights", [])
                             if not light.get("owned")]
    return json.dumps(value, sort_keys=True, default=str)


def same_scene(first, second):
    """Ignore Maya evaluation roundoff while still detecting artist edits.

    Re-evaluating skinned bounds can change the last few float digits. Compare
    numeric measurements with a tiny native-unit tolerance, without rounding
    strings, node identities, selections or the shape of the collected data.
    """
    def equal(a, b):
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            return math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-4)
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b
    return equal(json.loads(first), json.loads(second))


def _guard(adapter, shot, invoke):
    current = invoke(lambda: adapter.collect_shot(
        camera=shot["camera"]["name"],
        subjects=shot.get("subject_paths", [s["name"] for s in shot["subjects"]]),
        sample_count=3))
    if not same_scene(scene_fingerprint(current), scene_fingerprint(shot)):
        raise RuntimeError("The shot changed. Select your camera, set its playback range and press Light Shot again.")


def _checkpoint(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise Cancelled("Stopped. Any lighting already placed remains editable.")


def _save_result(result):
    Path(result["output_dir"], "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _activate(result, index):
    result["active_version"] = index
    version = result["versions"][index]
    for key in ("plan", "after", "refined", "warning", "assessment"):
        result[key] = version.get(key, [] if key == "after" else None)
    return result


def fast_light_shot(adapter, output_dir, config=None, status=None, invoke=None,
                    cancelled=None, camera=None, subjects=None, control=None):
    """Local versions with one baseline frame and one chosen-version preview."""
    invoke = invoke or (lambda fn: fn())
    status = status or (lambda message: None)
    control = control or Budget(90, cancelled, status)
    config = dict(config or {}, control=control)
    started = time.monotonic()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _checkpoint(cancelled)
    status("Reading your selected camera and playback range…")
    shot = invoke(lambda: adapter.collect_shot(camera=camera, subjects=subjects, sample_count=3))
    (directory / "shot.json").write_text(json.dumps(shot, indent=2), encoding="utf-8")
    status("Rendering one small shot reference locally…")
    baseline = dict(shot, frames=[shot["frames"][len(shot["frames"]) // 2]])
    before = invoke(lambda: adapter.capture_previews(baseline, str(directory / "before"), render=True, quick=True, control=control))
    _checkpoint(cancelled)
    status("Designing three lighting versions locally…")
    versions = planner.generate_versions(shot, before, config=config)
    # Revalidate the whole bundle at the mutation boundary as well.
    versions = planner.validate_versions({"versions": versions}, shot)
    control.check()
    _checkpoint(cancelled)
    _guard(adapter, shot, invoke)
    control.check()
    status("Applying the recommended lighting version…")
    group = invoke(lambda: adapter.apply_plan(versions[0]["plan"], shot))
    for version in versions:
        version.update(after=[], refined=False, warning=None, assessment=None)
    result = {"group": group, "shot": shot, "versions": versions, "active_version": 0,
              "before": before, "camera": shot["camera"]["name"], "frames": shot["frames"],
              "output_dir": str(directory), "mode": "quick", "seconds": 0}
    _activate(result, 0)
    result = preview_version(adapter, result, status=status, invoke=invoke, cancelled=cancelled, control=control, calibrate=True)
    result["seconds"] = round(time.monotonic() - started, 1)
    return _save_result(result)


def switch_version(adapter, result, index, invoke=None):
    """Apply a cached native plan. No network request or render when switching."""
    invoke = invoke or (lambda fn: fn())
    if type(index) is not int or not 0 <= index < len(result["versions"]):
        raise ValueError("Choose an available lighting version.")
    _guard(adapter, result["shot"], invoke)
    updated = copy.deepcopy(result)
    updated["group"] = invoke(lambda: adapter.apply_plan(updated["versions"][index]["plan"], updated["shot"]))
    _activate(updated, index)
    updated["mode"] = "reviewed" if updated.get("assessment") else "quick"
    return _save_result(updated)


def restore_original(adapter, result, invoke=None):
    """Remove only generated lights, keeping the version bank for reapplication."""
    invoke = invoke or (lambda fn: fn())
    invoke(lambda: adapter.remove_rig())
    updated = copy.deepcopy(result)
    updated.update(group=None, active_version=None, plan=None, after=[], refined=False,
                   warning=None, assessment=None, mode="original")
    return _save_result(updated)


def preview_version(adapter, result, status=None, invoke=None, cancelled=None, control=None, calibrate=False):
    """Local Arnold preview of the selected version; cached until it is replaced."""
    invoke = invoke or (lambda fn: fn())
    status = status or (lambda message: None)
    control = control or Budget(30, cancelled, status)
    updated = copy.deepcopy(result)
    index = updated["active_version"]
    version = updated["versions"][index]
    stopped = None
    try:
        _checkpoint(cancelled)
        _guard(adapter, updated["shot"], invoke)
        status("Rendering one small preview of " + version["label"] + "…")
        sample = dict(updated["shot"])
        sample["frames"] = [sample["frames"][len(sample["frames"]) // 2]]
        after = invoke(lambda: adapter.capture_previews(sample, str(Path(updated["output_dir"], "version_%d" % index)), render=True, quick=True, control=control))
        _guard(adapter, updated["shot"], invoke)
        control.check()
        version.update(after=after, warning=None, assessment=None, refined=False)
        if calibrate:
            status("Checking preview exposure locally…")
            improved = planner.analyze(sample, after, previous_plan=version["plan"], config={"control": control})
            _guard(adapter, updated["shot"], invoke)
            control.check()
            if improved != version["plan"]:
                updated["group"] = invoke(lambda: adapter.apply_plan(improved, updated["shot"]))
                version.update(plan=improved, after=[], refined=True)
                status("Rendering the exposure-adjusted preview…")
                version["after"] = invoke(lambda: adapter.capture_previews(sample, str(Path(updated["output_dir"], "calibrated_%d" % index)), render=True, quick=True, control=control))
                _guard(adapter, updated["shot"], invoke)
                control.check()
    except Exception as error:
        stopped = error
        version.update(warning="Lights are placed; the quick preview or exposure check could not complete: %s" % error, assessment=None)
    _activate(updated, index)
    updated["mode"] = "quick"
    status("Stopped · existing lighting kept." if isinstance(stopped, Cancelled) else
           "Time limit reached · existing lighting kept." if isinstance(stopped, TimedOut) else
           "Quick draft ready · choose a version or check the full shot." if not updated["warning"] else
           "Lighting needs review; see the note below.")
    return _save_result(updated)


def review_version(adapter, result, config=None, status=None, invoke=None, cancelled=None, control=None):
    """Optional sampled-frame renders and local technical exposure checks."""
    invoke = invoke or (lambda fn: fn())
    status = status or (lambda message: None)
    control = control or Budget(90, cancelled, status)
    config = dict(config or {}, control=control)
    updated = copy.deepcopy(result)
    shot = updated["shot"]
    index = updated["active_version"]
    version = updated["versions"][index]
    started = time.monotonic()
    _checkpoint(cancelled)
    _guard(adapter, shot, invoke)
    version.update(after=[], warning=None, assessment=None, refined=False)
    stopped = None
    try:
        status("Rendering the selected version at the start, middle and end…")
        after = invoke(lambda: adapter.capture_previews(shot, str(Path(updated["output_dir"], "review_%d" % index)), render=True, quick=True, control=control))
        version["after"] = after
        _checkpoint(cancelled)
        status("Checking sampled exposure locally…")
        improved = planner.analyze(shot, after, previous_plan=version["plan"],
            config=config)
        control.check()
        _checkpoint(cancelled)
        _guard(adapter, shot, invoke)
        control.check()
        if improved != version["plan"]:
            updated["group"] = invoke(lambda: adapter.apply_plan(improved, shot))
            version.update(plan=improved, after=[], refined=True)
            status("Rendering the exposure-adjusted sampled frames…")
            version["after"] = invoke(lambda: adapter.capture_previews(shot, str(Path(updated["output_dir"], "final_%d" % index)), render=True, quick=True, control=control))
        _checkpoint(cancelled)
        status("Measuring the final sampled exposure locally…")
        version["assessment"] = planner.assess(shot, version["after"], version["plan"], config=config)
        _checkpoint(cancelled)
        _guard(adapter, shot, invoke)
        control.check()
        if not version["assessment"]["acceptable"]:
            version["warning"] = "Lighting needs review: " + version["assessment"]["reason"]
    except Exception as error:
        stopped = error
        version["assessment"] = None
        version["warning"] = "Lights remain editable; the full shot check could not complete: %s" % error
    _activate(updated, index)
    updated["mode"] = "reviewed" if updated.get("assessment") else "quick"
    updated["review_seconds"] = round(time.monotonic() - started, 1)
    status("Stopped · existing lighting kept." if isinstance(stopped, Cancelled) else
           "Time limit reached · existing lighting kept." if isinstance(stopped, TimedOut) else
           "Sampled exposure checked · inspect the lighting." if not updated["warning"] else "Lighting needs review; see the note below.")
    return _save_result(updated)


def light_shot(adapter, output_dir, config=None, status=None, invoke=None,
               cancelled=None, camera=None, subjects=None):
    """One user action, with real render references and an explicit final review.

    ``invoke`` dispatches Maya operations to its main thread. Local analysis
    stays on the worker thread. All plans are validated before scene edits.
    """
    status = status or (lambda message: None)
    invoke = invoke or (lambda fn: fn())
    cancelled = cancelled or threading.Event()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    group = None
    control = Budget(90, cancelled, status)
    config = dict(config or {}, control=control)

    def checkpoint():
        control.check()
        if cancelled.is_set():
            raise Cancelled("Stopped. Any lighting already placed remains editable.")

    def emit(message):
        checkpoint()
        status(message)

    def check_scene():
        current = invoke(lambda: adapter.collect_shot(
            camera=shot["camera"]["name"],
            subjects=shot.get("subject_paths", [s["name"] for s in shot["subjects"]]),
            sample_count=3))
        if not same_scene(scene_fingerprint(current), fingerprint):
            raise RuntimeError("The shot changed during analysis. Click Light Shot again for the updated shot.")

    emit("Reading the camera, subjects and animation range…")
    shot = invoke(lambda: adapter.collect_shot(camera=camera, subjects=subjects, sample_count=3))
    fingerprint = scene_fingerprint(shot)
    (directory / "shot.json").write_text(json.dumps(shot, indent=2), encoding="utf-8")
    emit("Rendering the current shot through %s…" % shot["camera"]["name"].split("|")[-1])
    before = invoke(lambda: adapter.capture_previews(shot, str(directory / "before"), render=True, quick=True, control=control))
    emit("Choosing camera-relative lighting locally…")
    plan = planner.analyze(shot, before, config=config)
    (directory / "initial_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    checkpoint()
    check_scene()
    emit("Creating editable lights in your scene…")
    group = invoke(lambda: adapter.apply_plan(plan, shot))
    warning = None
    after = []
    refined = False
    assessment = None
    try:
        emit("Rendering small previews to check the lighting…")
        after = invoke(lambda: adapter.capture_previews(shot, str(directory / "check"), render=True, quick=True, control=control))
        emit("Checking preview exposure locally…")
        improved = planner.analyze(
            shot, after, previous_plan=plan,
            feedback="Review the actual rendered frames. Keep successful choices. Correct only observed lighting problems: unreadable subject, excessive highlights, misplaced shadows, occlusion or inconsistent appearance across the animation. These are preview renders; do not mistake sampling noise for a lighting problem.",
            config=config)
        checkpoint()
        check_scene()
        emit("Applying the reviewed lighting…")
        group = invoke(lambda: adapter.apply_plan(improved, shot))
        plan = improved
        refined = True
        after = []  # Earlier check images no longer describe the replaced rig.
        emit("Rendering the final lighting previews…")
        after = invoke(lambda: adapter.capture_previews(shot, str(directory / "after"), render=True, quick=True, control=control))
        emit("Checking subject readability and highlights in the final renders…")
        assessment = planner.assess(shot, after, plan, config=config)
        checkpoint()
        check_scene()
        if not assessment["acceptable"]:
            warning = "Lighting needs review: " + assessment["reason"]
    except Cancelled:
        warning = "Stopped after lights were placed. The current rig remains editable."
    except Exception as error:
        warning = "Lights are placed; their preview/refinement could not complete: %s" % error
    result = {"group": group, "plan": plan, "before": before, "after": after,
              "refined": refined, "warning": warning, "assessment": assessment,
              "seconds": round(time.monotonic() - started, 1),
              "camera": shot["camera"]["name"], "frames": shot["frames"]}
    (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    status("Sampled exposure checked · inspect the lighting." if not warning else "Lighting needs review; check the note below.")
    return result
