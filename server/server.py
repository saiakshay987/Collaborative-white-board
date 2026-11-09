# OWNER: P1
"""server.py: TCP accept loop, one reader thread per client, and the Session object.

Networking model (thread per connection):

    main thread                          one thread per client
    -----------                          ---------------------
    listen on 0.0.0.0:PORT
    loop: conn = accept()  ----------->  Session.run():
          start Session thread              loop: msg = recv_msg(conn)   (blocking)
                                                  dispatch(session, msg)
                                            on EOF / error: close, member offline

Blocking reads are fine here because each client has its own thread; a slow
client only blocks its own thread. Sends to a socket can come from several
threads (own replies plus broadcasts triggered by other clients);
framing.send_msg holds a per-socket lock so frames never interleave.

Skeleton: heartbeat (PING/timeout) and STATS are added in the presence task.
"""

import socket
import threading
import time
import traceback

from common import config, framing, protocol
from server import dispatcher, rooms


class Session:
    """One client connection: the `session` object handed to every handler.

    Attributes: member (rooms.Member or None), room (rooms.Room or None),
    addr (peer (ip, port)). Methods: send, send_error, close, run.
    """

    def __init__(self, sock, addr):
        """Wrap an accepted socket."""
        self.sock = sock
        self.addr = addr
        self.member = None
        self.room = None
        self.closed = False
        self.last_rx = time.time()  # time of the last frame received (heartbeat)

    def send(self, type, payload, **env):
        """Send one message to this client.

        The server always stamps ts (server clock) and room. env may add
        op_id, seq and user. A failed send (peer gone) closes the session;
        the reader thread then notices and cleans up.
        """
        if self.closed:
            return
        env.setdefault("room", self.room.code if self.room is not None else None)
        env["ts"] = time.time()
        try:
            framing.send_msg(self.sock, protocol.make_msg(type, payload, **env))
        except OSError:
            self.close()

    def send_error(self, code, text, ref_type=None):
        """Send ERROR {code, text, ref_type} to this client only (contract B)."""
        self.send(protocol.ERROR, {"code": code, "text": text, "ref_type": ref_type})

    def close(self):
        """Close the socket once. shutdown() also wakes the blocked recv()."""
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def run(self):
        """Reader loop: receive frames and dispatch them until the peer goes.

        recv_msg returns None on a clean close and raises ProtocolError for
        a malformed or oversized frame, which also ends the connection.
        """
        try:
            while not self.closed:
                msg = framing.recv_msg(self.sock)
                if msg is None:
                    break
                self.last_rx = time.time()
                dispatcher.dispatch(self, msg)
        except framing.ProtocolError as exc:
            print("[server] %s:%d protocol error: %s" % (self.addr[0], self.addr[1], exc))
        except OSError:
            pass  # connection reset or closed by us
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self.close()
            # The seat stays (offline) for the rejoin window; see rooms.member_left.
            if self.room is not None and self.member is not None and self.member.session is self:
                rooms.manager.member_left(self.room, self.member)
            print("[server] %s:%d disconnected" % self.addr)


class Server:
    """Listening socket plus the accept loop."""

    def __init__(self, host="0.0.0.0", port=config.PORT):
        """Remember where to listen; nothing is opened yet."""
        self.host = host
        self.port = port
        self.sock = None

    def start(self):
        """Create, bind and listen on the TCP socket.

        SO_REUSEADDR lets the server restart at once on the same port
        instead of waiting for old connections in TIME_WAIT.
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(32)

    def serve_forever(self):
        """Accept clients forever; each gets a daemon reader thread.

        TCP_NODELAY disables Nagle's algorithm so small messages (cursor,
        stroke parts) go out at once instead of being batched.
        """
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break  # listening socket closed by stop()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("[server] %s:%d connected" % addr)
            session = Session(conn, addr)
            threading.Thread(target=session.run, name="client-%s:%d" % addr, daemon=True).start()

    def stop(self):
        """Close the listening socket; serve_forever then returns."""
        if self.sock is not None:
            self.sock.close()
