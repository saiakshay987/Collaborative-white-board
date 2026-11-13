"""render.py: render_board(painter, objects, ctx): paints the board through the renderer registry.

Contract section F. Each object is painted by the renderer registered for
its kind (registry.register_renderer). Objects are painted in z order
(z = seq at creation, set by the server), so later objects are on top.
Objects without z yet (optimistic local adds waiting for the echo) are
painted last, on top, in their insertion order.

The same function paints the canvas, and Janith's exports reuse it on a QImage
or QPdfWriter: it only needs a QPainter.
"""

import traceback

from client import registry

_reported = set()  # kinds whose missing/broken renderer was already reported


def _z_key(indexed):
    """Sort key: (has no z, z, insertion index)."""
    index, obj = indexed
    z = obj.get("z")
    return (z is None, z if z is not None else 0, index)


def render_board(painter, objects, ctx=None):
    """Paint objects (an ordered dict obj_id -> obj, or a list) in z order.

    painter.save()/restore() around each object stops one renderer's pen,
    brush or transform from leaking into the next. A missing or failing
    renderer is reported once per kind and that object is skipped, so the
    rest of the board still paints.
    """
    items = objects.values() if hasattr(objects, "values") else objects
    for _, obj in sorted(enumerate(items), key=_z_key):
        kind = obj.get("kind")
        fn = registry.get_renderer(kind)
        if fn is None:
            if kind not in _reported:
                _reported.add(kind)
                print("[render] no renderer for kind %r" % (kind,))
            continue
        painter.save()
        try:
            fn(painter, obj, ctx)
        except Exception:  # noqa: BLE001
            if kind not in _reported:
                _reported.add(kind)
                traceback.print_exc()
        finally:
            painter.restore()
