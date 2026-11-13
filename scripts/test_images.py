# OWNER: P3
"""test_images.py: Tests for the client side of pictures: asset_cache, render_image and tools/image_tool.

Run from the project root:  python scripts/test_images.py

Needs PyQt5, runs on Qt's offscreen platform (no window, NO FONTS: captions paint nothing, so pixel checks use
the grey fill and the cross, not text) with fake net/canvas objects that only record what happens. One test feeds
the uploader's output into the real server-side AssetStore, so the two halves of the protocol are checked against
each other. Disk caches live in temporary folders.
"""
import base64
import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WB_DATA_DIR"] = tempfile.mkdtemp(prefix="wb_img_test_")      # server.versions (imported below) must not touch server_data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication, QFileDialog  # noqa: E402

from common import config, kinds_image, protocol  # noqa: E402
from client import asset_cache, registry, render, render_image  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.tools import image_tool  # noqa: E402
from server.assets import AssetStore  # noqa: E402
from server import versions  # noqa: E402

QAPP = QApplication.instance() or QApplication([])
versions.stop()                     # the autosave thread of the server module is not needed here

CHUNK = kinds_image.CHUNK_BYTES


# ---------------------------------------------------------------- helpers

def encode(image, fmt="PNG"):
    """Encode a QImage to bytes."""
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert image.save(buf, fmt)
    return bytes(buf.data())


def solid(w, h, color="#ff0000", alpha=False):
    """A QImage filled with one color."""
    image = QImage(w, h, QImage.Format_ARGB32 if alpha else QImage.Format_RGB32)
    image.fill(QColor(color))
    return image


def noisy(w, h, alpha=False):
    """A QImage of random pixels: it compresses badly, like a photo (and unlike a diagram)."""
    fmt = QImage.Format_RGBA8888 if alpha else QImage.Format_RGB32
    raw = os.urandom(w * h * 4)
    return QImage(raw, w, h, w * 4, fmt).copy()


def png_bytes(w=20, h=20, color="#ff0000"):
    """The bytes of a small PNG."""
    return encode(solid(w, h, color))


def sha(data):
    """sha256 hex."""
    return hashlib.sha256(data).hexdigest()


def stream(data, mime="image/png", name="x.png"):
    """What the server sends for an ASSET_GET of data: [(type, payload)] BEGIN, CHUNKs, END."""
    aid, n = sha(data), kinds_image.chunk_count(len(data))
    out = [(protocol.IMG_BEGIN, {"asset_id": aid, "name": name, "mime": mime, "size": len(data), "chunks": n, "w": 20, "h": 20})]
    for i in range(n):
        out.append((protocol.IMG_CHUNK, {"asset_id": aid, "idx": i,
                                         "data": base64.b64encode(data[i * CHUNK:(i + 1) * CHUNK]).decode()}))
    out.append((protocol.IMG_END, {"asset_id": aid, "sha256": aid}))
    return out


class FakeNet:
    """Stands in for NetClient: records send() calls; send returns False when not connected."""

    def __init__(self):
        """Connected, nothing sent."""
        self.is_connected = True
        self.sent = []

    def send(self, type, payload=None, op_id=None):
        """Record the message."""
        if not self.is_connected:
            return False
        self.sent.append((type, payload, op_id))
        return True

    def of_type(self, type_):
        """The payloads sent with this type."""
        return [p for t, p, _ in self.sent if t == type_]


class FakeCanvas:
    """Stands in for CanvasWidget: the view rectangle, snapping and repaint counting."""

    def __init__(self, view=None, snap_on=False):
        """A view over part of the board."""
        self.view = view or QRectF(1000, 800, 1000, 600)
        self.snap_enabled = snap_on
        self.updates = 0

    def visible_board_rect(self):
        """The visible part of the board in board units."""
        return QRectF(self.view)

    def snap(self, pt):
        """Snap to the 20 unit grid."""
        g = config.GRID
        return QPointF(round(pt.x() / g) * g, round(pt.y() / g) * g)

    def update(self):
        """Count repaint requests."""
        self.updates += 1


def make_app(role="editor", canvas=None):
    """A real AppState with a fake net, canvas and toast recorder."""
    app = AppState()
    app.net = FakeNet()
    app.canvas = canvas
    app.me.username, app.me.role = "hana", role
    app.toasts = []
    app.toast = app.toasts.append
    return app


def run_until(condition, timeout_s=5.0):
    """Run the Qt event loop (timers) until condition() is true; returns it."""
    deadline = time.time() + timeout_s
    while not condition() and time.time() < deadline:
        QAPP.processEvents()
        time.sleep(0.002)
    return condition()


class CacheBase(unittest.TestCase):
    """A cache with its own disk folder, wired to a fake app."""

    def setUp(self):
        """Fresh cache, folder, app and canvas."""
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.cache = asset_cache.AssetCache(self.dir)
        self.canvas = FakeCanvas()
        self.app = make_app("editor", self.canvas)
        self.cache.app = self.app

    def tearDown(self):
        """Remove the folder."""
        self._tmp.cleanup()

    def feed(self, messages, cache=None):
        """Hand server messages to the cache the way the on_message handlers do."""
        cache = cache or self.cache
        handlers = {protocol.IMG_BEGIN: cache.on_begin, protocol.IMG_CHUNK: cache.on_chunk, protocol.IMG_END: cache.on_end}
        for type_, payload in messages:
            handlers[type_](payload)


# ---------------------------------------------------------------- asset_cache

