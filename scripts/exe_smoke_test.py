"""exe_smoke_test.py: Smoke test of the BUILT executables (not the Python sources).

    python scripts/exe_smoke_test.py [--dist dist/linux] [--port 5998] [--client-seconds 5]

Uses only the standard library, so any Python 3 can run it (it does not import
the project: it talks to the executable over a raw socket, like a client would).

Checks:
  1. server: WhiteboardServer is copied into an empty temporary folder and
     started on --port with stdin closed. A raw TCP socket sends CREATE_ROOM
     with the contract's framing (4-byte big-endian length + UTF-8 JSON) and
     must get JOIN_OK with a room code and role "host". server_data/ must
     appear NEXT TO the executable (not in PyInstaller's temp folder). Then
     the server is stopped with Ctrl+C (SIGINT; terminate on Windows) and the
     port must be free again.
  2. server error exit: with the port already taken, the server must print
     "Cannot listen" and exit with a non-zero code on its own. Its stdin is
     not a console, so the Windows "Press Enter to close" pause must not run
     and the test must not hang.
  3. clients: WhiteboardClient and WhiteboardClient-console are started for
     --client-seconds (QT_QPA_PLATFORM=offscreen on Linux, so no screen is
     needed). Passing means still running at the end (the connect dialog is
     open), i.e. no crash at start-up.
Exit code 0 when everything passed.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = struct.Struct("!I")       # 4-byte big-endian unsigned length, contract section A

results = []                       # (name, ok, detail)


def report(name, ok, detail=""):
    """Record and print one check result."""
    results.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (": " + detail) if detail else ""))


# --- framing (independent copy of common/framing.py, so the test does not trust the code under test) ---

def send_frame(sock, msg):
    """Send one frame: length header, then the UTF-8 JSON body."""
    body = json.dumps(msg).encode("utf-8")
    sock.sendall(HEADER.pack(len(body)) + body)


def recv_exact(sock, n):
    """Read exactly n bytes (TCP is a byte stream, one recv may return less); None if the peer closed."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def recv_frame(sock):
    """Read one frame and return the decoded JSON message, or None if the connection closed."""
    head = recv_exact(sock, HEADER.size)
    if head is None:
        return None
    (n,) = HEADER.unpack(head)
    body = recv_exact(sock, n)
    return None if body is None else json.loads(body.decode("utf-8"))


def envelope(msg_type, payload):
    """A client message in the contract's envelope (section B); the server fills user, seq and ts."""
    return {"v": 1, "type": msg_type, "room": None, "user": None, "op_id": None,
            "seq": None, "ts": 0.0, "payload": payload}


# --- process helpers ---------------------------------------------------------------

class Proc:
    """A started executable whose combined stdout/stderr is collected by a reader thread."""

    def __init__(self, args, env=None, cwd=None):
        """Start args with stdin closed (never a console), output piped."""
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        self.p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env, cwd=cwd, creationflags=flags)
        self.lines = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        """Collect output lines until the pipe closes."""
        for raw in self.p.stdout:
            self.lines.append(raw.decode("utf-8", "replace").rstrip())

    def output(self):
        """Everything printed so far, as one string."""
        return "\n".join(self.lines)

    def stop(self, ctrl_c=True, timeout=15):
        """Stop the process (Ctrl+C on Linux, terminate on Windows); kill if it does not exit. Returns the exit code."""
        if self.p.poll() is None:
            if ctrl_c and not WINDOWS:
                self.p.send_signal(signal.SIGINT)
            else:
                self.p.terminate()
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        time.sleep(0.3)              # let the reader thread drain the pipe
        return self.p.returncode


def clean_env():
    """A copy of the environment without WB_DATA_DIR, so the executable picks its own data folder."""
    env = dict(os.environ)
    env.pop("WB_DATA_DIR", None)
    return env


def wait_for_port(port, proc, timeout):
    """Connect to 127.0.0.1:port until it works; None if the process died or time ran out."""
    deadline = time.time() + timeout
    while time.time() < deadline and proc.p.poll() is None:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.3)
    return None


