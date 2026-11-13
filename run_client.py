# OWNER: P1
"""run_client.py: Entry point: starts the whiteboard client.

Imports client.bootstrap (every module registers itself), creates the
shared AppState, the NetClient and the BoardModel, shows the connect dialog
(host or join a room) and then the main window.

When a session ends (Session > Disconnect, a kick, auto-reconnect giving up
after 60 s, a server shutdown) SessionFlow resets the client and opens the
connect dialog again over the window, pre-filled with the last server, room
code and username and showing the reason. Joining from it continues in the
same window: the server kept the room, and JOIN_OK, USER_LIST, SNAPSHOT,
CHAT_HISTORY and VERSIONS fill the emptied client. Cancel quits.

Look: high-DPI scaling is switched on before the QApplication exists (Qt
reads these attributes only then), and client/theme.py applies the saved
light or dark theme before the first widget is built. theme.decorate_widgets
then adds the painted icons to the window and the dialogs.
"""

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

import client.bootstrap  # noqa: F401  (runs every registration)
from client.app_state import AppState
from client.board_model import BoardModel
from client import cursor_overlay, shell, theme, toasts
from common import config
from client.connect_dialog import ConnectDialog
from client.main_window import MainWindow
from client.net_client import NetClient


class ConnectionStatus(shell.ConnectionChip):
    """Permanent status-bar pill: Connected (green), Reconnecting... (amber) or Disconnected (red).

    Driven only by NetClient's Qt signals (contract F), which are emitted on
    the GUI thread, so updating it is thread-safe. It is a permanent widget,
    so a status-bar message never hides it. The look and the states live in
    shell.ConnectionChip, which the top bar uses too.
    """


def wire_status(app, window):
    """Show the connection state in the status bar, events as toasts, room in the title."""
    window.statusBar().addPermanentWidget(ConnectionStatus(app.net, window))
    app.net.reconnecting.connect(lambda: app.toast("Connection lost, reconnecting..."))
    app.net.reconnected.connect(lambda: app.toast("Reconnected, catching up..."))
    window.setWindowTitle("LAN Whiteboard - room %s - %s" % (app.me.room, app.me.username))


def build_window(app):
    """Build the main window for app and make it ready to show.

    MainWindow (P4) builds everything from the registries; wire_status adds
    the connection pill and toasts; shell.install arranges the window (top
    bar, compact tool rail, docks, status pills, saved layout) and
    cursor_overlay.install puts the live cursors over the canvas (it needs
    app.canvas, which MainWindow creates). scripts/ui_smoke_test.py uses this
    same function, so the screenshots show exactly what users get.
    """
    window = MainWindow(app)
    app.window = window
    wire_status(app, window)
    shell.install(app, window)
    cursor_overlay.install(app)
    return window


class SessionFlow:
    """Takes the user back to the join screen whenever the session ends.

    NetClient emits `disconnected(reason)` exactly when the session is over
    for good, whatever the cause:
      * Session > Disconnect (the user chose to leave: LEAVE was sent),
      * the host kicked us,
      * the connection dropped and auto-reconnect gave up after 60 s (this
        is also what a server that was shut down looks like to a client),
      * the server could not be reached.
    In every case the TCP link and the reconnect loop are already stopped by
    NetClient when the signal arrives, so what is left is on the client side:
    reset every module (AppState.reset_session), then show the connect dialog
    again, pre-filled with the last server, room code and username. When the
    dialog joins again, JOIN_OK, USER_LIST, SNAPSHOT, CHAT_HISTORY and
    VERSIONS fill the emptied client from the server, which kept the room.
    """

    def __init__(self, app, window):
        """Remember app and window; nothing is shown until a session ends."""
        self.app = app
        self.window = window
        # True while we are resetting or the join screen is open. The dialog
        # itself reports failed connects through `disconnected`; those must
        # not start another round of this flow.
        self.busy = False
        # True once the application is closing (no join screen then).
        self.quitting = False
        # Last known join details, used to pre-fill the join screen.
        self.last = {"server": "", "room": "", "username": ""}

    def remember_session(self):
        """Store server, room code and username of the session that is ending.

        Fields that are unknown (nothing joined yet) keep their old value.
        """
        net, me = self.app.net, self.app.me
        if net.host:
            self.last["server"] = net.host if net.port == config.PORT else "%s:%d" % (net.host, net.port)
        if me.room:
            self.last["room"] = me.room
        if me.username:
            self.last["username"] = me.username

    def on_disconnected(self, reason):
        """NetClient slot: the session ended; schedule the join screen.

        The dialog is opened with QTimer.singleShot(0): this slot runs inside
        NetClient's signal emission (inside a USER_LEFT handler for a kick),
        and a modal dialog started there would hold up the other handlers of
        that message until it closes.
        """
        if self.busy or self.quitting:
            return
        self.busy = True
        self.remember_session()
        QTimer.singleShot(0, lambda: self.show_join_screen(reason))

    def show_join_screen(self, reason):
        """Reset the client, then run the connect dialog (Cancel quits the app)."""
        # Reset first: while the dialog is open the board, chat, users and
        # history must already be empty, and the next join must not see
        # anything of the old session.
        self.app.reset_session()
        self.window.statusBar().clearMessage()
        dialog = ConnectDialog(self.app, self.window, notice=reason, prefill=self.last)
        theme.decorate_widgets(dialog)
        accepted = dialog.exec_() == ConnectDialog.Accepted
        if accepted:
            self.busy = False        # joined again: the next end of session starts this flow anew
            return
        # Cancel: nothing left to show, so quit. Closing the TCP link first
        # (if the dialog opened one) lets the window close without asking.
        self.quitting = True
        self.app.net.disconnect()
        self.window.close()


def enable_high_dpi():
    """Turn on Qt's high-DPI support; must run BEFORE the QApplication is created.

    AA_EnableHighDpiScaling scales the whole UI by the screen's scale factor
    (125 %, 150 % on Windows laptops), AA_UseHighDpiPixmaps lets the painted
    icons carry a device pixel ratio so they stay sharp, and the PassThrough
    rounding policy (Qt 5.14+) keeps 125 % at 1.25 instead of rounding it to 1.
    """
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def main():
    """Create state, run the connect dialog, then the main window and event loop."""
    enable_high_dpi()
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("LAN Whiteboard")
    theme.apply_theme(qapp)      # the saved light/dark theme, before any widget exists

    app = AppState()
    app.net = NetClient(app)
    app.board = BoardModel()

    # Board messages (SNAPSHOT, CHAT_HISTORY, ...) may arrive while the
    # dialog is still open; the handlers store them in app.board etc., so
    # the window built afterwards shows them.
    dialog = ConnectDialog(app)
    theme.decorate_widgets(dialog)
    if dialog.exec_() != ConnectDialog.Accepted:
        app.net.disconnect()
        return 0

    window = build_window(app)
    flow = SessionFlow(app, window)
    app.net.disconnected.connect(flow.on_disconnected)
    qapp.aboutToQuit.connect(lambda: setattr(flow, "quitting", True))
    toasts.preload_sounds()      # generate the join/leave sounds before the first join
    window.show()
    code = qapp.exec_()
    app.net.disconnect()
    return code


if __name__ == "__main__":
    sys.exit(main())
