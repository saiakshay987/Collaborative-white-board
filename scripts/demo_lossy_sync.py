"""demo_lossy_sync.py: Live demo of 'delayed, duplicated or missing updates' (contract E2, E3).

Run from the project root (PowerShell shown; Linux/macOS: WB_DROP_PCT=20 python scripts/...):

    $env:WB_DROP_PCT=20; $env:WB_DUP_PCT=10; python scripts/demo_lossy_sync.py
    python scripts/demo_lossy_sync.py --clients 3 --seconds 20

What it does, all in one process but over REAL TCP sockets:
  * starts the real server (server/bootstrap handlers, framing, rooms) on a free local port
  * connects N clients; each has the real BoardModel and draws through the real client/ops.send_op
  * the clients draw random rectangles and strokes, move and delete objects, all at the same time
  * with WB_DROP_PCT / WB_DUP_PCT every client randomly loses or duplicates incoming board ops
  * when drawing stops it waits for recovery (gap timer, SYNC_REQUEST, SYNC_DELTA / SNAPSHOT)
    and then prints seq, checksum and counters of the server and of every client

Exit code 0 when every client board has the same sha1 checksum as the server's, 1 otherwise.
The client network layer here is a minimal stand-in (a reader thread that hands frames to the
GUI thread through a Qt signal, like net_client will) because net_client.py is not finished yet.
"""
import argparse
import os
import random
import socket
import sys
import tempfile
import threading
import time

# Make "import server" / "import client" work when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The demo server autosaves its rooms (server/versions.py); keep that out of the real server_data/.
os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp(prefix='wb_demo_'))

from PyQt5.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal  # noqa: E402

from common import framing, models, protocol  # noqa: E402
import server.bootstrap  # noqa: E402,F401  (registers every server handler and hook)
from server import rooms  # noqa: E402
from server.server import Server  # noqa: E402
import client.board_model  # noqa: E402,F401  (registers the client handlers)
from client import ops, registry  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel  # noqa: E402

# The demo brings its own kinds: without a registered kind the server would reject every object as 'unknown kind'.
DEMO_KINDS = {
    'rect': {'required': {'x': float, 'y': float, 'w': float, 'h': float, 'color': str,
                          'width': float, 'fill': (str, type(None))},
             'defaults': {'fill': None}},
    'stroke': {'required': {'points': list, 'color': str, 'width': float, 'tool': str, 'alpha': float},
               'defaults': {}},
}


class Inbox(QObject):
    """Carries frames from reader threads to the GUI thread (Qt queues the signal across threads)."""

    message = pyqtSignal(object, object)     # (DemoClient, envelope)


