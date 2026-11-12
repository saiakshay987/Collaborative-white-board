# OWNER: P2
"""shortcuts.py: Keyboard shortcuts, the AutoCAD-style command bar and the shortcut help.

Everything here plugs in through the registries (contract G):
register_shortcut, register_menu_action. Nothing in this file talks to the
network directly: board changes go through client/ops.send_op, exactly
like the mouse tools, so they are applied locally at once (optimistic,
contract E3) and confirmed by the server's echo.

Keyboard shortcuts
------------------
P M H E         pen, marker, highlighter, eraser
L A R O         line, arrow, rectangle, ellipse
V T N           select, text, sticky note
                A tool is looked up by id when the key is pressed; a tool
                that is not registered (yet) is ignored. main_window's tool
                bar already binds every tool's own `shortcut` attribute, and
                Qt treats a key bound twice in one window as AMBIGUOUS (then
                neither binding fires). So a key is only registered here when
                no registered tool claims it already; bootstrap imports all
                tools before this module, so the check is complete.
G               grid and snap together (View menu action, see below)
+ (or =)  -     zoom in / out around the mouse (or the canvas centre)
0               reset the zoom to 100 %
Escape          cancel the current drawing (canvas.cancel_tool), or close
                the command bar
Ctrl+Z  Ctrl+Y  send UNDO / REDO {} through send_op. There is NO undo
                logic in the client: the server answers with ordinary OP_*
                messages (the inverse operation, contract C2 and E6), which
                BoardModel applies like any other change, in z order. Nothing
                left to undo gives ERROR NOT_FOUND (shown as a toast by
                connect_dialog's ERROR handler); a viewer gets FORBIDDEN, so
                we do not even send it for a viewer.
/               open the command bar
F1              Help > Keyboard shortcuts
Ctrl+S and Ctrl+E are NOT bound here: P3 registers them (export).

Plain keys never steal typing: when a text field (chat, the command bar)
has focus, Qt first offers the key to that field (ShortcutOverride), and a
QLineEdit/QPlainTextEdit accepts printable keys and its own Ctrl+Z/Ctrl+Y,
so window shortcuts only fire while the canvas or a button has focus.

G: grid and snap-to-grid
------------------------
G toggles the grid display AND snapping together, so what you see always
matches what happens: with the grid shown, every point a tool receives is
snapped to the grid (canvas.snap) before anything is sent, so the server
only stores snapped values (contract D). Both are local view options; other
users are not affected. It is a View menu action with the shortcut G (one
key binding only, so Qt never sees an ambiguous shortcut).

Command bar
-----------
A line edit at the bottom of the canvas (a child widget laid over it, like
AutoCAD's command line), opened with '/' and closed with Escape:

    line x1,y1 x2,y2      arrow x1,y1 x2,y2     rect x,y w,h
    circle cx,cy r        ellipse x,y w,h       color #rrggbb
    width n               grid n | on | off     snap on | off
    zoom n (percent) | fit                      undo   redo   clear

Coordinates are board units, snapped to the grid when snapping is on.
Shapes are built by the same shape tool classes as a mouse drag
(shape_tools.*.build) with the current pen color and width, validated like
the server does, and sent as ONE OP_ADD each (shape_tools.send_shape).
undo, redo and clear go through send_op. While typing, a hint list shows
the matching commands and their syntax; Tab completes the command name;
Up/Down walk through the history; errors are printed in the bar (red),
never raised.
"""

import difflib
import html
import math
import re
import traceback
from collections import OrderedDict

from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QTextBrowser, QVBoxLayout)

from client import ops, registry, toolbar
from client.canvas import on_page
from client.tools import shape_tools
from common import config, protocol

# --- Tool keys ----------------------------------------------------------------

# (key, tool id, what it is). Looked up in registry.TOOLS when pressed.
TOOL_KEYS = (
    ("P", "pen", "Pen"),
    ("M", "marker", "Marker"),
    ("H", "highlighter", "Highlighter"),
    ("E", "eraser", "Eraser"),
    ("L", "line", "Line"),
    ("A", "arrow", "Arrow"),
    ("R", "rect", "Rectangle"),
    ("O", "ellipse", "Ellipse"),
    ("V", "select", "Select"),
    ("T", "text", "Text"),
    ("N", "sticky", "Sticky note"),
)

# --- Zoom ----------------------------------------------------------------------

ZOOM_KEY_STEP = 1.25          # one + or - press
ZOOM_MIN_PCT = 10             # canvas.MIN_ZOOM as a percentage
ZOOM_MAX_PCT = 800            # canvas.MAX_ZOOM as a percentage

