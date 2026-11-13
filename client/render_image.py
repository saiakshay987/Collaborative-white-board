"""render_image.py: Renderer for image objects: the picture, or a grey placeholder while it is not there.

registry.register_renderer('image', render_image), called by render.render_board for every object of kind 'image'
{x, y, w, h, asset_id} in board units (the painter already has the zoom/pan, or the export, transform).

    picture available (asset_cache memory or disk)   drawn into the box, smoothly scaled
    picture not available                            a grey box with a cross and the word 'loading'
                                                     ('unavailable' when the server does not have it or the bytes
                                                      were damaged, 'image' for an asset id that cannot be one)

Asking the cache for the picture is what starts the download (one ASSET_GET, see asset_cache.py), so a board that
is joined with images on it fetches them as they are painted, and the canvas repaints when they arrive.

Exports: ctx["export"] is true when client/export.py paints. Then nothing is requested (an export is a snapshot of
what this client has, it must not start network traffic) and a missing picture is a placeholder, never an error.
A renderer must not raise for a damaged object, so every field is read defensively.
"""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen

from client import asset_cache, registry

PLACEHOLDER_FILL = QColor("#ececec")
PLACEHOLDER_LINE = QColor("#9a9a9a")
CAPTIONS = {asset_cache.LOADING: "loading", asset_cache.FAILED: "unavailable", asset_cache.INVALID: "image"}


def _number(value, default=0.0):
    """value as a finite float, or default (a damaged object must not crash a renderer)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def box_of(obj):
    """The box of an image object as a normalized QRectF (a negative w or h is allowed while resizing)."""
    return QRectF(_number(obj.get("x")), _number(obj.get("y")), _number(obj.get("w")), _number(obj.get("h"))).normalized()


def paint_placeholder(painter, box, caption):
    """Paint the stand-in of a picture: a grey box, a cross and `caption`. A box under 1 unit is skipped.

    Everything is in board units and uses ordinary (not cosmetic) pens, so
    it scales with the view and with an export.
    """
    if box.width() < 1 or box.height() < 1:
        return
    pen = QPen(PLACEHOLDER_LINE)
    pen.setWidthF(2.0)
    painter.setPen(pen)
    painter.setBrush(PLACEHOLDER_FILL)
    painter.drawRect(box)
    painter.drawLine(box.topLeft(), box.bottomRight())
    painter.drawLine(box.topRight(), box.bottomLeft())
    size = min(24.0, box.height() / 4.0, box.width() / 6.0)
    if size >= 6:
        font = QFont()
        font.setPixelSize(int(size))
        painter.setFont(font)
        label = QRectF(box.center().x() - size * 2.5, box.center().y() - size, size * 5, size * 2)
        painter.fillRect(label, PLACEHOLDER_FILL)           # keeps the word readable over the cross
        painter.setPen(PLACEHOLDER_LINE.darker(150))
        painter.drawText(label, Qt.AlignCenter, caption)


def render_image(painter, obj, ctx):
    """Paint one image object (see the module docstring)."""
    box = box_of(obj)
    if box.width() < 1 or box.height() < 1:
        return
    asset_id = obj.get("asset_id")
    exporting = bool(ctx and ctx.get("export"))
    image = asset_cache.cache.get_image(asset_id, request=not exporting)
    if image is not None and not image.isNull():
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(box, image)
        return
    paint_placeholder(painter, box, CAPTIONS.get(asset_cache.cache.state(asset_id), "loading"))


registry.register_renderer("image", render_image)