class CacheLookupTests(CacheBase):
    """memory, disk and the request."""

    def test_store_then_get_from_memory(self):
        data = png_bytes()
        aid = sha(data)
        self.assertTrue(self.cache.store(aid, data))
        image = self.cache.get_image(aid)
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (20, 20))
        self.assertEqual(self.cache.state(aid), asset_cache.READY)
        self.assertEqual(self.app.net.sent, [])                                  # no request: we have it

    def test_picture_is_kept_on_disk_between_runs(self):
        data = png_bytes(30, 10)
        aid = sha(data)
        self.cache.store(aid, data)
        with open(os.path.join(self.dir, aid + ".bin"), "rb") as f:
            self.assertEqual(f.read(), data)
        fresh = asset_cache.AssetCache(self.dir)                                  # "next day": empty memory, same folder
        fresh.app = self.app
        image = fresh.get_image(aid)
        self.assertEqual((image.width(), image.height()), (30, 10))
        self.assertEqual(self.app.net.sent, [])

    def test_store_refuses_bytes_that_do_not_match_the_id(self):
        data = png_bytes()
        self.assertFalse(self.cache.store(sha(b"other"), data))                   # wrong hash
        self.assertFalse(self.cache.store(sha(b"not a picture"), b"not a picture"))   # right hash, not an image
        self.assertFalse(self.cache.store("../evil", data))
        self.assertEqual(os.listdir(self.dir), [])

    def test_damaged_disk_file_is_deleted_and_asked_for_again(self):
        data = png_bytes()
        aid = sha(data)
        with open(os.path.join(self.dir, aid + ".bin"), "wb") as f:
            f.write(data[:-3] + b"xyz")                                           # same name, different bytes
        self.assertIsNone(self.cache.get_image(aid))
        self.assertFalse(os.path.exists(os.path.join(self.dir, aid + ".bin")))
        self.assertEqual(self.app.net.of_type(protocol.ASSET_GET), [{"asset_id": aid}])

    def test_missing_picture_is_requested_exactly_once(self):
        aid = sha(b"unknown")
        for _ in range(5):                                                        # five paints
            self.assertIsNone(self.cache.get_image(aid))
        self.assertEqual(self.app.net.of_type(protocol.ASSET_GET), [{"asset_id": aid}])
        self.assertEqual(self.cache.state(aid), asset_cache.LOADING)

    def test_request_is_repeated_only_after_the_retry_time(self):
        now = [100.0]
        self.cache.clock = lambda: now[0]
        aid = sha(b"unknown")
        self.cache.get_image(aid)
        now[0] += asset_cache.RETRY_S - 1
        self.cache.get_image(aid)
        self.assertEqual(len(self.app.net.sent), 1)
        now[0] += 2
        self.cache.get_image(aid)
        self.assertEqual(len(self.app.net.sent), 2)

    def test_request_waits_for_a_connection(self):
        aid = sha(b"unknown")
        self.app.net.is_connected = False
        self.assertIsNone(self.cache.get_image(aid))
        self.assertEqual(self.cache.requested, {})                                # not marked: asked again at the next paint
        self.app.net.is_connected = True
        self.cache.get_image(aid)
        self.assertEqual(len(self.app.net.sent), 1)

    def test_export_never_requests(self):
        self.assertIsNone(self.cache.get_image(sha(b"unknown"), request=False))
        self.assertEqual(self.app.net.sent, [])

    def test_no_app_no_request_no_crash(self):
        self.cache.app = None
        self.assertIsNone(self.cache.get_image(sha(b"unknown")))

    def test_ids_that_are_not_asset_ids_never_touch_the_disk(self):
        outside = tempfile.mkdtemp()
        for bad in ("../" + "a" * 61, "..\\..\\x", "A" * 64, "g" * 64, "", None, 5, ["a"], {"a": 1}, "a" * 63):
            self.assertIsNone(self.cache.get_image(bad), bad)
            self.assertEqual(self.cache.state(bad), asset_cache.INVALID)
            self.assertFalse(self.cache.request(bad))
        self.assertEqual((self.app.net.sent, os.listdir(self.dir), os.listdir(outside)), ([], [], []))

    def test_memory_is_an_lru_and_evicted_pictures_come_back_from_disk(self):
        ids = []
        for i in range(asset_cache.MAX_MEMORY_IMAGES + 3):
            data = png_bytes(10 + i, 10)
            self.cache.store(sha(data), data)
            ids.append(sha(data))
        self.assertEqual(len(self.cache.images), asset_cache.MAX_MEMORY_IMAGES)
        self.assertNotIn(ids[0], self.cache.images)
        self.assertEqual(self.cache.get_image(ids[0]).width(), 10)                # reloaded from disk, no request
        self.assertEqual(self.app.net.sent, [])
        self.assertIn(ids[0], self.cache.images)                                  # and it is the most recent now

    def test_unwritable_disk_only_means_no_disk_cache(self):
        bad = asset_cache.AssetCache(os.path.join(self.dir, "x" * 300, "deeper"))
        data = png_bytes()
        self.assertTrue(bad.store(sha(data), data))                               # memory still works
        self.assertFalse(bad.get_image(sha(data)).isNull())


