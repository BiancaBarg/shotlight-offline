"""Scene-lifecycle integration tests; only disposable native Maya scenes."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

try:
    import maya.cmds as cmds
    MAYA_AVAILABLE = bool(hasattr(cmds, 'file'))
except ImportError:
    MAYA_AVAILABLE = False


@unittest.skipUnless(MAYA_AVAILABLE, 'Maya UI integration test')
class SceneUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1]))
        from shotlight import ui
        from shotlight.runtime import Cancelled
        cls.ui = ui
        cls.cancel_type = Cancelled
        cls.app = ui.QtWidgets.QApplication.instance() or ui.QtWidgets.QApplication([])

    def setUp(self):
        cmds.file(new=True, force=True)
        self.window = self.ui.ShotLightWindow()
        self.addCleanup(self.close_window)

    def close_window(self):
        try:
            self.window.close()
        except RuntimeError:
            pass

    def seed_old_choices(self):
        self.window._last_result = {'versions': [{'label': 'Old lighting'}],
                                    'active_version': 0, 'camera': '|OldCam|OldCamShape'}
        self.window.version_combo.clear()
        self.window.version_combo.addItem('Old lighting')
        self.window.version_combo.setEnabled(True)
        self.window.result_text.setPlainText('Old scene lighting decisions')

    def test_new_scene_clears_choices_closes_previews_and_keeps_artist_nodes(self):
        self.seed_old_choices()
        preview = self.ui.QtWidgets.QDialog(self.window)
        preview.setObjectName('ShotLightPreview')
        preview.show()
        cmds.file(new=True, force=True)
        artist = cmds.directionalLight(name='NewSceneArtistKey')
        self.app.processEvents()
        self.assertIsNone(self.window._last_result)
        self.assertFalse(self.window.version_combo.isEnabled())
        self.assertNotIn('Old scene', self.window.result_text.toPlainText())
        self.assertFalse(preview.isVisible())
        self.assertTrue(cmds.objExists(artist))
        self.assertIn('Untitled', self.window.scene_label.text())

    def test_reopen_same_file_rejects_queued_work_and_late_old_results(self):
        with tempfile.TemporaryDirectory(prefix='shotlight-scene-session-') as folder:
            path = str(Path(folder, 'same.ma'))
            cmds.file(rename=path)
            cmds.file(save=True, type='mayaAscii')
            self.seed_old_choices()
            old_token = self.window._scene.token
            self.window._job_scene_token = old_token
            self.window._control = self.ui.Budget(90)
            self.window._running = True
            cmds.file(path, open=True, force=True)
            self.assertTrue(self.window._cancelled.is_set())
            mutation = mock.Mock()
            with mock.patch.object(self.ui.maya.utils, 'executeInMainThreadWithResult', side_effect=lambda fn: fn()):
                with self.assertRaisesRegex(self.cancel_type, 'Scene changed'):
                    self.window._invoke(mutation)
            mutation.assert_not_called()
            self.window._accept_result(old_token, {'versions': [{'label': 'Stale'}]})
            self.assertFalse(self.window._running)
            self.assertIsNone(self.window._last_result)
            self.assertIn('New scene detected', self.window.status_label.text())
            self.window._accept_status(old_token, 'Old AI work finished')
            self.assertNotIn('Old AI', self.window.status_label.text())

    def test_other_folder_same_basename_updates_path_and_range(self):
        with tempfile.TemporaryDirectory(prefix='shotlight-scene-session-') as folder:
            paths = [Path(folder, part, 'shot.ma') for part in ('one', 'two')]
            for path in paths:
                path.parent.mkdir()
                cmds.file(new=True, force=True)
                cmds.file(rename=str(path))
                cmds.file(save=True, type='mayaAscii')
            cmds.file(str(paths[0]), open=True, force=True)
            self.seed_old_choices()
            cmds.file(str(paths[1]), open=True, force=True)
            cmds.playbackOptions(minTime=10, maxTime=50)
            self.window._refresh_scene()
            self.assertIsNone(self.window._last_result)
            self.assertEqual(self.window.scene_label.toolTip(), str(paths[1]))
            self.assertIn('10–50', self.window.scene_label.text())

    def test_closing_window_removes_document_callbacks(self):
        self.assertEqual(len(self.window._scene_callbacks), 5)
        self.window.close()
        self.assertEqual(self.window._scene_callbacks, [])

    def test_same_file_save_and_scrubbing_keep_current_choices(self):
        with tempfile.TemporaryDirectory(prefix='shotlight-scene-session-') as folder:
            cmds.file(rename=str(Path(folder, 'current.ma')))
            cmds.file(save=True, type='mayaAscii')
            self.seed_old_choices()
            result = self.window._last_result
            token = self.window._scene.token
            cmds.currentTime(9)
            cmds.file(save=True, type='mayaAscii')
            self.window._refresh_scene()
            self.assertIs(self.window._last_result, result)
            self.assertEqual(self.window._scene.token, token)

    def test_real_worker_signal_completes_on_current_document(self):
        result = {'versions': [{'label': 'Current choice'}], 'active_version': None,
                  'mode': 'original', 'after': []}
        self.window._job('original', lambda: result)
        deadline = time.monotonic() + 2
        while self.window._running and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(self.window._running)
        self.assertIs(self.window._last_result, result)
        self.assertEqual(self.window.status_label.text(), 'Original scene lighting restored.')
        self.assertFalse(self.window.cancel_button.isEnabled())


if __name__ == '__main__':
    import sys
    # Standalone otherwise creates QCoreApplication, which cannot own widgets.
    try:
        from PySide6 import QtWidgets
    except ImportError:
        from PySide2 import QtWidgets
    qt_app = QtWidgets.QApplication([])
    import maya.standalone
    maya.standalone.initialize(name='python')
    SceneUITests.__unittest_skip__ = False
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        maya.standalone.uninitialize()
    sys.exit(0 if result.wasSuccessful() else 1)
