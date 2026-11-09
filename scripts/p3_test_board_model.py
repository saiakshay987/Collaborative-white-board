# OWNER: P3
"""p3_test_board_model.py: Unit tests for client/board_model.py.

Run from the project root:  python scripts/p3_test_board_model.py

No window and no sockets: BoardModel is a QObject, its `changed` signal works
without a QApplication, and the on_message handlers are called through
registry.deliver_message exactly as net_client does. The last test replays
the SERVER's real output (server/board_state.py) into a model and checks both
boards end up identical.
"""
import base64
import copy
import json
import os
import sys
import unittest
import zlib

# Make "import client" / "import server" work when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import board_model, registry  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel  # noqa: E402
from server.board_state import BoardState  # noqa: E402


def rect(oid, x=0):
    """A complete rect object as a tool would send it."""
    return {'id': oid, 'kind': 'rect', 'x': x, 'y': 0, 'w': 40, 'h': 40,
            'color': '#ff0000', 'width': 2, 'fill': None}


STYLE = {'tool': 'pen', 'color': '#112233', 'width': 4, 'alpha': 1}


def echo(type_, payload, op_id, seq, user, **extra):
    """A server broadcast envelope (what apply_remote receives)."""
    return dict({'v': 1, 'type': type_, 'room': 'K7QX2M', 'user': user, 'op_id': op_id,
                 'seq': seq, 'ts': 1.0, 'payload': payload}, **extra)


def snapshot(objects, seq):
    """Build a SNAPSHOT payload the way the server does."""
    raw = json.dumps({'objects': objects}).encode('utf-8')
    return {'seq': seq, 'enc': 'zlib-b64', 'data': base64.b64encode(zlib.compress(raw)).decode('ascii')}


class ModelTestCase(unittest.TestCase):
    def setUp(self):
        """A model attached to an AppState for user 'me', recording changed()."""
        self.app = AppState()
        self.app.me.username = 'me'
        self.board = BoardModel()
        self.board.attach(self.app)
        self.changes = []
        self.board.changed.connect(self.changes.append)


class LocalAndEchoTests(ModelTestCase):
    def test_attach_sets_app_board(self):
        self.assertIs(self.app.board, self.board)

    def test_apply_local_is_immediate_and_pending(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.assertIn('me-1', self.board.objects)
        self.assertEqual(list(self.board.pending), ['me-1'])
        self.assertEqual(self.changes, ['me-1'])
        self.assertEqual(self.board.last_seq, 0)          # nothing from the server yet

    def test_echo_confirms_without_applying_twice_and_adds_server_fields(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        obj = dict(rect('me-1'), owner='me', z=5)
        self.board.apply_remote(echo('OP_ADD', {'obj': obj}, 'me-1', 5, 'me'))
        self.assertEqual(len(self.board.objects), 1)
        self.assertEqual((self.board.objects['me-1']['owner'], self.board.objects['me-1']['z']), ('me', 5))
        self.assertEqual((self.board.last_seq, dict(self.board.pending)), (5, {}))

    def test_confirm_of_a_delete_does_not_resurrect_or_fail(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.board.apply_local('OP_DELETE', {'obj_id': 'me-1'}, 'me-2')
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('me-1'), owner='me', z=1)}, 'me-1', 1, 'me'))
        self.board.apply_remote(echo('OP_DELETE', {'obj_id': 'me-1'}, 'me-2', 2, 'me'))
        self.assertEqual((len(self.board.objects), self.board.last_seq), (0, 2))

    def test_payload_is_copied_not_shared(self):
        payload = {'obj': rect('me-1')}
        self.board.apply_local('OP_ADD', payload, 'me-1')
        payload['obj']['x'] = 999
        self.assertEqual(self.board.objects['me-1']['x'], 0)

    def test_undo_redo_are_not_pending(self):
        self.board.apply_local('UNDO', {}, 'me-3')
        self.assertEqual((dict(self.board.pending), self.changes), ({}, []))

    def test_my_update_wins_over_an_earlier_remote_update(self):
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('bob-1'), owner='bob', z=1)}, 'bob-1', 1, 'bob'))
        self.board.apply_local('OP_UPDATE', {'obj_id': 'bob-1', 'set': {'x': 10}}, 'me-1')
        # server order: bob's update (seq 2) first, mine (seq 3) second -> x must end as 10
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'bob-1', 'set': {'x': 50}}, 'bob-2', 2, 'bob'))
        self.assertEqual(self.board.objects['bob-1']['x'], 50)
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'bob-1', 'set': {'x': 10}}, 'me-1', 3, 'me'))
        self.assertEqual(self.board.objects['bob-1']['x'], 10)

    def test_local_stroke_then_echo_gets_owner_and_z(self):
        self.board.apply_local('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[0, 0]], 'done': False,
                                                  'style': STYLE}, 'me-2')
        self.board.apply_local('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[1, 1]], 'done': True}, 'me-3')
        self.assertEqual(self.board.objects['me-2']['points'], [[0, 0], [1, 1]])
        self.assertIsNone(self.board.objects['me-2']['z'])
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[0, 0]], 'done': False,
                                                        'style': STYLE}, 'me-2', 7, 'me'))
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[1, 1]], 'done': True},
                                     'me-3', 8, 'me'))
        o = self.board.objects['me-2']
        self.assertEqual((o['points'], o['z'], o['owner'], o['color']), ([[0, 0], [1, 1]], 7, 'me', '#112233'))
        self.assertEqual(self.board.last_seq, 8)