# --- Command bar ---------------------------------------------------------------

GRID_MIN = 5                  # smallest grid spacing 'grid n' accepts (board units)
GRID_MAX = 500                # largest
BAR_MARGIN = 8                # pixels between the bar and the canvas edges
HISTORY_MAX = 100             # commands remembered for Up/Down
HINT_ROWS = 6                 # at most this many matching commands in the hint list
ERROR_STYLE = "QLabel { color: #c62828; }"
INFO_STYLE = "QLabel { color: #2e7d32; }"
HINT_STYLE = "QLabel { color: #555; }"

# command -> (syntax, description), in the order the hints show them.
COMMANDS = OrderedDict([
    ("line", ("line x1,y1 x2,y2", "straight line between two points")),
    ("arrow", ("arrow x1,y1 x2,y2", "arrow from the first point to the second")),
    ("rect", ("rect x,y w,h", "rectangle: top-left corner, width and height")),
    ("circle", ("circle cx,cy r", "circle: centre and radius")),
    ("ellipse", ("ellipse x,y w,h", "ellipse inside the box x,y w,h")),
    ("color", ("color #rrggbb", "pen color of the current tool")),
    ("width", ("width n", "pen width of the current tool (1-40)")),
    ("grid", ("grid n | on | off", "grid spacing in board units (%d-%d), or show/hide it"
              % (GRID_MIN, GRID_MAX))),
    ("snap", ("snap on | off", "snap points to the grid")),
    ("zoom", ("zoom n | fit", "zoom to n percent (%d-%d) or fit the page"
              % (ZOOM_MIN_PCT, ZOOM_MAX_PCT))),
    ("undo", ("undo", "undo my last change (same as Ctrl+Z)")),
    ("redo", ("redo", "redo what I undid (same as Ctrl+Y)")),
    ("clear", ("clear", "clear the whole board (host only)")),
])


# ==============================================================================
# Shortcut actions (all run on the GUI thread, called by QShortcut/QAction)
# ==============================================================================

def _status(app, text, ms=2000):
    """Show text briefly in the main window's status bar (if there is a window)."""
    window = getattr(app, "window", None)
    if window is not None and hasattr(window, "statusBar"):
        window.statusBar().showMessage(text, ms)


def _is_viewer(app):
    """True when the server made me a viewer (the server checks again anyway)."""
    return getattr(app.me, "role", None) == "viewer"


def select_tool_by_id(app, tool_id):
    """Make tool_id the active tool; ignored when that tool is not registered (yet).

    Goes through main_window.select_tool, the single entry point that also
    checks the toolbar button and tells the tool dock (tool_selected).
    """
    if tool_id not in registry.TOOLS:
        return
    if _is_viewer(app):
        app.toast("Viewers cannot draw")
        return
    window = getattr(app, "window", None)
    if window is not None and hasattr(window, "select_tool"):
        window.select_tool(tool_id)


def _tool_selector(tool_id):
    """Return fn(app) that selects tool_id (a separate function per key for the registry)."""
    def select(app):
        """Select the tool bound to this key, if it is registered."""
        select_tool_by_id(app, tool_id)
    select.__doc__ = "Select the %s tool." % tool_id
    return select


def toggle_grid_snap(app):
    """Turn the grid display and snapping on or off together (G)."""
    canvas = getattr(app, "canvas", None)
    if canvas is None:
        return
    on = not canvas.snap_enabled
    canvas.set_show_grid(on)
    canvas.set_snapping(on)
    app.toast(("Snap on (grid %g units)" % canvas.grid_size) if on else "Snap off")


def _zoom_anchor(canvas):
    """Screen point to zoom around: the mouse if it is over the canvas, else None (centre)."""
    pos = canvas.mapFromGlobal(QCursor.pos())
    return QPointF(pos) if canvas.rect().contains(pos) else None


def _zoom_to(app, zoom):
    """Set the canvas zoom (clamped by the canvas) and show the result in the status bar."""
    canvas = getattr(app, "canvas", None)
    if canvas is None:
        return
    canvas.set_zoom(zoom, _zoom_anchor(canvas))
    _status(app, "Zoom %d%%" % round(canvas.zoom * 100))


def zoom_in(app):
    """+ : zoom in one step around the mouse."""
    canvas = getattr(app, "canvas", None)
    if canvas is not None:
        _zoom_to(app, canvas.zoom * ZOOM_KEY_STEP)


