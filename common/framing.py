# OWNER: P1
"""framing.py: Message framing on top of a TCP byte stream (contract section A).

TCP delivers a reliable, ordered stream of BYTES, not of messages: one send()
may arrive split over several recv() calls, and several sends may arrive
glued together in one recv(). To recover message boundaries every message is
sent as a frame:

    +----------------------------+-------------------------------+
    | N: 4-byte unsigned length  | N bytes of UTF-8 encoded JSON |
    |    (big-endian, "!I")      |                               |
    +----------------------------+-------------------------------+

The receiver first reads exactly 4 bytes to learn N, then reads exactly N
bytes. Both reads loop, because recv() may return fewer bytes than asked.

Public API:
    send_msg(sock, msg)   send one dict as one frame (thread-safe per socket)
    recv_msg(sock)        receive one frame -> dict, or None on clean close
    STATS                 traffic counters for the stats panel
    stats_snapshot()      consistent copy of STATS
    ProtocolError         raised for a malformed or oversized frame
"""

import json
import struct
import threading
import weakref

from common import config

# Header format: network byte order (big-endian) unsigned 32-bit int.
_HEADER = struct.Struct("!I")
HEADER_SIZE = _HEADER.size  # 4 bytes

# Traffic counters shown in the STATS message / traffic panel. Bytes include
# the 4-byte header, so they reflect what actually crossed the wire.
STATS = {"msgs_in": 0, "msgs_out": 0, "bytes_in": 0, "bytes_out": 0}
_stats_lock = threading.Lock()

# One send lock per socket. Several threads (for example the heartbeat thread
# and a broadcast from another client's thread) may send on the same socket
# at once; without the lock their frames could interleave and corrupt the
# stream. A WeakKeyDictionary drops the lock automatically when the socket
# object is garbage-collected, so closed connections do not leak locks.
_send_locks = weakref.WeakKeyDictionary()
_send_locks_guard = threading.Lock()  # protects creation of entries above


class ProtocolError(ValueError):
    """Raised when a peer violates the framing rules.

    Examples: a length header above config.MAX_FRAME, a body that is not
    valid UTF-8 JSON, a body that is not a JSON object, or a connection that
    closes in the middle of a frame. The caller should close the connection.
    """


def _count(direction, nbytes):
    """Add one message of nbytes (header included) to STATS.

    direction is "in" or "out". Runs under _stats_lock so concurrent
    sender/receiver threads never lose an update.
    """
    with _stats_lock:
        STATS["msgs_" + direction] += 1
        STATS["bytes_" + direction] += nbytes


def stats_snapshot():
    """Return a consistent copy of STATS, taken under the stats lock."""
    with _stats_lock:
        return dict(STATS)


def _lock_for(sock):
    """Return the send lock of sock, creating it on first use.

    Creation is guarded so two threads cannot create two different locks for
    the same socket.
    """
    with _send_locks_guard:
        lock = _send_locks.get(sock)
        if lock is None:
            lock = threading.Lock()
            _send_locks[sock] = lock
        return lock


def encode_msg(msg):
    """Encode a dict as one complete frame (header + body) and return bytes.

    Raises ProtocolError if the encoded body is larger than config.MAX_FRAME,
    because the receiver would reject it anyway.
    """
    # Compact separators keep frames small; ensure_ascii=False keeps non-ASCII
    # text (names, chat) as real UTF-8 instead of \\uXXXX escapes.
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > config.MAX_FRAME:
        raise ProtocolError("frame of %d bytes exceeds MAX_FRAME" % len(body))
    return _HEADER.pack(len(body)) + body


def send_msg(sock, msg):
    """Send one message (a dict) on sock as a single length-prefixed frame.

    The whole frame is built first and written with sendall() while holding
    the per-socket lock, so frames from different threads never interleave.
    sendall() itself loops until every byte is handed to the kernel.
    Raises OSError if the connection is broken.
    """
    frame = encode_msg(msg)
    with _lock_for(sock):
        sock.sendall(frame)
    _count("out", len(frame))


def _recv_exact(sock, n):
    """Read exactly n bytes from sock and return them as bytes.

    TCP may deliver fewer bytes per recv() than requested, so keep reading
    into a preallocated buffer until it is full. Returns None if the peer
    closed the connection before sending ANY of the n bytes (a clean close
    at a frame boundary); raises ProtocolError if it closed part-way through.
    """
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        # recv_into writes straight into our buffer: no extra copies.
        r = sock.recv_into(view[got:], n - got)
        if r == 0:  # recv returning 0 bytes means the peer closed (FIN)
            if got == 0:
                return None
            raise ProtocolError("connection closed mid-frame (%d of %d bytes)" % (got, n))
        got += r
    return bytes(buf)


def recv_msg(sock):
    """Receive one frame from sock and return the decoded message dict.

    Returns None when the peer closed the connection cleanly between frames.
    Raises ProtocolError for an oversized length, invalid UTF-8/JSON, a body
    that is not a JSON object, or a close in the middle of a frame.
    Raises OSError on socket errors (reset, timeout).
    """
    # Step 1: the 4-byte length header.
    header = _recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None
    (n,) = _HEADER.unpack(header)

    # Step 2: check N BEFORE allocating or reading the body, so a bad header
    # cannot make us allocate up to 4 GiB.
    if n > config.MAX_FRAME:
        raise ProtocolError("announced frame of %d bytes exceeds MAX_FRAME" % n)

    # Step 3: the body. A close here is never clean: we were promised n bytes.
    body = _recv_exact(sock, n) if n else b""
    if body is None:
        raise ProtocolError("connection closed after header, before body")

    # Step 4: decode. Every envelope is a JSON object.
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("bad frame body: %s" % exc) from None
    if not isinstance(msg, dict):
        raise ProtocolError("frame body is not a JSON object")

    _count("in", HEADER_SIZE + n)
    return msg
