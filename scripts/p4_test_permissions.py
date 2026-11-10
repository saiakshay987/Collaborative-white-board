# OWNER: P4
"""p4_test_permissions.py: unit tests for permissions.can, ChatLog, SET_ROLE, CHAT and the chat hooks.

Run from the project root with either
    python scripts/p4_test_permissions.py
    python -m unittest scripts.p4_test_permissions
No network is used: sessions, rooms and members are small fakes.
"""

import os
import sys
import threading
import unittest
from unittest import mock

# Allow `python scripts/p4_test_permissions.py` (script dir is not the root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import protocol  # noqa: E402
from server import chat, permissions, presence  # noqa: E402


# --- permissions.can ---------------------------------------------------------

# Expected table, written out independently of permissions._TABLE.
EXPECTED = {
    "host": {"draw", "undo", "upload", "version_save", "chat", "cursor",
             "clear", "version_restore", "set_role", "kick"},
    "editor": {"draw", "undo", "upload", "version_save", "chat", "cursor"},
    "viewer": {"chat", "cursor"},
}


class PermissionTableTest(unittest.TestCase):
    """Checks permissions.can against contract C4."""

    def test_every_role_action_pair(self):
        """Every role x action pair gives exactly the contract answer."""
        for role, allowed in EXPECTED.items():
            for action in permissions.ACTIONS:
                with self.subTest(role=role, action=action):
                    self.assertEqual(permissions.can(role, action), action in allowed)

    def test_actions_set_is_the_contract_one(self):
        """ACTIONS holds the ten contract actions."""
        self.assertEqual(set(permissions.ACTIONS), EXPECTED["host"])

    def test_unknown_role_or_action_is_denied(self):
        """Default deny for unknown or malformed role/action values."""
        self.assertFalse(permissions.can("admin", "draw"))
        self.assertFalse(permissions.can("host", "format_disk"))
        self.assertFalse(permissions.can(None, "draw"))
        self.assertFalse(permissions.can("host", None))
        self.assertFalse(permissions.can(["host"], "draw"))
        self.assertFalse(permissions.can("host", ["draw"]))
        self.assertFalse(permissions.can("", ""))


# --- ChatLog -----------------------------------------------------------------

