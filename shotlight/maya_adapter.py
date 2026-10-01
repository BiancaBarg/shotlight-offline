"""Scene-safe Maya/Arnold adapter for ShotLight.

Importing this module does not load Maya, modify a scene, or contact a service.
Scene data is collected on Maya's main thread. Generated lights remain native,
editable nodes; only nodes explicitly tagged as ShotLight-owned are replaceable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import traceback
import uuid

from .runtime import Budget, Cancelled, TimedOut, RenderWatchdog


OWNER_ATTRIBUTE = "shotlightOwnerUUID"
RIG_ATTRIBUTE = "shotlightRig"
VERSION_ATTRIBUTE = "shotlightVersion"
VERSION = "0.2.0"
_WATERMARK_SEEN = False  # Conservative for this Maya process after a native license warning.
_LIGHT_TYPES = {"area": "aiAreaLight", "directional": "directionalLight", "skydome": "aiSkyDomeLight"}


class MayaAdapterError(RuntimeError):
    """An actionable host error; no user scene should be discarded to recover."""


def _cmds():
    try:
        import maya.cmds as cmds
    except ImportError as exc:
        raise MayaAdapterError("Open ShotLight inside Maya to inspect and light a shot.") from exc
    return cmds


def _get(cmds, plug, default=None):
    try:
        if cmds.objExists(plug):
            return cmds.getAttr(plug)
    except (RuntimeError, ValueError):
        pass
    return default


def _set(cmds, plug, value):
    if isinstance(value, str) or cmds.getAttr(plug, type=True) == "string":
        if value is None:
            # Maya distinguishes an unset string (None) from an empty string.
            import maya.api.OpenMaya as om
            selection = om.MSelectionList()
            selection.add(plug)
            selection.getPlug(0).setMObject(om.MObject.kNullObj)
        else:
            cmds.setAttr(plug, value, type="string")
    elif isinstance(value, (tuple, list)):
        values = value[0] if len(value) == 1 and isinstance(value[0], (list, tuple)) else value
        cmds.setAttr(plug, *values)
    else:
        cmds.setAttr(plug, value)


@contextlib.contextmanager
def _preserve(cmds, attributes=()):
    """Restore every changed setting even when image capture or rendering fails."""
    time = cmds.currentTime(query=True)
    selection = cmds.ls(selection=True, long=True) or []
    states = {plug: cmds.getAttr(plug) for plug in attributes if cmds.objExists(plug)}
    try:
        yield
    finally:
        restoration_errors = []
        for plug, value in states.items():
            try:
                if cmds.getAttr(plug) != value:
                    _set(cmds, plug, value)
            except Exception as exc:
                restoration_errors.append("{}: {}".format(plug, exc))
        try:
            if cmds.currentTime(query=True) != time:
                cmds.currentTime(time, edit=True)
        except Exception as exc:
            restoration_errors.append("current frame: {}".format(exc))
        try:
            remaining = [node for node in selection if cmds.objExists(node)]
            if (cmds.ls(selection=True, long=True) or []) != remaining:
                cmds.select(remaining, replace=True) if remaining else cmds.select(clear=True)
        except Exception as exc:
            restoration_errors.append("selection: {}".format(exc))
        if restoration_errors:
            raise MayaAdapterError("Could not restore Maya state: " + "; ".join(restoration_errors))


def _long(cmds, name):
    names = cmds.ls(name, long=True) or []
    if len(names) != 1:
        raise MayaAdapterError("Choose an unambiguous scene node: {!r}.".format(name))
    return names[0]


@contextlib.contextmanager
def _temporary_edits(cmds):
    """Keep transient capture operations out of the artist's existing undo stack."""
    enabled = cmds.undoInfo(query=True, state=True)
    if enabled:
        cmds.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        if enabled:
            cmds.undoInfo(stateWithoutFlush=True)


def _camera_shape(cmds, node):
    node = _long(cmds, node)
    if cmds.nodeType(node) == "camera":
        return node
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True, type="camera") or []
    if len(shapes) != 1:
        raise MayaAdapterError("Choose a shot camera, rather than {!r}.".format(node))
    return shapes[0]


def _active_camera(cmds):
    if cmds.about(batch=True):
        return None
    try:
        panel = cmds.getPanel(withFocus=True)
        if panel and cmds.getPanel(typeOf=panel) == "modelPanel":
            camera = _camera_shape(cmds, cmds.modelPanel(panel, query=True, camera=True))
            if not cmds.camera(camera, query=True, startupCamera=True):
                return camera
        # Clicking a Qt tool can take focus away from the model panel. A unique
        # visible production camera is still clear evidence of the intended shot;
        # multiple visible shot cameras must never be reduced to the first one.
        visible = set()
        for candidate in cmds.getPanel(visiblePanels=True) or []:
            if cmds.getPanel(typeOf=candidate) != "modelPanel":
                continue
            camera = _camera_shape(cmds, cmds.modelPanel(candidate, query=True, camera=True))
            if not cmds.camera(camera, query=True, startupCamera=True):
                visible.add(camera)
        if len(visible) == 1:
            return next(iter(visible))
    except (RuntimeError, MayaAdapterError):
        pass
    return None