def port_is_free(port):
    """True if nothing accepts connections on 127.0.0.1:port any more."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return False
    except OSError:
        return True


# --- the checks ----------------------------------------------------------------------

def test_server(exe, port):
    """Check 1: CREATE_ROOM -> JOIN_OK, server_data next to the exe, clean Ctrl+C stop."""
    folder = tempfile.mkdtemp(prefix="wb_smoke_")
    try:
        copy = os.path.join(folder, os.path.basename(exe))
        shutil.copy2(exe, copy)
        # cwd is a different folder on purpose: the data must follow the exe, not the cwd.
        proc = Proc([copy, "--port", str(port)], env=clean_env(), cwd=tempfile.gettempdir())
        sock = wait_for_port(port, proc, timeout=40)   # one-file exes unpack first: allow time
        if sock is None:
            report("server starts and listens on %d" % port, False, proc.output()[-800:])
            proc.stop()
            return
        report("server starts and listens on %d" % port, True)
        try:
            sock.settimeout(10)
            send_frame(sock, envelope("CREATE_ROOM", {"username": "smoke"}))
            reply = None
            while True:                   # JOIN_OK is first; skip anything else just in case
                msg = recv_frame(sock)
                if msg is None or msg.get("type") in ("JOIN_OK", "ERROR"):
                    reply = msg
                    break
            payload = (reply or {}).get("payload") or {}
            ok = (reply is not None and reply.get("type") == "JOIN_OK"
                  and len(str(payload.get("room", ""))) == 6 and payload.get("role") == "host")
            report("CREATE_ROOM -> JOIN_OK", ok,
                   "room %s, role %s" % (payload.get("room"), payload.get("role")) if ok else repr(reply)[:300])
        except (OSError, ValueError) as exc:
            report("CREATE_ROOM -> JOIN_OK", False, "%s: %s" % (type(exc).__name__, exc))
        finally:
            sock.close()
        data = os.path.join(folder, "server_data")
        report("server_data created next to the executable", os.path.isdir(data), data)
        code = proc.stop(ctrl_c=True)
        out = proc.output()
        if not WINDOWS:
            report("Ctrl+C stops the server cleanly", "Stopping." in out and "Press Enter" not in out,
                   "exit code %s" % code)
        report("port %d free after stop (no orphan process)" % port, port_is_free(port))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_server_port_busy(exe, port):
    """Check 2: port taken -> message and non-zero exit by itself, no 'Press Enter' wait with stdin closed."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("0.0.0.0", port))
        blocker.listen(1)
    except OSError as exc:
        report("server exits when the port is busy", False, "could not take port %d: %s" % (port, exc))
        blocker.close()
        return
    folder = tempfile.mkdtemp(prefix="wb_smoke_")
    try:
        proc = Proc([exe, "--port", str(port)], env=dict(clean_env(), WB_DATA_DIR=folder))
        try:
            code = proc.p.wait(40)
        except subprocess.TimeoutExpired:
            code = None
        proc.stop()                  # kills it only if it hung; also drains the output
        out = proc.output()
        ok = code not in (None, 0) and "Cannot listen" in out
        report("server exits with an error when the port is busy (no hang)", ok,
               "exit code %s" % code if ok else "exit code %s, output: %s" % (code, out[-400:]))
    finally:
        blocker.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_client(exe, seconds):
    """Check 3: the client is still running after `seconds` (no crash at start-up)."""
    env = dict(os.environ)
    if not WINDOWS:
        env["QT_QPA_PLATFORM"] = "offscreen"     # no display needed
    proc = Proc([exe], env=env)
    time.sleep(seconds)
    alive = proc.p.poll() is None
    code = proc.stop(ctrl_c=False)
    detail = "running after %ds" % seconds if alive else "exited with %s: %s" % (code, proc.output()[-800:])
    report("%s starts without crashing" % os.path.basename(exe), alive, detail)


def main():
    """Parse arguments, run the checks for whatever executables exist, print a summary."""
    default = os.path.join(ROOT, "dist", "windows" if WINDOWS else "linux")
    parser = argparse.ArgumentParser(description="Smoke test of the built whiteboard executables")
    parser.add_argument("--dist", default=default, help="folder with the executables (default %(default)s)")
    parser.add_argument("--port", type=int, default=5998)
    parser.add_argument("--client-seconds", type=int, default=5)
    args = parser.parse_args()

    server = os.path.join(args.dist, "WhiteboardServer" + EXE)
    if os.path.isfile(server):
        test_server(server, args.port)
        test_server_port_busy(server, args.port)
    else:
        report("WhiteboardServer%s exists" % EXE, False, server)
    for name in ("WhiteboardClient", "WhiteboardClient-console"):
        path = os.path.join(args.dist, name + EXE)
        if os.path.isfile(path):
            test_client(path, args.client_seconds)
        else:
            report("%s%s exists" % (name, EXE), False, path)

    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
