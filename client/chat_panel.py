# OWNER: P4
"""chat_panel.py: Dock panel for room chat and direct messages.

Contract sections C4 and F (client). The panel is a tab widget: a permanent
"Room" tab for CHAT, plus one tab per direct-message partner (DM sending and
receiving arrive in a later task; open_dm_tab() and the routing code are
already here).

Data flow (the GUI thread is never blocked, nothing here touches a socket):

    network thread -> NetClient._frame signal (queued) -> GUI thread
        -> registry.deliver_message -> @on_message handlers below
        -> ChatPanel.add_messages -> ConversationView.add_message

Sending only calls app.net.send(), which puts the frame on a queue that the
sender thread drains.

Rules the views follow:
  * Messages are kept sorted by the server timestamp `ts` (the server is the
    single clock, so every client shows the same order).
  * A msg_id that is already shown is ignored: after a reconnect the server
    resends CHAT_HISTORY, which overlaps with live messages.
  * The view scrolls down to new messages only if it was already at the bottom,
    so reading old messages is not interrupted.
  * Message text is HTML-escaped before display, so a user cannot inject markup.

CHAT_HISTORY and CHAT can arrive while the connect dialog is still open, i.e.
before this panel exists (run_client.py builds the window afterwards). The
handlers therefore park such messages in _pending and the panel replays them
when it is created.
"""

import bisect
import html
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QPlainTextEdit, QTabWidget, QTextBrowser, QVBoxLayout,
                             QWidget)

from common import protocol
from client.registry import on_message, register_panel

ROOM_TAB = "Room"            # label of the room tab
ROOM_KEY = "room"            # conversation key of the room; DMs use "dm:<name>"
FALLBACK_COLOR = "#555555"   # name colour when the sender is not in app.users
OWN_BACKGROUND = "#e8f0fe"   # highlight of my own messages
BOTTOM_TOLERANCE = 4         # pixels: "at the bottom" if the scrollbar is this close


def format_time(ts):
    """Return the server timestamp ts (epoch seconds) as local HH:MM."""
    return time.strftime("%H:%M", time.localtime(ts))


class ChatInput(QPlainTextEdit):
    """Multi-line input: Enter submits, Shift+Enter inserts a new line."""

    submitted = pyqtSignal()

    def __init__(self, parent=None):
        """Create a compact input box of about three lines."""
        super().__init__(parent)
        self.setFixedHeight(64)
        self.setTabChangesFocus(True)

    def keyPressEvent(self, event):
        """Emit `submitted` on Enter; let every other key (and Shift+Enter) act normally."""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ConversationView(QTextBrowser):
    """The message list of ONE conversation (the room, or one DM partner).

    Stores the messages (sorted by ts) and renders them as HTML. Appending a
    message that belongs at the end only adds one block; an older message
    (rare, e.g. history after live messages) triggers a full re-render.
    """

    def __init__(self, app, key, parent=None):
        """Create an empty conversation for AppState app, identified by key."""
        super().__init__(parent)
        self.app = app
        self.key = key        # ROOM_KEY or "dm:<name>"
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self._messages = []   # message dicts, sorted by ts
        self._ids = set()     # msg_ids already shown

    # -- data ----------------------------------------------------------------

    def add_message(self, msg):
        """Insert one message dict unless its msg_id is already shown.

        Returns True if it was added.
        """
        msg_id = msg.get("msg_id")
        if msg_id in self._ids:
            return False
        self._ids.add(msg_id)
        ts = msg.get("ts") or 0.0
        # bisect_right keeps messages with an equal ts in arrival order.
        index = bisect.bisect_right(self._messages, ts, key=lambda m: m.get("ts") or 0.0)
        self._messages.insert(index, msg)

        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - BOTTOM_TOLERANCE
        if index == len(self._messages) - 1:
            self.append(self._line_html(msg))      # the common case: newest message
        else:
            self.render_all()                       # out of order: rebuild
            return True
        if at_bottom:
            bar.setValue(bar.maximum())
        return True

    def clear_messages(self):
        """Forget every message (used when the user enters a different room)."""
        self._messages = []
        self._ids = set()
        self.clear()

    def render_all(self):
        """Re-render every message, keeping the scroll position.

        If the view was at the bottom it stays there; otherwise the old
        scrollbar value is restored.
        """
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - BOTTOM_TOLERANCE
        old_value = bar.value()
        self.setHtml("".join(self._line_html(m) for m in self._messages))
        bar.setValue(bar.maximum() if at_bottom else old_value)

    # -- rendering -----------------------------------------------------------

    def _color_of(self, name):
        """Return the colour of user `name` from app.users (grey if unknown)."""
        for user in self.app.users:
            if user.get("name") == name:
                return user.get("color") or FALLBACK_COLOR
        return FALLBACK_COLOR

    def _line_html(self, msg):
        """Return the HTML block for one message: HH:MM, coloured name, text.

        My own messages are right-aligned on a tinted background.
        Everything that comes from the network goes through html.escape.
        """
        sender = str(msg.get("from", "?"))
        mine = sender == self.app.me.username
        body = html.escape(str(msg.get("text", ""))).replace("\n", "<br>")
        stamp = format_time(msg.get("ts") or 0.0)
        color = html.escape(self._color_of(sender), quote=True)
        line = ('<span style="color:#888888">%s</span> '
                '<b style="color:%s">%s</b><br>%s'
                % (stamp, color, html.escape(sender), body))
        align = "right" if mine else "left"
        background = ' bgcolor="%s"' % OWN_BACKGROUND if mine else ""
        return ('<table width="100%%" cellspacing="0" cellpadding="3"><tr>'
                '<td align="%s"%s>%s</td></tr></table>' % (align, background, line))


