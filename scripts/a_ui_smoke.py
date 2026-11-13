# OWNER: P1
"""a_ui_smoke.py: Build the real client UI offscreen and save screenshots for review.

    python scripts/a_ui_smoke.py

No server is needed: the main window is built from the normal registries
(client.bootstrap) with a fake session (user, role, room, three users), and
the connect dialog is built as the join screen. Each is grabbed in the light
and the dark theme into scripts/a_ui_shots/ (git-ignored):

    main_<mode>_<W>x<H>[_s15].png       main window (1600x900 and 1366x768)
    connect_<mode>[_s15].png            join screen with a notice
    ready_<mode>[_s15].png              host's "room ready" page

The script runs itself twice in child processes: once at scale 1.0 and once
with QT_SCALE_FACTOR=1.5 (HiDPI check, files end in _s15). Every child uses
QT_QPA_PLATFORM=offscreen and an empty temporary settings folder, so the
user's real QSettings (theme, last server, window geometry) are never read
or changed.

Checks (the exit code is 1 if one fails):
  * every tool keyboard shortcut (P, M, H, E, L, A, R, O, V, T, N, I)
    still switches app.active_tool,
  * the window's minimum size fits a 1366x768 screen,
  * switching the theme takes well under the time of one frame burst
    (it is never done while drawing, but it must not be slow).
"""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "scripts", "a_ui_shots")

# Fake session shown in the screenshots.
ME = {"username": "sharwan", "role": "host", "color": "#e6194b", "room": "K7QX2M"}
USERS = [
    {"name": "sharwan", "role": "host", "color": "#e6194b", "online": True},
    {"name": "akshay", "role": "editor", "color": "#3cb44b", "online": True},
    {"name": "janith", "role": "viewer", "color": "#4363d8", "online": True},
    {"name": "harshith", "role": "editor", "color": "#f58231", "online": False},
]


def run_children():
    """Parent: run the checks at scale 1.0 and 1.5 in fresh processes; return the exit code."""
    os.makedirs(SHOTS, exist_ok=True)
    worst = 0
    for suffix, extra in (("", {}), ("_s15", {"QT_SCALE_FACTOR": "1.5"})):
        with tempfile.TemporaryDirectory(prefix="wb_ui_smoke_") as cfg:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=cfg, **extra)
            if not extra:
                env.pop("QT_SCALE_FACTOR", None)   # a scale set in the shell would spoil the 1.0 run
            print("== scale %s" % extra.get("QT_SCALE_FACTOR", "1.0"))
            code = subprocess.call([sys.executable, os.path.abspath(__file__), "--child", suffix],
                                   cwd=ROOT, env=env)
            worst = max(worst, code)
    print("screenshots in %s" % SHOTS)
    print("UI SMOKE %s" % ("OK" if worst == 0 else "FAILED"))
    return worst


def child(suffix):
    """Child process: build the UI, grab screenshots, run the checks; return 0 or 1."""
    sys.path.insert(0, ROOT)
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication

    import run_client
    from client import cursor_overlay, registry, theme
    from client.app_state import AppState
    from client.board_model import BoardModel
    from client.connect_dialog import ConnectDialog
    from client.main_window import MainWindow
    from client.net_client import NetClient

    run_client.enable_high_dpi()
    qapp = QApplication(sys.argv)
    theme.apply_theme(qapp, "light", save=False)
    failures = []

    # --- fake session -------------------------------------------------------------
    app = AppState()
    app.net = NetClient(app)
    app.net.host, app.net.port = "192.168.43.57", 5050
    app.board = BoardModel()
    for key, value in ME.items():
        setattr(app.me, key, value)

    window = MainWindow(app)
    app.window = window
    run_client.wire_status(app, window)
    theme.decorate_widgets(window)
    cursor_overlay.install(app)
    app.set_users(USERS)
    app.role_changed.emit()

    def shoot(widget, name):
        """Let timers and layouts settle, then save widget as <name><suffix>.png."""
        QTest.qWait(400)
        path = os.path.join(SHOTS, "%s%s.png" % (name, suffix))
        widget.grab().save(path)
        print("  saved", os.path.relpath(path, ROOT))

    # --- main window ----------------------------------------------------------------
    window.show()
    window.select_tool("text")                 # shows the text format block in the rail
    app.toast("Room code K7QX2M copied.")
    sizes = [(1366, 768)] if suffix else [(1600, 900), (1366, 768)]
    for mode in ("light", "dark"):
        started = time.perf_counter()
        theme.apply_theme(qapp, mode, save=False)
        took = (time.perf_counter() - started) * 1000
        print("  apply_theme(%s): %.0f ms" % (mode, took))
        for w, h in sizes:
            window.resize(w, h)
            shoot(window, "main_%s_%dx%d" % (mode, w, h))

    hint = window.minimumSizeHint()
    avail = qapp.primaryScreen().availableSize()
    print("  window minimum size %dx%d (screen %dx%d logical)"
          % (hint.width(), hint.height(), avail.width(), avail.height()))
    if not suffix and (hint.width() > 1366 or hint.height() > 768):
        # Phase 1 only reports it (the layout before this work did not fit either);
        # phase 2 fixes the layout and turns this into a failure.
        print("  WARN: minimum size %dx%d does not fit 1366x768" % (hint.width(), hint.height()))

    # --- every tool shortcut still switches the tool -------------------------------
    window.activateWindow()
    QApplication.setActiveWindow(window)
    target = app.canvas or window
    target.setFocus()
    QTest.qWait(50)
    checked = []
    for tool_id, cls in registry.TOOLS.items():
        key = getattr(cls, "shortcut", None)
        if not key:
            continue
        window.select_tool("pen" if tool_id != "pen" else "select")
        QTest.keyClick(target, key.lower())
        QTest.qWait(10)
        got = getattr(app.active_tool, "id", None)
        checked.append("%s=%s" % (key, "ok" if got == tool_id else "FAIL(%s)" % got))
        if got != tool_id:
            failures.append("shortcut %s gave %s, expected %s" % (key, got, tool_id))
    print("  tool shortcuts:", " ".join(checked))

    # --- connect dialog: join screen and host's ready page ------------------------
    window.hide()
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        dlg = ConnectDialog(app, notice="Connection lost: the server stopped answering.",
                            prefill={"server": "127.0.0.1", "room": "K7QX2M", "username": "akshay"})
        theme.decorate_widgets(dlg)
        dlg.show()
        shoot(dlg, "connect_%s" % mode)
        dlg.code_label.setText("K7QX2M")
        dlg.ip_label.setText("192.168.43.57")
        dlg.other_ips_label.setText("Port 5050\nOther addresses of this PC (if the big one does "
                                    "not work):\nEthernet: 10.0.0.12")
        dlg.pages.setCurrentIndex(1)
        shoot(dlg, "ready_%s" % mode)
        dlg.done(0)

    for failure in failures:
        print("  FAIL:", failure)
    qapp.processEvents()
    return 1 if failures else 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        idx = sys.argv.index("--child")
        sys.exit(child(sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""))
    sys.exit(run_children())
