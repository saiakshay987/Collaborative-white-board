"""text_tool.py: Text and sticky-note tools, and the text editor registered with register_editor.

Contract sections D, F and G. Everything here goes through the registries:

    TextTool    id 'text'    key T   drag a box, type, Enter sends
    StickyTool  id 'sticky'  key N   click: a 200 x 160 note in the next palette color
    register_editor('text') and register_editor('sticky'): Akshay's select tool
    calls the editor on a double-click, as fn(app, canvas, obj).

How typing works (InlineEditor)
-------------------------------
A QTextEdit is laid over the canvas exactly where the object is, in the
object's font (family, bold, italic, underline, alignment) with font size =
size x canvas zoom, so what you type looks like what everybody will see. It is
moved, resized and re-fonted again whenever the canvas is zoomed or panned. It is a child widget of the canvas, never a dialog.

    Enter           finish and send          Shift+Enter  new line
    click away      finish and send          (focus lost to anything else)
    Escape          cancel, nothing is sent

"Click away" is a focus loss. Switching to another window or a popup menu also
takes the focus, but that must not end the edit, so those two focus reasons are
ignored. The text is capped at kinds_text.TEXT_MAX_CHARS (the number the server
enforces) while typing, and the overlay follows the canvas when it is zoomed or
panned (a light timer on the GUI thread; nothing here ever blocks).

What goes over the network
--------------------------
Only one message per finished edit, through client/ops.send_op (applied locally
at once, confirmed by the server echo):

    new text or sticky   OP_ADD {obj}                        (empty text: nothing is sent)
    double-click edit    OP_UPDATE {obj_id, set:{text}}      (only if the text changed)
    format bar change    OP_UPDATE {obj_id, set:{font, size, ...}}  (sent by client/toolbar.py)

The object is checked with kinds_text.validate (the same rules as the server)
before sending. Moving and resizing are Akshay's select tool, not repeated here.
Viewers cannot create or edit (toast); the server still checks
permissions.can(role, 'draw') on every OP_ADD and OP_UPDATE.
"""

import traceback

from PyQt5.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPen, QTextCursor, QTextOption
from PyQt5.QtWidgets import QFrame, QTextEdit

from client import ops, registry
from client.render_text import (STICKY_PAD, align_flag, box_of, color_of, family_of, flag_of,
                                make_font, size_of, text_color_of, wrap_option)
from client.tools.pen_tool import _can_draw
from common import config, kinds_text, protocol

# Text tool: a click without a real drag makes a box of this size (board units).
DEFAULT_TEXT_W = 240.0
DEFAULT_TEXT_H = 80.0
# A dragged box is never smaller than this, so there is room to type.
MIN_BOX_W = 60.0
MIN_BOX_H = 30.0
# A drag shorter than this in both directions counts as a click.
MIN_DRAG = 2.0

# Sticky tool: size of a new note and the 8 pastel background colors it cycles through
# (the format bar in client/toolbar.py offers the same 8).
STICKY_W = 200.0
STICKY_H = 160.0
STICKY_PALETTE = ("#fff176", "#ffab91", "#a5d6a7", "#90caf9", "#f48fb1", "#ce93d8",
                  "#ffe082", "#e0e0e0")

# The format fields every text / sticky carries (contract D, common/kinds_text.py).
FORMAT_FIELDS = ("font", "size", "bold", "italic", "underline", "align")

# The overlay keeps at least this size on screen (pixels), even for a tiny box or low zoom.
OVERLAY_MIN_W_PX = 80
OVERLAY_MIN_H_PX = 32
# How often the overlay checks whether the canvas was zoomed or panned (milliseconds).
FOLLOW_MS = 60

# Focus losses that do NOT end the edit: another window was activated, or a popup
# (the editor's own context menu) opened.
_KEEP_EDITING = (Qt.ActiveWindowFocusReason, Qt.PopupFocusReason)

