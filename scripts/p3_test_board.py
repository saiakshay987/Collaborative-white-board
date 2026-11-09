# OWNER: P3
"""p3_test_board.py: Unit tests for server/board_state.py, server/undo.py, server/board_ops.py and server/sync.py.

Run from the project root:  python scripts/p3_test_board.py

The first part (ordering, strokes, snapshots, deltas, undo) tests BoardState
and UndoManager directly. The last part (HandlerTests, SyncTests) drives the
real dispatcher and room hooks with fake sessions instead of sockets, so it
checks permissions, validation, the echo to every member and the SNAPSHOT /
SYNC_DELTA choice without a network.
"""
import base64
import copy
import json
import os
import sys
import unittest
import zlib

# Make "import server" / "import common" work when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import board_state  # noqa: E402
from server.board_state import (BoardState, BoardError, decode_snapshot, MAX_DELTA,  # noqa: E402
                                OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE, OP_CLEAR)
from server.undo import UndoManager  # noqa: E402

ROOM = 'K7QX2M'


def env(type_, user, payload, seq=None):
    return {'v': 1, 'type': type_, 'room': ROOM, 'user': user, 'op_id': f'{user}-1',
            'seq': seq, 'ts': None, 'payload': payload}


def rect(oid, x=0, y=0):
    return {'id': oid, 'kind': 'rect', 'x': x, 'y': y, 'w': 40, 'h': 40,
            'color': '#ff0000', 'width': 2, 'fill': None}


STYLE = {'tool': 'pen', 'color': '#112233', 'width': 4, 'alpha': 1}


class Room:
    """What board_ops will do: apply a client op and record it for undo."""

    def __init__(self):
        self.state = BoardState()
        self.undo = UndoManager()

    def do(self, user, type_, payload):
        e = env(type_, user, payload)
        oid = payload.get('obj_id') or (payload.get('obj') or {}).get('id')
        prev = self.state.get_copy(oid)
        e = self.state.apply(e)
        self.undo.record(user, e, prev)
        return e

    def add(self, user, oid, **kw):
        return self.do(user, OP_ADD, {'obj': rect(oid, **kw)})

    def do_undo(self, user):
        inv = self.undo.undo(user, self.state)
        return self.state.apply(inv, restore=True) if inv else None

    def do_redo(self, user):
        inv = self.undo.redo(user, self.state)
        return self.state.apply(inv, restore=True) if inv else None

    def ids(self):
        return list(self.state.objects)


class OrderingTests(unittest.TestCase):
    def test_seq_and_z_follow_acceptance_order(self):
        s = BoardState()
        a = s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-1')}))
        b = s.apply(env(OP_ADD, 'bob', {'obj': rect('bob-1')}))
        self.assertEqual((a['seq'], b['seq'], s.seq), (1, 2, 2))
        self.assertEqual(list(s.objects), ['alice-1', 'bob-1'])
        self.assertEqual((s.objects['alice-1']['z'], s.objects['alice-1']['owner']), (1, 'alice'))
        self.assertEqual((s.objects['bob-1']['z'], s.objects['bob-1']['owner']), (2, 'bob'))
        # the broadcast envelope carries the server-set fields
        self.assertEqual(b['payload']['obj']['z'], 2)
        self.assertEqual([e['seq'] for e in s.log], [1, 2])

    def test_client_cannot_forge_owner_or_z(self):
        s = BoardState()
        obj = rect('bob-1')
        obj.update(owner='alice', z=99)
        s.apply(env(OP_ADD, 'bob', {'obj': obj}))
        self.assertEqual((s.objects['bob-1']['owner'], s.objects['bob-1']['z']), ('bob', 1))

    def test_last_update_wins_field_by_field(self):
        s = BoardState()
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-1')}))
        s.apply(env(OP_UPDATE, 'alice', {'obj_id': 'alice-1', 'set': {'x': 10, 'color': '#00ff00'}}))
        s.apply(env(OP_UPDATE, 'bob', {'obj_id': 'alice-1', 'set': {'x': 50}}))
        o = s.objects['alice-1']
        self.assertEqual((o['x'], o['color'], o['y']), (50, '#00ff00', 0))

    def test_rejected_op_changes_nothing(self):
        s = BoardState()
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-1')}))
        before = (s.seq, len(s.log), copy.deepcopy(s.objects))
        bad = [
            env(OP_UPDATE, 'a', {'obj_id': 'nope', 'set': {'x': 1}}),
            env(OP_DELETE, 'a', {'obj_id': 'nope'}),
            env(OP_UPDATE, 'a', {'obj_id': 'alice-1', 'set': {'z': 0}}),
            env(OP_UPDATE, 'a', {'obj_id': 'alice-1', 'set': {'owner': 'bob'}}),
            env(OP_ADD, 'a', {'obj': rect('alice-1')}),             # duplicate id
            env(OP_ADD, 'a', {'obj': {'kind': 'rect'}}),            # no id
            env(OP_STROKE_PART, 'a', {'obj_id': 'a-9', 'points': [[1, 1]], 'done': False}),
            env('OP_BOGUS', 'a', {}),
        ]
        for e in bad:
            with self.assertRaises(BoardError, msg=e['type']):
                s.apply(e)
        self.assertEqual((s.seq, len(s.log), s.objects), before)

    def test_error_codes(self):
        s = BoardState()
        with self.assertRaises(BoardError) as cm:
            s.apply(env(OP_DELETE, 'a', {'obj_id': 'x'}))
        self.assertEqual(cm.exception.code, 'NOT_FOUND')

    def test_explicit_seq_must_be_next(self):
        s = BoardState()
        with self.assertRaises(ValueError):
            s.apply(env(OP_ADD, 'a', {'obj': rect('a-1')}, seq=5))
        s.apply(env(OP_ADD, 'a', {'obj': rect('a-1')}, seq=1))
        self.assertEqual(s.seq, 1)

    def test_delete_and_clear(self):
        s = BoardState()
        for i in range(3):
            s.apply(env(OP_ADD, 'a', {'obj': rect(f'a-{i}')}))
        s.apply(env(OP_DELETE, 'a', {'obj_id': 'a-1'}))
        self.assertEqual(list(s.objects), ['a-0', 'a-2'])
        s.apply(env(OP_CLEAR, 'host', {}))
        self.assertEqual((len(s.objects), s.seq), (0, 5))

    def test_limits(self):
        s = BoardState()
        old = board_state.MAX_OBJECTS
        board_state.MAX_OBJECTS = 2
        try:
            s.apply(env(OP_ADD, 'a', {'obj': rect('a-1')}))
            s.apply(env(OP_ADD, 'a', {'obj': rect('a-2')}))
            with self.assertRaises(BoardError) as cm:
                s.apply(env(OP_ADD, 'a', {'obj': rect('a-3')}))
            self.assertEqual(cm.exception.code, 'TOO_LARGE')
        finally:
            board_state.MAX_OBJECTS = old


