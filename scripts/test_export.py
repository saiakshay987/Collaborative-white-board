"""test_export.py: Tests for client/export.py (JSON save/load, PNG and PDF export).

Run from the project root:  python scripts/test_export.py

No window and no network: the file format and the object preparation are plain
functions, BoardLoader runs on a Qt event loop with a fake app and a fake send
function that only records what would have been sent, and the PNG/PDF export
paints into real QImage / QPdfWriter targets in a temporary folder. Qt runs on
its offscreen platform, which has NO FONTS: text paints nothing there, so the
pixel checks only use shapes (the text renderers have their own tests).
"""
import copy
import json
import os
import re
import sys
import tempfile
import unittest
import zlib
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QRectF, QSizeF, QTimer  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication, QFileDialog  # noqa: E402

from common import config, protocol  # noqa: E402
from client import export, registry  # noqa: E402
from client import render_draw, render_text  # noqa: E402,F401  (register the renderers of the drawing kinds)

APP = QApplication.instance() or QApplication([])


def rect(oid, z=None, owner=None):
    """A valid rect object, optionally with the server-set fields."""
    obj = {'id': oid, 'kind': 'rect', 'x': 0, 'y': 0, 'w': 40, 'h': 40, 'color': '#ff0000', 'width': 2, 'fill': None}
    if z is not None:
        obj.update(z=z, owner=owner)
    return obj


class FakeNet:
    """Stands in for NetClient: only is_connected matters here."""

    def __init__(self):
        """Start connected."""
        self.is_connected = True


class FakeMe:
    """Stands in for AppState.me."""

    def __init__(self, role='host'):
        """Name and role of the local user."""
        self.username, self.role, self.room = 'hana', role, 'ABCDEF'


class FakeApp:
    """Stands in for AppState: net, me, toast."""

    def __init__(self, role='host'):
        """A connected host (or other role) that records its toasts."""
        self.net, self.me, self.toasts, self.board, self.window = FakeNet(), FakeMe(role), [], None, None

    def toast(self, text):
        """Record a notification."""
        self.toasts.append(text)


def run_loader(loader, timeout_ms=5000):
    """Run the Qt event loop until the loader is done (or the timeout)."""
    QTimer.singleShot(timeout_ms, APP.quit)
    loader._on_done_user = loader._on_done
    loader._on_done = lambda sent, total: (loader._on_done_user(sent, total) if loader._on_done_user else None, APP.quit())
    loader.start()
    if not loader.finished:
        APP.exec_()


class FormatTests(unittest.TestCase):
    """build_document / parse_document."""

    def test_round_trip_in_paint_order(self):
        board = {o['id']: o for o in (rect('b', z=2, owner='x'), rect('a', z=1, owner='y'), rect('c'))}
        text = export.build_document(board)
        doc = json.loads(text)
        self.assertEqual((doc['format'], doc['version']), ('whiteboard-board', 1))
        self.assertEqual([o['id'] for o in doc['objects']], ['a', 'b', 'c'])   # z order, no-z last
        objects, skipped = export.parse_document(text)
        self.assertEqual((skipped, [o['id'] for o in objects]), (0, ['a', 'b', 'c']))

    def test_empty_board_round_trip(self):
        self.assertEqual(export.parse_document(export.build_document({})), ([], 0))

    def test_invalid_objects_are_skipped_and_counted(self):
        bad_kind = dict(rect('k'), kind='hologram')
        bad_field = dict(rect('f'), w='wide')
        text = json.dumps({'format': 'whiteboard-board', 'version': 1,
                           'objects': [rect('ok'), bad_kind, bad_field, 'junk', {'id': 'x'}]})
        objects, skipped = export.parse_document(text)
        self.assertEqual(([o['id'] for o in objects], skipped), (['ok'], 4))

    def test_not_a_board_file(self):
        for text in ('not json', '[]', '{}', '{"format":"other","version":1,"objects":[]}',
                     '{"format":"whiteboard-board","version":2,"objects":[]}',
                     '{"format":"whiteboard-board","version":1,"objects":{}}',
                     '{"format":"whiteboard-board","version":1}'):
            with self.assertRaises(ValueError, msg=text):
                export.parse_document(text)