# Index into STICKY_PALETTE of the next new sticky. Module level (not per tool
# instance), because the select tool creates a fresh tool object on every switch.
_next_sticky = 0


# What the NEXT new text / sticky gets: the format bar writes here when nothing is
# selected. Only the fields the user changed are stored; the rest come from the
# kinds_text defaults. Module level (not per tool instance) for the same reason as
# _next_sticky: the select tool makes a fresh tool object on every switch.
_new_overrides = {"text": {}, "sticky": {}}


def new_object_style(kind):
    """Return the complete format dict a new object of `kind` ('text' or 'sticky') gets.

    Keys: font, size, bold, italic, underline, align (and text_color for a
    sticky): the kinds_text defaults overridden by the user's format bar choices.
    """
    defaults = kinds_text.TEXT_DEFAULTS if kind == "text" else kinds_text.STICKY_DEFAULTS
    keys = FORMAT_FIELDS + (("text_color",) if kind == "sticky" else ())
    style = {key: defaults[key] for key in keys}
    style.update({k: v for k, v in _new_overrides.get(kind, {}).items() if k in style})
    return style


def set_new_default(kind, **fields):
    """Remember format fields (and, for a sticky, its background `color`) for the next new `kind`."""
    _new_overrides.setdefault(kind, {}).update(fields)


def new_override(kind, field):
    """Return the value the user chose for `field` of the next new `kind`, or None if none was chosen."""
    return _new_overrides.get(kind, {}).get(field)


def _r1(value):
    """Round to one decimal (contract D), as an int when it is whole (smaller JSON)."""
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


# --- The inline editor ---------------------------------------------------------------

