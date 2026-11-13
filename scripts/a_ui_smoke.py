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
    menu_file_<mode>.png, menu_view_<mode>.png, tooltip_<mode>.png,
    command_bar_<mode>_1366x768.png, roles_<mode>.png, shortcuts_help_<mode>.png
                                        (scale 1.0 only, for review)

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
  * switching the theme stays fast (it is only done from the menu),
  * contrast: every text/background token pair of both themes is 4.5:1 or more,
  * shortcuts: all of them are listed, none is bound twice; every tool button's
    tooltip names its key,
  * icon sizes: icon-only buttons theme.ICON_SIZE (20), icons next to text
    theme.ICON_TEXT (16),
  * keyboard: Tab in the main window goes top bar, tools, canvas, bottom, right,
    status; Tab in the connect dialog reaches every field (no trap) and the
    arrows switch Host / Join,
  * dark mode: no light boxes (except the white page, the pen preview and colour
    swatches) in the main window (host and viewer), connect dialog, menus,
    command bar, Manage roles and the shortcuts help.
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
    from common import config
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
        dlg._show_rooms("127.0.0.1:%d" % config.PORT, ROOMS, "")
        shoot(dlg, "connect_%s" % mode)
        dlg.code_label.setText("K7QX2M")
        dlg.ip_label.setText("192.168.43.57")
        dlg.other_ips_label.setText("Port 5050\nOther addresses of this PC (if the big one does "
                                    "not work):\nEthernet: 10.0.0.12")
        dlg.pages.setCurrentIndex(1)
        shoot(dlg, "ready_%s" % mode)
        dlg.done(0)

    window.show()
    phase5_checks(qapp, app, window, check, shoot, suffix)

    for failure in failures:
        print("  FAIL:", failure)
    qapp.processEvents()
    return 1 if failures else 0


# --- phase 5: contrast, keyboard, icon sizes, dark-mode surfaces --------------------

# Text token on background token: every pair the style sheet uses for readable text.
TEXT_PAIRS = ([("text", b) for b in ("bg", "surface", "surface_alt", "hover", "pressed", "input_bg",
                                     "primary_soft")]
              + [("text_soft", b) for b in ("bg", "surface", "surface_alt", "hover")]
              + [("muted", b) for b in ("bg", "surface", "surface_alt", "input_bg", "hover")]
              + [("primary_text", "primary_soft"), ("primary_text", "surface"),
                 ("primary_text", "surface_alt"), ("on_primary", "primary"),
                 ("on_primary", "primary_hover"), ("success_text", "success_soft"),
                 ("warning_text", "warning_soft"), ("danger_text", "danger_soft"),
                 ("success_text", "surface"), ("warning_text", "surface"), ("danger_text", "surface"),
                 ("tooltip_text", "tooltip_bg")])

# Widgets that may stay light in dark mode: the white page and small pieces of it
# (the pen preview), and colour swatches (their fill is the colour itself).
LIGHT_ALLOWED = {"CanvasWidget", "WidthPreview", "swatch", "inkSwatch", "currentSwatch"}


def _short(widget):
    """'Class[objectName|text]' for messages."""
    text = ""
    for getter in ("text", "placeholderText", "toolTip"):
        text = text or (getattr(widget, getter, lambda: "")() or "")
    return "%s[%s|%s]" % (widget.metaObject().className(), widget.objectName(), text[:24])


