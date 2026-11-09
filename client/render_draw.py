# OWNER: P2
"""render_draw.py: Renderers for stroke, line, arrow, rect and ellipse objects.

Contract sections F and G. One function per kind, registered with
registry.register_renderer(kind, fn(painter, obj, ctx)). render.render_board
calls them in z order and wraps each call in painter.save()/restore(), so a
renderer may set pens and brushes freely.

All coordinates are board units; the caller's painter transform maps them
to the screen (canvas zoom/pan) or to an export image, so the renderers
never think about zoom. Missing optional fields fall back to the defaults
in common/kinds_draw.py, so a half-built stroke still paints.
"""

import math

from PyQt5.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF

from client import registry
from common import kinds_draw

# Arrowhead shape: length grows with the line width, half-angle 25 degrees.
ARROW_MIN_LEN = 12.0
ARROW_LEN_PER_WIDTH = 4.0
ARROW_HALF_ANGLE = math.radians(25)


def _color(obj, field="color", alpha=1.0):
    """Return a QColor for obj[field] ('#rrggbb') with the given alpha 0-1."""
    color = QColor(kinds_draw.get(obj, field) or "#000000")
    color.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return color


def _pen(obj, cap=Qt.RoundCap, join=Qt.RoundJoin, alpha=1.0):
    """Build the outline pen of a draw object from its color and width."""
    pen = QPen(_color(obj, alpha=alpha))
    pen.setWidthF(float(kinds_draw.get(obj, "width")))
    pen.setCapStyle(cap)
    pen.setJoinStyle(join)
    return pen


def _smooth_path(points):
    """Build a smooth QPainterPath through the points.

    Each raw point becomes the control point of a quadratic curve between
    the midpoints of its neighbouring segments. This hides the corners of
    mouse sampling without moving the stroke away from what was drawn.
    """
    path = QPainterPath(QPointF(*points[0]))
    if len(points) == 2:
        path.lineTo(QPointF(*points[1]))
        return path
    for i in range(1, len(points) - 1):
        ctrl = QPointF(*points[i])
        nxt = QPointF(*points[i + 1])
        path.quadTo(ctrl, (ctrl + nxt) / 2.0)
    path.lineTo(QPointF(*points[-1]))
    return path


def render_stroke(painter, obj, ctx):
    """Paint a freehand stroke (pen, marker or highlighter).

    pen and marker: round caps and joins. highlighter: flat cap, so it
    looks like a chisel-tip highlighter. The object's alpha goes into the
    pen color. The whole stroke is ONE path, so a translucent stroke does
    not get darker where it overlaps itself.
    """
    points = obj.get("points") or []
    if not points:
        return
    painter.setRenderHint(QPainter.Antialiasing, True)
    if kinds_draw.get(obj, "tool") == "highlighter":
        pen = _pen(obj, cap=Qt.FlatCap, join=Qt.BevelJoin, alpha=kinds_draw.get(obj, "alpha"))
    else:
        pen = _pen(obj, alpha=kinds_draw.get(obj, "alpha"))
    if len(points) == 1:
        # A click without movement: a dot the size of the pen tip. Qt skips
        # zero-length lines, so fill a circle (square for the flat
        # highlighter tip) instead of relying on the cap.
        r = pen.widthF() / 2.0
        x, y = points[0]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(pen.color()))
        if pen.capStyle() == Qt.FlatCap:
            painter.drawRect(QRectF(x - r, y - r, 2 * r, 2 * r))
        else:
            painter.drawEllipse(QPointF(x, y), r, r)
        return
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(_smooth_path(points))


def render_line(painter, obj, ctx):
    """Paint a straight line from (x1, y1) to (x2, y2) with round caps."""
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(_pen(obj))
    painter.drawLine(QLineF(obj["x1"], obj["y1"], obj["x2"], obj["y2"]))


def render_arrow(painter, obj, ctx):
    """Paint a line from (x1, y1) to (x2, y2) with a filled arrowhead at (x2, y2).

    The shaft stops at the base of the head, so a thick line does not
    poke out through the tip.
    """
    painter.setRenderHint(QPainter.Antialiasing, True)
    start = QPointF(obj["x1"], obj["y1"])
    tip = QPointF(obj["x2"], obj["y2"])
    dx, dy = tip.x() - start.x(), tip.y() - start.y()
    length = math.hypot(dx, dy)
    width = float(kinds_draw.get(obj, "width"))
    head = max(ARROW_MIN_LEN, width * ARROW_LEN_PER_WIDTH)
    if length < 1e-6:
        # Degenerate arrow: no direction, so just a dot.
        painter.setPen(_pen(obj))
        painter.drawLine(QLineF(start, tip))
        return
    head = min(head, length)  # a short arrow is all head
    angle = math.atan2(dy, dx)
    # The two back corners of the triangle, rotated +-25 degrees from the shaft.
    left = QPointF(tip.x() - head * math.cos(angle - ARROW_HALF_ANGLE),
                   tip.y() - head * math.sin(angle - ARROW_HALF_ANGLE))
    right = QPointF(tip.x() - head * math.cos(angle + ARROW_HALF_ANGLE),
                    tip.y() - head * math.sin(angle + ARROW_HALF_ANGLE))
    base = QPointF(tip.x() - head * math.cos(ARROW_HALF_ANGLE) * math.cos(angle),
                   tip.y() - head * math.cos(ARROW_HALF_ANGLE) * math.sin(angle))

    painter.setPen(_pen(obj, cap=Qt.FlatCap))
    painter.drawLine(QLineF(start, base))
    color = _color(obj)
    painter.setPen(QPen(color, 1.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(QBrush(color))
    painter.drawPolygon(QPolygonF([tip, left, right]))


def _box(obj):
    """Return the QRectF of a rect/ellipse, normalized so w and h are positive."""
    return QRectF(obj["x"], obj["y"], obj["w"], obj["h"]).normalized()


def _setup_box(painter, obj):
    """Set the outline pen and the optional fill brush for rect and ellipse."""
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(_pen(obj, cap=Qt.SquareCap, join=Qt.MiterJoin))
    fill = kinds_draw.get(obj, "fill")
    painter.setBrush(QBrush(_color(obj, "fill")) if fill else Qt.NoBrush)


def render_rect(painter, obj, ctx):
    """Paint a rectangle outline, filled when obj['fill'] is a color."""
    _setup_box(painter, obj)
    painter.drawRect(_box(obj))


def render_ellipse(painter, obj, ctx):
    """Paint an ellipse inside the box (x, y, w, h), filled when obj['fill'] is a color."""
    _setup_box(painter, obj)
    painter.drawEllipse(_box(obj))


registry.register_renderer("stroke", render_stroke)
registry.register_renderer("line", render_line)
registry.register_renderer("arrow", render_arrow)
registry.register_renderer("rect", render_rect)
registry.register_renderer("ellipse", render_ellipse)
