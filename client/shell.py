# OWNER: P1
"""shell.py: Arranges the finished main window from outside: top bar, compact tool rail,
dock layout, status-bar pills, the stats footer chip and the saved layout.

main_window.py (P4) builds the window from the registries; this module only
rearranges and restyles what it built, through public Qt API and the
objectNames main_window gives its docks (dock_tools, dock_users, dock_chat,
dock_history, dock_stats) and its tool bar (toolbar_tools). No other owner's
file is edited. run_client.build_window() calls install(app, window) once,
right after the window exists and before it is shown.

What install() does
    * Hides the text tool bar (Pen, Marker, ... Image). Its QActions hold the
      tool shortcuts (P, M, H, ...); actions of a hidden tool bar no longer fire,
      so they are also added to the window itself, where they keep working.
    * Adds TopBar: room-code pill + copy, server address, connection chip,
      Undo, Redo and the theme toggle.
    * Compacts P2's tool dock: icon-only tool buttons in a 4-column grid,
      small groups side by side, the colour palette in 6 columns. The buttons
      are P2's own, so tool selection, per-tool memory and the viewer lock
      work exactly as before.
    * Right side: Users (short) above Chat (the rest of the height), both
      always visible. Bottom: History and Latency/traffic tabbed in one short
      dock. Panels sit in scroll areas so a tall panel can never make the
      window taller than a 1366x768 screen.
    * Status bar: pills for name and role, room, address and connection, plus
      the stats chip (my RTT coloured, KB/s) that opens Latency/traffic.
    * The View-only banner uses the theme's warning colours.
    * Remembers the dock layout (QMainWindow.saveState) in QSettings and
      falls back to the default layout when the saved one cannot be restored.
      View > Reset layout restores the default at any time.

Everything runs on the GUI thread; the widgets here follow AppState and
NetClient signals and never touch a socket.
"""

from PyQt5.QtCore import QByteArray, QSettings, QSize, Qt
from PyQt5.QtWidgets import (QApplication, QDockWidget, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolBar, QToolButton,
                             QWidget)

from client import icons, registry, shortcuts, stats_panel, theme
from client.main_window import ViewOnlyBanner

# Layout sizes (logical pixels).
TOOL_BUTTON = 34          # square icon-only tool button
TOOL_COLUMNS = 4          # tool buttons per row inside a group
SWATCH_COLUMNS = 6        # colour swatches per row
LEFT_WIDTH = 204
RIGHT_WIDTH = 280
USERS_HEIGHT = 190        # Users starts a few rows tall; Chat takes the rest
BOTTOM_HEIGHT = 240       # History / Latency starts short; the canvas gets the rest

# QSettings: the same file as the rest of the client.
SETTINGS_ORG = "CN Lab"
SETTINGS_APP = "LAN Whiteboard"
STATE_KEY = "window/dock_state"
STATE_VERSION = 2         # bump when the default arrangement changes; old states are ignored

DOCK_NAMES = ("dock_tools", "dock_users", "dock_chat", "dock_history", "dock_stats")
SCROLLED_DOCKS = ("dock_tools", "dock_history", "dock_stats")
# Tabbed together at the bottom: their tabs name them, so they need no title bar.
UNTITLED_DOCKS = ("dock_history", "dock_stats")

# Text of a role pill and the pill colour of each role.
ROLE_PILLS = {"host": "primary", "editor": "neutral", "viewer": "warning"}