class CacheDownloadTests(CacheBase):
    """ASSET_GET and the stream that answers it."""

    def ask(self, data):
        """Request the picture and return its id."""
        aid = sha(data)
        self.assertIsNone(self.cache.get_image(aid))
        return aid

    def test_download_is_reassembled_verified_cached_and_repainted(self):
        data = encode(noisy(160, 160))                                              # several chunks
        self.assertGreater(kinds_image.chunk_count(len(data)), 1)
        aid = self.ask(data)
        got = []
        self.cache.loaded.connect(got.append)
        self.feed(stream(data))
        self.assertEqual(got, [aid])
        self.assertEqual(self.cache.state(aid), asset_cache.READY)
        self.assertEqual((self.cache.get_image(aid).width(), self.canvas.updates), (160, 1))
        self.assertTrue(os.path.exists(os.path.join(self.dir, aid + ".bin")))
        self.assertEqual((list(self.cache.outstanding), self.cache.incoming, len(self.app.net.sent)), ([], {}, 1))

    def test_bad_downloads_are_rejected(self):
        data = png_bytes(40, 40)
        bad_streams = {}
        s = stream(data)
        bad_streams["wrong sha in END"] = s[:-1] + [(protocol.IMG_END, {"asset_id": sha(data), "sha256": sha(b"x")})]
        bad_streams["no sha in END"] = s[:-1] + [(protocol.IMG_END, {"asset_id": sha(data)})]
        bad_streams["missing chunk"] = [m for m in stream(encode(noisy(160, 160)))[:-2]]
        bad_streams["not a picture"] = stream(b"this is no picture at all, just text")
        bad_streams["html as png"] = stream(b"<html>" + b"x" * 50)
        for name, messages in bad_streams.items():
            if name == "missing chunk":
                data2 = encode(noisy(160, 160))
                aid = self.ask(data2)
                messages = stream(data2)[:-2] + [stream(data2)[-1]]               # END arrives after a chunk was skipped
            else:
                aid = messages[0][1]["asset_id"]
                self.assertIsNone(self.cache.get_image(aid))
            self.feed(messages)
            self.assertEqual(self.cache.state(aid), asset_cache.FAILED, name)
            self.assertNotIn(aid, self.cache.images, name)
            self.assertFalse(os.path.exists(os.path.join(self.dir, aid + ".bin")), name)

    def test_chunk_errors_fail_the_download(self):
        data = encode(noisy(160, 160))
        n = kinds_image.chunk_count(len(data))
        good = stream(data)
        cases = {
            "out of order": [good[0], good[2]],
            "short chunk": [good[0], (protocol.IMG_CHUNK, dict(good[1][1], data=base64.b64encode(b"x").decode()))],
            "not base64": [good[0], (protocol.IMG_CHUNK, dict(good[1][1], data="!!!"))],
            "bool idx": [good[0], (protocol.IMG_CHUNK, dict(good[1][1], idx=False))],
            "no data": [good[0], (protocol.IMG_CHUNK, {"asset_id": sha(data), "idx": 0})],
        }
        self.assertGreater(n, 2)
        for name, messages in cases.items():
            self.cache.reset()
            aid = self.ask(data)
            self.feed(messages)
            self.assertEqual(self.cache.state(aid), asset_cache.FAILED, name)
            self.assertEqual(self.cache.incoming, {}, name)
            self.cache.failed.clear()

    def test_bad_begin_announcements(self):
        data = png_bytes()
        aid = self.ask(data)
        ok = stream(data)[0][1]
        for name, change in {"too big": {"size": kinds_image.MAX_IMAGE_BYTES + 1, "chunks": 214 + 1},
                             "zero": {"size": 0, "chunks": 0}, "bool size": {"size": True}, "bad mime": {"mime": "text/html"},
                             "wrong chunk count": {"chunks": 7}, "bool chunks": {"chunks": True}}.items():
            self.cache.failed.clear()
            self.cache.on_begin(dict(ok, **change))
            self.assertEqual(self.cache.incoming, {}, name)
            self.assertEqual(self.cache.state(aid), asset_cache.FAILED, name)

    def test_unsolicited_and_garbage_messages_are_ignored(self):
        data = png_bytes()
        self.feed(stream(data))                                                   # nobody asked for it
        self.assertEqual((self.cache.images, self.cache.incoming, self.cache.failed), ({}, {}, set()))
        for payload in ({}, {"asset_id": ["a"]}, {"asset_id": 5, "idx": 0}, {"asset_id": None}):
            self.cache.on_begin(payload)
            self.cache.on_chunk(payload)
            self.cache.on_end(payload)
            self.cache.on_error(payload)
        self.assertEqual(self.canvas.updates, 0)

    def test_error_for_asset_get_marks_the_oldest_outstanding_one(self):
        a, b = sha(b"first"), sha(b"second")
        self.cache.get_image(a)
        self.cache.get_image(b)
        self.cache.on_error({"code": "NOT_FOUND", "text": "no such picture", "ref_type": protocol.ASSET_GET})
        self.assertEqual((self.cache.state(a), self.cache.state(b)), (asset_cache.FAILED, asset_cache.LOADING))
        self.cache.on_error({"code": "FORBIDDEN", "text": "x", "ref_type": protocol.OP_ADD})        # not ours
        self.assertEqual(self.cache.state(b), asset_cache.LOADING)
        self.assertEqual(self.canvas.updates, 1)                                  # the placeholder text changed: repaint

    def test_failed_picture_is_retried_later_and_success_clears_the_failure(self):
        now = [0.0]
        self.cache.clock = lambda: now[0]
        data = png_bytes()
        aid = self.ask(data)
        self.cache.on_error({"ref_type": protocol.ASSET_GET})
        self.assertEqual(self.cache.state(aid), asset_cache.FAILED)
        now[0] += asset_cache.RETRY_S + 1
        self.assertIsNone(self.cache.get_image(aid))                              # asks again
        self.assertEqual(len(self.app.net.of_type(protocol.ASSET_GET)), 2)
        self.feed(stream(data))
        self.assertEqual(self.cache.state(aid), asset_cache.READY)
        self.assertNotIn(aid, self.cache.failed)

    def test_reconnect_clears_the_requests(self):
        aid = sha(b"unknown")
        self.cache.get_image(aid)
        self.cache.reset()
        self.cache.get_image(aid)
        self.assertEqual(len(self.app.net.sent), 2)                               # asked again on the new connection

    def test_retry_restarts_a_half_finished_download(self):
        now = [0.0]
        self.cache.clock = lambda: now[0]
        data = encode(noisy(160, 160))
        aid = self.ask(data)
        s = stream(data)
        self.feed(s[:2])                                                          # BEGIN and the first chunk, then the link dropped
        now[0] += asset_cache.RETRY_S + 1
        self.cache.get_image(aid)
        self.assertEqual(self.cache.incoming, {})
        self.feed(s)
        self.assertEqual(self.cache.state(aid), asset_cache.READY)

    def test_message_handlers_are_registered(self):
        for type_ in (protocol.JOIN_OK, protocol.IMG_BEGIN, protocol.IMG_CHUNK, protocol.IMG_END, protocol.ERROR):
            self.assertTrue(any(fn.__module__ == "client.asset_cache" for fn in registry.MESSAGE_HANDLERS[type_]), type_)

    def test_handlers_drive_the_module_cache(self):
        app = make_app()
        asset_cache._on_join_ok(app, {"payload": {}})
        self.assertIs(asset_cache.cache.app, app)
        asset_cache._on_img_begin(app, {"payload": None})                         # a message without payload must not crash
        asset_cache._on_img_chunk(app, {})
        asset_cache._on_img_end(app, {"payload": {}})
        asset_cache._on_error(app, {"payload": {"ref_type": "OP_ADD"}})


