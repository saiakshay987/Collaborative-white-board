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

The Session menu (Copy room code, Disconnect) only uses NetClient's public
API: disconnect() and its four signals. Disconnect, a kick and a lost
connection all end the same way: run_client.py resets the client and shows
the join screen again. The window's close button asks first. All
network results arrive as Qt signals on the GUI thread, so nothing here can
block the window.

Roles (contract C4): Session > Manage roles opens ManageRolesDialog for the
host. The dialog only SENDS SET_ROLE {user, role}; the server decides (host
only, valid role, target in the room, never demote the last host) and answers
with a USER_LIST that updates every client. Hiding the menu entry or greying a
combo box is a convenience: the server enforces the rules. A user whose role
changes gets a toast.
"""

from PyQt5.QtCore import QByteArray, QEvent, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAction, QActionGroup, QApplication, QComboBox, QDialog,
                             QDockWidget, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QShortcut, QVBoxLayout)

from common import protocol
from client import canvas as canvas_mod
from client import registry
from client.registry import on_message

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

# Roles the host can give (the three roles of contract C4, which the server accepts).
ASSIGNABLE_ROLES = ("editor", "viewer", "host")

# What a role means, shown in the toast a user gets when their role changes.
ROLE_HINTS = {
    "host": "You can draw, manage roles, clear the board and remove users.",
    "editor": "You can draw and chat.",
    "viewer": "You can watch and chat, but not draw.",
}


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


class ManageRolesDialog(QDialog):
    """Host-only dialog: one row per user with a combo box to change that user's role.

    Choosing a role sends SET_ROLE {user, role} through app.net.send and
    nothing else: the dialog never changes a role itself. The server checks
    the request and, if it accepts it, sends USER_LIST to everybody, which
    reaches app.users_changed and rebuilds the rows. If the server refuses
    (ERROR with ref_type SET_ROLE) the rows are rebuilt too, so a combo box
    never shows a role the server did not give.
    """

    def __init__(self, app, parent=None):
        """Build the dialog for AppState app and fill it from app.users."""
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Manage roles")
        self.setMinimumWidth(380)
        self._grid = QGridLayout()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a role for a user. The server applies it and tells everybody."))
        layout.addLayout(self._grid)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self._on_my_role_changed)
        self.refresh()

    def refresh(self):
        """Rebuild one row per user in app.users: name, online state, role combo box."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)   # gone at once, so old and new rows never overlap
                widget.deleteLater()
        users = self.app.users
        hosts = [u for u in users if u.get("role") == "host"]
        for row, user in enumerate(users):
            name, role = user.get("name", "?"), user.get("role")
            label = name + (" (you)" if name == self.app.me.username else "")
            if not user.get("online", True):
                label += " (offline)"
            name_label = QLabel(label)
            name_label.setStyleSheet("font-weight: bold; color: %s;" % (user.get("color") or "#555555"))
            combo = QComboBox()
            combo.addItems(ASSIGNABLE_ROLES)
            if role not in ASSIGNABLE_ROLES:
                combo.addItem(str(role))
            combo.setCurrentText(str(role))
            if role == "host" and len(hosts) == 1:
                # The server refuses to demote the last host; do not offer it.
                combo.setEnabled(False)
                combo.setToolTip("The last host cannot be demoted. Make someone else host first.")
            # `activated` fires only when the USER picks an entry, not when the
            # code sets the current text above, so refreshing never sends anything.
            combo.activated.connect(lambda _i, n=name, c=combo: self._on_choice(n, c))
            self._grid.addWidget(name_label, row, 0)
            self._grid.addWidget(combo, row, 1)

    def _role_of(self, name):
        """Return the role app.users lists for `name`, or None."""
        for user in self.app.users:
            if user.get("name") == name:
                return user.get("role")
        return None

    def _on_choice(self, name, combo):
        """The host picked a role in a combo box: send SET_ROLE {user, role}.

        The combo box keeps showing the pick until USER_LIST (accepted) or an
        ERROR (refused) makes refresh() show what the server really decided.
        """
        role = combo.currentText()
        if role == self._role_of(name):
            return
        if not self.app.net.send(protocol.SET_ROLE, {"user": name, "role": role}):
            self.app.toast("Not connected: the role was not changed.")
            self.refresh()

    def _on_my_role_changed(self):
        """My own role changed: a user who is no longer host may not manage roles."""
        if self.app.me.role != "host":
            self.close()


