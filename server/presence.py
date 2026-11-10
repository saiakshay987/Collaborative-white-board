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

Heartbeat and liveness (contract C1, E4, E5) live here too, see below,
and so does the STATS broadcast (latency and traffic, every 2 s) and the
CURSOR relay (live cursors, see below). KICK comes in a later P1 task.
"""

# =============================================================================
# Why our own heartbeat (PING/PONG) and not TCP keepalive?
#
# TCP keepalive (SO_KEEPALIVE) is a kernel feature: after the connection
# has been idle for a while the OS sends an empty probe segment, and if
# several probes get no ACK it resets the socket. It has three problems
# for us:
#   1. The defaults are far too slow: Linux waits 2 HOURS of idle time,
#      then sends 9 probes 75 s apart (about 2 h 11 min before a dead peer
#      is noticed). Windows also waits 2 hours. Tuning it needs
#      platform-specific socket options (TCP_KEEPIDLE / TCP_KEEPINTVL /
#      TCP_KEEPCNT on Linux, SIO_KEEPALIVE_VALS ioctl on Windows).
#   2. It only proves that the peer's KERNEL is alive and ACKs segments.
#      A client whose application is frozen (hung GUI, stuck thread)
#      still ACKs keepalive probes, so TCP would call it healthy.
#   3. The application never sees the probes: no round-trip time, nothing
#      to show in the users panel or the traffic panel.
#
# Our heartbeat is an APPLICATION-level message that travels the normal
# framed path: every config.HEARTBEAT_S (5 s) the server sends PING {t} to
# every connection, and the client's receiver thread answers PONG {t}.
#   * Liveness: the server remembers the time of the last frame received
#     from each client (session.last_rx; ANY frame counts, not only PONG).
#     No frame for config.TIMEOUT_S (15 s, i.e. three missed PINGs) means
#     the client is gone: we close the socket, which marks the member
#     offline and sends USER_LEFT {reason: "timeout"} (contract E5). The
#     seat (name, role, color, token) is kept for config.REJOIN_WINDOW_S
#     so the client can come back with its token (contract E4).
#   * Both directions: the client also treats 15 s without any frame from
#     the server as a lost connection and starts reconnecting, because a
#     PING should arrive every 5 s (client/net_client.py).
#   * RTT: PONG echoes the server's own t, so now - t is the round-trip
#     time measured with ONE clock (no clock sync between laptops needed).
#   * Portable: the same Python code on Windows, Linux and macOS.
#
# A real Wi-Fi drop is exactly the case keepalive handles badly: no FIN or
# RST is ever sent, so recv() on both ends would just block "forever".
# =============================================================================

import math
import threading
import time
import traceback

from common import config, framing, protocol
from server import permissions, rooms
from server.dispatcher import handler

# How often the heartbeat thread wakes up (seconds). Each tick checks for
# timed-out clients; a PING goes out every config.HEARTBEAT_S. A 1 s tick
# means a dead client is noticed between TIMEOUT_S and TIMEOUT_S + 1 s.
TICK_S = 1.0

# STATS goes to every room this often (seconds, contract C1: "Every 2 s").
STATS_EVERY_S = 2.0

# The RTT reported for a client is the moving average of its last
# RTT_AVG_N PING/PONG round trips. One sample alone jumps around (a single
# Wi-Fi retransmission can double it); averaging 5 smooths that out while
# still following a real change within 5 pings (25 s).
RTT_AVG_N = 5


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


@handler(protocol.PONG)
def on_pong(session, msg):
    """PONG {t}: the client echoed the t of our PING; compute the RTT.

    session.last_rx was already updated by the reader thread for this
    frame (any frame proves liveness), so here we only measure the
    round-trip time. t is our own clock, so no clock sync is needed. A
    missing or forged t is ignored (never trust the client): it must be a
    number from the past, not older than the timeout.

    The sample goes into session.rtt_samples (a deque that keeps only the
    last RTT_AVG_N), and session.rtt_ms becomes their average.
    """
    t = msg["payload"].get("t")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return
    rtt = time.time() - t
    if 0 <= rtt <= config.TIMEOUT_S:
        samples = session.rtt_samples
        samples.append(rtt * 1000.0)
        session.rtt_ms = round(sum(samples) / len(samples), 1)


# --- CURSOR (live cursors) ---------------------------------------------------
#
# CURSOR {x, y} is the cheapest message in the protocol and also the most
# frequent one: it is pure presence, NOT board state. So, unlike board ops:
#   * it is not stored, not logged and gets no seq (a late joiner does not
#     need old mouse positions; the next move refreshes them),
#   * it is relayed to everyone EXCEPT the sender (the sender already sees
#     its own mouse pointer), with user = the authenticated sender name,
#   * it changes no room state, so room.lock is not taken here; broadcast()
#     copies the online member list under the lock by itself.
# The client sends at most CURSOR_RATE per second (client/cursor_overlay.py).
# Never trust the client: the server enforces that limit too, with a token
# bucket per connection, so a hacked client cannot flood the room.

# Contract C1: "Max 20 per second per client".
CURSOR_RATE = 20.0

# Token-bucket size: how many CURSOR frames may arrive back to back. A
# small burst absorbs normal network jitter (two frames 50 ms apart can
# arrive 10 ms apart after a Wi-Fi hiccup) without loosening the average.
CURSOR_BURST = 5.0


def _cursor_allowed(session, now):
    """Token bucket: return True if this CURSOR frame is within the rate limit.

    The bucket holds up to CURSOR_BURST tokens and refills at CURSOR_RATE
    tokens per second; every relayed frame costs one token. On average a
    client can therefore never exceed CURSOR_RATE frames per second. Only
    this client's reader thread calls this, so the two attributes kept on
    the session need no lock.
    """
    tokens = getattr(session, "cursor_tokens", CURSOR_BURST)
    last = getattr(session, "cursor_last", now)
    tokens = min(CURSOR_BURST, tokens + (now - last) * CURSOR_RATE)
    session.cursor_last = now
    if tokens < 1.0:
        session.cursor_tokens = tokens
        return False
    session.cursor_tokens = tokens - 1.0
    return True


def _board_coord(value, limit):
    """Return value as a board coordinate (0..limit, one decimal), or None.

    Rejects bools, non-numbers, NaN and infinity (json allows NaN), and
    clamps the rest onto the board (contract D: at most one decimal).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(min(max(float(value), 0.0), float(limit)), 1)