class FreshObjectTests(unittest.TestCase):
    """fresh_objects: new ids, no owner, no z, input untouched."""

    def test_fresh_ids_and_no_server_fields(self):
        original = [rect('hana-1', z=7, owner='eve'), rect('hana-2', z=8, owner='eve')]
        before = copy.deepcopy(original)
        # the counter of new_op_id restarts with every program run, so ids that are
        # already on the board (here the ones of the file itself) must be skipped
        fresh = export.fresh_objects(original, 'hana', taken={'hana-1', 'hana-2'})
        self.assertEqual(original, before)
        ids = [o['id'] for o in fresh]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(i.startswith('hana-') for i in ids))
        self.assertTrue(set(ids).isdisjoint({'hana-1', 'hana-2'}))
        for o in fresh:
            self.assertNotIn('owner', o)
            self.assertNotIn('z', o)
            self.assertEqual((o['kind'], o['x'], o['w']), ('rect', 0, 40))


class LoaderTests(unittest.TestCase):
    """BoardLoader sends in small groups through the send function."""

    def make(self, n, **kw):
        """A loader for n objects with a recording send function; returns (loader, app, sent, groups)."""
        app, sent, groups = FakeApp(), [], []

        def send(app_, type_, payload):
            sent.append((type_, payload))
            return 'op'

        loader = export.BoardLoader(app, [rect('o-%d' % i) for i in range(n)], send=send,
                                    interval_ms=5, on_progress=lambda s, t: groups.append(s), **kw)
        return loader, app, sent, groups

    def test_sends_every_object_as_op_add_in_small_groups(self):
        loader, app, sent, groups = self.make(23, batch=10)
        run_loader(loader)
        self.assertTrue(loader.finished)
        self.assertEqual([t for t, _ in sent], [protocol.OP_ADD] * 23)
        self.assertEqual([p['obj']['id'] for _, p in sent], ['o-%d' % i for i in range(23)])   # file order kept
        self.assertEqual(groups, [10, 20, 23])           # never more than `batch` per tick

    def test_nothing_is_sent_inside_start(self):
        loader, app, sent, groups = self.make(5, batch=10)
        loader.start()
        self.assertEqual(sent, [])                       # the first group waits for the first tick
        loader._timer.stop()

    def test_empty_list_finishes_at_once(self):
        loader, app, sent, groups = self.make(0)
        results = []
        loader._on_done = lambda s, t: results.append((s, t))
        loader.start()
        self.assertEqual((loader.finished, results), (True, [(0, 0)]))

    def test_stops_when_the_connection_is_lost(self):
        loader, app, sent, groups = self.make(30, batch=10)
        results = []
        loader._on_done = lambda s, t: (results.append((s, t)), APP.quit())

        def drop_after_first_group(sent_count, total):
            groups.append(sent_count)
            app.net.is_connected = False

        loader._on_progress = drop_after_first_group
        QTimer.singleShot(3000, APP.quit)
        loader.start()
        APP.exec_()
        self.assertEqual(len(sent), 10)
        self.assertEqual(results, [(10, 30)])

    def test_default_send_is_ops_send_op(self):
        from client import ops
        loader = export.BoardLoader(FakeApp(), [])
        self.assertIs(loader._send, ops.send_op)


