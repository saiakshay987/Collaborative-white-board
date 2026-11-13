# OWNER: P1
"""test_framing.py: Stress test for common/framing.py over a local socket pair.

Run from the project root:  python scripts/test_framing.py

Checks:
  1. 1000 messages of random sizes (a few bytes up to ~1 MB, with non-ASCII
     text) arrive intact and in order.
  2. One frame written ONE BYTE PER send() is still reassembled correctly
     (proves recv_msg loops instead of trusting a single recv()).
  3. Several threads calling send_msg on the same socket never interleave
     their frames (proves the per-socket lock works).
  4. A header announcing more than MAX_FRAME raises ProtocolError.
  5. A clean close returns None; a close mid-frame raises ProtocolError.
  6. STATS counted the traffic.
"""

import os
import random
import socket
import struct
import sys
import threading
import time

# Make "import common" work when run as a plain script from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, framing  # noqa: E402

N_MESSAGES = 1000
SPLIT_INDEX = 500  # this message is sent one byte at a time


def random_message(i, rng):
    """Build message number i with a random-size payload (mixed ASCII/UTF-8)."""
    size = rng.choice([0, 1, 10, 100, 1000, 10000, 100000]) + rng.randrange(0, 1000)
    if i % 97 == 0:
        size = rng.randrange(500000, 1000000)  # occasional big frame
    text = "".join(rng.choice("abcxyz09 é漢😀") for _ in range(size // 4)) + "x" * (size % 4)
    return {"v": 1, "type": "CHAT", "seq": i, "payload": {"text": text, "n": rng.random()}}


def sender(sock, messages):
    """Send every message; the SPLIT_INDEX one by hand, one byte per send()."""
    for i, msg in enumerate(messages):
        if i == SPLIT_INDEX:
            frame = framing.encode_msg(msg)
            for b in range(len(frame)):
                sock.sendall(frame[b:b + 1])
        else:
            framing.send_msg(sock, msg)


def test_bulk_and_split():
    """Tests 1 and 2: 1000 random messages, one split into 1-byte sends."""
    rng = random.Random(1234)
    messages = [random_message(i, rng) for i in range(N_MESSAGES)]
    # Keep the 1-byte-split message small enough to finish quickly.
    messages[SPLIT_INDEX] = {"type": "CHAT", "payload": {"text": "split ✂ " * 300}}
    a, b = socket.socketpair()
    t = threading.Thread(target=sender, args=(a, messages))
    t.start()
    total = 0
    for i, expected in enumerate(messages):
        got = framing.recv_msg(b)
        assert got == expected, "message %d corrupted" % i
        total += len(framing.encode_msg(expected))
    t.join()
    a.close()
    assert framing.recv_msg(b) is None, "clean close must return None"
    b.close()
    print("ok  %d messages (%.1f MB) intact, #%d sent in 1-byte pieces"
          % (N_MESSAGES, total / 1e6, SPLIT_INDEX))


def test_concurrent_senders():
    """Test 3: 4 threads x 200 frames on one socket, no interleaving."""
    a, b = socket.socketpair()
    threads = []
    for tid in range(4):
        msgs = [{"t": tid, "i": i, "pad": "p" * (i * 37 % 20000)} for i in range(200)]
        threads.append(threading.Thread(target=lambda m=msgs: [framing.send_msg(a, x) for x in m]))
    for t in threads:
        t.start()
    last = {tid: -1 for tid in range(4)}
    for _ in range(800):
        msg = framing.recv_msg(b)  # raises if frames were interleaved
        assert msg["i"] == last[msg["t"]] + 1, "per-thread order broken"
        last[msg["t"]] = msg["i"]
    for t in threads:
        t.join()
    a.close()
    b.close()
    print("ok  4 concurrent senders, 800 frames, no interleaving")


def expect_protocol_error(raw, what):
    """Write raw bytes, close, and assert recv_msg raises ProtocolError."""
    a, b = socket.socketpair()
    a.sendall(raw)
    a.close()
    try:
        framing.recv_msg(b)
    except framing.ProtocolError:
        print("ok  %s rejected" % what)
    else:
        raise AssertionError("%s was accepted" % what)
    finally:
        b.close()


def test_errors():
    """Tests 4 and 5: oversize header, close mid-frame, bad JSON."""
    expect_protocol_error(struct.pack("!I", config.MAX_FRAME + 1), "N > MAX_FRAME")
    expect_protocol_error(struct.pack("!I", 100) + b"{\"a\":", "close mid-body")
    expect_protocol_error(b"\x00\x00", "close mid-header")
    expect_protocol_error(struct.pack("!I", 3) + b"{x}", "invalid JSON")
    try:
        framing.encode_msg({"x": "a" * (config.MAX_FRAME + 1)})
    except framing.ProtocolError:
        print("ok  oversized send refused")
    else:
        raise AssertionError("oversized send was allowed")


def main():
    """Run all tests and print the STATS counters."""
    start = time.time()
    before = framing.stats_snapshot()
    test_bulk_and_split()
    test_concurrent_senders()
    test_errors()
    after = framing.stats_snapshot()
    # 1000 bulk + 800 concurrent received; 999 + 800 sent via send_msg.
    assert after["msgs_in"] - before["msgs_in"] == N_MESSAGES + 800
    assert after["msgs_out"] - before["msgs_out"] == N_MESSAGES - 1 + 800
    print("ok  STATS", after)
    print("ALL PASSED in %.1f s" % (time.time() - start))


if __name__ == "__main__":
    main()