@handler(protocol.CURSOR)
def on_cursor(session, msg):
    """CURSOR {x, y}: relay the sender's mouse position to the rest of the room.

    Steps (in this order, so a refused frame costs as little as possible):
      1. permissions.can(role, "cursor"), else ERROR FORBIDDEN.
      2. Rate limit (token bucket): frames above CURSOR_RATE are silently
         dropped. No ERROR, because an answer per dropped frame would add
         traffic instead of saving it; the client's next frame repairs it.
      3. x and y must be numbers, else ERROR BAD_FORMAT; they are clamped
         to the board and rounded to one decimal.
      4. Rebuild a payload with ONLY x and y (extra fields from the client
         are not forwarded) and broadcast it to the others with
         user = sender. No seq, no log, no print (it would flood the
         console 20 times a second per user).
    """
    room, member = session.room, session.member
    if not permissions.can(member.role, "cursor"):
        session.send_error(protocol.FORBIDDEN, "your role may not share a cursor", protocol.CURSOR)
        return
    if not _cursor_allowed(session, time.time()):
        return
    p = msg["payload"]
    x = _board_coord(p.get("x"), config.BOARD_W)
    y = _board_coord(p.get("y"), config.BOARD_H)
    if x is None or y is None:
        session.send_error(protocol.BAD_FORMAT, "CURSOR needs numbers x and y", protocol.CURSOR)
        return
    room.broadcast(protocol.CURSOR, {"x": x, "y": y}, exclude=member.name, user=member.name)


