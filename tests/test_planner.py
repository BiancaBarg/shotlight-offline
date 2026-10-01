"""Local planning, exposure boundaries and absence of external dependencies."""
import copy
import math
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from shotlight import planner
from shotlight.plan import validate_plan
from shotlight.runtime import Budget, Cancelled

SHOT = {"center": [0, 85, 0], "radius": 100, "bounds": [-50, 0, -30, 50, 170, 30],
        "unit": "cm", "up_axis": "y", "frames": [1, 12, 24], "subjects": [],
        "camera": {"position": [0, 120, 600], "forward": [0, 0, -1], "up": [0, 1, 0],
                   "focal_length": 50, "horizontal_film_aperture": 1.417}}
METRIC = {"p50": .2, "p70": .3, "p90": .6, "clipped": 0, "dark": .1}


class LocalPlannerTests(unittest.TestCase):
    def test_three_distinct_valid_native_plans_without_any_external_calls(self):
        with mock.patch('socket.create_connection', side_effect=AssertionError('network')), mock.patch('subprocess.Popen', side_effect=AssertionError('process')):
            versions = planner.generate_versions(SHOT)
        self.assertEqual(len(versions), 3)
        self.assertEqual(len({str(v['plan']['lights']) for v in versions}), 3)
        for version in versions:
            self.assertEqual(len(validate_plan(version['plan'], SHOT)['lights']), 3)

    def test_camera_rotation_rotates_light_positions(self):
        shot = copy.deepcopy(SHOT)
        shot['camera'].update(position=[600,120,0], forward=[-1,0,0])
        first = planner.generate_versions(SHOT)[0]['plan']['lights'][0]['position']
        second = planner.generate_versions(shot)[0]['plan']['lights'][0]['position']
        self.assertAlmostEqual(second[0], first[2])
        self.assertAlmostEqual(second[2], -first[0])

    def test_native_unit_change_preserves_physical_power_and_locations(self):
        meters = copy.deepcopy(SHOT)
        meters.update(unit='m', radius=1, center=[0,.85,0], bounds=[-.5,0,-.3,.5,1.7,.3])
        meters['camera']['position'] = [0,1.2,6]
        cm = planner.generate_versions(SHOT)[0]['plan']['lights']
        m = planner.generate_versions(meters)[0]['plan']['lights']
        for a,b in zip(cm,m):
            self.assertAlmostEqual(a['exposure'], b['exposure'])
            for x,y in zip(a['position'], b['position']):
                self.assertAlmostEqual(x/100,y)

    def test_larger_motion_union_increases_light_coverage_and_power(self):
        shot = copy.deepcopy(SHOT)
        shot['radius'] = 200
        a = planner.generate_versions(SHOT)[0]['plan']['lights'][0]
        b = planner.generate_versions(shot)[0]['plan']['lights'][0]
        self.assertEqual(b['size'][0], a['size'][0]*2)
        self.assertAlmostEqual(b['exposure'], a['exposure']+2)

    def test_z_up_is_supported(self):
        shot = copy.deepcopy(SHOT)
        shot.update(up_axis='z',center=[0,0,85],bounds=[-50,-30,0,50,30,170])
        shot['camera'].update(position=[0,-600,120],forward=[0,1,0],up=[0,0,1])
        key = planner.generate_versions(shot)[0]['plan']['lights'][0]
        self.assertGreater(key['position'][2],shot['center'][2])
        self.assertLess(key['position'][1],0)

    def test_top_down_camera_has_a_stable_basis(self):
        shot = copy.deepcopy(SHOT)
        shot['camera'].update(position=[0,600,0],forward=[0,-1,0],up=[0,0,-1])
        self.assertEqual(len(planner.generate_versions(shot)),3)

    def test_nonfinite_and_zero_subject_size_rejected(self):
        for value in (0,-1,math.nan,math.inf,True):
            with self.subTest(value=value),self.assertRaises(planner.PlannerError):
                planner.generate_versions(dict(SHOT,radius=value))

    def test_extreme_scale_cannot_escape_exposure_limits(self):
        for radius in (1e-8,1e8):
            versions=planner.generate_versions(dict(SHOT,radius=radius))
            self.assertTrue(all(-12<=l['exposure']<=32 for v in versions for l in v['plan']['lights']))

    def test_bright_artist_baseline_reduces_added_light_power(self):
        with mock.patch.object(planner,'measure_previews',return_value=[dict(METRIC,p70=.6)]):
            lit=planner.generate_versions(SHOT,[{'frame':12,'path':'unused'}])
        unlit=planner.generate_versions(SHOT)
        self.assertAlmostEqual(lit[0]['plan']['lights'][0]['exposure'],unlit[0]['plan']['lights'][0]['exposure']-1.5)

    def test_old_owned_lights_do_not_bias_new_baseline(self):
        shot=dict(SHOT,existing_lights=[{'owned':True}])
        with mock.patch.object(planner,'measure_previews') as measure:
            planner.generate_versions(shot,[{'frame':12,'path':'unused'}])
        measure.assert_not_called()

    def test_environment_box_moves_emitter_out_of_solid_bounds(self):
        pos=planner.generate_versions(SHOT)[0]['plan']['lights'][0]['position']
        shot=dict(SHOT,environment=[{'bounds':[p-30 for p in pos]+[p+30 for p in pos]}])
        revised=planner.generate_versions(shot)[0]['plan']['lights'][0]['position']
        self.assertNotEqual(pos,revised)

    def test_dark_material_compensation_is_restrained(self):
        shot=dict(SHOT,subjects=[{'materials':[{'name':'black','properties':{'baseColor':[.005]*3}}]}])
        diff=planner.generate_versions(shot)[0]['plan']['lights'][0]['exposure']-planner.generate_versions(SHOT)[0]['plan']['lights'][0]['exposure']
        self.assertGreater(diff,0)
        self.assertLessEqual(diff,.65)

    def test_opaque_black_is_dark_and_transparent_pixels_are_excluded(self):
        dark=planner.pixel_metrics([(0,0,0,1)]*20+[(1,1,1,0)]*20)
        self.assertEqual(dark['pixels'],20)
        self.assertEqual(dark['dark'],1)
        self.assertEqual(dark['p90'],0)
        self.assertEqual(planner.exposure_correction([dark]),2.5)

    def test_clipped_channel_prioritizes_lowering_exposure(self):
        m=planner.pixel_metrics([(1,.1,.1,1)]*20)
        self.assertEqual(m['clipped'],1)
        self.assertLess(planner.exposure_correction([m]),0)

    def test_missing_or_transparent_preview_cannot_pass_review(self):
        with self.assertRaises(planner.PlannerError):
            planner.pixel_metrics([(0,0,0,0)]*100)
        plan=planner.generate_versions(SHOT)[0]['plan']
        with mock.patch.object(planner,'measure_previews',return_value=[]),self.assertRaises(planner.PlannerError):
            planner.assess(SHOT,[],plan)

    def test_review_is_explicitly_technical_and_requires_all_sample_frames(self):
        plan=planner.generate_versions(SHOT)[0]['plan']
        metrics=[dict(METRIC,frame=f) for f in SHOT['frames']]
        with mock.patch.object(planner,'measure_previews',return_value=metrics):
            assessment=planner.assess(SHOT,[],plan)
        self.assertTrue(assessment['acceptable'])
        self.assertEqual(assessment['scope'],'technical_exposure')
        self.assertIn('Artist review',assessment['reason'])
        with mock.patch.object(planner,'measure_previews',return_value=metrics[:1]),self.assertRaises(planner.PlannerError):
            planner.assess(SHOT,[],plan)

    def test_dark_render_never_claims_acceptance(self):
        plan=planner.generate_versions(SHOT)[0]['plan']
        with mock.patch.object(planner,'measure_previews',return_value=[dict(METRIC,p90=.01,frame=f) for f in SHOT['frames']]):
            self.assertFalse(planner.assess(SHOT,[],plan)['acceptable'])

    def test_refinement_does_not_change_direction_colour_or_mutate_input(self):
        plan=planner.generate_versions(SHOT)[0]['plan']
        original=copy.deepcopy(plan)
        with mock.patch.object(planner,'measure_previews',return_value=[dict(METRIC,p70=.02)]):
            adjusted=planner.analyze(SHOT,[],previous_plan=plan)
        self.assertEqual(plan,original)
        for first,second in zip(plan['lights'],adjusted['lights']):
            self.assertEqual(first['position'],second['position'])
            self.assertEqual(first['color'],second['color'])
            self.assertLessEqual(second['exposure']-first['exposure'],2.5)

    def test_projection_rejects_subject_behind_camera(self):
        shot=copy.deepcopy(SHOT)
        shot['camera']['forward']=[0,0,1]
        with self.assertRaises(planner.PlannerError):
            planner.projected_region(shot,12,384,216)

    def test_projection_uses_sampled_camera_and_orthographic_width(self):
        shot=copy.deepcopy(SHOT)
        shot['camera'].update(orthographic=True,orthographic_width=400)
        region=planner.projected_region(shot,12,400,400)
        self.assertEqual(region[0],150)
        self.assertEqual(region[2],250)

    def test_duplicate_versions_rejected_before_application(self):
        versions=planner.generate_versions(SHOT)
        versions[2]=copy.deepcopy(versions[0])
        with self.assertRaises(planner.PlannerError):
            planner.validate_versions({'versions':versions},SHOT)

    def test_local_cancellation_needs_no_external_process(self):
        import threading
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):
            planner.generate_versions(SHOT,config={'control':Budget(30,event)})

    def test_watermarked_baseline_is_skipped_without_preventing_lighting(self):
        with mock.patch.object(planner, 'measure_previews') as measure:
            self.assertEqual(len(planner.generate_versions(SHOT,[{'analysis_usable':False}])),3)
        measure.assert_not_called()

    def test_shipping_planner_has_no_network_or_cli_imports(self):
        import ast
        source=Path(planner.__file__).read_text()
        imported={node.names[0].name.split('.')[0] for node in ast.walk(ast.parse(source)) if isinstance(node,ast.Import)}
        imported|={node.module.split('.')[0] for node in ast.walk(ast.parse(source)) if isinstance(node,ast.ImportFrom) and node.module}
        self.assertFalse(imported&{'urllib','requests','socket','subprocess','openai','http'})
        self.assertNotIn('api.openai.com',source)


if __name__=='__main__':
    unittest.main()