def _resolve_camera(cmds, camera):
    if camera:
        return _camera_shape(cmds, camera)
    for selected in cmds.ls(selection=True, long=True) or []:
        try:
            return _camera_shape(cmds, selected)
        except MayaAdapterError:
            continue
    all_cameras = cmds.ls(type="camera", long=True) or []
    production = [c for c in all_cameras if not cmds.camera(c, query=True, startupCamera=True)]
    renderable = [c for c in production if _get(cmds, c + ".renderable", False)]
    active = _active_camera(cmds)
    # Artists can inspect a production camera before making it renderable.
    # That visible shot takes precedence over a different saved render camera.
    if active in production:
        return active
    if len(renderable) == 1:
        return renderable[0]
    if len(production) == 1:
        return production[0]
    raise MayaAdapterError("Select your shot camera in Maya, then click Light Shot again; there are {} possible shot cameras.".format(len(renderable or production)))


def selected_camera():
    """The panel requires an explicit camera selection, avoiding the wrong shot."""
    cmds = _cmds()
    for selected in cmds.ls(selection=True, long=True) or []:
        try:
            return _camera_shape(cmds, selected)
        except MayaAdapterError:
            continue
    raise MayaAdapterError("Select your shot camera in Maya, set the timeline playback start and end to your shot, then press Light Shot.")


def _visible(cmds, shape):
    if _get(cmds, shape + ".intermediateObject", False):
        return False
    if not _get(cmds, shape + ".primaryVisibility", True):
        return False
    node = shape
    while node:
        if not _get(cmds, node + ".visibility", True):
            return False
        if _get(cmds, node + ".overrideEnabled", False) and not _get(cmds, node + ".overrideVisibility", True):
            return False
        parent = cmds.listRelatives(node, parent=True, fullPath=True) or []
        node = parent[0] if parent else None
    return True


def _meshes_below(cmds, node, available):
    node = node.split(".", 1)[0]
    if not cmds.objExists(node):
        raise MayaAdapterError("The subject no longer exists: {}.".format(node))
    node = _long(cmds, node)
    if cmds.nodeType(node) == "mesh":
        return [node] if node in available else []
    return [m for m in available if m.startswith(node + "|")]


def _geometry_groups(meshes):
    """Group complete visible hierarchies, never individual meshes by size."""
    groups = {}
    for mesh in meshes:
        root = "|" + mesh.split("|")[1]
        groups.setdefault(root, []).append(mesh)
    return groups


def _available_meshes(cmds):
    result = []
    for mesh in cmds.ls(type="mesh", long=True) or []:
        if not _visible(cmds, mesh):
            continue
        node = mesh
        excluded = False
        while node:
            if _get(cmds, node + "." + RIG_ATTRIBUTE, False):
                excluded = True
                break
            if cmds.nodeType(node) == "transform" and cmds.listRelatives(node, shapes=True, type="camera"):
                excluded = True
                break
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            node = parents[0] if parents else None
        if not excluded:
            result.append(mesh)
    return sorted(result)


def _deformed_geometry_roots(cmds, groups):
    """A skinCluster identifies an animated character more reliably than size."""
    roots = []
    for root, meshes in groups.items():
        for mesh in meshes:
            history = cmds.listHistory(mesh, pruneDagObjects=True) or []
            if history and cmds.ls(history, type="skinCluster"):
                roots.append(root)
                break
    return roots


def _subject_meshes(cmds, subjects, warnings):
    available = _available_meshes(cmds)
    groups = _geometry_groups(available)
    explicit = subjects is not None
    selected = list(subjects or []) if explicit else cmds.ls(selection=True, long=True) or []
    if isinstance(subjects, str):
        selected = [subjects]
    result = set()
    for node in selected:
        found = _meshes_below(cmds, node, available)
        # A control may sit beside limb geometry. Do not stop at that first limb:
        # preserve the entire containing character hierarchy. Explicit mesh/group
        # requests stay authoritative, including the paths used for scene guards.
        if not explicit:
            selected_node = _long(cmds, node.split(".", 1)[0])
            shapes = cmds.listRelatives(selected_node, shapes=True, fullPath=True) or []
            control = cmds.nodeType(selected_node) == "joint" or any(
                cmds.nodeType(shape) in {"nurbsCurve", "locator"} for shape in shapes)
            if control or not found:
                root = "|" + selected_node.split("|")[1]
                found = groups.get(root, found)
        result.update(found)
    if result:
        return sorted(result)
    if explicit:
        raise MayaAdapterError("The chosen subject contains no visible renderable meshes.")
    if selected:
        warnings.append("The current selection contains no visible subject meshes; visible scene geometry was used.")
    if not available:
        raise MayaAdapterError("There are no visible renderable meshes in this shot.")
    rig_roots = _deformed_geometry_roots(cmds, groups)
    if rig_roots:
        warnings.append("Subject was inferred from complete skinned character hierarchies; other visible geometry is provided as environment context.")
        return sorted(mesh for root in rig_roots for mesh in groups[root])
    warnings.append("No skinned character hierarchy was identified. All visible geometry is retained; select a character or object for precise framing.")
    return available


