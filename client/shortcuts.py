# OWNER: P2
"""shortcuts.py: Keyboard shortcut handling through register_shortcut.

G: grid and snap-to-grid
------------------------
G toggles the grid display AND snapping together, so what you see always
matches what happens: with the grid shown, every point a tool receives is
snapped to config.GRID (canvas.snap) before anything is sent, so the server
only stores snapped values (contract D). Both are local view options; other
users are not affected. The state is shown as 'Snap on' / 'Snap off' in the
status bar (canvas.py) and briefly as a toast.

It is registered as a View menu action with the shortcut G (one key binding
only, so Qt never sees an ambiguous shortcut). Like the tool keys, a plain G
typed into a text field (chat) goes to that field, not to the shortcut.

Tool keys (P, M, H, E, L, A, R, O, V) are bound by main_window's tool bar
from each tool's `shortcut` attribute; the Delete key is registered by the
select tool.
"""

from client import registry
from common import config


def toggle_grid_snap(app):
    """Turn the grid display and snapping on or off together (G)."""
    canvas = getattr(app, "canvas", None)
    if canvas is None:
        return
    on = not canvas.snap_enabled
    canvas.set_show_grid(on)
    canvas.set_snapping(on)
    app.toast(("Snap on (grid %d units)" % config.GRID) if on else "Snap off")


registry.register_menu_action("View", "Grid and snap on/off", toggle_grid_snap, shortcut="G")
