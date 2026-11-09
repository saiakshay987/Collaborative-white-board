# OWNER: P3
"""p3_test_board_model.py: Unit tests for client/board_model.py (contract E2, E3).

Run from the project root:  python scripts/p3_test_board_model.py

No window and no sockets. The model's clock is replaced by a fake one and the
timer callbacks (_on_gap_timeout, _tick) are called by hand, so nothing sleeps.
A FakeNet answers SYNC_REQUEST the way the server does (SYNC_DELTA when
BoardState.delta() works, otherwise SNAPSHOT). The convergence tests feed the
REAL server board (server/board_state.py) into a model through the lossy
receive_live() path and compare checksums.
"""
import base64
import copy
import json
import os
import random
import sys
import unittest
import zlib

# Make "import client" / "import server" work when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from client import board_model, registry  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel, board_checksum  # noqa: E402
from server.board_state import BoardState, objects_checksum  # noqa: E402

_qapp = QCoreApplication.instance() or QCoreApplication([])    # QTimer needs an application object


def rect(oid, x=0):
    """A complete rect object as a tool would send it."""
    return {'id': oid, 'kind': 'rect', 'x': x, 'y': 0, 'w': 40, 'h': 40,
            'color': '#ff0000', 'width': 2, 'fill': None}


STYLE = {'tool': 'pen', 'color': '#112233', 'width': 4, 'alpha': 1}


def echo(type_, payload, op_id, seq, user, **extra):
    """A server broadcast envelope (what apply_remote receives)."""
    return dict({'v': 1, 'type': type_, 'room': 'K7QX2M', 'user': user, 'op_id': op_id,
                 'seq': seq, 'ts': 1.0, 'payload': payload}, **extra)


def add_op(oid, seq, user='bob', x=0, op_id=None):
    """The broadcast of an OP_ADD of a rect."""
    return echo('OP_ADD', {'obj': dict(rect(oid, x), owner=user, z=seq)}, op_id or oid, seq, user)


def snapshot(objects, seq):
    """Build a SNAPSHOT payload the way the server does."""
    raw = json.dumps({'objects': objects}).encode('utf-8')
    return {'seq': seq, 'enc': 'zlib-b64', 'data': base64.b64encode(zlib.compress(raw)).decode('ascii')}


class FakeClock:
    """A clock the tests move by hand."""

    def __init__(self):
        """Start at t = 1000 s."""
        self.t = 1000.0

    def __call__(self):
        """Current fake time."""
        return self.t


class FakeNet:
    """Stands in for NetClient: records sends; can answer SYNC_REQUEST like the server."""

    def __init__(self, server=None, board=None):
        """server: a BoardState to answer from (None: just record)."""
        self.sent = []
        self.server = server
        self.board = board

    def send(self, type, payload, op_id=None):
        """Record the message; answer a SYNC_REQUEST from the server board if one is set."""
        self.sent.append((type, payload, op_id))
        if type == 'SYNC_REQUEST' and self.server is not None:
            ops = self.server.delta(payload.get('last_seq'))
            if ops is not None:
                self.board.apply_delta({'from_seq': payload['last_seq'], 'to_seq': self.server.seq, 'ops': ops})
            else:
                self.board.load_snapshot(self.server.snapshot_payload())

    def syncs(self):
        """The SYNC_REQUEST payloads sent so far."""
        return [p for t, p, _ in self.sent if t == 'SYNC_REQUEST']