class InlineEditor(QTextEdit):
    """Plain-text editor laid over the canvas on top of one board box.

    Emits `committed(text)` when the user finishes (Enter or click-away) and
    `cancelled()` on Escape. Exactly one of them is emitted, once; then the
    widget removes itself. It never touches the board or the network: whoever
    opened it decides what to send.
    """

    committed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, canvas, box, text, size, text_color, background, padding=0.0, style=None):
        """Create the editor over `box` (QRectF, board units) and give it the focus.

        text: the initial content; size: font size in board units; text_color
        and background: QColor for the text and the editor's fill; padding: the
        space between the box edge and the text in board units, the same value
        the renderer uses, so the text does not move when the edit ends.
        style: optional {font, bold, italic, underline, align} so the overlay
        looks like the formatted object.
        """
        super().__init__(canvas)
        self.canvas = canvas
        self.box = QRectF(box)
        self.size_units = float(size)
        self.padding = float(padding)
        style = style or {}
        self.family = style.get("font")
        self.bold = style.get("bold", False)
        self.italic = style.get("italic", False)
        self.underline = style.get("underline", False)
        self._done = False
        self._view = None                 # (zoom, offset x, offset y) the geometry was made for
        self.setAcceptRichText(False)     # plain text only: no pasted formatting
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setStyleSheet(
            "QTextEdit { background: %s; color: %s; border: 1px dashed #1565c0; padding: 0px; }"
            % (background.name(), text_color.name()))
        self.document().setDefaultTextOption(wrap_option(align_flag(style.get("align"))))
        self.setPlainText(text)
        self.textChanged.connect(self._limit_length)
        self._sync_geometry()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync_geometry)
        self._timer.start(FOLLOW_MS)
        self.show()
        self.setFocus(Qt.OtherFocusReason)
        self.moveCursor(QTextCursor.End)

    # -- placement -----------------------------------------------------------------
    def _sync_geometry(self):
        """Place the editor over its board box for the canvas' current zoom and pan.

        Does nothing when neither changed since the last call, so the timer is cheap.
        """
        view = (self.canvas.zoom, self.canvas.offset.x(), self.canvas.offset.y())
        if view == self._view:
            return
        self._view = view
        zoom = self.canvas.zoom
        top_left = self.canvas.board_to_screen(self.box.topLeft())
        width = max(OVERLAY_MIN_W_PX, int(self.box.width() * zoom))
        height = max(OVERLAY_MIN_H_PX, int(self.box.height() * zoom))
        self.setGeometry(int(top_left.x()), int(top_left.y()), width, height)
        # Font size on screen = size x zoom. This runs again whenever the canvas is
        # zoomed or panned (the _view check above), so the overlay keeps matching
        # the board while the user edits.
        self.setFont(make_font(self.size_units * zoom, self.family, self.bold,
                               self.italic, self.underline))
        self.document().setDocumentMargin(self.padding * zoom)

    def _limit_length(self):
        """Cut the text to kinds_text.TEXT_MAX_CHARS (what the server accepts), keeping the cursor."""
        text = self.toPlainText()
        limit = kinds_text.TEXT_MAX_CHARS
        if len(text) <= limit:
            return
        position = min(self.textCursor().position(), limit)
        self.blockSignals(True)
        self.setPlainText(text[:limit])
        self.blockSignals(False)
        cursor = self.textCursor()
        cursor.setPosition(position)
        self.setTextCursor(cursor)

    # -- finishing -----------------------------------------------------------------
    def _finish(self, commit):
        """End the edit once: emit committed(text) or cancelled(), then remove the widget.

        _done is set first, so the focus-out caused by hiding the widget cannot
        finish it a second time. A failing receiver is printed, never raised:
        PyQt5 aborts the whole program on an exception inside an event handler.
        """
        if self._done:
            return
        self._done = True
        self._timer.stop()
        text = self.toPlainText()
        self.hide()
        self.deleteLater()
        self.canvas.setFocus(Qt.OtherFocusReason)
        try:
            if commit:
                self.committed.emit(text)
            else:
                self.cancelled.emit()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self.canvas.update()

    def commit(self):
        """Finish the edit and keep the text."""
        self._finish(True)

    def cancel(self):
        """Finish the edit and throw the text away."""
        self._finish(False)

    # -- events --------------------------------------------------------------------
    def event(self, ev):
        """Take Escape for ourselves: a window shortcut must not get it before keyPressEvent."""
        if ev.type() == QEvent.ShortcutOverride and ev.key() == Qt.Key_Escape:
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        """Enter sends, Shift+Enter is a new line, Escape cancels; other keys edit the text."""
        key = ev.key()
        if key == Qt.Key_Escape:
            self.cancel()
        elif key in (Qt.Key_Return, Qt.Key_Enter) and not ev.modifiers() & Qt.ShiftModifier:
            self.commit()
        else:
            super().keyPressEvent(ev)

    def focusOutEvent(self, ev):
        """Click-away: the focus went somewhere else, so finish and send.

        Activating another window or opening a popup also takes the focus
        (see _KEEP_EDITING); the edit goes on in those cases.
        """
        super().focusOutEvent(ev)
        if not self._done and ev.reason() not in _KEEP_EDITING:
            self.commit()


# The editor that is open now (None if none). Only one at a time: opening another
# one finishes this one first.
_open_editor = None


def close_open_editor(commit=True):
    """Finish the open editor, if any: send its text (commit=True) or drop it."""
    global _open_editor
    editor, _open_editor = _open_editor, None
    if editor is None or editor._done:
        return
    if commit:
        editor.commit()
    else:
        editor.cancel()


def open_editor(canvas, box, text, size, text_color, background, on_commit, padding=0.0, style=None):
    """Open an InlineEditor over box; on_commit(text) is called when it is finished with Enter or click-away.

    padding is the text inset in board units (STICKY_PAD for a sticky, 0 for
    a text); style is the optional format dict of InlineEditor. Any editor
    still open is finished first. Returns the new editor.
    """
    global _open_editor
    close_open_editor(commit=True)
    editor = InlineEditor(canvas, box, text, size, text_color, background, padding, style)
    editor.committed.connect(on_commit)
    _open_editor = editor
    return editor


# --- Sending -----------------------------------------------------------------------------