class MenuTests(unittest.TestCase):
    """Registration and the host-only rule."""

    def test_both_actions_are_registered_in_the_file_menu(self):
        entries = {e['label']: e for e in registry.MENU_ACTIONS if e['menu'] == 'File'}
        self.assertIs(entries['Save board as JSON']['fn'], export.save_board)
        self.assertIs(entries['Load board from JSON']['fn'], export.load_board)

    def test_only_the_host_can_load(self):
        for role in ('editor', 'viewer', None):
            app = FakeApp(role)
            export.load_board(app)
            self.assertEqual(app.toasts, ['Only the host can load a board'], role)

    def test_load_needs_a_connection(self):
        app = FakeApp('host')
        app.net.is_connected = False
        export.load_board(app)
        self.assertEqual(app.toasts, ['Not connected'])

    def test_saving_an_empty_board_is_refused(self):
        app = FakeApp('host')
        export.save_board(app)
        self.assertEqual(app.toasts, ['The board is empty: nothing to save'])


def box(oid, x, y, w, h, color='#ff0000', width=2, fill=None, z=None):
    """A rect object at a given place (z decides the paint order)."""
    return {'id': oid, 'kind': 'rect', 'x': x, 'y': y, 'w': w, 'h': h, 'color': color, 'width': width,
            'fill': fill, 'z': z if z is not None else 1}


def picture(oid='img-1', x=100, y=100, w=200, h=100, z=1, **extra):
    """An image object (asset_id only; the image feature itself is not part of this module)."""
    return dict({'id': oid, 'kind': 'image', 'x': x, 'y': y, 'w': w, 'h': h, 'asset_id': 'a1', 'z': z}, **extra)


def rgb(image, x, y):
    """The (r, g, b) of one pixel."""
    c = QColor(image.pixel(x, y))
    return c.red(), c.green(), c.blue()


class ExportRectTests(unittest.TestCase):
    """export_rect: bounding box of all objects plus a 40 unit margin."""

    def test_empty_board_is_the_whole_board(self):
        self.assertEqual(export.export_rect({}), QRectF(0, 0, config.BOARD_W, config.BOARD_H))
        self.assertEqual(export.export_rect([]), QRectF(0, 0, config.BOARD_W, config.BOARD_H))

    def test_one_shape_plus_margin(self):
        # box 100,200 50x40 with pen width 2: the pen adds 2 on each side, then the 40 margin
        r = export.export_rect([box('a', 100, 200, 50, 40, width=2)])
        self.assertEqual((r.x(), r.y(), r.width(), r.height()), (58, 158, 134, 124))

    def test_union_of_kinds(self):
        objs = [box('a', 100, 100, 50, 50, width=0),
                {'id': 's', 'kind': 'stroke', 'points': [[400, 300], [500, 350]], 'width': 0, 'color': '#000000',
                 'tool': 'pen', 'alpha': 1},
                {'id': 'l', 'kind': 'line', 'x1': 90, 'y1': 600, 'x2': 120, 'y2': 580, 'width': 0, 'color': '#000000'}]
        r = export.export_rect({o['id']: o for o in objs})            # a dict, like app.board.objects
        self.assertEqual((r.left(), r.top(), r.right(), r.bottom()), (50, 60, 540, 640))

    def test_unusable_objects_are_ignored(self):
        good = box('a', 100, 100, 50, 50, width=0)
        junk = [{'id': 'u', 'kind': 'mystery'}, {'id': 'n', 'kind': 'rect', 'x': float('nan'), 'y': 0, 'w': 1, 'h': 1},
                'not a dict', {'id': 'i', 'kind': 'rect', 'x': float('inf'), 'y': 0, 'w': 1, 'h': 1}]
        r = export.export_rect([good] + junk)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()), (60, 60, 130, 130))
        self.assertEqual(export.export_rect(junk), QRectF(0, 0, config.BOARD_W, config.BOARD_H))   # nothing usable

    def test_images_count_too(self):
        r = export.export_rect([picture(x=10, y=20, w=100, h=50)])
        self.assertEqual((r.x(), r.y(), r.width(), r.height()), (-30, -20, 180, 130))