class DemoClient:
    """One headless client: a socket, an AppState with a real BoardModel, and a reader thread."""

    def __init__(self, name, inbox):
        """Create the app state and board; connect() opens the socket."""
        self.name = name
        self.inbox = inbox
        self.sock = None
        self.app = AppState()
        self.app.me.username = name
        self.app.net = self                      # ops.send_op calls self.send(...)
        self.app.board = BoardModel()
        self.app.board.attach(self.app)
        self.errors = []

    def send(self, type, payload, op_id=None):
        """Send one envelope (the same call signature as NetClient.send)."""
        framing.send_msg(self.sock, protocol.make_msg(type, payload, op_id=op_id))

    def connect(self, port, room=None):
        """Open the socket, create (room None) or join a room, wait for JOIN_OK, start the reader.

        The handshake is done synchronously, before the reader thread exists,
        so the SNAPSHOT that follows JOIN_OK is read by the reader thread.
        Returns the room code.
        """
        self.sock = socket.create_connection(('127.0.0.1', port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if room is None:
            self.send(protocol.CREATE_ROOM, {'username': self.name})
        else:
            self.send(protocol.JOIN_ROOM, {'room': room, 'username': self.name})
        while True:
            msg = framing.recv_msg(self.sock)
            if msg is None or msg['type'] == protocol.ERROR:
                raise RuntimeError('join failed: %r' % (msg,))
            if msg['type'] == protocol.JOIN_OK:
                self.app.me.role = msg['payload']['role']
                self.app.me.room = msg['payload']['room']
                break
        threading.Thread(target=self._read, daemon=True).start()
        return self.app.me.room

    def _read(self):
        """Reader thread: blocking recv_msg, then hand every frame to the GUI thread."""
        try:
            while True:
                msg = framing.recv_msg(self.sock)
                if msg is None:
                    break
                self.inbox.message.emit(self, msg)
        except (OSError, framing.ProtocolError):
            pass

    def handle(self, msg):
        """GUI thread: answer PING, remember ERRORs, give everything else to the on_message handlers."""
        if msg['type'] == protocol.PING:
            self.send(protocol.PONG, {'t': msg['payload'].get('t')})
        elif msg['type'] == protocol.ERROR:
            self.errors.append(msg['payload'])
        else:
            registry.deliver_message(self.app, msg)

    def draw_something(self, rnd):
        """Do one random board action through the real send_op path."""
        board, name = self.app.board, self.name
        ids = list(board.objects)
        roll = rnd.random()
        if roll < 0.35 or not ids:
            oid = protocol.new_op_id(name)
            obj = {'id': oid, 'kind': 'rect', 'x': rnd.randrange(0, 3900, 20), 'y': rnd.randrange(0, 2900, 20),
                   'w': 60, 'h': 40, 'color': '#%06x' % rnd.randrange(1 << 24), 'width': 2, 'fill': None}
            ops.send_op(self.app, protocol.OP_ADD, {'obj': obj})
        elif roll < 0.65:
            oid = protocol.new_op_id(name)
            style = {'tool': 'pen', 'color': '#%06x' % rnd.randrange(1 << 24), 'width': 3, 'alpha': 1}
            x, y = rnd.randrange(3900), rnd.randrange(2900)
            for part in range(3):                  # one stroke = three parts, like draw_sync would flush
                payload = {'obj_id': oid, 'points': [[x + part * 5 + k, y + k] for k in range(4)],
                           'done': part == 2}
                if part == 0:
                    payload['style'] = style
                ops.send_op(self.app, protocol.OP_STROKE_PART, payload)
        elif roll < 0.9:
            ops.send_op(self.app, protocol.OP_UPDATE,
                        {'obj_id': rnd.choice(ids), 'set': {'x': rnd.randrange(0, 3900, 20)}})
        else:
            ops.send_op(self.app, protocol.OP_DELETE, {'obj_id': rnd.choice(ids)})


def snapshot_of_state(room, clients):
    """Return (server_seq, server_checksum, converged) read under the room lock."""
    with room.lock:
        seq, checksum = room.board.seq, room.board.checksum()
    done = all(c.app.board.last_seq == seq and not c.app.board.pending and not c.app.board._buffer
               and c.app.board.checksum() == checksum for c in clients)
    return seq, checksum, done


def main():
    """Run the demo and return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--clients', type=int, default=2, help='number of clients (default 2)')
    parser.add_argument('--seconds', type=float, default=10, help='how long they draw (default 10)')
    parser.add_argument('--timeout', type=float, default=40, help='max seconds to wait for recovery (default 40)')
    parser.add_argument('--seed', type=int, default=None, help='seed for the drawing (default random)')
    args = parser.parse_args()

    if not models.KINDS:
        models.KINDS = DEMO_KINDS
        print('[demo] kinds_*.py are still stubs: using the demo registry for rect and stroke')
    rnd = random.Random(args.seed)
    qapp = QCoreApplication([])

    srv = Server('127.0.0.1', 0)
    srv.start()
    port = srv.sock.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    inbox = Inbox()
    inbox.message.connect(lambda client, msg: client.handle(msg))
    clients = [DemoClient('user%d' % (i + 1), inbox) for i in range(args.clients)]
    code = clients[0].connect(port)
    for c in clients[1:]:
        c.connect(port, code)
    room = rooms.manager.get(code)
    print('[demo] room %s, %d clients, drop %s%%, duplicate %s%%' % (
        code, args.clients, os.environ.get('WB_DROP_PCT', '0'), os.environ.get('WB_DUP_PCT', '0')))

    state = {'phase': 'draw', 'start': time.monotonic(), 'deadline': None, 'ok': False}

    def step():
        """25 ms driver: draw until time is up, then wait for convergence (or timeout) and quit."""
        now = time.monotonic()
        if state['phase'] == 'draw':
            rnd.choice(clients).draw_something(rnd)
            if now - state['start'] >= args.seconds:
                state['phase'], state['deadline'] = 'settle', now + args.timeout
                print('[demo] drawing stopped, waiting for the clients to recover ...')
            return
        _, _, done = snapshot_of_state(room, clients)
        if done or now >= state['deadline']:
            state['ok'] = done
            driver.stop()
            qapp.quit()

    driver = QTimer()
    driver.timeout.connect(step)
    driver.start(25)
    qapp.exec_()

    seq, checksum, done = snapshot_of_state(room, clients)
    print('\n%-8s %6s  %-14s %7s %8s %9s %6s %8s %9s %9s' % (
        'who', 'seq', 'checksum', 'objects', 'pending', 'received', 'dup', 'syncs', 'inj.drop', 'inj.dup'))
    print('%-8s %6d  %-14s %7d' % ('server', seq, checksum[:12], len(room.board.objects)))
    for c in clients:
        b = c.app.board
        s = b.stats
        print('%-8s %6d  %-14s %7d %8d %9d %6d %8d %9d %9d' % (
            c.name, b.last_seq, b.checksum()[:12], len(b.objects), len(b.pending), s['received'],
            s['dup_dropped'], s['sync_requests'], s['injected_drops'], s['injected_dups']))
        if c.errors:
            print('         server errors for %s: %s' % (c.name, c.errors[:3]))
    print('\nRESULT: %s' % ('OK, every client board is identical to the server board' if done
                            else 'MISMATCH: the boards did not converge'))
    srv.stop()
    return 0 if done else 1


if __name__ == '__main__':
    sys.exit(main())