# The one open dialog (None when closed), so the ERROR handler can reach it.
_dialog = None


def _forget_dialog():
    """Clear the reference to the dialog once it is closed."""
    global _dialog
    _dialog = None


def open_manage_roles(app):
    """Session > Manage roles: open the dialog if I am the host, else show a toast.

    The role check here only saves a pointless dialog. The server checks
    permissions.can(role, 'set_role') on every SET_ROLE itself.
    """
    global _dialog
    if app.me.role != "host":
        app.toast("Only the host can manage roles.")
        return
    if _dialog is not None:      # already open: bring it to the front
        _dialog.raise_()
        _dialog.activateWindow()
        return
    _dialog = ManageRolesDialog(app, app.window)
    _dialog.setAttribute(Qt.WA_DeleteOnClose)
    _dialog.finished.connect(_forget_dialog)
    _dialog.show()


@on_message(protocol.ERROR)
def _on_role_error(app, msg):
    """ERROR for a SET_ROLE: rebuild the open dialog so it shows the real roles.

    The error text itself is shown as a toast by the generic ERROR handler.
    """
    if _dialog is not None and (msg.get("payload") or {}).get("ref_type") == protocol.SET_ROLE:
        _dialog.refresh()


registry.register_menu_action("Session", "Manage roles", open_manage_roles)


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
        self._last_role = app.me.role  # to tell a real role change from the first JOIN_OK
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
        """Add Copy room code and Disconnect to the Session menu."""
        self._act_copy = menu.addAction("Copy room code")
        self._act_copy.triggered.connect(self.copy_room_code)
        self._act_leave = menu.addAction("Disconnect")
        self._act_leave.triggered.connect(self.leave_room)
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
        """Session > Disconnect: ask for confirmation, then leave the room.

        NetClient.disconnect() sends LEAVE (the server removes my seat but
        keeps the room), stops any reconnect attempt, closes the connection
        and emits `disconnected`. run_client.py reacts to that signal: it
        resets the whole client and shows the join screen again, pre-filled
        with this server, room code and username. The app does not quit.
        """
        if self.app.net is None or self._net_state == "disconnected":
            return
        answer = QMessageBox.question(
            self, "Disconnect", "Disconnect from room %s?\nThe room stays on the server; "
            "you can join it again with the same room code." % (self.app.me.room or ""))
        if answer == QMessageBox.Yes:
            self.app.net.disconnect()

    def _set_net_state(self, state):
        """Remember the connection state and refresh what depends on it."""
        self._net_state = state
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
        self.app.role_changed.connect(self._toast_role_change)
        self.app.users_changed.connect(self._refresh_all)   # JOIN_OK always emits it
        net = self.app.net
        if net is not None:
            net.connected.connect(lambda: self._set_net_state("connected"))
            net.reconnected.connect(lambda: self._set_net_state("connected"))
            net.reconnecting.connect(lambda: self._set_net_state("reconnecting"))
            net.disconnected.connect(lambda _reason: self._set_net_state("disconnected"))

    def _toast_role_change(self):
        """Tell me when the host changed my role (not for the role I get at join)."""
        old, new = self._last_role, self.app.me.role
        self._last_role = new
        if old is not None and new is not None and new != old:
            self.app.toast("Your role is now %s. %s" % (new, ROLE_HINTS.get(new, "")))

    def _refresh_all(self):
        """Update status bar, title, banner and Session menu from the current state."""
        self._refresh_status()
        self._refresh_title()
        self._banner.setVisible(self.app.me.role == "viewer")
        self._act_leave.setEnabled(self._net_state != "disconnected")

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
        """Ask before quitting while in a room; remember the window geometry, then close.

        The close button leaves the room (the app's exit path sends LEAVE),
        so while connected the user must confirm. Without a connection
        (the join screen is showing) there is nothing to lose and it closes
        at once. Answering No ignores the event: the window stays open.
        """
        if self._net_state != "disconnected":
            answer = QMessageBox.question(
                self, "Quit", "Quit the whiteboard?\nYou will be disconnected from room %s."
                % (self.app.me.room or ""))
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
