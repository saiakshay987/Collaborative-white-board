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

import argparse
import socket
import sys
import threading
import time
import traceback
from collections import deque

from common import config, framing, netinfo, protocol
from server import dispatcher, presence, rooms

# How often (seconds) the accept loop wakes up from accept() to check whether
# the server is stopping. Without a timeout, a blocked accept() on Windows
# does not see Ctrl+C until the next client connects.
ACCEPT_POLL_S = 1.0

# Socket timeout for client connections (seconds): a backstop BEHIND the
# heartbeat. If the Wi-Fi of a client disappears, no FIN/RST ever arrives;
# once the kernel's send buffer for that socket is full, sendall() would
# block forever, and a broadcast holds room.lock while sending, which would
# freeze the whole room. With a timeout, a blocked send or recv raises
# socket.timeout (an OSError) and the connection is closed. It is longer
# than config.TIMEOUT_S so that, normally, the heartbeat (presence.py)
# notices a dead client first; a live client sends at least a PONG every
# config.HEARTBEAT_S, so recv() never waits this long on a healthy link.
SOCKET_TIMEOUT_S = config.TIMEOUT_S + config.HEARTBEAT_S


class ClientHandler(threading.Thread):
    """One client connection, served by its own daemon thread.

    This is the `session` object handed to every handler. Attributes:
    member (rooms.Member or None), room (rooms.Room or None), addr (peer
    (ip, port)), last_rx (time of the last received frame, for the
    heartbeat), rtt_ms (moving average of the last PING round-trip times,
    None until the first PONG). Methods: send, send_error, close, run.
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
        # Last-seen time: updated for EVERY received frame, read by the
        # heartbeat thread (presence.Heartbeat) to detect silent clients.
        self.last_rx = time.time()
        # RTT: the last presence.RTT_AVG_N PING round-trip times (ms) and
        # their moving average. Written by presence.on_pong on this reader
        # thread, read by the heartbeat thread for STATS (a float
        # assignment is atomic in CPython, so no lock is needed).
        self.rtt_samples = deque(maxlen=presence.RTT_AVG_N)
        self.rtt_ms = None  # moving average, None until the first PONG

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
        # Read self.room once: KICK/LEAVE may set it to None from another
        # thread between a "is not None" check and ".code" (check-then-use).
        room = self.room
        env.setdefault("room", room.code if room is not None else None)
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

    def close_after_flush(self, grace_s):
        """Close the connection gracefully: our last frames first, then FIN.

        Used by KICK (presence.py), where the client MUST read the final
        USER_LEFT {reason: "kicked"} before the connection ends.

        Why not plain close()? If the client's frames (a PONG, a CURSOR)
        are still unread in OUR receive buffer when we close(), the kernel
        answers with a RST instead of a FIN, and on a RST the client's
        kernel may throw away data it has not read yet (Windows always
        does), so the client could miss the USER_LEFT and think the
        connection just dropped, and auto-reconnect.

        So we only shutdown(SHUT_WR): the kernel sends everything already
        queued, then a FIN. The client reads USER_LEFT, then sees EOF and
        closes its side; our reader thread then gets EOF and does the
        normal cleanup. A client that never closes (hacked or frozen) is
        cut off hard by a timer after grace_s seconds.
        """
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # already disconnected
        timer = threading.Timer(grace_s, self.close)
        timer.daemon = True
        timer.start()

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
        except socket.timeout:
            print("[server] %r: socket timed out" % (self,))
        except OSError:
            pass  # connection reset, or closed by us / heartbeat / shutdown
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
        """Remember where to listen; nothing is opened yet.

        host stays "0.0.0.0" (all interfaces) for the real server; see
        start() for why localhost would make the server unreachable.
        """
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.clients = set()
        self._clients_lock = threading.Lock()

    def start(self):
        """Create, bind and listen on the TCP socket.

        Why "0.0.0.0" and never "localhost"/"127.0.0.1": binding a specific
        address makes the OS accept connections ONLY on that address.
        127.0.0.1 is the loopback interface, which no other computer can
        reach, so a server bound to it works on the host's own screen and
        silently refuses every friend. 0.0.0.0 means "all local addresses":
        Wi-Fi, Ethernet, hotspot. Clients still need the right IP of the
        interface they share with us (see print_listen_info).

        SO_REUSEADDR lets the server restart at once on the same port
        instead of failing with "address already in use" while old
        connections sit in TIME_WAIT. Windows is the exception: there
        SO_REUSEADDR means something different and dangerous, it lets a
        SECOND server bind the same port, so clients would reach one of the
        two servers at random (and "room not found" for the other one's
        rooms). On Windows we therefore ask for exclusive use instead, which
        makes a second server fail loudly with "address already in use".
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(32)  # backlog: connections waiting for accept()
        self.sock.settimeout(ACCEPT_POLL_S)
        self.running = True
        # Liveness: PING every HEARTBEAT_S, time out silent clients, delete
        # long-empty rooms (contract C1, E5).
        presence.start_heartbeat(self)

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
                # Blocking, but never forever (see SOCKET_TIMEOUT_S).
                conn.settimeout(SOCKET_TIMEOUT_S)
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

    def client_list(self):
        """Return a copy of the live ClientHandlers (safe to iterate in another thread)."""
        with self._clients_lock:
            return list(self.clients)

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


