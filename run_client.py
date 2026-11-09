# OWNER: P1
"""run_client.py: Entry point: starts the whiteboard client.

Imports client.bootstrap (every module registers itself), creates the
shared AppState, the NetClient and the BoardModel, shows the connect dialog
(host or join a room) and then the main window.
"""

import sys

from PyQt5.QtWidgets import QApplication

import client.bootstrap  # noqa: F401  (runs every registration)
from client.app_state import AppState
from client.board_model import BoardModel
from client.connect_dialog import ConnectDialog
from client.main_window import MainWindow
from client.net_client import NetClient


def wire_status(app, window):
    """Show connection events as toasts and the room code in the title bar."""
    app.net.reconnecting.connect(lambda: app.toast("Connection lost, reconnecting..."))
    app.net.reconnected.connect(lambda: app.toast("Reconnected, catching up..."))
    app.net.disconnected.connect(lambda reason: app.toast("Disconnected: " + reason))
    window.setWindowTitle("LAN Whiteboard - room %s - %s" % (app.me.room, app.me.username))


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
    window.show()
    code = qapp.exec_()
    app.net.disconnect()
    return code


if __name__ == "__main__":
    sys.exit(main())