def _radius(bounds):
    return max(1e-6, math.sqrt(sum((bounds[i + 3] - bounds[i]) ** 2 for i in range(3))) / 2.0)


def _union(bounds):
    return [min(b[i] for b in bounds) for i in range(3)] + [max(b[i] for b in bounds) for i in range(3, 6)]


def _camera_info(cmds, shape):
    transform = (cmds.listRelatives(shape, parent=True, fullPath=True) or [shape])[0]
    matrix = cmds.xform(transform, query=True, matrix=True, worldSpace=True)
    def normalize(vector):
        length = math.sqrt(sum(v * v for v in vector))
        return [v / max(length, 1e-12) for v in vector]
    return {
        "name": shape, "transform": transform,
        "position": matrix[12:15],
        "forward": normalize([-v for v in matrix[8:11]]),
        "up": normalize(matrix[4:7]),
        "focal_length": _get(cmds, shape + ".focalLength", 35.0),
        "horizontal_film_aperture": _get(cmds, shape + ".horizontalFilmAperture", 1.417),
        "vertical_film_aperture": _get(cmds, shape + ".verticalFilmAperture", 0.945),
        "orthographic": bool(_get(cmds, shape + ".orthographic", False)),
        "orthographic_width": _get(cmds, shape + ".orthographicWidth", 30.0),
        "film_fit": _get(cmds, shape + ".filmFit", 0),
        "lens_squeeze_ratio": _get(cmds, shape + ".lensSqueezeRatio", 1.0),
    }


def _material_info(cmds, mesh):
    materials = []
    for group in cmds.listConnections(mesh, type="shadingEngine") or []:
        for material in cmds.listConnections(group + ".surfaceShader", source=True, destination=False) or []:
            entry = {"name": material, "type": cmds.nodeType(material)}
            properties = {}
            for attribute in ("color", "diffuse", "transparency", "incandescence", "base", "baseColor",
                              "specular", "specularColor", "specularRoughness", "metalness", "transmission",
                              "subsurface", "subsurfaceColor", "emission", "emissionColor", "opacity"):
                plug = material + "." + attribute
                if cmds.objExists(plug):
                    value = _get(cmds, plug)
                    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
                        value = list(value[0])
                    properties[attribute] = value
            entry["properties"] = properties
            input_nodes = set(cmds.listConnections(material, source=True, destination=False) or [])
            entry["inputs"] = [{"name": node, "type": cmds.nodeType(node),
                                "file": os.path.basename(_get(cmds, node + ".fileTextureName", "") or "")}
                               for node in sorted(input_nodes)[:20]]
            if entry not in materials:
                materials.append(entry)
    return materials[:8]


def _light_info(cmds):
    lights = set(cmds.ls(lights=True, long=True) or [])
    for node_type in ("aiAreaLight", "aiSkyDomeLight", "aiPhotometricLight"):
        try:
            lights.update(cmds.ls(type=node_type, long=True) or [])
        except RuntimeError:
            pass
    result = []
    for light in sorted(lights):
        transform = (cmds.listRelatives(light, parent=True, fullPath=True) or [light])[0]
        matrix = cmds.xform(transform, query=True, matrix=True, worldSpace=True)
        color = _get(cmds, light + ".color", [(1., 1., 1.)])
        if isinstance(color, (list, tuple)) and len(color) == 1:
            color = list(color[0])
        result.append({"name": light, "type": cmds.nodeType(light), "visible": _visible(cmds, light),
                       "position": matrix[12:15], "matrix": matrix,
                       "color": color, "intensity": _get(cmds, light + ".intensity", 1.0),
                       "exposure": _get(cmds, light + ".aiExposure", _get(cmds, light + ".exposure", 0.0)),
                       "normalize": _get(cmds, light + ".aiNormalize", True),
                       "owned": bool(_get(cmds, light + "." + OWNER_ATTRIBUTE, ""))})
    return result


