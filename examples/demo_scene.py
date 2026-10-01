"""Build and light a synthetic Maya scene in a SEPARATE mayapy process.

Never execute this example inside a running artist scene: it starts a new file.
It writes only under the supplied output directory. No external assets needed.
"""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_scene(cmds):
    from shotlight import maya_adapter
    cmds.file(new=True, force=True)
    cmds.currentUnit(linear="cm", time="film")
    maya_adapter._load_arnold(cmds)
    hero = cmds.group(empty=True, name="RobotToy")

    def material(name, colour, roughness=.35, metallic=0):
        node = cmds.shadingNode("aiStandardSurface", asShader=True, name=name)
        cmds.setAttr(node + ".baseColor", *colour, type="double3")
        cmds.setAttr(node + ".base", 1)
        cmds.setAttr(node + ".specularRoughness", roughness)
        cmds.setAttr(node + ".metalness", metallic)
        group = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
        cmds.connectAttr(node + ".outColor", group + ".surfaceShader", force=True)
        return group

    mint = material("MintCeramic", (.19, .57, .48), .31)
    cream = material("Porcelain", (.72, .69, .62), .28)
    dark = material("DarkVisor", (.018, .023, .03), .22)
    amber = material("AmberEyes", (.9, .31, .055), .32)
    ground = material("WarmGround", (.19, .16, .13), .7)

    def sphere(name, center, scale, shader):
        mesh = cmds.polySphere(name=name, radius=1, subdivisionsX=32, subdivisionsY=20)[0]
        cmds.xform(mesh, translation=center, scale=scale)
        cmds.parent(mesh, hero)
        cmds.sets(mesh, edit=True, forceElement=shader)
        return mesh

    sphere("RoundedBody", (0, 76, 0), (39, 44, 28), mint)
    sphere("PorcelainHead", (0, 140, 0), (43, 33, 32), cream)
    sphere("Visor", (0, 144, 28), (31, 17, 8), dark)
    sphere("EyeLeft", (-13, 146, 35), (5.5, 6.5, 3), amber)
    sphere("EyeRight", (13, 146, 35), (5.5, 6.5, 3), amber)
    sphere("LeftArm", (-47, 79, 0), (10, 27, 12), cream)
    sphere("RightArm", (47, 79, 0), (10, 27, 12), cream)
    sphere("LeftFoot", (-19, 19, 5), (16, 18, 22), dark)
    sphere("RightFoot", (19, 19, 5), (16, 18, 22), dark)
    sphere("ChestButton", (0, 81, 27), (8, 8, 3), amber)
    floor = cmds.polyPlane(name="StudioFloor", width=4000, height=4000)[0]
    cmds.sets(floor, edit=True, forceElement=ground)
    camera, shape = cmds.camera(name="ShotCamera", focalLength=65)
    cmds.xform(camera, translation=(265, 210, 560))
    target = cmds.spaceLocator(name="CameraAim")[0]
    cmds.xform(target, translation=(0, 88, 0))
    constraint = cmds.aimConstraint(target, camera, aimVector=(0, 0, -1), upVector=(0, 1, 0), worldUpType="scene")
    cmds.delete(constraint, target)
    for other in cmds.ls(type="camera"):
        cmds.setAttr(other + ".renderable", other == shape)
    # Existing neutral environment gives headless analysis a readable baseline.
    import mtoa.utils
    base, base_transform = mtoa.utils.createLocatorWithName(
        "aiSkyDomeLight", "ArtistBaselineEnvironment", asLight=True)
    cmds.setAttr(base + ".color", 1, 1, 1, type="double3")
    cmds.setAttr(base + ".intensity", 1)
    cmds.setAttr(base + ".aiExposure", -2)
    if cmds.objExists(base + ".aiCamera"):
        cmds.setAttr(base + ".aiCamera", 0)
    cmds.sets(base, edit=True, forceElement="defaultLightSet")
    for frame, x, yaw in ((1, -12, -12), (12, 0, 0), (24, 12, 12)):
        cmds.setKeyframe(hero, attribute="translateX", time=frame, value=x)
        cmds.setKeyframe(hero, attribute="rotateY", time=frame, value=yaw)
    cmds.playbackOptions(minTime=1, maxTime=24)
    cmds.currentTime(12)
    cmds.select(hero)
    return camera, hero


def preservation_data(cmds, camera):
    curves = cmds.ls(type="animCurve") or []
    return {
        "curves": {node: [cmds.keyframe(node, query=True, timeChange=True),
                           cmds.keyframe(node, query=True, valueChange=True)] for node in curves},
        "materials": sorted(cmds.ls(materials=True) or []),
        "baseline_exposure": cmds.getAttr("ArtistBaselineEnvironmentShape.aiExposure"),
        "time": cmds.currentTime(query=True),
        "selection": cmds.ls(selection=True, long=True),
        "camera_matrix": cmds.xform(camera, query=True, worldSpace=True, matrix=True),
    }


def main():
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--output", required=True)
    args_parser.add_argument("--build-only", action="store_true")
    args = args_parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    import maya.standalone
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds
    from shotlight import maya_adapter, workflow
    try:
        camera, hero = build_scene(cmds)
        cmds.file(rename=str(output / "demo_before.ma"))
        cmds.file(save=True, type="mayaAscii")
        before_data = preservation_data(cmds, camera)
        shot = maya_adapter.collect_shot(camera=camera, subjects=[hero], sample_count=3)
        (output / "shot.json").write_text(json.dumps(shot, indent=2), encoding="utf-8")
        if args.build_only:
            previews = maya_adapter.capture_previews(shot, str(output / "baseline"), render=True)
            (output / "previews.json").write_text(json.dumps(previews, indent=2), encoding="utf-8")
            print("BUILD_ONLY_COMPLETE", flush=True)
        else:
            result = workflow.fast_light_shot(
                maya_adapter, output, config={},
                status=lambda text: print("SHOTLIGHT: " + text, flush=True),
                camera=camera, subjects=[hero])
            if before_data != preservation_data(cmds, camera):
                raise RuntimeError("Demo scene preservation check failed.")
            cmds.file(rename=str(output / "demo_lit.ma"))
            cmds.file(save=True, type="mayaAscii")
            cmds.file(new=True, force=True)
            cmds.file(str(output / "demo_lit.ma"), open=True, force=True)
            if len(maya_adapter._owned_groups(cmds)) != 1:
                raise RuntimeError("Demo editable rig did not persist after reopen.")
            print("DEMO_COMPLETE " + json.dumps({"lights": len(result["plan"]["lights"]),
                "refined": result["refined"], "warning": result["warning"],
                "seconds": result["seconds"], "preservation": True, "reopened": True}), flush=True)
    finally:
        maya.standalone.uninitialize()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        sys.stdout.flush()
        # Some Maya shutdown paths otherwise return exit 0 after a traceback.
        os._exit(1)
