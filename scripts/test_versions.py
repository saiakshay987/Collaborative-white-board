"""test_versions.py: Tests for the version history (server/versions.py): VERSION_SAVE / LIST / RESTORE,
the periodic version and the version files on disk.

Run from the project root:  python scripts/test_versions.py

Uses the fake sessions of test_board.py (no sockets). Disk tests write into
their own temporary folder, never into the real server_data/.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_board as base  # noqa: E402  (sets WB_DATA_DIR to a temp folder before server.versions loads)
from test_board import FakeSession, HandlerBase, rect  # noqa: E402

from server import chat, permissions, rooms, versions  # noqa: E402,F401  (chat: registers room.chat, as server/bootstrap.py does)
from server.board_state import BoardState  # noqa: E402


def setUpModule():
    """Stop the autosave thread that importing server.versions started; the tests call its functions directly."""
    versions.stop()


class VersionsBase(HandlerBase):
    """HandlerBase plus helpers to read VERSIONS / SNAPSHOT messages."""

    def clear_inboxes(self):
        """Forget everything the fake sessions received so far."""
        for s in self.everyone:
            s.sent.clear()

    def ids(self):
        """Object ids of the board, in paint order."""
        return list(self.room.board.objects)

    def names(self, session):
        """Names of the versions in the last VERSIONS message the session got."""
        return [v['name'] for v in session.of_type('VERSIONS')[-1][0]['versions']]


class SaveAndListTests(VersionsBase):
    """VERSION_SAVE and VERSION_LIST."""

    def test_editor_saves_a_copy_and_everyone_gets_the_list(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.send(self.bob, 'OP_ADD', {'obj': rect('bob-1')})
        seq = self.room.board.seq
        self.clear_inboxes()
        self.send(self.alice, 'VERSION_SAVE', {'name': 'first draft'})
        for s in self.everyone:                     # host, editors AND the viewer
            payload = s.of_type('VERSIONS')[0][0]
            (v,) = payload['versions']
            self.assertEqual((v['name'], v['by'], v['seq'], v['count']), ('first draft', 'alice', seq, 2))
            self.assertEqual(set(v), {'id', 'name', 'by', 'ts', 'seq', 'count'})
            self.assertEqual(len(s.sent), 1, s.member.name)
        self.assertEqual(self.room.board.seq, seq)  # a version is not a board operation: no seq used
        self.assertEqual([o['id'] for o in self.room.versions.get_objects(v['id'])], ['alice-1', 'bob-1'])

    def test_by_comes_from_the_server_not_from_the_payload(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'x', 'by': 'mallory'}, user='mallory')
        self.assertEqual(self.room.versions.meta_list()[0]['by'], 'alice')

    def test_viewer_cannot_save(self):
        self.clear_inboxes()
        self.send(self.view, 'VERSION_SAVE', {'name': 'x'})
        self.assertEqual(self.error_of(self.view), 'FORBIDDEN')
        self.assertEqual(self.room.versions.meta_list(), [])
        self.assertEqual([s.sent for s in (self.host, self.alice, self.bob)], [[], [], []])

    def test_bad_names(self):
        for name in (None, 5, '', '   ', '\n\t', ['a'], {'a': 1}, 'bell\x07'):
            self.send(self.alice, 'VERSION_SAVE', {'name': name})
            self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', repr(name))
            self.alice.sent.clear()
        self.send(self.alice, 'VERSION_SAVE', {})
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')
        self.assertEqual(self.room.versions.meta_list(), [])

    def test_name_is_tidied_and_cut(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': '  my \n  version  '})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'x' * 500})
        names = [v['name'] for v in self.room.versions.meta_list()]
        self.assertEqual(names, ['my version', 'x' * versions.MAX_NAME_LEN])

    def test_copy_is_not_changed_by_later_edits(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1', x=20)})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'v'})
        self.send(self.alice, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'x': 300}})
        vid = self.room.versions.meta_list()[0]['id']
        self.assertEqual(self.room.versions.get_objects(vid)[0]['x'], 20)

    def test_list_goes_to_the_sender_only_and_viewers_may_ask(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'v'})
        self.clear_inboxes()
        self.send(self.view, 'VERSION_LIST', {})
        self.assertEqual(self.names(self.view), ['v'])
        self.assertEqual([s.sent for s in (self.host, self.alice, self.bob)], [[], [], []])

    def test_empty_list(self):
        self.send(self.view, 'VERSION_LIST', {})
        self.assertEqual(self.view.of_type('VERSIONS')[0][0], {'versions': []})


class RestoreTests(VersionsBase):
    """VERSION_RESTORE."""

    def setUp(self):
        """Board with a (alice) and b (bob), saved as version 'two shapes', then b deleted and c added."""
        super().setUp()
        self.send(self.alice, 'OP_ADD', {'obj': rect('a', x=20)})
        self.send(self.bob, 'OP_ADD', {'obj': rect('b', x=40)})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'two shapes'})
        self.vid = self.room.versions.meta_list()[0]['id']
        self.send(self.bob, 'OP_DELETE', {'obj_id': 'b'})
        self.send(self.alice, 'OP_ADD', {'obj': rect('c', x=60)})
        self.old_seq = self.room.board.seq
        self.clear_inboxes()

    def test_host_restores_and_everyone_gets_a_snapshot(self):
        self.send(self.host, 'VERSION_RESTORE', {'id': self.vid})
        self.assertEqual(self.ids(), ['a', 'b'])
        self.assertEqual(self.room.board.seq, self.old_seq + 1)
        for s in self.everyone:                     # the host and the viewer too
            self.assertEqual([t for t, _, _ in s.sent], ['VERSIONS', 'SNAPSHOT'], s.member.name)
            snap = s.of_type('SNAPSHOT')[0][0]
            self.assertEqual(snap['seq'], self.old_seq + 1)
            client = BoardState()
            client.load_snapshot(snap)
            self.assertEqual(client.checksum(), self.room.board.checksum())

    def test_owner_and_z_come_back_with_the_objects(self):
        self.send(self.host, 'VERSION_RESTORE', {'id': self.vid})
        a, b = self.room.board.objects['a'], self.room.board.objects['b']
        self.assertEqual((a['owner'], b['owner']), ('alice', 'bob'))
        self.assertLess(a['z'], b['z'])

    def test_current_board_is_saved_first_so_a_restore_can_be_reversed(self):
        self.send(self.host, 'VERSION_RESTORE', {'id': self.vid})
        metas = self.room.versions.meta_list()
        self.assertEqual([m['name'] for m in metas], ['two shapes', 'before restore two shapes'])
        before = metas[1]
        self.assertEqual((before['by'], before['count'], before['seq']), ('host', 2, self.old_seq))
        self.assertEqual([o['id'] for o in self.room.versions.get_objects(before['id'])], ['a', 'c'])
        self.assertEqual(self.names(self.view), ['two shapes', 'before restore two shapes'])
        # ... and restoring that one brings back the board as it was before
        self.send(self.host, 'VERSION_RESTORE', {'id': before['id']})
        self.assertEqual(self.ids(), ['a', 'c'])
        self.assertEqual(self.room.board.seq, self.old_seq + 2)

    def test_log_is_emptied_so_old_clients_take_a_snapshot(self):
        self.send(self.host, 'VERSION_RESTORE', {'id': self.vid})
        board = self.room.board
        self.assertIsNone(board.delta(self.old_seq))        # a client at the old seq: no delta with a hole
        self.assertIsNone(board.delta(self.old_seq - 3))
        self.assertEqual(board.delta(board.seq), [])        # a client that has the snapshot: nothing missing
        self.send(self.alice, 'OP_ADD', {'obj': rect('d')})
        self.assertEqual(board.seq, self.old_seq + 2)       # operations continue with seq + 1
        self.assertEqual([e['seq'] for e in board.delta(self.old_seq + 1)], [self.old_seq + 2])
        self.assertGreater(board.objects['d']['z'], board.objects['b']['z'])   # new objects paint on top

    def test_undo_stacks_are_reset(self):
        self.send(self.host, 'VERSION_RESTORE', {'id': self.vid})
        self.clear_inboxes()
        self.send(self.alice, 'UNDO', {})                   # alice's last step was adding 'c', gone now
        self.assertEqual(self.error_of(self.alice), 'NOT_FOUND')
        self.assertEqual(self.ids(), ['a', 'b'])

    def test_editor_and_viewer_cannot_restore(self):
        for who in (self.alice, self.view):
            self.send(who, 'VERSION_RESTORE', {'id': self.vid})
            self.assertEqual(self.error_of(who), 'FORBIDDEN')
        self.assertEqual(self.ids(), ['a', 'c'])
        self.assertEqual(self.room.board.seq, self.old_seq)
        self.assertEqual(len(self.room.versions.meta_list()), 1)    # no 'before restore' either
        self.assertEqual(self.host.sent, [])

    def test_bad_requests_change_nothing(self):
        for bad, code in ((999, 'NOT_FOUND'), (True, 'BAD_FORMAT'), ('1', 'BAD_FORMAT'), (None, 'BAD_FORMAT'),
                          (1.5, 'BAD_FORMAT')):
            self.host.sent.clear()
            self.send(self.host, 'VERSION_RESTORE', {'id': bad})
            self.assertEqual(self.error_of(self.host), code, repr(bad))
            self.assertEqual(len(self.host.sent), 1)
        self.send(self.host, 'VERSION_RESTORE', {})
        self.assertEqual(self.error_of(self.host), 'BAD_FORMAT')
        self.assertEqual((self.ids(), self.room.board.seq), (['a', 'c'], self.old_seq))
        self.assertEqual(len(self.room.versions.meta_list()), 1)

    def test_version_without_contents_cannot_be_restored(self):
        self.room.versions.load_meta([{'id': 77, 'name': 'old one', 'by': 'x', 'ts': 1, 'seq': 1, 'count': 1}])
        self.send(self.host, 'VERSION_RESTORE', {'id': 77})
        self.assertEqual(self.error_of(self.host), 'NOT_FOUND')
        self.assertEqual((self.ids(), len(self.room.versions.meta_list())), (['a', 'c'], 2))

    def test_clear_then_restore_gets_the_board_back(self):
        self.send(self.host, 'OP_CLEAR', {})
        before_clear = [m for m in self.room.versions.meta_list() if m['name'] == 'before clear'][0]
        self.send(self.host, 'VERSION_RESTORE', {'id': before_clear['id']})
        self.assertEqual(self.ids(), ['a', 'c'])


class ManageTests(VersionsBase):
    """VERSION_DELETE and VERSION_RENAME (contract revision 6)."""

    def setUp(self):
        """Two versions, 'first' (id 1) and 'second' (id 2), saved by alice."""
        super().setUp()
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'first'})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'second'})
        self.clear_inboxes()

    def test_host_renames_and_everyone_sees_it_at_once(self):
        self.send(self.host, 'VERSION_RENAME', {'id': 1, 'name': '  Final design  '})
        for s in self.everyone:
            self.assertEqual(self.names(s), ['Final design', 'second'], s.member.name)
            self.assertEqual(len(s.sent), 1, s.member.name)
        meta = self.room.versions.meta_list()[0]
        self.assertEqual((meta['id'], meta['by'], meta['count']), (1, 'alice', 1))   # only the name changed
        self.assertEqual(len(self.room.versions.get_objects(1)), 1)                  # the contents too

    def test_rename_name_limits(self):
        self.send(self.host, 'VERSION_RENAME', {'id': 1, 'name': 'x' * versions.MAX_NAME_LEN})     # 60: fine
        self.assertEqual(self.room.versions.get_meta(1)['name'], 'x' * 60)
        for name in (None, 5, '', '   ', '\n', ['a'], 'x' * 61, 'bell\x07'):
            self.host.sent.clear()
            self.send(self.host, 'VERSION_RENAME', {'id': 1, 'name': name})
            self.assertEqual(self.error_of(self.host), 'BAD_FORMAT', repr(name))
            self.assertEqual(len(self.host.sent), 1)
        self.send(self.host, 'VERSION_RENAME', {'id': 1})
        self.assertEqual(self.error_of(self.host), 'BAD_FORMAT')
        self.assertEqual(self.room.versions.get_meta(1)['name'], 'x' * 60)       # nothing was changed

    def test_host_deletes_and_everyone_sees_it_at_once(self):
        self.send(self.host, 'VERSION_DELETE', {'id': 1})
        for s in self.everyone:
            self.assertEqual(self.names(s), ['second'], s.member.name)
            self.assertEqual(len(s.sent), 1, s.member.name)
        self.assertIsNone(self.room.versions.get_objects(1))
        self.assertEqual(self.room.versions.get_meta(2)['name'], 'second')

    def test_unknown_or_malformed_id(self):
        for kind in ('VERSION_DELETE', 'VERSION_RENAME'):
            for bad, code in ((999, 'NOT_FOUND'), (True, 'BAD_FORMAT'), ('1', 'BAD_FORMAT'), (None, 'BAD_FORMAT'),
                              (1.5, 'BAD_FORMAT')):
                self.host.sent.clear()
                self.send(self.host, kind, {'id': bad, 'name': 'ok'})
                self.assertEqual(self.error_of(self.host), code, '%s %r' % (kind, bad))
                self.assertEqual(len(self.host.sent), 1)
            self.send(self.host, kind, {'name': 'ok'})
            self.assertEqual(self.error_of(self.host), 'BAD_FORMAT')
        self.assertEqual([v['name'] for v in self.room.versions.meta_list()], ['first', 'second'])
        self.assertEqual([s.sent for s in (self.alice, self.bob, self.view)], [[], [], []])   # nobody else heard

    def test_editor_and_viewer_cannot_rename_or_delete(self):
        """A raw message from a non-host is FORBIDDEN and changes nothing (the table is the test copy of C4)."""
        for who in (self.alice, self.view):
            self.send(who, 'VERSION_RENAME', {'id': 1, 'name': 'hacked'})
            self.assertEqual(self.error_of(who), 'FORBIDDEN')
            self.send(who, 'VERSION_DELETE', {'id': 1})
            self.assertEqual(self.error_of(who), 'FORBIDDEN')
        self.assertEqual([v['name'] for v in self.room.versions.meta_list()], ['first', 'second'])
        self.assertEqual([s.sent for s in (self.host, self.bob)], [[], []])

    def test_the_real_permission_table_also_refuses_non_hosts(self):
        """Same as above with the REAL permissions.can (HandlerBase swaps in a copy of the table)."""
        permissions.can = self._can
        for who in (self.alice, self.bob, self.view):
            self.send(who, 'VERSION_RENAME', {'id': 1, 'name': 'hacked'})
            self.assertEqual(self.error_of(who), 'FORBIDDEN')
            self.send(who, 'VERSION_DELETE', {'id': 1})
            self.assertEqual(self.error_of(who), 'FORBIDDEN')
        self.assertEqual([v['name'] for v in self.room.versions.meta_list()], ['first', 'second'])
        self.send(self.host, 'VERSION_RENAME', {'id': 2, 'name': 'renamed'})        # the host may
        self.send(self.host, 'VERSION_DELETE', {'id': 1})
        self.assertEqual([v['name'] for v in self.room.versions.meta_list()], ['renamed'])

    def test_a_renamed_periodic_version_is_no_longer_periodic(self):
        self.room.versions.save('auto', 'server', auto=True)
        self.send(self.host, 'VERSION_RENAME', {'id': 3, 'name': 'keep me'})
        self.assertFalse(self.room.versions.entries[-1]['auto'])

    def test_both_wake_the_autosave_thread(self):
        for kind, payload in (('VERSION_RENAME', {'id': 1, 'name': 'n'}), ('VERSION_DELETE', {'id': 2})):
            versions._wake.clear()
            self.send(self.host, kind, payload)
            self.assertTrue(versions._wake.is_set(), kind)
        versions._wake.clear()

    def test_next_id_is_never_reused(self):
        self.send(self.host, 'VERSION_DELETE', {'id': 2})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'third'})
        self.assertEqual([v['id'] for v in self.room.versions.meta_list()], [1, 3])


class PeriodicTests(VersionsBase):
    """The version every 5 minutes, only when the board changed."""

    def setUp(self):
        """A board with one object and a known clock."""
        super().setUp()
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.t0 = self.room.versions.next_auto_ts
        self.clear_inboxes()

    def tick(self, now, room=None):
        """Run the periodic check for one room (this test's room by default); returns 1 if a version was taken."""
        return int(versions.auto_version_due(room or self.room, now))

    def test_not_due_yet(self):
        self.assertEqual(self.tick(self.t0 - 1), 0)
        self.assertEqual(self.room.versions.meta_list(), [])

    def test_due_and_changed_makes_one_auto_version_for_everyone(self):
        self.assertEqual(self.tick(self.t0 + 1), 1)
        (v,) = self.room.versions.meta_list()
        self.assertEqual((v['name'], v['by'], v['count']), ('auto', 'server', 1))
        for s in self.everyone:
            self.assertEqual(self.names(s), ['auto'])
        self.assertEqual(self.room.versions.next_auto_ts, self.t0 + 1 + versions.AUTO_VERSION_INTERVAL_S)

    def test_nothing_changed_since_the_last_version(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'manual'})       # board is at its seq
        self.clear_inboxes()
        self.assertEqual(self.tick(self.t0 + 1), 0)
        self.assertEqual([s.sent for s in self.everyone], [[], [], [], []])
        self.send(self.alice, 'OP_ADD', {'obj': rect('b')})
        self.assertEqual(self.tick(self.t0 + 1 + versions.AUTO_VERSION_INTERVAL_S), 1)

    def test_untouched_board_gets_none(self):
        room = rooms.manager.create_room()
        self.assertEqual(self.tick(room.versions.next_auto_ts + 1, room), 0)
        self.assertEqual(room.versions.meta_list(), [])

    def test_tick_covers_every_live_room(self):
        other = rooms.manager.create_room()
        FakeSession(other, 'zed', 'host')
        self.send(self.alice, 'OP_ADD', {'obj': rect('a2')})
        self.assertGreaterEqual(versions.auto_versions_tick(self.t0 + 1), 1)
        self.assertEqual([m['name'] for m in self.room.versions.meta_list()], ['auto'])
        self.assertEqual(other.versions.meta_list(), [])        # its board was never touched

    def test_periodic_versions_are_dropped_first_over_the_limit(self):
        store = self.room.versions
        store.save('keep me', 'alice')
        for i in range(versions.MAX_VERSIONS + 5):
            store.save('auto', 'server', auto=True)
        names = [m['name'] for m in store.meta_list()]
        self.assertEqual(len(names), versions.MAX_VERSIONS)
        self.assertIn('keep me', names)
        self.assertEqual(len(store.dropped), 6)