def collect_shot(camera=None, subjects=None, sample_count=3):
    """Return bounded shot metadata; never save or modify animation/materials."""
    cmds = _cmds()
    sample_count = min(7, max(1, int(sample_count)))
    shape = _resolve_camera(cmds, camera)
    warnings = []
    meshes = _subject_meshes(cmds, subjects, warnings)
    subject_groups = _geometry_groups(meshes)
    subject_set = set(meshes)
    environment_groups = _geometry_groups([m for m in _available_meshes(cmds) if m not in subject_set])
    # Supply surrounding geometry without letting a large landscape set the
    # character's lighting scale. Bound the descriptive payload, not subjects.
    environment_roots = sorted(environment_groups)[:64]
    if len(environment_groups) > len(environment_roots):
        warnings.append("Environment descriptions are limited to 64 geometry roots.")
    current = float(cmds.currentTime(query=True))
    start = float(cmds.playbackOptions(query=True, minTime=True))
    end = float(cmds.playbackOptions(query=True, maxTime=True))
    frames = [current] if sample_count == 1 else sorted(set(round(start + (end - start) * i / (sample_count - 1), 4) for i in range(sample_count)))
    per_mesh = {m: [] for m in meshes}
    environment_samples = {root: [] for root in environment_roots}
    samples = []
    with _temporary_edits(cmds), _preserve(cmds):
        for frame in frames:
            cmds.currentTime(frame, edit=True)
            frame_bounds = []
            for mesh in meshes:
                bounds = [float(v) for v in cmds.exactWorldBoundingBox(mesh)]
                if not all(math.isfinite(v) for v in bounds):
                    raise MayaAdapterError("Non-finite geometry bounds: {}.".format(mesh))
                per_mesh[mesh].append(bounds)
                frame_bounds.append(bounds)
            samples.append({"frame": frame, "camera": _camera_info(cmds, shape), "bounds": _union(frame_bounds)})
            for root in environment_roots:
                context_bounds = [float(v) for v in cmds.exactWorldBoundingBox(environment_groups[root])]
                if not all(math.isfinite(v) for v in context_bounds):
                    raise MayaAdapterError("Non-finite environment bounds: {}.".format(root))
                environment_samples[root].append({"frame": frame, "bounds": context_bounds})
    bounds = _union([b for values in per_mesh.values() for b in values])
    center = [(bounds[i] + bounds[i + 3]) / 2 for i in range(3)]
    lights = _light_info(cmds)
    if any(n["visible"] and not n["owned"] for n in lights):
        warnings.append("Existing lights will remain active. ShotLight adds an editable rig without altering them.")
    if len(meshes) > 150:
        warnings.append("Individual subject details are limited to 150 meshes; overall bounds include every subject mesh.")
    detailed_meshes = sorted(meshes, key=lambda mesh: (-_radius(_union(per_mesh[mesh])), mesh))[:150]
    identity_parts = [cmds.file(query=True, sceneName=True) or "Untitled"] + (cmds.ls(shape, uuid=True) or []) + sorted(cmds.ls(meshes, uuid=True) or [])
    scene_identity = hashlib.sha256("|".join(identity_parts).encode("utf-8")).hexdigest()
    return {
        "version": 1, "host": "maya", "renderer": "arnold",
        "scene_identity": scene_identity, "subject_paths": list(meshes),
        "up_axis": cmds.upAxis(query=True, axis=True),
        "unit": cmds.currentUnit(query=True, linear=True),
        "scene_name": os.path.basename(cmds.file(query=True, sceneName=True) or "Untitled"),
        "camera": _camera_info(cmds, shape),
        "subjects": [{"name": m, "bounds": _union(per_mesh[m]), "materials": _material_info(cmds, m)} for m in detailed_meshes],
        "subject_groups": [{"name": root, "mesh_count": len(group),
                            "bounds": _union([b for m in group for b in per_mesh[m]])}
                           for root, group in sorted(subject_groups.items())],
        "environment": [{"name": root, "mesh_count": len(environment_groups[root]),
                         "bounds": _union([sample["bounds"] for sample in environment_samples[root]]),
                         "samples": environment_samples[root]} for root in environment_roots],
        "bounds": bounds, "center": center, "radius": _radius(bounds),
        "frames": frames, "current_frame": current, "samples": samples,
        "existing_lights": lights, "warnings": warnings,
        "render_resolution": [int(_get(cmds, "defaultResolution.width", 1920)), int(_get(cmds, "defaultResolution.height", 1080))],
        "display_aspect_ratio": float(_get(cmds, "defaultResolution.deviceAspectRatio", 16.0 / 9.0)),
    }


def _load_arnold(cmds):
    if not cmds.pluginInfo("mtoa", query=True, loaded=True):
        try:
            cmds.loadPlugin("mtoa", quiet=True)
        except RuntimeError as exc:
            raise MayaAdapterError("Arnold for Maya (mtoa) is required. Enable it in Maya's Plug-in Manager.") from exc


def _tag(cmds, node, owner):
    for attribute, value in ((OWNER_ATTRIBUTE, owner), (VERSION_ATTRIBUTE, VERSION)):
        cmds.addAttr(node, longName=attribute, dataType="string")
        cmds.setAttr(node + "." + attribute, value, type="string")
        cmds.setAttr(node + "." + attribute, lock=True)


