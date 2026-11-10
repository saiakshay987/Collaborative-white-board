# OWNER: P4
"""main_window.py: Main window: assembles the canvas, toolbar, menus and registered panels.

The final shell (P4). Everything is built from client/registry.py, so
features appear without editing this file:
    registry.TOOLS         -> "Tools" toolbar (one checkable action per tool)
    registry.MENU_ACTIONS  -> menu bar, in the fixed order File, Edit, View,
                              Insert, Session, Help
    registry.SHORTCUTS     -> window-wide shortcuts
    registry.PANELS        -> dock widgets, each in its registered area

Layout: app.canvas is the central widget. The docks of the "right" area
(users, chat) are stacked while the window is wide and tabbed together when
it is narrower than NARROW_WIDTH. The status bar shows my name and role, the
room code and the server address (the connection state itself is the label
run_client.py adds). A yellow banner over the canvas says "View only" while
my role is viewer. The window geometry is remembered with QSettings.

The Session menu (Copy room code, Leave, Reconnect) only uses NetClient's
public API: disconnect(), connect_to(), send() and its four signals. All
network results arrive as Qt signals on the GUI thread, so nothing here can
block the window.
"""

from PyQt5.QtCore import QByteArray, QEvent, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAction, QActionGroup, QApplication, QDockWidget, QLabel,
                             QMainWindow, QMessageBox, QShortcut)

from common import protocol
from client import canvas as canvas_mod
from client import registry

# registry area names -> Qt dock areas.
_AREAS = {
    "left": Qt.LeftDockWidgetArea,
    "right": Qt.RightDockWidgetArea,
    "bottom": Qt.BottomDockWidgetArea,
    "top": Qt.TopDockWidgetArea,
}


# Menus in the order they always appear, whatever the registration order.
MENU_ORDER = ("File", "Edit", "View", "Insert", "Session", "Help")

# Below this window width the right-hand docks are tabbed instead of stacked.
NARROW_WIDTH = 1000

# QSettings keys (stored per user by Qt, e.g. ~/.config/CN Lab/LAN Whiteboard.conf).
SETTINGS_ORG = "CN Lab"
SETTINGS_APP = "LAN Whiteboard"
GEOMETRY_KEY = "window/geometry"

BANNER_HEIGHT = 28