def add_object(app, kind, box, text, color, size, style=None):
    """Send OP_ADD for a new text or sticky object; nothing is sent for an empty text.

    style: the other format fields (see new_object_style), merged into the object.

    The object gets an id like an op_id (contract D), is checked with
    kinds_text.validate (the rules the server runs) and goes out as ONE OP_ADD.
    Returns True if it was sent.
    """
    text = text.strip()
    if not text:
        return False
    if not _can_draw(app):            # I became a viewer while typing
        return False
    x = max(0.0, min(box.x(), config.BOARD_W - box.width()))
    y = max(0.0, min(box.y(), config.BOARD_H - box.height()))
    obj = {"id": protocol.new_op_id(app.me.username or "me"), "kind": kind,
           "x": _r1(x), "y": _r1(y), "w": _r1(box.width()), "h": _r1(box.height()),
           "text": text, "color": color}
    obj.update(style or {})
    obj["size"] = size
    ok, reason = kinds_text.validate(obj)
    if not ok:
        app.toast("%s not sent: %s" % (kind.capitalize(), reason))
        return False
    ops.send_op(app, protocol.OP_ADD, {"obj": obj})
    return True


def update_text(app, obj_id, old_text, new_text):
    """Send OP_UPDATE {obj_id, set:{text}} if the text changed and the object still exists.

    The merged object is checked with kinds_text.validate first, like the
    server does for an OP_UPDATE. Returns True if it was sent.
    """
    if new_text == old_text:
        return False
    board = getattr(app, "board", None)
    current = board.objects.get(obj_id) if board is not None else None
    if current is None:
        app.toast("That object was deleted while you were editing it.")
        return False
    if not _can_draw(app):
        return False
    ok, reason = kinds_text.validate(dict(current, text=new_text))
    if not ok:
        app.toast("Text not changed: %s" % reason)
        return False
    ops.send_op(app, protocol.OP_UPDATE, {"obj_id": obj_id, "set": {"text": new_text}})
    return True


# --- The editor for Akshay's select tool (double-click) ---------------------------------------

def edit_object(app, canvas, obj):
    """Registered with register_editor for 'text' and 'sticky': edit obj's text in place.

    Called by the select tool on a double-click with a COPY of the object. The
    overlay starts with the current text selected at its end; on Enter or
    click-away an OP_UPDATE {obj_id, set:{text}} is sent if the text changed.
    Viewers cannot edit.
    """
    if not _can_draw(app):
        return
    obj_id, old_text = obj.get("id"), str(kinds_text.get(obj, "text") or "")
    sticky = obj.get("kind") == "sticky"
    text_color = text_color_of(obj)
    background = color_of(obj, kinds_text.STICKY_DEFAULTS["color"]) if sticky else QColor("#ffffff")
    open_editor(canvas, box_of(obj), old_text, size_of(obj), text_color, background,
                lambda text: update_text(app, obj_id, old_text, text.strip()),
                padding=STICKY_PAD if sticky else 0.0,
                style={"font": family_of(obj), "bold": flag_of(obj, "bold"),
                       "italic": flag_of(obj, "italic"), "underline": flag_of(obj, "underline"),
                       "align": kinds_text.get(obj, "align")})


# --- The tools ---------------------------------------------------------------------------------