class RemoteTests(ModelTestCase):
    def test_remote_ops_apply_in_arrival_order(self):
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('bob-1'), owner='bob', z=1)}, 'bob-1', 1, 'bob'))
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'bob-1', 'set': {'x': 5, 'z': 77, 'owner': 'x'}},
                                     'bob-2', 2, 'bob'))
        o = self.board.objects['bob-1']
        self.assertEqual((o['x'], o['z'], o['owner']), (5, 1, 'bob'))     # protected fields untouched
        self.board.apply_remote(echo('OP_DELETE', {'obj_id': 'bob-1'}, 'bob-3', 3, 'bob'))
        self.assertEqual((len(self.board.objects), self.board.last_seq), (0, 3))
        self.assertEqual(self.changes, ['bob-1', 'bob-1', 'bob-1'])

    def test_remote_stroke_parts_build_a_stroke(self):
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'bob-1', 'points': [[0, 0], [1, 1]], 'done': False,
                                                        'style': {}}, 'bob-1', 4, 'bob'))
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'bob-1', 'points': [[2, 2]], 'done': True},
                                     'bob-2', 5, 'bob'))
        o = self.board.objects['bob-1']
        self.assertEqual((o['kind'], o['owner'], o['z'], o['tool'], len(o['points'])), ('stroke', 'bob', 4, 'pen', 3))

    def test_clear_and_missing_targets(self):
        self.board.apply_remote(echo('OP_ADD', {'obj': rect('bob-1')}, 'bob-1', 1, 'bob'))
        self.board.apply_remote(echo('OP_CLEAR', {}, 'host-1', 2, 'host'))
        self.assertEqual((len(self.board.objects), self.changes[-1]), (0, None))
        before = list(self.changes)
        self.board.apply_remote(echo('OP_DELETE', {'obj_id': 'nope'}, 'bob-4', 3, 'bob'))
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'nope', 'set': {'x': 1}}, 'bob-5', 4, 'bob'))
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'nope', 'points': [[1, 1]]}, 'bob-6', 5, 'bob'))
        self.assertEqual(self.changes, before)            # nothing changed, no signal
        self.assertEqual(self.board.last_seq, 5)

    def test_duplicates_by_seq_are_dropped(self):
        e = echo('OP_ADD', {'obj': rect('bob-1')}, 'bob-1', 1, 'bob')
        self.board.apply_remote(e)
        self.board.apply_remote(copy.deepcopy(e))
        self.assertEqual((len(self.board.objects), len(self.changes)), (1, 1))

    def test_garbage_is_ignored(self):
        for msg in ({}, {'type': 'CHAT'}, {'type': 'OP_ADD', 'payload': 'x'}, {'type': 'OP_ADD', 'payload': {}}):
            self.board.apply_remote(msg)
        self.assertEqual((len(self.board.objects), self.changes), (0, []))