# --- command line --------------------------------------------------------------

def print_listen_info(port):
    """Print every IPv4 address of this PC and which one friends should use.

    Typical output on a laptop sharing a phone hotspot:

        This PC's IPv4 addresses:
          Wi-Fi             192.168.43.57   <- likely (Wi-Fi / hotspot)
          vEthernet (WSL)   172.28.80.1     (virtual adapter: other PCs cannot reach it)
        Clients should connect to: 192.168.43.57 : 5050

    Network note, important for the viva: many campus / guest Wi-Fi networks
    enable "client isolation" (AP isolation). The access point then refuses
    to forward frames between two Wi-Fi clients, so a laptop can reach the
    internet but not the laptop next to it, and every connect attempt just
    times out even though the IP and the firewall are right. A phone hotspot
    or a router without isolation does not do this, which is why the lab
    uses a hotspot.
    """
    addresses = netinfo.local_addresses()
    print("This PC's IPv4 addresses:")
    if not addresses:
        print("  (none found)")
    width = max([len(a.name) for a in addresses] + [1])
    for a in addresses:
        if a.likely:
            note = "<- likely (Wi-Fi / hotspot)" if a.wifi else "<- likely"
        else:
            note = "(%s)" % a.note if a.note else ""
        print("  %-*s  %-15s %s" % (width, a.name, a.ip, note))
    best = netinfo.likely_address(addresses)
    if best is None:
        print("No usable network address: connect this PC to the hotspot / Wi-Fi first.")
    else:
        print("Clients should connect to: %s : %d" % (best.ip, port))


def main(argv=None):
    """Entry point: parse --port, start the server, run until Ctrl+C.

    Usage: python run_server.py [--port N]     (a bare "5050" still works)
    """
    # Static import inside the function: registers every @handler and hook
    # (bootstrap.py). Done here, not at the top of the file, so that
    # importing server.server for ClientHandler stays cheap and cycle-free.
    import server.bootstrap  # noqa: F401

    parser = argparse.ArgumentParser(description="LAN whiteboard server")
    parser.add_argument("legacy_port", nargs="?", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None,
                        help="TCP port to listen on (default %d)" % config.PORT)
    args = parser.parse_args(argv)
    port = args.port if args.port is not None else (args.legacy_port or config.PORT)
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")

    srv = Server("0.0.0.0", port)  # explicitly all interfaces, never localhost
    try:
        srv.start()
    except OSError as exc:
        print("Cannot listen on port %d: %s" % (port, exc))
        print("Is another server already running? Try: python run_server.py --port %d" % (port + 1))
        sys.exit(1)
    print("Whiteboard server listening on 0.0.0.0:%d (all interfaces)" % port)
    print_listen_info(port)
    print("Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
