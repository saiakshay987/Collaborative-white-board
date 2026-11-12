# OWNER: P1
"""net_client.py: NetClient(QObject): the client's one TCP connection to the server.

Contract sections A, C1, E4 and F (client).

Threads (the GUI thread must never block on the network):

    GUI thread                    receiver thread            sender thread
    ----------                    ---------------            -------------
    net.send(type, payload) --->  .                          queue.get()
      (only puts on a queue)      .                          framing.send_msg()
    _on_frame(gen, msg)  <-----   framing.recv_msg()
      registry.deliver_message      emits _frame (queued Qt
      -> every @on_message fn       signal: crosses threads)

* The receiver thread blocks in recv_msg() and turns each frame into a Qt
  signal. Because NetClient lives on the GUI thread, Qt queues the signal
  and runs the slot on the GUI thread, so on_message handlers can touch
  widgets safely.
* The sender thread drains an outgoing queue, so a slow socket (or a big
  image chunk) never freezes the window. framing.send_msg already holds a
  per-socket lock, but with a single sender thread frames are in order anyway.
* PING is answered with PONG directly from the receiver thread, so the
  server's RTT measurement does not include GUI-thread delays.

Every connection gets a generation number. Signals from the threads of an
older connection carry the old number and are ignored, so a late message
from a dead socket can never be mistaken for one from the new socket.

Reconnect (contract E4): when the connection drops while we are in a room,
NetClient emits `reconnecting`, then tries every 2 s for up to 60 s. On
success it sends JOIN_ROOM {room, username, password, token, last_seq}; the
server resumes our seat and answers JOIN_OK plus SYNC_DELTA or SNAPSHOT.
Then `reconnected` is emitted. If 60 s pass, `disconnected(reason)`.

Kicked (KICK, contract C1): the server first sends us USER_LEFT about
OURSELVES with reason "kicked", then closes the connection. The USER_LEFT
handler below calls kicked_out(): the token is forgotten and the link is
dropped BEFORE the receiver thread reports the closed socket, so _on_lost
ignores it and no reconnect starts. `kicked` stays True until the next
connect_to(), so the GUI can tell a kick apart from other disconnects.

Also here: the JOIN_OK and USER_LIST handlers that keep app.me and app.users
up to date.

Public API (contract F):
    NetClient(app)
    connect_to(host, port)        non-blocking; emits connected or disconnected(str)
    send(type, payload, op_id=None) -> bool
    disconnect()                  sends LEAVE if in a room, closes, emits disconnected
    is_connected                  property
    kicked                        True after the host removed us (until connect_to)
    signals: connected, disconnected(str), reconnecting, reconnected
"""

import errno
import queue
import socket
import threading
import time

from PyQt5.QtCore import QObject, pyqtSignal

from common import config, framing, protocol
from client import registry
from client.registry import on_message

# Reconnect policy (contract E4).
RECONNECT_EVERY_S = 2
RECONNECT_FOR_S = 60

# How long one TCP connect attempt may take before we give up on it. A
# connect to a PC that silently drops our packets (firewall, Wi-Fi client
# isolation, wrong IP) never gets an answer; without a timeout the OS would
# wait about 20 s (Windows) before failing.
CONNECT_TIMEOUT_S = 5

# Plain-language reasons for a failed connect (shown in the connect dialog).
MSG_TIMEOUT = "Server not reachable: check same hotspot, firewall on the server, correct IP"
MSG_REFUSED = "No server on that IP/port"
MSG_UNREACHABLE = "Network unreachable"
MSG_BAD_ADDRESS = "Not a valid server address: use the IP shown on the host's screen"
CONNECT_MESSAGES = (MSG_TIMEOUT, MSG_REFUSED, MSG_UNREACHABLE, MSG_BAD_ADDRESS)

# OS error numbers behind those reasons. Windows reports Winsock codes
# (10060 ...) that Python may not map onto the errno names, so both appear.
_TIMEOUT_CODES = {errno.ETIMEDOUT, 10060}
_REFUSED_CODES = {errno.ECONNREFUSED, 10061}
_UNREACHABLE_CODES = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, 10050, 10051, 10065}

# USER_LEFT reason when the host removed a member (contract C1).
REASON_KICKED = "kicked"

# disconnected(reason) text after a kick.
KICKED_TEXT = "You were removed from the room."

# Marker put on the send queue to make the sender thread stop.
_STOP = object()


