# OWNER: P1
"""app_state.py: AppState, the one shared object every client module receives as `app`.

Contract section F (client). It holds references to the main parts of the
client so modules never import each other just to find them:

    app.net          NetClient
    app.me           Me: username, role, color, room, token
    app.users        list of {name, color, role, online} from USER_LIST
    app.board        BoardModel (P3)
    app.canvas       CanvasWidget (P2)
    app.window       MainWindow (P4)
    app.pen          dict: color, width, tool, alpha
    app.active_tool  current tool instance
    app.toast(text)  short notification for the user

Signals (emitted on the GUI thread):
    users_changed()      app.users was replaced
    dm_requested(str)    open a direct-message conversation with that user
    role_changed()       app.me.role changed
"""

from PyQt5.QtCore import QObject, pyqtSignal


class Me:
    """Who this client is in the room; filled in from JOIN_OK."""

    def __init__(self):
        """Start as an anonymous, not-yet-joined user."""
        self.username = None
        self.role = None
        self.color = None
        self.room = None
        self.token = None

    def __repr__(self):
        """Readable form for debugging."""
        return "Me(%r, role=%r, room=%r)" % (self.username, self.role, self.room)


class AppState(QObject):
    """Shared client state and the app-wide Qt signals."""

    users_changed = pyqtSignal()
    dm_requested = pyqtSignal(str)
    role_changed = pyqtSignal()

    def __init__(self, parent=None):
        """Create empty state; run_client.py wires in net, board, window."""
        super().__init__(parent)
        self.net = None
        self.me = Me()
        self.users = []
        self.board = None
        self.canvas = None
        self.window = None
        self.pen = {"color": "#000000", "width": 3, "tool": "pen", "alpha": 1.0}
        self.active_tool = None

    def set_users(self, users):
        """Replace the user list and emit users_changed."""
        self.users = list(users)
        self.users_changed.emit()

    def set_role(self, role):
        """Change my role and emit role_changed (only when it really changes)."""
        if role != self.me.role:
            self.me.role = role
            self.role_changed.emit()

    def toast(self, text):
        """Show a short notification: the status bar for now, stdout without a window."""
        if self.window is not None and hasattr(self.window, "statusBar"):
            self.window.statusBar().showMessage(text, 4000)
        else:
            print("[toast]", text)
