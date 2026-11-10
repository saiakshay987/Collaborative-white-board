# OWNER: P3
"""p3_test_export.py: Tests for client/export.py (save/load board as JSON).

Run from the project root:  python scripts/p3_test_export.py

No window and no network: the file format and the object preparation are plain
functions, and BoardLoader runs on a QCoreApplication event loop with a fake
app and a fake send function that only records what would have been sent.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication, QTimer  # noqa: E402

from common import protocol  # noqa: E402
from client import export, registry  # noqa: E402

APP = QCoreApplication.instance() or QCoreApplication([])


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
        self.username, self.role = 'hana', role


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


if __name__ == '__main__':
    unittest.main()
