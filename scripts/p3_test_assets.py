# OWNER: P3
"""p3_test_assets.py: Tests for server/assets.py (IMG_* upload, ASSET_GET, quotas, persistence) and the image rules of board_ops.

Run from the project root:  python scripts/p3_test_assets.py

Uses the fake sessions of p3_test_board.py (no sockets). Disk tests write into their own
temporary folder, never into the real server_data/.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p3_test_board as base  # noqa: E402  (sets WB_DATA_DIR to a temp folder before server.versions loads)
from p3_test_board import FakeSession, HandlerBase, TEST_KINDS  # noqa: E402

from common import kinds_image, models  # noqa: E402
from server import assets, chat, rooms, versions  # noqa: E402,F401  (chat: registers room.chat, as server/bootstrap.py does)

CHUNK = kinds_image.CHUNK_BYTES


def setUpModule():
    """Stop the autosave thread that importing server.versions started; the tests call its functions directly."""
    versions.stop()


def png(n=100, fill=b'x'):
    """n bytes that start like a PNG file (that is all the server checks)."""
    return (b'\x89PNG\r\n\x1a\n' + fill * n)[:max(n, 8)]


def jpeg(n=100):
    """n bytes that start like a JPEG file."""
    return (b'\xff\xd8\xff\xe0' + b'y' * n)[:max(n, 4)]


def sha(data):
    """sha256 hex of data (the asset id)."""
    return hashlib.sha256(data).hexdigest()


def begin_payload(data, mime='image/png', **over):
    """A valid IMG_BEGIN payload for data, with fields overridden by `over`."""
    p = {'asset_id': sha(data), 'name': 'cat.png', 'mime': mime, 'size': len(data),
         'chunks': kinds_image.chunk_count(len(data)), 'w': 640, 'h': 480}
    p.update(over)
    return p


def chunk_payloads(data):
    """The IMG_CHUNK payloads of data, in order."""
    return [{'asset_id': sha(data), 'idx': i, 'data': base64.b64encode(data[i * CHUNK:(i + 1) * CHUNK]).decode()}
            for i in range(kinds_image.chunk_count(len(data)))]


class AssetBase(HandlerBase):
    """HandlerBase with an 'image' kind and helpers to upload and download."""

    def setUp(self):
        """Room with host, alice, bob (editors) and a viewer; the image kind is registered."""
        super().setUp()
        models.KINDS = dict(TEST_KINDS, **kinds_image.KINDS)      # HandlerBase.tearDown restores the real one

    def upload(self, who, data, mime='image/png', **over):
        """Send IMG_BEGIN, every IMG_CHUNK and IMG_END as `who`; returns the asset id."""
        self.send(who, 'IMG_BEGIN', begin_payload(data, mime, **over))
        for p in chunk_payloads(data):
            self.send(who, 'IMG_CHUNK', p)
        self.send(who, 'IMG_END', {'asset_id': sha(data), 'sha256': sha(data)})
        return sha(data)

    def clear_inboxes(self):
        """Forget what the fake sessions received so far."""
        for s in self.everyone:
            s.sent.clear()

    def types(self, session):
        """The message types the session received, in order."""
        return [t for t, _, _ in session.sent]

    def nothing_pending_or_stored(self):
        """True if the room holds no upload in progress and no picture."""
        return not self.room.assets.pending and not self.room.assets.assets


class UploadTests(AssetBase):
    """IMG_BEGIN / IMG_CHUNK / IMG_END."""

    def test_editor_uploads_a_picture_and_only_the_sender_gets_ok(self):
        data = png(2 * CHUNK + 1000)                                    # three chunks, the last one short
        self.clear_inboxes()
        aid = self.upload(self.alice, data)
        self.assertEqual(self.alice.of_type('IMG_OK')[0][0], {'asset_id': aid})
        self.assertEqual(self.types(self.alice), ['IMG_OK'])
        for s in (self.host, self.bob, self.view):
            self.assertEqual(s.sent, [], s.member.name)
        entry = self.room.assets.get(aid)
        self.assertEqual(entry['data'], data)
        meta = entry['meta']
        self.assertEqual((meta['name'], meta['mime'], meta['size'], meta['w'], meta['h'], meta['by']),
                         ('cat.png', 'image/png', len(data), 640, 480, 'alice'))
        self.assertEqual(self.room.assets.total, len(data))
        self.assertEqual(self.room.assets.pending, {})                  # the upload is finished

    def test_host_and_jpeg_work_too(self):
        data = jpeg(500)
        aid = self.upload(self.host, data, 'image/jpeg')
        self.assertEqual(self.types(self.host), ['IMG_OK'])
        self.assertEqual(self.room.assets.get(aid)['meta']['mime'], 'image/jpeg')

    def test_single_chunk_picture_and_exact_chunk_boundary(self):
        for n in (8, CHUNK, CHUNK + 1, 2 * CHUNK):
            self.room.assets.assets.clear()
            self.alice.sent.clear()
            self.upload(self.alice, png(n))
            self.assertEqual(self.types(self.alice), ['IMG_OK'], n)

    def test_the_largest_allowed_picture_is_accepted(self):
        data = png(kinds_image.MAX_IMAGE_BYTES)
        self.upload(self.alice, data)
        self.assertEqual(self.types(self.alice), ['IMG_OK'])
        self.assertEqual(self.room.assets.total, kinds_image.MAX_IMAGE_BYTES)

    def test_viewer_cannot_upload(self):
        self.send(self.view, 'IMG_BEGIN', begin_payload(png()))
        self.assertEqual(self.error_of(self.view), 'FORBIDDEN')
        self.assertTrue(self.nothing_pending_or_stored())

    def test_too_large_is_refused_at_begin_before_any_byte_arrives(self):
        big = kinds_image.MAX_IMAGE_BYTES + 1
        self.send(self.alice, 'IMG_BEGIN', {'asset_id': 'a' * 64, 'name': 'x', 'mime': 'image/png', 'size': big,
                                            'chunks': kinds_image.chunk_count(big), 'w': 10, 'h': 10})
        self.assertEqual(self.error_of(self.alice), 'TOO_LARGE')
        self.assertEqual(self.alice.of_type('ERROR')[0][0]['ref_type'], 'IMG_BEGIN')
        self.assertTrue(self.nothing_pending_or_stored())

    def test_bad_begin_payloads(self):
        good = begin_payload(png())
        bad = [
            {'asset_id': 'abc'}, {'asset_id': 'A' * 64}, {'asset_id': 'g' * 64}, {'asset_id': 'a' * 63}, {'asset_id': 5},
            {'asset_id': None}, {'asset_id': '../../etc/passwd' + 'a' * 49},
            {'mime': 'image/gif'}, {'mime': None}, {'mime': 'IMAGE/PNG'},
            {'size': 0}, {'size': -5}, {'size': True}, {'size': '100'}, {'size': 100.0}, {'size': None},
            {'chunks': 0}, {'chunks': 2}, {'chunks': '1'}, {'chunks': True}, {'chunks': None},
            {'w': 0}, {'h': 0}, {'w': 4001}, {'w': '5'}, {'h': 1.5}, {'w': True}, {'h': None},
        ]
        for change in bad:
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_BEGIN', dict(good, **change))
            self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', change)
            self.assertTrue(self.nothing_pending_or_stored(), change)
        for missing in good:
            payload = {k: v for k, v in good.items() if k != missing}
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_BEGIN', payload)
            if missing == 'name':
                self.assertEqual(self.alice.sent, [])                           # the name is only a label: optional
                self.room.assets.pending.clear()
            else:
                self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', missing)

    def test_name_is_cleaned(self):
        self.send(self.alice, 'IMG_BEGIN', begin_payload(png(), name='a\nb\x07' + 'z' * 500))
        (up,) = self.room.assets.pending.values()
        self.assertEqual(up['meta']['name'], 'ab' + 'z' * (kinds_image.MAX_NAME - 2))
        self.send(self.bob, 'IMG_BEGIN', begin_payload(png(20), name=12345))
        self.assertEqual(self.room.assets.pending[('bob', sha(png(20)))]['meta']['name'], '')

    # -- chunks ------------------------------------------------------------------

    def begun(self, who, data):
        """Open an upload of data for `who` and return its chunk payloads."""
        self.send(who, 'IMG_BEGIN', begin_payload(data))
        return chunk_payloads(data)

    def test_chunk_without_begin(self):
        self.send(self.alice, 'IMG_CHUNK', chunk_payloads(png())[0])
        self.assertEqual(self.error_of(self.alice), 'NOT_FOUND')
        self.send(self.alice, 'IMG_END', {'asset_id': sha(png()), 'sha256': sha(png())})
        self.assertEqual(self.alice.of_type('ERROR')[-1][0]['code'], 'NOT_FOUND')

    def test_bad_chunks_drop_the_whole_upload(self):
        data = png(3 * CHUNK)
        good = chunk_payloads(data)
        b64 = lambda raw: base64.b64encode(raw).decode()      # noqa: E731
        bad = {
            'out of order': dict(good[1]),
            'repeated': dict(good[0]),
            'negative idx': dict(good[0], idx=-1),
            'bool idx': dict(good[0], idx=False),
            'string idx': dict(good[0], idx='0'),
            'short chunk': dict(good[0], data=b64(data[:CHUNK - 1])),
            'long chunk': dict(good[0], data=b64(data[:CHUNK + 1])),
            'not base64': dict(good[0], data='!!!!'),
            'base64 with junk': dict(good[0], data=good[0]['data'] + '\n!'),
            'no data': {k: v for k, v in good[0].items() if k != 'data'},
            'data is a number': dict(good[0], data=5),
            'giant data': dict(good[0], data='A' * (assets.B64_CHUNK_CHARS + 4)),
        }
        for name, payload in bad.items():
            if name == 'repeated':
                self.send(self.alice, 'IMG_BEGIN', begin_payload(data))
                self.send(self.alice, 'IMG_CHUNK', good[0])
            else:
                self.send(self.alice, 'IMG_BEGIN', begin_payload(data))
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_CHUNK', payload)
            self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', name)
            self.assertEqual(self.alice.of_type('ERROR')[0][0]['ref_type'], 'IMG_CHUNK')
            self.assertTrue(self.nothing_pending_or_stored(), name)          # the upload was thrown away

    def test_last_chunk_must_have_exactly_the_remaining_length(self):
        data = png(CHUNK + 10)
        good = self.begun(self.alice, data)
        self.send(self.alice, 'IMG_CHUNK', good[0])
        self.send(self.alice, 'IMG_CHUNK', dict(good[1], data=base64.b64encode(data[CHUNK:] + b'extra').decode()))
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')

    def test_nobody_else_can_continue_or_finish_my_upload(self):
        data = png(2 * CHUNK)
        good = self.begun(self.alice, data)
        self.send(self.bob, 'IMG_CHUNK', good[0])
        self.assertEqual(self.error_of(self.bob), 'NOT_FOUND')
        self.send(self.bob, 'IMG_END', {'asset_id': sha(data), 'sha256': sha(data)})
        self.assertEqual(self.bob.of_type('ERROR')[-1][0]['code'], 'NOT_FOUND')
        self.assertEqual(self.room.assets.pending[('alice', sha(data))]['next'], 0)   # alice's upload is untouched

    # -- end ---------------------------------------------------------------------

    def end_after(self, data, end_payload, chunks=None):
        """Upload data's chunks (all, or the first `chunks`) and then send this IMG_END payload."""
        self.send(self.alice, 'IMG_BEGIN', begin_payload(data))
        for p in chunk_payloads(data)[:chunks]:
            self.send(self.alice, 'IMG_CHUNK', p)
        self.alice.sent.clear()
        self.send(self.alice, 'IMG_END', end_payload)

    def test_end_before_all_chunks(self):
        data = png(3 * CHUNK)
        self.end_after(data, {'asset_id': sha(data), 'sha256': sha(data)}, chunks=2)
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')
        self.assertEqual(self.alice.of_type('ERROR')[0][0]['ref_type'], 'IMG_END')
        self.assertTrue(self.nothing_pending_or_stored())

    def test_wrong_hash_is_refused(self):
        data = png(500)
        self.end_after(data, {'asset_id': sha(data), 'sha256': sha(b'something else')})
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')
        self.assertTrue(self.nothing_pending_or_stored())
        self.end_after(data, {'asset_id': sha(data)})                          # no hash at all
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')
        self.end_after(data, {'asset_id': sha(data), 'sha256': sha(data).upper()})
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')              # lowercase hex only

    def test_id_must_be_the_hash_of_the_bytes(self):
        """Content addressing: a client cannot store bytes under an id that is not their sha256."""
        data = png(500)
        fake_id = sha(b'another picture')
        self.send(self.alice, 'IMG_BEGIN', begin_payload(data, asset_id=fake_id))
        self.send(self.alice, 'IMG_CHUNK', dict(chunk_payloads(data)[0], asset_id=fake_id))
        self.alice.sent.clear()
        self.send(self.alice, 'IMG_END', {'asset_id': fake_id, 'sha256': sha(data)})
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')
        self.assertTrue(self.nothing_pending_or_stored())

    def test_bytes_must_match_the_declared_file_type(self):
        for data, mime in ((b'GIF89a' + b'x' * 100, 'image/png'), (png(100), 'image/jpeg'), (jpeg(100), 'image/png'),
                           (b'<html>not a picture</html>', 'image/png')):
            self.send(self.alice, 'IMG_BEGIN', begin_payload(data, mime))
            for p in chunk_payloads(data):
                self.send(self.alice, 'IMG_CHUNK', p)
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_END', {'asset_id': sha(data), 'sha256': sha(data)})
            self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', (data[:6], mime))
            self.assertTrue(self.nothing_pending_or_stored())

    def test_same_picture_twice_is_stored_once(self):
        data = png(300)
        self.upload(self.alice, data)
        rev = self.room.assets.rev
        self.alice.sent.clear()
        self.upload(self.bob, data)                                            # somebody else, same bytes
        self.assertEqual(self.types(self.bob), ['IMG_OK'])
        self.assertEqual((len(self.room.assets.assets), self.room.assets.rev, self.room.assets.total), (1, rev, 300))
        self.assertEqual(self.room.assets.get(sha(data))['meta']['by'], 'alice')    # the first uploader stays

    # -- limits and housekeeping --------------------------------------------------

    def test_new_begin_replaces_an_unfinished_upload(self):
        data = png(2 * CHUNK)
        self.send(self.alice, 'IMG_BEGIN', begin_payload(data))
        self.send(self.alice, 'IMG_CHUNK', chunk_payloads(data)[0])
        self.send(self.alice, 'IMG_BEGIN', begin_payload(data))                # start again
        self.assertEqual(self.room.assets.pending[('alice', sha(data))]['next'], 0)
        self.assertEqual(self.alice.of_type('ERROR'), [])
        self.upload(self.alice, data)
        self.assertEqual(self.types(self.alice), ['IMG_OK'])

    def test_at_most_two_uploads_in_progress_per_user(self):
        for i in (1, 2):
            self.send(self.alice, 'IMG_BEGIN', begin_payload(png(10 + i)))
        self.send(self.alice, 'IMG_BEGIN', begin_payload(png(13)))
        self.assertEqual(self.error_of(self.alice), 'TOO_LARGE')
        self.send(self.bob, 'IMG_BEGIN', begin_payload(png(14)))               # the limit is per user
        self.assertEqual(self.bob.sent, [])

    def test_unfinished_uploads_are_dropped_when_the_member_leaves(self):
        self.send(self.alice, 'IMG_BEGIN', begin_payload(png(50)))
        self.send(self.bob, 'IMG_BEGIN', begin_payload(png(60)))
        rooms.manager.member_left(self.room, self.alice.member, remove=True, reason='leave')
        self.assertEqual(list(self.room.assets.pending), [('bob', sha(png(60)))])

    def test_room_quota(self):
        with mock.patch.object(assets, 'MAX_ROOM_ASSET_BYTES', 1000):
            self.upload(self.alice, png(600))
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_BEGIN', begin_payload(png(500)))        # 600 stored + 500 > 1000
            self.assertEqual(self.error_of(self.alice), 'TOO_LARGE')
            self.alice.sent.clear()
            self.send(self.alice, 'IMG_BEGIN', begin_payload(png(300)))        # 600 + 300 fits ...
            self.send(self.bob, 'IMG_BEGIN', begin_payload(png(200)))          # ... but pending bytes count too
            self.assertEqual(self.error_of(self.bob), 'TOO_LARGE')

    def test_a_failed_upload_does_not_block_a_retry(self):
        data = png(2 * CHUNK)
        good = self.begun(self.alice, data)
        self.send(self.alice, 'IMG_CHUNK', good[1])                            # out of order: dropped
        self.alice.sent.clear()
        self.upload(self.alice, data)
        self.assertEqual(self.types(self.alice), ['IMG_OK'])


