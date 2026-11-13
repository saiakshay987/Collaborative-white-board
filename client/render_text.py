"""render_text.py: Renderers for text and sticky objects.

Contract sections D, F and G. Two functions registered with
registry.register_renderer(kind, fn(painter, obj, ctx)); render.render_board
calls them in z order and wraps each call in painter.save()/restore().

    text    the text in its `color`, word-wrapped inside the box, no background
    sticky  a rounded rectangle in its `color` (the background) with a light
            shadow, and the text in its `text_color` word-wrapped inside it

Both honour the format fields font, size, bold, italic, underline and align
(see common/kinds_text.py); a missing field means its default, so objects saved
before the fields existed look the same as a default-formatted one.

All coordinates are board units; the caller's painter transform does the zoom,
so nothing here knows about it. Text longer than the box is CLIPPED to the box:
the box (which Akshay's select tool moves and resizes) decides what is visible, and
the same object looks the same for everybody and in Janith's exports.

The font size is a PIXEL size in board units (not a point size), so it does not
depend on the screen's DPI and every client wraps the text the same way. Words
longer than the box are broken anywhere instead of overflowing.

Zoom: the font is ALWAYS made at the board size and the painter's zoom transform
(set once by the canvas, never reset here) scales it together with the box, so
text and box grow and shrink as one, also in the PNG/PDF export. Font hinting is
turned off (QFont.PreferNoHinting): hinting snaps glyphs to whole screen pixels
and would make the wrapping depend on the zoom level.

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
STICKY_TEXT_COLOR = kinds_text.STICKY_TEXT_COLOR   # default dark text on every pastel background
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


def family_of(obj):
    """Return the font family of obj; a missing or empty one gives the default family."""
    family = kinds_text.get(obj, "font")
    if isinstance(family, str) and family.strip():
        return family.strip()
    return kinds_text.TEXT_DEFAULTS["font"]


def flag_of(obj, field):
    """Return the bool format field `field` (bold, italic, underline); anything but True is False."""
    return kinds_text.get(obj, field) is True


def align_flag(name):
    """Return the Qt horizontal alignment flag for 'left', 'center' or 'right'; anything else gives left."""
    return {"center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(name, Qt.AlignLeft)


def align_of(obj):
    """Return the object's alignment as a Qt flag; an unknown value gives left."""
    return align_flag(kinds_text.get(obj, "align"))


def text_color_of(obj):
    """Return the color the text is drawn in: `color` for a text, `text_color` for a sticky."""
    if obj.get("kind") == "sticky":
        color = QColor(str(kinds_text.get(obj, "text_color")))
        return color if color.isValid() else QColor(STICKY_TEXT_COLOR)
    return color_of(obj, "#000000")


def make_font(pixel_size, family=None, bold=False, italic=False, underline=False):
    """Return a font of pixel_size (whole pixels, at least 1) in the given family and style.

    The caller passes the BOARD size for drawing on the board (the painter's
    zoom does the rest) and size x zoom for the on-screen text editor. Hinting
    is off so the glyph widths scale linearly with the size (see the module doc).
    """
    font = QFont(family or kinds_text.TEXT_DEFAULTS["font"])
    font.setPixelSize(max(1, int(round(pixel_size))))
    font.setBold(bool(bold))
    font.setItalic(bool(italic))
    font.setUnderline(bool(underline))
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def font_of(obj, scale=1.0):
    """Return obj's complete font (family, style, size x scale); scale is 1 on the board."""
    return make_font(size_of(obj) * scale, family_of(obj), flag_of(obj, "bold"),
                     flag_of(obj, "italic"), flag_of(obj, "underline"))


def wrap_option(align=Qt.AlignLeft):
    """Return the QTextOption used everywhere: horizontally aligned, top, wrap at words else anywhere."""
    option = QTextOption(align | Qt.AlignTop)
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    return option


def draw_wrapped(painter, rect, text, font, color, align=Qt.AlignLeft):
    """Draw text word-wrapped inside rect, clipped to rect, in the given font, color and alignment."""
    if rect.width() < MIN_TEXT_BOX or rect.height() < MIN_TEXT_BOX or not text:
        return
    painter.setClipRect(rect)
    painter.setFont(font)
    painter.setPen(QPen(color))
    painter.drawText(rect, text, wrap_option(align))


def render_text(painter, obj, ctx):
    """Paint a text object: its text, word-wrapped inside the box, formatted and in its color."""
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    text = str(kinds_text.get(obj, "text") or "")
    draw_wrapped(painter, box_of(obj), text, font_of(obj), text_color_of(obj), align_of(obj))


def render_sticky(painter, obj, ctx):
    """Paint a sticky note: soft shadow, rounded background, formatted wrapped text inside."""
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
    draw_wrapped(painter, inner, text, font_of(obj), text_color_of(obj), align_of(obj))


registry.register_renderer("text", render_text)
registry.register_renderer("sticky", render_sticky)