class ModelTestCase(unittest.TestCase):
    def setUp(self):
        """A model attached to an AppState for user 'me', with a fake clock and net."""
        self.old_env = {k: os.environ.pop(k, None) for k in ('WB_DROP_PCT', 'WB_DUP_PCT')}
        self.app = AppState()
        self.app.me.username = 'me'
        self.board = BoardModel()
        self.board.attach(self.app)
        self.clock = FakeClock()
        self.board.clock = self.clock
        self.board._last_rx = self.clock()
        self.net = FakeNet()
        self.app.net = self.net
        self.changes = []
        self.board.changed.connect(self.changes.append)

    def tearDown(self):
        """Stop the timers and restore the environment."""
        self.board._gap_timer.stop()
        self.board._tick_timer.stop()
        for k, v in self.old_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


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
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('me-1'), owner='me', z=1)}, 'me-1', 1, 'me'))
        self.assertEqual(len(self.board.objects), 1)
        self.assertEqual((self.board.objects['me-1']['owner'], self.board.objects['me-1']['z']), ('me', 1))
        self.assertEqual((self.board.last_seq, dict(self.board.pending)), (1, {}))

    def test_confirm_copies_defaults_the_server_filled_in(self):
        local = rect('me-1')
        del local['fill']
        self.board.apply_local('OP_ADD', {'obj': local}, 'me-1')
        self.board.apply_remote(echo('OP_ADD', {'obj': dict(rect('me-1'), owner='me', z=1)}, 'me-1', 1, 'me'))
        self.assertIn('fill', self.board.objects['me-1'])

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
        self.board.apply_remote(add_op('bob-1', 1))
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
                                                        'style': STYLE}, 'me-2', 7 - 6, 'me'))
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'me-2', 'points': [[1, 1]], 'done': True},
                                     'me-3', 2, 'me'))
        o = self.board.objects['me-2']
        self.assertEqual((o['points'], o['z'], o['owner'], o['color']), ([[0, 0], [1, 1]], 1, 'me', '#112233'))
        self.assertEqual(self.board.last_seq, 2)


class OrderingTests(ModelTestCase):
    def test_in_order_ops_apply_immediately(self):
        for seq in (1, 2, 3):
            self.board.apply_remote(add_op('bob-%d' % seq, seq))
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['bob-1', 'bob-2', 'bob-3'], 3))

    def test_remote_protected_fields_are_untouched_by_update(self):
        self.board.apply_remote(add_op('bob-1', 1))
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'bob-1', 'set': {'x': 5, 'z': 77, 'owner': 'x'}},
                                     'bob-2', 2, 'bob'))
        o = self.board.objects['bob-1']
        self.assertEqual((o['x'], o['z'], o['owner']), (5, 1, 'bob'))

    def test_seq_ahead_is_buffered_then_drained_in_order(self):
        self.board.apply_remote(add_op('bob-3', 3))
        self.board.apply_remote(add_op('bob-2', 2))
        self.assertEqual((len(self.board.objects), self.board.last_seq), (0, 0))      # waiting for seq 1
        self.assertTrue(self.board._gap_timer.isActive())
        self.board.apply_remote(add_op('bob-1', 1))
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['bob-1', 'bob-2', 'bob-3'], 3))
        self.assertFalse(self.board._gap_timer.isActive())                            # gap closed
        self.assertEqual(self.net.syncs(), [])                                         # and no request needed

    def test_duplicates_by_seq_are_dropped(self):
        e = add_op('bob-1', 1)
        self.board.apply_remote(e)
        self.board.apply_remote(copy.deepcopy(e))
        self.board.apply_remote(add_op('other', 1))           # same seq, different content: still a duplicate
        self.assertEqual((list(self.board.objects), len(self.changes)), (['bob-1'], 1))
        self.assertEqual(self.board.stats['dup_dropped'], 2)

    def test_op_id_already_applied_is_not_applied_again(self):
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'bob-1', 'points': [[0, 0]], 'style': {}},
                                     'bob-1', 1, 'bob'))
        # a replay carrying a NEW seq but an op_id we already applied must not append twice
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'bob-1', 'points': [[0, 0]], 'style': {}},
                                     'bob-1', 2, 'bob'))
        self.assertEqual(self.board.objects['bob-1']['points'], [[0, 0]])
        self.assertEqual(self.board.last_seq, 2)              # but last_seq moves on, never stuck

    def test_buffer_ignores_a_second_copy_of_the_same_seq(self):
        self.board.apply_remote(add_op('first', 3))
        self.board.apply_remote(add_op('second', 3))
        self.board.apply_remote(add_op('a', 1))
        self.board.apply_remote(add_op('b', 2))
        self.assertEqual(list(self.board.objects), ['a', 'b', 'first'])

    def test_clear_and_missing_targets(self):
        self.board.apply_remote(add_op('bob-1', 1))
        self.board.apply_remote(echo('OP_CLEAR', {}, 'host-1', 2, 'host'))
        self.assertEqual((len(self.board.objects), self.changes[-1]), (0, None))
        before = list(self.changes)
        self.board.apply_remote(echo('OP_DELETE', {'obj_id': 'nope'}, 'bob-4', 3, 'bob'))
        self.board.apply_remote(echo('OP_UPDATE', {'obj_id': 'nope', 'set': {'x': 1}}, 'bob-5', 4, 'bob'))
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'nope', 'points': [[1, 1]]}, 'bob-6', 5, 'bob'))
        self.assertEqual(self.changes, before)            # nothing changed, no signal
        self.assertEqual(self.board.last_seq, 5)

    def test_garbage_is_ignored(self):
        for msg in ({}, {'type': 'CHAT'}, {'type': 'OP_ADD', 'payload': 'x', 'seq': 1},
                    {'type': 'OP_ADD', 'payload': {}, 'seq': None}, {'type': 'OP_ADD', 'payload': {}, 'seq': True},
                    {'type': 'OP_ADD', 'payload': {}, 'seq': '1'}):
            self.board.apply_remote(msg)
        self.assertEqual((len(self.board.objects), self.changes, self.board.last_seq), (0, [], 0))