class DownloadTests(AssetBase):
    """ASSET_GET."""

    def fetch(self, who, asset_id):
        """Send ASSET_GET and return the messages that came back as (type, payload) pairs."""
        who.sent.clear()
        self.send(who, 'ASSET_GET', {'asset_id': asset_id})
        return [(t, p) for t, p, _ in who.sent]

    def test_stream_has_the_same_three_messages_as_an_upload(self):
        data = png(2 * CHUNK + 77)
        aid = self.upload(self.alice, data)
        for who in (self.view, self.bob, self.alice):                          # any member, viewers included
            got = self.fetch(who, aid)
            self.assertEqual([t for t, _ in got], ['IMG_BEGIN', 'IMG_CHUNK', 'IMG_CHUNK', 'IMG_CHUNK', 'IMG_END'])
            begin, end = got[0][1], got[-1][1]
            self.assertEqual(begin, {'asset_id': aid, 'name': 'cat.png', 'mime': 'image/png', 'size': len(data),
                                     'chunks': 3, 'w': 640, 'h': 480})
            self.assertEqual([p['idx'] for t, p in got if t == 'IMG_CHUNK'], [0, 1, 2])
            raw = b''.join(base64.b64decode(p['data']) for t, p in got if t == 'IMG_CHUNK')
            self.assertEqual(raw, data)
            self.assertTrue(all(len(base64.b64decode(p['data'])) <= CHUNK for t, p in got if t == 'IMG_CHUNK'))
            self.assertEqual(end, {'asset_id': aid, 'sha256': sha(data)})

    def test_only_the_asker_receives_it(self):
        aid = self.upload(self.alice, png(100))
        self.clear_inboxes()
        self.send(self.view, 'ASSET_GET', {'asset_id': aid})
        for s in (self.host, self.alice, self.bob):
            self.assertEqual(s.sent, [])

    def test_a_download_can_be_uploaded_again(self):
        """What the server streams is exactly what an upload accepts (the same BEGIN/CHUNK/END shapes)."""
        data = png(CHUNK + 5)
        aid = self.upload(self.alice, data)
        got = self.fetch(self.bob, aid)
        self.room.assets.assets.clear()
        for t, p in got:
            self.send(self.bob, t, p)
        self.assertEqual(self.room.assets.get(aid)['data'], data)

    def test_unknown_and_bad_ids(self):
        self.assertEqual(self.fetch(self.view, sha(b'nothing'))[0][1]['code'], 'NOT_FOUND')
        for bad in ('abc', 5, None, '../x', 'A' * 64):
            self.assertEqual(self.fetch(self.view, bad)[0][1]['code'], 'BAD_FORMAT', bad)
        self.view.sent.clear()
        self.send(self.view, 'ASSET_GET', {})
        self.assertEqual(self.error_of(self.view), 'BAD_FORMAT')

    def test_pictures_of_another_room_are_not_reachable(self):
        aid = self.upload(self.alice, png(100))
        other = rooms.manager.create_room()
        stranger = FakeSession(other, 'eve', 'host')
        self.assertEqual(self.fetch(stranger, aid)[0][1]['code'], 'NOT_FOUND')

    def test_streaming_stops_when_the_asker_is_gone(self):
        aid = self.upload(self.alice, png(5 * CHUNK))
        self.alice.sent.clear()
        sent_before_close = []

        def closing_send(type_, payload, **env):
            sent_before_close.append(type_)
            if len(sent_before_close) == 2:
                self.alice.closed = True
        self.alice.send = closing_send
        self.send(self.alice, 'ASSET_GET', {'asset_id': aid})
        self.assertEqual(sent_before_close, ['IMG_BEGIN', 'IMG_CHUNK'])        # no more after it closed


