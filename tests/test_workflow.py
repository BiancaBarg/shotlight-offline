import copy
import tempfile
import unittest
from unittest import mock

from shotlight import workflow
from shotlight.runtime import Budget, TimedOut


SHOT = {"camera": {"name": "shotCam", "position": [0, 3, 5]},
        "unit": "cm", "up_axis": "y", "bounds": [-1, 0, -1, 1, 2, 1],
        "center": [0, 1, 0], "radius": 2,
        "frames": [1, 12, 24], "subjects": [{"name": "Hero", "bounds": [-1, 0, -1, 1, 2, 1]}]}
PLAN = {"summary": "Main light from camera left.", "look_description": "Neutral.",
        "confidence": .8, "lights": [{"name": "Key", "type": "area", "role": "key",
        "position": [-2, 4, 3], "target": [0, 1, 0], "size": [2, 2],
        "color": [1, 1, 1], "exposure": 2}]}


class Adapter:
    def __init__(self):
        self.applied = []
        self.collected = 0
        self.rendered = 0
        self.change = False
        self.change_sample = False
        self.change_light = False
        self.fail_render = False
        self.fail_baseline = False
        self.capture_modes = []
        self.jitter = False
        self.capture_calls = []
        self.removed = 0

    def collect_shot(self, **kwargs):
        self.collected += 1
        value = copy.deepcopy(SHOT)
        if self.change and self.collected > 1:
            value["camera"]["position"][0] = 10
        if self.jitter and self.collected > 1:
            value["subjects"][0]["bounds"][0] += 0.000003
        value["samples"] = [{"frame": 12, "camera": {"position": [0, 3, 5]}}]
        if self.change and self.collected > 1:
            value["samples"][0]["camera"]["position"][0] = 10
        if self.change_sample and self.collected > 1:
            value["samples"][0]["camera"]["position"][0] = 10
        value["existing_lights"] = [{"name": "ArtistKey", "owned": False, "exposure": 1}]
        if self.change_light and self.collected > 1:
            value["existing_lights"][0]["exposure"] = 10
        if self.applied:
            value["existing_lights"].append({"name": "SL_Key", "owned": True, "exposure": 2})
        return value

    def capture_previews(self, shot, directory, render=False, quick=False, control=None):
        self.capture_modes.append(render)
        self.capture_calls.append((list(shot["frames"]), quick))
        if render:
            self.rendered += 1
            if self.fail_baseline or (self.fail_render and self.applied):
                raise RuntimeError("Render failed")
        return [{"frame": frame, "path": "/unused.png"} for frame in shot["frames"]]

    def apply_plan(self, plan, shot):
        self.applied.append(plan)
        return "ShotLight_Rig"

    def remove_rig(self):
        self.removed += 1


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        review = mock.patch("shotlight.workflow.planner.assess", return_value={
            "acceptable": True, "reason": "Subject is readable across sampled frames."}, create=True)
        self.assess = review.start()
        self.addCleanup(review.stop)

    def test_real_pipeline_reviews_rendered_images_before_replacing_rig(self):
        adapter = Adapter()
        improved = copy.deepcopy(PLAN)
        improved["lights"][0]["exposure"] = 1
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", side_effect=[PLAN, improved]) as analyze:
            result = workflow.light_shot(adapter, directory)
        self.assertTrue(result["refined"])
        self.assertIsNone(result["warning"])
        self.assertEqual(adapter.rendered, 3)
        self.assertEqual(adapter.capture_modes, [True, True, True])
        self.assertEqual(adapter.applied, [PLAN, improved])
        self.assertEqual(analyze.call_args.kwargs["previous_plan"], PLAN)
        self.assess.assert_called_once()
        self.assertTrue(result["assessment"]["acceptable"])

    def test_final_render_rejection_is_reported_as_needing_review(self):
        adapter = Adapter()
        self.assess.return_value = {"acceptable": False, "reason": "The face is unreadable."}
        status = []
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            result = workflow.light_shot(adapter, directory, status=status.append)
        self.assertIn("face is unreadable", result["warning"])
        self.assertFalse(result["assessment"]["acceptable"])
        self.assertIn("needs review", status[-1])

    def test_skinned_bound_roundoff_does_not_report_an_artist_edit(self):
        adapter = Adapter()
        adapter.jitter = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            result = workflow.light_shot(adapter, directory)
        self.assertIsNone(result["warning"])
        self.assertTrue(result["assessment"]["acceptable"])

    def test_baseline_render_failure_does_not_place_lights_or_call_planner(self):
        adapter = Adapter()
        adapter.fail_baseline = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN) as analyze:
            with self.assertRaisesRegex(RuntimeError, "Render failed"):
                workflow.light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])
        analyze.assert_not_called()

    def test_no_scene_mutations_when_local_planner_fails(self):
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", side_effect=RuntimeError("Invalid local plan")):
            with self.assertRaisesRegex(RuntimeError, "Invalid local plan"):
                workflow.light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])

    def test_changed_camera_aborts_before_first_application(self):
        adapter = Adapter()
        adapter.change = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            with self.assertRaisesRegex(RuntimeError, "shot changed"):
                workflow.light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])

    def test_failed_render_keeps_initial_valid_lighting_and_reports_incomplete_check(self):
        adapter = Adapter()
        adapter.fail_render = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            result = workflow.light_shot(adapter, directory)
        self.assertFalse(result["refined"])
        self.assertIn("could not complete", result["warning"])
        self.assertEqual(adapter.applied, [PLAN])

    def test_changed_animated_camera_sample_aborts_before_application(self):
        adapter = Adapter()
        adapter.change_sample = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            with self.assertRaisesRegex(RuntimeError, "shot changed"):
                workflow.light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])

    def test_scrubbing_animated_camera_is_not_mistaken_for_camera_edit(self):
        first = Adapter().collect_shot()
        second = copy.deepcopy(first)
        second["camera"]["position"] = [10, 20, 30]
        second["current_frame"] = 10
        self.assertTrue(workflow.same_scene(workflow.scene_fingerprint(first), workflow.scene_fingerprint(second)))
        second["samples"][0]["camera"]["position"][0] = 10
        self.assertFalse(workflow.same_scene(workflow.scene_fingerprint(first), workflow.scene_fingerprint(second)))

    def test_changed_artist_light_aborts_but_generated_lights_do_not_change_fingerprint(self):
        adapter = Adapter()
        adapter.change_light = True
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                "shotlight.workflow.planner.analyze", return_value=PLAN):
            with self.assertRaisesRegex(RuntimeError, "shot changed"):
                workflow.light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])