def _safely(fn, *args):
    """Run one install step; a widget that is missing or changed only skips that step."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001  (the window must open even if one step fails)
        print("[shell] step %s skipped: %s" % (fn.__name__, exc))
        return None


def _dock(window, name):
    """Return the dock with objectName name, or None."""
    return window.findChild(QDockWidget, name)


def _pill(text="", kind="neutral", tip=""):
    """Return a QLabel shown as a pill badge."""
    label = QLabel(text)
    label.setToolTip(tip)
    theme.set_pill(label, kind)
    return label


def _icon_button(name, tip, slot):
    """Return a flat icon-only QToolButton (20 px icon) that calls slot when clicked."""
    btn = QToolButton()
    btn.setProperty("iconName", name)
    btn.setToolTip(tip)
    btn.setAutoRaise(True)
    btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
    btn.clicked.connect(slot)
    return btn


def _paint_icon(btn):
    """(Re)paint the icon of a button made by _icon_button in the current theme colours."""
    t = theme.tokens()
    btn.setIcon(icons.get_state_icon(btn.property("iconName"), theme.ICON_SIZE,
                                     normal=t["text_soft"], checked=t["primary_text"],
                                     disabled=t["disabled_text"], active=t["text"]))


def server_address(app):
    """Return 'host:port' of the current server, or '' before the first connect."""
    net = app.net
    return "%s:%s" % (net.host, net.port) if net is not None and net.host else ""


# --- connection chip ------------------------------------------------------------------

class ConnectionChip(QLabel):
    """Pill that shows Connected (green), Reconnecting... (amber) or Disconnected (red).

    Driven only by NetClient's Qt signals, which are emitted on the GUI
    thread, so updating the label is thread-safe.
    """

    # state -> (text, pill kind)
    STATES = {
        "connected": ("Connected", "success"),
        "reconnecting": ("Reconnecting...", "warning"),
        "disconnected": ("Disconnected", "danger"),
    }

    def __init__(self, net, parent=None):
        """Start as Connected (the window opens only after JOIN_OK) and follow net."""
        super().__init__(parent)
        self.state = None
        self.set_state("connected")
        net.connected.connect(lambda: self.set_state("connected"))
        net.reconnected.connect(lambda: self.set_state("connected"))
        net.reconnecting.connect(lambda: self.set_state("reconnecting"))
        net.disconnected.connect(lambda _reason: self.set_state("disconnected"))

    def set_state(self, state):
        """Show one of STATES."""
        text, kind = self.STATES[state]
        self.state = state
        self.setText(text)
        self.setToolTip("Connection to the server: %s" % text.rstrip(".").lower())
        theme.set_pill(self, kind)


# --- top bar ----------------------------------------------------------------------------

class TopBar(QToolBar):
    """Slim bar above the canvas: room pill + copy, server address, connection chip,
    Undo, Redo and the light/dark toggle. It replaces the text tool bar."""

    def __init__(self, app, window):
        """Build the bar for AppState app and the main window."""
        super().__init__("Session bar", window)
        self.app = app
        self.window = window
        self.setObjectName("toolbar_top")
        self.setMovable(False)
        self.setFloatable(False)
        self.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        self.toggleViewAction().setVisible(False)    # always shown: it holds the session info

        brand = QLabel("LAN Whiteboard")
        brand.setObjectName("brand")
        self.room = _pill(kind="primary", tip="Room code: others type it in Join")
        self.copy_btn = _icon_button("copy", "Copy room code", window.copy_room_code)
        self.address = QLabel()
        self.address.setProperty("muted", True)
        self.address.setToolTip("Server address (others type the IP in Join)")
        self.chip = ConnectionChip(app.net) if app.net is not None else _pill("No server")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.undo_btn = _icon_button("undo", "Undo (Ctrl+Z)", lambda: shortcuts.undo(app))
        self.redo_btn = _icon_button("redo", "Redo (Ctrl+Y)", lambda: shortcuts.redo(app))
        self.theme_btn = _icon_button("moon", "Toggle dark theme", self._toggle_theme)

        for widget in (brand, self._gap(12), self.room, self.copy_btn, self._gap(8), self.address,
                       self._gap(8), self.chip, spacer, self.undo_btn, self.redo_btn):
            self.addWidget(widget)
        self.addSeparator()
        self.addWidget(self.theme_btn)

        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self.refresh)
        if app.net is not None:
            app.net.connected.connect(self.refresh)
        theme.notifier.changed.connect(self._repaint_icons)
        self._repaint_icons()
        self.refresh()

    @staticmethod
    def _gap(width):
        """Return a fixed-width empty widget used as spacing."""
        gap = QWidget()
        gap.setFixedWidth(width)
        return gap

    def refresh(self):
        """Show the current room and server; viewers get Undo/Redo greyed out."""
        room = self.app.me.room
        self.room.setText("Room %s" % room if room else "No room")
        self.copy_btn.setEnabled(bool(room))
        address = server_address(self.app)
        self.address.setText(address or "no server")
        editable = self.app.me.role != "viewer"
        self.undo_btn.setEnabled(editable)
        self.redo_btn.setEnabled(editable)

    def _toggle_theme(self):
        """Switch light/dark (remembered in QSettings by theme.toggle_theme)."""
        theme.toggle_theme(QApplication.instance())

    def _repaint_icons(self, _mode=None):
        """Paint the icons in the colours of the current theme (moon in light, sun in dark)."""
        dark = theme.current_mode() == "dark"
        self.theme_btn.setProperty("iconName", "sun" if dark else "moon")
        self.theme_btn.setToolTip("Switch to the %s theme" % ("light" if dark else "dark"))
        for btn in (self.copy_btn, self.undo_btn, self.redo_btn, self.theme_btn):
            _paint_icon(btn)


# --- status bar -------------------------------------------------------------------------

class StatsChip(QToolButton):
    """Footer chip: my RTT (green / amber / red pill) and total KB/s; a click opens Latency/traffic.

    RTT comes from stats_panel.model (server STATS every 2 s), KB/s from the
    StatsPanel's own 1 s sampling (its `sampled` signal), both on the GUI thread.
    """

    def __init__(self, app, window):
        """Build the chip and follow the stats model and the stats panel."""
        super().__init__()
        self.app = app
        self.window = window
        self.setObjectName("statsChip")
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(14, 14))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("My round-trip time and traffic. Click to open Latency and traffic.")
        self.clicked.connect(self.open_stats)
        self.panel = window.findChild(stats_panel.StatsPanel)
        stats_panel.model.updated.connect(self.refresh)
        if self.panel is not None and hasattr(self.panel, "sampled"):
            self.panel.sampled.connect(self.refresh)
        theme.notifier.changed.connect(self.refresh)
        self.refresh()

    def refresh(self, *_args):
        """Show 'RTT x ms  y KB/s' and colour the pill by the RTT thresholds."""
        ms = stats_panel.model.rtt_of(self.app.me.username)
        rates = getattr(self.panel, "rates", None) or {}
        kbs = (rates.get("bytes_in", 0.0) + rates.get("bytes_out", 0.0)) / stats_panel.KB
        self.setText("RTT %s   %.1f KB/s" % (stats_panel.fmt_rtt(ms), kbs))
        if ms is None:
            kind = "neutral"
        elif ms < stats_panel.GREEN_MS:
            kind = "success"
        elif ms < stats_panel.AMBER_MS:
            kind = "warning"
        else:
            kind = "danger"
        if self.property("pill") != kind:
            self.setProperty("pill", kind)
            theme.repolish(self)
        self.setIcon(icons.get_icon("activity", 14, theme.tokens()[
            "%s_text" % kind if kind != "neutral" else "text_soft"]))

    def open_stats(self):
        """Show the Latency/traffic tab (or hide it when it is already in front)."""
        dock = _dock(self.window, "dock_stats")
        if dock is None:
            return
        if dock.isVisible() and not dock.visibleRegion().isEmpty():
            dock.hide()
            return
        dock.show()
        dock.raise_()


class StatusPills(QWidget):
    """Status-bar group: name + role pill, room pill and address pill (GUI thread)."""

    def __init__(self, app):
        """Build the pills and follow user, role and connection changes."""
        super().__init__()
        self.app = app
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.name = _pill(tip="My name")
        self.role = _pill(tip="My role (the host can change it)")
        self.room = _pill(tip="Room code")
        self.address = _pill(tip="Server address")
        for pill in (self.name, self.role, self.room, self.address):
            layout.addWidget(pill)
        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self.refresh)
        if app.net is not None:
            app.net.connected.connect(self.refresh)
        self.refresh()

    def refresh(self):
        """Show my name, role, room and server address (hidden while unknown)."""
        me = self.app.me
        self.name.setText(me.username or "not joined")
        self.role.setText(me.role or "")
        self.role.setVisible(bool(me.role))
        theme.set_pill(self.role, ROLE_PILLS.get(me.role, "neutral"))
        self.room.setText("Room %s" % me.room if me.room else "no room")
        address = server_address(self.app)
        self.address.setText(address or "no server")


def _build_status_bar(app, window):
    """Replace the text status label with pills and add the stats chip in front.

    main_window's own label ("name (role) | Room X | host:port") is found by
    its separator and hidden; it keeps updating itself, it is just not shown.
    """
    bar = window.statusBar()
    bar.setSizeGripEnabled(False)
    for label in bar.findChildren(QLabel):
        if "  |  " in label.text():
            label.hide()
    bar.insertPermanentWidget(0, StatsChip(app, window))
    bar.insertPermanentWidget(1, StatusPills(app))


# --- tool dock ----------------------------------------------------------------------------

def _find_grid(widget, child):
    """Return the QGridLayout under widget's layout that holds child, or None."""
    stack = [widget.layout()]
    while stack:
        layout = stack.pop()
        if layout is None:
            continue
        if isinstance(layout, QGridLayout) and layout.indexOf(child) >= 0:
            return layout
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.layout() is not None:
                stack.append(item.layout())
    return None