class ImageObjectTests(AssetBase):
    """board_ops: an image object may only name a picture of this room; limits of kinds_image."""

    def image(self, aid, oid='alice-1', **over):
        """An image object."""
        return dict({'id': oid, 'kind': 'image', 'x': 100, 'y': 100, 'w': 320, 'h': 240, 'asset_id': aid}, **over)

    def test_add_with_an_uploaded_picture(self):
        aid = self.upload(self.alice, png(100))
        self.clear_inboxes()
        self.send(self.alice, 'OP_ADD', {'obj': self.image(aid)}, op_id='alice-7')
        for s in self.everyone:
            (payload, env), = s.of_type('OP_ADD')
            self.assertEqual((payload['obj']['asset_id'], payload['obj']['owner'], env['op_id']), (aid, 'alice', 'alice-7'))
        self.assertIn('alice-1', self.room.board.objects)

    def test_add_without_an_upload_is_refused(self):
        self.clear_inboxes()
        self.send(self.alice, 'OP_ADD', {'obj': self.image(sha(b'never uploaded'))})
        self.assert_rejected(self.alice, 'NOT_FOUND', 0)

    def test_picture_of_another_room_does_not_count(self):
        other = rooms.manager.create_room()
        stranger = FakeSession(other, 'eve', 'host')
        aid = self.upload(stranger, png(100))
        self.send(self.alice, 'OP_ADD', {'obj': self.image(aid)})
        self.assertEqual(self.error_of(self.alice), 'NOT_FOUND')

    def test_update_may_not_point_an_image_at_a_missing_picture(self):
        aid = self.upload(self.alice, png(100))
        self.send(self.alice, 'OP_ADD', {'obj': self.image(aid)})
        seq = self.room.board.seq
        self.clear_inboxes()
        self.send(self.alice, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'asset_id': sha(b'nope')}})
        self.assert_rejected(self.alice, 'NOT_FOUND', seq)
        # moving and resizing is still fine, and so is switching to another uploaded picture
        self.send(self.alice, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'x': 500, 'w': 100}})
        other = self.upload(self.alice, png(200))
        self.send(self.alice, 'OP_UPDATE', {'obj_id': 'alice-1', 'set': {'asset_id': other}})
        self.assertEqual(self.room.board.objects['alice-1']['asset_id'], other)
        self.assertEqual(self.room.board.objects['alice-1']['x'], 500)

    def test_update_of_other_kinds_is_not_affected(self):
        self.send(self.alice, 'OP_ADD', {'obj': {'id': 'alice-2', 'kind': 'rect', 'x': 0, 'y': 0, 'w': 5, 'h': 5,
                                                 'color': '#ff0000', 'width': 2, 'fill': None}})
        self.send(self.alice, 'OP_UPDATE', {'obj_id': 'alice-2', 'set': {'x': 50}})
        self.assertEqual(self.room.board.objects['alice-2']['x'], 50)

    def test_kind_limits(self):
        aid = self.upload(self.alice, png(100))
        for over in ({'w': 0}, {'h': 0}, {'w': 4001}, {'h': -3}, {'asset_id': aid[:10]}, {'asset_id': aid + 'a'},
                     {'x': 'left'}, {'w': '320'}, {'asset_id': 5}):
            self.alice.sent.clear()
            self.send(self.alice, 'OP_ADD', {'obj': self.image(aid, **over)})
            self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT', over)
        missing = {k: v for k, v in self.image(aid).items() if k != 'asset_id'}
        self.send(self.alice, 'OP_ADD', {'obj': missing})
        self.assertEqual(self.error_of(self.alice), 'BAD_FORMAT')

    def test_viewer_cannot_add_images(self):
        aid = self.upload(self.alice, png(100))
        self.view.sent.clear()
        self.send(self.view, 'OP_ADD', {'obj': self.image(aid, 'view-1')})
        self.assertEqual(self.error_of(self.view), 'FORBIDDEN')

    def test_undo_of_a_delete_brings_the_image_back(self):
        aid = self.upload(self.alice, png(100))
        self.send(self.alice, 'OP_ADD', {'obj': self.image(aid)})
        self.send(self.alice, 'OP_DELETE', {'obj_id': 'alice-1'})
        self.send(self.alice, 'UNDO', {})
        self.assertIn('alice-1', self.room.board.objects)


