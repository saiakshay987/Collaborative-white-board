# OWNER: P4
"""chat.py: Room chat and direct messages, CHAT_HISTORY on join, SET_ROLE handler.

Contract sections C4 and E. This file holds:
  * ChatLog   - the per-room message store,
  * hooks     - room.chat = ChatLog() on room creation, CHAT_HISTORY on every
                (re)join,
  * CHAT      - the room-wide chat handler (broadcast),
  * DM        - the personal chat handler (unicast to the recipient + echo),
  * SET_ROLE  - the host-only role change handler.
"""

import threading
import time

from common import protocol
from server import permissions
from server import presence
from server import rooms
from server.dispatcher import handler

# Longest chat text the server accepts (contract C4); longer text is cut.
MAX_CHAT_CHARS = 1000

# Roles a member can have (contract C4). Kept local so this module only
# depends on the dispatcher/presence APIs of P1, not on rooms internals.
VALID_ROLES = frozenset(["host", "editor", "viewer"])


# --- ChatLog -----------------------------------------------------------------

class ChatLog:
    """Thread-safe log of the chat and direct messages of ONE room.

    Several client reader threads add messages at the same time, so every
    access takes the log's own lock (separate from room.lock, so chat never
    blocks board operations). Only the newest MAX_KEPT messages are kept.
    """

    ROOM_HISTORY = 200  # room messages sent in CHAT_HISTORY
    MAX_KEPT = 500      # total messages kept (room + DM)

    def __init__(self):
        """Create an empty log."""
        self._lock = threading.Lock()
        self._messages = []   # oldest first
        self._counter = 0     # source of msg_id values

    def _append(self, msg_type, sender, to, text):
        """Build a message, store it, trim the log and return a copy.

        The server sets msg_id and ts here: the client never decides them.
        """
        with self._lock:
            self._counter += 1
            msg = {
                "msg_id": "m%d" % self._counter,
                "type": msg_type,
                "from": sender,
                "text": text,
                "ts": time.time(),
            }
            if to is not None:
                msg["to"] = to
            self._messages.append(msg)
            # Keep only the newest MAX_KEPT messages (memory bound).
            if len(self._messages) > self.MAX_KEPT:
                del self._messages[:len(self._messages) - self.MAX_KEPT]
            return dict(msg)

    def add_room(self, sender, text):
        """Store a message for the whole room and return it as
        {msg_id, type:"CHAT", from, text, ts}."""
        return self._append(protocol.CHAT, sender, None, text)

    def add_dm(self, sender, to, text):
        """Store a direct message and return it as
        {msg_id, type:"DM", from, to, text, ts}."""
        return self._append(protocol.DM, sender, to, text)

    def history_for(self, user):
        """Return the history that `user` may see, oldest first.

        That is the last ROOM_HISTORY room messages plus every DM that the
        user sent or received. DMs between other people are never included
        (privacy), whatever the caller passes.
        """
        with self._lock:
            room_msgs = [m for m in self._messages if m["type"] == protocol.CHAT]
            room_msgs = room_msgs[-self.ROOM_HISTORY:]
            own_dms = [m for m in self._messages
                       if m["type"] == protocol.DM and (m["from"] == user or m.get("to") == user)]
            # sorted() is stable, so equal timestamps keep insertion order.
            merged = sorted(room_msgs + own_dms, key=lambda m: m["ts"])
            return [dict(m) for m in merged]


# --- SET_ROLE ----------------------------------------------------------------

@handler("SET_ROLE")
def on_set_role(session, msg):
    """Handle SET_ROLE {user, role}: the host changes another member's role.

    Server-side rules (never trust the client):
      1. The sender's role must allow `set_role` (host only) -> else FORBIDDEN.
      2. `role` must be host/editor/viewer and `user` a string -> else BAD_FORMAT.
      3. The target must be in the room -> else NOT_FOUND.
      4. The last host can never be demoted, or nobody could administer the
         room any more -> FORBIDDEN.
    The check and the change run together under room.lock, so two hosts
    demoting each other at the same moment cannot both pass the "last host"
    check. The error reply and the USER_LIST broadcast happen after the lock
    is released so network sends never hold up the room.
    """
    room = session.room
    payload = msg["payload"]
    target_name = payload.get("user")
    new_role = payload.get("role")

    error = None  # (code, text) to send after leaving the lock
    with room.lock:
        if not permissions.can(session.member.role, "set_role"):
            error = (protocol.FORBIDDEN, "only the host can change roles")
        elif not isinstance(target_name, str) or not isinstance(new_role, str) \
                or new_role not in VALID_ROLES:
            error = (protocol.BAD_FORMAT, "role must be one of host, editor, viewer")
        else:
            target = room.members.get(target_name)
            if target is None:
                error = (protocol.NOT_FOUND, "no such user in this room")
            elif target.role == "host" and new_role != "host" \
                    and sum(1 for m in room.members.values() if m.role == "host") <= 1:
                error = (protocol.FORBIDDEN, "the last host cannot be demoted")
            else:
                target.role = new_role

    if error is not None:
        session.send_error(error[0], error[1], protocol.SET_ROLE)
        return
    # Tell everyone (the target included) about the new role.
    presence.broadcast_user_list(room)