def _regrid(grid, widgets, columns):
    """Put widgets into grid again, `columns` per row, packed to the left."""
    for w in widgets:
        grid.removeWidget(w)
    for i, w in enumerate(widgets):
        grid.addWidget(w, i // columns, i % columns)
    for c in range(max(columns, grid.columnCount()) + 1):
        grid.setColumnStretch(c, 1 if c == columns else 0)


def _compact_tool_dock(window):
    """Icon-only tool buttons in a 4-column grid; groups of one or two tools side by side.

    The buttons were tagged with their tool id by theme.decorate_widgets.
    Only their size, style and grid position change; P2's signals stay.
    """
    dock = _dock(window, "dock_tools")
    panel = dock.widget() if dock is not None else None
    if panel is None:
        return
    buttons = [b for b in panel.findChildren(QToolButton) if b.property("toolId")]
    order = list(registry.TOOLS)
    buttons.sort(key=lambda b: order.index(b.property("toolId")) if b.property("toolId") in order else 99)
    groups = []
    for btn in buttons:
        btn.setProperty("railCompact", True)          # decorate keeps them icon-only from now on
        btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        btn.setFixedSize(TOOL_BUTTON, TOOL_BUTTON)
        btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        box = btn.parentWidget()
        if isinstance(box, QGroupBox) and box not in groups:
            groups.append(box)
    for box in groups:
        grid = box.layout()
        if isinstance(grid, QGridLayout):
            mine = [b for b in buttons if b.parentWidget() is box]
            _regrid(grid, mine, TOOL_COLUMNS)
            grid.setSpacing(4)
            grid.setContentsMargins(0, 2, 0, 0)

    # Groups with one or two tools share a row, up to TOOL_COLUMNS buttons per row.
    container = groups[0].parentWidget() if groups else None
    outer = container.layout() if container is not None else None
    if outer is None:
        return
    for box in groups:
        outer.removeWidget(box)
    row, used = None, 0
    for box in groups:
        count = sum(1 for b in buttons if b.parentWidget() is box)
        if count > 2:
            outer.addWidget(box)
            row = None
            continue
        if row is None or used + count > TOOL_COLUMNS:
            row = QHBoxLayout()
            row.setSpacing(10)
            outer.addLayout(row)
            used = 0
        row.addWidget(box)
        used += count
    if row is not None:
        row.addStretch(1)

    # Colour palettes: SWATCH_COLUMNS per row instead of four (one row less).
    swatches = [b for b in panel.findChildren(QToolButton) if b.objectName() == "swatch"]
    done = set()
    for sw in swatches:
        grid = _find_grid(sw.parentWidget(), sw)
        if grid is None or id(grid) in done:
            continue
        done.add(id(grid))
        mine = [s for s in swatches if grid.indexOf(s) >= 0]
        mine.sort(key=lambda s: grid.indexOf(s))
        _regrid(grid, mine, SWATCH_COLUMNS)
    panel.setProperty("compactRail", True)


def _fit_tool_dock_width(window):
    """Make the tool dock at least as wide as its widest section, the text format block included.

    The format block is hidden until a text tool is chosen, so the dock's own
    size hint does not count it; without this it would open cut off.
    """
    dock = _dock(window, "dock_tools")
    scroll = dock.widget() if dock is not None else None
    if not isinstance(scroll, QScrollArea) or scroll.widget() is None:
        return
    panel = scroll.widget()
    margins = panel.layout().contentsMargins() if panel.layout() is not None else None
    pad = (margins.left() + margins.right()) if margins is not None else 12
    need = panel.minimumSizeHint().width()
    for box in panel.findChildren(QGroupBox):
        need = max(need, box.minimumSizeHint().width() + pad)
    scroll.setMinimumWidth(need + scroll.verticalScrollBar().sizeHint().width())


def _drop_title_bar(dock):
    """Hide a dock's title bar (an empty widget replaces it); View menu still shows/hides it."""
    dock.setTitleBarWidget(QWidget())


def _wrap_in_scroll(dock):
    """Put the dock's panel inside a frameless scroll area.

    A dock's minimum height is its panel's minimum height; inside a scroll
    area the panel can be taller than the screen without forcing the window
    to grow. Nothing scrolls while there is room.
    """
    panel = dock.widget()
    if panel is None or isinstance(panel, QScrollArea):
        return
    scroll = QScrollArea()
    scroll.setObjectName("dockScroll")
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(panel)
    dock.setWidget(scroll)


# --- dock arrangement --------------------------------------------------------------------

def _hide_text_toolbar(window):
    """Hide the Pen ... Image text tool bar but keep its shortcuts working.

    QAction shortcuts only fire while one of the widgets the action is added
    to is visible. Adding the actions to the window as well keeps P, M, H, ...
    bound (the window is always visible) without binding any key twice.
    """
    bar = window.findChild(QToolBar, "toolbar_tools")
    if bar is None:
        return
    for action in bar.actions():
        if action not in window.actions():
            window.addAction(action)
    bar.hide()
    bar.toggleViewAction().setVisible(False)


def _stop_right_dock_juggling(window):
    """Keep Users above Chat at every window width.

    main_window._update_right_docks tabs or splits the right-hand docks again
    whenever the window crosses NARROW_WIDTH, which would undo the arrangement
    below. That method returns at once when it knows fewer than two right
    docks, so its private list is emptied ONCE here (approved by P1 for the
    UI polish; the clean fix is in docs/ui_handoff_for_akshay.md for P4).
    """
    try:
        window._right_docks = []
    except Exception as exc:  # noqa: BLE001
        print("[shell] could not stop the right-dock rearranging:", exc)


def apply_default_layout(window):
    """Arrange the docks the default way: tools left, Users over Chat right, bottom tabbed."""
    window.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
    window.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
    docks = {name: _dock(window, name) for name in DOCK_NAMES}
    tools, users, chat = docks["dock_tools"], docks["dock_users"], docks["dock_chat"]
    history, stats = docks["dock_history"], docks["dock_stats"]

    if tools is not None:
        tools.setFloating(False)
        window.addDockWidget(Qt.LeftDockWidgetArea, tools)
        tools.show()
    # PyQt detail: removeDockWidget hands the dock's ownership to Python, and only
    # addDockWidget hands it back to Qt. A dock that is removed and then only
    # tabified or split would be deleted with its last Python reference, so every
    # removed dock is added again first.
    if users is not None and chat is not None:
        for dock in (users, chat):
            dock.setFloating(False)
            window.removeDockWidget(dock)
            window.addDockWidget(Qt.RightDockWidgetArea, dock)
        window.splitDockWidget(users, chat, Qt.Vertical)
        users.show()
        chat.show()
    if history is not None and stats is not None:
        for dock in (history, stats):
            dock.setFloating(False)
            window.removeDockWidget(dock)
            window.addDockWidget(Qt.BottomDockWidgetArea, dock)
        window.tabifyDockWidget(history, stats)
        history.show()
        stats.show()
        history.raise_()
    _fix_toolbars(window)
    _size_docks(window)


def _size_docks(window):
    """Give the docks their starting sizes (the user can drag them afterwards)."""
    found = {name: _dock(window, name) for name in DOCK_NAMES}
    left = [d for d in (found["dock_tools"],) if d is not None]
    right = [d for d in (found["dock_users"],) if d is not None]
    if left:
        window.resizeDocks(left, [LEFT_WIDTH], Qt.Horizontal)
    if right:
        window.resizeDocks(right, [RIGHT_WIDTH], Qt.Horizontal)
    if found["dock_users"] is not None:
        # Only Users is sized: Chat, below it, takes whatever height is left.
        window.resizeDocks([found["dock_users"]], [USERS_HEIGHT], Qt.Vertical)
    if found["dock_history"] is not None:
        window.resizeDocks([found["dock_history"]], [BOTTOM_HEIGHT], Qt.Vertical)


def _fix_toolbars(window):
    """Whatever a saved state says: the text tool bar stays hidden, the top bar shown."""
    old = window.findChild(QToolBar, "toolbar_tools")
    if old is not None:
        old.hide()
    top = window.findChild(QToolBar, "toolbar_top")
    if top is not None:
        top.show()


def save_layout(window):
    """Store the dock and tool-bar arrangement (QMainWindow.saveState) in QSettings."""
    try:
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(STATE_KEY, window.saveState(STATE_VERSION))
    except RuntimeError:
        pass  # the window is already gone


def restore_layout(window):
    """Restore the saved arrangement; on any problem use the default one. Returns True if restored.

    restoreState returns False for data that is corrupt or was saved with
    another STATE_VERSION; such a state is deleted so it is not tried again.
    """
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    data = settings.value(STATE_KEY)
    ok = False
    if isinstance(data, QByteArray) and not data.isEmpty():
        try:
            ok = bool(window.restoreState(data, STATE_VERSION))
        except Exception:  # noqa: BLE001
            ok = False
    if ok:
        # A state where every important dock is closed is not useful either.
        names = ("dock_tools", "dock_users", "dock_chat")
        ok = any(_dock(window, n) is not None and not _dock(window, n).isHidden() for n in names)
    if not ok:
        if data is not None:
            settings.remove(STATE_KEY)
        apply_default_layout(window)
    _fix_toolbars(window)
    return ok


def reset_layout(app):
    """View > Reset layout: forget the saved arrangement and use the default."""
    window = getattr(app, "window", None)
    if window is None:
        return
    QSettings(SETTINGS_ORG, SETTINGS_APP).remove(STATE_KEY)
    apply_default_layout(window)
    app.toast("Layout reset.")


registry.register_menu_action("View", "Reset layout", reset_layout)


# --- banner -----------------------------------------------------------------------------

def _restyle_banner(window):
    """Give main_window's View-only banner the theme's warning colours (objectName for QSS)."""
    central = window.centralWidget()
    banner = central.findChild(ViewOnlyBanner) if central is not None else None
    if banner is not None:
        banner.setStyleSheet("")            # drop the fixed yellow; the theme styles it
        banner.setObjectName("viewOnlyBanner")
        theme.repolish(banner)


# --- entry point ------------------------------------------------------------------------

def install(app, window):
    """Arrange and restyle the freshly built main window (call once, before show())."""
    theme.decorate_widgets(window)            # tags tool buttons, names swatches, icons
    _safely(_stop_right_dock_juggling, window)
    _safely(_hide_text_toolbar, window)
    bar = _safely(TopBar, app, window)
    if bar is not None:
        window.addToolBar(Qt.TopToolBarArea, bar)
    _safely(_compact_tool_dock, window)
    for name in SCROLLED_DOCKS:
        dock = _dock(window, name)
        if dock is not None:
            _safely(_wrap_in_scroll, dock)
    _safely(_fit_tool_dock_width, window)
    for name in UNTITLED_DOCKS:
        dock = _dock(window, name)
        if dock is not None:
            _safely(_drop_title_bar, dock)
    _safely(_build_status_bar, app, window)
    _safely(_restyle_banner, window)
    _safely(apply_default_layout, window)
    _safely(restore_layout, window)
    theme.decorate_widgets(window)            # icons for the widgets added above
    qapp = QApplication.instance()
    if qapp is not None:
        qapp.aboutToQuit.connect(lambda: save_layout(window))
    window.setMinimumSize(QSize(960, 600))
    return window