@contextlib.contextmanager
def _transaction(cmds, name):
    """Rollback the entire tool edit, including failed prior-rig deletion."""
    if not cmds.undoInfo(query=True, state=True):
        raise MayaAdapterError("Enable Undo in Maya before editing the lighting rig.")
    cmds.undoInfo(openChunk=True, chunkName=name)
    opened = True
    try:
        yield
    except Exception:
        cmds.undoInfo(closeChunk=True)
        opened = False
        # A failed edit with no undoable operations must not undo artist work.
        if cmds.undoInfo(query=True, undoName=True) == name:
            cmds.undo()
        raise
    finally:
        if opened:
            cmds.undoInfo(closeChunk=True)


def _owned_groups(cmds):
    return [node for node in cmds.ls(type="transform", long=True) or []
            if _get(cmds, node + "." + RIG_ATTRIBUTE, False)
            and _get(cmds, node + "." + OWNER_ATTRIBUTE, "")]


def _verify_owned_group(cmds, group):
    group = _long(cmds, group)
    owner = _get(cmds, group + "." + OWNER_ATTRIBUTE, "")
    if not owner or not _get(cmds, group + "." + RIG_ATTRIBUTE, False):
        raise MayaAdapterError("This is not a ShotLight-owned lighting rig: {}.".format(group))
    try:
        uuid.UUID(owner)
    except ValueError as exc:
        raise MayaAdapterError("The lighting rig has an invalid ShotLight ownership tag.") from exc
    for node in cmds.listRelatives(group, allDescendents=True, fullPath=True) or []:
        if _get(cmds, node + "." + OWNER_ATTRIBUTE, "") != owner:
            raise MayaAdapterError("The ShotLight rig contains an unowned node; move it out before replacing the rig: {}.".format(node))
        if cmds.nodeType(node) not in set(_LIGHT_TYPES.values()) | {"transform"}:
            raise MayaAdapterError("The ShotLight rig contains a non-light node: {}.".format(node))
    return group


def _validate_plan(plan, shot):
    from .plan import PlanError, validate_plan
    try:
        return validate_plan(plan, shot)
    except PlanError as exc:
        raise MayaAdapterError(str(exc)) from exc


def _orient(cmds, transform, position, target, up_axis):
    """Set a real transform directly, without an aim constraint or helper nodes."""
    def normalized(v):
        length = math.sqrt(sum(n * n for n in v))
        return [n / length for n in v]
    def cross(a, b):
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    z = normalized([position[i] - target[i] for i in range(3)])
    up = [0., 0., 1.] if up_axis == "z" else [0., 1., 0.]
    x = cross(up, z)
    if sum(v*v for v in x) < 1e-8:
        x = cross([1., 0., 0.], z)
    x = normalized(x)
    y = cross(z, x)
    matrix = x + [0.] + y + [0.] + z + [0.] + list(position) + [1.]
    cmds.xform(transform, matrix=matrix, worldSpace=True)


def apply_plan(plan, shot):
    """Atomically replace only ShotLight-owned native lights, in one undo chunk."""
    plan = _validate_plan(plan, shot)  # Validate everything before the first scene edit.
    cmds = _cmds()
    _load_arnold(cmds)
    old_groups = [_verify_owned_group(cmds, node) for node in _owned_groups(cmds)]
    owner = str(uuid.uuid4())
    created = None
    with _transaction(cmds, "ShotLight: light shot"):
        with _preserve(cmds):
            created = cmds.createNode("transform", name="ShotLight_Rig", skipSelect=True)
            _tag(cmds, created, owner)
            cmds.addAttr(created, longName=RIG_ATTRIBUTE, attributeType="bool", defaultValue=True)
            cmds.setAttr(created + "." + RIG_ATTRIBUTE, lock=True)
            cmds.addAttr(created, longName="shotlightSummary", dataType="string")
            cmds.setAttr(created + ".shotlightSummary", str(plan.get("summary", ""))[:4000], type="string")
            for index, light in enumerate(plan["lights"]):
                safe_name = re.sub(r"[^A-Za-z0-9_]", "_", str(light.get("name", light["role"])))[:48].strip("_") or "Light"
                name = "SL_{:02d}_{}".format(index + 1, safe_name)
                import mtoa.utils
                # asLight registers default illumination/light-link connections;
                # createNode alone makes an editable light that may not illuminate.
                shape, transform = mtoa.utils.createLocatorWithName(_LIGHT_TYPES[light["type"]], name, asLight=True)
                transform = cmds.parent(transform, created)[0]
                shape = (cmds.listRelatives(transform, shapes=True, fullPath=True) or [])[0]
                _tag(cmds, transform, owner)
                _tag(cmds, shape, owner)
                if light["type"] != "skydome":
                    _orient(cmds, transform, light["position"], light["target"], shot.get("up_axis", "y"))
                _set(cmds, shape + ".color", light["color"])
                _set(cmds, shape + ".intensity", 1.0)
                # Use actual renderer shadows; light size/placement controls
                # softness. Never paint shadows or change artist mesh settings.
                for attribute, value in (("aiCastShadows", True), ("aiShadowDensity", 1.0),
                                         ("aiShadowColor", [0.0, 0.0, 0.0])):
                    if cmds.objExists(shape + "." + attribute):
                        _set(cmds, shape + "." + attribute, value)
                exposure_attribute = ".aiExposure" if cmds.objExists(shape + ".aiExposure") else ".exposure"
                if not cmds.objExists(shape + exposure_attribute):
                    raise MayaAdapterError("Arnold exposure attribute is missing from {}.".format(shape))
                _set(cmds, shape + exposure_attribute, float(light["exposure"]))
                if light["type"] == "area":
                    _set(cmds, transform + ".scaleX", float(light["size"][0]) / 2.0)
                    _set(cmds, transform + ".scaleY", float(light["size"][1]) / 2.0)
                    if cmds.objExists(shape + ".aiNormalize"):
                        _set(cmds, shape + ".aiNormalize", True)
                    if cmds.objExists(shape + ".aiTranslator"):
                        _set(cmds, shape + ".aiTranslator", "quad")
                if light["type"] == "skydome" and cmds.objExists(shape + ".aiCamera"):
                    _set(cmds, shape + ".aiCamera", 0.0)
            # The previous valid rig is retained until every new light succeeds.
            if old_groups:
                cmds.delete(old_groups)
            return _long(cmds, created)


