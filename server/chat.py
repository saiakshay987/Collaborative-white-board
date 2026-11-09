# OWNER: P4
"""chat.py: Room chat and direct messages, CHAT_HISTORY on join, SET_ROLE handler.

Contract section C4. This file currently holds:
  * ChatLog   - the per-room message store (no handlers use it yet),
  * SET_ROLE  - the host-only role change handler.
The CHAT / DM handlers and the room hooks come in a later task.
"""

import threading
import time

from common import protocol
from server import permissions
from server import presence
from server.dispatcher import handler

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
