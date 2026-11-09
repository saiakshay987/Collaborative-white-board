# OWNER: P3
"""board_model.py: BoardModel, the client's copy of the board (contract sections E2, E3 and F).

Everything here runs on the GUI thread (net_client delivers incoming messages
through a Qt signal), so no locking is needed. Widgets never read the
network: they listen to `changed` and read `objects`.

Data flow
---------
    my tool --send_op--> apply_local()   applies AT ONCE (optimistic), remembers the op_id as pending
                         net.send()      the server stamps seq/owner/z and broadcasts to everybody
    server echo ------>  apply_remote()  my own op_id -> only CONFIRM it (never draw it twice)
    other user's op -->  apply_remote()  apply it
    SNAPSHOT --------->  load_snapshot() replace the whole board
    SYNC_DELTA ------->  apply_delta()   feed the missed ops through apply_remote()

Ordering (contract E2): the server numbers accepted operations 1, 2, 3, ...
and TCP keeps them in order, but a message can still be missing (a dropped
connection, or the WB_DROP_PCT demo switch below). So apply_remote() only
applies an operation when seq == last_seq + 1:

    seq <= last_seq         already applied -> dropped as a duplicate
    seq == last_seq + 1     applied; then the buffer is drained in order
    seq >  last_seq + 1     a gap: kept in a buffer keyed by seq and a 1 s
                            timer starts; if the gap is still open when it
                            fires, SYNC_REQUEST {last_seq} is sent (at most
                            once every 2 s) and the server answers with
                            SYNC_DELTA or SNAPSHOT

Optimistic apply (contract E3): my own operation is on screen at once. If
its echo has not arrived after 5 s, SYNC_REQUEST is sent too.

Rejected operations: the server answers a rejected op with an ERROR that has
no op_id and no echo, so it would stay pending forever. But the server
handles one client's messages in order, so when the SYNC_DELTA / SNAPSHOT
that answers a SYNC_REQUEST has been processed, every op that was pending
when that request was sent is settled: it was either echoed (and confirmed)
or rejected. Whatever is still pending is dropped (_settle_after_reply). A
rejected OP_ADD / OP_STROKE_PART leaves an optimistic object without z; it is
removed. A rejected OP_UPDATE / OP_DELETE is not rolled back (most are
NOT_FOUND after somebody else's delete, where the local op changed nothing).

Demo switches (environment variables, read when a message arrives):
    WB_DROP_PCT=20   randomly throw away 20 % of the incoming live board ops
    WB_DUP_PCT=10    deliver 10 % of them twice (once at once, once a message later)
The board must still end up identical to the server's; board_checksum()
(shown by Debug > Show board checksum) proves it.
"""
import base64
import copy
import hashlib
import json
import os
import random
import time
import zlib
from collections import OrderedDict, deque

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from common import protocol
from client.registry import on_message, register_menu_action

# The operations that change objects. UNDO and REDO are not in this set: the
# server answers them with ordinary OP_* messages (contract C2).
OBJECT_OPS = (protocol.OP_ADD, protocol.OP_STROKE_PART, protocol.OP_UPDATE,
              protocol.OP_DELETE, protocol.OP_CLEAR)

# Fields OP_UPDATE may never change; mirrors the server's rule.
PROTECTED_FIELDS = frozenset(("id", "kind", "owner", "z"))

# Style a stroke gets for every field its first part leaves out (same as the server).
STROKE_DEFAULTS = {"tool": "pen", "color": "#000000", "width": 2, "alpha": 1}

# Timings of contract E2/E3, in seconds (the gap timer in milliseconds for QTimer).
GAP_TIMEOUT_MS = 1000       # a gap must stay open this long before we ask the server
SYNC_MIN_INTERVAL_S = 2.0   # never send SYNC_REQUEST more often than this
PENDING_TIMEOUT_S = 5.0     # no echo for one of my ops after this long -> SYNC_REQUEST
TICK_MS = 1000              # how often pending ops (and the idle check) are inspected
IDLE_SYNC_S = 3.0           # demo mode only: ask after this much silence (see _tick)

