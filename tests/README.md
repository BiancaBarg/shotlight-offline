# Tests

Core: `python3 -m unittest discover -s tests -q` from the repository root.

Normal Python skips native Maya/UI checks. Run `test_maya_adapter.py` and `test_ui_scene.py` with Maya's mayapy, a separate MAYA_APP_DIR and disposable scenes. UI tests may use QT_QPA_PLATFORM=offscreen. Tests create and replace scenes: never run them inside an artist session.
