# OWNER: P4
"""render_text.py: Renderers for text and sticky objects.

Contract sections D, F and G. Two functions registered with
registry.register_renderer(kind, fn(painter, obj, ctx)); render.render_board
calls them in z order and wraps each call in painter.save()/restore().

    text    the text in its `color`, word-wrapped inside the box, no background
    sticky  a rounded rectangle in its `color` (the background) with a light
            shadow, and dark text word-wrapped inside it

All coordinates are board units; the caller's painter transform does the zoom,
so nothing here knows about it. Text longer than the box is CLIPPED to the box:
the box (which P2's select tool moves and resizes) decides what is visible, and
the same object looks the same for everybody and in P3's exports.

The font size is a PIXEL size in board units (not a point size), so it does not
depend on the screen's DPI and every client wraps the text the same way. Words
longer than the box are broken anywhere instead of overflowing.

A renderer must never raise on an odd object (another client may send anything
that passed the server's type and limit checks), so every field goes through
kinds_text.get() with a fallback: a missing or invalid color becomes the
default color, a missing size the default size, a negative w or h is
normalized.
"""

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPen, QTextOption

from client import registry
from common import kinds_text

# Sticky look.
STICKY_TEXT_COLOR = "#2b2b2b"   # dark text on every pastel background
STICKY_RADIUS = 8.0             # corner radius, board units
STICKY_PAD = 10.0               # space between the sticky's edge and its text
SHADOW_OFFSET = 3.0             # how far the shadow is shifted down and right
SHADOW_LAYERS = 3               # stacked translucent rects fake a soft edge
SHADOW_ALPHA = 28               # alpha (0-255) of each shadow layer

# Text with this little room is not worth laying out (and may be a half-built object).
MIN_TEXT_BOX = 1.0


def _num(value, default):
    """Return value as a float, or default if it is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def box_of(obj):
    """Return the object's x, y, w, h as a normalized QRectF (w and h made non-negative)."""
    return QRectF(_num(obj.get("x"), 0.0), _num(obj.get("y"), 0.0),
                  _num(obj.get("w"), 0.0), _num(obj.get("h"), 0.0)).normalized()


def color_of(obj, fallback):
    """Return the object's color as a QColor; an invalid or missing one gives fallback."""
    color = QColor(str(kinds_text.get(obj, "color")))
    return color if color.isValid() else QColor(fallback)


def size_of(obj):
    """Return the font size of obj in board units, kept inside the contract limits."""
    low, high = kinds_text.LIMITS["size"]
    default = kinds_text.TEXT_DEFAULTS["size"]
    return max(low, min(high, _num(kinds_text.get(obj, "size"), default)))


def make_font(pixel_size):
    """Return the board font at pixel_size (whole pixels, at least 1).

    A generic family keeps the look close on Windows, Linux and macOS.
    """
    font = QFont("Sans Serif")
    font.setPixelSize(max(1, int(round(pixel_size))))
    return font


def wrap_option():
    """Return the QTextOption used everywhere: top-left aligned, wrap at words, else anywhere."""
    option = QTextOption(Qt.AlignLeft | Qt.AlignTop)
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    return option


def draw_wrapped(painter, rect, text, font, color):
    """Draw text word-wrapped inside rect, clipped to rect, in the given font and color."""
    if rect.width() < MIN_TEXT_BOX or rect.height() < MIN_TEXT_BOX or not text:
        return
    painter.setClipRect(rect)
    painter.setFont(font)
    painter.setPen(QPen(color))
    painter.drawText(rect, text, wrap_option())


def render_text(painter, obj, ctx):
    """Paint a text object: its text, word-wrapped inside the box, in its color."""
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    text = str(kinds_text.get(obj, "text") or "")
    draw_wrapped(painter, box_of(obj), text, make_font(size_of(obj)), color_of(obj, "#000000"))


def render_sticky(painter, obj, ctx):
    """Paint a sticky note: soft shadow, rounded background, dark wrapped text inside."""
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    rect = box_of(obj)
    if rect.width() < MIN_TEXT_BOX or rect.height() < MIN_TEXT_BOX:
        return
    # Shadow: a few translucent rounded rects, the biggest and faintest last,
    # shifted down and right, so the edge looks soft without any blur filter.
    painter.setPen(Qt.NoPen)
    for layer in range(SHADOW_LAYERS):
        grow = layer * 1.5
        shadow = rect.translated(SHADOW_OFFSET, SHADOW_OFFSET).adjusted(-grow, -grow, grow, grow)
        painter.setBrush(QBrush(QColor(0, 0, 0, SHADOW_ALPHA)))
        painter.drawRoundedRect(shadow, STICKY_RADIUS + grow, STICKY_RADIUS + grow)
    # The note itself, with a thin darker edge so a pale color stays visible on white.
    background = color_of(obj, kinds_text.STICKY_DEFAULTS["color"])
    edge = QPen(background.darker(115))
    edge.setCosmetic(True)
    painter.setPen(edge)
    painter.setBrush(QBrush(background))
    painter.drawRoundedRect(rect, STICKY_RADIUS, STICKY_RADIUS)
    text = str(kinds_text.get(obj, "text") or "")
    inner = rect.adjusted(STICKY_PAD, STICKY_PAD, -STICKY_PAD, -STICKY_PAD)
    draw_wrapped(painter, inner, text, make_font(size_of(obj)), QColor(STICKY_TEXT_COLOR))


registry.register_renderer("text", render_text)
registry.register_renderer("sticky", render_sticky)