class ChatPanel(QWidget):
    """Tabbed chat: the Room tab, one tab per DM partner, and the input box."""

    def __init__(self, app, parent=None):
        """Build the tabs and input, connect the signals and replay parked messages."""
        super().__init__(parent)
        self.app = app
        self._room = app.me.room          # room whose messages are shown
        self._views = {}                  # conversation key -> ConversationView
        self._link_up = app.net is not None and app.net.is_connected

        self.tabs = QTabWidget()
        self.input = ChatInput()
        self.input.submitted.connect(self.send_current)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.tabs)
        layout.addWidget(self.input)

        self._tab_for(ROOM_KEY)

        app.users_changed.connect(self._on_users_changed)
        app.dm_requested.connect(self.open_dm_tab)
        if app.net is not None:
            # Network state decides whether the input is usable.
            app.net.connected.connect(lambda: self._set_link(True))
            app.net.reconnected.connect(lambda: self._set_link(True))
            app.net.reconnecting.connect(lambda: self._set_link(False))
            app.net.disconnected.connect(lambda _reason: self._set_link(False))
        self._update_input()

    # -- conversations ---------------------------------------------------------

    def _tab_for(self, key, switch=False):
        """Return the view for conversation `key`, creating its tab if needed.

        key is ROOM_KEY for the room, or "dm:<name>" for a DM. Keys are kept
        apart from tab labels, so a user called "Room" cannot hijack the
        room tab. switch=True also makes that tab the current one.
        """
        view = self._views.get(key)
        if view is None:
            view = ConversationView(self.app, key)
            self._views[key] = view
            self.tabs.addTab(view, ROOM_TAB if key == ROOM_KEY else key[len("dm:"):])
        if switch:
            self.tabs.setCurrentWidget(view)
        return view

    def open_dm_tab(self, name):
        """Switch to the DM tab with `name`, creating it first if needed.

        Connected to app.dm_requested (double-click in the users panel).
        Talking to myself makes no sense, so that is ignored.
        """
        if not name or name == self.app.me.username:
            return
        self._tab_for("dm:" + name, switch=True)
        self.input.setFocus()

    def _conversation_key(self, msg):
        """Return the key of the tab a message belongs to: the room, or the other party of a DM."""
        if msg.get("type") == protocol.DM:
            me = self.app.me.username
            other = msg.get("to") if msg.get("from") == me else msg.get("from")
            return "dm:" + other if isinstance(other, str) and other else None
        return ROOM_KEY

    def add_messages(self, messages):
        """Show a list of message dicts (history or live), each in its own tab."""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            key = self._conversation_key(msg)
            if key:
                self._tab_for(key).add_message(msg)

    # -- room changes and users ------------------------------------------------

    def on_join_ok(self, room_code):
        """JOIN_OK arrived: forget old messages if this is a different room."""
        if room_code != self._room:
            for view in self._views.values():
                view.clear_messages()
            self._room = room_code
        self._update_input()

    def _on_users_changed(self):
        """Colours may have changed (new user, rejoin): re-render every view."""
        for view in self._views.values():
            view.render_all()

    # -- input and network state -----------------------------------------------

    def _set_link(self, up):
        """Remember whether the TCP link is up and update the input box."""
        self._link_up = up
        self._update_input()

    def _update_input(self):
        """Enable the input only while connected and inside a room.

        `connected` fires when TCP is up, before JOIN_OK; sending then would
        only earn a NO_ROOM error, so a room is required as well.
        """
        usable = self._link_up and self.app.me.room is not None
        self.input.setEnabled(usable)
        self.input.setPlaceholderText(
            "Enter sends, Shift+Enter for a new line" if usable else "Not connected")

    def send_current(self):
        """Send the input text to the conversation of the current tab.

        The text is not drawn locally: the server echo (CHAT goes to the
        sender too) is what appears, so every client shows the same thing.
        The box is cleared only if the message was queued for sending.
        """
        text = self.input.toPlainText().strip()
        if not text or not self.input.isEnabled():
            return
        key = self.tabs.currentWidget().key
        if key == ROOM_KEY:
            sent = self.app.net.send(protocol.CHAT, {"text": text})
        else:
            sent = self.send_dm(key[len("dm:"):], text)
        if sent:
            self.input.clear()

    def send_dm(self, name, text):
        """Hook for the DM task: send `text` to `name`. Not implemented yet.

        Returns False so the typed text is kept.
        """
        self.app.toast("Direct messages are not available yet.")
        return False