class VersionWorkflowTests(unittest.TestCase):
    def setUp(self):
        measure = mock.patch.object(workflow.planner, "measure_previews", return_value=[
            {"frame": 12, "p70": .3, "p90": .6, "clipped": 0}])
        measure.start()
        self.addCleanup(measure.stop)

    def versions(self):
        versions = []
        for i, label in enumerate(("Natural", "Soft daylight", "Directional contrast")):
            plan = copy.deepcopy(PLAN)
            plan["lights"][0]["position"][0] -= i
            versions.append({"label": label, "plan": plan})
        return versions

    def generate(self, adapter, directory):
        with mock.patch.object(workflow.planner, "generate_versions", return_value=self.versions()) as generate:
            result = workflow.fast_light_shot(adapter, directory)
        generate.assert_called_once()
        return result

    def test_fast_draft_has_three_choices_two_small_frames_and_no_false_review(self):
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(workflow.planner, "assess") as assess:
            result = self.generate(adapter, directory)
        self.assertEqual(adapter.capture_calls, [([12], True), ([12], True)])
        self.assertEqual(len(result["versions"]), 3)
        self.assertIsNone(result["assessment"])
        self.assertFalse(result["refined"])
        assess.assert_not_called()

    def test_invalid_third_choice_prevents_all_scene_edits(self):
        adapter = Adapter()
        versions = self.versions()
        versions[2]["plan"]["lights"][0]["exposure"] = 200
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(workflow.planner, "generate_versions", return_value=versions):
            with self.assertRaises(workflow.planner.PlannerError):
                workflow.fast_light_shot(adapter, directory)
        self.assertEqual(adapter.applied, [])

    def test_expiry_during_scene_guard_prevents_first_light_application(self):
        control = Budget(90)
        class SlowGuardAdapter(Adapter):
            def collect_shot(self, **kwargs):
                shot = super().collect_shot(**kwargs)
                if self.collected > 1:
                    control.started -= 100
                return shot
        adapter = SlowGuardAdapter()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(workflow.planner, "generate_versions", return_value=self.versions()):
            with self.assertRaises(TimedOut):
                workflow.fast_light_shot(adapter, directory, control=control)
        self.assertEqual(adapter.applied, [])

    def test_review_timeout_retains_current_rig_and_never_claims_approval(self):
        adapter = Adapter()
        status = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(adapter, directory)
            with mock.patch.object(workflow.planner, "analyze", side_effect=TimedOut("Local analysis time limit reached")), mock.patch.object(workflow.planner, "assess") as assess:
                checked = workflow.review_version(adapter, result, status=status.append)
        self.assertEqual(adapter.applied, [result["plan"]])
        self.assertIsNone(checked["assessment"])
        self.assertFalse(checked["refined"])
        self.assertIn("could not complete", checked["warning"])
        self.assertIn("Time limit reached", status[-1])
        assess.assert_not_called()

    def test_switch_uses_cached_plan_and_never_calls_planner_or_renders(self):
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(adapter, directory)
            renders = adapter.rendered
            with mock.patch.object(workflow.planner, "generate_versions") as generate, mock.patch.object(workflow.planner, "analyze") as analyze:
                switched = workflow.switch_version(adapter, result, 2)
                returned = workflow.switch_version(adapter, switched, 0)
        self.assertEqual(adapter.rendered, renders)
        generate.assert_not_called()
        analyze.assert_not_called()
        self.assertEqual(switched["after"], [])
        self.assertEqual(returned["after"], result["after"])
        self.assertEqual(adapter.applied[-2], result["versions"][2]["plan"])

    def test_original_removes_generated_lights_and_keeps_all_choices(self):
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(adapter, directory)
            original = workflow.restore_original(adapter, result)
            self.assertIsNone(original["active_version"])
            self.assertIsNone(original["group"])
            self.assertEqual(original["after"], [])
            self.assertEqual(original["versions"], result["versions"])
            reapplied = workflow.switch_version(adapter, original, 0)
            self.assertEqual(reapplied["plan"], result["plan"])
        self.assertEqual(adapter.removed, 1)

    def test_changed_shot_blocks_switch_without_replacing_current_lights(self):
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(adapter, directory)
            adapter.change = True
            with self.assertRaisesRegex(RuntimeError, "shot changed"):
                workflow.switch_version(adapter, result, 1)
        self.assertEqual(len(adapter.applied), 1)

    def test_optional_review_refines_only_selected_version_and_reports_rejection(self):
        adapter = Adapter()
        improved = copy.deepcopy(PLAN)
        improved["lights"][0]["exposure"] = 1
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(adapter, directory)
            result = workflow.switch_version(adapter, result, 1)
            with mock.patch.object(workflow.planner, "analyze", return_value=improved), mock.patch.object(workflow.planner, "assess", return_value={"acceptable": False, "reason": "Face too dark."}):
                reviewed = workflow.review_version(adapter, result)
        self.assertEqual(reviewed["versions"][1]["plan"], improved)
        self.assertEqual(reviewed["versions"][0]["plan"], result["versions"][0]["plan"])
        self.assertEqual(adapter.capture_calls[-2:], [([1, 12, 24], True), ([1, 12, 24], True)])
        self.assertIn("Face too dark", reviewed["warning"])
        self.assertFalse(reviewed["assessment"]["acceptable"])


if __name__ == "__main__":
    unittest.main()