class StrokeTests(unittest.TestCase):
    def test_first_part_creates_later_parts_append(self):
        s = BoardState()
        s.apply(env(OP_ADD, 'bob', {'obj': rect('bob-1')}))
        s.apply(env(OP_STROKE_PART, 'alice',
                    {'obj_id': 'alice-2', 'points': [[0, 0], [1, 1]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_STROKE_PART, 'alice',
                    {'obj_id': 'alice-2', 'points': [[2, 2]], 'done': False}))
        s.apply(env(OP_STROKE_PART, 'alice',
                    {'obj_id': 'alice-2', 'points': [[3, 3]], 'done': True}))
        o = s.objects['alice-2']
        self.assertEqual(o['kind'], 'stroke')
        self.assertEqual(o['points'], [[0, 0], [1, 1], [2, 2], [3, 3]])
        self.assertEqual((o['tool'], o['color'], o['width'], o['alpha']), ('pen', '#112233', 4, 1))
        self.assertEqual((o['owner'], o['z']), ('alice', 2))     # z = seq at creation
        self.assertEqual(s.seq, 4)                                # every part has its own seq

    def test_interleaved_strokes_of_two_users(self):
        s = BoardState()
        s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-1', 'points': [[0, 0]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'bob-1', 'points': [[9, 9]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-1', 'points': [[1, 1]], 'done': True}))
        s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'bob-1', 'points': [[8, 8]], 'done': True}))
        self.assertEqual(s.objects['alice-1']['points'], [[0, 0], [1, 1]])
        self.assertEqual(s.objects['bob-1']['points'], [[9, 9], [8, 8]])

    def test_style_defaults_and_part_size_limit(self):
        s = BoardState()
        s.apply(env(OP_STROKE_PART, 'a', {'obj_id': 'a-1', 'points': [[0, 0]], 'done': False, 'style': {}}))
        self.assertEqual(s.objects['a-1']['tool'], 'pen')
        with self.assertRaises(BoardError) as cm:
            s.apply(env(OP_STROKE_PART, 'a', {'obj_id': 'a-1', 'points': [[0, 0]] * 51, 'done': False}))
        self.assertEqual(cm.exception.code, 'TOO_LARGE')
        s.apply(env(OP_STROKE_PART, 'a', {'obj_id': 'a-1', 'points': [[0, 0]] * 50, 'done': False}))
        self.assertEqual(len(s.objects['a-1']['points']), 51)

    def test_total_point_limit(self):
        s = BoardState()
        old = board_state.MAX_POINTS
        board_state.MAX_POINTS = 5
        try:
            s.apply(env(OP_STROKE_PART, 'a', {'obj_id': 'a-1', 'points': [[0, 0]] * 4, 'done': False, 'style': STYLE}))
            with self.assertRaises(BoardError) as cm:
                s.apply(env(OP_STROKE_PART, 'a', {'obj_id': 'a-1', 'points': [[0, 0]] * 2, 'done': False}))
            self.assertEqual(cm.exception.code, 'TOO_LARGE')
            self.assertEqual(len(s.objects['a-1']['points']), 4)
        finally:
            board_state.MAX_POINTS = old

    def test_only_owner_appends_and_target_must_be_a_stroke(self):
        s = BoardState()
        s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-1', 'points': [[0, 0]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-2')}))
        with self.assertRaises(BoardError) as cm:
            s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'alice-1', 'points': [[1, 1]], 'done': False}))
        self.assertEqual(cm.exception.code, 'FORBIDDEN')
        with self.assertRaises(BoardError) as cm:
            s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-2', 'points': [[1, 1]], 'done': False}))
        self.assertEqual(cm.exception.code, 'BAD_FORMAT')

    def test_late_part_after_erase_is_rejected(self):
        s = BoardState()
        s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-1', 'points': [[0, 0]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_DELETE, 'bob', {'obj_id': 'alice-1'}))
        with self.assertRaises(BoardError) as cm:
            s.apply(env(OP_STROKE_PART, 'alice', {'obj_id': 'alice-1', 'points': [[1, 1]], 'done': True}))
        self.assertEqual(cm.exception.code, 'NOT_FOUND')
        self.assertNotIn('alice-1', s.objects)