def remove_rig(group=None):
    """Remove owned lights only. Refuse to remove any group containing user nodes."""
    cmds = _cmds()
    groups = [_verify_owned_group(cmds, group)] if group else [_verify_owned_group(cmds, node) for node in _owned_groups(cmds)]
    if not groups:
        return []
    with _transaction(cmds, "ShotLight: remove rig"):
        with _preserve(cmds):
            cmds.delete(groups)
    return groups


def _capture_viewport(cmds, shot, folder):
    import maya.api.OpenMaya as om
    import maya.api.OpenMayaUI as omui
    shape = _camera_shape(cmds, shot["camera"]["name"])
    panel = cmds.getPanel(withFocus=True)
    if not panel or cmds.getPanel(typeOf=panel) != "modelPanel":
        visible = [p for p in cmds.getPanel(visiblePanels=True) or [] if cmds.getPanel(typeOf=p) == "modelPanel"]
        panel = visible[0] if visible else None
    if not panel:
        raise MayaAdapterError("Open a Maya viewport to capture the shot, or use Arnold previews.")
    editor = cmds.modelPanel(panel, query=True, modelEditor=True)
    attrs = ("displayLights", "displayAppearance", "displayTextures", "polymeshes", "nurbsCurves", "grid", "hud", "cameras", "lights", "joints", "locators", "selectionHiliteDisplay")
    flags = {a: cmds.modelEditor(editor, query=True, **{a: True}) for a in attrs}
    old_camera = cmds.modelPanel(panel, query=True, camera=True)
    previews = []
    try:
        cmds.modelPanel(panel, edit=True, camera=shape)
        cmds.modelEditor(editor, edit=True, displayLights="default", displayAppearance="smoothShaded", displayTextures=True,
                         polymeshes=True, nurbsCurves=False, grid=False, hud=False, cameras=False, lights=False,
                         joints=False, locators=False, selectionHiliteDisplay=False)
        cmds.select(clear=True)
        for index, frame in enumerate(shot["frames"]):
            cmds.currentTime(frame, edit=True)
            cmds.refresh(force=True)
            path = folder / "shot_{:02d}.png".format(index)
            # readColorBuffer is more deterministic than platform movie codecs.
            view = omui.M3dView.getM3dViewFromModelPanel(panel)
            image = om.MImage()
            view.readColorBuffer(image, True)
            image.writeToFile(str(path), "png")
            if not path.is_file() or not path.stat().st_size:
                raise MayaAdapterError("Maya did not write the viewport preview.")
            previews.append({"frame": float(frame), "path": str(path)})
    finally:
        cmds.modelPanel(panel, edit=True, camera=old_camera)
        cmds.modelEditor(editor, edit=True, **flags)
    return previews


