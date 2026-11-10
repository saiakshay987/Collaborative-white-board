# OWNER: P1
"""run_client.py: Entry point: starts the whiteboard client.

Imports client.bootstrap (every module registers itself), creates the
shared AppState, the NetClient and the BoardModel, shows the connect dialog
(host or join a room) and then the main window.

When the host kicks us, NetClient does not reconnect (net.kicked); the
connect dialog opens again over the window with "You were removed from the
room". Joining from it continues in the same window (JOIN_OK, SNAPSHOT and
CHAT_HISTORY refresh everything, as after a reconnect); Cancel quits.
"""

import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QLabel

import client.bootstrap  # noqa: F401  (runs every registration)
from client.app_state import AppState
from client.board_model import BoardModel
from client import cursor_overlay, toasts
from client.connect_dialog import ConnectDialog
from client.main_window import MainWindow
from client.net_client import NetClient


class ConnectionStatus(QLabel):
    """Permanent status-bar label: Connected, Reconnecting... or Disconnected.

    Driven only by NetClient's Qt signals (contract F), which are emitted on
    the GUI thread, so updating the label here is thread-safe. It is a
    permanent widget, so a status-bar message never hides it.
    """

    # state -> (text, colour)
    STATES = {
        "connected": ("Connected", "#2e7d32"),
        "reconnecting": ("Reconnecting...", "#e67e22"),
        "disconnected": ("Disconnected", "#c0392b"),
    }

    def __init__(self, net, parent=None):
        """Start as Connected (the window opens only after JOIN_OK) and follow net."""
        super().__init__(parent)
        self.set_state("connected")
        net.connected.connect(lambda: self.set_state("connected"))
        net.reconnected.connect(lambda: self.set_state("connected"))
        net.reconnecting.connect(lambda: self.set_state("reconnecting"))
        net.disconnected.connect(lambda _reason: self.set_state("disconnected"))

    def set_state(self, state):
        """Show one of STATES."""
        text, colour = self.STATES[state]
        self.setText(text)
        self.setStyleSheet("color: %s; font-weight: bold; padding: 0 6px;" % colour)


def wire_status(app, window):
    """Show the connection state in the status bar, events as toasts, room in the title."""
    window.statusBar().addPermanentWidget(ConnectionStatus(app.net, window))
    app.net.reconnecting.connect(lambda: app.toast("Connection lost, reconnecting..."))
    app.net.reconnected.connect(lambda: app.toast("Reconnected, catching up..."))
    app.net.disconnected.connect(lambda reason: on_disconnected(app, window, reason))
    window.setWindowTitle("LAN Whiteboard - room %s - %s" % (app.me.room, app.me.username))


def on_disconnected(app, window, reason):
    """NetClient gave up or we were kicked: toast it; after a kick, back to the dialog.

    The dialog is opened with QTimer.singleShot(0): this slot runs inside
    NetClient's signal emission (inside a USER_LEFT handler for a kick),
    and a modal dialog started there would hold up the other handlers of
    that message until it closes.
    """
    if not app.net.kicked:
        app.toast("Disconnected: " + reason)
        return
    app.toast(reason)
    QTimer.singleShot(0, lambda: back_to_connect_dialog(app, window, reason))


def back_to_connect_dialog(app, window, reason):
    """After a kick: show the connect dialog over the window; Cancel closes the app."""
    dialog = ConnectDialog(app, window, notice=reason)
    if dialog.exec_() != ConnectDialog.Accepted:
        window.close()


def main():
    """Create state, run the connect dialog, then the main window and event loop."""
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("LAN Whiteboard")

    app = AppState()
    app.net = NetClient(app)
    app.board = BoardModel()

    # Board messages (SNAPSHOT, CHAT_HISTORY, ...) may arrive while the
    # dialog is still open; the handlers store them in app.board etc., so
    # the window built afterwards shows them.
    if ConnectDialog(app).exec_() != ConnectDialog.Accepted:
        app.net.disconnect()
        return 0

    window = MainWindow(app)
    app.window = window
    wire_status(app, window)
    cursor_overlay.install(app)  # live cursors need app.canvas, built by MainWindow
    toasts.preload_sounds()      # generate the join/leave sounds before the first join
    window.show()
    code = qapp.exec_()
    app.net.disconnect()
    return code


if __name__ == "__main__":
    sys.exit(main())
