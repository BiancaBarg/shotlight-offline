"""Fast shot lighting and cached versions; local analysis stays off the UI thread."""

import tempfile
import threading
import json
import time
from pathlib import Path

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance

import maya.OpenMayaUI as omui
import maya.api.OpenMaya as om
import maya.utils

from . import __version__, maya_adapter, planner, workflow
from .runtime import Budget, SceneSession

_WINDOW = None


class _Signals(QtCore.QObject):
    status = QtCore.Signal(object, str)
    finished = QtCore.Signal(object, object)
    failed = QtCore.Signal(object, str)


class ShotLightWindow(QtWidgets.QDialog):
    def __init__(self, parent=None, camera=None):
        super().__init__(parent)
        self.setWindowTitle("ShotLight")
        self.setMinimumSize(490, 570)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self._running = False
        self._cancelled = threading.Event()
        self._signals = _Signals(self)
        self._signals.status.connect(self._accept_status)
        self._signals.finished.connect(self._accept_result)
        self._signals.failed.connect(self._accept_failure)
        self._last_result = None
        self._camera = camera
        self._stage = "Ready"
        self._control = None
        self._progress_dir = None
        self._scene = SceneSession(self._scene_path())
        self._job_scene_token = None
        self._scene_callbacks = []
        self._clock = QtCore.QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)
        heading = QtWidgets.QLabel("ShotLight")
        font = heading.font()
        font.setPointSize(24)
        font.setBold(True)
        heading.setFont(font)
        root.addWidget(heading)
        subtitle = QtWidgets.QLabel("Automatic lighting · runs locally in Maya.")
        subtitle.setStyleSheet("font-size: 14px;")
        root.addWidget(subtitle)
        details = QtWidgets.QLabel("1. Select your shot camera in Maya.\n2. Set the timeline playback start and end to that shot.\n3. Press Light Shot, then choose a lighting version.")
        details.setWordWrap(True)
        root.addWidget(details)
        self.scene_label = QtWidgets.QLabel()
        self.scene_label.setWordWrap(True)
        self.scene_label.setStyleSheet("font-size: 11px; color: #b8b8b8;")
        root.addWidget(self.scene_label)
        self.light_button = QtWidgets.QPushButton("Light Shot")
        self.light_button.setAutoDefault(False)
        self.light_button.setMinimumHeight(58)
        self.light_button.setStyleSheet("QPushButton {background: #566fe2; color: white; border: none; border-radius: 7px; font-size: 18px; font-weight: bold;} QPushButton:hover {background: #647ded;} QPushButton:disabled {background: #424954; color: #afb6c2;}")
        self.light_button.clicked.connect(self._start)
        root.addWidget(self.light_button)
        versions = QtWidgets.QHBoxLayout()
        versions.addWidget(QtWidgets.QLabel("Lighting version"))
        self.version_combo = QtWidgets.QComboBox()
        self.version_combo.addItem("Create lighting to get three choices")
        self.version_combo.setEnabled(False)
        self.version_combo.setToolTip("The first choice is Recommended. Applies the chosen editable rig immediately, from the cached local plans. Maya Undo restores the previous rig.")
        self.version_combo.setPlaceholderText("Original · no ShotLight lights")
        self.version_combo.activated.connect(self._switch)
        versions.addWidget(self.version_combo, 1)
        self.original_button = QtWidgets.QPushButton("Original")
        self.original_button.setMaximumWidth(80)
        self.original_button.setToolTip("Remove ShotLight lights and return to your original scene lighting. Keep the three versions available.")
        self.original_button.clicked.connect(self._restore)
        self.remove_button = self.original_button
        versions.addWidget(self.original_button)
        root.addLayout(versions)
        checks = QtWidgets.QHBoxLayout()
        self.render_button = QtWidgets.QPushButton("Preview this version")
        self.render_button.setEnabled(False)
        self.render_button.clicked.connect(self._preview)
        self.review_button = QtWidgets.QPushButton("Check shot (3 frames)")
        self.review_button.setEnabled(False)
        self.review_button.setToolTip("Review small renders at the start, middle and end. Stops at a 90-second job budget; Maya may need extra time to finish renderer cleanup.")
        self.review_button.clicked.connect(self._review)
        checks.addWidget(self.render_button)
        checks.addWidget(self.review_button)
        root.addLayout(checks)
        self.status_label = QtWidgets.QLabel("Ready · Offline · Maya / Arnold · " + __version__)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.result_text = QtWidgets.QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setPlaceholderText("The lighting decisions and preview results will appear here.")
        root.addWidget(self.result_text, 1)
        actions = QtWidgets.QHBoxLayout()
        self.preview_button = QtWidgets.QPushButton("View previews")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self._show_previews)
        self.cancel_button = QtWidgets.QPushButton("Stop")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._stop)
        actions.addWidget(self.preview_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        credit = QtWidgets.QLabel("made by Bianca Bargan 😊")
        credit.setStyleSheet("font-size: 11px; color: #a6a6a6;")
        credit.setAlignment(QtCore.Qt.AlignRight)
        root.addWidget(credit)
        for button in self.findChildren(QtWidgets.QPushButton):
            button.setAutoDefault(False)
        for event in (om.MSceneMessage.kBeforeOpen, om.MSceneMessage.kBeforeNew):
            self._scene_callbacks.append(om.MSceneMessage.addCallback(event, self._begin_scene_change))
        for event in (om.MSceneMessage.kAfterOpen, om.MSceneMessage.kAfterNew):
            self._scene_callbacks.append(om.MSceneMessage.addCallback(event, self._end_scene_change))
        self._scene_callbacks.append(om.MSceneMessage.addCallback(om.MSceneMessage.kAfterSave, self._refresh_scene))
        self._scene_poll = QtCore.QTimer(self)
        self._scene_poll.setInterval(1000)
        self._scene_poll.timeout.connect(self._refresh_scene)
        self._scene_poll.start()
        self.destroyed.connect(self._disconnect_scene)
        self._refresh_scene()

    @staticmethod
    def _scene_path():
        return maya_adapter._cmds().file(query=True, sceneName=True) or ""

    def _clear_scene_cache(self):
        self._last_result = None
        self._camera = None
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItem("Create lighting for this scene")
        self.version_combo.blockSignals(False)
        self.result_text.clear()
        for window in self.findChildren(QtWidgets.QDialog):
            if window.objectName() == "ShotLightPreview":
                window.close()
        for control in (self.version_combo, self.render_button, self.review_button, self.preview_button,
                        self.original_button):
            control.setEnabled(False)

    def _begin_scene_change(self, *unused):
        self._scene.begin_change()
        self._cancelled.set()
        self._clear_scene_cache()
        self.light_button.setEnabled(False)
        self._stage = "Scene changed · stopping previous work…" if self._running else "Opening a different scene…"
        self.status_label.setText(self._stage)

    def _end_scene_change(self, *unused):
        self._scene.finish_change(self._scene_path())
        self._refresh_scene()
        self._scene_ready()

    def _scene_ready(self):
        self._stage = ("Scene changed · stopping previous work…" if self._running else
                       "New scene detected · select its camera and playback range, then press Light Shot.")
        self.status_label.setText(self._stage)
        self.light_button.setEnabled(not self._running and not self._scene.changing)
        if not self._running and not self._scene.changing:
            self.original_button.setEnabled(bool(maya_adapter._owned_groups(maya_adapter._cmds())))

    def _refresh_scene(self, *unused):
        if self._scene.changing:
            return
        path = self._scene_path()
        if path != self._scene.path:
            self._begin_scene_change()
            self._end_scene_change()
            return
        cmds = maya_adapter._cmds()
        try:
            camera = maya_adapter.selected_camera().split("|")[-1]
        except maya_adapter.MayaAdapterError:
            camera = (self._last_result or {}).get("camera", "Select a shot camera").split("|")[-1]
        start = cmds.playbackOptions(query=True, minTime=True)
        end = cmds.playbackOptions(query=True, maxTime=True)
        self.scene_label.setText("Scene: %s\nCamera: %s · Range: %g–%g" % (Path(path).name if path else "Untitled", camera, start, end))
        self.scene_label.setToolTip(path or "Unsaved Maya scene")

    def _disconnect_scene(self, *unused):
        for callback in self._scene_callbacks:
            try:
                om.MMessage.removeCallback(callback)
            except RuntimeError:
                pass
        self._scene_callbacks.clear()

    def _invoke(self, function):
        token = self._job_scene_token
        def current_scene_only():
            self._scene.check(token, self._scene_path())
            self._control.check()
            return function()
        return maya.utils.executeInMainThreadWithResult(current_scene_only)

    @QtCore.Slot(object, str)
    def _accept_status(self, token, message):
        if token == self._scene.token and not self._scene.changing:
            self._set_status(message)

    def _accept_completion(self, token):
        self._refresh_scene()
        if token != self._scene.token or self._scene.changing:
            self._unlock()
            self._clear_scene_cache()
            self._scene_ready()
            return False
        return True

    @QtCore.Slot(object, object)
    def _accept_result(self, token, result):
        if self._accept_completion(token):
            self._finished(result)

    @QtCore.Slot(object, str)
    def _accept_failure(self, token, message):
        if self._accept_completion(token):
            self._failed(message)

    @QtCore.Slot(str)
    def _set_status(self, value):
        self._stage = value
        self._tick() if self._running else self.status_label.setText(value)

    def _report(self, message):
        if self._progress_dir and self._control:
            try:
                Path(self._progress_dir, "progress.json").write_text(json.dumps({
                    "status": message, "elapsed": round(self._control.elapsed, 1),
                    "remaining": round(self._control.remaining, 1), "limit": self._control.seconds,
                    "updated_at": time.time(), "running": self._running}), encoding="utf-8")
            except OSError:
                pass
        self._signals.status.emit(self._job_scene_token or self._scene.token, message)

    def _tick(self):
        if not self._running or self._control is None:
            return
        if self._job_scene_token != self._scene.token or self._scene.changing:
            self.status_label.setText("Scene changed · stopping previous work…")
            return
        if self._cancelled.is_set():
            self.status_label.setText("Stopping the current operation; keeping your lights…")
        elif self._control.remaining <= 0:
            self.status_label.setText("Time limit reached · stopping the current operation…")
        else:
            self.status_label.setText("%s · %.0fs elapsed / %.0fs limit" % (self._stage, self._control.elapsed, self._control.seconds))

    def _stop(self):
        self._cancelled.set()
        self.cancel_button.setEnabled(False)
        self._tick()

    def _start(self):
        self._refresh_scene()
        if self._running:
            return
        try:
            camera = maya_adapter.selected_camera()
        except maya_adapter.MayaAdapterError as error:
            self._failed(str(error))
            return
        self.status_label.setText("Shot camera: " + camera.split("|")[-1])
        directory = tempfile.mkdtemp(prefix="shotlight-")
        self._progress_dir = directory
        self.result_text.clear()
        self._job("create", lambda: workflow.fast_light_shot(
            maya_adapter, directory, config={},
            status=self._report, invoke=self._invoke,
            cancelled=self._cancelled, camera=camera, control=self._control))

    def _job(self, operation, function):
        self._refresh_scene()
        if self._running or self._scene.changing:
            return
        self._job_scene_token = self._scene.token
        token = self._job_scene_token
        self._operation = operation
        self._cancelled.clear()
        self._running = True
        limits = {"create": 90, "review": 90, "preview": 30, "switch": 15, "original": 15}
        self._control = Budget(limits[operation], self._cancelled, self._report)
        if operation != "create" and self._last_result:
            self._progress_dir = self._last_result.get("output_dir")
        self._clock.start()
        for control in (self.light_button, self.remove_button, self.preview_button,
                        self.version_combo, self.render_button, self.review_button):
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)

        def run():
            try:
                self._scene.check(token)
                self._signals.finished.emit(token, function())
            except Exception as error:
                # Providers deliberately redact credentials in their errors.
                self._signals.failed.emit(token, str(error))
        threading.Thread(target=run, name="ShotLightLocal", daemon=True).start()

    def _switch(self, index):
        self._refresh_scene()
        if not self._last_result or self._running or index == self._last_result["active_version"]:
            return
        self.status_label.setText("Applying lighting version…")
        self._job("switch", lambda: workflow.switch_version(maya_adapter, self._last_result, index,
                  invoke=self._invoke))

    def _preview(self):
        self._refresh_scene()
        if not self._last_result or self._running:
            return
        self._job("preview", lambda: workflow.preview_version(maya_adapter, self._last_result,
                  status=self._report, invoke=self._invoke,
                  cancelled=self._cancelled, control=self._control))

    def _review(self):
        self._refresh_scene()
        if not self._last_result or self._running:
            return
        self._job("review", lambda: workflow.review_version(maya_adapter, self._last_result,
                  config={}, status=self._report,
                  invoke=self._invoke, cancelled=self._cancelled, control=self._control))

    def _restore(self):
        self._refresh_scene()
        if self._running:
            return
        if self._last_result:
            self._job("original", lambda: workflow.restore_original(maya_adapter, self._last_result,
                      invoke=self._invoke))
        else:
            self._remove()

    def _unlock(self):
        self._running = False
        self._clock.stop()
        self.light_button.setEnabled(not self._scene.changing)
        self.remove_button.setEnabled(not self._scene.changing and bool(
            self._last_result or maya_adapter._owned_groups(maya_adapter._cmds())))
        self.cancel_button.setEnabled(False)
        available = bool(self._last_result and self._last_result.get("versions"))
        self.version_combo.setEnabled(available)
        active = available and self._last_result.get("active_version") is not None
        self.render_button.setEnabled(active)
        self.review_button.setEnabled(active)
        self.preview_button.setEnabled(bool(self._last_result and self._last_result.get("after")))
        self._report(self._stage)

    @QtCore.Slot(object)
    def _finished(self, result):
        self._last_result = result
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for index, version in enumerate(result.get("versions", [])):
            label = version["label"]
            if index == 0 and "recommended" not in label.casefold():
                label = "Recommended · " + label
            self.version_combo.addItem(label)
        index = result.get("active_version", 0)
        self.version_combo.setCurrentIndex(-1 if index is None else index)
        self.version_combo.blockSignals(False)
        self._unlock()
        if result.get("mode") == "original":
            self.status_label.setText("Original scene lighting restored.")
            self.result_text.setPlainText("ShotLight lights removed. Your original scene lights remain.\n\nChoose a lighting version above to apply it again.")
            self._report(self.status_label.text())
            return
        text = result["plan"]["summary"] + "\n\n" + result["plan"]["look_description"]
        text += "\n\n%s editable lights · %.1f seconds" % (len(result["plan"]["lights"]), result["seconds"])
        if result.get("review_seconds") and result.get("assessment"):
            text += "\nSampled exposure check · %.1f seconds" % result["review_seconds"]
        if result.get("warning"):
            text += "\n\n" + result["warning"]
        elif result.get("assessment"):
            text += "\n\nLocal exposure measurements checked %s sampled Arnold frames. This does not judge composition or artistic quality; inspect the previews." % len(result["frames"])
        else:
            text += "\n\nQuick draft. Choose between three lighting versions above. Use Preview this version to compare the render, or Check shot (3 frames) for local exposure tuning at the start, middle and end."
        text += "\n\nCamera: " + result["camera"].split("|")[-1]
        self.result_text.setPlainText(text)
        self.preview_button.setEnabled(bool(result.get("after")))
        if getattr(self, "_operation", "create") == "switch":
            self.status_label.setText("Applied " + self.version_combo.currentText() + " · preview or check this version.")
        self._report(self.status_label.text())
        if result.get("after") and getattr(self, "_operation", "create") != "switch":
            self._show_previews()

    def _show_previews(self):
        self._refresh_scene()
        result = self._last_result
        if not result or not result.get("after"):
            return
        window = QtWidgets.QDialog(self)
        window.setObjectName("ShotLightPreview")
        window.setWindowTitle("ShotLight · rendered previews")
        window.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        outer = QtWidgets.QVBoxLayout(window)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        window.resize(920, 740)
        grid.addWidget(QtWidgets.QLabel("Before · shot reference"), 0, 0)
        grid.addWidget(QtWidgets.QLabel("After · Arnold test render"), 0, 1)
        shown_frames = {preview["frame"] for preview in result.get("after", [])}
        for column, key in enumerate(("before", "after")):
            previews = [p for p in result.get(key, []) if p["frame"] in shown_frames]
            for row, preview in enumerate(previews, 1):
                panel = QtWidgets.QWidget()
                layout = QtWidgets.QVBoxLayout(panel)
                label = QtWidgets.QLabel("Frame %g" % preview["frame"])
                picture = QtWidgets.QLabel()
                pixmap = QtGui.QPixmap(preview["path"])
                picture.setPixmap(pixmap.scaled(420, 236, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
                layout.addWidget(label)
                layout.addWidget(picture)
                grid.addWidget(panel, row, column)
        window.show()

    @QtCore.Slot(str)
    def _failed(self, message):
        self._unlock()
        if self._last_result and self._last_result.get("versions"):
            index = self._last_result["active_version"]
            self.version_combo.setCurrentIndex(-1 if index is None else index)
        if message.startswith("Stopped."):
            self.status_label.setText("Stopped · existing lighting kept.")
        elif "time limit" in message or "timed out" in message:
            self.status_label.setText("Time limit reached · existing lighting kept.")
        else:
            self.status_label.setText("Could not finish lighting.")
        self._report(self.status_label.text())
        self.result_text.setPlainText(message)

    def _remove(self):
        self._refresh_scene()
        try:
            maya_adapter.remove_rig()
            self._last_result = None
            self.version_combo.clear()
            self.version_combo.addItem("Create lighting to get three choices")
            self._unlock()
            self.status_label.setText("ShotLight lighting removed.")
        except Exception as error:
            self.result_text.setPlainText(str(error))

    def closeEvent(self, event):
        if self._running:
            self._cancelled.set()
            self.status_label.setText("Stopping after the current operation…")
            event.ignore()
        else:
            self._scene_poll.stop()
            self._disconnect_scene()
            global _WINDOW
            if _WINDOW is self:
                _WINDOW = None
            event.accept()

    def reject(self):
        # Escape follows the same safe stop path as the window close button.
        self.close()


def show():
    global _WINDOW
    if _WINDOW is not None:
        try:
            _WINDOW.show()
            _WINDOW.raise_()
            _WINDOW.activateWindow()
            return _WINDOW
        except RuntimeError:
            _WINDOW = None
    pointer = omui.MQtUtil.mainWindow()
    parent = wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None
    try:
        camera = maya_adapter._resolve_camera(maya_adapter._cmds(), None)
    except maya_adapter.MayaAdapterError:
        camera = None
    _WINDOW = ShotLightWindow(parent, camera=camera)
    _WINDOW.show()
    return _WINDOW