class TextTool:
    """Text tool: drag a box on the board, type in the overlay, Enter sends OP_ADD."""

    id = "text"
    label = "Text"
    shortcut = "T"
    group = "text"

    # Own remembered color in the tool dock (black at first), so text is not
    # drawn in the highlighter's yellow just because that tool was used before.
    pen_defaults = {"color": "#000000", "width": 3}
    uses_color = True

    def __init__(self):
        """Start idle: no drag in progress."""
        self._start = None      # board point where the drag began, None when idle
        self._end = None        # current drag point

    def on_press(self, canvas, p, ev):
        """Left press: start dragging a box (nothing is sent). Viewers get a toast instead."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._start = QPointF(p)
        self._end = QPointF(p)

    def on_move(self, canvas, p, ev):
        """While dragging, remember the corner; the canvas repaints the dashed preview."""
        if self._start is not None and ev.buttons() & Qt.LeftButton:
            self._end = QPointF(p)

    def on_release(self, canvas, p, ev):
        """Finish the drag and open the editor over the box (a click gives a default-size box)."""
        if self._start is None:
            return
        start, end = self._start, QPointF(p)
        self._start = self._end = None
        app = canvas.app
        width, height = abs(end.x() - start.x()), abs(end.y() - start.y())
        if width < MIN_DRAG and height < MIN_DRAG:
            box = QRectF(start.x(), start.y(), DEFAULT_TEXT_W, DEFAULT_TEXT_H)
        else:
            box = QRectF(min(start.x(), end.x()), min(start.y(), end.y()),
                         max(width, MIN_BOX_W), max(height, MIN_BOX_H))
        color = app.pen.get("color") or "#000000"
        if not kinds_text.is_color(color):
            color = "#000000"
        style = new_object_style("text")         # the format bar's choices for the next text
        size = style.pop("size")
        open_editor(canvas, box, "", size, QColor(color), QColor("#ffffff"),
                    lambda text: add_object(app, "text", box, text, color, size, style),
                    style=style)

    def cancel(self, canvas):
        """Escape: drop the box being dragged, or the open editor. Nothing was sent."""
        self._start = self._end = None
        close_open_editor(commit=False)

    def paint_overlay(self, painter, canvas):
        """Dashed preview of the box being dragged (local only, never sent)."""
        if self._start is None or self._end is None:
            return
        pen = QPen(QColor("#1565c0"), 1, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(self._start, self._end).normalized())

    def cursor(self):
        """Text cursor: this tool puts text on the board."""
        return Qt.IBeamCursor


class StickyTool:
    """Sticky tool: a click puts a 200 x 160 note in the next palette color and opens the editor."""

    id = "sticky"
    label = "Sticky note"
    shortcut = "N"
    group = "text"

    def __init__(self):
        """Start idle: no click in progress."""
        self._start = None      # board point of the press, None when idle

    def on_press(self, canvas, p, ev):
        """Left press: remember where the note goes. Viewers get a toast instead."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._start = QPointF(p)

    def on_release(self, canvas, p, ev):
        """Open the editor over a new note centered on the click.

        The palette color is used up only when the note is really sent, so
        cancelling with Escape or leaving it empty does not skip a color.
        """
        global _next_sticky
        if self._start is None:
            return
        center, self._start = self._start, None
        app = canvas.app
        box = QRectF(center.x() - STICKY_W / 2, center.y() - STICKY_H / 2, STICKY_W, STICKY_H)
        box.moveTo(max(0.0, min(box.x(), config.BOARD_W - STICKY_W)),
                   max(0.0, min(box.y(), config.BOARD_H - STICKY_H)))
        # A background chosen in the format bar wins; otherwise cycle the palette.
        chosen = new_override("sticky", "color")
        color = chosen or STICKY_PALETTE[_next_sticky % len(STICKY_PALETTE)]
        style = new_object_style("sticky")
        size = style.pop("size")

        def commit(text):
            """Send the note; move to the next palette color only if it was sent (and cycling)."""
            global _next_sticky
            if add_object(app, "sticky", box, text, color, size, style) and not chosen:
                _next_sticky += 1

        open_editor(canvas, box, "", size, QColor(style["text_color"]), QColor(color), commit,
                    padding=STICKY_PAD, style=style)

    def cancel(self, canvas):
        """Escape: forget the click, or drop the open editor. Nothing was sent."""
        self._start = None
        close_open_editor(commit=False)

    def cursor(self):
        """Crosshair: the click point is the center of the note."""
        return Qt.CrossCursor


registry.register_tool(TextTool)
registry.register_tool(StickyTool)
registry.register_editor("text", edit_object)
registry.register_editor("sticky", edit_object)
