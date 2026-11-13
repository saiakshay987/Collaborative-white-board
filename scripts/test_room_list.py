# OWNER: P3
"""test_room_list.py: Revision 5 end to end: saved rooms come back after a server restart and are listed.

Run from the project root (no display needed):  python scripts/test_room_list.py

A real server runs in this process (temp data folder, server_data/ is untouched).

  1. Two rooms are created and drawn in: ALPHA is open, BRAVO has a password.
     In ALPHA the host turns bob into a viewer. Chat messages go into both.
  2. A connection that has NOT joined asks ROOM_LIST_REQUEST and gets ROOM_LIST
     with both rooms (the dispatcher lets that one type through before join);
     any other type before join is still refused with NO_ROOM.
  3. The server is stopped (rooms saved, memory forgotten, like a process exit)
     and a new server starts and loads the autosave files.
  4. The room list of the new server shows both rooms, nobody online, with the
     right host, member count, lock flag and object count.
  5. Rejoining gives the old content, the old role (bob is still a viewer), the
     old color and the old chat; BRAVO still wants its password.
  6. The real connect dialog (offscreen) shows the rooms in its Join table and
     a double-click fills the room code, asking for the password when locked.
"""

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WB_DATA_DIR"] = tempfile.mkdtemp(prefix="wb_roomlist_")  # before server.versions loads

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import server.bootstrap  # noqa: E402,F401  (registers handlers and hooks)
import client.bootstrap  # noqa: E402,F401
from client import connect_dialog, net_client  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.net_client import NetClient  # noqa: E402
from common import protocol  # noqa: E402
from server import rooms, versions  # noqa: E402
from server import server as server_mod  # noqa: E402

from test_rejoin import Client, check, FAILS, rect_obj, snapshot_ids  # noqa: E402

qapp = QApplication(sys.argv)


def pump(seconds=1.0, until=None):
    """Run the Qt event loop for up to `seconds` (stop early when until() is true)."""
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def start_server():
    """Start a real server on a free port; return (server, port)."""
    srv = server_mod.Server("127.0.0.1", 0)
    srv.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.sock.getsockname()[1]


def create(port, name, password=None):
    """CREATE_ROOM as `name`; return (client, JOIN_OK payload, frames)."""
    c = Client(port)
    payload = {"username": name}
    if password:
        payload["password"] = password
    c.send(protocol.CREATE_ROOM, payload)
    frames = c.until(protocol.VERSIONS)
    return c, frames[0]["payload"], frames


def join(port, room, name, password=None):
    """JOIN_ROOM; return (client, first frame, all frames up to VERSIONS)."""
    c = Client(port)
    payload = {"room": room, "username": name}
    if password:
        payload["password"] = password
    c.send(protocol.JOIN_ROOM, payload)
    first = c.recv()
    if first is None or first["type"] != protocol.JOIN_OK:
        return c, first, [first]
    return c, first, [first] + c.until(protocol.VERSIONS)


def chat_texts(frames):
    """Texts of the CHAT_HISTORY in a frame list."""
    for f in frames:
        if f["type"] == protocol.CHAT_HISTORY:
            return [m["text"] for m in f["payload"]["messages"]]
    return None


def ask_room_list(port):
    """A fresh, not-joined connection: ROOM_LIST_REQUEST -> {code: row}."""
    c = Client(port)
    c.send(protocol.ROOM_LIST_REQUEST, {})
    msg = c.recv()
    c.close()
    return {r["code"]: r for r in msg["payload"]["rooms"]} if msg and msg["type"] == protocol.ROOM_LIST else None