class ViewOnlyBanner(QLabel):
    """Yellow strip across the top of a widget, shown while I am a viewer.

    It is a child of the canvas (so it floats over it) and ignores the mouse,
    so panning and zooming the canvas still work underneath. An event filter
    on the parent keeps it as wide as the canvas.
    """

    def __init__(self, parent):
        """Create the hidden banner as a child of `parent` and follow its size."""
        super().__init__("View only: you can watch and chat, but not draw", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #ffeb3b; color: #333333; font-weight: bold;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        parent.installEventFilter(self)
        self.reposition()
        self.hide()

    def reposition(self):
        """Stretch the banner over the full width of the parent, at its top edge."""
        self.setGeometry(0, 0, self.parent().width(), BANNER_HEIGHT)
        self.raise_()

    def eventFilter(self, obj, event):
        """Resize the banner whenever the canvas is resized; never swallow the event."""
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.reposition()
        return False


class MainWindow(QMainWindow):
    """The application window, built from the registries."""

    # Emitted (GUI thread) with the tool id whenever the active tool changes,
    # whoever changed it. Widgets show the current tool by connecting here.
    tool_selected = pyqtSignal(str)

    def __init__(self, app):
        """Build canvas, toolbar, menus, shortcuts and docks for AppState app."""
        super().__init__()
        self.app = app
        self._tool_actions = {}
        self._menus = {}              # menu title -> QMenu
        self._right_docks = []        # docks registered for the right area, in order
        self._right_tabbed = False    # True while those docks are tabbed together
        # Connection state seen through NetClient's signals: connected,
        # reconnecting or disconnected. The window opens after JOIN_OK.
        net = app.net
        self._net_state = "connected" if net is not None and net.is_connected else "disconnected"
        self._rejoin_pending = False  # a manual Reconnect waits for the TCP connection
        self.setWindowTitle("LAN Whiteboard")
        self.resize(1200, 800)
        self._build_canvas()
        self._build_toolbar()
        self._build_menus()
        self._build_shortcuts()
        self._build_docks()
        self._build_status_bar()
        self._banner = ViewOnlyBanner(self.centralWidget())
        self._connect_signals()
        self._restore_geometry()
        self._update_right_docks()
        self.statusBar().showMessage("Ready")
        self._refresh_all()
        # run_client.py sets its own title right after constructing the window;
        # once the event loop starts, this makes the title "LAN Whiteboard - <room>".
        QTimer.singleShot(0, self._refresh_title)

    def _build_canvas(self):
        """Central widget: P2's CanvasWidget, or a placeholder until it exists."""
        cls = getattr(canvas_mod, "CanvasWidget", None)
        if self.app.canvas is not None:
            widget = self.app.canvas   # already created by someone else: just place it
        elif cls is not None:
            widget = cls(self.app)
            self.app.canvas = widget
        else:
            widget = QLabel("Canvas not implemented yet (P2)")
            widget.setAlignment(Qt.AlignCenter)
        self.setCentralWidget(widget)

    def _build_toolbar(self):
        """One checkable action per registered tool; exactly one is active.

        This toolbar is the ONLY owner of the tool keyboard shortcuts. If a
        second widget (P2's tool dock) also bound the same keys, Qt would
        treat every key as ambiguous and neither binding would fire. Other
        widgets therefore never set shortcuts; they call select_tool() to
        change the tool and listen to tool_selected to follow changes.
        """
        bar = self.addToolBar("Tools")
        bar.setObjectName("toolbar_tools")
        group = QActionGroup(self)
        group.setExclusive(True)
        self._tool_actions = {}   # tool id -> its checkable QAction
        for tool_id, cls in registry.TOOLS.items():
            action = QAction(getattr(cls, "label", tool_id), self, checkable=True)
            if getattr(cls, "shortcut", None):
                action.setShortcut(QKeySequence(cls.shortcut))
            action.triggered.connect(lambda _checked, t=tool_id: self.select_tool(t))
            group.addAction(action)
            bar.addAction(action)
            self._tool_actions[tool_id] = action
        # Start with the first registered tool so a click on the canvas
        # never lands on "no tool".
        if self._tool_actions and self.app.active_tool is None:
            self.select_tool(next(iter(self._tool_actions)))

    def select_tool(self, tool):
        """Make a new instance of a tool the active tool.

        tool is a tool id ("pen") or a registered tool class. This is the
        single entry point for every way of changing the tool (toolbar click,
        shortcut, other panels): it creates the instance, checks the matching
        toolbar button without re-triggering it, and emits tool_selected so
        other widgets can follow without polling. Unknown tools are ignored.
        """
        tool_id = tool if isinstance(tool, str) else getattr(tool, "id", None)
        cls = registry.TOOLS.get(tool_id)
        if cls is None:
            return
        self.app.active_tool = cls()
        action = self._tool_actions.get(tool_id)
        if action is not None and not action.isChecked():
            action.setChecked(True)   # setChecked does not emit triggered
        self.tool_selected.emit(tool_id)

    def _build_menus(self):
        """Create the menu bar: the six fixed menus, then fill them from the registry.

        File, Edit, View, Insert, Session and Help always come in this order,
        whatever order the modules registered their actions in. A menu title
        that is not one of the six (a registration with a new name) is
        inserted before Help. Session starts with its built-in actions;
        registered Session actions follow them.
        """
        for title in MENU_ORDER:
            self._menus[title] = self.menuBar().addMenu(title)
        self._add_session_actions(self._menus["Session"])
        help_action = self._menus["Help"].menuAction()
        for entry in registry.MENU_ACTIONS:
            menu = self._menus.get(entry["menu"])
            if menu is None:
                menu = self._menus[entry["menu"]] = self.menuBar().addMenu(entry["menu"])
                self.menuBar().insertMenu(help_action, menu)
            action = menu.addAction(entry["label"])
            if entry["shortcut"]:
                action.setShortcut(QKeySequence(entry["shortcut"]))
            action.triggered.connect(lambda _checked, fn=entry["fn"]: fn(self.app))

    def _build_shortcuts(self):
        """Window-wide keyboard shortcuts."""
        for entry in registry.SHORTCUTS:
            sc = QShortcut(QKeySequence(entry["keyseq"]), self)
            sc.activated.connect(lambda fn=entry["fn"]: fn(self.app))

    def _build_docks(self):
        """One dock widget per registered panel, in its registered area.

        Panels come only from registry.PANELS; this window never creates or
        edits a panel, it only places it. Each dock gets a show/hide toggle in
        the View menu. Docks of the right area are remembered so they can be
        tabbed when the window is narrow.
        """
        # Let the tall tool dock on the left run the full height of the window,
        # with the bottom dock only under the canvas and the right docks. By
        # default the bottom dock spans the whole width, so the tool dock plus
        # the stats dock would force the window to be taller than a laptop screen.
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        view = self._menus["View"]
        for panel in registry.PANELS.values():
            dock = QDockWidget(panel["title"], self)
            dock.setObjectName("dock_" + panel["name"])
            dock.setWidget(panel["factory"](self.app))
            self.addDockWidget(_AREAS[panel["area"]], dock)
            if panel["area"] == "right":
                self._right_docks.append(dock)
            view.addAction(dock.toggleViewAction())

    def _update_right_docks(self):
        """Tab the right-hand docks when the window is narrow, stack them when wide.

        Only acts when the situation changes (narrow <-> wide), so resizing
        within one range never rearranges anything the user has dragged.
        """
        narrow = self.width() < NARROW_WIDTH
        if len(self._right_docks) < 2 or narrow == self._right_tabbed:
            return
        first = self._right_docks[0]
        for other in self._right_docks[1:]:
            hidden = other.isHidden()   # the user may have closed it from the View menu
            if narrow:
                self.tabifyDockWidget(first, other)
            else:
                # splitDockWidget alone leaves a tabbed dock in its tab group, so
                # take the dock out of the layout, put it back and then split.
                self.removeDockWidget(other)
                self.addDockWidget(Qt.RightDockWidgetArea, other)
                self.splitDockWidget(first, other, Qt.Vertical)
                other.show()            # removeDockWidget hid it
            if hidden:
                other.hide()
        self._right_tabbed = narrow
        if narrow:
            first.raise_()   # make the first tab the visible one

    def resizeEvent(self, event):
        """Re-check whether the right-hand docks should be tabbed."""
        super().resizeEvent(event)
        self._update_right_docks()

    # ------------------------------------------------------------ session menu

    def _add_session_actions(self, menu):
        """Add Copy room code, Leave and Reconnect to the Session menu."""
        self._act_copy = menu.addAction("Copy room code")
        self._act_copy.triggered.connect(self.copy_room_code)
        self._act_leave = menu.addAction("Leave")
        self._act_leave.triggered.connect(self.leave_room)
        self._act_reconnect = menu.addAction("Reconnect")
        self._act_reconnect.triggered.connect(self.reconnect)
        menu.addSeparator()

    def copy_room_code(self):
        """Put the room code on the clipboard so it can be pasted to others."""
        code = self.app.me.room
        if not code:
            self.app.toast("There is no room code yet.")
            return
        QApplication.clipboard().setText(code)
        self.app.toast("Room code %s copied." % code)

    def leave_room(self):
        """Ask for confirmation, then leave the room (NetClient.disconnect).

        disconnect() sends LEAVE (the server removes my seat), closes the
        connection and emits `disconnected`. The window stays open so
        Session > Reconnect can join again.
        """
        if self.app.net is None or self._net_state == "disconnected":
            return
        answer = QMessageBox.question(
            self, "Leave room", "Leave room %s?\nYou can join again with Session > Reconnect."
            % (self.app.me.room or ""))
        if answer == QMessageBox.Yes:
            self.app.net.disconnect()

    def reconnect(self):
        """Join the room again after being disconnected or after leaving.

        While connected, or while NetClient is already retrying by itself
        (it does that every 2 s for 60 s), nothing needs doing. Otherwise
        open a new TCP connection with connect_to(); once `connected` fires
        (see _on_net_connected) a JOIN_ROOM for the same room and username is
        sent. The old seat is gone by now (NetClient forgets the token when it
        gives up or leaves), so this is a fresh join.
        """
        net = self.app.net
        if net is None:
            return
        if self._net_state != "disconnected":
            self.app.toast("Already connected." if self._net_state == "connected"
                           else "Already trying to reconnect.")
            return
        if not net.host or not self.app.me.room:
            self.app.toast("No room to rejoin. Restart the client to connect again.")
            return
        self._rejoin_pending = True
        self.app.toast("Reconnecting to %s..." % net.host)
        net.connect_to(net.host, net.port)

    def _on_net_connected(self):
        """TCP is up again: if Reconnect asked for it, send the JOIN_ROOM now."""
        self._set_net_state("connected")
        if self._rejoin_pending:
            self._rejoin_pending = False
            me = self.app.me
            self.app.net.send(protocol.JOIN_ROOM, {"room": me.room, "username": me.username})

    def _set_net_state(self, state):
        """Remember the connection state and refresh what depends on it."""
        self._net_state = state
        if state == "disconnected":
            self._rejoin_pending = False   # a failed attempt must not rejoin later
        self._refresh_all()

    # ----------------------------------------------------- status, title, banner

    def _build_status_bar(self):
        """Permanent status-bar label: my name and role, room code, server address.

        The connection state is the ConnectionStatus label added by
        run_client.py, so it is not repeated here.
        """
        self._status_label = QLabel()
        self._status_label.setStyleSheet("padding: 0 6px;")
        self.statusBar().addPermanentWidget(self._status_label)

    def _connect_signals(self):
        """Follow role and user changes (AppState) and the connection (NetClient)."""
        self.app.role_changed.connect(self._refresh_all)
        self.app.users_changed.connect(self._refresh_all)   # JOIN_OK always emits it
        net = self.app.net
        if net is not None:
            net.connected.connect(self._on_net_connected)
            net.reconnected.connect(lambda: self._set_net_state("connected"))
            net.reconnecting.connect(lambda: self._set_net_state("reconnecting"))
            net.disconnected.connect(lambda _reason: self._set_net_state("disconnected"))

    def _refresh_all(self):
        """Update status bar, title, banner and Session menu from the current state."""
        self._refresh_status()
        self._refresh_title()
        self._banner.setVisible(self.app.me.role == "viewer")
        self._act_leave.setEnabled(self._net_state != "disconnected")
        self._act_reconnect.setEnabled(self._net_state == "disconnected")

    def _refresh_status(self):
        """Show "name (role) | Room CODE | host:port" in the status bar."""
        me, net = self.app.me, self.app.net
        who = "%s (%s)" % (me.username, me.role) if me.username else "not joined"
        room = "Room %s" % me.room if me.room else "no room"
        server = "%s:%s" % (net.host, net.port) if net is not None and net.host else "no server"
        self._status_label.setText("%s  |  %s  |  %s" % (who, room, server))

    def _refresh_title(self):
        """Set the window title to "LAN Whiteboard - <room code>"."""
        room = self.app.me.room
        self.setWindowTitle("LAN Whiteboard - %s" % room if room else "LAN Whiteboard")

    # -------------------------------------------------------------- geometry

    def _restore_geometry(self):
        """Restore the window size and position saved by the last session, if any."""
        geometry = QSettings(SETTINGS_ORG, SETTINGS_APP).value(GEOMETRY_KEY)
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)

    def closeEvent(self, event):
        """Remember the window geometry for the next start, then close."""
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
