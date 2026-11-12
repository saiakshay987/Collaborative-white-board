# OWNER: P1
"""registry.py: The client's extension points (contract section G, frozen).

Every feature plugs into the app by registering itself here at import time;
client/bootstrap.py imports all modules, then main_window.py builds the GUI
from whatever is registered. Nobody edits someone else's file to add a tool,
panel, menu entry, renderer, editor, shortcut or message handler.

    register_tool(cls)                               toolbar + canvas tools
    register_renderer(kind, fn(painter, obj, ctx))   paints one board object kind
    register_editor(kind, fn(app, canvas, obj))      double-click editor (select tool)
    register_panel(name, factory(app), area, title)  dock widgets
    register_menu_action(menu, label, fn(app), shortcut=None)
    register_shortcut(keyseq, fn(app))
    @on_message(type)  fn(app, msg)                  incoming network messages
    @on_session_reset  fn(app)                       clear this module's per-session state
                                                     when the user leaves the room

Registration happens on the main thread during import; afterwards the
tables are only read, so no locking is needed.
"""

import traceback
from collections import OrderedDict

from common import protocol

# --- Tables (read by main_window, canvas, render and net_client) -------------

TOOLS = OrderedDict()      # tool id -> tool class, in registration order
RENDERERS = {}             # kind -> fn(painter, obj, ctx)
EDITORS = {}               # kind -> fn(app, canvas, obj)
PANELS = OrderedDict()     # name -> {"name", "factory", "area", "title"}
MENU_ACTIONS = []          # [{"menu", "label", "fn", "shortcut"}], in order
SHORTCUTS = []             # [{"keyseq", "fn"}]
MESSAGE_HANDLERS = {}      # message type -> [fn(app, msg), ...]
SESSION_RESETTERS = []     # [fn(app), ...] run by AppState.reset_session

# Dock areas a panel may ask for. Strings keep PyQt out of feature modules'
# registration calls; main_window maps them to Qt.DockWidgetArea.
PANEL_AREAS = ("left", "right", "bottom", "top")


def register_tool(cls):
    """Register a tool class (attributes id, label, shortcut, group).

    Returns cls, so it also works as a class decorator.
    """
    tool_id = getattr(cls, "id", None)
    if not tool_id:
        raise ValueError("tool class %s has no id" % cls.__name__)
    if tool_id in TOOLS:
        raise ValueError("tool id %r registered twice" % tool_id)
    TOOLS[tool_id] = cls
    return cls


def register_renderer(kind, fn):
    """Register fn(painter, obj, ctx) as the painter for board objects of kind."""
    if kind in RENDERERS:
        raise ValueError("renderer for kind %r registered twice" % kind)
    RENDERERS[kind] = fn
    return fn


def register_editor(kind, fn):
    """Register fn(app, canvas, obj), opened by the select tool on double-click."""
    if kind in EDITORS:
        raise ValueError("editor for kind %r registered twice" % kind)
    EDITORS[kind] = fn
    return fn


def register_panel(name, factory, area="right", title=None):
    """Register a dock panel. factory(app) returns the QWidget to dock.

    area is one of PANEL_AREAS; title defaults to name.
    """
    if area not in PANEL_AREAS:
        raise ValueError("panel area must be one of %s" % (PANEL_AREAS,))
    if name in PANELS:
        raise ValueError("panel %r registered twice" % name)
    PANELS[name] = {"name": name, "factory": factory, "area": area, "title": title or name}
    return factory


def register_menu_action(menu, label, fn, shortcut=None):
    """Register a menu entry: menu title (e.g. "File"), label, fn(app), optional key."""
    MENU_ACTIONS.append({"menu": menu, "label": label, "fn": fn, "shortcut": shortcut})
    return fn


def register_shortcut(keyseq, fn):
    """Register a window-wide keyboard shortcut (e.g. "Ctrl+Z") calling fn(app)."""
    SHORTCUTS.append({"keyseq": keyseq, "fn": fn})
    return fn


def on_message(msg_type):
    """Decorator: call fn(app, msg) for every incoming message of msg_type.

    Several handlers per type are allowed; they run in registration order
    on the GUI thread (net_client delivers through a Qt signal). An
    off-contract type fails at import time.
    """
    if msg_type not in protocol.MESSAGE_TYPES:
        raise ValueError("on_message for unknown message type %r" % (msg_type,))

    def register(fn):
        """Append fn to the handler list of msg_type and return it."""
        MESSAGE_HANDLERS.setdefault(msg_type, []).append(fn)
        return fn

    return register


def on_session_reset(fn):
    """Decorator: call fn(app) when the session ends (Disconnect, kick, connection lost for good).

    Every module that keeps per-room state (board, chat, versions list, ...)
    registers a function that empties it, so the next JOIN_OK / SNAPSHOT /
    CHAT_HISTORY / VERSIONS start from a clean slate and nothing is shown
    twice. Runs on the GUI thread, in registration order.
    """
    SESSION_RESETTERS.append(fn)
    return fn


# --- Lookups -----------------------------------------------------------------

def get_renderer(kind):
    """Return the renderer for kind, or None."""
    return RENDERERS.get(kind)


def get_editor(kind):
    """Return the editor for kind, or None."""
    return EDITORS.get(kind)


def run_session_resetters(app):
    """Call every on_session_reset function (GUI thread only).

    Used by AppState.reset_session. A failing function is reported and does
    not stop the others, so one buggy module cannot leave the rest dirty.
    """
    for fn in SESSION_RESETTERS:
        try:
            fn(app)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def deliver_message(app, msg):
    """Call every on_message handler for msg["type"] (GUI thread only).

    Used by net_client. A failing handler is reported and does not stop
    the others.
    """
    for fn in MESSAGE_HANDLERS.get(msg.get("type"), ()):
        try:
            fn(app, msg)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