def _reevaluate_export(cmds, errors):
    """Refresh valid Maya mesh data after a failed native export, without editing it."""
    import maya.api.OpenMaya as om
    meshes = set()
    for message in errors:
        match = re.search(r"\[polymesh\]\s+(.+?):\s*incorrectly defined UV coordinates", message)
        if match:
            meshes.add(match.group(1).replace("/", "|"))
    if not cmds.about(batch=True):
        cmds.refresh(force=True)
    for mesh in sorted(meshes):
        if not cmds.objExists(mesh):
            raise MayaAdapterError("Arnold reported invalid UV data on a missing mesh: " + mesh)
        # Read the evaluated plug, rather than stale shape data after a frame
        # change. Do not dirty meshes or change their UV sets/reference data.
        selection = om.MSelectionList()
        selection.add(mesh + ".outMesh")
        data = selection.getPlug(0).asMObject()
        fn = om.MFnMesh(data)
        uv_sets = fn.getUVSetNames()
        if not uv_sets:
            raise MayaAdapterError("Repair missing UV data on %s before rendering. ShotLight has not changed the mesh." % mesh)
        uv_set = uv_sets[0] if len(uv_sets) == 1 else cmds.polyUVSet(mesh, query=True, currentUVSet=True)[0]
        u, v = [list(values) for values in fn.getUVs(uv_set)]
        counts, indices = [list(values) for values in fn.getAssignedUVs(uv_set)]
        if (not u or len(u) != len(v) or len(counts) != fn.numPolygons or
                not all(counts) or sum(counts) != len(indices) or
                any(index < 0 or index >= len(u) for index in indices) or
                not all(math.isfinite(value) for value in list(u) + list(v))):
            raise MayaAdapterError("Repair UV set %s on %s before rendering. ShotLight has not changed the mesh." % (uv_set, mesh))


def _render_arnold_frame(cmds, arnold, camera, width, height, frame, index, folder, control, watchdog):
    """One bounded recovery attempt; capture native errors that Maya otherwise hides."""
    import maya.api.OpenMaya as om
    diagnostic = folder / ("render_%02d.json" % index)
    report = {"frame": float(frame), "camera": camera, "attempts": []}
    for attempt in range(2):
        control.check()
        if watchdog.error:
            raise watchdog.error
        _set(cmds, "defaultRenderGlobals.imageFilePrefix", str(folder / ("shot_%02d_try%d" % (index, attempt))))
        before = {str(p) for p in folder.glob("*.png")}
        errors = []
        messages = []
        def output(message, kind, unused):
            messages.append(str(message))
            del messages[:-20]
            if kind == om.MCommandMessage.kError:
                errors.append(str(message))
                del errors[:-20]
        callback = om.MCommandMessage.addCommandOutputCallback(output)
        started = time.monotonic()
        failure = None
        trace = None
        try:
            if cmds.about(batch=True):
                cmds.arnoldRender(batch=True, camera=camera, width=width, height=height)
            else:
                cmds.arnoldRender(seq="", cam=camera, w=width, h=height, srv="")
        except (Cancelled, TimedOut) as exc:
            failure, trace = str(exc), traceback.format_exc()
            raise
        except RuntimeError as exc:
            failure, trace = str(exc), traceback.format_exc()
        finally:
            om.MMessage.removeCallback(callback)
            record = {"attempt": attempt + 1, "seconds": round(time.monotonic() - started, 2),
                      "native_errors": errors, "messages": messages, "exception": failure, "traceback": trace}
            report["attempts"].append(record)
            diagnostic.write_text(json.dumps(report, indent=2), encoding="utf-8")
        control.check()
        if watchdog.error:
            raise watchdog.error
        images = [p for p in folder.glob("*.png") if str(p) not in before]
        if not failure and not errors and len(images) == 1 and images[0].stat().st_size:
            record["completed"] = True
            diagnostic.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return images[0]
        detail = "\n".join(errors) or failure or "Arnold did not write a unique PNG preview."
        if attempt == 0 and not (arnold.AiArnoldIsActive() and arnold.AiRenderIsAnyActive()):
            control.status("Recovering Arnold export at frame %g · retry 1/1…" % frame)
            try:
                _reevaluate_export(cmds, errors)
            except (RuntimeError, ValueError) as exc:
                detail += "\n" + str(exc)
            else:
                continue
        raise MayaAdapterError("Arnold could not render frame %g through %s.\n%s\nDiagnostic: %s" %
                               (frame, camera.split("|")[-1], detail[-3000:], diagnostic))