class SnapshotTests(unittest.TestCase):
    def build(self):
        s = BoardState()
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-1', x=20, y=40)}))
        s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'bob-1', 'points': [[0.5, 1.5], [2, 3]], 'done': True, 'style': STYLE}))
        s.apply(env(OP_ADD, 'alice', {'obj': {'id': 'alice-2', 'kind': 'text', 'x': 0, 'y': 0, 'w': 10, 'h': 10,
                                              'text': 'héllo ✓', 'color': '#000000', 'size': 14}}))
        return s

    def test_round_trip_through_zlib(self):
        s = self.build()
        snap = s.snapshot_payload()
        self.assertEqual(set(snap), {'seq', 'enc', 'data'})
        self.assertEqual((snap['enc'], snap['seq']), ('zlib-b64', 3))
        # decode by hand, exactly as the contract describes it
        raw = zlib.decompress(base64.b64decode(snap['data']))
        decoded = json.loads(raw)
        self.assertEqual([o['id'] for o in decoded['objects']], ['alice-1', 'bob-1', 'alice-2'])
        self.assertEqual(decoded['objects'], list(s.objects.values()))

        t = BoardState()
        t.load_snapshot(snap)
        self.assertEqual(t.seq, 3)
        self.assertEqual(list(t.objects.items()), list(s.objects.items()))
        self.assertEqual(t.log, [])
        self.assertEqual(t.snapshot_payload(), snap)

    def test_empty_board_and_compression(self):
        snap = BoardState().snapshot_payload()
        self.assertEqual((snap['seq'], decode_snapshot(snap)), (0, []))
        s = BoardState()
        for i in range(200):
            s.apply(env(OP_ADD, 'a', {'obj': rect(f'a-{i}')}))
        snap = s.snapshot_payload()
        self.assertLess(len(base64.b64decode(snap['data'])),
                        len(json.dumps(list(s.objects.values()))))

    def test_snapshot_is_independent_of_later_changes(self):
        s = self.build()
        snap = s.snapshot_payload()
        s.apply(env(OP_UPDATE, 'a', {'obj_id': 'alice-1', 'set': {'x': 999}}))
        self.assertEqual(decode_snapshot(snap)[0]['x'], 20)

    def test_unknown_encoding(self):
        with self.assertRaises(ValueError):
            decode_snapshot({'seq': 1, 'enc': 'gzip', 'data': ''})

    def test_snapshot_then_continue(self):
        s = self.build()
        t = BoardState()
        t.load_snapshot(s.snapshot_payload())
        e = t.apply(env(OP_ADD, 'carol', {'obj': rect('carol-1')}))
        self.assertEqual(e['seq'], 4)
        self.assertEqual(t.objects['carol-1']['z'], 4)