class KindsTests(unittest.TestCase):
    """common/kinds_image.py helpers."""

    def test_asset_id(self):
        self.assertTrue(kinds_image.is_asset_id(sha(b'x')))
        for bad in ('', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, ('a' * 63) + '\n', '../' + 'a' * 61, None, 5, b'a' * 64):
            self.assertFalse(kinds_image.is_asset_id(bad), bad)

    def test_chunk_count(self):
        self.assertEqual([kinds_image.chunk_count(n) for n in (1, CHUNK, CHUNK + 1, 2 * CHUNK, kinds_image.MAX_IMAGE_BYTES)],
                         [1, 1, 2, 2, 214])

    def test_contract_numbers(self):
        self.assertEqual((CHUNK, kinds_image.MAX_IMAGE_BYTES, kinds_image.MAX_SIDE), (24 * 1024, 5 * 1024 * 1024, 1600))
        self.assertEqual(assets.B64_CHUNK_CHARS, 32768)                         # base64 of 24 KB

    def test_mime_matches(self):
        self.assertTrue(kinds_image.mime_matches('image/png', png()))
        self.assertTrue(kinds_image.mime_matches('image/jpeg', jpeg()))
        self.assertFalse(kinds_image.mime_matches('image/gif', b'GIF89a'))
        self.assertFalse(kinds_image.mime_matches('image/png', b''))

    def test_registry_merges_without_clash(self):
        self.assertIn('image', models.KINDS)
        self.assertEqual(models.validate_obj({'id': 'a-1', 'kind': 'image', 'x': 0, 'y': 0, 'w': 10, 'h': 10,
                                              'asset_id': 'a' * 64}), (True, ''))


