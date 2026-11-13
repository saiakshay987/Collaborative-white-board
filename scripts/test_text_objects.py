"""test_text_objects.py: tests for text and sticky objects: kinds_text, render_text and text_tool.

Run from the project root with either
    python scripts/test_text_objects.py
    python -m unittest scripts.test_text_objects
No network and no display are needed: Qt runs with the "offscreen" platform,
the network is a small fake that records what would be sent, and keyboard and
mouse input is simulated with QTest on the real canvas and select tool.
"""

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Allow `python scripts/test_text_objects.py` (the script folder is not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, QPoint, QPointF, QSettings, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QFontDatabase, QImage, QPainter  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import client.bootstrap  # noqa: E402,F401  (every registration)
from client import main_window as mw, registry, render, toolbar  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel  # noqa: E402
from client.tools import text_tool as tt  # noqa: E402
from common import kinds_text, models  # noqa: E402


# --- kinds_text and the limits of contract rev 3 --------------------------------------

def make(kind="text", **fields):
    """Return a complete valid text/sticky object, with `fields` overriding."""
    obj = {"id": "a-1", "kind": kind, "x": 10, "y": 10, "w": 200, "h": 80,
           "text": "hi", "color": "#000000", "size": 18}
    obj.update(fields)
    return obj


class KindsTest(unittest.TestCase):
    """Checks the KINDS entries, their limits and the helper checks."""

    def test_limits_have_the_contract_shape(self):
        """Each kind has "limits": {field: [min, max]}, and LIMITS is the same dict."""
        for kind in ("text", "sticky"):
            limits = models.KINDS[kind]["limits"]
            self.assertEqual(limits["text"], [0, 2000])
            self.assertEqual(limits["size"], [8, 96])
        self.assertEqual(models.KINDS["sticky"]["limits"]["text_color"], [7, 7])
        self.assertEqual(kinds_text.LIMITS["size"], [8, 96])
        self.assertIsNot(models.KINDS["text"]["limits"], models.KINDS["sticky"]["limits"])

    def test_valid_objects(self):
        """A complete text and sticky pass; defaults may be left out."""
        self.assertEqual(models.validate_obj(make("text")), (True, ""))
        self.assertEqual(models.validate_obj(make("sticky")), (True, ""))
        bare = {"id": "a", "kind": "sticky", "x": 0, "y": 0, "w": 200, "h": 160}
        self.assertEqual(models.validate_obj(bare), (True, ""))

    def test_size_limits(self):
        """size 8 and 96 pass; 7, 97, NaN, bool and strings fail."""
        for size, ok in ((8, True), (96, True), (18.5, True), (7, False), (97, False),
                         (float("nan"), False), (True, False), ("big", False)):
            with self.subTest(size=size):
                self.assertEqual(models.validate_obj(make(size=size))[0], ok)

    def test_format_fields(self):
        """Revision 2 fields: optional (old objects load), typed, limited and defaulted."""
        full = make(font="Arial", bold=True, italic=False, underline=True, align="center")
        self.assertEqual(kinds_text.validate(full), (True, ""))
        self.assertEqual(kinds_text.validate(make("sticky", text_color="#112233")), (True, ""))
        for bad in ({"bold": "yes"}, {"font": ""}, {"font": "x" * 65}, {"align": 3},
                    {"underline": 1}):
            with self.subTest(bad=bad):
                self.assertFalse(kinds_text.validate(make(**bad))[0])
        self.assertFalse(models.validate_obj(make("sticky", text_color="#12345678"))[0])
        # An object saved before the fields existed still loads and gets the defaults.
        self.assertEqual(kinds_text.get(make(), "font"), "Segoe UI")
        self.assertEqual(kinds_text.get(make(), "align"), "left")
        self.assertIs(kinds_text.get(make(), "bold"), False)
        self.assertEqual(kinds_text.get(make("sticky"), "text_color"), "#2b2b2b")

    def test_text_length_limit(self):
        """2000 characters pass, 2001 fail with a reason naming the field; non-strings fail."""
        self.assertTrue(models.validate_obj(make(text="x" * 2000))[0])
        ok, reason = models.validate_obj(make(text="x" * 2001))
        self.assertFalse(ok)
        self.assertIn("'text'", reason)
        self.assertFalse(models.validate_obj(make(text=5))[0])

    def test_check_formats(self):
        """check() rejects a bad color and a negative box; validate() runs both layers."""
        self.assertFalse(kinds_text.check(make(color="red"))[0])
        self.assertFalse(kinds_text.check(make(color="#12345"))[0])
        self.assertFalse(kinds_text.check(make(w=-1))[0])
        self.assertEqual(kinds_text.validate(make()), (True, ""))
        self.assertFalse(kinds_text.validate(make(size=99))[0])
        self.assertFalse(kinds_text.validate(make(align="justify"))[0])
        self.assertFalse(kinds_text.validate(make("sticky", text_color="red"))[0])
        self.assertTrue(kinds_text.check({"kind": "rect"})[0])   # other kinds are left alone

    def test_get_uses_defaults(self):
        """get() falls back to the kind's default for a missing field."""
        self.assertEqual(kinds_text.get({"kind": "sticky"}, "color"), "#fff176")
        self.assertEqual(kinds_text.get({"kind": "text"}, "size"), 18)
        self.assertEqual(kinds_text.get({"kind": "text", "size": 30}, "size"), 30)


# --- the renderers ------------------------------------------------------------------------

def render_image(objects, width=520, height=360):
    """Paint objects on a white QImage with render_board and return the image."""
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    render.render_board(painter, objects, {})
    painter.end()
    return image


def non_white(image, x0, y0, x1, y1):
    """Count the non-white pixels of image inside the rectangle [x0, x1) x [y0, y1)."""
    white = QColor("white").rgb()
    return sum(1 for y in range(y0, y1) for x in range(x0, x1) if image.pixel(x, y) != white)


class RenderTest(unittest.TestCase):
    """Checks that text and sticky are painted, wrapped, clipped and never crash."""

    @classmethod
    def setUpClass(cls):
        """Make sure a QApplication exists (fonts need one) and note whether any font exists."""
        cls.qapp = QApplication.instance() or QApplication([])
        cls.has_fonts = bool(QFontDatabase().families())

    def need_fonts(self):
        """Skip the calling test when this Qt platform has no fonts (text would paint nothing)."""
        if not self.has_fonts:
            self.skipTest("no fonts on this Qt platform")

    def test_renderers_are_registered(self):
        """Both kinds have a renderer in the registry."""
        self.assertIsNotNone(registry.get_renderer("text"))
        self.assertIsNotNone(registry.get_renderer("sticky"))

    def test_text_is_painted_inside_its_box_only(self):
        """Long text paints pixels in the box and none outside it (clipped to the box)."""
        self.need_fonts()
        obj = make(x=50, y=50, w=120, h=40, size=20, text="A long text that has to wrap and be cut. " * 6)
        image = render_image([obj])
        self.assertGreater(non_white(image, 50, 50, 170, 90), 50)
        self.assertEqual(non_white(image, 0, 0, 520, 49), 0)       # above
        self.assertEqual(non_white(image, 0, 91, 520, 360), 0)     # below
        self.assertEqual(non_white(image, 172, 0, 520, 360), 0)    # right (wrapped, not overflowing)

    def test_long_word_is_broken_inside_the_box(self):
        """A word longer than the box does not overflow to the right."""
        self.need_fonts()
        obj = make(x=50, y=50, w=80, h=100, size=16, text="Supercalifragilisticexpialidocious")
        image = render_image([obj])
        self.assertGreater(non_white(image, 50, 50, 130, 150), 50)
        self.assertEqual(non_white(image, 132, 0, 520, 360), 0)

    def test_sticky_has_background_and_shadow(self):
        """The note's center has its color; the area just below and right of it is shadowed."""
        obj = make("sticky", x=50, y=50, w=200, h=160, text="", color="#90caf9")
        image = render_image([obj])
        self.assertEqual(QColor(image.pixel(150, 130)).name(), "#90caf9")
        self.assertNotEqual(QColor(image.pixel(252, 130)).name(), "#ffffff")   # shadow, right
        self.assertNotEqual(QColor(image.pixel(150, 212)).name(), "#ffffff")   # shadow, below
        self.assertEqual(QColor(image.pixel(30, 30)).name(), "#ffffff")        # far away: white

    def test_sticky_text_is_dark(self):
        """Text on a sticky is dark even on a dark-ish background."""
        self.need_fonts()
        obj = make("sticky", x=50, y=50, w=200, h=160, text="MMMMMMMM", color="#fff176", size=40)
        image = render_image([obj])
        darkest = min(QColor(image.pixel(x, y)).lightness()
                      for y in range(60, 110) for x in range(60, 240))
        self.assertLess(darkest, 90)

    def test_odd_objects_do_not_raise(self):
        """Negative size, bad color, huge size, missing fields and empty text all paint without error."""
        odd = [{"id": "1", "kind": "text", "x": 5, "y": 5, "w": -80, "h": -30, "text": "neg", "color": "nope", "size": 9999},
               {"id": "2", "kind": "sticky", "x": "a", "y": None, "w": 100, "h": 100, "text": None, "color": 5, "size": "x"},
               {"id": "3", "kind": "text"}, {"id": "4", "kind": "sticky", "x": 1, "y": 1, "w": 0, "h": 0}]
        render_image(odd)


# --- the tools and the editor, driven with simulated input ----------------------------------

class FakeNet(QObject):
    """Stand-in for NetClient: records every message the client would send."""

    connected = pyqtSignal()
    disconnected = pyqtSignal(str)
    reconnecting = pyqtSignal()
    reconnected = pyqtSignal()

    def __init__(self):
        """Start connected with an empty send list."""
        super().__init__()
        self.sent = []
        self.is_connected = True
        self.host = "127.0.0.1"
        self.port = 5050

    def send(self, type, payload=None, op_id=None):
        """Record the message and pretend it was queued."""
        self.sent.append((type, payload))
        return True


class ToolTest(unittest.TestCase):
    """Drives the text tool, the sticky tool and the double-click editor on a real canvas."""

    @classmethod
    def setUpClass(cls):
        """Build one window with canvas, board model and fake network for all tests."""
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp())
        cls.qapp = QApplication.instance() or QApplication([])
        cls.toasts = []
        cls.app = AppState()
        cls.app.net = FakeNet()
        cls.app.board = BoardModel()
        cls.app.toast = cls.toasts.append
        cls.app.me.username, cls.app.me.room, cls.app.me.role = "alice", "ROOM23", "editor"
        cls.window = mw.MainWindow(cls.app)
        cls.app.window = cls.window
        cls.window.resize(1300, 900)
        cls.window.show()
        cls.window.activateWindow()
        QTest.qWait(50)
        cls.canvas = cls.app.canvas

    def setUp(self):
        """Reset the view, the role, the board and what was sent, and close any editor."""
        tt.close_open_editor(commit=False)
        self.app.set_role("editor")
        self.canvas.set_zoom(1.0)
        self.canvas.offset = QPointF(50, 50)
        self.app.board.objects.clear()
        self.app.net.sent.clear()
        self.toasts.clear()
        tt._next_sticky = 0

    def sent(self):
        """The messages sent so far as (type, payload) pairs."""
        return list(self.app.net.sent)

    def scr(self, x, y):
        """Screen point (in canvas pixels) of board point (x, y) at the current view."""
        return QPoint(int(x * self.canvas.zoom + self.canvas.offset.x()),
                      int(y * self.canvas.zoom + self.canvas.offset.y()))

    def editor(self):
        """The open InlineEditor, or None."""
        ed = tt._open_editor
        return ed if ed is not None and not ed._done else None

    def drag(self, a, b):
        """Press at board point a, move and release at board point b with the left button."""
        QTest.mousePress(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(*a))
        QTest.mouseMove(self.canvas, self.scr(*b))
        QTest.mouseRelease(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(*b))
        self.qapp.processEvents()

    def click(self, x, y):
        """Left click at board point (x, y)."""
        QTest.mouseClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(x, y))
        self.qapp.processEvents()

    def add_text_via_tool(self, text="Hello", at=((100, 100), (400, 220))):
        """Use the text tool to create a text object; return its id."""
        self.window.select_tool("text")
        self.drag(*at)
        QTest.keyClicks(self.editor(), text)
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        return [p["obj"]["id"] for t, p in self.sent() if t == "OP_ADD"][-1]

    # -- text tool ---------------------------------------------------------------------
    def test_drag_type_enter_sends_one_op_add(self):
        """Drag a box: the editor sits over it; Enter sends exactly one valid OP_ADD."""
        self.window.select_tool("text")
        self.drag((100, 100), (400, 220))
        ed = self.editor()
        geo = ed.geometry()
        self.assertEqual((geo.x(), geo.y(), geo.width(), geo.height()), (150, 150, 300, 120))
        self.assertTrue(ed.hasFocus())
        QTest.keyClicks(ed, "Hello")
        QTest.keyClick(ed, Qt.Key_Return, Qt.ShiftModifier)        # Shift+Enter: new line
        QTest.keyClicks(ed, "world")
        self.assertIs(self.editor(), ed)
        self.assertEqual(ed.toPlainText(), "Hello\nworld")
        QTest.keyClick(ed, Qt.Key_Return)
        self.qapp.processEvents()
        self.assertIsNone(self.editor())
        self.assertEqual(len(self.sent()), 1)
        kind, payload = self.sent()[0]
        obj = payload["obj"]
        self.assertEqual(kind, "OP_ADD")
        self.assertEqual((obj["kind"], obj["x"], obj["y"], obj["w"], obj["h"]), ("text", 100, 100, 300, 120))
        self.assertEqual((obj["text"], obj["color"], obj["size"]), ("Hello\nworld", "#000000", 18))
        self.assertEqual(kinds_text.validate(obj), (True, ""))
        self.assertIn(obj["id"], self.app.board.objects)           # applied locally at once

    def test_backwards_drag_is_normalized(self):
        """Dragging up and to the left gives the same box as dragging down and right."""
        self.window.select_tool("text")
        self.drag((700, 400), (500, 300))
        geo = self.editor().geometry()
        self.assertEqual((geo.x(), geo.y(), geo.width(), geo.height()), (550, 350, 200, 100))

    def test_escape_cancels(self):
        """Escape closes the editor and sends nothing."""
        self.window.select_tool("text")
        self.drag((100, 100), (300, 200))
        QTest.keyClicks(self.editor(), "never sent")
        QTest.keyClick(self.editor(), Qt.Key_Escape)
        self.qapp.processEvents()
        self.assertIsNone(self.editor())
        self.assertEqual(self.sent(), [])

    def test_click_gives_default_box_and_empty_text_sends_nothing(self):
        """A click makes a 240 x 80 box; Enter on an empty editor sends nothing."""
        self.window.select_tool("text")
        self.drag((900, 100), (900, 100))
        ed = self.editor()
        self.assertEqual((ed.width(), ed.height()), (240, 80))
        QTest.keyClick(ed, Qt.Key_Return)
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [])

    def test_click_away_sends(self):
        """Clicking elsewhere on the canvas finishes the edit and sends it."""
        self.window.select_tool("text")
        self.drag((100, 400), (300, 460))
        QTest.keyClicks(self.editor(), "click away")
        QTest.mouseClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(1500, 1500))
        self.qapp.processEvents()
        self.assertEqual([p["obj"]["text"] for t, p in self.sent()], ["click away"])
        QTest.mouseRelease(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(1500, 1500))
        tt.close_open_editor(commit=False)

    def test_text_is_capped_at_2000(self):
        """Pasting 2600 characters keeps 2000."""
        self.window.select_tool("text")
        self.drag((100, 600), (400, 700))
        ed = self.editor()
        ed.insertPlainText("x" * 2600)
        self.assertEqual(len(ed.toPlainText()), 2000)

    def test_editor_follows_zoom_and_pan(self):
        """After a zoom the overlay is twice as big; after a pan it moves with the board."""
        self.window.select_tool("text")
        self.drag((100, 600), (400, 700))
        ed = self.editor()
        before = ed.geometry()
        self.canvas.set_zoom(2.0)
        QTest.qWait(150)
        after = ed.geometry()
        self.assertEqual((after.width(), after.height()), (2 * before.width(), 2 * before.height()))
        self.canvas.pan_by(40, 30)
        QTest.qWait(150)
        self.assertEqual(ed.geometry().x(), after.x() + 40)

    # -- sticky tool -------------------------------------------------------------------
    def test_sticky_click_makes_a_centered_200_by_160_note(self):
        """A click opens the editor over a 200 x 160 note centered on the click."""
        self.window.select_tool("sticky")
        self.click(1000, 1000)
        geo = self.editor().geometry()
        self.assertEqual((geo.width(), geo.height()), (200, 160))
        self.assertEqual((geo.x(), geo.y()), (50 + 900, 50 + 920))

    def test_sticky_palette_cycles_only_when_sent(self):
        """The palette colors in order, then it starts over; a cancelled note uses none up."""
        self.window.select_tool("sticky")
        self.click(1000, 1000)
        QTest.keyClick(self.editor(), Qt.Key_Escape)               # cancelled
        colors = []
        count = len(tt.STICKY_PALETTE)
        for i in range(count + 1):
            self.click(400 + i * 10, 1500)
            QTest.keyClicks(self.editor(), "note %d" % i)
            QTest.keyClick(self.editor(), Qt.Key_Return)
            self.qapp.processEvents()
            colors.append(self.sent()[-1][1]["obj"]["color"])
        self.assertEqual(colors[:count], list(tt.STICKY_PALETTE))
        self.assertEqual(len(set(colors[:count])), count)
        self.assertEqual(colors[count], tt.STICKY_PALETTE[0])
        sticky = self.sent()[-1][1]["obj"]
        self.assertEqual((sticky["kind"], sticky["w"], sticky["h"], sticky["size"]), ("sticky", 200, 160, 16))
        self.assertEqual(kinds_text.validate(sticky), (True, ""))

    # -- double-click editing with Akshay's select tool --------------------------------------
    def test_double_click_edits_text_and_sends_op_update(self):
        """Double-click opens the editor with the text; a change sends OP_UPDATE {obj_id, set:{text}}."""
        text_id = self.add_text_via_tool("Hello")
        self.app.net.sent.clear()
        self.window.select_tool("select")
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(150, 130))
        self.qapp.processEvents()
        ed = self.editor()
        self.assertIsNotNone(ed)
        self.assertEqual(ed.toPlainText(), "Hello")
        QTest.keyClick(ed, Qt.Key_Escape)                           # cancel: nothing sent
        self.assertEqual(self.sent(), [])
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(150, 130))
        QTest.keyClick(self.editor(), Qt.Key_Return)                # unchanged: nothing sent
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [])
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(150, 130))
        self.editor().selectAll()
        QTest.keyClicks(self.editor(), "edited")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [("OP_UPDATE", {"obj_id": text_id, "set": {"text": "edited"}})])
        self.assertEqual(self.app.board.objects[text_id]["text"], "edited")

    def test_double_click_edits_a_sticky(self):
        """The same editor works for a sticky; click-away sends the change."""
        self.window.select_tool("sticky")
        self.click(500, 500)
        QTest.keyClicks(self.editor(), "first")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        sticky_id = self.sent()[-1][1]["obj"]["id"]
        self.app.net.sent.clear()
        self.window.select_tool("select")
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(500, 500))
        self.qapp.processEvents()
        self.editor().selectAll()
        QTest.keyClicks(self.editor(), "second")
        QTest.mouseClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(3000, 2500))
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [("OP_UPDATE", {"obj_id": sticky_id, "set": {"text": "second"}})])

    # -- format bar (client/toolbar.py) and text formatting ------------------------------
    def format_bar(self):
        """The FormatBar of the tool dock, refreshed from the current tool and selection."""
        bar = toolbar._dock._format_box
        bar.refresh()
        return bar

    def select_object(self, obj_id, x, y):
        """Switch to the select tool and click board point (x, y) so obj_id is selected."""
        self.window.select_tool("select")
        self.click(x, y)
        self.assertEqual(self.app.active_tool.selected_ids, [obj_id])

    def test_format_bar_visibility(self):
        """Hidden for the pen, shown for the text and sticky tools; the sticky also has the palette."""
        self.window.select_tool("pen")
        self.assertTrue(self.format_bar().isHidden())
        self.window.select_tool("text")
        bar = self.format_bar()
        self.assertFalse(bar.isHidden())
        self.assertTrue(bar._bg_box.isHidden())
        self.window.select_tool("sticky")
        bar = self.format_bar()
        self.assertFalse(bar.isHidden())
        self.assertFalse(bar._bg_box.isHidden())
        self.assertEqual(len(bar._bg_swatches), 8)

    def test_format_bar_updates_the_selected_text(self):
        """With a text selected, each control sends one OP_UPDATE {obj_id, set:{field}}."""
        text_id = self.add_text_via_tool("Hello")
        self.select_object(text_id, 150, 130)
        bar = self.format_bar()
        self.assertFalse(bar.isHidden())
        self.assertEqual(bar._size.value(), 18)
        self.app.net.sent.clear()
        bar._size.setValue(40)
        bar._flags["bold"].click()
        bar._aligns["center"].click()
        bar._send({"font": "Arial"})
        bar._send({}, ink="#ff0000")
        self.assertEqual(self.sent(), [
            ("OP_UPDATE", {"obj_id": text_id, "set": {"size": 40}}),
            ("OP_UPDATE", {"obj_id": text_id, "set": {"bold": True}}),
            ("OP_UPDATE", {"obj_id": text_id, "set": {"align": "center"}}),
            ("OP_UPDATE", {"obj_id": text_id, "set": {"font": "Arial"}}),
            ("OP_UPDATE", {"obj_id": text_id, "set": {"color": "#ff0000"}})])
        obj = self.app.board.objects[text_id]
        self.assertEqual((obj["size"], obj["bold"], obj["align"], obj["font"], obj["color"]),
                         (40, True, "center", "Arial", "#ff0000"))
        self.assertEqual(kinds_text.validate(obj), (True, ""))
        self.assertTrue(bar._flags["bold"].isChecked())
        self.assertTrue(bar._aligns["center"].isChecked())

    def test_format_bar_updates_a_selected_sticky(self):
        """A sticky's text color goes to text_color and its background to color."""
        self.window.select_tool("sticky")
        self.click(500, 500)
        QTest.keyClicks(self.editor(), "note")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        sticky_id = self.sent()[-1][1]["obj"]["id"]
        self.select_object(sticky_id, 500, 500)
        bar = self.format_bar()
        self.app.net.sent.clear()
        bar._send({}, ink="#112233")
        bar._send({}, bg="#90caf9")
        self.assertEqual(self.sent(), [
            ("OP_UPDATE", {"obj_id": sticky_id, "set": {"text_color": "#112233"}}),
            ("OP_UPDATE", {"obj_id": sticky_id, "set": {"color": "#90caf9"}})])
        bar._send({"size": 500})                                    # outside 8-96: refused
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("size", self.toasts[-1])

    def test_format_bar_sets_defaults_of_the_next_object(self):
        """With nothing selected the bar changes the format of the NEXT text / sticky only."""
        tt._new_overrides["text"].clear()
        tt._new_overrides["sticky"].clear()
        self.window.select_tool("text")
        bar = self.format_bar()
        bar._size.setValue(30)
        bar._flags["italic"].click()
        bar._aligns["right"].click()
        self.assertEqual(self.sent(), [])                           # nothing goes to the network
        self.drag((100, 100), (400, 220))
        QTest.keyClicks(self.editor(), "next")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        obj = self.sent()[-1][1]["obj"]
        self.assertEqual((obj["size"], obj["italic"], obj["align"], obj["bold"]), (30, True, "right", False))
        self.assertEqual(kinds_text.validate(obj), (True, ""))
        self.window.select_tool("sticky")
        bar = self.format_bar()
        bar._send({}, bg="#e0e0e0")
        bar._send({}, ink="#aa0000")
        self.click(1000, 1000)
        QTest.keyClicks(self.editor(), "note")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        sticky = self.sent()[-1][1]["obj"]
        self.assertEqual((sticky["color"], sticky["text_color"], sticky["size"]), ("#e0e0e0", "#aa0000", 16))
        tt._new_overrides["text"].clear()
        tt._new_overrides["sticky"].clear()

    def test_editor_font_follows_zoom_and_style(self):
        """The overlay font is size x zoom in the object's family/style and is redone on zoom."""
        text_id = self.add_text_via_tool("Hello")
        self.app.board.objects[text_id].update(size=20, bold=True, font="Arial", align="center")
        self.window.select_tool("select")
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(150, 130))
        ed = self.editor()
        self.assertEqual((ed.font().pixelSize(), ed.font().bold(), ed.font().family()), (20, True, "Arial"))
        self.assertTrue(ed.document().defaultTextOption().alignment() & Qt.AlignHCenter)
        self.canvas.set_zoom(2.0)
        ed._sync_geometry()
        self.assertEqual(ed.font().pixelSize(), 40)
        self.assertEqual(ed.geometry().width(), int(self.app.board.objects[text_id]["w"] * 2.0))
        QTest.keyClick(ed, Qt.Key_Escape)

    def test_text_scales_with_the_zoom(self):
        """The same object painted at zoom 1 and 3 has the same ink bounds in board units."""
        obj = make(x=10, y=10, w=300, h=120, size=24, text="Zoom test of wrapping text here")

        def bounds(zoom):
            image = QImage(int(340 * zoom), int(160 * zoom), QImage.Format_ARGB32)
            image.fill(QColor("white"))
            painter = QPainter(image)
            painter.scale(zoom, zoom)
            render.render_board(painter, {"a": dict(obj, z=1)}, {})
            painter.end()
            dark = [(x, y) for y in range(image.height()) for x in range(image.width())
                    if QColor(image.pixel(x, y)).lightness() < 128]
            xs, ys = [d[0] for d in dark], [d[1] for d in dark]
            return (min(xs) / zoom, max(xs) / zoom, min(ys) / zoom, max(ys) / zoom) if dark else None

        one, three = bounds(1), bounds(3)
        if one is None or three is None:
            self.skipTest("no fonts on this Qt platform")
        for a, b in zip(one, three):
            self.assertAlmostEqual(a, b, delta=2.0)

    def test_object_deleted_while_editing(self):
        """If the object disappears during the edit, nothing is sent and the user gets a toast."""
        text_id = self.add_text_via_tool("Hello")
        self.app.net.sent.clear()
        self.window.select_tool("select")
        QTest.mouseDClick(self.canvas, Qt.LeftButton, Qt.NoModifier, self.scr(150, 130))
        self.qapp.processEvents()
        del self.app.board.objects[text_id]
        self.editor().selectAll()
        QTest.keyClicks(self.editor(), "ghost")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.toasts[-1], "That object was deleted while you were editing it.")

    # -- viewers ------------------------------------------------------------------------------
    def test_viewer_cannot_create_or_edit(self):
        """For a viewer both tools and the editor do nothing except a toast."""
        self.app.set_role("viewer")
        self.toasts.clear()                  # drop the "Your role is now viewer" toast
        for tool in ("text", "sticky"):
            self.app.active_tool = registry.TOOLS[tool]()
            self.click(700, 800)
            self.drag((100, 800), (300, 900))
            self.assertIsNone(self.editor())
        self.assertTrue(self.toasts and all(t == "Viewers cannot draw" for t in self.toasts))
        tt.edit_object(self.app, self.canvas, make())
        self.assertIsNone(self.editor())
        self.assertEqual(self.sent(), [])

    def test_role_lost_while_typing_sends_nothing(self):
        """If I become a viewer while the editor is open, Enter sends nothing."""
        self.window.select_tool("sticky")
        self.click(700, 800)
        QTest.keyClicks(self.editor(), "too late")
        self.app.set_role("viewer")
        QTest.keyClick(self.editor(), Qt.Key_Return)
        self.qapp.processEvents()
        self.assertEqual(self.sent(), [])

    # -- registration -------------------------------------------------------------------------
    def test_registrations(self):
        """Tools text and sticky (group text, keys T and N) and both editors are registered."""
        self.assertEqual((registry.TOOLS["text"].shortcut, registry.TOOLS["sticky"].shortcut), ("T", "N"))
        self.assertEqual({registry.TOOLS["text"].group, registry.TOOLS["sticky"].group}, {"text"})
        self.assertIs(registry.get_editor("text"), tt.edit_object)
        self.assertIs(registry.get_editor("sticky"), tt.edit_object)


if __name__ == "__main__":
    unittest.main()