class DeltaTests(unittest.TestCase):
    def filled(self, n):
        s = BoardState()
        for i in range(n):
            s.apply(env(OP_ADD, 'a', {'obj': rect(f'a-{i}')}))
        return s

    def test_returns_entries_after_from_seq(self):
        s = self.filled(10)
        d = s.delta(7)
        self.assertEqual([e['seq'] for e in d], [8, 9, 10])
        self.assertEqual(d[0]['payload']['obj']['id'], 'a-7')
        self.assertEqual(len(s.delta(0)), 10)

    def test_up_to_date_gives_empty_list(self):
        s = self.filled(10)
        self.assertEqual(s.delta(10), [])
        self.assertEqual(BoardState().delta(0), [])

    def test_limit_is_max_delta(self):
        self.assertEqual(MAX_DELTA, 500)
        s = self.filled(600)
        self.assertEqual(len(s.delta(100)), 500)      # gap exactly 500
        self.assertIsNone(s.delta(99))                # gap 501
        self.assertIsNone(s.delta(0))

    def test_client_ahead_of_server(self):
        s = self.filled(5)
        self.assertIsNone(s.delta(6))     # e.g. server restarted from an older autosave

    def test_bad_values(self):
        s = self.filled(5)
        for bad in (None, -1, '3', 2.5, True):
            self.assertIsNone(s.delta(bad), repr(bad))

    def test_not_in_log_after_trim(self):
        s = self.filled(board_state.LOG_LIMIT + 300)
        self.assertEqual(len(s.log), board_state.LOG_LIMIT)
        self.assertEqual(s.log[0]['seq'], 301)
        self.assertEqual(len(s.delta(s.seq - 500)), 500)
        self.assertIsNone(s.delta(10))
        # a small log limit makes the log itself the limit, not MAX_DELTA
        old = board_state.LOG_LIMIT
        board_state.LOG_LIMIT = 20
        try:
            t = self.filled(100)
            self.assertEqual(len(t.delta(80)), 20)
            self.assertIsNone(t.delta(79))
        finally:
            board_state.LOG_LIMIT = old

    def test_after_load_snapshot_log_starts_there(self):
        s = self.filled(50)
        t = BoardState()
        t.load_snapshot(s.snapshot_payload())
        self.assertEqual(t.delta(50), [])
        self.assertIsNone(t.delta(10))
        t.apply(env(OP_ADD, 'a', {'obj': rect('a-new')}))
        self.assertEqual([e['seq'] for e in t.delta(50)], [51])
        self.assertIsNone(t.delta(49))

    def test_delta_replays_to_the_same_board(self):
        s = BoardState()
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-1')}))
        base = s.snapshot_payload()
        s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'bob-1', 'points': [[0, 0]], 'done': False, 'style': STYLE}))
        s.apply(env(OP_STROKE_PART, 'bob', {'obj_id': 'bob-1', 'points': [[1, 1]], 'done': True}))
        s.apply(env(OP_UPDATE, 'alice', {'obj_id': 'alice-1', 'set': {'x': 80}}))
        s.apply(env(OP_ADD, 'alice', {'obj': rect('alice-2')}))
        s.apply(env(OP_DELETE, 'bob', {'obj_id': 'alice-2'}))
        client = BoardState()
        client.load_snapshot(base)
        for e in s.delta(1):
            client.apply(copy.deepcopy(e))
        self.assertEqual(list(client.objects.items()), list(s.objects.items()))

    def test_delta_is_a_copy(self):
        s = self.filled(3)
        s.delta(0)[0]['payload']['obj']['x'] = 12345
        self.assertEqual(s.log[0]['payload']['obj']['x'], 0)


