# OWNER: P1
"""presence.py: Session and presence messages: CREATE_ROOM, JOIN_ROOM, LEAVE, USER_LIST,
USER_JOINED and USER_LEFT (contract C1, section F).

Join sequence seen by a client (all under room.lock, so no board operation
or other join can slip in between):

    client                         server
    CREATE_ROOM / JOIN_ROOM  --->  validate name, password, token
                             <---  JOIN_OK {room, token, role, color, users}
                                   rooms.member_joined() runs the hooks in
                                   bootstrap order:
                             <---    USER_LIST (to all)   (this module)
                             <---    SNAPSHOT / SYNC_DELTA  (P3, sync.py)
                             <---    CHAT_HISTORY           (P4, chat.py)
    other clients            <---  USER_JOINED {name, color}, USER_LIST

Leaving: a LEAVE message removes the seat (reason "leave"). A dropped TCP
connection only marks the seat offline (reason "timeout") so the user can
resume it with the token within config.REJOIN_WINDOW_S.

Heartbeat (PING/PONG), STATS, CURSOR and KICK come in a later P1 task.
"""

from common import protocol
from server import rooms
from server.dispatcher import handler


# --- USER_LIST ---------------------------------------------------------------

def user_list_payload(room):
    """Return the USER_LIST payload {users:[{name, color, role, online}]}."""
    with room.lock:
        return {"users": [m.public() for m in room.members.values()]}


def broadcast_user_list(room):
    """Send USER_LIST to everyone in room. Call after any member or role change."""
    room.broadcast(protocol.USER_LIST, user_list_payload(room))


# --- Room hooks: announce joins and departures -------------------------------

@rooms.on_member_joined
def _announce_join(room, member, rejoin, last_seq):
    """Hook: tell the others someone (re)joined, then send USER_LIST to all.

    Registered first (bootstrap imports presence before P3/P4), so the
    joiner gets USER_LIST before SNAPSHOT and CHAT_HISTORY. The joiner
    already got JOIN_OK, so USER_JOINED goes only to the others.
    """
    room.broadcast(protocol.USER_JOINED, {"name": member.name, "color": member.color},
                   exclude=member.name)
    broadcast_user_list(room)


@rooms.on_member_left
def _announce_leave(room, member):
    """Hook: send USER_LEFT {name, color, reason} and USER_LIST to the rest.

    The member is already offline or removed, so it receives neither.
    """
    room.broadcast(protocol.USER_LEFT, {"name": member.name, "color": member.color,
                                        "reason": member.left_reason or rooms.REASON_TIMEOUT})
    broadcast_user_list(room)


# --- Handlers ----------------------------------------------------------------

def _seat(session, room, name, ref_type, password=None, token=None, last_seq=None):
    """Seat session in room, send JOIN_OK, then run the join hooks.

    Everything happens under room.lock: the seat is taken, JOIN_OK is sent
    and the hooks run as one step, so the snapshot a joiner receives
    matches exactly the seq it continues from. A refused join is answered
    with ERROR to this client only.
    """
    with room.lock:
        try:
            member, rejoin = rooms.manager.join(room, name, session, password, token)
        except rooms.JoinError as exc:
            session.send_error(exc.code, exc.text, ref_type)
            return
        session.room = room
        session.member = member
        session.send(protocol.JOIN_OK, {
            "room": room.code,
            "token": member.token,  # secret: only ever sent to this member
            "role": member.role,
            "color": member.color,
            "users": user_list_payload(room)["users"],
        }, user=member.name)
        print("[server] %s %s room %s as %s" % (name, "rejoined" if rejoin else "joined",
                                                room.code, member.role))
        rooms.manager.member_joined(room, member, rejoin, last_seq)


def _already_seated(session, ref_type):
    """Refuse CREATE/JOIN from a connection that is already in a room.

    Returns True (after sending ERROR BAD_FORMAT) if it is.
    """
    if session.member is not None:
        session.send_error(protocol.BAD_FORMAT, "already in room %s; send LEAVE first"
                           % session.room.code, ref_type)
        return True
    return False


@handler(protocol.CREATE_ROOM)
def on_create_room(session, msg):
    """CREATE_ROOM {username, password?}: new room, sender becomes host.

    The name and password are validated BEFORE the room exists, so a bad
    request never leaves an empty room behind.
    """
    if _already_seated(session, protocol.CREATE_ROOM):
        return
    p = msg["payload"]
    try:
        name = rooms.clean_username(p.get("username"))
        password = rooms.clean_password(p.get("password"))
    except rooms.JoinError as exc:
        session.send_error(exc.code, exc.text, protocol.CREATE_ROOM)
        return
    room = rooms.manager.create_room(password)
    print("[server] room %s created%s" % (room.code, " (password)" if password else ""))
    _seat(session, room, name, protocol.CREATE_ROOM, password=password)


@handler(protocol.JOIN_ROOM)
def on_join_room(session, msg):
    """JOIN_ROOM {room, username, password?, token?, last_seq?}.

    token and last_seq are sent only on reconnect (contract E4); with a
    valid token the old seat is resumed and the join hooks get rejoin=True.
    """
    if _already_seated(session, protocol.JOIN_ROOM):
        return
    p = msg["payload"]
    try:
        code = rooms.clean_code(p.get("room"))
        name = rooms.clean_username(p.get("username"))
        password = rooms.clean_password(p.get("password"))
    except rooms.JoinError as exc:
        session.send_error(exc.code, exc.text, protocol.JOIN_ROOM)
        return
    room = rooms.manager.get(code)
    if room is None:
        session.send_error(protocol.NO_ROOM, "no room with code %s" % code, protocol.JOIN_ROOM)
        return
    token = p.get("token") if isinstance(p.get("token"), str) else None
    last_seq = p.get("last_seq")
    if isinstance(last_seq, bool) or not isinstance(last_seq, int) or last_seq < 0:
        last_seq = None
    _seat(session, room, name, protocol.JOIN_ROOM, password=password, token=token, last_seq=last_seq)


@handler(protocol.LEAVE)
def on_leave(session, msg):
    """LEAVE {}: clean exit. The seat is removed and the others get USER_LEFT.

    The connection stays open, so the client may CREATE or JOIN again.
    """
    room, member = session.room, session.member
    with room.lock:
        session.room = None
        session.member = None
        if member.session is session:  # not already taken over by a reconnect
            rooms.manager.member_left(room, member, remove=True, reason=rooms.REASON_LEAVE)
    print("[server] %s left room %s" % (member.name, room.code))