class SnapshotAndDeltaTests(ModelTestCase):
    def test_snapshot_replaces_everything_and_sets_last_seq(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        objs = [dict(rect('a-1'), owner='a', z=1), dict(rect('a-2', x=9), owner='a', z=2)]
        self.board.load_snapshot(snapshot(objs, 12))
        self.assertEqual(list(self.board.objects), ['a-1', 'a-2'])
        self.assertEqual((self.board.last_seq, self.changes[-1]), (12, None))
        self.assertEqual(self.board.objects['a-2']['x'], 9)

    def test_ops_already_in_the_snapshot_are_dropped(self):
        self.board.load_snapshot(snapshot([dict(rect('a-1'), owner='a', z=1)], 5))
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('a-1'), owner='a', z=1)}, 'a-1', 1, 'a'))
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('a-2'), owner='a', z=6)}, 'a-2', 6, 'a'))
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['a-1', 'a-2'], 6))

    def test_bad_snapshot_encoding(self):
        with self.assertRaises(ValueError):
            self.board.load_snapshot({'seq': 1, 'enc': 'gzip', 'data': ''})

    def test_delta_applies_missed_ops_with_one_signal(self):
        self.board.load_snapshot(snapshot([dict(rect('a-1'), owner='a', z=1)], 1))
        self.changes.clear()
        ops = [echo('OP_ADD', {'obj': dict(rect('a-2'), owner='a', z=2)}, 'a-2', 2, 'a'),
               echo('OP_UPDATE', {'obj_id': 'a-1', 'set': {'x': 30}}, 'a-3', 3, 'a'),
               echo('OP_DELETE', {'obj_id': 'a-2'}, 'a-4', 4, 'a')]
        self.board.apply_delta({'from_seq': 1, 'to_seq': 4, 'ops': ops})
        self.assertEqual((list(self.board.objects), self.board.objects['a-1']['x']), (['a-1'], 30))
        self.assertEqual((self.board.last_seq, self.changes), (4, [None]))

    def test_empty_delta_when_up_to_date(self):
        self.board.last_seq = 4
        self.board.apply_delta({'from_seq': 4, 'to_seq': 4, 'ops': []})
        self.assertEqual(self.board.last_seq, 4)


class HandlerTests(unittest.TestCase):
    def test_handlers_registered_for_every_op_snapshot_and_delta(self):
        for t in ('OP_ADD', 'OP_STROKE_PART', 'OP_UPDATE', 'OP_DELETE', 'OP_CLEAR', 'SNAPSHOT', 'SYNC_DELTA'):
            self.assertTrue(registry.MESSAGE_HANDLERS.get(t), t)

    def test_messages_reach_the_model_through_deliver_message(self):
        app = AppState()
        app.me.username = 'me'
        app.board = BoardModel()
        registry.deliver_message(app, {'type': 'SNAPSHOT', 'payload': snapshot([dict(rect('a-1'), owner='a', z=1)], 3)})
        registry.deliver_message(app, echo('OP_ADD', {'obj': dict(rect('a-2'), owner='a', z=4)}, 'a-2', 4, 'a'))
        registry.deliver_message(app, {'type': 'SYNC_DELTA', 'payload': {'from_seq': 4, 'to_seq': 5, 'ops': [
            echo('OP_DELETE', {'obj_id': 'a-1'}, 'a-3', 5, 'a')]}})
        self.assertIs(app.board.app, app)
        self.assertEqual((list(app.board.objects), app.board.last_seq), (['a-2'], 5))

    def test_handler_creates_the_model_if_app_has_none(self):
        app = AppState()
        registry.deliver_message(app, echo('OP_ADD', {'obj': rect('a-1')}, 'a-1', 1, 'a'))
        self.assertIsInstance(app.board, BoardModel)
        self.assertEqual(list(app.board.objects), ['a-1'])


class AgainstTheServerTests(ModelTestCase):
    def test_model_equals_server_board_after_a_mixed_session(self):
        """Feed the server's own broadcast envelopes (including my pending ones) into the model."""
        server = BoardState()

        def server_op(type_, user, payload, op_id):
            """What the server broadcasts for one accepted client op."""
            return server.apply({'v': 1, 'type': type_, 'room': 'R', 'user': user, 'op_id': op_id,
                                 'seq': None, 'ts': 1.0, 'payload': copy.deepcopy(payload)})

        # me draws a rect and a stroke (optimistic), bob draws in between
        mine = [('OP_ADD', {'obj': rect('me-1')}, 'me-1'),
                ('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[0, 0], [1, 1]], 'done': False, 'style': STYLE}, 'me-2')]
        for t, p, oid in mine:
            self.board.apply_local(t, p, oid)
        script = [('bob', 'OP_ADD', {'obj': rect('bob-1')}, 'bob-1'),
                  ('me', *mine[0]),
                  ('bob', 'OP_UPDATE', {'obj_id': 'me-1', 'set': {'x': 60}}, 'bob-2'),
                  ('me', *mine[1]),
                  ('me', 'OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[2, 2]], 'done': True}, 'me-3'),
                  ('bob', 'OP_DELETE', {'obj_id': 'bob-1'}, 'bob-3')]
        for user, t, p, oid in script:
            if oid == 'me-3':
                self.board.apply_local(t, p, oid)
            self.board.apply_remote(server_op(t, user, p, oid))
        self.assertEqual(list(self.board.objects.items()), list(server.objects.items()))
        self.assertEqual((self.board.last_seq, dict(self.board.pending)), (server.seq, {}))
        # and a late joiner built from the server's snapshot ends up the same
        late = BoardModel()
        late.load_snapshot(server.snapshot_payload())
        self.assertEqual(list(late.objects.items()), list(server.objects.items()))


if __name__ == '__main__':
    unittest.main()
