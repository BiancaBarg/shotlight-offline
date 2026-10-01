"""Bounded operations and cooperative cancellation, independent of Maya."""

import threading
import time


class Cancelled(RuntimeError):
    pass


class TimedOut(RuntimeError):
    pass


class SceneSession:
    """Document generation, independent of shot cameras and playback scrubbing."""
    def __init__(self, path):
        self.path = path
        self.revision = 0
        self.changing = False

    @property
    def token(self):
        return (self.revision, self.path)

    def begin_change(self):
        self.revision += 1
        self.changing = True

    def finish_change(self, path):
        self.path = path
        self.changing = False

    def check(self, token, path=None):
        if self.changing or token != self.token or (path is not None and path != self.path):
            raise Cancelled("Scene changed. Work from the previous scene was stopped. Select a camera and playback range in the current scene, then press Light Shot.")


class Budget:
    def __init__(self, seconds=90, cancelled=None, status=None):
        self.started = time.monotonic()
        self.seconds = float(seconds)
        self.cancelled = cancelled if cancelled is not None else threading.Event()
        self.status = status or (lambda message: None)

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    @property
    def remaining(self):
        return max(0.0, self.seconds - self.elapsed)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled("Stopped. Your existing scene and editable lighting are kept.")
        if self.remaining <= 0:
            raise TimedOut("ShotLight reached its time limit. Your editable lighting is kept; the check is incomplete.")


class RenderWatchdog:
    """Request clean renderer abort without calling Maya commands off-thread.

    The owner must stop/join this watchdog before another render may start.
    Renderer cleanup and restoration stay on Maya's normal command path.
    """
    def __init__(self, budget, seconds, abort):
        self.budget = budget
        self.seconds = min(float(seconds), budget.remaining)
        self.abort = abort
        self.done = threading.Event()
        self.error = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._watch, name="ShotLightRenderLimit", daemon=True)

    def _watch(self):
        while not self.done.wait(0.05):
            try:
                self.budget.check()
                if time.monotonic() - self.started >= self.seconds:
                    raise TimedOut("This preview frame exceeded its %.0f second time limit. Your lighting is kept; the preview is incomplete. Try a lighter scene." % self.seconds)
            except (Cancelled, TimedOut) as error:
                self.error = error
                self.budget.status("Stopping the preview; keeping your lighting…" if isinstance(error, Cancelled)
                                   else "Time limit reached · stopping the preview…")
                # Export can precede the render session. Repeat the nonblocking
                # abort until the owning command returns, never into a later job.
                while not self.done.is_set():
                    try:
                        self.abort()
                    except Exception:
                        pass
                    self.done.wait(0.1)
                return

    def __enter__(self):
        self.budget.check()
        self.thread.start()
        return self

    def __exit__(self, kind, value, tb):
        self.done.set()
        self.thread.join()
        if self.error:
            raise self.error
        self.budget.check()
        return False
