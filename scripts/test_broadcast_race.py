# OWNER: P1
"""test_broadcast_race.py: Stress test for the room.broadcast / send_to race.

Run from the project root (no server needed, no sockets are opened):

    python scripts/test_broadcast_race.py

What it checks. Room.broadcast used to copy the member list under
room.lock and then read m.session AGAIN outside the lock. If another thread
ran member_left() in between (m.session = None), the broadcasting thread
crashed with AttributeError and the remaining members missed the message.

The test starts:
  * a broadcaster thread that calls room.broadcast, room.send_to and
    presence.stats_payload in a tight loop;
  * a flipper thread that takes members offline (member_left) and back
    online (resume_member with a new fake session) FLIPS times.

Fake sessions record what they receive and sometimes raise OSError, like a
socket that died, to check that one failing client does not abort the
broadcast to the others. sys.setswitchinterval is lowered so the threads
switch very often, and both fake send() and the flipper yield the CPU
(time.sleep(0)), so the race window is hit many times. Against the old
code this test fails with "'NoneType' object has no attribute 'send'".

Exit code 0 and "OK" if no thread raised; 1 otherwise.
"""

import os
import random
import sys
import threading
import time
import traceback

# Make "import common" / "import server" work when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import presence, rooms  # noqa: E402  (presence: stats_payload)

FLIPS = 1000          # online/offline flips done by the flipper thread
N_MEMBERS = 6         # members in the test room
FAIL_RATE = 0.02      # share of fake sends that raise OSError (dead socket)


class FakeSession:
    """Stand-in for server.Session: counts received messages, sometimes fails."""

    def __init__(self, name):
        """Create a fake session for member `name`."""
        self.name = name
        self.room = None
        self.member = None
        self.closed = False
        self.rtt_ms = random.choice([None, 1.5, 12.0])
        self.received = 0

    def send(self, type, payload, **env):
        """Pretend to send; raise OSError now and then like a broken socket.

        time.sleep(0) gives up the CPU like a real sendall() that waits for
        the socket, so other threads run in the middle of a broadcast.
        """
        time.sleep(0)
        if random.random() < FAIL_RATE:
            raise OSError("fake: connection reset")
        self.received += 1

    def close(self):
        """Mark the fake connection closed."""
        self.closed = True


def main():
    """Run broadcaster and flipper threads together; return the exit code."""
    sys.setswitchinterval(1e-6)  # switch threads as often as possible
    room = rooms.manager.create_room()
    members = []
    for i in range(N_MEMBERS):
        name = "user%d" % i
        s = FakeSession(name)
        m, _ = rooms.manager.join(room, name, s)
        s.room, s.member = room, m
        members.append(m)

    errors = []
    done = threading.Event()
    counts = {"broadcasts": 0, "send_to": 0, "stats": 0}

    def broadcaster():
        """Broadcast, send_to and build STATS until the flipper is done."""
        try:
            while not done.is_set():
                room.broadcast("CURSOR", {"x": 1, "y": 2}, exclude="user0", user="user0")
                counts["broadcasts"] += 1
                room.send_to(random.choice(members).name, "CHAT", {"text": "hi"})
                counts["send_to"] += 1
                presence.stats_payload(room, {"msgs_in": 0}, N_MEMBERS)
                counts["stats"] += 1
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())

    def flipper():
        """Take members offline and back online FLIPS times."""
        try:
            for _ in range(FLIPS):
                m = random.choice(members[1:])  # keep user0 (the host) online
                if m.online:
                    rooms.manager.member_left(room, m, remove=False)
                else:
                    s = FakeSession(m.name)
                    rooms.manager.resume_member(room, m, s)
                    s.room, s.member = room, m
                time.sleep(0)  # let the broadcasters run between flips
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())
        finally:
            done.set()

    threads = [threading.Thread(target=broadcaster, name="broadcaster"),
               threading.Thread(target=broadcaster, name="broadcaster-2"),
               threading.Thread(target=flipper, name="flipper")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    if any(t.is_alive() for t in threads):
        errors.append("a thread did not finish within 120 s (deadlock?)")

    print("flips: %d, broadcasts: %d, send_to: %d, stats: %d"
          % (FLIPS, counts["broadcasts"], counts["send_to"], counts["stats"]))
    if errors:
        print("FAILED with %d exception(s):" % len(errors))
        for e in errors:
            print(e)
        return 1
    print("OK: zero exceptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