class PngTests(unittest.TestCase):
    """render_png / write_png."""

    def setUp(self):
        """A red rectangle with a known place; the image is cropped to it plus the margin."""
        self.objs = [box('a', 100, 100, 100, 60, color='#ff0000', width=2, fill='#ff0000')]
        self._saved = registry.RENDERERS.pop('image', None)

    def tearDown(self):
        """Put back an 'image' renderer if the test environment had one."""
        registry.RENDERERS.pop('image', None)
        if self._saved is not None:
            registry.RENDERERS['image'] = self._saved

    def test_size_follows_the_scale(self):
        one, two = export.render_png(self.objs, 1), export.render_png(self.objs, 2)
        self.assertEqual((one.width(), one.height()), (184, 144))      # 100x60 + 2 pen + 2*40 margin
        self.assertEqual((two.width(), two.height()), (368, 288))

    def test_white_background_and_content_in_place(self):
        for scale in (1, 2):
            image = export.render_png(self.objs, scale)
            self.assertEqual(rgb(image, 2 * scale, 2 * scale), (255, 255, 255))                # margin
            self.assertEqual(rgb(image, image.width() - 3, image.height() - 3), (255, 255, 255))
            self.assertEqual(rgb(image, image.width() // 2, image.height() // 2), (255, 0, 0))   # the rectangle
            self.assertEqual(rgb(image, 38 * scale, image.height() // 2), (255, 255, 255))      # just left of it
            self.assertEqual(rgb(image, 42 * scale, image.height() // 2), (255, 0, 0))          # just inside

    def test_empty_board_is_the_whole_white_board(self):
        image = export.render_png({}, 1)
        self.assertEqual((image.width(), image.height()), (config.BOARD_W, config.BOARD_H))
        self.assertEqual({rgb(image, x, y) for x, y in ((0, 0), (2000, 1500), (3999, 2999))}, {(255, 255, 255)})

    def test_objects_paint_in_z_order(self):
        objs = [box('top', 100, 100, 100, 60, color='#0000ff', fill='#0000ff', z=2),
                box('bottom', 100, 100, 100, 60, color='#ff0000', fill='#ff0000', z=1)]
        image = export.render_png(objs, 1)
        self.assertEqual(rgb(image, image.width() // 2, image.height() // 2), (0, 0, 255))

    def test_too_large_picture_is_refused(self):
        with mock.patch.object(export, 'PNG_MAX_PIXELS', 1000):
            with self.assertRaises(export.ExportError) as cm:
                export.render_png(self.objs, 1)
        self.assertIn('try 1x', str(cm.exception))
        with self.assertRaises(export.ExportError):
            export.render_png([], 2 + 6)                                   # 32000 x 24000 pixels

    def test_write_png_creates_a_readable_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'out.png')
            self.assertEqual(export.write_png(path, self.objs, 2), (368, 288))
            loaded = QImage(path)
            self.assertEqual((loaded.width(), loaded.height()), (368, 288))
            with open(path, 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')
            with self.assertRaises(export.ExportError):
                export.write_png(os.path.join(d, 'no-such-folder', 'x.png'), self.objs, 1)

    # -- images that are not available ----------------------------------------------

    def test_image_without_renderer_is_a_placeholder(self):
        image = export.render_png([picture(x=100, y=100, w=200, h=100)], 1)
        self.assertEqual(rgb(image, 40 + 60, 40 + 10), export.PLACEHOLDER_FILL.getRgb()[:3])   # inside the box
        self.assertEqual(rgb(image, 10, 10), (255, 255, 255))                                  # margin stays white

    def test_failing_image_renderer_is_a_placeholder(self):
        def broken(painter, obj, ctx):
            painter.fillRect(QRectF(0, 0, 500, 500), QColor('#00ff00'))      # paints something, then fails
            raise RuntimeError('still loading')
        registry.RENDERERS['image'] = broken
        image = export.render_png([picture(x=100, y=100, w=200, h=100)], 1)
        self.assertEqual(rgb(image, 100, 50), export.PLACEHOLDER_FILL.getRgb()[:3])

    def test_working_image_renderer_is_used_and_told_it_is_an_export(self):
        seen = []

        def good(painter, obj, ctx):
            seen.append((obj['id'], ctx.get('export'), ctx.get('zoom')))
            painter.fillRect(QRectF(obj['x'], obj['y'], obj['w'], obj['h']), QColor('#0000ff'))
        registry.RENDERERS['image'] = good
        image = export.render_png([picture(x=100, y=100, w=200, h=100)], 2)
        self.assertEqual(seen, [('img-1', True, 2)])
        self.assertEqual(rgb(image, 2 * (40 + 100), 2 * (40 + 50)), (0, 0, 255))

    def test_damaged_image_objects_do_not_crash(self):
        objs = [picture('i1', w='wide'), picture('i2', x=None), {'id': 'i3', 'kind': 'image'},
                picture('i4', w=0, h=0), picture('i5', w=float('nan')), box('ok', 0, 0, 40, 40)]
        image = export.render_png(objs, 1)                                    # must simply work
        self.assertFalse(image.isNull())

    def test_stand_in_keeps_the_z_order(self):
        objs = [box('under', 100, 100, 200, 100, color='#ff0000', fill='#ff0000', z=1),
                picture('img', 100, 100, 200, 100, z=2),
                box('over', 150, 130, 40, 40, color='#0000ff', fill='#0000ff', z=3)]
        image = export.render_png(objs, 1)
        self.assertEqual(rgb(image, 40 + 70, 40 + 50), (0, 0, 255))           # 'over'
        self.assertEqual(rgb(image, 40 + 150, 40 + 20), export.PLACEHOLDER_FILL.getRgb()[:3])   # placeholder hides 'under'

    def test_export_objects_swaps_only_images(self):
        rect_obj, img = box('r', 0, 0, 1, 1), picture()
        out = export.export_objects({'r': rect_obj, 'i': img})
        self.assertIs(out[0], rect_obj)
        self.assertEqual((out[1]['kind'], out[1]['inner'], out[1]['z']), (export.EXPORT_IMAGE_KIND, img, 1))
        self.assertIsNotNone(registry.get_renderer(export.EXPORT_IMAGE_KIND))


class PdfTests(unittest.TestCase):
    """fit_into, footer_text, paint_page and write_pdf."""

    def test_fit_into_keeps_the_aspect_ratio_and_centres(self):
        scale, dx, dy = export.fit_into(QSizeF(400, 200), QSizeF(1000, 1000))
        self.assertEqual((scale, dx, dy), (2.5, 0.0, 250.0))              # width limits, centred vertically
        scale, dx, dy = export.fit_into(QSizeF(100, 400), QSizeF(1000, 800))
        self.assertEqual((scale, dx, dy), (2.0, 400.0, 0.0))              # height limits, centred horizontally
        self.assertEqual(export.fit_into(QSizeF(4000, 3000), QSizeF(4000, 3000)), (1.0, 0.0, 0.0))
        self.assertEqual(export.fit_into(QSizeF(0, 10), QSizeF(5, 5)), (1.0, 0.0, 0.0))   # no division by zero

    def test_footer_text(self):
        when = 1794319700.0
        text = export.footer_text('K7QX2M', when)
        self.assertIn('Room K7QX2M', text)
        self.assertIn('exported from LAN Whiteboard', text)
        self.assertRegex(text, r'\d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d')
        self.assertIn('Room -', export.footer_text(None, when))

    def test_page_layout_centres_the_board_above_the_footer(self):
        """Paint the page into a QImage with the PDF's geometry (75 dpi to keep it quick) and measure the ink."""
        dpi = 75
        width, height = round(277 * dpi / 25.4), round(190 * dpi / 25.4)
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(Qt_white())
        painter = QPainter(image)
        try:
            export.paint_page(painter, QRectF(0, 0, width, height),
                              [box('a', 100, 100, 400, 100, color='#000000', width=2, fill='#000000')], 'K7QX2M', 0, dpi)
        finally:
            painter.end()
        xs, ys = [], []
        footer_top = height - export.PDF_FOOTER_MM * dpi / 25.4
        for y in range(0, int(footer_top) - 2, 2):
            for x in range(0, width, 2):
                if rgb(image, x, y) != (255, 255, 255):
                    xs.append(x)
                    ys.append(y)
        # 400x100 (+ pen) in 277 x 183 mm: width limits it, so the ink is centred vertically and spans the width minus the margin
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, width / 2, delta=3)
        self.assertAlmostEqual((min(ys) + max(ys)) / 2, footer_top / 2, delta=3)
        ratio = (max(xs) - min(xs)) / (max(ys) - min(ys))
        self.assertAlmostEqual(ratio, 402 / 102, delta=0.25)             # the aspect ratio of the rectangle survives
        self.assertLess(max(ys), footer_top)                              # nothing paints into the footer strip

    def pdf_streams(self, path):
        """The decompressed content streams of a PDF file, as text."""
        with open(path, 'rb') as f:
            data = f.read()
        out = []
        for m in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.S):
            try:
                out.append(zlib.decompress(m.group(1)).decode('latin1'))
            except zlib.error:
                pass
        return data, out

    def test_write_pdf_is_one_a4_landscape_page(self):
        objs = [box('a', 100, 100, 300, 120, fill='#ffd8d8'), picture(x=500, y=100),
                {'id': 'h', 'kind': 'stroke', 'points': [[100, 300], [400, 320]], 'color': '#ffe000', 'width': 30,
                 'tool': 'highlighter', 'alpha': 0.5}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'out.pdf')
            export.write_pdf(path, objs, 'K7QX2M', 1794319700.0)
            data, streams = self.pdf_streams(path)
        self.assertEqual(data[:5], b'%PDF-')
        self.assertIn(b'/MediaBox [0 0 842.000000 595.000000]', data)         # A4 landscape in points
        self.assertEqual(len(re.findall(rb'/Type /Page\b', data)), 1)
        self.assertIn('K7QX2M'.encode('utf-16-be'), data)                      # the title carries the room code
        self.assertIn('LAN Whiteboard'.encode('utf-16-be'), data)              # the creator

    def test_pdf_origin_is_the_printable_corner(self):
        """Regression: QPdfWriter's origin is inside the margins; using paintRectPixels() shifted everything twice."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'out.pdf')
            export.write_pdf(path, [box('a', 100, 100, 300, 120)], 'K7QX2M')
            _, streams = self.pdf_streams(path)
        text = '\n'.join(streams)
        clip = re.search(r'0 0 m\s+([\d.]+) 0 l\s+[\d.]+ ([\d.]+) l', text)       # the first clip path: the board area
        self.assertIsNotNone(clip)
        self.assertGreater(float(clip.group(1)), 3200)                            # ~3274 px wide at 300 dpi
        self.assertLess(float(clip.group(2)), 2245)                               # shorter than the printable height: the footer fits below

    def test_empty_board_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'empty.pdf')
            export.write_pdf(path, {}, None)
            self.assertGreater(os.path.getsize(path), 500)

    def test_unwritable_path_is_an_export_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(export.ExportError):
                export.write_pdf(os.path.join(d, 'no-such-folder', 'x.pdf'), {}, 'ABCDEF')


def Qt_white():
    """QColor white as an int for QImage.fill (keeps the import list short)."""
    return QColor('#ffffff').rgb()


class FakeBoard:
    """Stands in for BoardModel: only .objects."""

    def __init__(self, objects):
        """Keep the objects dict."""
        self.objects = objects


class ExportMenuTests(unittest.TestCase):
    """The two menu actions, with the file dialog replaced."""

    def setUp(self):
        """A host app with one rectangle on its board and a temporary folder."""
        self.app = FakeApp('host')
        self.app.board = FakeBoard({'a': box('a', 100, 100, 100, 60, fill='#ff0000')})
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        """Remove the folder."""
        self._tmp.cleanup()

    def dialog(self, name, chosen_filter=''):
        """Patch QFileDialog.getSaveFileName to 'choose' <folder>/<name>."""
        path = os.path.join(self.dir, name) if name else ''
        return mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(path, chosen_filter))

    def test_registration(self):
        entries = {e['label']: e for e in registry.MENU_ACTIONS if e['menu'] == 'File'}
        self.assertEqual((entries['Export PNG']['fn'], entries['Export PNG']['shortcut']), (export.export_png, 'Ctrl+E'))
        self.assertEqual((entries['Export PDF']['fn'], entries['Export PDF']['shortcut']), (export.export_pdf, None))

    def test_png_scale_comes_from_the_dialog_filter(self):
        for chosen, size in (('PNG image, 1x (*.png)', (184, 144)), ('PNG image, 2x (*.png)', (368, 288)), ('', (184, 144))):
            with self.dialog('pic.png', chosen) as dlg:
                export.export_png(self.app)
            image = QImage(os.path.join(self.dir, 'pic.png'))
            self.assertEqual((image.width(), image.height()), size, chosen)
            os.remove(os.path.join(self.dir, 'pic.png'))
        filters = dlg.call_args[0][3]
        self.assertIn('1x', filters)
        self.assertIn('2x', filters)
        self.assertEqual(dlg.call_args[0][2], 'whiteboard-ABCDEF.png')       # default file name carries the room code

    def test_png_toast_and_suffix(self):
        with self.dialog('noext', 'PNG image, 2x (*.png)'):
            export.export_png(self.app)
        path = os.path.join(self.dir, 'noext.png')
        self.assertTrue(os.path.exists(path))                                # the missing .png was added
        self.assertEqual(self.app.toasts, ['Exported PNG (368 x 288 px): ' + path])

    def test_cancelled_dialog_does_nothing(self):
        with self.dialog(None):
            export.export_png(self.app)
            export.export_pdf(self.app)
        self.assertEqual((self.app.toasts, os.listdir(self.dir)), ([], []))

    def test_png_failure_is_a_toast(self):
        with self.dialog(os.path.join('missing-folder', 'x.png')):
            export.export_png(self.app)
        self.assertEqual(len(self.app.toasts), 1)
        self.assertTrue(self.app.toasts[0].startswith('Could not export: '))

    def test_png_too_large_is_a_toast(self):
        with self.dialog('big.png', 'PNG image, 2x (*.png)'), mock.patch.object(export, 'PNG_MAX_PIXELS', 10):
            export.export_png(self.app)
        self.assertIn('too large', self.app.toasts[0])
        self.assertEqual(os.listdir(self.dir), [])

    def test_pdf_export(self):
        with self.dialog('doc'):
            export.export_pdf(self.app)
        path = os.path.join(self.dir, 'doc.pdf')
        with open(path, 'rb') as f:
            self.assertEqual(f.read(5), b'%PDF-')
        self.assertEqual(self.app.toasts, ['Exported PDF: ' + path])

    def test_pdf_failure_is_a_toast(self):
        with self.dialog(os.path.join('missing-folder', 'x.pdf')):
            export.export_pdf(self.app)
        self.assertTrue(self.app.toasts[0].startswith('Could not export: '))

    def test_exports_work_without_a_board(self):
        self.app.board = None
        with self.dialog('empty.png', 'PNG image, 1x (*.png)'):
            export.export_png(self.app)
        self.assertEqual(QImage(os.path.join(self.dir, 'empty.png')).width(), config.BOARD_W)

    def test_viewers_may_export_too(self):
        self.app.me.role = 'viewer'                    # exporting only reads the board
        with self.dialog('v.pdf'):
            export.export_pdf(self.app)
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'v.pdf')))


if __name__ == '__main__':
    unittest.main()