def _capture_arnold(cmds, shot, folder, quick=False, control=None):
    global _WATERMARK_SEEN
    _load_arnold(cmds)
    import mtoa.core
    import arnold
    mtoa.core.createOptions()
    control = control or Budget(seconds=90)
    control.check()
    if arnold.AiArnoldIsActive() and arnold.AiRenderIsAnyActive():
        raise MayaAdapterError("Another Arnold render is active. Stop it before starting a ShotLight preview.")
    camera = _camera_shape(cmds, shot["camera"]["name"])
    aspect = float(_get(cmds, "defaultResolution.deviceAspectRatio", 16.0 / 9.0))
    if not math.isfinite(aspect) or not 0.05 <= aspect <= 20:
        raise MayaAdapterError("The shot has an invalid render aspect ratio.")
    edge = 384 if quick else 640
    width, height = (edge, max(32, int(round(edge / aspect)))) if aspect >= 1 else (max(32, int(round(edge * aspect))), edge)
    settings = {
        "defaultRenderGlobals.currentRenderer": "arnold",
        "defaultRenderGlobals.imageFilePrefix": str(folder / "shot"),
        "defaultRenderGlobals.animation": False,
        "defaultRenderGlobals.outFormatControl": 0,
        "defaultRenderGlobals.putFrameBeforeExt": True,
        "defaultRenderGlobals.periodInExt": 1,
        "defaultRenderGlobals.useRenderRegion": False,
        "defaultResolution.width": width,
        "defaultResolution.height": height,
        "defaultResolution.pixelAspect": 1.0,
        "defaultResolution.deviceAspectRatio": width / float(height),
        "defaultArnoldDriver.aiTranslator": "png",
        # PNGs must match the scene's configured display/view transform.
        # Changing aiTranslator alone only updates this via a GUI scriptJob.
        "defaultArnoldDriver.colorManagement": 1,
        "defaultArnoldDriver.prefix": "",
        "defaultArnoldRenderOptions.AASamples": 2 if quick else 3,
        "defaultArnoldRenderOptions.GIDiffuseSamples": 1,
        "defaultArnoldRenderOptions.GISpecularSamples": 1,
        "defaultArnoldRenderOptions.GITransmissionSamples": 1,
        "defaultArnoldRenderOptions.GISssSamples": 1,
        "defaultArnoldRenderOptions.GIVolumeSamples": 1,
        "defaultArnoldRenderOptions.enableAdaptiveSampling": False,
        "defaultArnoldRenderOptions.AASamplesMax": 2 if quick else 3,
        "defaultArnoldRenderOptions.aovMode": 0,
        # A preview must not generate texture caches beside artist assets.
        "defaultArnoldRenderOptions.autotx": False,
        # Allow Arnold's documented watermarked evaluation output for previews.
        # This does not skip license checks or suppress the watermark.
        "defaultArnoldRenderOptions.abortOnLicenseFail": False,
    }
    # Preserve output paths and settings; never set a scene file or save preferences.
    camera_plugs = [c + ".renderable" for c in cmds.ls(type="camera", long=True) or []]
    with _preserve(cmds, list(settings) + camera_plugs):
        for plug, value in settings.items():
            if cmds.objExists(plug):
                if cmds.getAttr(plug, lock=True):
                    raise MayaAdapterError("Unlock {} before creating Arnold previews.".format(plug))
                _set(cmds, plug, value)
        for plug in camera_plugs:
            # Do not unlock or disconnect animation on camera attributes.
            if not cmds.getAttr(plug, lock=True) and not cmds.listConnections(plug, source=True, destination=False):
                _set(cmds, plug, plug == camera + ".renderable")
        previews = []
        for index, frame in enumerate(shot["frames"]):
            control.check()
            limit = min(15 if quick else 20, control.remaining)
            control.status("Rendering frame %g (%d/%d) · %.0fs limit…" % (frame, index + 1, len(shot["frames"]), limit))
            cmds.currentTime(frame, edit=True)
            def abort_owned_render():
                # Arnold's documented nonblocking abort; no off-thread Maya API.
                if arnold.AiArnoldIsActive() and arnold.AiRenderIsAnyActive():
                    arnold.AiRenderAbort(None, arnold.AI_NON_BLOCKING)
            with RenderWatchdog(control, limit, abort_owned_render) as watchdog:
                image = _render_arnold_frame(cmds, arnold, camera, width, height, frame, index, folder, control, watchdog)
            diagnostic = json.loads((folder / ("render_%02d.json" % index)).read_text())
            watermarked = any("watermark" in message.lower() for attempt in diagnostic["attempts"] for message in attempt["messages"])
            _WATERMARK_SEEN = _WATERMARK_SEEN or watermarked
            previews.append({"frame": float(frame), "path": str(image), "renderer": "arnold",
                             "analysis_usable": not _WATERMARK_SEEN,
                             "license_note": "Arnold reported a license watermark; automatic exposure checks are unavailable." if _WATERMARK_SEEN else None})
        return previews


def capture_previews(shot, output_dir, render=False, quick=False, control=None):
    """Capture actual scene pixels, without touching shaders, poses or saved files.

    Interactive capture uses temporary neutral viewport lighting for local analysis.
    Rendered capture uses the scene's real Arnold lighting. Arnold may watermark
    batch previews when its separate render license is unavailable.
    """
    cmds = _cmds()
    folder = Path(output_dir).expanduser().resolve() / ("capture_" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    with _temporary_edits(cmds), _preserve(cmds):
        return _capture_arnold(cmds, shot, folder, quick=quick, control=control) if render or cmds.about(batch=True) else _capture_viewport(cmds, shot, folder)


def render_previews(shot, output_dir):
    return capture_previews(shot, output_dir, render=True)