def zoom_out(app):
    """- : zoom out one step around the mouse."""
    canvas = getattr(app, "canvas", None)
    if canvas is not None:
        _zoom_to(app, canvas.zoom / ZOOM_KEY_STEP)


def zoom_reset(app):
    """0 : back to 100 % (one board unit = one screen pixel)."""
    _zoom_to(app, 1.0)


def escape(app):
    """Escape: close the command bar if it has focus, else cancel the current drawing.

    Order: a focused command bar closes first; then a drag in progress is
    cancelled (canvas.cancel_tool); then an open but unfocused bar closes;
    otherwise the tool is still asked to cancel (the select tool clears
    its selection).
    """
    canvas = getattr(app, "canvas", None)
    if canvas is None:
        return
    bar = CommandBar.find(canvas)
    if bar is not None and bar.isVisible() and bar.has_focus():
        bar.close_bar()
    elif canvas.is_drawing():
        canvas.cancel_tool()
    elif bar is not None and bar.isVisible():
        bar.close_bar()
    else:
        canvas.cancel_tool()


def _send_history_op(app, msg_type):
    """Send UNDO or REDO with an empty payload through send_op; returns an error text or None.

    send_op makes the op_id (contract B: UNDO/REDO carry one) and calls
    BoardModel.apply_local, which does nothing for UNDO/REDO: the server
    answers with the inverse OP_* (fresh seq, payload cause 'undo'/'redo'),
    broadcast to everyone including me, and that is what changes the board.
    """
    if _is_viewer(app):
        return "Viewers cannot undo or redo"
    ops.send_op(app, msg_type, {})
    return None


def undo(app):
    """Ctrl+Z: ask the server to undo my last change."""
    err = _send_history_op(app, protocol.UNDO)
    if err:
        app.toast(err)


def redo(app):
    """Ctrl+Y: ask the server to redo what I undid."""
    err = _send_history_op(app, protocol.REDO)
    if err:
        app.toast(err)


def open_command_bar(app):
    """/ : open the command bar under the canvas and focus it."""
    canvas = getattr(app, "canvas", None)
    if canvas is not None:
        CommandBar.for_canvas(canvas).open_bar()


# ==============================================================================
# Command bar: parsing and running one command
# ==============================================================================

class CommandError(Exception):
    """A command the user typed is wrong; the message is shown in the bar."""


def _split_args(rest):
    """Split the arguments: '10, 20  30 ,40' -> ['10,20', '30,40'] (spaces around commas ignored)."""
    rest = re.sub(r"\s*,\s*", ",", rest.strip())
    return rest.split() if rest else []


def _numbers(token, count, name):
    """Parse 'a,b' (count numbers separated by commas) into floats, or raise CommandError."""
    parts = token.split(",")
    if len(parts) != count:
        raise CommandError("%s should be %d number%s separated by commas, got '%s'"
                           % (name, count, "s" if count > 1 else "", token))
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise CommandError("%s must be numbers, got '%s'" % (name, token)) from None
    if not all(math.isfinite(v) for v in values):
        raise CommandError("%s must be finite numbers" % name)
    return values


def _expect(args, count, cmd):
    """Raise CommandError unless there are exactly count arguments (shows the syntax)."""
    if len(args) != count:
        raise CommandError("Usage: %s" % COMMANDS[cmd][0])


def _snap(canvas, x, y):
    """(x, y) snapped to the grid when snapping is on (canvas.snap), as a QPointF."""
    pt = QPointF(x, y)
    return canvas.snap(pt) if canvas is not None else pt


def _check_on_board(*points):
    """Raise CommandError if a point lies outside the page (0..BOARD_W x 0..BOARD_H, contract D).

    The points are the ones the shape is built from (both end points of a
    line, both corners of a box), so a shape that would stick out of the
    page on any side is refused before anything is sent.
    """
    for p in points:
        if not on_page(p):
            raise CommandError("(%g, %g) is outside the page: x must be 0-%d and y 0-%d"
                               % (p.x(), p.y(), config.BOARD_W, config.BOARD_H))


def _require_drawer(app):
    """Raise CommandError for a viewer (the server would answer FORBIDDEN)."""
    if _is_viewer(app):
        raise CommandError("Viewers cannot change the board")


def _build_and_send(app, tool_cls, start, end, what):
    """Build a shape with tool_cls.build (same as a mouse drag) and send it; return a message."""
    obj = tool_cls().build(app, start, end)
    ok, reason = shape_tools.send_shape(app, obj)
    if not ok:
        raise CommandError("%s not sent: %s" % (what, reason))
    if app.canvas is not None:
        app.canvas.update()
    return "%s added" % what