# --- STATS -------------------------------------------------------------------

def stats_payload(room, counters, n_clients):
    """Return the STATS payload for room (contract C1).

    {rtt: {name: ms}, server: {clients, msgs_in, msgs_out, bytes_in, bytes_out}}

    rtt holds the averaged RTT of every ONLINE member that has answered at
    least one PING (a member who just joined appears after its first PONG).
    counters is a framing.stats_snapshot() of the whole server process and
    n_clients the number of open connections, the same for every room.
    """
    with room.lock:
        rtt = {m.name: m.session.rtt_ms for m in room.online_members()
               if m.session.rtt_ms is not None}
    server = dict(counters)
    server["clients"] = n_clients
    return {"rtt": rtt, "server": server}


def broadcast_stats(n_clients):
    """Send STATS to every room that has someone online.

    The counters are read once, so all rooms see the same numbers. The
    STATS frames sent here are counted in the NEXT round's counters
    (framing counts a frame after sending it).
    """
    counters = framing.stats_snapshot()
    with rooms.manager.lock:  # copy only; never hold manager.lock while sending
        room_list = list(rooms.manager.rooms.values())
    for room in room_list:
        if room.online_members():
            room.broadcast(protocol.STATS, stats_payload(room, counters, n_clients))


# --- Heartbeat thread --------------------------------------------------------

class Heartbeat(threading.Thread):
    """Daemon thread: sends PING, detects silent clients, deletes idle rooms.

    One thread for the whole server (not one per client): every TICK_S it
      1. closes every connection that sent no frame for config.TIMEOUT_S
         (its reader thread then marks the member offline and sends
         USER_LEFT reason "timeout", see server.ClientHandler._release_seat),
      2. every config.HEARTBEAT_S sends PING {t} to every connection,
      3. every STATS_EVERY_S sends STATS (RTT per user + traffic
         counters) to every room,
      4. lets rooms.manager drop expired offline seats and delete rooms
         that have been empty for rooms.ROOM_EMPTY_TTL_S.
    Timeouts are checked BEFORE pinging, so a PING to a dead socket can
    never delay the detection of other dead clients.
    """

    def __init__(self, server):
        """server is the server.Server whose connections we watch."""
        super().__init__(name="heartbeat", daemon=True)
        self.server = server
        self._next_ping = 0.0
        self._next_stats = 0.0

    def run(self):
        """Loop until the server stops; one failing tick never kills the thread."""
        while self.server.running:
            try:
                self.tick(time.time())
            except Exception:  # noqa: BLE001 - liveness must keep running
                traceback.print_exc()
            time.sleep(TICK_S)

    def tick(self, now):
        """One heartbeat round at time now (see the class docstring)."""
        clients = self.server.client_list()

        # 1. Disconnect detection (contract E5). close() only shuts the socket
        #    down; the client's own reader thread wakes up from recv() and
        #    does the normal cleanup, so all seat changes stay in one place.
        for c in clients:
            if not c.closed and now - c.last_rx > config.TIMEOUT_S:
                print("[server] %r: no frame for %.0f s, timing out" % (c, now - c.last_rx))
                c.close()

        # 2. PING every HEARTBEAT_S, also to clients not yet in a room, so a
        #    connection that never joins is timed out as well.
        if now >= self._next_ping:
            self._next_ping = now + config.HEARTBEAT_S
            for c in clients:
                if not c.closed:
                    c.send(protocol.PING, {"t": now})

        # 3. STATS every STATS_EVERY_S (latency and traffic panel).
        if now >= self._next_stats:
            self._next_stats = now + STATS_EVERY_S
            broadcast_stats(sum(1 for c in clients if not c.closed))

        # 4. Housekeeping: expired seats and long-empty rooms.
        rooms.manager.sweep(now)


def start_heartbeat(server):
    """Start and return the Heartbeat thread for server (called by Server.start)."""
    hb = Heartbeat(server)
    hb.start()
    return hb