def describe_connect_error(exc):
    """Turn an OSError raised while connecting into one clear sentence.

    * timed out    -> the packets vanished: wrong network, firewall on the
                      server, wrong IP, or Wi-Fi client isolation.
    * refused      -> the PC answered but nothing listens on that port.
    * unreachable  -> this PC has no route to that address (not on the
                      same network, Wi-Fi off).
    """
    codes = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
    if isinstance(exc, socket.gaierror):
        return MSG_BAD_ADDRESS
    if isinstance(exc, (socket.timeout, TimeoutError)) or codes & _TIMEOUT_CODES:
        return MSG_TIMEOUT
    if isinstance(exc, ConnectionRefusedError) or codes & _REFUSED_CODES:
        return MSG_REFUSED
    if codes & _UNREACHABLE_CODES:
        return MSG_UNREACHABLE
    return "Could not connect: %s" % exc


def probe_connection(host, port, timeout=CONNECT_TIMEOUT_S):
    """Try one TCP connect and close it again; return (ok, message, seconds).

    Blocks for up to `timeout` seconds, so callers on the GUI thread must run
    it on a worker thread (the connect dialog's "Test connection" does).
    """
    start = time.time()
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
    except (OSError, ValueError) as exc:
        return False, describe_connect_error(exc), time.time() - start
    return True, "Server reachable", time.time() - start