# --- Module-level wiring -------------------------------------------------------

_panel = None    # the one ChatPanel, set by create_chat_panel
_pending = []    # (type, msg) received before the panel existed


def create_chat_panel(app):
    """Panel factory for register_panel: build the ChatPanel and replay parked messages."""
    global _panel
    _panel = ChatPanel(app)
    parked = list(_pending)
    del _pending[:]
    for msg_type, msg in parked:
        _dispatch(msg_type, msg)
    return _panel


def _dispatch(msg_type, msg):
    """Give an incoming message to the panel."""
    payload = msg.get("payload") or {}
    if msg_type == protocol.CHAT_HISTORY:
        _panel.add_messages(payload.get("messages") or [])
    elif msg_type == protocol.CHAT:
        # A live CHAT payload has no `type` field; add it so it routes to the Room tab.
        _panel.add_messages([dict(payload, type=protocol.CHAT)])


@on_message(protocol.CHAT_HISTORY)
def _on_chat_history(app, msg):
    """CHAT_HISTORY {messages}: sent after every join and rejoin (duplicates are ignored)."""
    if _panel is None:
        _pending.append((protocol.CHAT_HISTORY, msg))
    else:
        _dispatch(protocol.CHAT_HISTORY, msg)


@on_message(protocol.CHAT)
def _on_chat(app, msg):
    """CHAT {msg_id, from, text, ts}: one live room message (the sender gets it too)."""
    if _panel is None:
        _pending.append((protocol.CHAT, msg))
    else:
        _dispatch(protocol.CHAT, msg)


@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK: lets the panel clear old messages when the room changed.

    Runs after net_client's own JOIN_OK handler (bootstrap order), so
    app.me.room is already up to date. Before the panel exists there is
    nothing to clear.
    """
    if _panel is not None:
        _panel.on_join_ok((msg.get("payload") or {}).get("room"))


register_panel("chat", create_chat_panel, area="right", title="Chat")
