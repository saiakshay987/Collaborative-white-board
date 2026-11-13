# OWNER: P1
"""test_client_session_flow.py: The real Qt client leaves a room and joins it again.

Run from the project root (no display needed):  python scripts/test_client_session_flow.py

A real server runs in this process (temp data folder). Client A is the real
GUI client (AppState, NetClient, BoardModel, MainWindow, SessionFlow of
run_client.py); client B is a bare socket. The connect dialog is replaced by
a stand-in that records what it was given and rejoins like the user would.

  1. A hosts and draws and chats, B joins, draws and chats.
  2. A: Session > Disconnect. The join screen must appear (reason shown,
     server / room code / username pre-filled) and the client must be empty:
     board, last_seq, pending, chat tabs and seen msg_ids, users, history.
  3. B keeps drawing and chatting. A joins again: board, chat (every message
     exactly once), users and versions are all back, including B's new work.
  4. The host (A) leaves and returns: host again, with the same color.
  5. A lost connection that never comes back ends at the join screen.
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["WB_DATA_DIR"] = tempfile.mkdtemp(prefix="wb_flow_")  # before server.versions loads

from PyQt5.QtWidgets import QApplication  # noqa: E402

import server.bootstrap  # noqa: E402,F401
import client.bootstrap  # noqa: E402,F401
import run_client  # noqa: E402
from client import history_panel, ops  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel  # noqa: E402
from client.main_window import MainWindow  # noqa: E402
from client.net_client import NetClient  # noqa: E402
from common import framing, protocol  # noqa: E402
from server import server as server_mod  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_rejoin import Client, check, FAILS  # noqa: E402

qapp = QApplication(sys.argv)
SCREENS = []     # one dict per join screen shown: the arguments and what the client looked like


def pump(seconds=0.5, until=None):
    """Run the Qt event loop for up to `seconds` (stop early when until() is true)."""
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def rect_obj(oid, x):
    """A valid rectangle for OP_ADD."""
    return {"id": oid, "kind": "rect", "x": x, "y": 40, "w": 80, "h": 60,
            "color": "#ff0000", "width": 2, "fill": None}


def chat_texts(app):
    """Texts shown in the Room tab of A's chat panel."""
    from client import chat_panel
    return [m["text"] for m in chat_panel._panel._views[chat_panel.ROOM_KEY]._messages]


class FakeDialog:
    """Stands in for ConnectDialog: records its arguments and the client's state, then accepts."""

    ACCEPT = True
    app = None

    def __init__(self, app, parent=None, notice=None, prefill=None):
        """Remember what the real dialog would have shown."""
        app_ = FakeDialog.app
        from client import chat_panel
        panel = chat_panel._panel
        SCREENS.append({
            "notice": notice, "prefill": dict(prefill or {}),
            "objects": len(app_.board.objects), "last_seq": app_.board.last_seq,
            "pending": len(app_.board.pending), "users": len(app_.users),
            "tabs": panel.tabs.count(), "room_msgs": len(chat_texts(app_)),
            "versions": len(history_panel.model.versions), "me_room": app_.me.room,
        })

    Accepted = 1

    def exec_(self):
        """The 'user' presses Connect with the pre-filled values."""
        return FakeDialog.Accepted if FakeDialog.ACCEPT else 0


def rejoin(app, screen, name):
    """Do what the dialog does after Connect: connect, send JOIN_ROOM, wait for JOIN_OK."""
    host, _, port = screen["prefill"]["server"].partition(":")
    app.net.connect_to(host, int(port) if port else 5050)
    assert pump(3, lambda: app.net.is_connected)
    app.net.send(protocol.JOIN_ROOM, {"room": screen["prefill"]["room"], "username": name})
    return pump(3, lambda: app.me.token is not None)


def snapshot_complete(app, ids):
    """True when the board holds exactly these object ids."""
    return set(app.board.objects) == set(ids)


