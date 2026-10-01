"""ShotLight: Local, shot-aware, editable shot lighting. Maya/Arnold prototype."""

__version__ = "0.2.0"


def show():
    """Open the Maya panel without importing Maya in the shared core."""
    from .ui import show as open_panel
    return open_panel()