# --- Room hooks --------------------------------------------------------------

@rooms.on_room_created
def attach_chat(room):
    """Hook: give every new room its own ChatLog (room.chat, contract F)."""
    room.chat = ChatLog()


@rooms.on_member_joined
def send_chat_history(room, member, rejoin, last_seq):
    """Hook: send the joining member CHAT_HISTORY {messages}.

    chat.py is imported last (server/bootstrap.py), so the joiner has already
    received USER_LIST and SNAPSHOT/SYNC_DELTA. history_for(member.name) holds
    the last 200 room messages plus only this user's own DMs, so no one sees
    other people's DMs. This also runs on a rejoin: the server does not track
    which messages a reconnecting client already has, so it resends the whole
    history and the client drops duplicates by msg_id.
    """
    room.send_to(member.name, protocol.CHAT_HISTORY,
                 {"messages": room.chat.history_for(member.name)})


# --- CHAT --------------------------------------------------------------------

@handler("CHAT")
def on_chat(session, msg):
    """Handle CHAT {text}: check the role, clean the text, broadcast to the room.

    Never trust the client: permissions.can(role, 'chat') is checked first
    (viewers may chat), the text must be a non-empty string after stripping
    whitespace (else BAD_FORMAT) and is cut to MAX_CHAT_CHARS. The sender
    name, msg_id and ts come from the server (dispatcher + ChatLog), never
    from the payload.
    """
    if not permissions.can(session.member.role, "chat"):
        session.send_error(protocol.FORBIDDEN, "your role may not chat", protocol.CHAT)
        return
    text = msg["payload"].get("text")
    if not isinstance(text, str) or not text.strip():
        session.send_error(protocol.BAD_FORMAT, "chat text is empty", protocol.CHAT)
        return
    text = text.strip()[:MAX_CHAT_CHARS]

    room = session.room
    # Storing and broadcasting under room.lock (an RLock, so broadcast may take
    # it again) gives every client the same message order: two messages
    # arriving on different reader threads cannot be delivered in a different
    # order to different clients.
    with room.lock:
        stored = room.chat.add_room(session.member.name, text)
        # Why a broadcast INCLUDING the sender (no `exclude`): the sender does
        # not draw its own message locally. The echo is the single source of
        # truth, so everyone sees the same text, msg_id, ts and order, and
        # a message the server rejected never appears in the sender's window.
        room.broadcast(protocol.CHAT, {
            "msg_id": stored["msg_id"],
            "from": stored["from"],
            "text": stored["text"],
            "ts": stored["ts"],
        })


# --- DM ----------------------------------------------------------------------

@handler("DM")
def on_dm(session, msg):
    """Handle DM {to, text}: a private message from the sender to ONE member.

    Checks, in order (never trust the client):
      1. permissions.can(role, 'chat'), else FORBIDDEN (viewers may DM).
      2. `to` must be a non-empty string other than the sender and the text a
         non-empty string after stripping, else BAD_FORMAT. Text is cut to
         MAX_CHAT_CHARS.
      3. The recipient must be a member of THIS room (room.members is per
         room, so a name from another room is simply unknown) and online,
         else NOT_FOUND. A DM to someone offline is refused rather than
         queued, as the contract says.
    The server alone sets msg_id, from and ts.

    Unicast versus broadcast: CHAT uses room.broadcast because everybody in
    the room is meant to read it. A DM is private, so it is NEVER broadcast.
    It goes with room.send_to to exactly two sockets: the recipient, and the
    sender as an echo (so the sender's window shows the same msg_id, ts and
    order as the recipient's, and a refused DM never appears). Nobody else
    receives a single byte of it, and ChatLog.history_for only hands it to
    these two users later, so a third member cannot see it even after a
    reconnect.
    """
    if not permissions.can(session.member.role, "chat"):
        session.send_error(protocol.FORBIDDEN, "your role may not chat", protocol.DM)
        return
    payload = msg["payload"]
    to = payload.get("to")
    text = payload.get("text")
    sender = session.member.name
    if not isinstance(to, str) or not to or to == sender \
            or not isinstance(text, str) or not text.strip():
        session.send_error(protocol.BAD_FORMAT, "a DM needs another user and some text",
                           protocol.DM)
        return
    text = text.strip()[:MAX_CHAT_CHARS]

    room = session.room
    # Same lock as CHAT: the online check, the log entry and both sends are one
    # step, so the recipient cannot go offline in between and DMs keep their order.
    with room.lock:
        recipient = room.members.get(to)
        if recipient is None or not recipient.online:
            error = True
        else:
            error = False
            stored = room.chat.add_dm(sender, to, text)
            out = {
                "msg_id": stored["msg_id"],
                "from": stored["from"],
                "to": stored["to"],
                "text": stored["text"],
                "ts": stored["ts"],
            }
            room.send_to(to, protocol.DM, out)       # unicast 1: the recipient
            room.send_to(sender, protocol.DM, out)   # unicast 2: echo to the sender
    if error:
        session.send_error(protocol.NOT_FOUND, "%s is not online in this room" % to,
                           protocol.DM)
