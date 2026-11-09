# OWNER: P1
"""server.py: TCP accept loop and one ClientHandler thread per connection.

Networking model (thread per connection):

    main thread                          one ClientHandler thread per client
    -----------                          -----------------------------------
    socket(AF_INET, SOCK_STREAM)
    bind(0.0.0.0, PORT); listen()
    loop: conn = accept()  ----------->  ClientHandler.run():
          start ClientHandler               loop: msg = recv_msg(conn)  (blocking)
                                                  dispatch(handler, msg)
                                            on EOF / error: close the socket,
                                            mark the member offline

Blocking reads are fine because each client has its own thread: a slow or
silent client only blocks its own thread. Sends to one socket can come from
several threads (its own replies plus broadcasts triggered by other
clients); framing.send_msg holds a per-socket lock so frames never
interleave.

Robustness: every ClientHandler wraps its whole loop in try/except, and the
dispatcher wraps every handler call, so one bad client (garbage bytes, a
huge frame, a handler bug) only ends its own connection, never the server.

The ClientHandler object is the `session` that every @handler receives
(contract section F): session.member, session.room, session.send(...),
session.send_error(...).
"""

import socket
import threading
import time
import traceback

from common import config, framing, protocol
from server import dispatcher, rooms

# How often (seconds) the accept loop wakes up from accept() to check whether
# the server is stopping. Without a timeout, a blocked accept() on Windows
# does not see Ctrl+C until the next client connects.
ACCEPT_POLL_S = 1.0


class ClientHandler(threading.Thread):
    """One client connection, served by its own daemon thread.

    This is the `session` object handed to every handler. Attributes:
    member (rooms.Member or None), room (rooms.Room or None), addr (peer
    (ip, port)), last_rx (time of the last received frame, for the
    heartbeat). Methods: send, send_error, close, run.
    """

    def __init__(self, sock, addr, server=None):
        """Wrap an accepted socket. server (optional) is told when we finish."""
        super().__init__(name="client-%s:%d" % addr, daemon=True)
        self.sock = sock
        self.addr = addr
        self.server = server
        self.member = None
        self.room = None
        self.closed = False
        self._close_lock = threading.Lock()
        self.last_rx = time.time()

    def __repr__(self):
        """Short description for log lines."""
        who = self.member.name if self.member is not None else "?"
        return "<client %s:%d %s>" % (self.addr[0], self.addr[1], who)

    # -- sending -------------------------------------------------------------

    def send(self, type, payload, **env):
        """Send one message to this client.

        The server always stamps ts (server clock) and the room code. env
        may add op_id, seq and user. A failed send (peer gone) closes the
        connection; the reader thread then notices and cleans up. May be
        called from any thread.
        """
        if self.closed:
            return
        env.setdefault("room", self.room.code if self.room is not None else None)
        env["ts"] = time.time()
        try:
            framing.send_msg(self.sock, protocol.make_msg(type, payload, **env))
        except (OSError, framing.ProtocolError):
            self.close()

    def send_error(self, code, text, ref_type=None):
        """Send ERROR {code, text, ref_type} to this client only (contract B)."""
        self.send(protocol.ERROR, {"code": code, "text": text, "ref_type": ref_type})

    def close(self):
        """Close the socket exactly once; safe to call from any thread.

        shutdown() first, because it also wakes up our reader thread if it
        is blocked inside recv(); close() alone may not on every OS.
        """
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already disconnected
        try:
            self.sock.close()
        except OSError:
            pass

    # -- reader thread -------------------------------------------------------

    def run(self):
        """Reader loop: receive frames and dispatch them until the peer goes.

        recv_msg returns None on a clean close and raises ProtocolError for
        a malformed or oversized frame, which also ends the connection.
        Whatever happens, the finally block releases the seat.
        """
        try:
            while not self.closed:
                msg = framing.recv_msg(self.sock)
                if msg is None:
                    break  # peer closed the connection (FIN)
                self.last_rx = time.time()
                dispatcher.dispatch(self, msg)
        except framing.ProtocolError as exc:
            print("[server] %r protocol error: %s" % (self, exc))
        except OSError:
            pass  # connection reset, or closed by us / by shutdown
        except Exception:  # noqa: BLE001 - never let one client kill the server
            traceback.print_exc()
        finally:
            self.close()
            self._release_seat()
            if self.server is not None:
                self.server.forget(self)
            print("[server] %s:%d disconnected" % self.addr)

    def _release_seat(self):
        """Mark our member offline if the connection dropped without LEAVE.

        The seat stays for config.REJOIN_WINDOW_S so the user can resume it
        with its token. If a reconnect already attached a NEW connection to
        the seat, member.session is no longer us and nothing happens.
        """
        room, member = self.room, self.member
        if room is None or member is None:
            return
        try:
            with room.lock:
                if member.session is self:
                    rooms.manager.member_left(room, member, reason=rooms.REASON_TIMEOUT)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# Name used by the contract and the dispatcher docs for the handler object.
Session = ClientHandler


class Server:
    """Listening socket, the accept loop and the set of live client handlers."""

    def __init__(self, host="0.0.0.0", port=config.PORT):
        """Remember where to listen; nothing is opened yet."""
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.clients = set()
        self._clients_lock = threading.Lock()

    def start(self):
        """Create, bind and listen on the TCP socket.

        SO_REUSEADDR lets the server restart at once on the same port
        instead of waiting for old connections in TIME_WAIT. Binding
        0.0.0.0 accepts connections on every interface (the LAN).
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(32)  # backlog: connections waiting for accept()
        self.sock.settimeout(ACCEPT_POLL_S)
        self.running = True

    def serve_forever(self):
        """Accept clients until stop(); each gets its own ClientHandler thread.

        TCP_NODELAY disables Nagle's algorithm so small messages (cursor,
        stroke parts) go out at once instead of being batched.
        """
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue  # periodic wake-up: lets Ctrl+C / stop() take effect
            except OSError:
                break  # listening socket closed by stop()
            try:
                conn.settimeout(None)  # client sockets are fully blocking
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client = ClientHandler(conn, addr, self)
                with self._clients_lock:
                    self.clients.add(client)
                print("[server] %s:%d connected" % addr)
                client.start()
            except Exception:  # noqa: BLE001 - one failed accept must not stop the loop
                traceback.print_exc()
                conn.close()

    def forget(self, client):
        """Remove a finished ClientHandler from the live set."""
        with self._clients_lock:
            self.clients.discard(client)

    def stop(self):
        """Clean shutdown: stop accepting, close every client, wait briefly.

        Closing a client socket wakes its reader thread, which then runs its
        normal cleanup (seat offline, USER_LEFT to the rest).
        """
        self.running = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self.clients)
        for c in clients:
            c.close()
        for c in clients:
            c.join(timeout=1.0)
        print("[server] stopped (%d client(s) closed)" % len(clients))