class ChatLogTest(unittest.TestCase):
    """Checks message shapes, DM privacy, limits and ordering of ChatLog."""

    def test_message_shapes(self):
        """Room messages have no `to`; DMs have one; ids are unique."""
        log = chat.ChatLog()
        room_msg = log.add_room("alice", "hi all")
        dm = log.add_dm("alice", "bob", "psst")
        self.assertEqual(set(room_msg), {"msg_id", "type", "from", "text", "ts"})
        self.assertEqual(room_msg["type"], "CHAT")
        self.assertEqual(set(dm), {"msg_id", "type", "from", "to", "text", "ts"})
        self.assertEqual(dm["type"], "DM")
        self.assertEqual(dm["to"], "bob")
        self.assertNotEqual(room_msg["msg_id"], dm["msg_id"])

    def test_dm_privacy(self):
        """Only sender and recipient get a DM in their history."""
        log = chat.ChatLog()
        log.add_room("alice", "hello")
        log.add_dm("alice", "bob", "secret")
        texts = lambda user: [m["text"] for m in log.history_for(user)]  # noqa: E731
        self.assertEqual(texts("alice"), ["hello", "secret"])
        self.assertEqual(texts("bob"), ["hello", "secret"])
        self.assertEqual(texts("carol"), ["hello"])
        self.assertEqual(texts("nobody"), ["hello"])

    def test_room_history_limit_is_200(self):
        """history_for returns only the last 200 room messages."""
        log = chat.ChatLog()
        for i in range(250):
            log.add_room("alice", "m%d" % i)
        history = log.history_for("alice")
        self.assertEqual(len(history), 200)
        self.assertEqual(history[0]["text"], "m50")
        self.assertEqual(history[-1]["text"], "m249")

    def test_room_limit_does_not_drop_own_dms(self):
        """An old DM stays in history even when 200+ newer room messages exist."""
        log = chat.ChatLog()
        log.add_dm("alice", "bob", "old dm")
        for i in range(210):
            log.add_room("carol", "r%d" % i)
        texts = [m["text"] for m in log.history_for("bob")]
        self.assertEqual(len(texts), 201)
        self.assertEqual(texts[0], "old dm")

    def test_keeps_at_most_500(self):
        """The log never holds more than 500 messages (oldest dropped first)."""
        log = chat.ChatLog()
        for i in range(520):
            log.add_dm("alice", "bob", "d%d" % i)
        history = log.history_for("bob")
        self.assertEqual(len(history), 500)
        self.assertEqual(history[0]["text"], "d20")

    def test_history_sorted_by_ts(self):
        """Room messages and DMs are merged in timestamp order."""
        log = chat.ChatLog()
        with mock.patch("server.chat.time.time", side_effect=[10.0, 20.0, 30.0, 40.0]):
            log.add_room("a", "one")
            log.add_dm("a", "b", "two")
            log.add_room("c", "three")
            log.add_dm("b", "a", "four")
        self.assertEqual([m["text"] for m in log.history_for("a")],
                         ["one", "two", "three", "four"])

    def test_returned_dicts_are_copies(self):
        """Mutating a returned message must not change the stored log."""
        log = chat.ChatLog()
        returned = log.add_room("alice", "original")
        returned["text"] = "tampered"
        log.history_for("alice")[0]["text"] = "tampered too"
        self.assertEqual(log.history_for("alice")[0]["text"], "original")

    def test_concurrent_adds_get_unique_ids(self):
        """Many threads adding at once never produce a duplicate msg_id."""
        log = chat.ChatLog()
        ids = []

        def worker():
            """Add 50 messages and remember their ids."""
            for _ in range(50):
                ids.append(log.add_room("t", "x")["msg_id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), len(set(ids)))


# --- SET_ROLE handler --------------------------------------------------------

class FakeMember:
    """Stand-in for rooms.Member: name, role and online state are used."""

    def __init__(self, name, role, online=True):
        """Create a member with a name, a role and an online flag."""
        self.name = name
        self.role = role
        self.online = online


class FakeRoom:
    """Stand-in for rooms.Room: members dict, an RLock, and recorded sends."""

    def __init__(self, *members):
        """Create a room holding the given FakeMembers."""
        self.members = {m.name: m for m in members}
        self.lock = threading.RLock()
        self.chat = None
        self.broadcasts = []  # (type, payload, kwargs)
        self.direct = []      # (name, type, payload)

    def broadcast(self, type, payload, exclude=None, **env):
        """Record a broadcast (exclude is kept so tests can assert it is unused)."""
        self.broadcasts.append((type, payload, dict(exclude=exclude, **env)))

    def send_to(self, name, type, payload, **env):
        """Record a message sent to one member; False if it is unknown or offline."""
        member = self.members.get(name)
        if member is None or not member.online:
            return False
        self.direct.append((name, type, payload))
        return True


class FakeSession:
    """Stand-in for a client session: records the errors it is asked to send."""

    def __init__(self, member, room):
        """Bind the session to its member and room."""
        self.member = member
        self.room = room
        self.errors = []

    def send_error(self, code, text, ref_type):
        """Record an ERROR reply instead of sending it."""
        self.errors.append((code, ref_type))


class SetRoleTest(unittest.TestCase):
    """Checks the SET_ROLE handler's permission, validation and last-host rules."""

    def setUp(self):
        """Room with host alice, editor bob, viewer carol; capture broadcasts."""
        self.alice = FakeMember("alice", "host")
        self.bob = FakeMember("bob", "editor")
        self.carol = FakeMember("carol", "viewer")
        self.room = FakeRoom(self.alice, self.bob, self.carol)
        patcher = mock.patch.object(presence, "broadcast_user_list")
        self.broadcast = patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, sender, user, role):
        """Run the handler as `sender`; return the fake session."""
        session = FakeSession(sender, self.room)
        msg = {"type": "SET_ROLE", "payload": {"user": user, "role": role}}
        chat.on_set_role(session, msg)
        return session

    def test_handler_is_registered(self):
        """SET_ROLE has a handler registered with the dispatcher."""
        from server import dispatcher
        self.assertIn("SET_ROLE", dispatcher.registered_types())

    def test_host_changes_role_and_broadcasts(self):
        """Host sets carol viewer -> editor and everyone gets a USER_LIST."""
        session = self.send(self.alice, "carol", "editor")
        self.assertEqual(self.carol.role, "editor")
        self.assertEqual(session.errors, [])
        self.broadcast.assert_called_once_with(self.room)

    def test_host_can_promote_to_host(self):
        """Promoting someone to host is allowed."""
        self.send(self.alice, "bob", "host")
        self.assertEqual(self.bob.role, "host")
        self.broadcast.assert_called_once_with(self.room)

    def test_editor_and_viewer_are_forbidden(self):
        """Non-hosts get FORBIDDEN and nothing changes."""
        for sender in (self.bob, self.carol):
            with self.subTest(sender=sender.name):
                session = self.send(sender, "carol", "host")
                self.assertEqual(session.errors, [(protocol.FORBIDDEN, "SET_ROLE")])
                self.assertEqual(self.carol.role, "viewer")
        self.broadcast.assert_not_called()

    def test_unknown_target_is_not_found(self):
        """A user that is not in the room gives NOT_FOUND."""
        session = self.send(self.alice, "mallory", "editor")
        self.assertEqual(session.errors, [(protocol.NOT_FOUND, "SET_ROLE")])
        self.broadcast.assert_not_called()

    def test_invalid_role_or_user_is_bad_format(self):
        """Invalid role or user values give BAD_FORMAT and change nothing."""
        for user, role in (("bob", "superuser"), ("bob", None), ("bob", ["host"]),
                           (None, "editor"), (["bob"], "editor")):
            with self.subTest(user=user, role=role):
                session = self.send(self.alice, user, role)
                self.assertEqual(session.errors, [(protocol.BAD_FORMAT, "SET_ROLE")])
        self.assertEqual(self.bob.role, "editor")
        self.broadcast.assert_not_called()

    def test_last_host_cannot_be_demoted(self):
        """The only host cannot demote themselves."""
        session = self.send(self.alice, "alice", "viewer")
        self.assertEqual(session.errors, [(protocol.FORBIDDEN, "SET_ROLE")])
        self.assertEqual(self.alice.role, "host")
        self.broadcast.assert_not_called()

    def test_host_can_be_demoted_when_another_host_exists(self):
        """With two hosts, one may be demoted; the last one then cannot."""
        self.send(self.alice, "bob", "host")
        session = self.send(self.alice, "alice", "editor")
        self.assertEqual(session.errors, [])
        self.assertEqual(self.alice.role, "editor")
        # bob is now the last host: alice (editor) is forbidden, bob cannot demote self.
        self.assertEqual(self.send(self.bob, "bob", "viewer").errors,
                         [(protocol.FORBIDDEN, "SET_ROLE")])
        self.assertEqual(self.bob.role, "host")


# --- CHAT handler and room hooks ----------------------------------------------

class ChatHandlerTest(unittest.TestCase):
    """Checks on_chat (role, validation, broadcast) and the two room hooks."""

    def setUp(self):
        """Room with host alice, editor bob, viewer carol and a real ChatLog."""
        self.alice = FakeMember("alice", "host")
        self.bob = FakeMember("bob", "editor")
        self.carol = FakeMember("carol", "viewer")
        self.room = FakeRoom(self.alice, self.bob, self.carol)
        chat.attach_chat(self.room)

    def send(self, sender, payload):
        """Run on_chat as `sender` with `payload`; return the fake session."""
        session = FakeSession(sender, self.room)
        chat.on_chat(session, {"type": "CHAT", "payload": payload})
        return session

    def test_handler_is_registered(self):
        """CHAT has a handler registered with the dispatcher."""
        from server import dispatcher
        self.assertIn("CHAT", dispatcher.registered_types())

    def test_room_gets_a_chatlog(self):
        """The on_room_created hook attaches a ChatLog."""
        self.assertIsInstance(self.room.chat, chat.ChatLog)

    def test_every_role_may_chat_and_sender_gets_the_echo(self):
        """Host, editor and viewer can chat; broadcast has no `exclude` (sender included)."""
        for sender in (self.alice, self.bob, self.carol):
            with self.subTest(sender=sender.name):
                self.room.broadcasts.clear()
                session = self.send(sender, {"text": "hello"})
                self.assertEqual(session.errors, [])
                self.assertEqual(len(self.room.broadcasts), 1)
                mtype, payload, env = self.room.broadcasts[0]
                self.assertEqual(mtype, "CHAT")
                self.assertEqual(set(payload), {"msg_id", "from", "text", "ts"})
                self.assertEqual(payload["from"], sender.name)
                self.assertEqual(payload["text"], "hello")
                self.assertIsNone(env["exclude"])

    def test_sender_name_is_not_taken_from_payload(self):
        """A forged `from` in the payload is ignored."""
        self.send(self.carol, {"text": "hi", "from": "alice", "msg_id": "evil"})
        payload = self.room.broadcasts[0][1]
        self.assertEqual(payload["from"], "carol")
        self.assertNotEqual(payload["msg_id"], "evil")

    def test_forbidden_role_is_rejected(self):
        """A role without the chat action gets FORBIDDEN and nothing is sent."""
        outsider = FakeMember("eve", "guest")
        session = self.send(outsider, {"text": "hi"})
        self.assertEqual(session.errors, [(protocol.FORBIDDEN, "CHAT")])
        self.assertEqual(self.room.broadcasts, [])

    def test_empty_or_bad_text_is_rejected(self):
        """Missing, empty, whitespace-only or non-string text gives BAD_FORMAT."""
        for payload in ({}, {"text": ""}, {"text": "   \n\t "}, {"text": None},
                        {"text": 42}, {"text": ["hi"]}):
            with self.subTest(payload=payload):
                session = self.send(self.bob, payload)
                self.assertEqual(session.errors, [(protocol.BAD_FORMAT, "CHAT")])
        self.assertEqual(self.room.broadcasts, [])
        self.assertEqual(self.room.chat.history_for("bob"), [])

    def test_text_is_stripped(self):
        """Leading and trailing whitespace is removed."""
        self.send(self.bob, {"text": "  hi there \n"})
        self.assertEqual(self.room.broadcasts[0][1]["text"], "hi there")

    def test_text_is_cut_to_1000_chars(self):
        """1001 characters are cut to 1000; exactly 1000 is untouched."""
        self.send(self.bob, {"text": "x" * 1001})
        self.assertEqual(len(self.room.broadcasts[0][1]["text"]), 1000)
        self.send(self.bob, {"text": "y" * 1000})
        self.assertEqual(self.room.broadcasts[1][1]["text"], "y" * 1000)

    def test_message_is_stored_in_the_log(self):
        """The broadcast message is the one stored in room.chat."""
        self.send(self.carol, {"text": "stored"})
        stored = self.room.chat.history_for("alice")
        self.assertEqual([m["text"] for m in stored], ["stored"])
        self.assertEqual(stored[0]["msg_id"], self.room.broadcasts[0][1]["msg_id"])

    def test_history_is_sent_on_join(self):
        """A joining member gets CHAT_HISTORY with its own view of the log."""
        self.send(self.alice, {"text": "before you came"})
        self.room.chat.add_dm("alice", "bob", "for bob only")
        dave = FakeMember("dave", "editor")
        self.room.members["dave"] = dave   # the joiner is a member of the room
        chat.send_chat_history(self.room, dave, False, None)
        self.assertEqual(len(self.room.direct), 1)
        name, mtype, payload = self.room.direct[0]
        self.assertEqual((name, mtype), ("dave", "CHAT_HISTORY"))
        self.assertEqual([m["text"] for m in payload["messages"]], ["before you came"])

    def test_history_is_sent_again_on_rejoin_with_own_dms(self):
        """A rejoin also gets the history, including the user's own DMs."""
        self.send(self.alice, {"text": "hello"})
        self.room.chat.add_dm("alice", "bob", "for bob only")
        chat.send_chat_history(self.room, self.bob, True, 7)
        name, mtype, payload = self.room.direct[0]
        self.assertEqual((name, mtype), ("bob", "CHAT_HISTORY"))
        self.assertEqual([m["text"] for m in payload["messages"]], ["hello", "for bob only"])

    def test_history_of_empty_room_is_empty_list(self):
        """CHAT_HISTORY is still sent when there are no messages."""
        chat.send_chat_history(self.room, self.bob, False, None)
        self.assertEqual(self.room.direct[0][2], {"messages": []})


# --- DM handler ------------------------------------------------------------------

class DmHandlerTest(unittest.TestCase):
    """Checks on_dm: role, validation, unicast delivery, echo and privacy."""

    def setUp(self):
        """Room with host alice, editor bob, viewer carol, offline dave."""
        self.alice = FakeMember("alice", "host")
        self.bob = FakeMember("bob", "editor")
        self.carol = FakeMember("carol", "viewer")
        self.dave = FakeMember("dave", "editor", online=False)
        self.room = FakeRoom(self.alice, self.bob, self.carol, self.dave)
        chat.attach_chat(self.room)

    def send(self, sender, payload):
        """Run on_dm as `sender` with `payload`; return the fake session."""
        session = FakeSession(sender, self.room)
        chat.on_dm(session, {"type": "DM", "payload": payload})
        return session

    def test_handler_is_registered(self):
        """DM has a handler registered with the dispatcher."""
        from server import dispatcher
        self.assertIn("DM", dispatcher.registered_types())

    def test_dm_goes_only_to_recipient_and_sender(self):
        """Unicast: recipient and sender get it; the third member and broadcast get nothing."""
        session = self.send(self.alice, {"to": "bob", "text": "  secret  "})
        self.assertEqual(session.errors, [])
        self.assertEqual(self.room.broadcasts, [])          # never broadcast
        self.assertEqual(sorted(name for name, _t, _p in self.room.direct), ["alice", "bob"])
        for name, mtype, payload in self.room.direct:
            self.assertEqual(mtype, "DM")
            self.assertEqual(set(payload), {"msg_id", "from", "to", "text", "ts"})
            self.assertEqual((payload["from"], payload["to"], payload["text"]),
                             ("alice", "bob", "secret"))
        # the same message (same msg_id) reaches both
        self.assertEqual(len({p["msg_id"] for _n, _t, p in self.room.direct}), 1)

    def test_third_member_never_gets_it_not_even_in_history(self):
        """carol sees nothing live and nothing in her history; alice and bob do."""
        self.send(self.alice, {"to": "bob", "text": "secret"})
        self.assertNotIn("carol", [name for name, _t, _p in self.room.direct])
        self.assertEqual(self.room.chat.history_for("carol"), [])
        self.assertEqual([m["text"] for m in self.room.chat.history_for("alice")], ["secret"])
        self.assertEqual([m["text"] for m in self.room.chat.history_for("bob")], ["secret"])
        # rejoin: the history hook sends carol an empty list, bob his DM
        self.room.direct.clear()
        chat.send_chat_history(self.room, self.carol, True, 3)
        chat.send_chat_history(self.room, self.bob, True, 3)
        self.assertEqual(self.room.direct[0][2], {"messages": []})
        self.assertEqual([m["text"] for m in self.room.direct[1][2]["messages"]], ["secret"])

    def test_viewer_may_dm(self):
        """The chat permission includes viewers."""
        session = self.send(self.carol, {"to": "alice", "text": "hi host"})
        self.assertEqual(session.errors, [])
        self.assertEqual(len(self.room.direct), 2)

    def test_role_without_chat_is_forbidden(self):
        """An unknown role is denied and nothing is stored or sent."""
        session = self.send(FakeMember("eve", "guest"), {"to": "bob", "text": "hi"})
        self.assertEqual(session.errors, [(protocol.FORBIDDEN, "DM")])
        self.assertEqual(self.room.direct, [])

    def test_offline_or_unknown_recipient_is_not_found(self):
        """Offline member or a name not in this room gives NOT_FOUND; nothing is stored."""
        for to in ("dave", "mallory"):
            with self.subTest(to=to):
                session = self.send(self.alice, {"to": to, "text": "hi"})
                self.assertEqual(session.errors, [(protocol.NOT_FOUND, "DM")])
        self.assertEqual(self.room.direct, [])
        self.assertEqual(self.room.chat.history_for("alice"), [])

    def test_bad_payloads_are_bad_format(self):
        """Missing or wrong-typed to/text, empty text and DM-to-self are rejected."""
        for payload in ({}, {"to": "bob"}, {"text": "hi"}, {"to": "bob", "text": "   "},
                        {"to": "bob", "text": 5}, {"to": ["bob"], "text": "hi"},
                        {"to": "", "text": "hi"}, {"to": "alice", "text": "to myself"}):
            with self.subTest(payload=payload):
                session = self.send(self.alice, payload)
                self.assertEqual(session.errors, [(protocol.BAD_FORMAT, "DM")])
        self.assertEqual(self.room.direct, [])

    def test_text_is_cut_to_1000(self):
        """Long text is cut like CHAT."""
        self.send(self.alice, {"to": "bob", "text": "x" * 1500})
        self.assertEqual(len(self.room.direct[0][2]["text"]), 1000)

    def test_sender_is_server_set(self):
        """A forged `from`/`msg_id`/`ts` in the payload is ignored."""
        self.send(self.carol, {"to": "bob", "text": "hi", "from": "alice", "msg_id": "evil", "ts": 1})
        payload = self.room.direct[0][2]
        self.assertEqual(payload["from"], "carol")
        self.assertNotEqual(payload["msg_id"], "evil")
        self.assertNotEqual(payload["ts"], 1)


if __name__ == "__main__":
    unittest.main()