def _cmd_two_points(app, args, cmd, tool_cls):
    """line / arrow x1,y1 x2,y2."""
    _require_drawer(app)
    _expect(args, 2, cmd)
    x1, y1 = _numbers(args[0], 2, "x1,y1")
    x2, y2 = _numbers(args[1], 2, "x2,y2")
    a, b = _snap(app.canvas, x1, y1), _snap(app.canvas, x2, y2)
    _check_on_board(a, b)
    if a == b:
        raise CommandError("the two points are the same%s" % _snap_note(app))
    return _build_and_send(app, tool_cls, a, b, cmd.capitalize())


def _cmd_box(app, args, cmd, tool_cls):
    """rect / ellipse x,y w,h: both corners are snapped, so the size stays on the grid."""
    _require_drawer(app)
    _expect(args, 2, cmd)
    x, y = _numbers(args[0], 2, "x,y")
    w, h = _numbers(args[1], 2, "w,h")
    if w <= 0 or h <= 0:
        raise CommandError("width and height must be greater than 0")
    a, b = _snap(app.canvas, x, y), _snap(app.canvas, x + w, y + h)
    _check_on_board(a, b)
    if a.x() == b.x() or a.y() == b.y():
        raise CommandError("too small%s" % _snap_note(app))
    return _build_and_send(app, tool_cls, a, b, "Rectangle" if cmd == "rect" else "Ellipse")


def _cmd_circle(app, args, cmd):
    """circle cx,cy r: an ellipse with w = h = 2r; centre and radius snapped when snap is on."""
    _require_drawer(app)
    _expect(args, 2, cmd)
    cx, cy = _numbers(args[0], 2, "cx,cy")
    (r,) = _numbers(args[1], 1, "r")
    if r <= 0:
        raise CommandError("the radius must be greater than 0")
    centre = _snap(app.canvas, cx, cy)
    canvas = app.canvas
    if canvas is not None and canvas.snap_enabled:
        r = round(r / canvas.grid_size) * canvas.grid_size
        if r <= 0:
            raise CommandError("radius is 0 after snapping to the %g grid" % canvas.grid_size)
    a = QPointF(centre.x() - r, centre.y() - r)
    b = QPointF(centre.x() + r, centre.y() + r)
    _check_on_board(a, b)
    return _build_and_send(app, shape_tools.EllipseTool, a, b, "Circle")


def _snap_note(app):
    """' (snap is on)' when snapping may explain a degenerate shape, else ''."""
    canvas = app.canvas
    return " (snap is on: grid %g)" % canvas.grid_size if canvas is not None and canvas.snap_enabled else ""


def _cmd_color(app, args, cmd):
    """color #rrggbb: through the tool dock, so it shows and remembers it per tool."""
    _expect(args, 1, cmd)
    color = args[0].lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        raise CommandError("color must look like #rrggbb, e.g. #e53935")
    toolbar.set_pen(app, color=color)
    return "Color %s" % color


def _cmd_width(app, args, cmd):
    """width n: 1-40 (contract D), through the tool dock."""
    _expect(args, 1, cmd)
    (width,) = _numbers(args[0], 1, "width")
    if not toolbar.WIDTH_MIN <= width <= toolbar.WIDTH_MAX:
        raise CommandError("width must be %d-%d" % (toolbar.WIDTH_MIN, toolbar.WIDTH_MAX))
    toolbar.set_pen(app, width=int(round(width)))
    return "Width %d" % int(round(width))


def _on_off(token, cmd):
    """'on' -> True, 'off' -> False, else CommandError."""
    word = token.lower()
    if word not in ("on", "off"):
        raise CommandError("Usage: %s" % COMMANDS[cmd][0])
    return word == "on"


def _cmd_grid(app, args, cmd):
    """grid n | on | off: spacing (local view option; also shows the grid) or show/hide."""
    _expect(args, 1, cmd)
    canvas = app.canvas
    if args[0].lower() in ("on", "off"):
        on = _on_off(args[0], cmd)
        canvas.set_show_grid(on)
        return "Grid %s" % ("shown" if on else "hidden")
    (size,) = _numbers(args[0], 1, "grid size")
    if not GRID_MIN <= size <= GRID_MAX:
        raise CommandError("grid size must be %d-%d board units" % (GRID_MIN, GRID_MAX))
    canvas.set_grid_size(size)
    canvas.set_show_grid(True)
    return "Grid %g units%s" % (size, ", snap on" if canvas.snap_enabled else "")