class _Link:
    """One TCP connection: the socket, its send queue and its generation number.

    A new _Link is made for every (re)connect; the old one is closed and
    its threads end on their own.
    """

    def __init__(self, sock, gen):
        """Wrap a connected socket; gen identifies this connection's signals."""
        self.sock = sock
        self.gen = gen
        self.out = queue.Queue()      # outgoing envelopes, drained by the sender thread
        self.closed = threading.Event()
        self.sender = None            # the sender thread, set by NetClient._start_link

    def close(self):
        """Close the connection once; wakes up both threads.

        shutdown() makes a recv() blocked in the receiver thread return at
        once; the _STOP marker ends the sender thread.
        """
        if self.closed.is_set():
            return
        self.closed.set()
        self.out.put(_STOP)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class NetClient(QObject):
    """The client's network connection, with background threads and auto-reconnect."""

    # Public signals (contract F).
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)
    reconnecting = pyqtSignal()
    reconnected = pyqtSignal()

    # Internal signals: emitted by worker threads, delivered on the GUI thread
    # (queued connection, because this QObject lives on the GUI thread).
    _frame = pyqtSignal(int, object)        # gen, message dict
    _lost = pyqtSignal(int, str)            # gen, reason
    _connect_done = pyqtSignal(int, object, str)   # gen, socket or None, error text
    _reconnect_done = pyqtSignal(int, object)      # gen, socket or None

    def __init__(self, app, parent=None):
        """Create an unconnected client for AppState app."""
        super().__init__(parent)
        self.app = app
        self.host = None
        self.port = config.PORT
        self._link = None
        self._gen = 0                 # bumped for every new connection attempt
        self._reconnecting = False
        # The last CREATE_ROOM/JOIN_ROOM payload we sent; its password is
        # reused when we rejoin after a dropped connection.
        self._join_payload = None
        # True after the host removed us; stops auto-reconnect (see kicked_out).
        self.kicked = False
        self._frame.connect(self._on_frame)
        self._lost.connect(self._on_lost)
        self._connect_done.connect(self._on_connect_done)
        self._reconnect_done.connect(self._on_reconnect_done)

    # ------------------------------------------------------------------ public

    @property
    def is_connected(self):
        """True while a TCP connection is open."""
        return self._link is not None and not self._link.closed.is_set()

    def connect_to(self, host, port=config.PORT):
        """Open a TCP connection to host:port in the background.

        Returns at once. Emits `connected` on success or `disconnected(reason)`
        on failure. Any previous connection is closed silently first.
        """
        self._drop_link()
        self._reconnecting = False
        self.kicked = False
        self.host, self.port = host, int(port)
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._connect_worker, args=(gen, host, self.port),
                         name="net-connect", daemon=True).start()

    def send(self, type, payload=None, op_id=None):
        """Queue one message for sending; returns False when not connected.

        Never blocks: the sender thread does the actual socket write. user,
        seq and ts are left empty; the server sets them (contract B).
        """
        msg = protocol.make_msg(type, payload or {}, op_id=op_id, room=self.app.me.room)
        if type in (protocol.CREATE_ROOM, protocol.JOIN_ROOM):
            self._join_payload = dict(msg["payload"])
        if not self.is_connected:
            return False
        self._link.out.put(msg)
        return True

    def disconnect(self):
        """Leave the room (LEAVE) and close the connection; emits disconnected.

        The LEAVE is queued before the stop marker, so the sender thread
        writes it before the socket is shut down.
        """
        link = self._link
        self._reconnecting = False
        self._gen += 1               # ignore anything the old threads still emit
        self._link = None
        if link is not None and not link.closed.is_set():
            if self.app.me.token is not None:
                link.out.put(protocol.make_msg(protocol.LEAVE, {}, room=self.app.me.room))
            link.out.put(_STOP)      # sender closes the link after flushing
            # Wait a moment (LAN: milliseconds) so LEAVE is really written
            # before the app may exit and kill the daemon sender thread.
            if link.sender is not None:
                link.sender.join(0.5)
        self.app.me.token = None     # seat removed by LEAVE: nothing to resume
        self.disconnected.emit("You left the room.")

    def kicked_out(self):
        """The host removed us: close without LEAVE and never reconnect.

        Called (GUI thread) by the USER_LEFT handler when the payload names
        us with reason "kicked". The server has already deleted our seat
        and changed our token, so a reconnect could not resume anyway; it
        must not even be tried. Bumping the generation makes the receiver
        thread's _lost (the server's FIN arrives right after USER_LEFT) a
        stale signal that _on_lost ignores. Emits disconnected(KICKED_TEXT).
        """
        self.kicked = True
        self._reconnecting = False
        self._gen += 1
        self._drop_link()
        self.app.me.token = None
        self.disconnected.emit(KICKED_TEXT)

    # ---------------------------------------------------------- worker threads

    def _connect_worker(self, gen, host, port):
        """Thread: one TCP connect attempt; reports through _connect_done."""
        try:
            sock = self._open_socket(host, port)
        except OSError as exc:
            self._connect_done.emit(gen, None, describe_connect_error(exc))
            return
        self._connect_done.emit(gen, sock, "")

    @staticmethod
    def _open_socket(host, port):
        """Connect a TCP socket (blocking, with a timeout); raises OSError."""
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        # Dead-server detection (contract E5, seen from the client): the
        # server PINGs every config.HEARTBEAT_S, so a healthy link delivers
        # a frame at least every 5 s. If recv() waits config.TIMEOUT_S with
        # nothing, the link is dead (e.g. Wi-Fi off: no FIN/RST ever comes)
        # and socket.timeout ends the receiver thread, which starts the
        # reconnect below. It also stops a send to a dead link from
        # blocking the sender thread forever.
        sock.settimeout(config.TIMEOUT_S)
        # Strokes are sent as many small frames; disable Nagle so they are not
        # held back waiting for ACKs (lower latency for live drawing).
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _receiver(self, link):
        """Thread: read frames until the connection ends; emit each as _frame.

        recv_msg returns None on a clean close (FIN) and raises on a reset
        or a malformed frame; either way the link is reported lost.
        """
        reason = "The server closed the connection."
        try:
            while not link.closed.is_set():
                msg = framing.recv_msg(link.sock)
                if msg is None:
                    break
                if msg.get("type") == protocol.PING:
                    # Answer at once from this thread: accurate RTT (contract C1).
                    t = (msg.get("payload") or {}).get("t")
                    link.out.put(protocol.make_msg(protocol.PONG, {"t": t}))
                self._frame.emit(link.gen, msg)
        except framing.ProtocolError as exc:
            reason = "Protocol error: %s" % exc
        except socket.timeout:
            reason = "No message from the server for %d s." % config.TIMEOUT_S
        except OSError as exc:
            reason = "Connection lost: %s" % exc
        link.close()
        self._lost.emit(link.gen, reason)

    def _sender(self, link):
        """Thread: write queued messages to the socket in order until _STOP."""
        while True:
            msg = link.out.get()
            if msg is _STOP:
                break
            try:
                framing.send_msg(link.sock, msg)
            except (OSError, framing.ProtocolError) as exc:
                print("[net] send failed: %s" % exc)
                break
        link.close()

    def _reconnect_worker(self, gen, host, port):
        """Thread: try to reconnect every RECONNECT_EVERY_S for RECONNECT_FOR_S.

        Stops early if a newer connection attempt (or disconnect) bumped the
        generation. Reports the socket, or None, through _reconnect_done.
        """
        deadline = time.time() + RECONNECT_FOR_S
        while time.time() < deadline and gen == self._gen:
            try:
                self._reconnect_done.emit(gen, self._open_socket(host, port))
                return
            except OSError:
                time.sleep(RECONNECT_EVERY_S)
        self._reconnect_done.emit(gen, None)

    # ------------------------------------------------- slots (GUI thread only)

    def _start_link(self, sock):
        """Wrap sock in a new _Link and start its receiver and sender threads."""
        link = _Link(sock, self._gen)
        self._link = link
        threading.Thread(target=self._receiver, args=(link,), name="net-recv", daemon=True).start()
        link.sender = threading.Thread(target=self._sender, args=(link,), name="net-send", daemon=True)
        link.sender.start()

    def _drop_link(self):
        """Close the current connection without emitting any signal."""
        if self._link is not None:
            self._link.close()
            self._link = None

    def _on_connect_done(self, gen, sock, error):
        """A connect_to attempt finished."""
        if gen != self._gen:            # superseded by a newer attempt
            if sock is not None:
                sock.close()
            return
        if sock is None:
            # error is already a clear sentence (describe_connect_error).
            self.disconnected.emit("%s (%s:%d)" % (error, self.host, self.port))
            return
        self._start_link(sock)
        self.connected.emit()

    def _on_frame(self, gen, msg):
        """One incoming message: hand it to every on_message handler."""
        if gen != self._gen:
            return                       # from an old, replaced connection
        registry.deliver_message(self.app, msg)

    def _on_lost(self, gen, reason):
        """The receiver thread ended: reconnect if we were in a room."""
        if gen != self._gen:
            return                       # old link, or we disconnected on purpose
        self._link = None
        if self.app.me.token is None or self.app.me.room is None:
            self.disconnected.emit(reason)
            return
        self._reconnecting = True
        self._gen += 1
        self.reconnecting.emit()
        threading.Thread(target=self._reconnect_worker, args=(self._gen, self.host, self.port),
                         name="net-reconnect", daemon=True).start()

    def _on_reconnect_done(self, gen, sock):
        """The reconnect loop ended: rejoin with token and last_seq, or give up."""
        if gen != self._gen:
            if sock is not None:
                sock.close()
            return
        self._reconnecting = False
        if sock is None:
            self.app.me.token = None
            self.disconnected.emit("Could not reconnect to %s:%d within %d s."
                                   % (self.host, self.port, RECONNECT_FOR_S))
            return
        self._start_link(sock)
        me = self.app.me
        payload = {"room": me.room, "username": me.username, "token": me.token}
        password = (self._join_payload or {}).get("password")
        if password:
            payload["password"] = password
        board = self.app.board
        if board is not None and getattr(board, "last_seq", None) is not None:
            payload["last_seq"] = board.last_seq
        self.send(protocol.JOIN_ROOM, payload)
        self.reconnected.emit()


