"""Native integration tests. Run with Maya 2025 mayapy, on a fresh scratch scene.

Normal Python test discovery skips this module; no running artist scene is used.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    import maya.cmds as cmds
    MAYA_AVAILABLE = bool(hasattr(cmds, "file"))
except ImportError:
    MAYA_AVAILABLE = False


@unittest.skipUnless(MAYA_AVAILABLE, "Maya standalone integration test")
class MayaAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1]))
        from shotlight import maya_adapter
        cls.adapter = maya_adapter
        cls.adapter._load_arnold(cmds)

    def setUp(self):
        cmds.file(new=True, force=True)
        self.adapter._WATERMARK_SEEN = False
        cmds.undoInfo(state=True)
        self.subject = cmds.group(empty=True, name="Subject")
        self.mesh, _ = cmds.polySphere(name="Head", radius=2.0)
        cmds.parent(self.mesh, self.subject)
        self.control = cmds.circle(name="Control", radius=30.0)[0]
        cmds.parent(self.control, self.subject)
        cmds.setKeyframe(self.mesh, attribute="translateX", time=1, value=-2)
        cmds.setKeyframe(self.mesh, attribute="translateX", time=12, value=3)
        self.camera, self.camera_shape = cmds.camera(name="ShotCamera")
        cmds.xform(self.camera, translation=(0, 1, 14))
        cmds.setAttr(self.camera_shape + ".renderable", True)
        self.existing = cmds.directionalLight(name="ArtistKey")
        cmds.setAttr(self.existing + ".intensity", 0.75)
        cmds.playbackOptions(minTime=1, maxTime=12)
        cmds.currentTime(7)
        cmds.select(self.control)
        cmds.flushUndo()

    def plan(self):
        base = {"name": "AI_key", "role": "key", "type": "area",
                "position": [5, 8, 9], "target": [0, 0, 0], "size": [4, 3],
                "color": [1, 0.9, 0.8], "exposure": 8.0}
        fill = dict(base, name="Fill", role="fill", type="directional", position=[-5, 3, 7], exposure=0.0)
        environment = dict(base, name="Environment", role="environment", type="skydome", exposure=-4.0)
        return {"summary": "Native integration test lighting", "look_description": "soft", "confidence": .8,
                "lights": [base, fill, environment]}

    def unchanged_data(self):
        curves = cmds.ls(type="animCurve") or []
        return {"curves": {n: [cmds.keyframe(n, query=True, timeChange=True), cmds.keyframe(n, query=True, valueChange=True)] for n in curves},
                "materials": sorted(cmds.ls(materials=True) or []),
                "existing_intensity": cmds.getAttr(self.existing + ".intensity"),
                "frame": cmds.currentTime(query=True), "selection": cmds.ls(selection=True, long=True)}

    def test_local_png_roi_excludes_bright_background_and_checks_dark_subject(self):
        from shotlight import planner
        from PySide6.QtGui import QImage, QColor
        image = QImage(100, 100, QImage.Format_RGBA8888)
        image.fill(QColor(255,255,255,255))
        for y in range(25,76):
            for x in range(25,76):
                image.setPixelColor(x,y,QColor(20,20,20,255))
        shot = {"center":[0,0,0],"radius":2,"bounds":[-1,-1,-1,1,1,1],"frames":[1],
                "camera":{"position":[0,0,10],"forward":[0,0,-1],"up":[0,1,0],
                          "orthographic":True,"orthographic_width":4}}
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/"local.png")
            self.assertTrue(image.save(path))
            previews=[{"frame":1,"path":path}]
            measurements=planner.measure_previews(shot,previews)
            self.assertLess(measurements[0]["p90"],.1)
            plan=planner.generate_versions(dict(shot,unit="cm"))[0]["plan"]
            adjusted=planner.analyze(shot,previews,previous_plan=plan)
            self.assertGreater(adjusted["lights"][0]["exposure"],plan["lights"][0]["exposure"])
            with self.assertRaisesRegex(planner.PlannerError,"watermark"):
                planner.measure_previews(shot,[dict(previews[0],analysis_usable=False)])
            with self.assertRaises(planner.PlannerError):
                planner.measure_previews(shot,[{"frame":1,"path":str(Path(folder)/"missing.png")}])

    def test_watermarked_native_preview_is_never_exposure_approved(self):
        import maya.api.OpenMaya as om
        shot = self.adapter.collect_shot()
        def render(**kwargs):
            om.MGlobal.displayWarning("rendering with watermarks because of failed authorization")
            self._write_mock_preview()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds,"arnoldRender",side_effect=render):
            previews = self.adapter.capture_previews(shot,folder,render=True,quick=True)
        self.assertTrue(all(not p["analysis_usable"] for p in previews))

    def test_prior_watermark_warning_remains_authoritative_when_arnold_goes_quiet(self):
        shot = self.adapter.collect_shot()
        self.adapter._WATERMARK_SEEN = True
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds,"arnoldRender",side_effect=lambda **kwargs:self._write_mock_preview()):
            previews = self.adapter.capture_previews(shot,folder,render=True,quick=True)
        self.assertTrue(all(not p["analysis_usable"] for p in previews))

    def test_collect_restores_state_and_uses_mesh_animation_bounds(self):
        before = self.unchanged_data()
        undo_before = cmds.undoInfo(query=True, undoName=True)
        shot = self.adapter.collect_shot(sample_count=3)
        self.assertEqual(before, self.unchanged_data())
        self.assertEqual(shot["frames"], [1.0, 6.5, 12.0])
        self.assertLess(shot["radius"], 10)  # Thirty-unit control is excluded.
        self.assertLessEqual(shot["bounds"][0], -3.9)
        self.assertGreaterEqual(shot["bounds"][3], 4.9)
        self.assertEqual(shot["camera"]["name"], cmds.ls(self.camera_shape, long=True)[0])
        self.assertEqual(undo_before, cmds.undoInfo(query=True, undoName=True))
        self.assertEqual(shot["subject_paths"], [cmds.ls(self.mesh, dagObjects=True, shapes=True, long=True)[0]])
        json.dumps(shot)

    def test_tiny_teeth_do_not_remove_large_body_from_unselected_subject(self):
        body = cmds.polySphere(name="Body", radius=100.0)[0]
        cmds.parent(body, self.subject)
        for index in range(12):
            tooth = cmds.polySphere(name="Tooth{:02d}".format(index), radius=.03)[0]
            cmds.parent(tooth, self.subject)
        cmds.select(clear=True)
        shot = self.adapter.collect_shot(sample_count=1)
        shape = cmds.listRelatives(body, shapes=True, noIntermediate=True, fullPath=True)[0]
        self.assertIn(shape, shot["subject_paths"])
        self.assertLessEqual(shot["bounds"][0], -99.9)
        self.assertGreaterEqual(shot["bounds"][3], 99.9)
        self.assertEqual(shot["subject_groups"][0]["mesh_count"], 14)

    def test_description_limit_keeps_large_body_and_all_subject_paths(self):
        body = cmds.polyCube(name="ZZLargeBody", width=80, height=100, depth=40)[0]
        cmds.parent(body, self.subject)
        for index in range(152):
            detail = cmds.polyCube(name="A_TinyDetail{:03d}".format(index), width=.02, height=.02, depth=.02)[0]
            cmds.parent(detail, self.subject)
        shot = self.adapter.collect_shot(subjects=[self.subject], sample_count=1)
        body_shape = cmds.listRelatives(body, shapes=True, fullPath=True)[0]
        self.assertEqual(len(shot["subjects"]), 150)
        self.assertEqual(len(shot["subject_paths"]), 154)
        self.assertEqual(shot["subjects"][0]["name"], body_shape)
        self.assertIn(body_shape, shot["subject_paths"])

    def test_skinned_character_retains_whole_root_and_separates_environment(self):
        body = cmds.polySphere(name="Body", radius=30.0)[0]
        cmds.parent(body, self.subject)
        cmds.select(clear=True)
        joint = cmds.joint(name="BodyJoint")
        cmds.parent(joint, self.subject)
        cmds.skinCluster(joint, body, toSelectedBones=True, normalizeWeights=1)
        for index in range(12):
            tooth = cmds.polySphere(name="Tooth{:02d}".format(index), radius=.03)[0]
            cmds.parent(tooth, self.subject)
        floor = cmds.polyPlane(name="Landscape", width=10000, height=10000)[0]
        cmds.select(clear=True)
        before = self.unchanged_data()
        shot = self.adapter.collect_shot(sample_count=3)
        body_shape = cmds.listRelatives(body, shapes=True, noIntermediate=True, fullPath=True)[0]
        floor_shape = cmds.listRelatives(floor, shapes=True, fullPath=True)[0]
        self.assertIn(body_shape, shot["subject_paths"])
        self.assertNotIn(floor_shape, shot["subject_paths"])
        self.assertEqual(len(shot["subject_paths"]), 14)
        self.assertLess(shot["radius"], 60)
        self.assertEqual(shot["environment"][0]["name"], "|Landscape")
        self.assertEqual(shot["environment"][0]["mesh_count"], 1)
        self.assertGreaterEqual(shot["environment"][0]["bounds"][3], 4999)
        self.assertEqual(len(shot["environment"][0]["samples"]), 3)
        self.assertEqual(before, self.unchanged_data())

    def test_limb_control_infers_whole_character_but_explicit_mesh_stays_precise(self):
        limb = cmds.group(empty=True, name="Limb", parent=self.subject)
        limb_mesh = cmds.polySphere(name="LimbGeometry", radius=.1)[0]
        cmds.parent(limb_mesh, limb)
        cmds.parent(self.control, limb)
        cmds.select(self.control, replace=True)
        inferred = self.adapter.collect_shot(sample_count=1)
        head_shape = cmds.listRelatives(self.mesh, shapes=True, fullPath=True)[0]
        limb_shape = cmds.listRelatives(limb_mesh, shapes=True, fullPath=True)[0]
        self.assertEqual(set(inferred["subject_paths"]), {head_shape, limb_shape})
        explicit = self.adapter.collect_shot(subjects=[limb_mesh], sample_count=1)
        self.assertEqual(explicit["subject_paths"], [limb_shape])
        self.assertEqual(explicit["environment"][0]["mesh_count"], 1)

    def test_native_rig_replacement_and_undo_preserve_artist_nodes(self):
        shot = self.adapter.collect_shot()
        before = self.unchanged_data()
        first = self.adapter.apply_plan(self.plan(), shot)
        first_owner = cmds.getAttr(first + "." + self.adapter.OWNER_ATTRIBUTE)
        shapes = [n for n in cmds.listRelatives(first, allDescendents=True, fullPath=True) or [] if cmds.nodeType(n) != "transform"]
        self.assertEqual({cmds.nodeType(s) for s in shapes}, {"aiAreaLight", "directionalLight", "aiSkyDomeLight"})
        for shape in shapes:
            if cmds.objExists(shape + ".aiCastShadows"):
                self.assertTrue(cmds.getAttr(shape + ".aiCastShadows"))
                self.assertEqual(cmds.getAttr(shape + ".aiShadowDensity"), 1.0)
        self.assertTrue(all(cmds.sets(s, isMember="defaultLightSet") or
                            cmds.sets(cmds.listRelatives(s, parent=True, fullPath=True)[0], isMember="defaultLightSet")
                            for s in shapes))
        self.assertEqual(before, self.unchanged_data())
        second = self.adapter.apply_plan(self.plan(), shot)
        self.assertEqual(len(self.adapter._owned_groups(cmds)), 1)
        self.assertNotEqual(cmds.getAttr(second + "." + self.adapter.OWNER_ATTRIBUTE), first_owner)
        cmds.undo()
        restored = self.adapter._owned_groups(cmds)
        self.assertEqual(len(restored), 1)
        self.assertEqual(cmds.getAttr(restored[0] + "." + self.adapter.OWNER_ATTRIBUTE), first_owner)
        self.assertEqual(before, self.unchanged_data())

    def test_selected_camera_resolves_two_production_cameras_without_scene_edits(self):
        alternate, alternate_shape = cmds.camera(name="AlternateShotCamera")
        cmds.xform(alternate, translation=(8, 2, 15))
        cmds.setAttr(alternate_shape + ".renderable", True)
        # Two renderable production cameras must not be silently guessed.
        cmds.select(self.control, replace=True)
        with self.assertRaises(self.adapter.MayaAdapterError):
            self.adapter.collect_shot()
        for selected in (alternate, alternate_shape):
            with self.subTest(selected=selected):
                cmds.select(selected, replace=True)
                before = self.unchanged_data()
                undo_before = cmds.undoInfo(query=True, undoName=True)
                shot = self.adapter.collect_shot(sample_count=3)
                self.assertEqual(shot["camera"]["name"], cmds.ls(alternate_shape, long=True)[0])
                self.assertEqual(shot["camera"]["position"], [8., 2., 15.])
                self.assertEqual(before, self.unchanged_data())
                self.assertEqual(undo_before, cmds.undoInfo(query=True, undoName=True))

    def test_active_production_camera_precedes_different_sole_renderable_camera(self):
        alternate, alternate_shape = cmds.camera(name="VisibleShotCamera")
        cmds.setAttr(alternate_shape + ".renderable", False)
        alternate_path = cmds.ls(alternate_shape, long=True)[0]
        original_path = cmds.ls(self.camera_shape, long=True)[0]
        cmds.select(self.control, replace=True)
        before = self.unchanged_data()
        with mock.patch.object(self.adapter, "_active_camera", return_value=alternate_path):
            self.assertEqual(self.adapter.collect_shot(sample_count=1)["camera"]["name"], alternate_path)
            cmds.select(self.camera, replace=True)
            self.assertEqual(self.adapter.collect_shot(sample_count=1)["camera"]["name"], original_path)
        cmds.select(self.control, replace=True)
        startup = cmds.ls("perspShape", long=True)[0]
        with mock.patch.object(self.adapter, "_active_camera", return_value=startup):
            self.assertEqual(self.adapter.collect_shot(sample_count=1)["camera"]["name"], original_path)
        self.assertEqual(before, self.unchanged_data())

    def test_unfocused_tool_uses_unique_visible_production_camera_without_guessing(self):
        alternate, alternate_shape = cmds.camera(name="VisibleShotCamera")
        alternate_path = cmds.ls(alternate_shape, long=True)[0]
        original_path = cmds.ls(self.camera_shape, long=True)[0]
        panels = {"perspectivePanel": "persp", "shotPanel": alternate}

        def panel_inventory(**kwargs):
            if kwargs.get("withFocus"):
                return None
            if kwargs.get("visiblePanels"):
                return list(panels)
            if kwargs.get("typeOf"):
                return "modelPanel"
            raise AssertionError("Unexpected panel query")

        def panel_camera(panel, **kwargs):
            return panels[panel]

        with mock.patch.object(cmds, "about", return_value=False), \
             mock.patch.object(cmds, "getPanel", side_effect=panel_inventory), \
             mock.patch.object(cmds, "modelPanel", side_effect=panel_camera):
            self.assertEqual(self.adapter._active_camera(cmds), alternate_path)
            # Another visible production view makes the evidence ambiguous.
            panels["otherShotPanel"] = self.camera
            self.assertIsNone(self.adapter._active_camera(cmds))
            panels.pop("shotPanel")
            self.assertEqual(self.adapter._active_camera(cmds), original_path)

    def test_invalid_and_failed_construction_keep_previous_rig(self):
        shot = self.adapter.collect_shot()
        first = self.adapter.apply_plan(self.plan(), shot)
        owner = cmds.getAttr(first + "." + self.adapter.OWNER_ATTRIBUTE)
        invalid = self.plan()
        invalid["lights"][0]["color"] = [float("nan"), 0, 0]
        with self.assertRaises(self.adapter.MayaAdapterError):
            self.adapter.apply_plan(invalid, shot)
        original_create = cmds.shadingNode
        def injected_failure(node_type, *args, **kwargs):
            if node_type == "aiSkyDomeLight":
                raise RuntimeError("Injected shape creation failure")
            return original_create(node_type, *args, **kwargs)
        with mock.patch.object(cmds, "shadingNode", injected_failure):
            with self.assertRaisesRegex(RuntimeError, "Injected"):
                self.adapter.apply_plan(self.plan(), shot)
        groups = self.adapter._owned_groups(cmds)
        self.assertEqual(len(groups), 1)
        self.assertEqual(cmds.getAttr(groups[0] + "." + self.adapter.OWNER_ATTRIBUTE), owner)

    def test_unowned_children_and_unowned_group_are_never_deleted(self):
        shot = self.adapter.collect_shot()
        group = self.adapter.apply_plan(self.plan(), shot)
        foreign = cmds.createNode("transform", name="ArtistsOwnNode", parent=group)
        with self.assertRaises(self.adapter.MayaAdapterError):
            self.adapter.remove_rig(group)
        with self.assertRaises(self.adapter.MayaAdapterError):
            self.adapter.apply_plan(self.plan(), shot)
        self.assertTrue(cmds.objExists(foreign))
        self.assertTrue(cmds.objExists(group))
        with self.assertRaises(self.adapter.MayaAdapterError):
            self.adapter.remove_rig(self.subject)
        self.assertTrue(cmds.objExists(self.subject))

    def test_failed_render_restores_unset_strings_settings_and_undo_stack(self):
        import mtoa.core
        mtoa.core.createOptions()
        shot = self.adapter.collect_shot()
        plugs = ["defaultRenderGlobals.currentRenderer", "defaultRenderGlobals.imageFilePrefix",
                 "defaultRenderGlobals.useRenderRegion", "defaultArnoldRenderOptions.aovMode",
                 "defaultRenderGlobals.animation", "defaultResolution.width", "defaultResolution.height",
                 "defaultResolution.pixelAspect", "defaultResolution.deviceAspectRatio",
                 "defaultArnoldDriver.aiTranslator", "defaultArnoldDriver.colorManagement", "defaultArnoldRenderOptions.AASamples",
                 "defaultArnoldRenderOptions.autotx",
                 "defaultArnoldRenderOptions.abortOnLicenseFail", self.camera_shape + ".renderable"]
        before = {p: cmds.getAttr(p) for p in plugs}
        scene_before = self.unchanged_data()
        undo_before = cmds.undoInfo(query=True, undoName=True)
        with tempfile.TemporaryDirectory(prefix="shotlight-render-failure-") as folder:
            with mock.patch.object(cmds, "arnoldRender", side_effect=RuntimeError("Injected render failure")):
                with self.assertRaisesRegex(RuntimeError, "Injected"):
                    self.adapter.render_previews(shot, folder)
        self.assertEqual(before, {p: cmds.getAttr(p) for p in plugs})
        self.assertEqual(scene_before, self.unchanged_data())
        self.assertEqual(undo_before, cmds.undoInfo(query=True, undoName=True))

    def test_quick_render_uses_smaller_pixels_and_restores_sampling_after_failure(self):
        import mtoa.core
        mtoa.core.createOptions()
        shot = self.adapter.collect_shot()
        plugs = ["defaultResolution.width", "defaultResolution.height", "defaultArnoldRenderOptions.AASamples",
                 "defaultArnoldRenderOptions.enableAdaptiveSampling", "defaultArnoldRenderOptions.AASamplesMax",
                 "defaultArnoldRenderOptions.GISssSamples", "defaultArnoldRenderOptions.GITransmissionSamples", "defaultArnoldRenderOptions.GIVolumeSamples"]
        before = [cmds.getAttr(p) for p in plugs]
        captured = []
        def fail(**kwargs):
            captured.append([cmds.getAttr(p) for p in plugs])
            raise RuntimeError("Injected render failure")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "Injected"):
                self.adapter.capture_previews(shot, folder, render=True, quick=True)
        self.assertEqual(captured[0][2], 2)
        self.assertEqual(max(captured[0][:2]), 384)
        self.assertEqual(captured[0][3:], [False, 2, 1, 1, 1])
        self.assertEqual(before, [cmds.getAttr(p) for p in plugs])

    def test_panel_requires_selected_camera_and_never_guesses(self):
        with self.assertRaisesRegex(self.adapter.MayaAdapterError, "Select your shot camera"):
            self.adapter.selected_camera()
        cmds.select(self.camera)
        self.assertEqual(self.adapter.selected_camera(), cmds.ls(self.camera_shape, long=True)[0])

    def _write_mock_preview(self):
        # Native renderer is injected; no cloud call or artist scene is involved.
        Path(cmds.getAttr("defaultRenderGlobals.imageFilePrefix") + "_1.png").write_bytes(b"test image")

    def test_transient_command_error_retries_only_failed_frame(self):
        shot = self.adapter.collect_shot()
        calls = []
        def render(**kwargs):
            calls.append(cmds.currentTime(query=True))
            if len(calls) == 2:
                raise RuntimeError("Maya command error")
            self._write_mock_preview()
        before = self.unchanged_data()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=render):
            previews = self.adapter.capture_previews(shot, folder, render=True, quick=True)
            records = [json.loads(p.read_text()) for p in Path(folder).rglob("render_*.json")]
        self.assertEqual(calls, [1., 6.5, 6.5, 12.])
        self.assertEqual([p["frame"] for p in previews], shot["frames"])
        self.assertEqual(sorted(len(r["attempts"]) for r in records), [1, 1, 2])
        self.assertEqual(before, self.unchanged_data())

    def test_valid_mesh_uv_export_error_is_reevaluated_and_retried(self):
        import maya.api.OpenMaya as om
        shot = self.adapter.collect_shot(sample_count=1)
        shape = cmds.listRelatives(self.mesh, shapes=True, fullPath=True)[0]
        message = "ERROR | [polymesh] %s: incorrectly defined UV coordinates, missing either uvlist or uvidxs" % shape.replace("|", "/")
        calls = []
        def render(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                om.MGlobal.displayError(message)
                raise RuntimeError("Maya command error")
            self._write_mock_preview()
        before = self.unchanged_data()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=render):
            previews = self.adapter.capture_previews(shot, folder, render=True, quick=True)
            record = json.loads(next(Path(folder).rglob("render_*.json")).read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(previews), 1)
        self.assertIn("incorrectly defined UV", record["attempts"][0]["native_errors"][0])
        self.assertTrue(record["attempts"][1]["completed"])
        self.assertEqual(before, self.unchanged_data())

    def test_persistent_native_error_reports_frame_camera_and_diagnostic(self):
        import maya.api.OpenMaya as om
        shot = self.adapter.collect_shot(sample_count=1)
        def fail(**kwargs):
            om.MGlobal.displayError("ERROR | texture file could not be read")
            raise RuntimeError("Maya command error")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=fail) as render:
            expected = "frame %g through %s" % (shot["frames"][0], self.camera_shape)
            with self.assertRaisesRegex(self.adapter.MayaAdapterError, expected) as caught:
                self.adapter.capture_previews(shot, folder, render=True, quick=True)
            self.assertIn("texture file could not be read", str(caught.exception))
            self.assertIn("Diagnostic:", str(caught.exception))
            record = json.loads(next(Path(folder).rglob("render_*.json")).read_text())
        self.assertEqual(render.call_count, 2)
        self.assertEqual(len(record["attempts"]), 2)

    def test_actual_missing_uv_data_is_not_modified_or_retried(self):
        import maya.api.OpenMaya as om
        cmds.polyMapDel(self.mesh + ".map[*]")
        shot = self.adapter.collect_shot(sample_count=1)
        shape = cmds.listRelatives(self.mesh, shapes=True, fullPath=True)[0]
        def fail(**kwargs):
            om.MGlobal.displayError("ERROR | [polymesh] %s: incorrectly defined UV coordinates, missing either uvlist or uvidxs" % shape.replace("|", "/"))
            raise RuntimeError("Maya command error")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=fail) as render:
            with self.assertRaisesRegex(self.adapter.MayaAdapterError, "Repair UV set map1"):
                self.adapter.capture_previews(shot, folder, render=True, quick=True)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(cmds.polyEvaluate(self.mesh, uv=True), 0)

    def test_cancelled_native_render_is_never_retried(self):
        from shotlight.runtime import Cancelled
        shot = self.adapter.collect_shot(sample_count=1)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=Cancelled("Stopped")) as render:
            with self.assertRaises(Cancelled):
                self.adapter.capture_previews(shot, folder, render=True, quick=True)
        self.assertEqual(render.call_count, 1)

    def test_native_error_cannot_accept_a_written_preview(self):
        import maya.api.OpenMaya as om
        shot = self.adapter.collect_shot(sample_count=1)
        def fail(**kwargs):
            self._write_mock_preview()
            om.MGlobal.displayError("ERROR | invalid render output")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cmds, "arnoldRender", side_effect=fail) as render:
            with self.assertRaisesRegex(self.adapter.MayaAdapterError, "invalid render output"):
                self.adapter.capture_previews(shot, folder, render=True, quick=True)
        self.assertEqual(render.call_count, 2)

    def test_save_reopen_persists_editable_rig_and_animation(self):
        shot = self.adapter.collect_shot()
        before = self.unchanged_data()
        group = self.adapter.apply_plan(self.plan(), shot)
        owner = cmds.getAttr(group + "." + self.adapter.OWNER_ATTRIBUTE)
        with tempfile.TemporaryDirectory(prefix="shotlight-scene-") as folder:
            filename = str(Path(folder) / "native_scratch.ma")
            cmds.file(rename=filename)
            cmds.file(save=True, type="mayaAscii")
            cmds.file(new=True, force=True)
            cmds.file(filename, open=True, force=True)
            groups = self.adapter._owned_groups(cmds)
            self.assertEqual(len(groups), 1)
            self.assertEqual(cmds.getAttr(groups[0] + "." + self.adapter.OWNER_ATTRIBUTE), owner)
            self.assertEqual(before["curves"], self.unchanged_data()["curves"])
            self.adapter.remove_rig(groups[0])
            self.assertEqual(self.adapter._owned_groups(cmds), [])
            self.assertTrue(cmds.objExists(self.existing))


if __name__ == "__main__":
    import sys
    import maya.standalone
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds
    MAYA_AVAILABLE = True
    # Decorator runs before standalone initializes; clear the initial skip.
    MayaAdapterTests.__unittest_skip__ = False
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        maya.standalone.uninitialize()
    sys.exit(0 if result.wasSuccessful() else 1)
