import copy
import unittest

from shotlight.plan import EXPOSURE_LIMITS, PLAN_SCHEMA, PlanError, validate_plan


SHOT = {"center": [0, 1, 0], "radius": 2}
PLAN = {"summary": "Soft main light with a restrained rim.",
        "look_description": "Natural neutral lighting.", "confidence": .8,
        "lights": [{"name": "Key", "role": "key", "type": "area",
                    "position": [2, 4, 4], "target": [0, 1, 0], "size": [2, 2],
                    "color": [1, .95, .9], "exposure": 4}]}


class PlanValidationTests(unittest.TestCase):
    def test_normalizes_without_mutating_input(self):
        value = copy.deepcopy(PLAN)
        result = validate_plan(value, SHOT)
        self.assertEqual(value, PLAN)
        self.assertIsNot(result["lights"], value["lights"])
        self.assertEqual(result["lights"][0]["position"], [2., 4., 4.])

    def test_rejects_model_commands_and_unknown_attributes(self):
        value = copy.deepcopy(PLAN)
        value["lights"][0]["command"] = "delete scene"
        with self.assertRaises(PlanError):
            validate_plan(value, SHOT)

    def test_rejects_nan_bool_and_infinity(self):
        for number in [float("nan"), float("inf"), True]:
            value = copy.deepcopy(PLAN)
            value["lights"][0]["exposure"] = number
            with self.assertRaises(PlanError):
                validate_plan(value, SHOT)

    def test_rejects_out_of_scene_position_and_excessive_power(self):
        for field, value in [("position", [1e8, 0, 0]), ("exposure", 99),
                             ("color", [2, 1, 1]), ("size", [0, 2])]:
            plan = copy.deepcopy(PLAN)
            plan["lights"][0][field] = value
            with self.assertRaises(PlanError):
                validate_plan(plan, SHOT)

    def test_rejects_ambiguous_or_hierarchical_names(self):
        for name in ["|existingRig|light", "light.translateX", "a:b", "$(touch x)"]:
            plan = copy.deepcopy(PLAN)
            plan["lights"][0]["name"] = name
            with self.assertRaises(PlanError):
                validate_plan(plan, SHOT)

    def test_rejects_duplicate_names_and_oversize_light_arrays(self):
        for count in [2, 7]:
            plan = copy.deepcopy(PLAN)
            plan["lights"] *= count
            with self.assertRaises(PlanError):
                validate_plan(plan, SHOT)

    def test_normalized_area_lights_support_large_scene_power(self):
        large_shot = {"center": [0, 100, 0], "radius": 2117.7}
        for exposure in (24, 32):
            with self.subTest(exposure=exposure):
                plan = copy.deepcopy(PLAN)
                plan["lights"][0].update(position=[2000, 1800, 2000], target=[0, 100, 0],
                                          size=[1000, 800], exposure=exposure)
                self.assertEqual(validate_plan(plan, large_shot)["lights"][0]["exposure"], exposure)

    def test_exposure_limits_are_type_specific_and_bounded(self):
        for light_type, (minimum, maximum) in EXPOSURE_LIMITS.items():
            for exposure, valid in ((minimum, True), (maximum, True), (minimum - .001, False), (maximum + .001, False)):
                with self.subTest(light_type=light_type, exposure=exposure):
                    plan = copy.deepcopy(PLAN)
                    plan["lights"][0].update(type=light_type, exposure=exposure)
                    if valid:
                        self.assertEqual(validate_plan(plan, SHOT)["lights"][0]["exposure"], exposure)
                    else:
                        with self.assertRaises(PlanError):
                            validate_plan(plan, SHOT)
        self.assertEqual(PLAN_SCHEMA["properties"]["lights"]["items"]["properties"]["exposure"]["maximum"], 32)


if __name__ == "__main__":
    unittest.main()