class PersistenceTests(AssetBase):
    """Pictures in the autosave."""

    def setUp(self):
        """AssetBase room plus a private data folder."""
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.folder = os.path.join(self.dir, self.room.code, 'assets')

    def tearDown(self):
        """Remove the folder and restore the registries."""
        self._tmp.cleanup()
        super().tearDown()

    def restart(self):
        """Save, forget every room in memory, load the saves; returns the restored room."""
        versions.save_room(self.room, self.dir)
        code = self.room.code
        with rooms.manager.lock:
            rooms.manager.rooms.clear()
        self.assertEqual(versions.restore_rooms(self.dir), [code])
        return rooms.manager.get(code)

    def test_picture_is_written_next_to_the_autosave(self):
        data = png(2 * CHUNK)
        aid = self.upload(self.alice, data)
        self.assertFalse(os.path.exists(self.folder))                          # handlers never write: the thread does
        self.assertTrue(versions.save_room(self.room, self.dir))
        with open(os.path.join(self.folder, aid + '.bin'), 'rb') as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(os.listdir(self.folder), [aid + '.bin'])              # no temp files left
        with open(os.path.join(self.dir, self.room.code, 'autosave.json'), encoding='utf-8') as f:
            doc = json.load(f)
        (meta,) = doc['assets']
        self.assertEqual((meta['asset_id'], meta['size'], meta['mime'], meta['w'], meta['by']), (aid, len(data), 'image/png', 640, 'alice'))
        self.assertNotIn('data', meta)                                         # bytes are not inside the JSON
        mtime = os.stat(os.path.join(self.folder, aid + '.bin')).st_mtime_ns
        self.assertFalse(versions.save_room(self.room, self.dir))              # nothing changed: nothing rewritten
        self.upload(self.alice, png(50))                                       # a second picture changes the key
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertEqual(os.stat(os.path.join(self.folder, aid + '.bin')).st_mtime_ns, mtime)   # the first file is not rewritten
        self.assertEqual(len(os.listdir(self.folder)), 2)

    def test_a_new_picture_wakes_the_autosave_thread(self):
        versions._wake.clear()
        self.upload(self.alice, png(50))
        self.assertTrue(versions._wake.is_set())
        versions._wake.clear()

    def test_pictures_survive_a_restart_and_can_be_downloaded(self):
        data = png(2 * CHUNK + 5)
        aid = self.upload(self.alice, data)
        self.send(self.alice, 'OP_ADD', {'obj': {'id': 'alice-1', 'kind': 'image', 'x': 1, 'y': 2, 'w': 3, 'h': 4, 'asset_id': aid}})
        room = self.restart()
        entry = room.assets.get(aid)
        self.assertEqual(entry['data'], data)
        self.assertEqual((entry['meta']['name'], entry['meta']['by'], entry['meta']['w']), ('cat.png', 'alice', 640))
        self.assertEqual(room.assets.total, len(data))
        self.assertIn('alice-1', room.board.objects)
        carol = FakeSession(room, 'carol', 'host')
        carol.sent.clear()
        self.send(carol, 'ASSET_GET', {'asset_id': aid})
        self.assertEqual([t for t, _, _ in carol.sent], ['IMG_BEGIN'] + ['IMG_CHUNK'] * 3 + ['IMG_END'])
        self.send(carol, 'OP_ADD', {'obj': {'id': 'carol-1', 'kind': 'image', 'x': 1, 'y': 2, 'w': 3, 'h': 4, 'asset_id': aid}})
        self.assertIn('carol-1', room.board.objects)                           # the restored picture counts as uploaded

    def test_damaged_files_are_skipped(self):
        good, flipped, short, missing, wrong_type = png(100), png(200), png(300), png(400), png(500)
        ids = {name: self.upload(self.alice, d) for name, d in
               (('good', good), ('flipped', flipped), ('short', short), ('missing', missing), ('wrong', wrong_type))}
        versions.save_room(self.room, self.dir)
        path = lambda name: os.path.join(self.folder, ids[name] + '.bin')       # noqa: E731
        with open(path('flipped'), 'r+b') as f:
            f.seek(50)
            f.write(b'Z')                                                      # same size, different bytes: the hash fails
        with open(path('short'), 'r+b') as f:
            f.truncate(100)
        os.remove(path('missing'))
        with open(path('wrong'), 'wb') as f:
            f.write(b'GIF89a' + b'x' * 494)                                    # also fails the hash
        room = self.restart_without_saving()
        self.assertEqual(list(room.assets.assets), [ids['good']])
        self.assertEqual(room.assets.total, 100)

    def restart_without_saving(self):
        """Like restart(), but keep the files exactly as the test left them."""
        code = self.room.code
        with rooms.manager.lock:
            rooms.manager.rooms.clear()
        self.assertEqual(versions.restore_rooms(self.dir), [code])
        return rooms.manager.get(code)

    def test_hostile_metadata_is_ignored(self):
        """autosave.json edited by hand: path tricks and nonsense must not load or escape the folder."""
        data = png(100)
        aid = self.upload(self.alice, data)
        versions.save_room(self.room, self.dir)
        auto = os.path.join(self.dir, self.room.code, 'autosave.json')
        with open(auto, encoding='utf-8') as f:
            doc = json.load(f)
        good = doc['assets'][0]
        doc['assets'] = [dict(good, asset_id='../../autosave'), dict(good, asset_id='x'), dict(good, mime='text/html'),
                         dict(good, size='100'), dict(good, w=None), 'junk', 5, None, good, good]       # the good one twice
        with open(auto, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        room = self.restart_without_saving()
        self.assertEqual(list(room.assets.assets), [aid])

    def test_autosave_without_assets_still_loads(self):
        """An autosave.json written before pictures existed has no 'assets' key."""
        self.send(self.alice, 'OP_ADD', {'obj': {'id': 'a', 'kind': 'rect', 'x': 0, 'y': 0, 'w': 5, 'h': 5,
                                                 'color': '#ff0000', 'width': 2, 'fill': None}})
        versions.save_room(self.room, self.dir)
        auto = os.path.join(self.dir, self.room.code, 'autosave.json')
        with open(auto, encoding='utf-8') as f:
            doc = json.load(f)
        del doc['assets']
        with open(auto, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        room = self.restart_without_saving()
        self.assertEqual((list(room.board.objects), room.assets.assets), (['a'], {}))

    def test_failed_write_is_retried(self):
        aid = self.upload(self.alice, png(100))
        with mock.patch('server.versions.os.replace', side_effect=OSError('no')):
            self.assertFalse(versions.save_room(self.room, self.dir))
        self.assertFalse(self.room.assets.get(aid)['on_disk'])
        self.assertTrue(versions.save_room(self.room, self.dir))
        self.assertTrue(os.path.exists(os.path.join(self.folder, aid + '.bin')))


if __name__ == '__main__':
    unittest.main()