# ---------------------------------------------------------------------------
# Session state handlers: keep app.me and app.users up to date (GUI thread).

@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK {room, token, role, color, users}: we are in the room.

    The server put our authenticated name in the envelope's user field.
    """
    p = msg.get("payload") or {}
    me = app.me
    me.username = msg.get("user") or me.username
    me.room = p.get("room")
    me.token = p.get("token")
    me.color = p.get("color")
    app.set_role(p.get("role"))
    app.set_users(p.get("users") or [])


@on_message(protocol.USER_LIST)
def _on_user_list(app, msg):
    """USER_LIST {users}: replace app.users; pick up a change of my own role."""
    users = (msg.get("payload") or {}).get("users") or []
    for u in users:
        if u.get("name") == app.me.username:
            app.set_role(u.get("role"))
            app.me.color = u.get("color", app.me.color)
    app.set_users(users)


@on_message(protocol.USER_LEFT)
def _on_user_left(app, msg):
    """USER_LEFT about ME with reason "kicked": the host removed us.

    The server sends it to the kicked client only (the others get their
    own copy without us). Any other USER_LEFT is just a notification for
    the toasts and the cursor overlay.
    """
    p = msg.get("payload") or {}
    if p.get("reason") == REASON_KICKED and p.get("name") == app.me.username \
            and app.net is not None:
        app.net.kicked_out()
