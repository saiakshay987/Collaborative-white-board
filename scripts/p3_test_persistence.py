# OWNER: P3
"""p3_test_persistence.py: Tests for 'before clear' versions and autosave/restore (server/versions.py).

Run from the project root:  python scripts/p3_test_persistence.py

Uses the fake sessions of p3_test_board.py (no sockets). Every test writes into
its own temporary folder, never into the real server_data/.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p3_test_board as base  # noqa: E402  (sets WB_DATA_DIR to a temp folder before server.versions loads)
from p3_test_board import FakeSession, HandlerBase, rect  # noqa: E402

from server import chat, rooms, versions  # noqa: E402,F401  (chat: registers room.chat, as server/bootstrap.py does)


def setUpModule():
    """Stop the autosave thread that importing server.versions started; the tests call its functions directly."""
    versions.stop()


class ClearVersionTests(HandlerBase):
    """OP_CLEAR takes the automatic version 'before clear' first."""

    def test_clear_saves_version_before_applying(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.send(self.bob, 'OP_ADD', {'obj': rect('bob-1')})
        seq_before_clear = self.room.board.seq
        self.send(self.host, 'OP_CLEAR', {})
        self.assertEqual(list(self.room.board.objects), [])
        metas = self.room.versions.meta_list()
        self.assertEqual(len(metas), 1)
        meta = metas[0]
        self.assertEqual((meta['name'], meta['by'], meta['count'], meta['seq']),
                         ('before clear', 'host', 2, seq_before_clear))
        # the saved version really holds the board as it was
        saved = self.room.versions.get_objects(meta['id'])
        self.assertEqual([o['id'] for o in saved], ['alice-1', 'bob-1'])

    def test_versions_message_comes_before_op_clear_for_everyone(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        for s in self.everyone:
            s.sent.clear()
        self.send(self.host, 'OP_CLEAR', {})
        for s in self.everyone:
            self.assertEqual([t for t, _, _ in s.sent], ['VERSIONS', 'OP_CLEAR'], s.member.name)
            payload = s.of_type('VERSIONS')[0][0]
            self.assertEqual([v['name'] for v in payload['versions']], ['before clear'])
            self.assertEqual(set(payload['versions'][0]), {'id', 'name', 'by', 'ts', 'seq', 'count'})

    def test_rejected_clear_saves_no_version(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        seq = self.room.board.seq
        for s in self.everyone:
            s.sent.clear()
        self.send(self.bob, 'OP_CLEAR', {})              # an editor is not allowed
        self.assertEqual(self.error_of(self.bob), 'FORBIDDEN')
        self.assertEqual(self.room.versions.meta_list(), [])
        self.assertEqual(self.room.board.seq, seq)
        self.assertEqual(self.host.sent, [])

    def test_version_is_a_deep_copy(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1', x=20)})
        self.send(self.host, 'OP_CLEAR', {})
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1', x=900)})   # same id again after the clear
        vid = self.room.versions.meta_list()[0]['id']
        self.assertEqual(self.room.versions.get_objects(vid)[0]['x'], 20)

    def test_ids_increase_and_old_versions_are_dropped_over_the_limit(self):
        store = self.room.versions
        for i in range(versions.MAX_VERSIONS + 3):
            store.save('v%d' % i, 'host')
        metas = store.meta_list()
        self.assertEqual(len(metas), versions.MAX_VERSIONS)
        self.assertEqual(metas[0]['name'], 'v3')
        self.assertEqual([m['id'] for m in metas], sorted(m['id'] for m in metas))
        self.assertIsNone(store.get_objects(1))          # dropped


class AutosaveTests(HandlerBase):
    """Writing autosave.json."""

    def setUp(self):
        """HandlerBase room plus a private data folder."""
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, self.room.code, 'autosave.json')

    def tearDown(self):
        """Remove the folder and restore the registries."""
        self._tmp.cleanup()
        super().tearDown()

    def read(self):
        """The parsed autosave.json of the room."""
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def test_file_has_objects_seq_and_version_metadata(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.send(self.host, 'OP_CLEAR', {})
        self.send(self.bob, 'OP_ADD', {'obj': rect('bob-1')})
        self.assertTrue(versions.save_room(self.room, self.dir))
        doc = self.read()
        self.assertEqual((doc['format'], doc['room'], doc['seq']), (1, self.room.code, self.room.board.seq))
        self.assertEqual([o['id'] for o in doc['objects']], ['bob-1'])
        self.assertEqual(doc['objects'][0]['owner'], 'bob')          # owner and z are saved too
        self.assertEqual([v['name'] for v in doc['versions']], ['before clear'])
        self.assertNotIn('objects', doc['versions'][0])              # metadata only

    def test_unchanged_room_is_not_written_again(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.assertTrue(versions.save_room(self.room, self.dir))
        mtime = os.stat(self.path).st_mtime_ns
        self.assertFalse(versions.save_room(self.room, self.dir))
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-2')})    # now it changed
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(len(self.read()['objects']), 2)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        versions.save_room(self.room, self.dir)
        self.assertEqual(os.listdir(os.path.join(self.dir, self.room.code)), ['autosave.json'])
        # A failure in the last step (the rename) keeps the OLD file complete and removes the temp file.
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-2')})
        with mock.patch('server.versions.os.replace', side_effect=OSError('disk says no')):
            self.assertFalse(versions.save_room(self.room, self.dir))
        self.assertEqual(os.listdir(os.path.join(self.dir, self.room.code)), ['autosave.json'])
        self.assertEqual([o['id'] for o in self.read()['objects']], ['alice-1'])
        # the failed room is still "unsaved", so the next tick tries again and succeeds
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(len(self.read()['objects']), 2)

    def test_leaving_the_last_member_wakes_the_autosave_thread(self):
        versions._wake.clear()
        for s in (self.alice, self.bob, self.view):
            rooms.manager.member_left(self.room, s.member, remove=True, reason='leave')
        self.assertFalse(versions._wake.is_set())        # the host is still online
        rooms.manager.member_left(self.room, self.host.member, remove=True, reason='leave')
        self.assertTrue(versions._wake.is_set())
        versions._wake.clear()

    def test_deleted_room_gets_its_last_save(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        with rooms.manager.lock:
            del rooms.manager.rooms[self.room.code]      # what RoomManager.sweep does
        self.assertFalse(os.path.exists(self.path))
        versions.save_all(self.dir)
        self.assertTrue(os.path.exists(self.path))
        self.assertNotIn(self.room, versions._known)     # forgotten after the last save

    def test_autosave_thread_writes_changes_and_restores_first(self):
        """The real thread: waits for bootstrap, restores, then saves on its tick."""
        with tempfile.TemporaryDirectory() as data, \
                mock.patch.dict(os.environ, {'WB_DATA_DIR': data}), \
                mock.patch.object(versions, 'AUTOSAVE_INTERVAL_S', 0.05), \
                mock.patch.object(versions, '_bootstrap_done', lambda: True):
            self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
            versions.start()
            try:
                path = os.path.join(data, self.room.code, 'autosave.json')
                deadline = time.time() + 5
                while not os.path.exists(path) and time.time() < deadline:
                    time.sleep(0.02)
                with open(path, encoding='utf-8') as f:
                    self.assertEqual([o['id'] for o in json.load(f)['objects']], ['alice-1'])
            finally:
                versions.stop()


class RestoreTests(HandlerBase):
    """Loading autosave.json files when the server starts."""

    def setUp(self):
        """HandlerBase room plus a private data folder."""
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        """Remove the folder and restore the registries."""
        self._tmp.cleanup()
        super().tearDown()

    def restart_server(self):
        """Forget every room in memory, like a new server process."""
        with rooms.manager.lock:
            rooms.manager.rooms.clear()

    def test_room_survives_a_restart(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1', x=20)})
        self.send(self.bob, 'OP_ADD', {'obj': rect('bob-1', x=40)})
        self.send(self.host, 'OP_CLEAR', {})
        self.send(self.bob, 'OP_ADD', {'obj': rect('bob-2', x=60)})
        code, seq = self.room.code, self.room.board.seq
        checksum, objects = self.room.board.checksum(), list(self.room.board.objects.values())
        versions.save_room(self.room, self.dir)

        self.restart_server()
        self.assertIsNone(rooms.manager.get(code))
        self.assertEqual(versions.restore_rooms(self.dir), [code])

        room = rooms.manager.get(code)
        self.assertEqual(room.code, code)
        self.assertEqual((room.board.seq, room.board.checksum()), (seq, checksum))
        self.assertEqual(list(room.board.objects.values()), objects)   # same objects, same paint order
        self.assertEqual([v['name'] for v in room.versions.meta_list()], ['before clear'])
        # every on_room_created hook ran for the restored room (board, undo, versions, chat)
        self.assertIsNotNone(room.undo)
        self.assertIsNotNone(room.chat)
        self.assertEqual(room.members, {})            # no members, no password after a restart

    def test_restored_room_is_usable_and_continues_the_seq(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        code, seq = self.room.code, self.room.board.seq
        versions.save_room(self.room, self.dir)
        self.restart_server()
        versions.restore_rooms(self.dir)
        room = rooms.manager.get(code)

        # the first user to join becomes host and receives the saved board as a SNAPSHOT
        carol = FakeSession.__new__(FakeSession)      # a session that is not seated yet
        carol.room, carol.sent = room, []
        member = rooms.manager.join(room, 'carol', carol)[0]
        carol.member = member
        self.assertEqual(member.role, 'host')
        rooms.manager.member_joined(room, member, False, None)
        snap = carol.of_type('SNAPSHOT')[0][0]
        self.assertEqual(snap['seq'], seq)

        # a new operation gets seq + 1
        self.send(carol, 'OP_ADD', {'obj': rect('carol-1')})
        self.assertEqual(room.board.seq, seq + 1)
        self.assertEqual(carol.of_type('OP_ADD')[-1][1]['seq'], seq + 1)
        # a client that was at the saved seq gets a (here empty) delta, an older one a SNAPSHOT
        self.assertEqual([o['seq'] for o in room.board.delta(seq)], [seq + 1])
        self.assertIsNone(room.board.delta(seq - 1))

    def test_unusable_files_are_ignored(self):
        def put(folder, text):
            os.makedirs(os.path.join(self.dir, folder), exist_ok=True)
            with open(os.path.join(self.dir, folder, 'autosave.json'), 'w', encoding='utf-8') as f:
                f.write(text)

        good = {'format': 1, 'room': 'AAAAAA', 'seq': 3, 'objects': [dict(rect('a-1'), owner='a', z=1)], 'versions': []}
        put('AAAAAA', json.dumps(good))
        put('BBBBBB', '{"format": 1, "room": "BBB')                                        # cut off
        put('CCCCCC', json.dumps(dict(good, room='DDDDDD')))                               # belongs to another room
        put('EEEEEE', json.dumps(dict(good, room='EEEEEE', seq=-1)))                       # bad seq
        put('FFFFFF', json.dumps(dict(good, room='FFFFFF', objects=[{'id': 1}])))          # bad object
        put('GGGGGG', json.dumps(dict(good, room='GGGGGG', format=99)))                    # unknown format
        put('not-a-code', json.dumps(dict(good, room='not-a-code')))                       # not a room code
        os.makedirs(os.path.join(self.dir, 'HHHHHH'))                                      # folder without a file
        self.restart_server()
        self.assertEqual(versions.restore_rooms(self.dir), ['AAAAAA'])
        self.assertEqual(list(rooms.manager.rooms), ['AAAAAA'])
        self.assertEqual(rooms.manager.get('AAAAAA').board.seq, 3)

    def test_missing_data_folder_is_fine(self):
        self.assertEqual(versions.restore_rooms(os.path.join(self.dir, 'nope')), [])

    def test_restore_does_not_replace_a_live_room(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        versions.save_room(self.room, self.dir)
        self.assertEqual(versions.restore_rooms(self.dir), [])     # the room is still in memory
        self.assertIs(rooms.manager.get(self.room.code), self.room)

    def test_new_version_ids_continue_after_a_restart(self):
        self.send(self.alice, 'OP_ADD', {'obj': rect('alice-1')})
        self.send(self.host, 'OP_CLEAR', {})
        code = self.room.code
        versions.save_room(self.room, self.dir)
        self.restart_server()
        versions.restore_rooms(self.dir)
        room = rooms.manager.get(code)
        old = room.versions.meta_list()[0]
        self.assertIsNone(room.versions.get_objects(old['id']))     # contents were not written to disk
        self.assertGreater(room.versions.save('later', 'carol')['id'], old['id'])


class StartupTests(unittest.TestCase):
    """The autosave thread's wait for server.bootstrap."""

    def test_bootstrap_done_is_false_without_bootstrap(self):
        with mock.patch.dict(sys.modules, {'server.bootstrap': None}):
            self.assertFalse(versions._bootstrap_done())

    def test_bootstrap_done_when_the_last_import_is_bound(self):
        fake = mock.Mock(spec=['chat'])
        with mock.patch.dict(sys.modules, {'server.bootstrap': fake}):
            self.assertTrue(versions._bootstrap_done())
        with mock.patch.dict(sys.modules, {'server.bootstrap': mock.Mock(spec=['sync'])}):
            self.assertFalse(versions._bootstrap_done())


if __name__ == '__main__':
    unittest.main()