def tab_walk(start, steps):
    """Press Tab `steps` times from start; return the widgets that got the focus, in order."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication
    start.setFocus()
    QTest.qWait(10)
    seen = []
    for _ in range(steps):
        widget = QApplication.focusWidget()
        if widget is None:
            break
        seen.append(widget)
        QTest.keyClick(widget, Qt.Key_Tab)
        QTest.qWait(2)
    return seen


def light_boxes(root):
    """Visible widgets of root that are mostly light pixels (lightness > 200) on screen."""
    from PyQt5.QtWidgets import QWidget
    image = root.grab().toImage()
    ratio = image.devicePixelRatio()       # 1.5 in the QT_SCALE_FACTOR run: pixels, not points
    found = []
    for w in root.findChildren(QWidget):
        if not w.isVisible() or w.width() < 12 or w.height() < 12:
            continue
        if w.metaObject().className() in LIGHT_ALLOWED or w.objectName() in LIGHT_ALLOWED:
            continue
        if any(a.metaObject().className() in LIGHT_ALLOWED for a in _ancestors(w, root)):
            continue
        top_left = w.mapTo(root, w.rect().topLeft())
        total = light = 0
        for y in range(top_left.y() + 2, top_left.y() + w.height() - 2, max(1, w.height() // 10)):
            for x in range(top_left.x() + 2, top_left.x() + w.width() - 2, max(1, w.width() // 10)):
                px, py = int(x * ratio), int(y * ratio)
                if 0 <= px < image.width() and 0 <= py < image.height():
                    total += 1
                    light += image.pixelColor(px, py).lightness() > 200
        if total and light > 0.6 * total:
            found.append(_short(w))
    if found:          # keep what was scanned, to see what looked light
        image.save(os.path.join(SHOTS, "lightbox_%s.png" % root.metaObject().className()))
    return found


def _ancestors(widget, root):
    """The parents of widget up to (not including) root."""
    out, w = [], widget.parentWidget()
    while w is not None and w is not root:
        out.append(w)
        w = w.parentWidget()
    return out


def phase5_checks(qapp, app, window, check, shoot, suffix):
    """Contrast of the theme tokens, shortcuts, tooltips, icon sizes, Tab order, dark surfaces."""
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import (QAbstractButton, QAction, QApplication, QDockWidget, QMenu,
                                 QShortcut, QTabBar, QToolBar, QToolButton, QToolTip)
    from common import config
    from client import connect_dialog, main_window, registry, shell, shortcuts, theme

    # 1. contrast: computed from the tokens, both modes.
    worst = []
    for mode in theme.MODES:
        t = theme.TOKENS[mode]
        for fg, bg in TEXT_PAIRS:
            ratio = theme.contrast_ratio(t[fg], t[bg])
            check(ratio >= 4.5, "%s: %s on %s is %.2f:1 (needs 4.5)" % (mode, fg, bg, ratio))
            worst.append((ratio, mode, fg, bg))
    ratio, mode, fg, bg = min(worst)
    print("  contrast: %d text pairs x 2 modes >= 4.5:1 (lowest %.2f, %s %s on %s)"
          % (len(TEXT_PAIRS), ratio, mode, fg, bg))

    # 2. shortcuts: list them all, no key bound twice.
    keys = {}
    for action in window.findChildren(QAction):
        for seq in action.shortcuts():
            if not seq.isEmpty():
                keys.setdefault(seq.toString(), []).append(action.text())
    for sc in window.findChildren(QShortcut):
        keys.setdefault(sc.key().toString(), []).append("shortcut")
    for key, users in sorted(keys.items()):
        check(len(users) == 1, "key %s is bound %d times: %s" % (key, len(users), users))
    print("  shortcuts (%d, no duplicates): %s" % (len(keys), " ".join(sorted(keys))))
    for key in ("PgUp", "PgDown"):
        print("  %s is %s" % (key, "free" if key not in keys else "taken by %s" % keys[key]))

    # 3. every tool button names its shortcut.
    tips = 0
    for btn in window.findChildren(QAbstractButton):
        tool_id = btn.property("toolId")
        if tool_id is None and btn.property("railCompact"):
            tool_id = next((t for t, c in registry.TOOLS.items()
                            if btn.toolTip().startswith(getattr(c, "label", t))), None)
        cls = registry.TOOLS.get(tool_id)
        if cls is not None and getattr(cls, "shortcut", None):
            tips += 1
            check("(%s)" % cls.shortcut in btn.toolTip(),
                  "tooltip %r has no shortcut %s" % (btn.toolTip(), cls.shortcut))
    print("  tool tooltips with their key: %d buttons" % tips)

    # 4. icon sizes: icon-only buttons theme.ICON_SIZE, icons next to text theme.ICON_TEXT.
    sizes = {}
    for btn in window.findChildren(QAbstractButton):
        if (btn.icon().isNull() or btn.objectName().startswith("qt_")
                or isinstance(btn.parentWidget(), QToolBar) and btn.parentWidget().isHidden()):
            continue                             # Qt's own buttons, the hidden text tool bar
        icon_only = (not btn.text() or bool(btn.property("railCompact"))
                     or (isinstance(btn, QToolButton)
                         and btn.toolButtonStyle() == Qt.ToolButtonIconOnly))
        if btn.objectName() == "formatToggle":
            want = theme.ICON_TEXT                   # fixed 26 x 24 by P2, matches B / I / U
        else:
            want = theme.ICON_SIZE if icon_only else theme.ICON_TEXT
        got = btn.iconSize().width()
        sizes[got] = sizes.get(got, 0) + 1
        check(got == want, "%s icon is %d px, expected %d" % (_short(btn), got, want))
    print("  icon sizes: %s" % ", ".join("%d px x %d" % kv for kv in sorted(sizes.items())))

    # 5. Tab order: main window and connect dialog, no trap.
    window.activateWindow()
    QApplication.setActiveWindow(window)
    QTest.qWait(50)
    bar = window.findChild(shell.TopBar)
    walk = tab_walk(bar.copy_btn, 120)
    names = [_short(w) for w in walk]
    order = []
    for w in walk:
        region = ("top bar" if bar.isAncestorOf(w) else "canvas" if w is app.canvas
                  else "bottom tabs" if isinstance(w, QTabBar) and w.parentWidget() is window
                  else None)
        if region is None:
            for dock in ("dock_tools", "dock_history", "dock_stats", "dock_users", "dock_chat"):
                d = window.findChild(QDockWidget, dock)
                if d is not None and d.isAncestorOf(w):
                    region = dock[5:]
                    break
        region = region or ("status" if window.statusBar().isAncestorOf(w) else "?")
        if not order or order[-1] != region:
            order.append(region)
    print("  main window Tab order: %s" % " > ".join(order[:12]))
    check(order[:3] == ["top bar", "tools", "canvas"], "Tab order starts %s" % order[:3])
    check("?" not in order, "Tab reaches widgets outside the panels: %s" % names)
    check(len(walk) == 120 or walk[-1] is not None, "Tab lost the focus")
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        dlg = connect_dialog.ConnectDialog(app)
        dlg._room_timer.stop()
        dlg.show()
        dlg.activateWindow()
        QApplication.setActiveWindow(dlg)
        QTest.qWait(150)
        if mode == "light":
            check(QApplication.focusWidget() is dlg.host_name,
                  "connect dialog does not start in the name field (%s)"
                  % _short(QApplication.focusWidget()) if QApplication.focusWidget() else None)
            for page, fields in ((0, [dlg.host_name, dlg.host_pw]),
                                 (1, [dlg.join_ip, dlg.join_code, dlg.join_name, dlg.join_pw])):
                dlg.tabs.setCurrentIndex(page)
                QTest.qWait(30)
                walk = tab_walk(dlg._segments.button(page), 14)
                check(len(set(map(id, walk))) > 3, "Tab is stuck in the connect dialog: %s"
                      % [_short(w) for w in walk])
                missing = [_short(f) for f in fields + [dlg.go_btn] if f not in walk]
                check(not missing, "Tab never reaches %s on page %d" % (missing, page))
                print("  connect dialog Tab order (%s): %s" % (("Host", "Join")[page],
                      " > ".join(_short(w) for w in walk[:len(set(map(id, walk)))])))
            QTest.keyClick(dlg._segments.button(0), Qt.Key_Right)
            check(dlg.tabs.currentIndex() == 1, "Right arrow on the switch did not open Join")
            QTest.keyClick(dlg._segments.button(1), Qt.Key_Left)
            check(dlg.tabs.currentIndex() == 0, "Left arrow on the switch did not open Host")
        dlg.tabs.setCurrentIndex(1)
        dlg._show_rooms("127.0.0.1:%d" % config.PORT,
                        ROOMS, "")
        dlg.join_ip.setText("127.0.0.1")
        dlg._show_rooms("127.0.0.1:%d" % config.PORT,
                        ROOMS, "")
        QTest.qWait(50)
        if mode == "dark":
            bad = light_boxes(dlg)
            check(not bad, "light boxes in the dark connect dialog: %s" % bad)
        dlg.done(0)

    # 6. dark mode: no light boxes; menus, tooltip, command bar and dialogs for review.
    for mode in ("light", "dark"):
        theme.apply_theme(qapp, mode, save=False)
        window.resize(*SMALL)
        QTest.qWait(200)
        if mode == "dark":
            for role in ("host", "viewer"):
                app.set_role(role)
                QTest.qWait(700)                 # the role toast fades in over the page first
                bad = light_boxes(window)
                check(not bad, "light boxes in the dark main window (%s): %s" % (role, bad))
            app.set_role("host")
        if suffix:
            continue                             # the extra screenshots only at scale 1.0
        menus = {m.title(): m for m in window.menuBar().findChildren(QMenu) if m.title()}
        for title in ("File", "View"):
            menu = menus.get(title)
            if menu is not None:
                menu.popup(window.mapToGlobal(QPoint(20, 30)))
                QTest.qWait(150)
                if mode == "dark":
                    bad = light_boxes(menu)
                    check(not bad, "light boxes in the dark %s menu: %s" % (title, bad))
                shoot(menu, "menu_%s_%s" % (title.lower(), mode))
                menu.hide()
        bar = window.findChild(shell.TopBar)
        QToolTip.showText(bar.undo_btn.mapToGlobal(QPoint(10, 30)), bar.undo_btn.toolTip(), bar.undo_btn)
        QTest.qWait(200)
        tip = next((w for w in QApplication.topLevelWidgets()
                    if w.metaObject().className() == "QTipLabel" and w.isVisible()), None)
        if tip is not None:
            shoot(tip, "tooltip_%s" % mode)
        QToolTip.hideText()
        shortcuts.open_command_bar(app)
        QTest.qWait(100)
        command_bar = window.findChild(shortcuts.CommandBar)
        if command_bar is not None:
            command_bar._edit.setText("re")
            if mode == "dark":
                bad = light_boxes(command_bar)
                check(not bad, "light boxes in the dark command bar: %s" % bad)
            shoot(window, "command_bar_%s_%dx%d" % (mode, SMALL[0], SMALL[1]))
            command_bar._edit.clear()
            command_bar.hide()
        main_window.open_manage_roles(app)
        QTest.qWait(150)
        roles = main_window._dialog
        if roles is not None:
            if mode == "dark":
                bad = light_boxes(roles)
                check(not bad, "light boxes in the dark Manage roles dialog: %s" % bad)
            shoot(roles, "roles_%s" % mode)
            roles.close()
        shortcuts.show_shortcuts_help(app)
        QTest.qWait(150)
        helper = shortcuts._help_dialog
        if helper is not None:
            if mode == "dark":
                bad = light_boxes(helper)
                check(not bad, "light boxes in the dark shortcuts help: %s" % bad)
            shoot(helper, "shortcuts_help_%s" % mode)
            helper.close()
    theme.apply_theme(qapp, "light", save=False)
    print("  dark mode: main window (host, viewer), connect dialog, menus, command bar, "
          "dialogs: checked for light boxes")


if __name__ == "__main__":
    if "--child" in sys.argv:
        idx = sys.argv.index("--child")
        sys.exit(child(sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""))
    sys.exit(run_children())
