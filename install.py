"""Drag this file into Maya's viewport to add the ShotLight shelf button."""

import sys
from pathlib import Path


def install():
    import maya.cmds as cmds
    import maya.mel as mel
    root = str(Path(__file__).resolve().parent)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    # Refresh an already installed copy so an upgrade cannot retain its old engine.
    import importlib
    old_ui = sys.modules.get("shotlight.ui")
    window = getattr(old_ui, "_WINDOW", None)
    if window is not None:
        if getattr(window, "_running", False):
            raise RuntimeError("Stop the current ShotLight job before installing the update.")
        window.close()
    for module in list(sys.modules):
        if module == "shotlight" or module.startswith("shotlight."):
            del sys.modules[module]
    importlib.invalidate_caches()
    shelf_root = mel.eval("$tmp = $gShelfTopLevel")
    shelf = cmds.shelfTabLayout(shelf_root, query=True, selectTab=True)
    if not shelf:
        shelf = cmds.shelfLayout("ShotLight", parent=shelf_root)
    for child in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        if cmds.objectTypeUI(child) == "shelfButton" and cmds.shelfButton(child, query=True, docTag=True) == "shotlight-launcher":
            cmds.deleteUI(child)
    command = "import sys\np = %r\nif p not in sys.path: sys.path.insert(0, p)\nimport shotlight\nshotlight.show()" % root
    cmds.shelfButton(parent=shelf, label="ShotLight", annotation="Local shot-aware lighting · no account required",
                     image="render_useBackground.png", imageOverlayLabel="LIGHT", sourceType="python",
                     command=command, docTag="shotlight-launcher")
    import shotlight
    shotlight.show()


def onMayaDroppedPythonFile(*args):
    install()


if __name__ == "__main__":
    install()