class DiskTests(VersionsBase):
    """versions/<id>.json files."""

    def setUp(self):
        """HandlerBase room plus a private data folder."""
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.folder = os.path.join(self.dir, self.room.code, 'versions')

    def tearDown(self):
        """Remove the folder and restore the registries."""
        self._tmp.cleanup()
        super().tearDown()

    def restart(self):
        """Save everything, forget every room in memory, load the saves; return the restored room."""
        versions.save_room(self.room, self.dir)
        code = self.room.code
        with rooms.manager.lock:
            rooms.manager.rooms.clear()
        self.assertEqual(versions.restore_rooms(self.dir), [code])
        return rooms.manager.get(code)

    def test_version_is_written_to_its_own_file(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.assertFalse(os.path.exists(self.folder))               # handlers never write: the thread does
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(os.listdir(self.folder), ['1.json'])
        with open(os.path.join(self.folder, '1.json'), encoding='utf-8') as f:
            doc = json.load(f)
        self.assertEqual((doc['format'], doc['meta']['name'], doc['meta']['by'], doc['meta']['count']), (1, 'one', 'alice', 1))
        self.assertEqual([o['id'] for o in doc['objects']], ['a'])
        mtime = os.stat(os.path.join(self.folder, '1.json')).st_mtime_ns
        self.send(self.alice, 'OP_ADD', {'obj': rect('b')})         # autosave again: the version file is not rewritten
        versions.save_room(self.room, self.dir)
        self.assertEqual(os.stat(os.path.join(self.folder, '1.json')).st_mtime_ns, mtime)

    def test_saving_a_version_wakes_the_thread(self):
        versions._wake.clear()
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.assertTrue(versions._wake.is_set())
        versions._wake.clear()

    def test_versions_survive_a_restart_and_can_be_restored(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('a', x=20)})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.send(self.alice, 'OP_ADD', {'obj': rect('b')})
        self.send(self.host, 'OP_CLEAR', {})                         # 'before clear' holds a and b
        metas = self.room.versions.meta_list()
        room = self.restart()
        self.assertEqual(room.versions.meta_list(), metas)
        for m in metas:
            self.assertIsNotNone(room.versions.get_objects(m['id']), m['name'])
        self.assertEqual(room.versions.next_id, max(m['id'] for m in metas) + 1)
        # restore in the restored room, with a session of that room
        host = FakeSession(room, 'host', 'host')
        self.send(host, 'VERSION_RESTORE', {'id': metas[1]['id']})   # 'before clear'
        self.assertEqual(list(room.board.objects), ['a', 'b'])

    def test_delete_removes_the_file_and_survives_a_restart(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'two'})
        versions.save_room(self.room, self.dir)
        self.assertEqual(sorted(os.listdir(self.folder)), ['1.json', '2.json'])
        self.send(self.host, 'VERSION_DELETE', {'id': 1})
        self.assertTrue(versions.save_room(self.room, self.dir))     # deleting counts as a change of the room
        self.assertEqual(os.listdir(self.folder), ['2.json'])
        self.assertEqual(self.room.versions.dropped, [])
        room = self.restart()
        self.assertEqual([m['name'] for m in room.versions.meta_list()], ['two'])

    def test_delete_before_the_file_was_written(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.send(self.host, 'VERSION_DELETE', {'id': 1})            # the autosave thread never saw it: no file, no crash
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(os.listdir(self.folder) if os.path.isdir(self.folder) else [], [])

    def test_rename_rewrites_the_file_and_survives_a_restart(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        versions.save_room(self.room, self.dir)
        self.send(self.host, 'VERSION_RENAME', {'id': 1, 'name': 'Better name'})
        self.assertTrue(versions.save_room(self.room, self.dir))
        with open(os.path.join(self.folder, '1.json'), encoding='utf-8') as f:
            doc = json.load(f)
        self.assertEqual((doc['meta']['name'], [o['id'] for o in doc['objects']]), ('Better name', ['a']))
        room = self.restart()
        self.assertEqual([m['name'] for m in room.versions.meta_list()], ['Better name'])
        self.assertIsNotNone(room.versions.get_objects(1))

    def test_rename_while_the_file_is_being_written_is_not_lost(self):
        """A rename that lands while the old name is being written leaves the file pending for the next save."""
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        real = versions.write_atomic

        def rename_during_write(path, text):
            """Write the old name, then let the host rename the version before the autosave marks it written."""
            real(path, text)
            if path.endswith('1.json'):
                self.send(self.host, 'VERSION_RENAME', {'id': 1, 'name': 'new name'})

        from unittest import mock
        with mock.patch('server.versions.write_atomic', side_effect=rename_during_write):
            versions.save_room(self.room, self.dir)
        self.assertFalse(self.room.versions.entries[0]['on_disk'])   # still pending: the file has the old name
        self.assertTrue(versions.save_room(self.room, self.dir))
        with open(os.path.join(self.folder, '1.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['meta']['name'], 'new name')

    def test_periodic_flag_survives_a_restart(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.room.versions.save('auto', 'server', auto=True)
        room = self.restart()
        self.assertTrue(room.versions.entries[0]['auto'])

    def test_dropped_version_files_are_deleted(self):
        store = self.room.versions
        for i in range(versions.MAX_VERSIONS):
            store.save('v%d' % i, 'host')
        versions.save_room(self.room, self.dir)
        self.assertEqual(len(os.listdir(self.folder)), versions.MAX_VERSIONS)
        store.save('one more', 'host')                              # the oldest goes
        versions.save_room(self.room, self.dir)
        files = os.listdir(self.folder)
        self.assertEqual(len(files), versions.MAX_VERSIONS)
        self.assertNotIn('1.json', files)
        self.assertIn('%d.json' % (versions.MAX_VERSIONS + 1), files)
        self.assertEqual(store.dropped, [])

    def test_version_dropped_before_it_was_written(self):
        store = self.room.versions
        for i in range(versions.MAX_VERSIONS + 1):
            store.save('v%d' % i, 'host')
        self.assertTrue(versions.save_room(self.room, self.dir))     # id 1 never had a file: nothing to delete, no crash
        self.assertEqual(len(os.listdir(self.folder)), versions.MAX_VERSIONS)

    def test_failed_write_is_retried(self):
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        from unittest import mock
        with mock.patch('server.versions.os.replace', side_effect=OSError('no')):
            self.assertFalse(versions.save_room(self.room, self.dir))
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.room.code, 'autosave.json')))
        self.assertFalse(self.room.versions.entries[0]['on_disk'])
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(os.listdir(self.folder), ['1.json'])

    def test_damaged_version_files_are_ignored(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('a')})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'one'})
        self.send(self.alice, 'VERSION_SAVE', {'name': 'two'})
        versions.save_room(self.room, self.dir)
        with open(os.path.join(self.folder, '1.json'), 'w', encoding='utf-8') as f:
            f.write('{"format": 1, "meta"')                            # cut off
        with open(os.path.join(self.folder, '5.json'), 'w', encoding='utf-8') as f:
            json.dump({'format': 1, 'meta': {'id': 9, 'name': 'liar'}, 'objects': []}, f)   # id differs from the file name
        with open(os.path.join(self.folder, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('hello')
        room = self.restart()
        # version 1 is still listed from autosave.json (metadata only) but cannot be restored; 2 is complete
        self.assertEqual([m['id'] for m in room.versions.meta_list()], [1, 2])
        self.assertIsNone(room.versions.get_objects(1))
        self.assertIsNotNone(room.versions.get_objects(2))

    def test_autosave_of_the_earlier_server_version_still_loads(self):
        """autosave.json with version metadata and no versions/ folder: listed, not restorable."""
        code = 'ABCDEF'
        os.makedirs(os.path.join(self.dir, code))
        doc = {'format': 1, 'room': code, 'seq': 4, 'objects': [dict(rect('a'), owner='x', z=1)],
               'versions': [{'id': 3, 'name': 'before clear', 'by': 'h', 'ts': 1.0, 'seq': 2, 'count': 5}]}
        with open(os.path.join(self.dir, code, 'autosave.json'), 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        with rooms.manager.lock:
            rooms.manager.rooms.clear()
        self.assertEqual(versions.restore_rooms(self.dir), [code])
        room = rooms.manager.get(code)
        self.assertEqual([m['id'] for m in room.versions.meta_list()], [3])
        self.assertIsNone(room.versions.get_objects(3))
        self.assertEqual(room.versions.next_id, 4)


if __name__ == '__main__':
    unittest.main()
