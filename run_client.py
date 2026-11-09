# OWNER: P1
"""run_client.py: Entry point: starts the whiteboard client.

Imports client.bootstrap (every module registers itself), creates the
shared AppState, the BoardModel and the MainWindow, and shows the window.

Skeleton: the connect dialog is a placeholder; NetClient and the real
ConnectDialog arrive in the next P1 task.
"""

import sys

from PyQt5.QtWidgets import QApplication

import client.bootstrap  # noqa: F401  (runs every registration)
from client.app_state import AppState
from client.board_model import BoardModel
from client.main_window import MainWindow


def ask_connection(app):
    """Placeholder for the connect dialog (host, port, username, room, password).

    Returns None for now: the client starts offline.
    """
    app.toast("Offline: connect dialog not implemented yet")
    return None


def main():
    """Build the app state and main window, then run the Qt event loop."""
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("LAN Whiteboard")

    app = AppState()
    app.me.username = "me"
    app.board = BoardModel()
    window = MainWindow(app)
    app.window = window
    window.show()

    ask_connection(app)
    sys.exit(qapp.exec_())


if __name__ == "__main__":
    main()