class SyncRequestTests(ModelTestCase):
    def test_gap_still_open_after_one_second_sends_sync_request(self):
        self.board.apply_remote(add_op('bob-1', 1))
        self.board.apply_remote(add_op('bob-4', 4))                 # 2 and 3 missing
        self.board._on_gap_timeout()                                # the 1 s timer fires
        self.assertEqual(self.net.syncs(), [{'last_seq': 1}])

    def test_gap_that_closed_sends_nothing(self):
        self.board.apply_remote(add_op('bob-2', 2))
        self.board.apply_remote(add_op('bob-1', 1))
        self.board._on_gap_timeout()
        self.assertEqual(self.net.syncs(), [])

    def test_at_most_one_request_per_two_seconds(self):
        self.board.apply_remote(add_op('bob-3', 3))
        self.board._on_gap_timeout()
        self.clock.t += 1.0
        self.board._on_gap_timeout()                                # only 1 s later: blocked, timer re-armed
        self.assertEqual(len(self.net.syncs()), 1)
        self.assertTrue(self.board._gap_timer.isActive())
        self.clock.t += 1.1
        self.board._on_gap_timeout()                                # 2.1 s after the first
        self.assertEqual(len(self.net.syncs()), 2)

    def test_delta_answer_fills_the_gap_and_drains_the_buffer(self):
        server = BoardState()
        for i in range(1, 6):
            server.apply({'v': 1, 'type': 'OP_ADD', 'room': 'R', 'user': 'bob', 'op_id': 'bob-%d' % i,
                          'seq': None, 'ts': 1.0, 'payload': {'obj': rect('bob-%d' % i)}})
        self.net.server, self.net.board = server, self.board
        env = server.log
        self.board.apply_remote(copy.deepcopy(env[0]))
        self.board.apply_remote(copy.deepcopy(env[3]))             # seq 4; 2 and 3 lost
        self.board.apply_remote(copy.deepcopy(env[4]))             # seq 5
        self.board._on_gap_timeout()
        self.assertEqual(self.board.last_seq, 5)
        self.assertEqual(self.board.checksum(), server.checksum())
        self.assertFalse(self.board._gap_timer.isActive())
        self.assertEqual(self.board.stats['snapshots'], 0)

    def test_snapshot_answer_when_delta_is_impossible(self):
        server = BoardState()
        for i in range(1, 700):                                     # gap above MAX_DELTA
            server.apply({'v': 1, 'type': 'OP_ADD', 'room': 'R', 'user': 'bob', 'op_id': 'bob-%d' % i,
                          'seq': None, 'ts': 1.0, 'payload': {'obj': rect('bob-%d' % i)}})
        self.net.server, self.net.board = server, self.board
        self.board.apply_remote(copy.deepcopy(server.log[-1]))      # seq 699 only: buffered
        self.board._on_gap_timeout()
        self.assertEqual((self.board.last_seq, self.board.stats['snapshots']), (699, 1))
        self.assertEqual(self.board.checksum(), server.checksum())

    def test_no_echo_for_five_seconds_triggers_sync_request(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.clock.t += 4.9
        self.board._tick()
        self.assertEqual(self.net.syncs(), [])
        self.clock.t += 0.2
        self.board._tick()
        self.assertEqual(self.net.syncs(), [{'last_seq': 0}])
        self.clock.t += 10
        self.board._tick()
        self.assertEqual(len(self.net.syncs()), 1)                  # one request per stale op, not one per tick
        self.assertIn('me-1', self.board.pending)                   # still pending: a late echo is still recognised

    def test_late_echo_after_the_sync_request_is_still_confirmed_not_applied_twice(self):
        self.board.apply_local('OP_STROKE_PART', {'obj_id': 'me-1', 'points': [[0, 0]], 'style': STYLE}, 'me-1')
        self.clock.t += 6
        self.board._tick()
        self.board.apply_remote(echo('OP_STROKE_PART', {'obj_id': 'me-1', 'points': [[0, 0]], 'style': STYLE},
                                     'me-1', 1, 'me'))
        self.assertEqual(self.board.objects['me-1']['points'], [[0, 0]])
        self.assertEqual(dict(self.board.pending), {})

    def test_sync_without_network_does_not_crash(self):
        self.app.net = None
        self.board.apply_remote(add_op('bob-3', 3))
        self.board._on_gap_timeout()
        self.assertEqual(self.board.stats['sync_requests'], 1)


class RejectedOpTests(ModelTestCase):
    def setUp(self):
        """A server board answering SYNC_REQUEST, and a model that already follows it."""
        super().setUp()
        self.server = BoardState()
        self.net.server, self.net.board = self.server, self.board

    def accept(self, type_, payload, user, op_id):
        """The server accepts an op and returns the broadcast envelope."""
        return self.server.apply({'v': 1, 'type': type_, 'room': 'R', 'user': user, 'op_id': op_id,
                                  'seq': None, 'ts': 1.0, 'payload': copy.deepcopy(payload)})

    def stale_sync(self):
        """Let 6 s pass and run the pending check, which sends SYNC_REQUEST."""
        self.clock.t += 6
        self.board._tick()

    def test_rejected_update_and_add_are_dropped_after_the_delta_and_the_ghost_is_removed(self):
        self.board.apply_local('OP_UPDATE', {'obj_id': 'gone', 'set': {'x': 1}}, 'me-1')     # server: NOT_FOUND
        self.board.apply_local('OP_ADD', {'obj': rect('me-2')}, 'me-2')                       # server: rejected
        self.assertIn('me-2', self.board.objects)
        self.stale_sync()
        self.assertEqual((dict(self.board.pending), 'me-2' in self.board.objects), ({}, False))
        self.assertEqual(self.board.checksum(), self.server.checksum())

    def test_accepted_op_is_confirmed_by_the_delta_not_removed(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.accept('OP_ADD', {'obj': rect('me-1')}, 'me', 'me-1')              # accepted, echo lost
        self.stale_sync()
        self.assertEqual((dict(self.board.pending), list(self.board.objects)), ({}, ['me-1']))
        self.assertEqual(self.board.objects['me-1']['z'], 1)
        self.assertEqual(self.board.checksum(), self.server.checksum())

    def test_ops_sent_after_the_request_stay_pending(self):
        self.board.apply_local('OP_DELETE', {'obj_id': 'gone'}, 'me-1')
        self.stale_sync()                                                       # request out, reply came at once
        self.net.server = None                                                  # next request gets no answer
        self.board.apply_local('OP_DELETE', {'obj_id': 'gone'}, 'me-2')
        self.assertEqual(list(self.board.pending), ['me-2'])

    def test_reply_settles_only_the_ops_pending_when_its_request_was_sent(self):
        self.net.server = None                                                  # replies are delayed
        self.board.apply_local('OP_DELETE', {'obj_id': 'gone'}, 'me-1')
        self.stale_sync()                                                       # request 1 (me-1)
        self.board.apply_local('OP_DELETE', {'obj_id': 'gone'}, 'me-2')
        self.stale_sync()                                                       # request 2 (me-1, me-2)
        self.net.server = self.server
        self.board.apply_delta({'from_seq': 0, 'to_seq': 0, 'ops': []})         # reply to request 1
        self.assertEqual(list(self.board.pending), ['me-2'])
        self.board.apply_delta({'from_seq': 0, 'to_seq': 0, 'ops': []})         # reply to request 2
        self.assertEqual(dict(self.board.pending), {})

    def test_unsolicited_snapshot_settles_nothing(self):
        self.board.apply_local('OP_DELETE', {'obj_id': 'gone'}, 'me-1')
        self.board.load_snapshot(snapshot([], 0))                               # the one sent right after joining
        self.assertEqual(list(self.board.pending), ['me-1'])


class SnapshotAndDeltaTests(ModelTestCase):
    def test_snapshot_replaces_everything_and_sets_last_seq(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        objs = [dict(rect('a-1'), owner='a', z=1), dict(rect('a-2', x=9), owner='a', z=2)]
        self.board.load_snapshot(snapshot(objs, 12))
        self.assertEqual(list(self.board.objects), ['a-1', 'a-2'])
        self.assertEqual((self.board.last_seq, self.changes[-1]), (12, None))
        self.assertEqual(self.board.objects['a-2']['x'], 9)

    def test_ops_already_in_the_snapshot_are_dropped_and_later_ones_applied(self):
        self.board.load_snapshot(snapshot([dict(rect('a-1'), owner='a', z=1)], 5))
        self.board.apply_remote(add_op('a-1', 1, user='a'))
        self.board.apply_remote(add_op('a-2', 6, user='a'))
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['a-1', 'a-2'], 6))

    def test_snapshot_drains_messages_buffered_beyond_it(self):
        self.board.apply_remote(add_op('a-7', 7, user='a'))          # buffered: waiting for 1..6
        self.board.apply_remote(add_op('a-3', 3, user='a'))
        self.board.load_snapshot(snapshot([dict(rect('a-1'), owner='a', z=1)], 6))
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['a-1', 'a-7'], 7))
        self.assertFalse(self.board._gap_timer.isActive())

    def test_my_pending_op_covered_by_the_snapshot_is_settled_by_its_echo(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.board.load_snapshot(snapshot([dict(rect('me-1'), owner='me', z=3)], 5))
        self.board.apply_remote(add_op('me-1', 3, user='me', op_id='me-1'))   # old echo arrives late
        self.assertEqual((dict(self.board.pending), list(self.board.objects)), ({}, ['me-1']))

    def test_my_pending_op_not_in_the_snapshot_is_applied_when_its_echo_comes(self):
        self.board.apply_local('OP_ADD', {'obj': rect('me-1')}, 'me-1')
        self.board.load_snapshot(snapshot([], 5))                     # wiped my optimistic copy
        self.assertEqual(len(self.board.objects), 0)
        self.board.apply_remote(add_op('me-1', 6, user='me', op_id='me-1'))
        self.assertEqual((list(self.board.objects), self.board.objects['me-1']['z']), (['me-1'], 6))

    def test_bad_snapshot_encoding(self):
        with self.assertRaises(ValueError):
            self.board.load_snapshot({'seq': 1, 'enc': 'gzip', 'data': ''})

    def test_delta_applies_missed_ops_with_one_signal(self):
        self.board.load_snapshot(snapshot([dict(rect('a-1'), owner='a', z=1)], 1))
        self.changes.clear()
        ops = [add_op('a-2', 2, user='a'),
               echo('OP_UPDATE', {'obj_id': 'a-1', 'set': {'x': 30}}, 'a-3', 3, 'a'),
               echo('OP_DELETE', {'obj_id': 'a-2'}, 'a-4', 4, 'a')]
        self.board.apply_delta({'from_seq': 1, 'to_seq': 4, 'ops': ops})
        self.assertEqual((list(self.board.objects), self.board.objects['a-1']['x']), (['a-1'], 30))
        self.assertEqual((self.board.last_seq, self.changes), (4, [None]))

    def test_delta_overlapping_what_we_have_is_harmless(self):
        for seq in (1, 2, 3):
            self.board.apply_remote(add_op('a-%d' % seq, seq, user='a'))
        self.board.apply_delta({'from_seq': 1, 'to_seq': 4, 'ops': [add_op('a-2', 2, user='a'),
                                                                    add_op('a-3', 3, user='a'),
                                                                    add_op('a-4', 4, user='a')]})
        self.assertEqual((list(self.board.objects), self.board.last_seq), (['a-1', 'a-2', 'a-3', 'a-4'], 4))

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
        registry.deliver_message(app, add_op('a-2', 4, user='a'))
        registry.deliver_message(app, {'type': 'SYNC_DELTA', 'payload': {'from_seq': 4, 'to_seq': 5, 'ops': [
            echo('OP_DELETE', {'obj_id': 'a-1'}, 'a-3', 5, 'a')]}})
        self.assertIs(app.board.app, app)
        self.assertEqual((list(app.board.objects), app.board.last_seq), (['a-2'], 5))

    def test_handler_creates_the_model_if_app_has_none(self):
        app = AppState()
        registry.deliver_message(app, add_op('a-1', 1, user='a'))
        self.assertIsInstance(app.board, BoardModel)
        self.assertEqual(list(app.board.objects), ['a-1'])

    def test_debug_menu_action_is_registered(self):
        entries = [e for e in registry.MENU_ACTIONS if e['menu'] == 'Debug']
        self.assertEqual([e['label'] for e in entries], ['Show board checksum'])


class ChecksumTests(unittest.TestCase):
    def test_client_and_server_formulas_agree_and_ignore_order(self):
        a = {'x-1': dict(rect('x-1'), owner='a', z=1), 'x-2': dict(rect('x-2', 9), owner='b', z=2)}
        b = {'x-2': dict(reversed(list(a['x-2'].items()))), 'x-1': a['x-1']}
        self.assertEqual(board_checksum(a), board_checksum(b))
        self.assertEqual(board_checksum(a), objects_checksum(a))
        self.assertEqual(len(board_checksum(a)), 40)
        before = board_checksum(a)
        self.assertNotEqual(before, board_checksum({'x-1': a['x-1']}))
        a['x-1']['x'] = 1
        self.assertNotEqual(before, board_checksum(a))

    def test_empty_board(self):
        self.assertEqual(board_checksum({}), objects_checksum({}))


class FaultInjectionTests(ModelTestCase):
    def feed(self, server, drop, dup, seed):
        """Send every server broadcast through receive_live with the given loss; then let recovery run."""
        os.environ['WB_DROP_PCT'], os.environ['WB_DUP_PCT'] = str(drop), str(dup)
        random.seed(seed)
        self.net.server, self.net.board = server, self.board
        for env in list(server.log):
            self.board.receive_live(copy.deepcopy(env))
        os.environ['WB_DROP_PCT'] = '0'                    # recovery (delta/snapshot) is never sabotaged
        for _ in range(20):                                 # timers: gap timeout and idle/pending checks
            if self.board.last_seq == server.seq and not self.board._buffer:
                break
            self.clock.t += 2.5
            self.board._on_gap_timeout()
            self.board._tick()

    def make_server(self, n):
        """A server board after n mixed operations by three users."""
        server = BoardState()
        users = ('alice', 'bob', 'carol')
        rnd = random.Random(7)
        for i in range(1, n + 1):
            user = rnd.choice(users)
            kind = rnd.choice(('add', 'add', 'stroke', 'update', 'delete'))
            ids = list(server.objects)
            if kind == 'update' and ids:
                t, p = 'OP_UPDATE', {'obj_id': rnd.choice(ids), 'set': {'x': rnd.randint(0, 99)}}
            elif kind == 'delete' and ids:
                t, p = 'OP_DELETE', {'obj_id': rnd.choice(ids)}
            elif kind == 'stroke':
                t, p = 'OP_STROKE_PART', {'obj_id': '%s-s%d' % (user, i), 'points': [[i, i]], 'style': STYLE}
            else:
                t, p = 'OP_ADD', {'obj': rect('%s-%d' % (user, i), i)}
            server.apply({'v': 1, 'type': t, 'room': 'R', 'user': user, 'op_id': '%s-%d' % (user, i),
                          'seq': None, 'ts': 1.0, 'payload': p})
        return server

    def test_converges_with_20_percent_drops_and_10_percent_duplicates(self):
        for seed in range(10):
            self.setUp()
            server = self.make_server(150)
            self.feed(server, 20, 10, seed)
            self.assertEqual(self.board.last_seq, server.seq, 'seed %d' % seed)
            self.assertEqual(self.board.checksum(), server.checksum(), 'seed %d' % seed)
            self.tearDown()

    def test_the_switches_really_do_something(self):
        server = self.make_server(100)
        self.feed(server, 30, 30, 1)
        s = self.board.stats
        self.assertGreater(s['injected_drops'], 0)
        self.assertGreater(s['injected_dups'], 0)
        self.assertGreater(s['dup_dropped'], 0)
        self.assertGreater(s['sync_requests'], 0)
        self.assertEqual(self.board.checksum(), server.checksum())

    def test_without_the_switches_nothing_is_injected(self):
        server = self.make_server(50)
        self.feed(server, 0, 0, 1)
        self.assertEqual((self.board.stats['injected_drops'], self.board.stats['injected_dups'],
                          self.board.stats['sync_requests']), (0, 0, 0))
        self.assertEqual(self.board.checksum(), server.checksum())

    def test_dropped_last_message_is_found_by_the_demo_idle_check(self):
        os.environ['WB_DROP_PCT'] = '100'
        server = self.make_server(3)
        self.net.server, self.net.board = server, self.board
        self.board.receive_live(copy.deepcopy(server.log[0]))        # everything is dropped
        self.assertEqual(self.board.last_seq, 0)
        self.clock.t += 3.5
        self.board._tick()                                            # idle for 3 s: asks the server
        self.assertEqual(self.board.last_seq, 3)

    def test_bad_percentages_are_ignored(self):
        os.environ['WB_DROP_PCT'] = 'lots'
        self.assertEqual(board_model._env_pct('WB_DROP_PCT'), 0.0)
        os.environ['WB_DROP_PCT'] = '250'
        self.assertEqual(board_model._env_pct('WB_DROP_PCT'), 100.0)


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
        self.assertEqual(self.board.checksum(), server.checksum())
        late = BoardModel()
        late.load_snapshot(server.snapshot_payload())
        self.assertEqual(late.checksum(), server.checksum())


if __name__ == '__main__':
    unittest.main()
