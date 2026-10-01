import threading
import time
import unittest
from unittest import mock

from shotlight.runtime import Budget, Cancelled, TimedOut, RenderWatchdog, SceneSession
from shotlight import planner


class RuntimeTests(unittest.TestCase):
    def test_reopening_same_file_invalidates_work_from_previous_document(self):
        scene = SceneSession('/shots/a.ma')
        token = scene.token
        scene.begin_change()
        scene.finish_change('/shots/a.ma')
        with self.assertRaisesRegex(Cancelled, 'Scene changed'):
            scene.check(token)

    def test_same_basename_in_other_folder_cannot_accept_old_work(self):
        scene = SceneSession('/shot_one/shot.ma')
        with self.assertRaises(Cancelled):
            scene.check(scene.token, '/shot_two/shot.ma')

    def test_scene_load_blocks_work_before_filename_updates(self):
        scene = SceneSession('/shots/a.ma')
        scene.begin_change()
        with self.assertRaises(Cancelled):
            scene.check(scene.token)

    def test_slow_render_requests_abort_and_is_never_reported_complete(self):
        aborted = threading.Event()
        start = time.monotonic()
        with self.assertRaises(TimedOut):
            with RenderWatchdog(Budget(5), .08, aborted.set):
                self.assertTrue(aborted.wait(1))
        self.assertLess(time.monotonic() - start, .6)

    def test_cancel_interrupts_render_and_stops_watchdog_before_next_job(self):
        cancelled = threading.Event()
        aborted = threading.Event()
        timer = threading.Timer(.08, cancelled.set)
        timer.start()
        with self.assertRaises(Cancelled):
            with RenderWatchdog(Budget(5, cancelled), 3, aborted.set) as watchdog:
                self.assertTrue(aborted.wait(1))
        timer.join()
        self.assertFalse(watchdog.thread.is_alive())

    def test_success_never_aborts_a_subsequent_render(self):
        abort = mock.Mock()
        with RenderWatchdog(Budget(5), 3, abort) as watchdog:
            pass
        self.assertFalse(watchdog.thread.is_alive())
        abort.assert_not_called()

    def test_expired_budget_stops_local_planner_before_work(self):
        control = Budget(1)
        control.started -= 2
        with self.assertRaises(TimedOut):
            planner.generate_versions({}, config={"control": control})