def _cmd_snap(app, args, cmd):
    """snap on | off: snapping only (G toggles grid and snap together)."""
    _expect(args, 1, cmd)
    on = _on_off(args[0], cmd)
    app.canvas.set_snapping(on)
    return "Snap %s (grid %g)" % ("on" if on else "off", app.canvas.grid_size)


def _cmd_zoom(app, args, cmd):
    """zoom n (percent) | fit."""
    _expect(args, 1, cmd)
    canvas = app.canvas
    if args[0].lower() == "fit":
        canvas.fit_page()
    else:
        (pct,) = _numbers(args[0].rstrip("%"), 1, "zoom")
        if not ZOOM_MIN_PCT <= pct <= ZOOM_MAX_PCT:
            raise CommandError("zoom must be %d-%d percent" % (ZOOM_MIN_PCT, ZOOM_MAX_PCT))
        canvas.set_zoom(pct / 100.0)
    return "Zoom %d%%" % round(canvas.zoom * 100)


def _cmd_history(app, args, cmd):
    """undo / redo: UNDO or REDO {} through send_op; the server does the rest."""
    _expect(args, 0, cmd)
    err = _send_history_op(app, protocol.UNDO if cmd == "undo" else protocol.REDO)
    if err:
        raise CommandError(err)
    return "%s sent" % cmd.capitalize()