# Bounds, so a broken or hostile server can never make the client grow without limit.
MAX_BUFFER = 2000
MAX_PENDING = 1000
MAX_APPLIED_IDS = 5000


def board_checksum(objects):
    """sha1 hex of the sorted JSON of a dict obj_id -> object.

    Objects are sorted by id and keys by name, so the insertion order does
    not matter. Identical to server.board_state.objects_checksum (the client
    must not import server code); a unit test keeps the two in step.
    """
    blob = json.dumps(sorted(objects.values(), key=lambda o: o["id"]),
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def decode_snapshot(payload):
    """Return the list of objects inside a SNAPSHOT payload {seq, enc, data}.

    data is base64(zlib(JSON {objects:[...]})) (contract C3). Raises
    ValueError for an unknown encoding.
    """
    if payload.get("enc") != "zlib-b64":
        raise ValueError("unknown snapshot encoding %r" % (payload.get("enc"),))
    raw = zlib.decompress(base64.b64decode(payload["data"]))
    return json.loads(raw.decode("utf-8"))["objects"]


def _env_pct(name):
    """Read a percentage (0 to 100) from the environment variable `name`; 0 if unset or bad."""
    try:
        return max(0.0, min(100.0, float(os.environ.get(name, "0"))))
    except ValueError:
        return 0.0


def _is_seq(value):
    """True for a real integer seq (bool is an int in Python, but never a seq)."""
    return isinstance(value, int) and not isinstance(value, bool)


class BoardModel(QObject):
    """The client's copy of the board (contract section F, client).

    objects   OrderedDict obj_id -> object dict (the renderer paints them in z order)
    last_seq  seq of the last server operation applied; sent on reconnect
    pending   op_id -> {type, sent, gen, asked}: my operations not echoed yet
    changed   signal(obj_id or None): None means "many or all objects changed"
    stats     counters for the demo: received, applied, buffered, dup_dropped,
              sync_requests, snapshots, injected_drops, injected_dups
    """

    changed = pyqtSignal(object)

    def __init__(self, parent=None):
        """Start with an empty board that is not yet attached to an app."""
        super().__init__(parent)
        self.objects = OrderedDict()
        self.last_seq = 0
        self.pending = OrderedDict()
        self.app = None
        self.stats = dict.fromkeys(("received", "applied", "buffered", "dup_dropped", "sync_requests",
                                    "snapshots", "injected_drops", "injected_dups"), 0)
        self.clock = time.monotonic          # replaceable, so tests need no sleeping
        self._buffer = {}                    # seq -> envelope that arrived too early
        self._applied_ids = OrderedDict()    # op_ids already applied (duplicate detection)
        self._generation = 0                 # +1 for every SNAPSHOT (it replaces local state)
        self._last_sync_at = None            # clock() of the last SYNC_REQUEST sent
        self._last_rx = self.clock()         # clock() of the last accepted board message
        self._dup_queue = []                 # demo: duplicates to be delivered one message later
        self._sync_queue = deque(maxlen=20)  # per outstanding SYNC_REQUEST: op_ids pending when it was sent
        self._gap_timer = QTimer(self)       # fires GAP_TIMEOUT_MS after a gap opened
        self._gap_timer.setSingleShot(True)
        self._gap_timer.timeout.connect(self._on_gap_timeout)
        self._tick_timer = QTimer(self)      # watches pending ops (and the demo idle check)
        self._tick_timer.timeout.connect(self._tick)

    def attach(self, app):
        """Remember the AppState and publish this model as app.board (idempotent)."""
        self.app = app
        app.board = self
        if _env_pct("WB_DROP_PCT") or _env_pct("WB_DUP_PCT"):
            print("[board] DEBUG fault injection on: drop %.0f%%, duplicate %.0f%% of incoming board ops"
                  % (_env_pct("WB_DROP_PCT"), _env_pct("WB_DUP_PCT")))

    # ------------------------------------------------------------ applying ops
    def _apply(self, type, payload, user=None, seq=None):
        """Apply one operation to self.objects. Returns (changed, obj_id).

        user is the author (new strokes get it as owner); seq, when known, is
        the z of a new stroke. obj_id is None for OP_CLEAR. Everything stored
        is copied, so a tool that keeps editing the dict it passed to
        send_op can never alter the board behind our back.
        """
        if type == protocol.OP_ADD:
            obj = payload.get("obj")
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                self.objects[obj["id"]] = copy.deepcopy(obj)
                return True, obj["id"]
        elif type == protocol.OP_STROKE_PART:
            return self._apply_stroke_part(payload, user, seq)
        elif type == protocol.OP_UPDATE:
            obj = self.objects.get(payload.get("obj_id"))
            fields = payload.get("set")
            if obj is not None and isinstance(fields, dict):
                obj.update(copy.deepcopy({k: v for k, v in fields.items() if k not in PROTECTED_FIELDS}))
                return True, obj["id"]
        elif type == protocol.OP_DELETE:
            if self.objects.pop(payload.get("obj_id"), None) is not None:
                return True, payload["obj_id"]
        elif type == protocol.OP_CLEAR:
            self.objects.clear()
            return True, None
        return False, None

    def _apply_stroke_part(self, payload, user, seq):
        """OP_STROKE_PART: the first part (with a style) creates the stroke, later parts append."""
        oid, points = payload.get("obj_id"), payload.get("points")
        if not isinstance(oid, str) or not isinstance(points, list):
            return False, None
        obj = self.objects.get(oid)
        if obj is None:
            style = payload.get("style")
            if not isinstance(style, dict):
                return False, None      # a later part of a stroke that is gone
            obj = {"id": oid, "kind": "stroke", "owner": user, "z": seq, "points": []}
            for key, default in STROKE_DEFAULTS.items():
                obj[key] = style.get(key, default)
            self.objects[oid] = obj
        elif obj.get("kind") != "stroke":
            return False, None
        obj["points"].extend(copy.deepcopy(points))
        return True, oid

    # ------------------------------------------------------------- local ops
    def apply_local(self, type, payload, op_id):
        """Apply my own operation at once (optimistic) and remember op_id as pending.

        Called by client/ops.send_op just before the message is sent. The
        object has no z and no seq yet; the server echo adds them (see
        _confirm). Painting such objects last, on top, is render.py's job.
        If no echo comes within PENDING_TIMEOUT_S, _tick asks for a SYNC.
        """
        if type not in OBJECT_OPS or not isinstance(payload, dict):
            return          # UNDO/REDO: nothing to apply, the server sends the result
        user = self.app.me.username if self.app is not None else None
        done, oid = self._apply(type, payload, user=user)
        if op_id is not None:
            target = payload.get("obj_id") or (payload.get("obj") or {}).get("id")
            self.pending[op_id] = {"type": type, "sent": self.clock(), "obj_id": target,
                                   "gen": self._generation, "asked": False}
            if len(self.pending) > MAX_PENDING:
                self.pending.popitem(last=False)     # forget the oldest (probably rejected) one
            self._ensure_tick()
        if done:
            self.changed.emit(oid)

    # ----------------------------------------------------------- remote ops
    def apply_remote(self, msg):
        """Handle one OP_* envelope from the server, in seq order (contract E2).

        See the module docstring for the three cases (duplicate, next, gap).
        """
        type, payload, seq = msg.get("type"), msg.get("payload"), msg.get("seq")
        if type not in OBJECT_OPS or not isinstance(payload, dict) or not _is_seq(seq):
            return
        self.stats["received"] += 1
        self._last_rx = self.clock()
        if seq <= self.last_seq:
            # Already applied, for example because a SNAPSHOT or SYNC_DELTA
            # covered it. If it is the echo of one of my own ops, that op is
            # settled now: stop waiting for it.
            self.stats["dup_dropped"] += 1
            self.pending.pop(msg.get("op_id"), None)
            return
        if seq > self.last_seq + 1:
            # A gap: keep the message until the missing ones arrive.
            if seq not in self._buffer and len(self._buffer) < MAX_BUFFER:
                self._buffer[seq] = msg
                self.stats["buffered"] += 1
            if not self._gap_timer.isActive():
                self._gap_timer.start(GAP_TIMEOUT_MS)
            return
        self._apply_next(msg)
        self._drain_buffer()

    def _apply_next(self, msg):
        """Apply the envelope whose seq is exactly last_seq + 1 and advance last_seq.

        * my own pending op, from the current board generation: only confirm
        * an op_id that was already applied: skip it (but still advance, or
          last_seq would stay stuck on this seq forever)
        * anything else: apply it (this includes my own op after a SNAPSHOT
          replaced the board, because the snapshot may not contain it)
        """
        type, payload, seq, op_id = msg["type"], msg["payload"], msg["seq"], msg.get("op_id")
        mine = self.pending.pop(op_id, None) if op_id is not None else None
        if mine is not None and mine["gen"] == self._generation:
            self._confirm(type, payload, msg)
        elif mine is None and op_id is not None and op_id in self._applied_ids:
            self.stats["dup_dropped"] += 1
        else:
            done, oid = self._apply(type, payload, user=msg.get("user"), seq=seq)
            if done:
                self.changed.emit(oid)
        if op_id is not None:
            self._applied_ids[op_id] = None
            if len(self._applied_ids) > MAX_APPLIED_IDS:
                self._applied_ids.popitem(last=False)
        self.stats["applied"] += 1
        self.last_seq = seq

    def _drain_buffer(self):
        """Apply buffered messages while the next seq is available; keep the gap timer right.

        When the gap is closed and nothing is left, the timer is stopped. If
        later messages remain, a new gap starts and the timer restarts.
        """
        drained = False
        while self.last_seq + 1 in self._buffer:
            self._apply_next(self._buffer.pop(self.last_seq + 1))
            drained = True
        for stale in [s for s in self._buffer if s <= self.last_seq]:
            del self._buffer[stale]
        if not self._buffer:
            self._gap_timer.stop()
        elif drained or not self._gap_timer.isActive():
            self._gap_timer.start(GAP_TIMEOUT_MS)

    def _confirm(self, type, payload, msg):
        """The server echoed one of my pending operations: do not apply it again.

        Small repairs keep my copy equal to the server's:
        * OP_ADD: take owner and z from the echo, and every field the server
          filled in (kind defaults) that my copy lacks.
        * first OP_STROKE_PART: owner and z.
        * OP_UPDATE: re-assert the fields. Another user's update with a LOWER
          seq may have arrived after my optimistic apply and overwritten it,
          but the server order says mine came later and wins (contract E1).
          Setting the same values again is idempotent, nothing is drawn twice.
        """
        if type == protocol.OP_ADD:
            echo = payload.get("obj") or {}
            local = self.objects.get(echo.get("id"))
            if local is not None:
                for key, value in echo.items():
                    local.setdefault(key, copy.deepcopy(value))
                local["owner"], local["z"] = echo.get("owner"), echo.get("z")
                self.changed.emit(local["id"])
        elif type == protocol.OP_STROKE_PART:
            local = self.objects.get(payload.get("obj_id"))
            if local is not None and local.get("z") is None:
                local["owner"], local["z"] = msg.get("user"), msg["seq"]
                self.changed.emit(local["id"])
        elif type == protocol.OP_UPDATE:
            done, oid = self._apply(type, payload)
            if done:
                self.changed.emit(oid)

    # --------------------------------------------------------- snapshot, delta
    def load_snapshot(self, payload):
        """Replace the whole board with a SNAPSHOT payload and set last_seq (contract C3).

        The snapshot is the server's state at payload['seq']: buffered
        messages up to that seq are obsolete, later ones may now be next in
        line. My pending ops stay pending but belong to an older generation:
        if their echo comes later than the snapshot, it is applied like any
        remote op (the snapshot wiped my optimistic copy); if it is covered
        by the snapshot, the echo is dropped as a duplicate.
        """
        objects = decode_snapshot(payload)
        self.objects = OrderedDict((o["id"], o) for o in objects)
        self.last_seq = payload["seq"]
        self._generation += 1
        self.stats["snapshots"] += 1
        self._drain_buffer()
        self._settle_after_reply()
        self.changed.emit(None)

    def apply_delta(self, payload):
        """Apply the ops of a SYNC_DELTA {from_seq, to_seq, ops}.

        They go through apply_remote like live messages, so ops already
        applied are dropped and the buffer is drained. One `changed(None)`
        is emitted at the end instead of one per op. last_seq is NOT simply
        set to to_seq: it only moves as far as real operations took it, so a
        missing op can never be hidden.
        """
        was_blocked = self.blockSignals(True)       # collect: no signal per op
        try:
            for op in payload.get("ops") or []:
                self.apply_remote(op)
        finally:
            self.blockSignals(was_blocked)
        self._settle_after_reply()
        self.changed.emit(None)

    def _settle_after_reply(self):
        """A SYNC_DELTA / SNAPSHOT was processed: drop the pending ops it proves were rejected.

        The oldest outstanding SYNC_REQUEST is the one this reply answers
        (TCP keeps replies in request order). The server had already handled
        every op that was pending when that request was sent, so an op that
        is STILL pending now was rejected and will never be echoed. A
        rejected add/stroke left an optimistic object without z behind; it
        is removed. An unsolicited reply (the one sent right after joining)
        has no queued request and settles nothing.
        """
        if not self._sync_queue:
            return
        for op_id in self._sync_queue.popleft():
            entry = self.pending.pop(op_id, None)
            if entry is None or entry["type"] not in (protocol.OP_ADD, protocol.OP_STROKE_PART):
                continue
            ghost = self.objects.get(entry["obj_id"])
            if ghost is not None and ghost.get("z") is None:
                del self.objects[entry["obj_id"]]

    # ------------------------------------------------------ gaps and timeouts
    def _sync_wait(self):
        """Seconds until a SYNC_REQUEST may be sent again (0 when allowed now)."""
        if self._last_sync_at is None:
            return 0.0
        return max(0.0, SYNC_MIN_INTERVAL_S - (self.clock() - self._last_sync_at))

    def request_sync(self):
        """Send SYNC_REQUEST {last_seq} unless one went out less than 2 s ago. True if sent."""
        if self._sync_wait() > 0:
            return False
        self._last_sync_at = self.clock()
        self.stats["sync_requests"] += 1
        self._sync_queue.append(set(self.pending))      # FIFO: the reply settles exactly these ops
        if self.app is not None and self.app.net is not None:
            self.app.net.send(protocol.SYNC_REQUEST, {"last_seq": self.last_seq})
        return True

    def _on_gap_timeout(self):
        """The 1 s gap timer fired: if the gap is still open, ask the server.

        Rate limited to one request per 2 s; when the limit blocks us the
        timer is re-armed for exactly the remaining time. While the gap stays
        open it keeps firing, so a lost SYNC_DELTA is asked for again.
        """
        if not self._buffer:
            return
        wait = self._sync_wait()
        if wait > 0:
            self._gap_timer.start(int(wait * 1000) + 20)
            return
        self.request_sync()
        if self._buffer:        # the answer may already have closed the gap (and stopped the timer)
            self._gap_timer.start(GAP_TIMEOUT_MS)

    def _ensure_tick(self):
        """Make sure the periodic pending-ops check is running."""
        if not self._tick_timer.isActive():
            self._tick_timer.start(TICK_MS)

    def _tick(self):
        """Every second: ask for a SYNC if one of my ops got no echo for 5 s (contract E3).

        Each stale op triggers at most one request (`asked`); it stays pending,
        so a very late echo is still recognised as mine and never applied twice.
        With the demo drop switch on, a client that has heard nothing for
        IDLE_SYNC_S also asks: a dropped LAST message leaves no later seq that
        would reveal the gap. (Real TCP never loses a message on a live
        connection, so this is only needed for the demo.)
        """
        now = self.clock()
        stale = [p for p in self.pending.values()
                 if not p["asked"] and now - p["sent"] >= PENDING_TIMEOUT_S]
        if stale and self.request_sync():
            for p in stale:
                p["asked"] = True
        elif (_env_pct("WB_DROP_PCT") and not self._buffer and now - self._last_rx >= IDLE_SYNC_S):
            if self.request_sync():
                self._last_rx = now
        if not self.pending and not _env_pct("WB_DROP_PCT"):
            self._tick_timer.stop()

    # ----------------------------------------------------------------- status
    def checksum(self):
        """board_checksum() of the current objects. Compare only when pending is empty."""
        return board_checksum(self.objects)

    def status_line(self):
        """One line for the Debug menu: seq, checksum, pending ops, buffer and counters."""
        s = self.stats
        return ("seq %d | checksum %s | pending %d | buffered %d | applied %d | dup dropped %d | "
                "syncs %d | snapshots %d | injected drops %d dups %d"
                % (self.last_seq, self.checksum()[:12], len(self.pending), len(self._buffer), s["applied"],
                   s["dup_dropped"], s["sync_requests"], s["snapshots"],
                   s["injected_drops"], s["injected_dups"]))

    # ----------------------------------------------------------- demo faults
    def receive_live(self, msg):
        """Entry point for a live OP_* broadcast; applies the WB_DROP_PCT / WB_DUP_PCT switches.

        Without the environment variables this is just apply_remote(msg).
        With them, some messages are discarded or delivered twice (one copy
        now, one after the next message), which is how the demo shows
        'missing, duplicated and delayed updates'. Messages that come from a
        SYNC_DELTA or SNAPSHOT never go through here, so recovery itself is
        not sabotaged.
        """
        if _env_pct("WB_DROP_PCT"):
            self._ensure_tick()         # the idle check in _tick catches a dropped LAST message
        queued, self._dup_queue = self._dup_queue, []
        for late in queued:
            self.apply_remote(late)
        if random.random() * 100 < _env_pct("WB_DROP_PCT"):
            self.stats["injected_drops"] += 1
            return
        self.apply_remote(msg)
        if random.random() * 100 < _env_pct("WB_DUP_PCT"):
            self.stats["injected_dups"] += 1
            self.apply_remote(copy.deepcopy(msg))                 # an immediate duplicate
            self._dup_queue.append(copy.deepcopy(msg))            # and a delayed one


# ---------------------------------------------------------------------- handlers
# net_client calls these on the GUI thread through registry.deliver_message.

def model_of(app):
    """Return app.board as an attached BoardModel, creating it if run_client did not."""
    board = app.board if isinstance(app.board, BoardModel) else BoardModel()
    if board.app is not app:
        board.attach(app)
    return board


def _on_op(app, msg):
    """Any OP_* message from the server."""
    model_of(app).receive_live(msg)


for _op_type in OBJECT_OPS:
    on_message(_op_type)(_on_op)


@on_message(protocol.SNAPSHOT)
def _on_snapshot(app, msg):
    """SNAPSHOT: replace the whole board."""
    model_of(app).load_snapshot(msg.get("payload") or {})


@on_message(protocol.SYNC_DELTA)
def _on_sync_delta(app, msg):
    """SYNC_DELTA: apply the operations we were missing."""
    model_of(app).apply_delta(msg.get("payload") or {})


def show_checksum(app):
    """Debug menu action: show (toast) and print the board status line."""
    line = model_of(app).status_line()
    print("[board]", line)
    app.toast(line)


register_menu_action("Debug", "Show board checksum", show_checksum)
