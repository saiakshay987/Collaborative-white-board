# OWNER: P1
"""presence.py: Who is in the room: USER_LIST broadcasting (contract C1, section F).

Skeleton: only broadcast_user_list for now. CREATE_ROOM/JOIN_ROOM/LEAVE,
heartbeat, STATS, CURSOR and KICK handlers come in the next P1 task.
"""

from common import protocol


def user_list_payload(room):
    """Return the USER_LIST payload {users:[{name, color, role, online}]}."""
    with room.lock:
        return {"users": [m.public() for m in room.members.values()]}


def broadcast_user_list(room):
    """Send USER_LIST to everyone in room. Call after any member or role change."""
    room.broadcast(protocol.USER_LIST, user_list_payload(room))