def _cmd_clear(app, args, cmd):
    """clear: OP_CLEAR {} through send_op, after a confirmation (it is not undoable)."""
    _expect(args, 0, cmd)
    role = getattr(app.me, "role", None)
    if role is not None and role != "host":
        raise CommandError("Only the host can clear the board")
    answer = QMessageBox.question(
        getattr(app, "window", None), "Clear board",
        "Clear the whole board for everyone?\n"
        "Clear cannot be undone (the server saves a version first).",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if answer != QMessageBox.Yes:
        return "Clear cancelled"
    ops.send_op(app, protocol.OP_CLEAR, {})
    return "Board cleared"


# command -> fn(app, args, cmd) returning a message, or raising CommandError.
_RUNNERS = {
    "line": lambda app, args, cmd: _cmd_two_points(app, args, cmd, shape_tools.LineTool),
    "arrow": lambda app, args, cmd: _cmd_two_points(app, args, cmd, shape_tools.ArrowTool),
    "rect": lambda app, args, cmd: _cmd_box(app, args, cmd, shape_tools.RectTool),
    "ellipse": lambda app, args, cmd: _cmd_box(app, args, cmd, shape_tools.EllipseTool),
    "circle": _cmd_circle,
    "color": _cmd_color,
    "width": _cmd_width,
    "grid": _cmd_grid,
    "snap": _cmd_snap,
    "zoom": _cmd_zoom,
    "undo": _cmd_history,
    "redo": _cmd_history,
    "clear": _cmd_clear,
}


def run_command(app, text):
    """Run one command line and return a short result message; CommandError on a mistake.

    The first word is the command (case-insensitive), the rest its
    arguments. An unknown command suggests the closest known one.
    """
    text = text.strip()
    if not text:
        raise CommandError("Type a command, e.g. rect 100,100 200,150")
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower()
    runner = _RUNNERS.get(cmd)
    if runner is None:
        close = difflib.get_close_matches(cmd, COMMANDS.keys(), n=1)
        hint = (" Did you mean '%s'?" % close[0]) if close else ""
        raise CommandError("Unknown command '%s'.%s" % (cmd, hint))
    if getattr(app, "canvas", None) is None:
        raise CommandError("No canvas")
    return runner(app, _split_args(rest), cmd)


def matching_commands(text):
    """Commands whose name starts with the first word of text (all of them for empty text)."""
    word = text.strip().split(" ")[0].lower() if text.strip() else ""
    if " " in text.lstrip():                     # name finished: only that command
        return [word] if word in COMMANDS else []
    return [c for c in COMMANDS if c.startswith(word)]


# ==============================================================================
# Command bar widget
# ==============================================================================

class CommandLine(QLineEdit):
    """The bar's line edit: Enter runs, Escape closes, Tab completes, Up/Down walk the history."""

    def __init__(self, bar):
        """Create the line edit for CommandBar bar."""
        super().__init__(bar)
        self._bar = bar

    def event(self, ev):
        """Keep Escape/Up/Down for ourselves and catch Tab before Qt moves the focus.

        ShortcutOverride is Qt asking the focused widget "do you want this
        key, or may the window shortcut have it?". Accepting it for Escape
        means the window-wide Escape (cancel drawing) does not fire while
        the user is typing here; the key comes to keyPressEvent instead.
        """
        if ev.type() == QEvent.ShortcutOverride and ev.key() in (Qt.Key_Escape, Qt.Key_Up,
                                                                  Qt.Key_Down):
            ev.accept()
            return True
        if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Tab:
            self._bar.complete()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        """Escape closes the bar, Up/Down step through the history; other keys edit the text."""
        key = ev.key()
        if key == Qt.Key_Escape:
            self._bar.close_bar()
        elif key == Qt.Key_Up:
            self._bar.history_step(-1)
        elif key == Qt.Key_Down:
            self._bar.history_step(+1)
        else:
            super().keyPressEvent(ev)


class CommandBar(QFrame):
    """AutoCAD-style command line laid over the bottom of the canvas.

    A child widget of the canvas (one per canvas, found again with
    findChild), so it needs no change to main_window. It follows the
    canvas size through an event filter. From top to bottom: the hint list
    (matching commands and their syntax), the result/error line, and the
    prompt with the line edit. Hidden until '/'.
    """

    def __init__(self, canvas):
        """Build the hidden bar over canvas."""
        super().__init__(canvas)
        self.canvas = canvas
        self.app = canvas.app
        self._history = []           # commands run, oldest first
        self._hist_pos = 0           # index into _history while walking; len() = the draft
        self._draft = ""             # what was typed before Up was pressed
        self.setObjectName("command_bar")
        self.setFrameShape(QFrame.StyledPanel)
        self.setAutoFillBackground(True)
        self.setStyleSheet("#command_bar { background: #fafafa; border: 1px solid #9e9e9e;"
                           " border-radius: 4px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        self._hints = QLabel()
        self._hints.setTextFormat(Qt.RichText)
        self._hints.setStyleSheet(HINT_STYLE)
        layout.addWidget(self._hints)
        self._msg = QLabel()
        self._msg.setWordWrap(True)
        layout.addWidget(self._msg)
        row = QHBoxLayout()
        prompt = QLabel("Command:")
        prompt.setStyleSheet("QLabel { font-weight: bold; }")
        row.addWidget(prompt)
        self._edit = CommandLine(self)
        self._edit.setPlaceholderText("e.g. rect 100,100 200,150   (Tab completes, Up/Down history, Esc closes)")
        row.addWidget(self._edit, 1)
        layout.addLayout(row)

        self._edit.returnPressed.connect(self.execute)
        self._edit.textChanged.connect(self._update_hints)
        canvas.installEventFilter(self)
        self.hide()

    # ----------------------------------------------------------- lifecycle
    @classmethod
    def find(cls, canvas):
        """Return the canvas's command bar, or None if it was never opened."""
        return canvas.findChild(cls)

    @classmethod
    def for_canvas(cls, canvas):
        """Return the canvas's command bar, creating it on first use."""
        bar = cls.find(canvas)
        return bar if bar is not None else cls(canvas)

    def open_bar(self):
        """Show the bar on top of the canvas and focus the line edit."""
        self._msg.clear()
        self._msg.hide()
        self._update_hints(self._edit.text())
        self.show()
        self._reposition()
        self.raise_()                  # above the cursor and eraser overlays
        self._edit.setFocus(Qt.ShortcutFocusReason)
        self._edit.selectAll()

    def close_bar(self):
        """Hide the bar and give the keyboard back to the canvas (tool keys work again)."""
        self.hide()
        self.canvas.setFocus(Qt.OtherFocusReason)

    def has_focus(self):
        """True while the line edit has the keyboard focus."""
        return self._edit.hasFocus()

    def eventFilter(self, obj, ev):
        """Follow the canvas size: stay glued to its bottom edge."""
        if obj is self.canvas and ev.type() == QEvent.Resize and self.isVisible():
            self._reposition()
        return False

    def _reposition(self):
        """Full canvas width (minus margins), as tall as the contents, at the bottom."""
        width = max(100, self.canvas.width() - 2 * BAR_MARGIN)
        # The result line wraps, so its height depends on the width.
        height = self.heightForWidth(width) if self.hasHeightForWidth() else -1
        if height <= 0:
            height = self.sizeHint().height()
        self.setGeometry(BAR_MARGIN, self.canvas.height() - height - BAR_MARGIN, width, height)

    # ------------------------------------------------------------- running
    def execute(self):
        """Enter: run the line, remember it, print the result or the error in the bar.

        Every exception is caught: a typo must never close the whiteboard
        (PyQt aborts on an exception escaping a slot). A command that
        failed stays in the line edit so it can be corrected.
        """
        text = self._edit.text().strip()
        if not text:
            return
        self._remember(text)
        try:
            message = run_command(self.app, text)
        except CommandError as exc:
            self._show(str(exc), error=True)
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._show("Error: %s" % exc, error=True)
            return
        self._show(message, error=False)
        self._edit.clear()

    def _show(self, text, error):
        """Print a result (green) or an error (red) above the line edit."""
        self._msg.setStyleSheet(ERROR_STYLE if error else INFO_STYLE)
        self._msg.setText(text)
        self._msg.show()
        self._reposition()

    # ------------------------------------------------------- hints and Tab
    def _update_hints(self, text):
        """Show the commands matching what is typed, with their syntax (the hint list)."""
        names = matching_commands(text)
        if not text.strip():
            body = "Commands: " + " &nbsp;".join("<b>%s</b>" % c for c in COMMANDS)
        elif not names:
            body = "<i>No command starts with '%s'</i>" % html.escape(text.strip().split(" ")[0])
        else:
            rows = ["<b>%s</b> &nbsp;&mdash; %s" % (html.escape(COMMANDS[c][0]),
                                                    html.escape(COMMANDS[c][1]))
                    for c in names[:HINT_ROWS]]
            if len(names) > HINT_ROWS:
                rows.append("... %d more" % (len(names) - HINT_ROWS))
            body = "<br>".join(rows)
        self._hints.setText(body)
        if self.isVisible():
            self._reposition()

    def complete(self):
        """Tab: complete the command name (one match: whole name + space; several: common prefix)."""
        text = self._edit.text()
        if " " in text.lstrip():
            return
        names = matching_commands(text)
        if len(names) == 1:
            self._edit.setText(names[0] + " ")
        elif names:
            prefix = names[0]
            for n in names[1:]:
                while not n.startswith(prefix):
                    prefix = prefix[:-1]
            if len(prefix) > len(text.strip()):
                self._edit.setText(prefix)

    # ------------------------------------------------------------- history
    def _remember(self, text):
        """Add text to the history (no immediate duplicates) and stop walking it."""
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            del self._history[:-HISTORY_MAX]
        self._hist_pos = len(self._history)
        self._draft = ""

    def history_step(self, step):
        """Up (-1) / Down (+1): show the previous/next command; past the newest, the draft."""
        if not self._history:
            return
        if self._hist_pos == len(self._history):
            self._draft = self._edit.text()          # leaving the draft: keep it
        pos = max(0, min(len(self._history), self._hist_pos + step))
        if pos == self._hist_pos:
            return
        self._hist_pos = pos
        self._edit.setText(self._history[pos] if pos < len(self._history) else self._draft)


# ==============================================================================
# Help > Keyboard shortcuts
# ==============================================================================

# Fixed shortcuts that are not a register_shortcut entry of their own, for the help.
_BUILTIN_HELP = (
    ("View", (
        ("G", "grid and snap-to-grid on/off"),
        ("+  or  =", "zoom in (around the mouse)"),
        ("-", "zoom out"),
        ("0", "zoom 100 %"),
        ("Ctrl + mouse wheel", "zoom around the mouse"),
        ("Mouse wheel / Shift + wheel", "scroll up-down / left-right"),
        ("Space + drag, middle drag", "pan"),
    )),
    ("Editing", (
        ("Ctrl+Z", "undo my last change (done by the server)"),
        ("Ctrl+Y", "redo"),
        ("Escape", "cancel the current drawing / drag; clear the selection"),
        ("Delete / Backspace", "delete the selected objects (select tool)"),
        ("Ctrl+A", "select every object (select tool)"),
        ("Shift+click", "add / remove one object in the selection"),
        ("Drag on empty board", "select everything inside the rectangle (Shift: add to the selection)"),
        ("Arrow keys", "nudge the selection one grid step (Shift: 5 steps), sent on key release"),
        ("Shift while dragging", "line/arrow at 0/45/90 degrees; square/circle"),
        ("Double-click", "edit a text or sticky note (select tool)"),
    )),
    ("Command bar", (
        ("/", "open the command bar"),
        ("Enter", "run the command"),
        ("Tab", "complete the command name"),
        ("Up / Down", "previous / next command"),
        ("Escape", "close the command bar"),
    )),
)


def _first_line(fn):
    """First line of fn's docstring (for listing other modules' shortcuts), or its name."""
    doc = (getattr(fn, "__doc__", None) or "").strip()
    return doc.splitlines()[0] if doc else getattr(fn, "__name__", "?")


def _help_html():
    """Build the help text from the fixed lists AND the registries, so new shortcuts appear."""
    def table(title, rows):
        """One titled two-column table."""
        cells = "".join("<tr><td style='padding:2px 14px 2px 0'><b>%s</b></td><td>%s</td></tr>"
                        % (html.escape(k), html.escape(v)) for k, v in rows)
        return "<h3 style='margin:10px 0 4px 0'>%s</h3><table>%s</table>" % (html.escape(title), cells)

    parts = []
    tools = []
    listed = set()
    for key, tool_id, name in TOOL_KEYS:
        cls = registry.TOOLS.get(tool_id)
        listed.add(tool_id)
        tools.append((key, (getattr(cls, "label", None) or name) if cls else
                      "%s (not available yet)" % name))
    for tool_id, cls in registry.TOOLS.items():      # tools added by others with their own key
        if tool_id not in listed and getattr(cls, "shortcut", None):
            tools.append((cls.shortcut, getattr(cls, "label", tool_id)))
    parts.append(table("Tools", tools))
    for title, rows in _BUILTIN_HELP:
        parts.append(table(title, rows))

    # Everything other modules registered (e.g. P3's Ctrl+S / Ctrl+E).
    others = [(e["keyseq"], _first_line(e["fn"])) for e in registry.SHORTCUTS
              if e["fn"] not in _OWN_FNS and e["keyseq"] not in ("Delete", "Backspace", "Ctrl+A")]
    others += [(e["shortcut"], "%s > %s" % (e["menu"], e["label"])) for e in registry.MENU_ACTIONS
               if e["shortcut"] and e["fn"] not in _OWN_FNS]
    if others:
        parts.append(table("Other shortcuts", others))

    parts.append(table("Commands (type / first; coordinates in board units, snapped when "
                       "snap is on)", [(syntax, desc) for syntax, desc in COMMANDS.values()]))
    return "".join(parts)


class ShortcutHelpDialog(QDialog):
    """Non-modal window listing every shortcut and command (rebuilt each time it opens)."""

    def __init__(self, parent=None):
        """Create the dialog with a read-only text browser and a Close button."""
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(560, 640)
        layout = QVBoxLayout(self)
        self._view = QTextBrowser()
        self._view.setOpenLinks(False)
        layout.addWidget(self._view)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def refresh(self):
        """Fill in the current registries."""
        self._view.setHtml(_help_html())


_help_dialog = None


def show_shortcuts_help(app):
    """Help > Keyboard shortcuts (F1): show the list; non-modal, so the board stays usable."""
    global _help_dialog
    if _help_dialog is None:
        _help_dialog = ShortcutHelpDialog(getattr(app, "window", None))
    _help_dialog.refresh()
    _help_dialog.show()
    _help_dialog.raise_()
    _help_dialog.activateWindow()


# ==============================================================================
# Registration (import time, on the main thread; main_window reads the tables)
# ==============================================================================

def _keys_claimed_by_tools():
    """Upper-case keys already bound by main_window's toolbar from tools' `shortcut` attributes."""
    return {str(cls.shortcut).strip().upper() for cls in registry.TOOLS.values()
            if getattr(cls, "shortcut", None)}


def _register():
    """Register every shortcut and menu action of this module (called once at import)."""
    claimed = _keys_claimed_by_tools()
    own = []
    for key, tool_id, _name in TOOL_KEYS:
        if key not in claimed:                      # else the toolbar already binds it
            own.append(registry.register_shortcut(key, _tool_selector(tool_id)))
    own.append(registry.register_shortcut("+", zoom_in))
    own.append(registry.register_shortcut("=", zoom_in))   # + without Shift on most keyboards
    own.append(registry.register_shortcut("-", zoom_out))
    own.append(registry.register_shortcut("0", zoom_reset))
    own.append(registry.register_shortcut("Escape", escape))
    own.append(registry.register_shortcut("Ctrl+Z", undo))
    own.append(registry.register_shortcut("Ctrl+Y", redo))
    own.append(registry.register_shortcut("/", open_command_bar))
    own.append(registry.register_menu_action("View", "Grid and snap on/off", toggle_grid_snap,
                                             shortcut="G"))
    own.append(registry.register_menu_action("Help", "Keyboard shortcuts", show_shortcuts_help,
                                             shortcut="F1"))
    return set(own)


_OWN_FNS = _register()