def main():
    """Run the scenario."""
    srv = server_mod.Server("127.0.0.1", 0)
    srv.start()
    port = srv.sock.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    run_client.ConnectDialog = FakeDialog      # SessionFlow opens the dialog through this name
    try:
        scenario(port, srv)
    finally:
        srv.stop()
    print("\nFAILED: %s" % ", ".join(FAILS) if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


def scenario(port, srv):
    """The steps of the module docstring."""
    app = AppState()
    app.net = NetClient(app)
    app.board = BoardModel()
    FakeDialog.app = app
    app.net.connect_to("127.0.0.1", port)
    pump(3, lambda: app.net.is_connected)
    app.net.send(protocol.CREATE_ROOM, {"username": "alice"})
    check("1 alice joined", pump(3, lambda: app.me.token is not None))
    window = MainWindow(app)
    app.window = window
    flow = run_client.SessionFlow(app, window)
    app.net.disconnected.connect(flow.on_disconnected)
    room, a_color = app.me.room, app.me.color

    ops.send_op(app, protocol.OP_ADD, {"obj": rect_obj("alice-1", 100)})
    app.net.send(protocol.CHAT, {"text": "hello from alice"})
    bob = Client(port)
    bob.send(protocol.JOIN_ROOM, {"room": room, "username": "bob"})
    bob.until(protocol.VERSIONS)
    bob.op(protocol.OP_ADD, {"obj": rect_obj("bob-1", 300)})
    bob.send(protocol.CHAT, {"text": "hello from bob"})
    check("1 alice sees the work", pump(3, lambda: snapshot_complete(app, ["alice-1", "bob-1"])
                                        and len(chat_texts(app)) == 2),
          (list(app.board.objects), chat_texts(app)))
    check("1 versions list known", pump(2, lambda: history_panel.model.known))
    seq_before = app.board.last_seq
    check("1 last_seq advanced", seq_before >= 2, seq_before)

    # 2. Session > Disconnect (the confirmation box is the menu's job; its action is net.disconnect()).
    app.net.disconnect()
    check("2 join screen shown", pump(3, lambda: len(SCREENS) == 1), len(SCREENS))
    s = SCREENS[0]
    check("2 reason shown", bool(s["notice"]), s["notice"])
    check("2 prefilled", s["prefill"] == {"server": "127.0.0.1:%d" % port, "room": room,
                                          "username": "alice"}, s["prefill"])
    check("2 board empty", s["objects"] == 0 and s["last_seq"] == 0 and s["pending"] == 0, s)
    check("2 chat empty, one tab", s["room_msgs"] == 0 and s["tabs"] == 1, s)
    check("2 users and history empty", s["users"] == 0 and s["versions"] == 0, s)
    check("2 identity cleared", s["me_room"] is None, s)
    check("2 app keeps running", not flow.quitting)

    # 3. bob keeps going, alice comes back.
    bob.op(protocol.OP_ADD, {"obj": rect_obj("bob-2", 500)})
    bob.send(protocol.CHAT, {"text": "alice is gone"})
    pump(0.5)
    flow.busy = False       # the stand-in dialog "accepted" at once, as the real one does on JOIN_OK
    check("3 rejoined", rejoin(app, s, "alice"))
    check("3 board restored with bob's new work",
          pump(3, lambda: snapshot_complete(app, ["alice-1", "bob-1", "bob-2"])),
          list(app.board.objects))
    check("3 chat restored, each message once",
          pump(3, lambda: len(chat_texts(app)) == 3) and chat_texts(app) ==
          ["hello from alice", "hello from bob", "alice is gone"], chat_texts(app))
    check("3 users back", pump(2, lambda: len(app.users) == 2), app.users)
    check("3 versions list asked/pushed", pump(2, lambda: history_panel.model.known))
    check("3 same color, host", app.me.color == a_color and app.me.role == "host",
          (app.me.color, a_color, app.me.role))
    check("3 last_seq caught up", app.board.last_seq >= seq_before + 1, app.board.last_seq)
    # Draw again after the rejoin: the echo must confirm, not duplicate.
    ops.send_op(app, protocol.OP_ADD, {"obj": rect_obj("alice-2", 700)})
    check("3 can draw again", pump(3, lambda: snapshot_complete(
        app, ["alice-1", "bob-1", "bob-2", "alice-2"]) and not app.board.pending), list(app.board.objects))

    # 4. the host leaves and returns.
    app.net.disconnect()
    check("4 second join screen", pump(3, lambda: len(SCREENS) == 2))
    flow.busy = False
    check("4 host rejoined", rejoin(app, SCREENS[1], "alice"))
    check("4 host again, same color", pump(2, lambda: app.me.role == "host") and app.me.color == a_color)
    check("4 board intact", pump(3, lambda: len(app.board.objects) == 4), list(app.board.objects))

    # 5. the server goes away: auto-reconnect (shortened here from 60 s) gives up, join screen again.
    from client import net_client
    net_client.RECONNECT_FOR_S = 3
    net_client.RECONNECT_EVERY_S = 1
    flow.busy = False
    srv.stop()
    check("5 join screen after the server stopped", pump(15, lambda: len(SCREENS) == 3), len(SCREENS))
    check("5 reason mentions reconnect", len(SCREENS) == 3 and "reconnect" in (SCREENS[2]["notice"] or "").lower(),
          SCREENS[-1])
    check("5 client empty again", len(SCREENS) == 3 and SCREENS[2]["objects"] == 0
          and SCREENS[2]["room_msgs"] == 0, SCREENS[-1])
    bob.close()


if __name__ == "__main__":
    sys.exit(main())