def scenario():
    """The six steps of the module docstring."""
    srv, port = start_server()

    # 1. two rooms with content.
    ann, ok_a, _ = create(port, "ann")
    code_a = ok_a["room"]
    bob, first, _ = join(port, code_a, "bob")
    bob_color = first["payload"]["color"]
    ann.op(protocol.OP_ADD, {"obj": rect_obj("ann-1", 100)})
    ann.op(protocol.OP_ADD, {"obj": rect_obj("ann-2", 200)})
    ann.send(protocol.CHAT, {"text": "alpha hello"})
    ann.send(protocol.SET_ROLE, {"user": "bob", "role": "viewer"})
    cat, ok_c, _ = create(port, "cat", "pw")
    code_b = ok_c["room"]
    cat.op(protocol.OP_ADD, {"obj": rect_obj("cat-1", 300)})
    cat.send(protocol.CHAT, {"text": "bravo hello"})
    ann.drain()
    cat.drain()

    # 2. the room list before joining.
    rows = ask_room_list(port)
    check("2 ROOM_LIST before join has both rooms", rows is not None and set(rows) == {code_a, code_b}, rows)
    if rows:
        a, b = rows[code_a], rows[code_b]
        check("2 row fields",
              set(a) == {"code", "host", "online", "members", "locked", "objects", "updated"}, sorted(a))
        check("2 open room: host, 2 members, objects", (a["host"], a["members"], a["objects"], a["locked"]) ==
              ("ann", 2, 2, False), a)
        check("2 online count", a["online"] == 2 and b["online"] == 1, (a["online"], b["online"]))
        check("2 locked room", b["locked"] is True and b["host"] == "cat" and b["objects"] == 1, b)
    stranger = Client(port)
    stranger.send(protocol.CHAT, {"text": "hi"})
    err = stranger.recv()
    check("2 other types are still refused before join",
          err is not None and err["type"] == protocol.ERROR and err["payload"]["code"] == protocol.NO_ROOM, err)
    stranger.close()

    # 3. stop the server: everybody leaves, rooms are written, memory is forgotten, a new server loads them.
    for c in (ann, bob, cat):
        c.close()
    time.sleep(0.3)
    versions.save_all()
    srv.stop()
    with rooms.manager.lock:
        rooms.manager.rooms.clear()
    restored = versions.restore_rooms()
    check("3 both rooms restored from disk", set(restored) == {code_a, code_b}, restored)
    srv, port = start_server()

    # 4. the list of the new server.
    rows = ask_room_list(port)
    check("4 both rooms listed after restart", rows is not None and set(rows) == {code_a, code_b}, rows)
    if rows:
        a, b = rows[code_a], rows[code_b]
        check("4 nobody online", a["online"] == 0 and b["online"] == 0, (a, b))
        check("4 host, members, lock, objects survive",
              (a["host"], a["members"], a["locked"], a["objects"], b["host"], b["members"], b["locked"], b["objects"])
              == ("ann", 2, False, 2, "cat", 1, True, 1), (a, b))
        check("4 updated is a recent time", abs(time.time() - a["updated"]) < 60, a["updated"])

    # 5. rejoin: content, role, color, chat, password.
    bob, first, frames = join(port, code_a, "bob")
    check("5 bob rejoins as viewer with his old color",
          first["type"] == protocol.JOIN_OK and first["payload"]["role"] == "viewer"
          and first["payload"]["color"] == bob_color, first)
    check("5 board content is back", snapshot_ids(frames) == {"ann-1", "ann-2"}, snapshot_ids(frames))
    check("5 chat is back", chat_texts(frames) == ["alpha hello"], chat_texts(frames))
    ann, first, _ = join(port, code_a, "ann")
    check("5 the host is host again", first["payload"]["role"] == "host", first)
    _, first, _ = join(port, code_b, "dan")
    check("5 locked room: no password refused",
          first["type"] == protocol.ERROR and first["payload"]["code"] == protocol.BAD_PASSWORD, first)
    cat, first, frames = join(port, code_b, "cat", "pw")
    check("5 locked room: right password, host role, content",
          first["type"] == protocol.JOIN_OK and first["payload"]["role"] == "host"
          and snapshot_ids(frames) == {"cat-1"} and chat_texts(frames) == ["bravo hello"], first)

    # 6. the real connect dialog.
    dialog_steps(port, code_a, code_b)
    for c in (ann, bob, cat):
        c.close()
    srv.stop()


def dialog_steps(port, code_open, code_locked):
    """Step 6: the Join tab lists the rooms and a double-click fills the form."""
    # The helper used by the dialog, on its own: a dead port must give an error text, not an exception.
    rows, err = net_client.fetch_room_list("127.0.0.1", port)
    check("6 fetch_room_list returns the rows", err is None and len(rows) == 2, (rows, err))
    rows, err = net_client.fetch_room_list("127.0.0.1", 1, timeout=1)
    check("6 fetch_room_list reports a dead server", rows is None and bool(err), (rows, err))

    app = AppState()
    app.net = NetClient(app)
    dlg = connect_dialog.ConnectDialog(app)
    dlg.tabs.setCurrentIndex(1)
    dlg.join_ip.setText("127.0.0.1:%d" % port)
    dlg.find_btn.click()
    check("6 the table fills", pump(3, lambda: dlg.rooms_table.rowCount() == 2), dlg.rooms_table.rowCount())
    codes = {dlg.rooms_table.item(r, 0).text(): r for r in range(dlg.rooms_table.rowCount())}
    check("6 table shows both codes", set(codes) == {code_open, code_locked}, codes)
    if set(codes) == {code_open, code_locked}:
        r = codes[code_locked]
        cells = [dlg.rooms_table.item(r, c).text() for c in range(6)]
        check("6 locked row: host, online/total, objects, lock", cells[1:5] == ["cat", "1/1", "1", connect_dialog.LOCK_ICON],
              cells)
        asked = []
        connect_dialog.QInputDialog.getText = staticmethod(lambda *a, **k: (asked.append(a) or ("pw", True)))
        dlg.rooms_table.cellDoubleClicked.emit(r, 0)
        check("6 double-click on a locked room fills code and asks for the password",
              dlg.join_code.text() == code_locked and dlg.join_pw.text() == "pw" and len(asked) == 1,
              (dlg.join_code.text(), dlg.join_pw.text(), len(asked)))
        dlg.rooms_table.cellDoubleClicked.emit(codes[code_open], 0)
        check("6 double-click on an open room asks nothing and clears the password",
              dlg.join_code.text() == code_open and dlg.join_pw.text() == "" and len(asked) == 1)
    # auto-refresh: the timer slot lists again without a click; a refresh after the IP was changed is dropped.
    dlg.rooms_table.setRowCount(0)
    dlg._auto_refresh()
    check("6 auto-refresh refills the table", pump(3, lambda: dlg.rooms_table.rowCount() == 2))
    check("6 timer runs every 5 s while open", dlg._room_timer.isActive() and dlg._room_timer.interval() == 5000)
    dlg.done(0)
    check("6 timer stops when the dialog closes", not dlg._room_timer.isActive())


def unit_checks():
    """The pure helpers of the dialog."""
    now = 1_000_000.0
    check("format_updated", [connect_dialog.format_updated(now - s, now) for s in (5, 300, 7300)]
          == ["just now", "5 min ago", "2 h ago"])
    check("room_cells survives junk", connect_dialog.room_cells({"code": "ABC234", "online": "x"})
          == ["ABC234", "-", "0/0", "0", "", connect_dialog.format_updated(None)])


def main():
    """Run everything; exit code 1 if a check failed."""
    sys.stdout.reconfigure(errors="replace")    # a failure detail may hold the padlock character
    versions.stop()                 # no background autosave thread: the test saves explicitly
    unit_checks()
    scenario()
    print("\nFAILED: %s" % ", ".join(FAILS) if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
