# OWNER: P1
"""dispatcher.py: Routes every incoming message to the handler registered for its type.

Contract section F (server). Feature modules register handlers with

    from server.dispatcher import handler

    @handler("CHAT")
    def on_chat(session, msg):
        ...

and server/server.py calls dispatch(session, msg) once per received frame,
on that client's reader thread.

The `session` object passed to every handler offers (see server/server.py):
    session.member      Member of the room, or None before CREATE/JOIN_ROOM
    session.room        Room, or None before CREATE/JOIN_ROOM
    session.send(type, payload, **env)
    session.send_error(code, text, ref_type)

Rule "never trust the client": before a handler runs, dispatch() overwrites
the envelope fields only the server may set (user, seq, ts, room), so a
handler can rely on msg["user"] being the authenticated sender.
"""

import time
import traceback

from common import protocol

# type -> handler function. Exactly one handler per message type.
_HANDLERS = {}

# Messages a client may send before it is a member of a room. Everything
# else needs a room first (a client cannot chat or draw "nowhere"). PONG is
# allowed because the heartbeat PINGs every connection, joined or not.
# ROOM_LIST_REQUEST (Revision 5) is allowed because the join screen asks which
# rooms exist BEFORE the user has picked one; it changes nothing on the server.
_PRE_JOIN_TYPES = frozenset([protocol.CREATE_ROOM, protocol.JOIN_ROOM, protocol.PONG,
                             protocol.ROOM_LIST_REQUEST])


def handler(msg_type):
    """Decorator: register fn(session, msg) as the handler for msg_type.

    msg_type must be a message type from the contract (common/protocol.py);
    registering an unknown type, or the same type twice, is a programming
    error and fails at import time so it is caught immediately.
    """
    if msg_type not in protocol.MESSAGE_TYPES:
        raise ValueError("handler for unknown message type %r" % (msg_type,))

    def register(fn):
        """Store fn in the handler table and return it unchanged."""
        if msg_type in _HANDLERS:
            raise ValueError("two handlers for %s: %s and %s"
                             % (msg_type, _HANDLERS[msg_type].__module__, fn.__module__))
        _HANDLERS[msg_type] = fn
        return fn

    return register


def registered_types():
    """Return the set of message types that currently have a handler."""
    return set(_HANDLERS)


def dispatch(session, msg):
    """Validate one received envelope and call its handler.

    Steps:
      1. Shape check: msg must be a dict with a string `type` and a dict
         `payload`; otherwise ERROR BAD_FORMAT.
      2. Unknown type, or a type with no registered handler: ERROR BAD_FORMAT.
      3. Not yet in a room and not CREATE_ROOM/JOIN_ROOM/PONG/ROOM_LIST_REQUEST: ERROR NO_ROOM.
      4. Overwrite server-owned envelope fields (user, room, seq, ts).
      5. Call the handler. An exception inside a handler is logged and
         answered with BAD_FORMAT, so one bad message never kills the
         client's reader thread.
    """
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        session.send_error(protocol.BAD_FORMAT, "message is not an envelope", None)
        return
    msg_type = msg["type"]

    if msg.get("payload") is None:
        msg["payload"] = {}
    if not isinstance(msg["payload"], dict):
        session.send_error(protocol.BAD_FORMAT, "payload must be an object", msg_type)
        return

    fn = _HANDLERS.get(msg_type)
    if fn is None:
        why = "no handler for" if msg_type in protocol.MESSAGE_TYPES else "unknown message type"
        session.send_error(protocol.BAD_FORMAT, "%s %s" % (why, msg_type), msg_type)
        return

    if session.member is None and msg_type not in _PRE_JOIN_TYPES:
        session.send_error(protocol.NO_ROOM, "create or join a room first", msg_type)
        return

    # Never trust the client: these fields are decided by the server only.
    msg["user"] = session.member.name if session.member is not None else None
    msg["room"] = session.room.code if session.room is not None else None
    msg["seq"] = None
    msg["ts"] = time.time()

    try:
        fn(session, msg)
    except Exception:  # noqa: BLE001 - keep the connection alive, report and go on
        traceback.print_exc()
        session.send_error(protocol.BAD_FORMAT, "server could not process %s" % msg_type, msg_type)
