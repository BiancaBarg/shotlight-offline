"""Exercise offline lighting in a separate mayapy process, never an artist scene."""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--output', required=True)
    output = Path(args.parse_args().output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    import maya.standalone
    maya.standalone.initialize(name='python')
    import maya.cmds as cmds
    from demo_scene import build_scene, preservation_data
    from shotlight import maya_adapter, workflow, planner, __version__
    try:
        camera, hero = build_scene(cmds)
        before = preservation_data(cmds, camera)
        with mock.patch('socket.create_connection', side_effect=AssertionError('external connection forbidden')), mock.patch('subprocess.Popen', side_effect=AssertionError('external process forbidden')):
            result = workflow.fast_light_shot(maya_adapter, output, camera=camera, subjects=[hero], status=lambda m: print(m, flush=True))
            if result['warning'] and not any(not p.get('analysis_usable',True) for p in result['after']):
                raise RuntimeError(result['warning'])
            versions = []
            for index in range(3):
                result = workflow.switch_version(maya_adapter, result, index)
                result = workflow.preview_version(maya_adapter, result)
                if result['warning']:
                    raise RuntimeError(result['warning'])
                sample = dict(result['shot'], frames=[p['frame'] for p in result['after']])
                metrics = planner.measure_previews(sample, result['after']) if all(p.get('analysis_usable',True) for p in result['after']) else None
                versions.append({'label': result['versions'][index]['label'], 'images': result['after'], 'metrics': metrics})
            result = workflow.switch_version(maya_adapter, result, 0)
            result = workflow.review_version(maya_adapter, result, status=lambda m: print(m,flush=True))
            if result['assessment'] is None and not any(not p.get('analysis_usable',True) for p in result['after']):
                raise RuntimeError(result['warning'])
            if before != preservation_data(cmds, camera):
                raise RuntimeError('Artist data changed during lighting/rendering')
            result = workflow.restore_original(maya_adapter, result)
            if maya_adapter._owned_groups(cmds) or before != preservation_data(cmds, camera):
                raise RuntimeError('Original did not restore the untouched baseline')
            result = workflow.switch_version(maya_adapter, result, 0)
        cmds.file(rename=str(output/'offline_lit.ma'))
        cmds.file(save=True,type='mayaAscii')
        cmds.file(new=True,force=True)
        cmds.file(str(output/'offline_lit.ma'),open=True,force=True)
        if len(maya_adapter._owned_groups(cmds)) != 1 or before['curves'] != preservation_data(cmds,camera)['curves']:
            raise RuntimeError('Saved editable rig/animation did not persist')
        report = {'version':__version__, 'generation_seconds':result['seconds'], 'variants':versions,
                  'assessment':result['assessment'], 'review_warning':result['warning'], 'network_and_cli_blocked':True,
                  'artist_data_preserved':True,'original_verified':True,'save_reopen_verified':True}
        (output/'offline_validation.json').write_text(json.dumps(report,indent=2))
        print('OFFLINE_VALIDATION_COMPLETE '+json.dumps({k:v for k,v in report.items() if k not in ('variants','assessment')}),flush=True)
    finally:
        maya.standalone.uninitialize()


if __name__=='__main__':
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.stdout.flush();sys.stderr.flush()
        os._exit(1)
