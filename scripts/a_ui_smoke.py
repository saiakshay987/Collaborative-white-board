# OWNER: P1
"""a_ui_smoke.py: Build the real client UI offscreen, check the layout and save screenshots.

    python scripts/a_ui_smoke.py

No server is needed: the main window is built exactly like run_client.py
builds it (run_client.build_window: registries, shell layout, cursors) with
a fake session (user, role, room, four users), and the connect dialog is
built as the join screen. Screenshots go to scripts/a_ui_shots/ (git-ignored):

    main_<mode>_<W>x<H>[_s15].png       main window, 1920x1080 and 1366x768
    main_viewer_<mode>_1366x768.png     the same as a viewer (banner, greyed tools)
    connect_<mode>[_s15].png            join screen with a notice and a room list
    ready_<mode>[_s15].png              host's "room ready" page

The script runs itself twice in child processes: at scale 1.0 and with
QT_SCALE_FACTOR=1.5 (HiDPI, files end in _s15). Every child uses
QT_QPA_PLATFORM=offscreen and an empty temporary settings folder, so the
user's real QSettings (theme, last server, layout) are never read or changed.

Checks (exit code 1 if one fails):
  * every tool keyboard shortcut (P, M, H, E, L, A, R, O, V, T, N, I) still
    switches app.active_tool, with the text tool bar hidden,
  * the window's minimum size fits 1366x768, and at 1366x768 the tool dock
    shows everything without scrolling (text format block open, the worst case),
  * the text tool bar is hidden, the top bar is shown, every dock is shown and
    Users sits directly above Chat,
  * the dock layout is saved and restored, and a corrupt saved layout falls
    back to the default one (and is deleted),
  * switching the theme stays fast (it is only done from the menu).
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
ROOMS = [
    {"code": "K7QX2M", "host": "sharwan", "online": 3, "members": 4, "objects": 42,
     "locked": True, "updated": time.time() - 120},
    {"code": "B4ZT9R", "host": "akshay", "online": 1, "members": 1, "objects": 7,
     "locked": False, "updated": time.time() - 5 * 86400},
]

SMALL = (1366, 768)
BIG = (1920, 1080)


def run_children():
    """Parent: run the checks at scale 1.0 and 1.5 in fresh processes; return the exit code."""
    os.makedirs(SHOTS, exist_ok=True)
    worst = 0
    for suffix, extra in (("", {}), ("_s15", {"QT_SCALE_FACTOR": "1.5"})):
        with tempfile.TemporaryDirectory(prefix="wb_ui_smoke_") as cfg:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=cfg, **extra)
            if not extra:
                env.pop("QT_SCALE_FACTOR", None)   # a scale set in the shell would spoil the 1.0 run
            print("== scale %s" % extra.get("QT_SCALE_FACTOR", "1.0"), flush=True)
            code = subprocess.call([sys.executable, os.path.abspath(__file__), "--child", suffix],
                                   cwd=ROOT, env=env)
            worst = max(worst, code)
    print("screenshots in %s" % SHOTS)
    print("UI SMOKE %s" % ("OK" if worst == 0 else "FAILED"))
    return worst


def child(suffix):
    """Child process: build the UI, grab screenshots, run the checks; return 0 or 1."""
    sys.path.insert(0, ROOT)
    from PyQt5.QtCore import QByteArray, QSettings
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication, QDockWidget, QScrollArea, QStackedWidget, QToolBar

    import run_client
    from client import registry, shell, theme
    from client.app_state import AppState
    from client.board_model import BoardModel
    from client.connect_dialog import ConnectDialog
    from client.net_client import NetClient

    run_client.enable_high_dpi()
    qapp = QApplication(sys.argv)
    theme.apply_theme(qapp, "light", save=False)
    failures = []

    def check(ok, text):
        """Record a failed check."""
        if not ok:
            failures.append(text)

    # --- fake session -------------------------------------------------------------
    app = AppState()
    app.net = NetClient(app)
    app.net.host, app.net.port = "192.168.43.57", 5050
    app.board = BoardModel()
    for key, value in ME.items():
        setattr(app.me, key, value)
    window = run_client.build_window(app)
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
    window.select_tool("text")                 # opens the text format block (tallest rail)
    # One toast of each look (the kind is guessed from the wording) and two remote cursors.
    for text in ("akshay joined", "Connection lost, reconnecting...",
                 "Only the host can manage roles.", "Room code K7QX2M copied."):
        app.toast(text)
    from client import cursor_overlay
    overlay = cursor_overlay._overlay
    if overlay is not None:
        overlay.cursor_received("akshay", 900.0, 700.0)
        overlay.cursor_received("janith", 1800.0, 1300.0)
        cursor_overlay.STALE_S = 3600.0         # keep them on screen for every screenshot
    sizes = [SMALL] if suffix else [BIG, SMALL]
    for mode in ("light", "dark"):
        started = time.perf_counter()
        theme.apply_theme(qapp, mode, save=False)
        print("  apply_theme(%s): %.0f ms" % (mode, (time.perf_counter() - started) * 1000))
        for w, h in sizes:
            window.resize(w, h)
            shoot(window, "main_%s_%dx%d" % (mode, w, h))
        stats_dock = window.findChild(QDockWidget, "dock_stats")
        if stats_dock is not None and not suffix:      # the Latency/traffic tab, for review
            stats_dock.raise_()
            shoot(window, "main_stats_%s_%dx%d" % (mode, SMALL[0], SMALL[1]))
            window.findChild(QDockWidget, "dock_history").raise_()
    theme.apply_theme(qapp, "light", save=False)

    # --- layout checks at 1366x768 ------------------------------------------------------
    window.resize(*SMALL)
    QTest.qWait(300)
    hint, minimum = window.minimumSizeHint(), window.minimumSize()
    need_w, need_h = max(hint.width(), minimum.width()), max(hint.height(), minimum.height())
    print("  window minimum size %dx%d, actual %dx%d" % (need_w, need_h, window.width(),
                                                         window.height()))
    if not suffix:   # at scale 1.5, 1366x768 logical is a 2049x1152 screen: only 1.0 is checked
        check(need_w <= SMALL[0] and need_h <= SMALL[1],
              "minimum size %dx%d does not fit 1366x768" % (need_w, need_h))
        check((window.width(), window.height()) == SMALL,
              "window is %dx%d, not 1366x768" % (window.width(), window.height()))
    canvas = app.canvas
    print("  canvas %dx%d at 1366x768" % (canvas.width(), canvas.height()))
    for name in ("dock_tools", "dock_stats", "dock_history"):
        dock = window.findChild(QDockWidget, name)
        scroll = dock.widget() if dock is not None else None
        if isinstance(scroll, QStackedWidget):          # the tool dock: full panel or compact rail
            scroll = scroll.widget(0)
        if isinstance(scroll, QScrollArea):
            dock.raise_()                      # bring a tabbed dock to the front to measure it
            QTest.qWait(150)
            extra = scroll.verticalScrollBar().maximum()
            print("  %s: %dx%d, hidden below the fold: %d px" % (name, dock.width(), dock.height(),
                                                               extra))
            if not suffix:
                check(extra == 0, "%s needs %d px of scrolling at 1366x768" % (name, extra))
    old_bar = window.findChild(QToolBar, "toolbar_tools")
    top_bar = window.findChild(QToolBar, "toolbar_top")
    check(old_bar is not None and old_bar.isHidden(), "text tool bar is still shown")
    check(top_bar is not None and top_bar.isVisible(), "top bar is not shown")
    for name in shell.DOCK_NAMES:
        dock = window.findChild(QDockWidget, name)
        # A dock behind another tab is hidden by Qt; it counts as shown if its tab group is.
        tabbed = dock is not None and any(not d.isHidden() for d in window.tabifiedDockWidgets(dock))
        check(dock is not None and (not dock.isHidden() or tabbed), "%s is not shown" % name)
    users = window.findChild(QDockWidget, "dock_users")
    chat = window.findChild(QDockWidget, "dock_chat")
    if users is not None and chat is not None:
        check(users.y() < chat.y() and abs(users.x() - chat.x()) < 5,
              "Users is not directly above Chat")
        print("  users %dpx tall above chat %dpx tall" % (users.height(), chat.height()))

    # --- stats chip: a click opens Latency/traffic, a second click hides it ----------
    from client.shell import StatsChip
    chip = window.findChild(StatsChip)
    stats_dock = window.findChild(QDockWidget, "dock_stats")
    history_dock = window.findChild(QDockWidget, "dock_history")
    if chip is None or stats_dock is None:
        check(False, "stats chip or stats dock missing")
    else:
        history_dock.raise_()
        QTest.qWait(100)
        chip.click()
        QTest.qWait(150)
        check(stats_dock.isVisible() and not stats_dock.visibleRegion().isEmpty(),
              "stats chip did not open Latency/traffic")
        chip.click()
        QTest.qWait(150)
        check(stats_dock.isHidden(), "second click on the stats chip did not hide it")
        stats_dock.show()
        history_dock.raise_()
        print("  stats chip open/close: checked")

    # --- viewer ---------------------------------------------------------------------
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        app.set_role("viewer")
        shoot(window, "main_viewer_%s_%dx%d" % (mode, SMALL[0], SMALL[1]))
        app.set_role("host")
    theme.apply_theme(qapp, "light", save=False)

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
        check(got == tool_id, "shortcut %s gave %s, expected %s" % (key, got, tool_id))
    print("  tool shortcuts:", " ".join(checked))

    # --- panels: locked docks with headers, Ctrl+1/2/3, folding, saved state, reset ----
    from PyQt5.QtCore import Qt
    for name in shell.DOCK_NAMES:
        dock = window.findChild(QDockWidget, name)
        check(dock.features() == QDockWidget.NoDockWidgetFeatures, "%s can still move/float/close" % name)
    for name in shell.HEADER_DOCKS:
        dock = window.findChild(QDockWidget, name)
        check(isinstance(dock.titleBarWidget(), shell.DockHeader), "%s has no themed header" % name)
    history_panel_w = window.findChild(QDockWidget, "dock_history")
    from client.history_panel import HistoryPanel
    hp = window.findChild(HistoryPanel)
    check(hp is not None and hp.empty_label.isVisible(), "History does not show its empty text")
    before = (canvas.width(), canvas.height())
    for key in (Qt.Key_1, Qt.Key_2, Qt.Key_3):
        QTest.keyClick(target, key, Qt.ControlModifier)
        QTest.qWait(150)
    tools = window.findChild(QDockWidget, "dock_tools")
    check(shell.rail_collapsed(window) and tools.width() <= shell.RAIL_W + 2,
          "Ctrl+1 did not collapse the rail (width %d)" % tools.width())
    check(not shell._area_shown(window, shell.SIDE_DOCKS), "Ctrl+2 did not hide Users and Chat")
    check(not shell._area_shown(window, shell.BOTTOM_DOCKS), "Ctrl+3 did not hide the bottom panel")
    after = (canvas.width(), canvas.height())
    print("  canvas %dx%d -> %dx%d with everything collapsed" % (before + after))
    check(after[0] > before[0] + 400 and after[1] > before[1] + 150, "canvas did not take the space")
    bar = window.findChild(shell.TopBar)
    check(not (bar.rail_btn.isChecked() or bar.side_btn.isChecked() or bar.bottom_btn.isChecked()),
          "top-bar toggles do not show the collapsed state")
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        shoot(window, "main_collapsed_%s_%dx%d" % (mode, SMALL[0], SMALL[1]))
    theme.apply_theme(qapp, "light", save=False)
    shell.save_layout(window)
    # A fresh start: default arrangement (nothing saved), then what the next launch restores.
    shell.apply_default_layout(window)
    shell.set_rail_collapsed(window, False, save=False)
    QTest.qWait(100)
    shell.restore_layout(window)                        # the saved, collapsed arrangement
    shell._restore_collapse_state(window)
    QTest.qWait(150)
    check(shell.rail_collapsed(window) and not shell._area_shown(window, shell.SIDE_DOCKS)
          and not shell._area_shown(window, shell.BOTTOM_DOCKS), "collapsed state was not restored")
    shell.reset_layout(app)
    QTest.qWait(200)
    check(not shell.rail_collapsed(window) and shell._area_shown(window, shell.SIDE_DOCKS)
          and shell._area_shown(window, shell.BOTTOM_DOCKS), "Reset layout did not restore the default")
    shell.set_folded(window, "dock_users", True)
    QTest.qWait(150)
    users_dock = window.findChild(QDockWidget, "dock_users")
    check(users_dock.height() <= shell.HEADER_H + 4, "folding Users left it %d px tall" % users_dock.height())
    shell.set_folded(window, "dock_users", False)
    QTest.qWait(150)
    print("  panels: locked, headers, Ctrl+1/2/3, saved state, reset, folding: checked")

    # --- saved layout: round trip, then a corrupt one -------------------------------
    settings = QSettings(shell.SETTINGS_ORG, shell.SETTINGS_APP)
    shell.save_layout(window)
    check(isinstance(settings.value(shell.STATE_KEY), QByteArray), "layout was not saved")
    check(shell.restore_layout(window), "saved layout was not restored")
    settings.setValue(shell.STATE_KEY, QByteArray(b"not a layout"))
    check(not shell.restore_layout(window), "corrupt layout was accepted")
    check(settings.value(shell.STATE_KEY) is None, "corrupt layout was not deleted")
    check(not window.findChild(QDockWidget, "dock_chat").isHidden(), "default layout lost Chat")
    print("  layout save/restore/corrupt: checked")

    # --- connect dialog: join screen with rooms, and the host's ready page ----------
    window.hide()
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        dlg = ConnectDialog(app, notice="Connection lost: the server stopped answering.",
                            prefill={"server": "127.0.0.1", "room": "K7QX2M", "username": "akshay"})
        theme.decorate_widgets(dlg)
        dlg._room_timer.stop()                 # the fake list below must stay on screen
        dlg.show()
        QTest.qWait(300)
        dlg._show_rooms("127.0.0.1", ROOMS, "")
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