class UndoTests(unittest.TestCase):
    def test_two_users_interleaving(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.add('bob', 'bob-1')
        r.add('alice', 'alice-2')
        r.add('bob', 'bob-2')
        self.assertEqual(r.ids(), ['alice-1', 'bob-1', 'alice-2', 'bob-2'])

        e = r.do_undo('alice')                       # removes alice-2, not bob-2
        self.assertEqual((e['type'], e['payload']['obj_id'], e['user']), (OP_DELETE, 'alice-2', 'alice'))
        self.assertEqual(r.ids(), ['alice-1', 'bob-1', 'bob-2'])
        r.do_undo('bob')
        self.assertEqual(r.ids(), ['alice-1', 'bob-1'])

        e = r.do_redo('alice')                       # brings alice-2 back
        self.assertEqual((e['type'], e['payload']['obj']['id']), (OP_ADD, 'alice-2'))
        self.assertEqual(r.ids(), ['alice-1', 'bob-1', 'alice-2'])
        self.assertEqual((r.state.objects['alice-2']['owner'], r.state.objects['alice-2']['z']), ('alice', 3))

        r.do_undo('alice')
        r.do_undo('alice')
        self.assertEqual(r.ids(), ['bob-1'])
        self.assertIsNone(r.do_undo('alice'))        # alice has nothing left
        r.do_undo('bob')
        self.assertEqual(r.ids(), [])
        r.do_redo('bob')
        self.assertEqual(r.ids(), ['bob-1'])

    def test_inverse_envelopes_are_server_shaped(self):
        r = Room()
        r.add('alice', 'alice-1')
        inv = r.undo.undo('alice', r.state)
        self.assertEqual((inv['v'], inv['room'], inv['user'], inv['seq'], inv['op_id']),
                         (1, ROOM, 'alice', None, None))
        e = r.state.apply(inv, restore=True)
        self.assertEqual(e['seq'], 2)                # fresh seq
        self.assertEqual([x['seq'] for x in r.state.log], [1, 2])

    def test_stroke_undo_is_one_step_and_redo_restores_all_points(self):
        r = Room()
        r.do('alice', OP_STROKE_PART, {'obj_id': 'alice-1', 'points': [[0, 0], [1, 1]], 'done': False, 'style': STYLE})
        r.do('alice', OP_STROKE_PART, {'obj_id': 'alice-1', 'points': [[2, 2]], 'done': False})
        r.do('alice', OP_STROKE_PART, {'obj_id': 'alice-1', 'points': [[3, 3]], 'done': True})
        r.add('alice', 'alice-2')
        r.do_undo('alice')                           # the rect
        e = r.do_undo('alice')                       # the whole stroke
        self.assertEqual((e['type'], e['payload']['obj_id']), (OP_DELETE, 'alice-1'))
        self.assertEqual(r.ids(), [])
        self.assertIsNone(r.do_undo('alice'))
        r.do_redo('alice')
        r.do_redo('alice')
        self.assertEqual(r.ids(), ['alice-1', 'alice-2'])
        self.assertEqual(r.state.objects['alice-1']['points'], [[0, 0], [1, 1], [2, 2], [3, 3]])
        self.assertEqual(r.state.objects['alice-1']['color'], '#112233')

    def test_delete_undo_restores_object_with_owner_and_z(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.add('bob', 'bob-1')
        r.add('alice', 'alice-2')
        r.do('alice', OP_DELETE, {'obj_id': 'bob-1'})        # alice erases bob's object
        self.assertEqual(r.ids(), ['alice-1', 'alice-2'])
        e = r.do_undo('alice')
        self.assertEqual((e['type'], e['payload']['obj']['id']), (OP_ADD, 'bob-1'))
        self.assertEqual(r.ids(), ['alice-1', 'bob-1', 'alice-2'])     # back in z order
        o = r.state.objects['bob-1']
        self.assertEqual((o['owner'], o['z']), ('bob', 2))             # not alice, not a new z
        self.assertEqual(e['payload']['obj']['z'], 2)                  # clients get the same
        r.do_redo('alice')
        self.assertEqual(r.ids(), ['alice-1', 'alice-2'])

    def test_update_undo_restores_previous_values_only(self):
        r = Room()
        r.add('alice', 'alice-1', x=10, y=10)
        r.do('alice', OP_UPDATE, {'obj_id': 'alice-1', 'set': {'x': 100, 'color': '#00ff00'}})
        r.do('bob', OP_UPDATE, {'obj_id': 'alice-1', 'set': {'y': 77}})   # other field, other user
        e = r.do_undo('alice')
        self.assertEqual((e['type'], e['payload']['set']), (OP_UPDATE, {'x': 10, 'color': '#ff0000'}))
        o = r.state.objects['alice-1']
        self.assertEqual((o['x'], o['y'], o['color']), (10, 77, '#ff0000'))   # bob's y untouched
        r.do_undo('bob')                                    # bob undoes his own update
        self.assertEqual(r.state.objects['alice-1']['y'], 10)
        r.do_redo('alice')
        o = r.state.objects['alice-1']
        self.assertEqual((o['x'], o['color']), (100, '#00ff00'))

    def test_skips_entries_whose_object_is_gone(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.add('alice', 'alice-2')
        r.add('alice', 'alice-3')
        r.do('bob', OP_DELETE, {'obj_id': 'alice-3'})
        r.do('bob', OP_DELETE, {'obj_id': 'alice-2'})
        e = r.do_undo('alice')                       # skips -3 and -2, removes -1
        self.assertEqual(e['payload']['obj_id'], 'alice-1')
        self.assertEqual(r.ids(), [])
        self.assertIsNone(r.do_undo('alice'))

    def test_nothing_to_undo_returns_none(self):
        r = Room()
        self.assertIsNone(r.undo.undo('nobody', r.state))
        self.assertIsNone(r.undo.redo('nobody', r.state))
        r.add('alice', 'alice-1')
        r.do('bob', OP_DELETE, {'obj_id': 'alice-1'})
        self.assertIsNone(r.do_undo('alice'))        # everything alice did is gone
        self.assertEqual(r.ids(), [])

    def test_skipped_redo_entry(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.do_undo('alice')
        r.add('bob', 'alice-1')                      # someone else now uses that id
        self.assertIsNone(r.do_redo('alice'))

    def test_new_action_clears_that_users_redo_only(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.add('bob', 'bob-1')
        r.do_undo('alice')
        r.do_undo('bob')
        r.add('alice', 'alice-2')                    # alice acts: her redo is gone
        self.assertIsNone(r.do_redo('alice'))
        e = r.do_redo('bob')                         # bob's survives
        self.assertEqual(e['payload']['obj']['id'], 'bob-1')
        self.assertEqual(r.ids(), ['bob-1', 'alice-2'])      # bob-1 keeps its older z

    def test_first_stroke_part_clears_redo_later_parts_do_not(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.do_undo('alice')
        r.do('alice', OP_STROKE_PART, {'obj_id': 'alice-2', 'points': [[0, 0]], 'done': False, 'style': STYLE})
        self.assertIsNone(r.do_redo('alice'))

    def test_redo_after_undo_chain_is_in_reverse_order(self):
        r = Room()
        for i in range(3):
            r.add('alice', f'alice-{i}')
        for _ in range(3):
            r.do_undo('alice')
        order = [r.do_redo('alice')['payload']['obj']['id'] for _ in range(3)]
        self.assertEqual(order, ['alice-0', 'alice-1', 'alice-2'])
        self.assertIsNone(r.do_redo('alice'))

    def test_clear_is_not_undoable(self):
        r = Room()
        r.add('alice', 'alice-1')
        r.add('bob', 'bob-1')
        r.do('alice', OP_DELETE, {'obj_id': 'alice-1'})
        r.do('host', OP_CLEAR, {})
        self.assertEqual(r.ids(), [])
        for user in ('alice', 'bob', 'host'):
            self.assertIsNone(r.do_undo(user), user)     # nothing resurrects the old board
        self.assertEqual(r.ids(), [])
        # and the stacks work normally again afterwards
        r.add('alice', 'alice-9')
        r.do_undo('alice')
        self.assertEqual(r.ids(), [])

    def test_history_limit(self):
        um = UndoManager(limit=3)
        s = BoardState()
        for i in range(5):
            e = s.apply(env(OP_ADD, 'a', {'obj': rect(f'a-{i}')}))
            um.record('a', e, None)
        undone = []
        while True:
            inv = um.undo('a', s)
            if inv is None:
                break
            undone.append(s.apply(inv, restore=True)['payload']['obj_id'])
        self.assertEqual(undone, ['a-4', 'a-3', 'a-2'])



# ===========================================================================
# Handlers and room hooks (fake sessions, no sockets)
# ===========================================================================

import threading  # noqa: E402

from common import models  # noqa: E402
from server import board_ops, dispatcher, permissions, rooms, sync  # noqa: E402,F401

# The real kinds_*.py registries belong to P2/P4 and may still be empty
# stubs, so the tests bring their own tiny registry for the kinds they use.
TEST_KINDS = {
    'rect': {'required': {'x': float, 'y': float, 'w': float, 'h': float, 'color': str,
                          'width': float, 'fill': (str, type(None))},
             'defaults': {'fill': None}},
    'stroke': {'required': {'points': list, 'color': str, 'width': float, 'tool': str, 'alpha': float},
               'defaults': {}},
}

# The contract C4 table (the real permissions.py is P4's).
ALLOWED = {'host': {'draw', 'undo', 'upload', 'version_save', 'chat', 'cursor',
                    'clear', 'version_restore', 'set_role', 'kick'},
           'editor': {'draw', 'undo', 'upload', 'version_save', 'chat', 'cursor'},
           'viewer': {'chat', 'cursor'}}


def contract_can(role, action):
    """permissions.can() as in contract C4, for the tests."""
    return action in ALLOWED.get(role, ())


class FakeSession:
    """Stands in for server.server.Session: records what would go on the wire."""

    def __init__(self, room, name, role):
        """Join `name` to `room` with `role`, like presence.py will."""
        self.room = room
        self.sent = []          # (type, payload, env) of every message received
        self.member = rooms.manager.add_member(room, name, role, self)

    def send(self, type, payload, **env):
        """Record a message instead of writing it to a socket."""
        self.sent.append((type, copy.deepcopy(payload), env))

    def send_error(self, code, text, ref_type=None):
        """Record an ERROR like Session.send_error."""
        self.send('ERROR', {'code': code, 'text': text, 'ref_type': ref_type})

    def of_type(self, type_):
        """All received messages of one type, as (payload, env)."""
        return [(p, e) for t, p, e in self.sent if t == type_]


class HandlerTests(unittest.TestCase):
    def setUp(self):
        """A room with a host, two editors and a viewer; swap in the test registries."""
        self._kinds, self._can = models.KINDS, permissions.can
        models.KINDS = TEST_KINDS
        permissions.can = contract_can
        self.room = rooms.manager.create_room()
        self.host = FakeSession(self.room, 'host', 'host')
        self.alice = FakeSession(self.room, 'alice', 'editor')
        self.bob = FakeSession(self.room, 'bob', 'editor')
        self.view = FakeSession(self.room, 'view', 'viewer')
        self.everyone = [self.host, self.alice, self.bob, self.view]

    def tearDown(self):
        """Restore the real registries."""
        models.KINDS, permissions.can = self._kinds, self._can

    def send(self, session, type_, payload, op_id='x-1', **forged):
        """Dispatch a client message the way server.py does."""
        msg = {'v': 1, 'type': type_, 'room': 'FORGED', 'user': 'FORGED', 'op_id': op_id,
               'seq': 999, 'ts': 1.0, 'payload': payload}
        msg.update(forged)
        dispatcher.dispatch(session, msg)

    def error_of(self, session):
        """The code of the last ERROR the session received, or None."""
        errs = session.of_type('ERROR')
        return errs[-1][0]['code'] if errs else None

    def assert_rejected(self, session, code, seq_before):
        """Only `session` got an ERROR with this code; nothing else changed."""
        self.assertEqual(self.error_of(session), code)
        for other in self.everyone:
            if other is not session:
                self.assertEqual(other.sent, [], other.member.name)
        self.assertEqual(self.room.board.seq, seq_before)

    # -- accepted operations ------------------------------------------------
    def test_add_is_echoed_to_everyone_including_the_sender(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')}, op_id='alice-7')
        for s in self.everyone:
            [(payload, env)] = s.of_type('OP_ADD')
            self.assertEqual((env['seq'], env['op_id'], env['user']), (1, 'alice-7', 'alice'))
            self.assertEqual((payload['obj']['owner'], payload['obj']['z']), ('alice', 1))
        self.assertGreater(self.room.board.log[0]['ts'], 1.0)    # server clock, not the forged 1.0

    def test_seq_owner_and_user_cannot_be_forged(self):
        obj = rect('bob-1')
        obj.update(owner='alice', z=99)
        self.send(self.bob, 'OP_ADD', {'obj': obj}, seq=50, user='alice')
        o = self.room.board.objects['bob-1']
        self.assertEqual((o['owner'], o['z'], self.room.board.seq), ('bob', 1, 1))

    def test_defaults_are_filled_in_and_extra_payload_fields_dropped(self):
        obj = rect('alice-1')
        del obj['fill']
        self.send(self.alice, 'OP_ADD', {'obj': obj, 'junk': 'x' * 100})
        self.assertIsNone(self.room.board.objects['alice-1']['fill'])
        [(payload, _)] = self.bob.of_type('OP_ADD')
        self.assertEqual(list(payload), ['obj'])

    def test_stroke_parts_are_broadcast_with_their_own_seq(self):
        self.send(self.alice, 'OP_STROKE_PART', {'obj_id': 'alice-2', 'points': [[0, 0], [1, 1]],
                                                  'done': False, 'style': {'color': '#112233', 'width': 4}})
        self.send(self.alice, 'OP_STROKE_PART', {'obj_id': 'alice-2', 'points': [[2, 2]], 'done': True})
        parts = self.bob.of_type('OP_STROKE_PART')
        self.assertEqual([e['seq'] for _, e in parts], [1, 2])
        self.assertEqual(parts[0][0]['style'], {'tool': 'pen', 'color': '#112233', 'width': 4, 'alpha': 1})
        self.assertIs(parts[1][0]['done'], True)
        self.assertEqual(self.room.board.objects['alice-2']['points'], [[0, 0], [1, 1], [2, 2]])

    def test_update_delete_and_host_clear(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.send(self.bob, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'x': 80}})   # anyone with draw may edit
        self.assertEqual(self.room.board.objects['alice-1']['x'], 80)
        self.send(self.bob, 'OP_DELETE', {'obj_id': 'alice-1'})
        self.assertEqual(len(self.room.board.objects), 0)
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-2')})
        self.send(self.host, 'OP_CLEAR', {})
        self.assertEqual((len(self.room.board.objects), self.room.board.seq), (0, 5))
        self.assertEqual([e['seq'] for _, e in self.view.of_type('OP_CLEAR')], [5])

    def test_accepted_ops_are_recorded_for_undo(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        inv = self.room.undo.undo('alice', self.room.board)
        self.assertEqual((inv['type'], inv['payload']), ('OP_DELETE', {'obj_id': 'alice-1'}))
        self.assertIsNone(self.room.undo.undo('bob', self.room.board))   # bob did nothing

    # -- rejected operations ------------------------------------------------
    def test_viewer_cannot_draw_and_editor_cannot_clear(self):
        self.send(self.view, 'OP_ADD', {'obj': rect('view-1')})
        self.assert_rejected(self.view, 'FORBIDDEN', 0)
        self.view.sent.clear()
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        for s in self.everyone:
            s.sent.clear()
        self.send(self.alice, 'OP_CLEAR', {})
        self.assert_rejected(self.alice, 'FORBIDDEN', 1)
        self.assertEqual(len(self.room.board.objects), 1)

    def test_invalid_objects_are_bad_format(self):
        bad = [rect('a-1', x='left'), {'id': 'a-1', 'kind': 'nope'}, {'kind': 'rect'}, 'not a dict']
        for obj in bad:
            self.send(self.alice, 'OP_ADD', {'obj': obj})
            self.assert_rejected(self.alice, 'BAD_FORMAT', 0)
            self.alice.sent.clear()

    def test_missing_targets_are_not_found(self):
        for type_, payload in (('OP_UPDATE', {'obj_id': 'nope', 'set': {'x': 1}}),
                               ('OP_DELETE', {'obj_id': 'nope'}),
                               ('OP_STROKE_PART', {'obj_id': 'nope', 'points': [[1, 1]], 'done': True})):
            self.send(self.alice, type_, payload)
            self.assert_rejected(self.alice, 'NOT_FOUND', 0)
            self.alice.sent.clear()

    def test_update_cannot_break_an_object(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        for s in self.everyone:
            s.sent.clear()
        self.send(self.bob, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'owner': 'bob'}})
        self.assert_rejected(self.bob, 'FORBIDDEN', 1)
        self.bob.sent.clear()
        self.send(self.bob, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'width': 'fat'}})
        self.assert_rejected(self.bob, 'BAD_FORMAT', 1)
        self.assertEqual(self.room.board.objects['alice-1']['width'], 2)

    def test_stroke_part_errors(self):
        style = {'color': '#000000'}
        self.send(self.alice, 'OP_STROKE_PART', {'obj_id': 'alice-1', 'points': [[0, 0]], 'done': False, 'style': style})
        for s in self.everyone:
            s.sent.clear()
        cases = [
            (self.bob, {'obj_id': 'alice-1', 'points': [[1, 1]], 'done': False}, 'FORBIDDEN'),   # not the owner
            (self.alice, {'obj_id': 'alice-1', 'points': [[1, 1]] * 51, 'done': False}, 'TOO_LARGE'),
            (self.alice, {'obj_id': 'alice-1', 'points': [[1]], 'done': False}, 'BAD_FORMAT'),
            (self.alice, {'obj_id': 'alice-1', 'points': [['a', 'b']], 'done': False}, 'BAD_FORMAT'),
            (self.alice, {'obj_id': 'alice-1', 'points': 'xx', 'done': False}, 'BAD_FORMAT'),
            (self.alice, {'obj_id': 5, 'points': [], 'done': False}, 'BAD_FORMAT'),
            (self.alice, {'obj_id': 'alice-9', 'points': [[0, 0]], 'done': False,
                          'style': {'width': 'fat'}}, 'BAD_FORMAT'),
        ]
        for session, payload, code in cases:
            self.send(session, 'OP_STROKE_PART', payload)
            self.assert_rejected(session, code, 1)
            session.sent.clear()
        self.assertEqual(self.room.board.objects['alice-1']['points'], [[0, 0]])

    def test_non_string_op_id_is_bad_format(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')}, op_id=['x'])
        self.assert_rejected(self.alice, 'BAD_FORMAT', 0)

    def test_duplicate_object_id_is_rejected(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        for s in self.everyone:
            s.sent.clear()
        self.send(self.bob, 'OP_ADD', {'obj': rect('alice-1')})
        self.assert_rejected(self.bob, 'BAD_FORMAT', 1)

    # -- concurrency --------------------------------------------------------
    def test_concurrent_senders_get_a_gap_free_ordered_stream(self):
        """Three threads add 50 objects each; every member must see seq 1..150 in order."""
        def work(session):
            """One 'client reader thread' sending 50 adds."""
            for i in range(50):
                self.send(session, 'OP_ADD', {'obj': rect('%s-%d' % (session.member.name, i))})

        threads = [threading.Thread(target=work, args=(s,)) for s in (self.host, self.alice, self.bob)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        accepted = 150
        self.assertEqual(self.room.board.seq, accepted)
        for s in self.everyone:
            seqs = [e['seq'] for _, e in s.of_type('OP_ADD')]
            self.assertEqual(seqs, list(range(1, accepted + 1)), s.member.name)
        # z equals seq at creation, so the paint order is the acceptance order
        self.assertEqual([o['z'] for o in self.room.board.objects.values()], list(range(1, accepted + 1)))


class SyncTests(unittest.TestCase):
    def setUp(self):
        """A room with a drawing member, and the registries swapped for the tests."""
        self._kinds, self._can = models.KINDS, permissions.can
        models.KINDS = TEST_KINDS
        permissions.can = contract_can
        self.room = rooms.manager.create_room()
        self.alice = FakeSession(self.room, 'alice', 'editor')
        self.counter = 0

    def tearDown(self):
        """Restore the real registries."""
        models.KINDS, permissions.can = self._kinds, self._can

    def draw(self, n):
        """Alice adds n objects."""
        for _ in range(n):
            self.counter += 1
            dispatcher.dispatch(self.alice, {'type': 'OP_ADD', 'op_id': 'alice-%d' % self.counter,
                                             'payload': {'obj': rect('alice-%d' % self.counter)}})

    def join(self, name, rejoin, last_seq):
        """Add a member and run the join hooks like presence.py does."""
        s = FakeSession(self.room, name, 'editor')
        rooms.manager.member_joined(self.room, s.member, rejoin, last_seq)
        return s

    def test_room_gets_board_and_undo(self):
        self.assertIsInstance(self.room.board, BoardState)
        self.assertIsInstance(self.room.undo, UndoManager)

    def test_first_join_gets_snapshot(self):
        self.draw(3)
        bob = self.join('bob', False, None)
        [(payload, _)] = bob.of_type('SNAPSHOT')
        self.assertEqual((payload['seq'], payload['enc']), (3, 'zlib-b64'))
        self.assertEqual(len(decode_snapshot(payload)), 3)
        self.assertEqual(bob.of_type('SYNC_DELTA'), [])

    def test_rejoin_with_small_gap_gets_delta(self):
        self.draw(10)
        bob = self.join('bob', True, 7)
        [(payload, _)] = bob.of_type('SYNC_DELTA')
        self.assertEqual((payload['from_seq'], payload['to_seq']), (7, 10))
        self.assertEqual([e['seq'] for e in payload['ops']], [8, 9, 10])
        self.assertEqual(bob.of_type('SNAPSHOT'), [])

    def test_rejoin_up_to_date_gets_empty_delta(self):
        self.draw(4)
        [(payload, _)] = self.join('bob', True, 4).of_type('SYNC_DELTA')
        self.assertEqual((payload['from_seq'], payload['to_seq'], payload['ops']), (4, 4, []))

    def test_rejoin_with_big_gap_or_bad_last_seq_gets_snapshot(self):
        self.draw(MAX_DELTA + 20)
        for name, last_seq in (('b1', 10), ('b2', None), ('b3', 99999), ('b4', 'x')):
            s = self.join(name, True, last_seq)
            self.assertEqual(len(s.of_type('SNAPSHOT')), 1, name)
            self.assertEqual(s.of_type('SYNC_DELTA'), [], name)
        self.assertEqual(len(self.join('b5', True, 20).of_type('SYNC_DELTA')), 1)    # gap exactly 500

    def test_delta_ops_replay_to_the_same_board(self):
        self.draw(2)
        base = self.room.board.snapshot_payload()
        self.draw(5)
        [(payload, _)] = self.join('bob', True, 2).of_type('SYNC_DELTA')
        client = BoardState()
        client.load_snapshot(base)
        for e in payload['ops']:
            client.apply(e)
        self.assertEqual(list(client.objects.items()), list(self.room.board.objects.items()))


if __name__ == '__main__':
    unittest.main()