# ---------------------------------------------------------------- render_image

def rgb(image, x, y):
    """(r, g, b) of a pixel."""
    c = QColor(image.pixel(x, y))
    return c.red(), c.green(), c.blue()


class RenderTests(unittest.TestCase):
    """render_image.render_image and its use through render.render_board."""

    def setUp(self):
        """Swap the module cache for one with a private folder."""
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = asset_cache.cache
        asset_cache.cache = asset_cache.AssetCache(self._tmp.name)
        self.cache = asset_cache.cache
        self.app = make_app("editor", FakeCanvas())
        self.cache.app = self.app

    def tearDown(self):
        """Put the real cache back."""
        asset_cache.cache = self._saved
        self._tmp.cleanup()

    def paint(self, objects, ctx=None, size=(200, 120)):
        """Render objects onto a white QImage with render.render_board."""
        image = QImage(size[0], size[1], QImage.Format_RGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        try:
            render.render_board(painter, objects, ctx)
        finally:
            painter.end()
        return image

    def obj(self, aid, **over):
        """An image object at 10,10 size 100x50."""
        return dict({"id": "i1", "kind": "image", "x": 10, "y": 10, "w": 100, "h": 50, "asset_id": aid, "z": 1}, **over)

    def test_registered_for_image_objects(self):
        self.assertIs(registry.get_renderer("image"), render_image.render_image)

    def test_available_picture_is_drawn_into_its_box(self):
        data = png_bytes(20, 20, "#ff0000")
        self.cache.store(sha(data), data)
        image = self.paint([self.obj(sha(data))])
        self.assertEqual(rgb(image, 60, 35), (255, 0, 0))                          # inside the box
        self.assertEqual(rgb(image, 5, 5), (255, 255, 255))                        # outside
        self.assertEqual(rgb(image, 115, 35), (255, 255, 255))
        self.assertEqual(self.app.net.sent, [])

    def test_missing_picture_is_a_grey_placeholder_and_one_request(self):
        aid = sha(b"not here")
        for _ in range(3):
            image = self.paint([self.obj(aid)], {"canvas": self.app.canvas})
        self.assertEqual(rgb(image, 20, 40), render_image.PLACEHOLDER_FILL.getRgb()[:3])
        self.assertEqual(rgb(image, 5, 5), (255, 255, 255))
        self.assertEqual(self.app.net.of_type(protocol.ASSET_GET), [{"asset_id": aid}])    # painted three times, asked once

    def test_painting_again_after_the_download_shows_the_picture(self):
        data = png_bytes(20, 20, "#0000ff")
        aid = sha(data)
        placeholder = self.paint([self.obj(aid)])
        self.cache.app = self.app
        self.cache.on_begin(stream(data)[0][1])
        for t, p in stream(data)[1:]:
            (self.cache.on_chunk if t == protocol.IMG_CHUNK else self.cache.on_end)(p)
        self.assertEqual(rgb(placeholder, 60, 35), render_image.PLACEHOLDER_FILL.getRgb()[:3])
        self.assertEqual(rgb(self.paint([self.obj(aid)]), 60, 35), (0, 0, 255))
        self.assertGreaterEqual(self.app.canvas.updates, 1)                       # the canvas was told to repaint

    def test_export_context_requests_nothing(self):
        image = self.paint([self.obj(sha(b"not here"))], {"export": True})
        self.assertEqual(rgb(image, 20, 40), render_image.PLACEHOLDER_FILL.getRgb()[:3])
        self.assertEqual(self.app.net.sent, [])

    def test_failed_picture_still_gets_a_placeholder(self):
        aid = sha(b"gone")
        self.cache.get_image(aid)
        self.cache.on_error({"ref_type": protocol.ASSET_GET})
        image = self.paint([self.obj(aid)])
        self.assertEqual(rgb(image, 20, 40), render_image.PLACEHOLDER_FILL.getRgb()[:3])
        self.assertEqual(render_image.CAPTIONS[self.cache.state(aid)], "unavailable")
        self.assertEqual(render_image.CAPTIONS[asset_cache.LOADING], "loading")

    def test_damaged_objects_do_not_raise(self):
        damaged = [self.obj(None), self.obj(5), self.obj(["a"]), self.obj("../../x" + "a" * 57), self.obj(sha(b"x"), w="wide"),
                   self.obj(sha(b"x"), x=None), self.obj(sha(b"x"), w=float("nan")), self.obj(sha(b"x"), w=0, h=0),
                   self.obj(sha(b"x"), h=float("inf")), {"id": "i2", "kind": "image"}]
        self.paint(damaged)                                                        # render_board reports exceptions; none may occur
        target = QImage(10, 10, QImage.Format_RGB32)                              # the image must outlive the painter
        painter = QPainter(target)
        try:
            for o in damaged:
                render_image.render_image(painter, o, None)                       # called directly: an exception would show here
        finally:
            painter.end()

    def test_negative_size_is_normalized(self):
        data = png_bytes(20, 20, "#ff0000")
        self.cache.store(sha(data), data)
        image = self.paint([self.obj(sha(data), x=110, y=60, w=-100, h=-50)])
        self.assertEqual(rgb(image, 60, 35), (255, 0, 0))

    def test_picture_is_scaled_to_the_box_not_drawn_at_its_own_size(self):
        data = png_bytes(4, 4, "#00ff00")
        self.cache.store(sha(data), data)
        image = self.paint([self.obj(sha(data), x=0, y=0, w=200, h=120)])
        self.assertEqual({rgb(image, x, y) for x, y in ((5, 5), (100, 60), (195, 115))}, {(0, 255, 0)})


# ---------------------------------------------------------------- image_tool: preparing

class PrepareTests(unittest.TestCase):
    """prepare_image and encode_image."""

    def setUp(self):
        """A folder for picture files."""
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        """Remove it."""
        self._tmp.cleanup()

    def file(self, image, name="pic.png", fmt="PNG"):
        """Save a QImage as a file and return its path."""
        path = os.path.join(self.dir, name)
        assert image.save(path, fmt)
        return path

    def test_small_picture_stays_png_and_hash_is_the_asset_id(self):
        p = image_tool.prepare_image(self.file(solid(300, 200)))
        self.assertEqual((p.mime, p.w, p.h, p.name), ("image/png", 300, 200, "pic.png"))
        self.assertEqual(p.sha256, hashlib.sha256(p.data).hexdigest())
        self.assertTrue(kinds_image.is_asset_id(p.sha256))
        self.assertEqual(p.chunks, kinds_image.chunk_count(len(p.data)))
        self.assertTrue(kinds_image.mime_matches(p.mime, p.data))
        decoded = QImage.fromData(p.data)
        self.assertEqual((decoded.width(), decoded.height()), (300, 200))

    def test_longest_side_is_cut_to_1600_keeping_the_aspect(self):
        wide = image_tool.prepare_image(self.file(solid(3200, 1000)))
        self.assertEqual((wide.w, wide.h), (1600, 500))
        tall = image_tool.prepare_image(self.file(solid(900, 2700), "tall.png"))
        self.assertEqual((tall.w, tall.h), (533, 1600))
        exact = image_tool.prepare_image(self.file(solid(1600, 1200), "exact.png"))
        self.assertEqual((exact.w, exact.h), (1600, 1200))                         # not touched
        small = image_tool.prepare_image(self.file(solid(50, 40), "small.png"))
        self.assertEqual((small.w, small.h), (50, 40))                              # never enlarged

    def test_jpeg_and_bmp_sources_are_read(self):
        for name, fmt in (("a.jpg", "JPEG"), ("b.bmp", "BMP")):
            p = image_tool.prepare_image(self.file(solid(120, 80, "#336699"), name, fmt))
            self.assertEqual((p.w, p.h, p.name), (120, 80, name))
            self.assertIn(p.mime, kinds_image.MIMES)

    def test_big_photo_becomes_jpeg(self):
        p = image_tool.prepare_image(self.file(noisy(900, 900)))
        self.assertEqual(p.mime, "image/jpeg")
        self.assertLessEqual(len(p.data), kinds_image.MAX_IMAGE_BYTES)
        self.assertTrue(kinds_image.mime_matches("image/jpeg", p.data))
        self.assertEqual((p.w, p.h), (900, 900))

    def test_big_picture_with_transparency_stays_png(self):
        data, mime, image = image_tool.encode_image(noisy(700, 700, alpha=True))
        self.assertEqual(mime, "image/png")
        self.assertGreater(len(data), image_tool.PNG_KEEP_BYTES)
        self.assertLessEqual(len(data), kinds_image.MAX_IMAGE_BYTES)

    def test_picture_that_does_not_fit_is_shrunk_until_it_does(self):
        with mock.patch.object(kinds_image, "MAX_IMAGE_BYTES", 120_000), mock.patch.object(image_tool, "PNG_KEEP_BYTES", 50_000):
            data, mime, image = image_tool.encode_image(noisy(700, 700))
        self.assertLessEqual(len(data), 120_000)
        self.assertLess(image.width(), 700)
        self.assertEqual((image.width() / image.height()), 1.0)

    def test_hopeless_picture_is_an_error(self):
        with mock.patch.object(kinds_image, "MAX_IMAGE_BYTES", 300), mock.patch.object(image_tool, "PNG_KEEP_BYTES", 100):
            with self.assertRaises(image_tool.ImageError):
                image_tool.encode_image(noisy(200, 200))

    def test_unreadable_files(self):
        text = os.path.join(self.dir, "notes.png")
        with open(text, "w") as f:
            f.write("this is not a picture")
        for path in (text, os.path.join(self.dir, "missing.png"), self.dir):
            with self.assertRaises(image_tool.ImageError, msg=path):
                image_tool.prepare_image(path)

    def test_oversized_source_files_are_refused_before_decoding(self):
        path = self.file(solid(10, 10))
        with mock.patch.object(image_tool, "MAX_SOURCE_BYTES", 10):
            with self.assertRaises(image_tool.ImageError) as cm:
                image_tool.prepare_image(path)
        self.assertIn("larger", str(cm.exception))
        with mock.patch.object(image_tool, "MAX_SOURCE_PIXELS", 50):
            with self.assertRaises(image_tool.ImageError) as cm:
                image_tool.prepare_image(path)
        self.assertIn("too many pixels", str(cm.exception))

    def test_same_picture_gives_the_same_asset_id(self):
        path = self.file(solid(64, 64, "#123456"))
        self.assertEqual(image_tool.prepare_image(path).sha256, image_tool.prepare_image(path).sha256)


# ---------------------------------------------------------------- image_tool: placement

class PlacementTests(unittest.TestCase):
    """image_tool.placement."""

    def test_centre_of_the_view(self):
        app = make_app(canvas=FakeCanvas(QRectF(1000, 800, 1000, 600)))
        self.assertEqual(image_tool.placement(app, 400, 200), {"x": 1300, "y": 1000, "w": 400, "h": 200})

    def test_click_point_is_the_centre(self):
        app = make_app(canvas=FakeCanvas())
        self.assertEqual(image_tool.placement(app, 400, 200, QPointF(500, 400)), {"x": 300, "y": 300, "w": 400, "h": 200})

    def test_big_picture_is_made_to_fit_the_view(self):
        app = make_app(canvas=FakeCanvas(QRectF(1000, 800, 1000, 600)))
        r = image_tool.placement(app, 1600, 1200)                                   # view 1000 x 600: 80 % of the height
        self.assertEqual((r["w"], r["h"]), (640, 480))
        self.assertAlmostEqual(r["x"] + r["w"] / 2, 1500, delta=0.1)
        self.assertAlmostEqual(r["y"] + r["h"] / 2, 1100, delta=0.1)

    def test_aspect_ratio_is_kept(self):
        app = make_app(canvas=FakeCanvas(QRectF(0, 0, 300, 300)))
        r = image_tool.placement(app, 1000, 500)
        self.assertAlmostEqual(r["w"] / r["h"], 2.0, places=1)
        self.assertLessEqual(r["w"], 240.1)

    def test_snapped_to_the_grid_when_snapping_is_on(self):
        app = make_app(canvas=FakeCanvas(QRectF(1000, 800, 1000, 600), snap_on=True))
        r = image_tool.placement(app, 333, 111)
        self.assertEqual((r["x"] % config.GRID, r["y"] % config.GRID), (0, 0))
        self.assertEqual((r["w"], r["h"]), (333, 111))                              # sizes are not snapped (the aspect ratio stays)

    def test_stays_inside_the_board(self):
        app = make_app(canvas=FakeCanvas(QRectF(-500, -500, 1000, 1000)))           # view partly off the board
        r = image_tool.placement(app, 400, 400)
        self.assertGreaterEqual(r["x"], 0)
        self.assertGreaterEqual(r["y"], 0)
        corner = image_tool.placement(make_app(canvas=FakeCanvas()), 400, 300, QPointF(3990, 2990))
        self.assertEqual((corner["x"] + corner["w"], corner["y"] + corner["h"]), (config.BOARD_W, config.BOARD_H))
        origin = image_tool.placement(make_app(canvas=FakeCanvas()), 400, 300, QPointF(0, 0))
        self.assertEqual((origin["x"], origin["y"]), (0, 0))

    def test_view_far_off_the_board_falls_back_to_the_board(self):
        app = make_app(canvas=FakeCanvas(QRectF(9000, 9000, 100, 100)))
        r = image_tool.placement(app, 200, 100)
        self.assertEqual((r["x"], r["y"]), (config.BOARD_W / 2 - 100, config.BOARD_H / 2 - 50))

    def test_without_a_canvas_the_board_centre_is_used(self):
        r = image_tool.placement(make_app(canvas=None), 200, 100)
        self.assertEqual(r, {"x": 1900, "y": 1450, "w": 200, "h": 100})

    def test_values_have_at_most_one_decimal(self):
        app = make_app(canvas=FakeCanvas(QRectF(0, 0, 777, 555)))
        for key, value in image_tool.placement(app, 1600, 999).items():
            self.assertEqual(value, round(value, 1), key)


# ---------------------------------------------------------------- image_tool: the upload

class UploaderTests(unittest.TestCase):
    """ImageUploader against a recording send function."""

    def setUp(self):
        """An app with a canvas; the uploader sends through a list."""
        self.app = make_app("editor", FakeCanvas())
        self.sent = []
        self.progress, self.done = [], []
        self.data = encode(noisy(160, 160))                                          # a few chunks, so batching shows
        self.prepared = image_tool.Prepared(self.data, "image/png", 120, 120, "n.png", sha(self.data))

    def make(self, send=None, **kw):
        """An uploader for self.prepared that records into self.sent."""
        def record(type_, payload):
            self.sent.append((type_, payload))
            return True
        return image_tool.ImageUploader(self.app, self.prepared, kw.pop("anchor", None),
                                        on_progress=lambda s, t: self.progress.append((s, t)),
                                        on_done=lambda ok, text: self.done.append((ok, text)),
                                        send=send or record, interval_ms=1, **kw)

    def finish_sending(self, up):
        """Run the timer until all chunks and IMG_END are out."""
        self.assertTrue(run_until(lambda: up.state != "sending"))

    def test_message_sequence_and_content(self):
        up = self.make(batch=2)
        up.start()
        self.assertEqual([t for t, _ in self.sent], [protocol.IMG_BEGIN])           # BEGIN at once, chunks come from the timer
        self.finish_sending(up)
        n = kinds_image.chunk_count(len(self.data))
        self.assertGreater(n, 2)
        self.assertEqual([t for t, _ in self.sent], [protocol.IMG_BEGIN] + [protocol.IMG_CHUNK] * n + [protocol.IMG_END])
        begin = self.sent[0][1]
        self.assertEqual(begin, {"asset_id": sha(self.data), "name": "n.png", "mime": "image/png", "size": len(self.data),
                                 "chunks": n, "w": 120, "h": 120})
        chunks = [p for t, p in self.sent if t == protocol.IMG_CHUNK]
        self.assertEqual([c["idx"] for c in chunks], list(range(n)))
        raw = [base64.b64decode(c["data"]) for c in chunks]
        self.assertTrue(all(len(r) == CHUNK for r in raw[:-1]))
        self.assertLessEqual(len(raw[-1]), CHUNK)
        self.assertEqual(b"".join(raw), self.data)
        self.assertEqual(self.sent[-1][1], {"asset_id": sha(self.data), "sha256": sha(self.data)})
        self.assertEqual(up.state, "waiting")                                       # no OP_ADD before IMG_OK
        self.assertEqual(self.app.net.sent, [])

    def test_chunks_are_sent_in_small_groups_with_progress(self):
        up = self.make(batch=2)
        up.start()
        self.finish_sending(up)
        n = kinds_image.chunk_count(len(self.data))
        self.assertEqual(self.progress[0], (2, n + 1))
        self.assertTrue(all(b - a <= 2 for (a, _), (b, _) in zip(self.progress, self.progress[1:])))   # never more than a batch
        self.assertEqual(self.progress[-1][0], n)

    def test_nothing_but_begin_is_sent_inside_start(self):
        up = self.make()
        up.start()
        self.assertEqual(len(self.sent), 1)
        up.cancel()

    def test_ok_adds_one_image_object_through_send_op(self):
        up = self.make(anchor=QPointF(2000, 1500))
        up.start()
        self.finish_sending(up)
        up.on_ok()
        (type_, payload, op_id), = self.app.net.sent
        self.assertEqual(type_, protocol.OP_ADD)
        self.assertTrue(op_id.startswith("hana-"))
        obj = payload["obj"]
        self.assertEqual(set(obj), {"id", "kind", "x", "y", "w", "h", "asset_id"})
        self.assertEqual((obj["kind"], obj["asset_id"], obj["w"], obj["h"]), ("image", sha(self.data), 120, 120))
        self.assertEqual((obj["x"] + obj["w"] / 2, obj["y"] + obj["h"] / 2), (2000, 1500))
        self.assertTrue(obj["id"].startswith("hana-"))
        self.assertEqual((up.state, self.done, self.progress[-1]), ("done", [(True, "Image added")], (up.total, up.total)))
        up.on_ok()                                                                  # a second IMG_OK changes nothing
        self.assertEqual(len(self.app.net.sent), 1)

    def test_default_position_is_the_view_centre(self):
        up = self.make()
        up.start()
        self.finish_sending(up)
        up.on_ok()
        obj = self.app.net.sent[0][1]["obj"]
        self.assertEqual((obj["x"] + obj["w"] / 2, obj["y"] + obj["h"] / 2), (1500, 1100))     # centre of FakeCanvas' view

    def test_object_passes_the_servers_validation(self):
        from common import models
        up = self.make()
        up.start()
        self.finish_sending(up)
        up.on_ok()
        self.assertEqual(models.validate_obj(self.app.net.sent[0][1]["obj"]), (True, ""))

    def test_not_connected_at_start(self):
        up = self.make(send=lambda t, p: False)
        up.start()
        self.assertEqual((up.state, self.done), ("failed", [(False, "Not connected")]))

    def test_connection_lost_in_the_middle(self):
        calls = []

        def flaky(type_, payload):
            calls.append(type_)
            return len(calls) < 3                                                   # BEGIN and one chunk go out, then it drops
        up = self.make(send=flaky, batch=1)
        up.start()
        self.assertTrue(run_until(lambda: up.state == "failed"))
        self.assertEqual(self.done, [(False, "Connection lost during the upload")])
        self.assertEqual(len(calls), 3)                                             # it stopped sending

    def test_server_error_stops_the_upload_at_once(self):
        up = self.make(batch=1)
        up.start()
        run_until(lambda: len(self.sent) >= 3)
        up.on_error("TOO_LARGE: nope")
        count = len(self.sent)
        run_until(lambda: False, 0.1)
        self.assertEqual((len(self.sent), up.state, self.done), (count, "failed", [(False, "TOO_LARGE: nope")]))

    def test_cancel_stops_and_a_late_ok_is_ignored(self):
        up = self.make(batch=1)
        up.start()
        run_until(lambda: len(self.sent) >= 3)
        up.cancel()
        count = len(self.sent)
        run_until(lambda: False, 0.1)
        up.on_ok()
        self.assertEqual((len(self.sent), up.state, self.app.net.sent), (count, "cancelled", []))
        self.assertEqual(self.done, [(False, "Upload cancelled")])

    def test_client_output_is_accepted_by_the_real_server_store(self):
        """Interop: what the uploader sends passes every check of server/assets.py."""
        class Room:
            lock = __import__("threading").RLock()
        store = AssetStore(Room())
        up = self.make()
        up.start()
        self.finish_sending(up)
        for type_, payload in self.sent:
            if type_ == protocol.IMG_BEGIN:
                store.begin("hana", payload)
            elif type_ == protocol.IMG_CHUNK:
                store.chunk("hana", payload)
            else:
                meta, data, claimed = store.finish("hana", payload)
                self.assertEqual((data, claimed, meta["asset_id"]), (self.data, sha(self.data), sha(self.data)))
                self.assertTrue(kinds_image.mime_matches(meta["mime"], data))
                store.add(meta, data)
        self.assertEqual(store.get(sha(self.data))["data"], self.data)

    def test_the_server_stream_is_accepted_by_the_client_cache(self):
        """Interop the other way: what the server streams for ASSET_GET is what the cache reassembles."""
        class Room:
            lock = __import__("threading").RLock()
        store = AssetStore(Room())
        meta = {"asset_id": sha(self.data), "name": "n.png", "mime": "image/png", "size": len(self.data), "w": 120, "h": 120,
                "by": "x", "ts": 0}
        store.add(meta, self.data)
        cache = asset_cache.AssetCache(tempfile.mkdtemp())
        cache.app = make_app()
        self.assertIsNone(cache.get_image(sha(self.data)))
        entry = store.get(sha(self.data))
        n = kinds_image.chunk_count(len(entry["data"]))
        cache.on_begin({"asset_id": sha(self.data), "name": "n.png", "mime": "image/png", "size": len(self.data), "chunks": n,
                        "w": 120, "h": 120})
        for i in range(n):
            cache.on_chunk({"asset_id": sha(self.data), "idx": i,
                            "data": base64.b64encode(self.data[i * CHUNK:(i + 1) * CHUNK]).decode()})
        cache.on_end({"asset_id": sha(self.data), "sha256": sha(self.data)})
        self.assertEqual(cache.state(sha(self.data)), asset_cache.READY)


class EntryPointTests(unittest.TestCase):
    """start_upload, the handlers, the menu entry and the tool."""

    def setUp(self):
        """A picture file, a private cache and no running upload."""
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "cat.png")
        solid(200, 100, "#00aa00").save(self.path)
        self._saved_cache, self._saved_active = asset_cache.cache, image_tool._active
        asset_cache.cache = asset_cache.AssetCache(os.path.join(self._tmp.name, "cache"))
        image_tool._active = None
        self.app = make_app("editor", FakeCanvas())

    def tearDown(self):
        """Restore the module state."""
        if image_tool._active is not None:
            image_tool._active.cancel()
        image_tool._active, asset_cache.cache = self._saved_active, self._saved_cache
        self._tmp.cleanup()

    def test_registration(self):
        self.assertIs(registry.TOOLS["image"], image_tool.ImageTool)
        tool = image_tool.ImageTool
        self.assertEqual((tool.id, tool.label, tool.shortcut, tool.group), ("image", "Image", "I", "media"))
        entry = [e for e in registry.MENU_ACTIONS if e["menu"] == "Insert"][0]
        self.assertEqual((entry["label"], entry["fn"]), ("Image...", image_tool.insert_image))
        for type_ in (protocol.IMG_OK, protocol.ERROR):
            self.assertIn(image_tool._on_img_ok if type_ == protocol.IMG_OK else image_tool._on_upload_error,
                          registry.MESSAGE_HANDLERS[type_])

    def test_start_upload_begins_at_once_and_shows_my_picture_without_a_download(self):
        up = image_tool.start_upload(self.app, self.path, show_progress=False)
        self.assertIsNotNone(up)
        (begin,) = self.app.net.of_type(protocol.IMG_BEGIN)
        self.assertEqual((begin["name"], begin["mime"], begin["w"], begin["h"]), ("cat.png", "image/png", 200, 100))
        self.assertFalse(asset_cache.cache.get_image(begin["asset_id"]).isNull())    # cached before the server answered
        self.assertEqual(self.app.net.of_type(protocol.ASSET_GET), [])

    def test_full_round_trip_with_the_handlers(self):
        up = image_tool.start_upload(self.app, self.path, show_progress=False)
        self.assertTrue(run_until(lambda: up.state == "waiting"))
        image_tool._on_img_ok(self.app, {"payload": {"asset_id": "f" * 64}})          # somebody else's picture: ignored
        self.assertEqual(self.app.net.of_type(protocol.OP_ADD), [])
        image_tool._on_img_ok(self.app, {"payload": {"asset_id": up.asset_id}})
        (obj,) = [p["obj"] for p in self.app.net.of_type(protocol.OP_ADD)]
        self.assertEqual((obj["kind"], obj["asset_id"]), ("image", up.asset_id))
        self.assertEqual(self.app.toasts[-1], "Image added")

    def test_error_handler_only_reacts_to_upload_errors(self):
        up = image_tool.start_upload(self.app, self.path, show_progress=False)
        image_tool._on_upload_error(self.app, {"payload": {"code": "FORBIDDEN", "text": "no", "ref_type": protocol.OP_ADD}})
        self.assertTrue(up.active)
        image_tool._on_upload_error(self.app, {"payload": {"code": "TOO_LARGE", "text": "too big", "ref_type": protocol.IMG_BEGIN}})
        self.assertEqual(up.state, "failed")
        self.assertEqual(self.app.toasts[-1], "Image not added: TOO_LARGE: too big")
        image_tool._on_upload_error(self.app, {"payload": None})                        # no payload: no crash
        image_tool._on_img_ok(self.app, {})

    def test_refusals_toast_and_send_nothing(self):
        viewer = make_app("viewer")
        self.assertIsNone(image_tool.start_upload(viewer, self.path, show_progress=False))
        self.assertEqual((viewer.toasts, viewer.net.sent), (["Viewers cannot add images"], []))
        offline = make_app("host")
        offline.net.is_connected = False
        self.assertIsNone(image_tool.start_upload(offline, self.path, show_progress=False))
        self.assertEqual(offline.toasts, ["Not connected"])
        unjoined = make_app(None)
        self.assertIsNone(image_tool.start_upload(unjoined, self.path, show_progress=False))
        bad = os.path.join(self._tmp.name, "notes.png")
        with open(bad, "w") as f:
            f.write("not a picture")
        self.assertIsNone(image_tool.start_upload(self.app, bad, show_progress=False))
        self.assertTrue(self.app.toasts[-1].startswith("Cannot use this picture"))
        self.assertEqual(self.app.net.sent, [])

    def test_only_one_upload_at_a_time(self):
        image_tool.start_upload(self.app, self.path, show_progress=False)
        self.app.toasts.clear()
        self.assertIsNone(image_tool.start_upload(self.app, self.path, show_progress=False))
        self.assertEqual(self.app.toasts, ["An image is still being uploaded"])

    def test_insert_menu_uses_the_view_centre(self):
        with mock.patch.object(image_tool, "pick_file", return_value=self.path):
            image_tool.insert_image(self.app)
        up = image_tool._active
        self.assertIsNotNone(up)
        self.assertIsNone(up.anchor)
        with mock.patch.object(image_tool, "pick_file", return_value=None):         # cancelled dialog
            image_tool._active.cancel()
            before = len(self.app.net.sent)
            image_tool.insert_image(self.app)
        self.assertEqual(len(self.app.net.sent), before)

    def test_insert_menu_for_a_viewer_does_not_even_open_the_dialog(self):
        viewer = make_app("viewer")
        with mock.patch.object(image_tool, "pick_file") as pick:
            image_tool.insert_image(viewer)
        pick.assert_not_called()

    def test_tool_click_places_the_picture_at_the_click(self):
        class Ev:
            def __init__(self, button=Qt.LeftButton):
                self._b = button

            def button(self):
                return self._b
        canvas = FakeCanvas()
        canvas.app = self.app
        tool = image_tool.ImageTool()
        with mock.patch.object(image_tool, "pick_file", return_value=self.path):
            tool.on_press(canvas, QPointF(300, 300), Ev())
            tool.on_release(canvas, QPointF(300, 300), Ev(Qt.RightButton))               # right button: nothing
            self.assertIsNone(image_tool._active)
            tool.on_release(canvas, QPointF(700, 500), Ev())
        up = image_tool._active
        self.assertEqual((up.anchor.x(), up.anchor.y()), (700, 500))
        self.assertEqual(tool.cursor(), Qt.PointingHandCursor)
        tool.on_move(canvas, QPointF(1, 1), Ev())
        tool.cancel(canvas)

    def test_tool_for_a_viewer_and_with_a_cancelled_dialog(self):
        canvas = FakeCanvas()
        viewer = make_app("viewer")
        canvas.app = viewer
        with mock.patch.object(image_tool, "pick_file") as pick:
            image_tool.ImageTool().on_release(canvas, QPointF(5, 5), type("E", (), {"button": lambda s: Qt.LeftButton})())
        pick.assert_not_called()
        canvas.app = self.app
        with mock.patch.object(image_tool, "pick_file", return_value=None):
            image_tool.ImageTool().on_release(canvas, QPointF(5, 5), type("E", (), {"button": lambda s: Qt.LeftButton})())
        self.assertIsNone(image_tool._active)

    def test_file_dialog_is_asked_for_pictures(self):
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("", "")) as dlg:
            self.assertIsNone(image_tool.pick_file(self.app))
        self.assertIn("*.png", dlg.call_args[0][3])
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(self.path, "")):
            self.assertEqual(image_tool.pick_file(self.app), self.path)


if __name__ == "__main__":
    unittest.main()
