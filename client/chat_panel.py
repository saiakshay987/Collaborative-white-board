# OWNER: P4
"""chat_panel.py: Dock panel for room chat and direct messages.

Contract sections C4 and F (client). The panel is a tab widget: a permanent
"Room" tab for CHAT, plus one tab per direct-message partner. A DM tab opens
when I double-click a user (app.dm_requested -> open_dm_tab) or automatically
when a DM arrives. A DM that arrives while its tab is not being looked at
adds an unread count to the tab label ("bob (2)") and shows a toast. DMs in
CHAT_HISTORY fill the DM tabs without counting as unread.

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
from client.registry import on_message, on_session_reset, register_panel

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
        self._note_count = 0  # source of ids for local notes

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

    def add_note(self, text):
        """Add a local grey line (e.g. "bob is not online"); it is not a network message.

        Notes are stored with the messages, so they survive re-rendering.
        """
        self._note_count += 1
        self.add_message({"msg_id": "note-%d" % self._note_count, "note": True,
                          "text": text, "ts": time.time()})

    def _line_html(self, msg):
        """Return the HTML block for one message: HH:MM, coloured name, text.

        My own messages are right-aligned on a tinted background.
        Everything that comes from the network goes through html.escape.
        """
        if msg.get("note"):
            return ('<p style="color:#888888"><i>%s</i></p>' % html.escape(str(msg.get("text", ""))))
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
        self._unread = {}                 # conversation key -> unread DM count
        self._last_dm_to = None           # whom my latest DM went to (for error notes)
        self._link_up = app.net is not None and app.net.is_connected

        self.tabs = QTabWidget()
        self.input = ChatInput()
        self.input.submitted.connect(self.send_current)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.tabs)
        layout.addWidget(self.input)

        self._tab_for(ROOM_KEY)
        self.tabs.currentChanged.connect(self._on_tab_changed)

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
            self.tabs.addTab(view, self._label(key))
        if switch:
            self.tabs.setCurrentWidget(view)
        return view

    def _label(self, key):
        """Return the tab text for a conversation: its name plus "(n)" if n DMs are unread."""
        base = ROOM_TAB if key == ROOM_KEY else key[len("dm:"):]
        count = self._unread.get(key, 0)
        return "%s (%d)" % (base, count) if count else base

    def _update_label(self, key):
        """Refresh the tab text of conversation `key` (unread badge)."""
        view = self._views.get(key)
        if view is not None:
            self.tabs.setTabText(self.tabs.indexOf(view), self._label(key))

    def _is_being_read(self, key):
        """True if conversation `key` is the current tab of a chat panel that is on screen."""
        view = self._views.get(key)
        return view is not None and self.tabs.currentWidget() is view and self.isVisible()

    def _mark_read(self, key):
        """Clear the unread badge of conversation `key`."""
        if self._unread.pop(key, 0):
            self._update_label(key)

    def _on_tab_changed(self, _index):
        """The user switched tabs: what is now in front counts as read."""
        view = self.tabs.currentWidget()
        if view is not None:
            self._mark_read(view.key)

    def showEvent(self, event):
        """The panel became visible again (e.g. its dock tab was clicked): mark the current tab read."""
        super().showEvent(event)
        self._on_tab_changed(self.tabs.currentIndex())

    def add_live_dm(self, payload):
        """A DM {msg_id, from, to, text, ts} just arrived from the server.

        It is shown in the tab of the other person (created if needed, without
        stealing focus). The server also echoes my own DMs; those are shown
        but never counted as unread. A new incoming DM whose tab is not being
        looked at adds to the unread badge and shows a toast.
        """
        msg = dict(payload, type=protocol.DM)
        key = self._conversation_key(msg)
        if not key:
            return
        added = self._tab_for(key).add_message(msg)
        if not added or msg.get("from") == self.app.me.username:
            return
        if not self._is_being_read(key):
            self._unread[key] = self._unread.get(key, 0) + 1
            self._update_label(key)
            self.app.toast("New message from %s" % msg.get("from"))

    def on_dm_error(self, payload):
        """The server refused my latest DM: say so inside that DM tab.

        P1's generic ERROR toast only gives a generic text for NOT_FOUND, so the
        reason is written into the conversation where the user is looking.
        """
        name = self._last_dm_to
        if not name:
            return
        if payload.get("code") == protocol.NOT_FOUND:
            reason = "%s is not online in this room. Message not delivered." % name
        else:
            reason = "Message not delivered: %s" % (payload.get("text") or payload.get("code"))
        self._tab_for("dm:" + name).add_note(reason)

    def open_dm_tab(self, name):
        """Switch to the DM tab with `name`, creating it first if needed.

        Connected to app.dm_requested (double-click in the users panel).
        Talking to myself makes no sense, so that is ignored.
        """
        if not name or name == self.app.me.username:
            return
        self._tab_for("dm:" + name, switch=True)
        self._mark_read("dm:" + name)   # no currentChanged if it was already current
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
            for key in list(self._views):
                view = self._views[key]
                if key == ROOM_KEY:
                    view.clear_messages()
                else:
                    # DMs belong to the old room: close their tabs.
                    self.tabs.removeTab(self.tabs.indexOf(view))
                    view.deleteLater()
                    del self._views[key]
            self._unread.clear()
            self._room = room_code
        self._update_input()

    def reset(self):
        """The user left the room: drop every conversation and start with an empty Room tab.

        All tabs go (the DM tabs are closed), the Room view forgets its
        messages and with them the set of seen msg_ids, so the CHAT_HISTORY
        of the next join is shown in full, once. The input is disabled until
        the next JOIN_OK.
        """
        for key, view in list(self._views.items()):
            if key == ROOM_KEY:
                view.clear_messages()
            else:
                self.tabs.removeTab(self.tabs.indexOf(view))
                view.deleteLater()
                del self._views[key]
        self._tab_for(ROOM_KEY, switch=True)
        self._unread.clear()
        self._last_dm_to = None
        self._room = None
        self._link_up = False
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
        """Send DM {to, text} to `name`; returns False if the link is down.

        Like CHAT, the message is not drawn locally: the server echoes it back
        to me (and only to me and the recipient), and that echo is shown. The
        server answers ERROR NOT_FOUND if `name` is offline; on_dm_error()
        turns that into a note in the tab.
        """
        self._last_dm_to = name
        return self.app.net.send(protocol.DM, {"to": name, "text": text})


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
    elif msg_type == protocol.DM:
        _panel.add_live_dm(payload)
    elif msg_type == protocol.ERROR and payload.get("ref_type") == protocol.DM:
        _panel.on_dm_error(payload)


@on_session_reset
def _on_session_reset(app):
    """The user left the room: empty the chat panel and forget messages parked for it."""
    del _pending[:]
    if _panel is not None:
        _panel.reset()


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


@on_message(protocol.DM)
def _on_dm(app, msg):
    """DM {msg_id, from, to, text, ts}: a private message for me, or the echo of mine.

    The server sends it only to the recipient and the sender (unicast), so
    whatever arrives here is always a conversation I am part of.
    """
    if _panel is None:
        _pending.append((protocol.DM, msg))
    else:
        _dispatch(protocol.DM, msg)


@on_message(protocol.ERROR)
def _on_error(app, msg):
    """ERROR for a DM (recipient offline, ...): written into the DM tab by the panel."""
    if _panel is not None:
        _dispatch(protocol.ERROR, msg)


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
