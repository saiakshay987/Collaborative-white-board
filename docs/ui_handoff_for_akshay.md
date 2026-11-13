<!-- OWNER: P1 -->
# UI polish: what changed in your files (P2, P3, P4)

Final version for the branch `sharwan/ui-polish` (UI polish phases 1 to 5).
Written by P1 (Sharwan) so every owner can review and explain the lines in
their own files. Akshay (P2), Janith (P3) and Harshith (P4) approved these
changes in the group chat before they were applied.

## How to read this

* Each owner has one section. Each file in it has a short "what and why",
  then the **complete** change as `git diff origin/main` (`-` before,
  `+` after). Nothing outside these diffs changed in your files.
* Rules kept in every file: the `# OWNER:` line is unchanged; no message types,
  registries, tool ids, labels, panel names, method signatures or behaviour
  changed. Only colours, styling and widget arrangement.
* Colours now come from `client/theme.py`:
  `theme.tokens()` gives the colours of the active light or dark mode,
  `theme.readable(color, bg)` adjusts a user colour to 4.5:1 contrast, and
  `theme.notifier.changed` fires after a theme switch. The style sheet rules
  for the object names below live in theme.py.
* Phase 4 part D (infinite canvas, zoom buttons) was reverted. `canvas.py`
  is back to the phase 3 look: the white page with a soft shadow on the
  themed surround. Its lines are no longer in this document.
* Phase 5 changed **no** file of P2, P3 or P4. Section 5 lists what phase 5
  changes around your widgets from P1's files.

| Owner | File | Change |
|---|---|---|
| Akshay (P2) | `client/canvas.py` | +3 / -3 lines |
| Akshay (P2) | `client/shortcuts.py` | +8 / -9 lines |
| Akshay (P2) | `client/toolbar.py` | +21 / -16 lines |
| Janith (P3) | `client/history_panel.py` | +45 / -13 lines |
| Harshith (P4) | `client/chat_panel.py` | +21 / -10 lines |
| Harshith (P4) | `client/main_window.py` | +6 / -1 lines |

---

## 1. Akshay (P2)

### `client/canvas.py`

The grey around the page comes from the theme (`surround`: #E9ECEF in light;
the window background in dark). The page stays white, and exports never use
this colour. Viva point: the board, units, snapping and export are untouched;
only the fill of the area outside the page changed.

```diff
diff --git a/client/canvas.py b/client/canvas.py
--- a/client/canvas.py
+++ b/client/canvas.py
@@ -55,5 +55,5 @@ from PyQt5.QtGui import QColor, QCursor, QPainter, QPen
 from PyQt5.QtWidgets import QLabel, QWidget
 
-from client import render
+from client import render, theme
 from common import config
 
@@ -64,5 +64,5 @@ ZOOM_STEP = 1.15
 
 # Colors of the background outside the page, the page and its grid.
-OUTSIDE_COLOR = QColor("#d6d6d6")
+OUTSIDE_COLOR = QColor("#d6d6d6")   # fallback; the surround comes from theme.tokens()["surround"]
 PAGE_COLOR = QColor("#ffffff")
 PAGE_BORDER_COLOR = QColor("#616161")
@@ -387,5 +387,5 @@ class CanvasWidget(QWidget):
         painter = QPainter(self)
         try:
-            painter.fillRect(self.rect(), OUTSIDE_COLOR)
+            painter.fillRect(self.rect(), QColor(theme.tokens()["surround"]))   # follows light/dark
             self._paint_page_shadow(painter)      # in screen pixels, before the transform
 
```

### `client/shortcuts.py`

Command bar: the fixed #fafafa panel is replaced by the theme rule
`#command_bar` (surface colour, border, 8 px radius). The hint, result and
error lines use the theme's muted, success and danger text colours, which are
readable in both modes.

Phase 5 note: the hint line takes `muted` when the bar is built. On a theme
switch, `theme.retint_widget_styles()` swaps that colour for the new mode, so
nothing has to change here. A later cleanup could use
`self._hints.setProperty("muted", True)` instead of the inline style (optional).

```diff
diff --git a/client/shortcuts.py b/client/shortcuts.py
--- a/client/shortcuts.py
+++ b/client/shortcuts.py
@@ -82,5 +82,5 @@ from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLa
                              QLineEdit, QMessageBox, QTextBrowser, QVBoxLayout)
 
-from client import ops, registry, toolbar
+from client import ops, registry, theme, toolbar
 from client.canvas import on_page
 from client.tools import shape_tools
@@ -117,7 +117,7 @@ BAR_MARGIN = 8                # pixels between the bar and the canvas edges
 HISTORY_MAX = 100             # commands remembered for Up/Down
 HINT_ROWS = 6                 # at most this many matching commands in the hint list
-ERROR_STYLE = "QLabel { color: #c62828; }"
-INFO_STYLE = "QLabel { color: #2e7d32; }"
-HINT_STYLE = "QLabel { color: #555; }"
+ERROR_STYLE = "QLabel { color: %s; }"   # %s: theme token danger_text (readable in light and dark)
+INFO_STYLE = "QLabel { color: %s; }"    # %s: theme token success_text
+HINT_STYLE = "QLabel { color: %s; }"    # %s: theme token muted
 
 # command -> (syntax, description), in the order the hints show them.
@@ -610,7 +610,5 @@ class CommandBar(QFrame):
         self.setObjectName("command_bar")
         self.setFrameShape(QFrame.StyledPanel)
-        self.setAutoFillBackground(True)
-        self.setStyleSheet("#command_bar { background: #fafafa; border: 1px solid #9e9e9e;"
-                           " border-radius: 4px; }")
+        self.setAutoFillBackground(True)   # background, border and radius: #command_bar in client/theme.py
 
         layout = QVBoxLayout(self)
@@ -619,5 +617,5 @@ class CommandBar(QFrame):
         self._hints = QLabel()
         self._hints.setTextFormat(Qt.RichText)
-        self._hints.setStyleSheet(HINT_STYLE)
+        self._hints.setStyleSheet(HINT_STYLE % theme.tokens()["muted"])
         layout.addWidget(self._hints)
         self._msg = QLabel()
@@ -711,5 +709,6 @@ class CommandBar(QFrame):
     def _show(self, text, error):
         """Print a result (green) or an error (red) above the line edit."""
-        self._msg.setStyleSheet(ERROR_STYLE if error else INFO_STYLE)
+        self._msg.setStyleSheet(ERROR_STYLE % theme.tokens()["danger_text"] if error
+                                else INFO_STYLE % theme.tokens()["success_text"])
         self._msg.setText(text)
         self._msg.show()
```

### `client/toolbar.py`

Swatches keep only their fill colour inline. The round shape, the outline and
the ring on the selected swatch come from the theme (`#swatch`, `#inkSwatch`,
`#currentSwatch`). The text colour button is 24 x 24 like the swatches. The
width preview is a small piece of the white page with a rounded theme border;
it stays white in dark mode on purpose, so a black pen dot stays visible.
The viewer hint and the info line use theme colours (`#viewerHint`, `muted`).

```diff
diff --git a/client/toolbar.py b/client/toolbar.py
--- a/client/toolbar.py
+++ b/client/toolbar.py
@@ -74,5 +74,5 @@ from PyQt5.QtWidgets import (QButtonGroup, QColorDialog, QComboBox, QGridLayout,
                              QVBoxLayout, QWidget)
 
-from client import ops, registry, render_text
+from client import ops, registry, render_text, theme
 from common import kinds_text, protocol
 
@@ -117,8 +117,11 @@ def _tool_tooltip(cls):
 
 def _swatch_style(color, checked_border="#1565c0"):
-    """Style sheet for a color swatch button: filled square, thick border when checked."""
-    return ("QToolButton { background: %s; border: 1px solid #888; border-radius: 3px; }"
-            "QToolButton:checked { border: 3px solid %s; }"
-            "QToolButton:disabled { border: 1px dashed #bbb; }" % (color, checked_border))
+    """Style sheet for a color swatch button: only its fill colour.
+
+    The round shape, the outline and the ring of the selected swatch come from
+    the theme (QToolButton#swatch in client/theme.py), so they follow light/dark.
+    checked_border is kept for callers; the ring uses the theme's primary colour.
+    """
+    return "QToolButton { background: %s; }" % color
 
 
@@ -166,7 +169,8 @@ class WidthPreview(QWidget):
         try:
             painter.setRenderHint(QPainter.Antialiasing, True)
-            painter.fillRect(self.rect(), QColor("#f4f4f4"))
-            painter.setPen(QPen(QColor("#c8c8c8")))
-            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
+            # A small piece of the (always white) page, framed in the theme's border colour.
+            painter.setPen(QPen(QColor(theme.tokens()["border_strong"])))
+            painter.setBrush(QColor(theme.tokens()["page"]))
+            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
             d = min(self._width, self.SIDE - 4.0)
             box = QRectF((self.width() - d) / 2.0, (self.height() - d) / 2.0, d, d)
@@ -227,5 +231,6 @@ class FormatBar(QGroupBox):
         self._size.valueChanged.connect(self._on_size)
         self._ink = QToolButton()
-        self._ink.setFixedSize(QSize(28, 24))
+        self._ink.setObjectName("inkSwatch")        # round, outlined: client/theme.py
+        self._ink.setFixedSize(QSize(24, 24))
         self._ink.setToolTip("Text color")
         self._ink.clicked.connect(self._pick_ink)
@@ -281,4 +286,5 @@ class FormatBar(QGroupBox):
             sw.setFixedSize(QSize(24, 24))
             sw.setToolTip(color)
+            sw.setObjectName("swatch")
             sw.setStyleSheet(_swatch_style(color))
             sw.clicked.connect(lambda _checked, c=color: self._send({}, bg=c))
@@ -388,6 +394,5 @@ class FormatBar(QGroupBox):
         self._size.blockSignals(False)
         self._ink_color = values["ink"]             # where the color dialog starts
-        self._ink.setStyleSheet("QToolButton { background: %s; border: 1px solid #888;"
-                                " border-radius: 3px; }" % values["ink"])
+        self._ink.setStyleSheet("QToolButton { background: %s; }" % values["ink"])
         self._ink.setToolTip("Text color %s" % values["ink"])
         for field, btn in self._flags.items():
@@ -492,6 +497,5 @@ class ToolDock(QWidget):
         self._hint = QLabel(VIEWER_HINT)
         self._hint.setWordWrap(True)
-        self._hint.setStyleSheet("QLabel { background: #fff3cd; color: #664d03;"
-                                 " border: 1px solid #ffe69c; border-radius: 4px; padding: 6px; }")
+        self._hint.setObjectName("viewerHint")      # warning colours: client/theme.py
         self._hint.hide()
         layout.addWidget(self._hint)
@@ -684,4 +688,5 @@ class ToolDock(QWidget):
             sw.setFixedSize(QSize(24, 24))
             sw.setToolTip(color)
+            sw.setObjectName("swatch")
             sw.setStyleSheet(_swatch_style(color))
             sw.clicked.connect(lambda _checked, c=color: self._set_color(c))
@@ -694,4 +699,5 @@ class ToolDock(QWidget):
         more_row = QHBoxLayout()
         self._current_swatch = QLabel()
+        self._current_swatch.setObjectName("currentSwatch")   # round, outlined: client/theme.py
         self._current_swatch.setFixedSize(24, 24)
         self._current_swatch.setToolTip("Current color")
@@ -723,5 +729,5 @@ class ToolDock(QWidget):
         self._info_label = QLabel()
         self._info_label.setWordWrap(True)
-        self._info_label.setStyleSheet("QLabel { color: #555; }")
+        self._info_label.setProperty("muted", True)    # muted text colour: client/theme.py
         v.addWidget(self._info_label)
         return box
@@ -774,6 +780,5 @@ class ToolDock(QWidget):
                 checked.setChecked(False)
                 self._swatch_group.setExclusive(True)
-        self._current_swatch.setStyleSheet("QLabel { background: %s; border: 1px solid #888;"
-                                           " border-radius: 3px; }" % color)
+        self._current_swatch.setStyleSheet("QLabel { background: %s; }" % color)
         self._current_swatch.setToolTip("Current color %s" % color)
 
```

---

## 2. Janith (P3)

### `client/history_panel.py`

The buttons move from a tall column to one compact row above the list:
Save version (with text) | Restore, Rename, Delete | Undo, Redo (icon-only,
with tooltips and accessible names; the icons are set by objectName in
theme.py, 20 px since phase 5). The Name column stretches; By, Time and
Objects fit their contents. Rows are 32 px without grid lines. The
Objects | seq | checksum line is a small muted footer. An empty list shows
"No versions yet. Save one to keep a snapshot." as a label over the table's
viewport that ignores the mouse, so selection and right-click work as before.
The same attributes (`save_btn`, `restore_btn`, ...) and slots are used, so
`p3_test_history_panel.py` passes unchanged.

```diff
diff --git a/client/history_panel.py b/client/history_panel.py
--- a/client/history_panel.py
+++ b/client/history_panel.py
@@ -51,4 +51,5 @@ HOST_ONLY_TIP = "Only the host can %s a version"   # tooltip of a disabled butto
 AUTO_NAME_RESTORE = "before restore "   # the names the server gives to its automatic versions
 AUTO_NAME_CLEAR = "before clear"
+EMPTY_TEXT = "No versions yet. Save one to keep a snapshot."   # shown while the list is empty
 
 
@@ -264,17 +265,31 @@ class HistoryPanel(QWidget):
         self.table.setMinimumHeight(90)
         header = self.table.horizontalHeader()
-        header.setSectionResizeMode(0, QHeaderView.Stretch)
+        header.setSectionResizeMode(0, QHeaderView.Stretch)       # Name fills the width
         for col in (1, 2, 3):
             header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
+        self.table.setShowGrid(False)                              # card-style rows: client/theme.py
+        self.table.verticalHeader().setDefaultSectionSize(32)
+        # Shown over the empty list (muted, centred; clicks go through to the table).
+        self.empty_label = QLabel(EMPTY_TEXT, self.table.viewport())
+        self.empty_label.setAlignment(Qt.AlignCenter)
+        self.empty_label.setProperty("muted", True)
+        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
 
         self.save_btn = QPushButton("Save version")
+        self.save_btn.setObjectName("historySave")
         self.save_btn.setToolTip("Store a copy of the board (Ctrl+S saves with a default name)")
-        self.restore_btn = QPushButton("Restore")
-        self.rename_btn = QPushButton("Rename")
-        self.delete_btn = QPushButton("Delete")
-        self.undo_btn = QPushButton("Undo")
+        # Icon-only buttons (icons and look: client/theme.py); the tooltip says what each one does.
+        self.restore_btn = QPushButton()
+        self.rename_btn = QPushButton()
+        self.delete_btn = QPushButton()
+        self.undo_btn = QPushButton()
         self.undo_btn.setToolTip("Undo my last change (Ctrl+Z)")
-        self.redo_btn = QPushButton("Redo")
+        self.redo_btn = QPushButton()
         self.redo_btn.setToolTip("Redo what I undid (Ctrl+Y)")
+        for button, name in ((self.restore_btn, "Restore"), (self.rename_btn, "Rename"),
+                             (self.delete_btn, "Delete"), (self.undo_btn, "Undo"),
+                             (self.redo_btn, "Redo")):
+            button.setObjectName("history" + name)
+            button.setAccessibleName(name)
         self.footer = QLabel()
         font = QFont("Consolas")
@@ -283,15 +298,21 @@ class HistoryPanel(QWidget):
         self.footer.setTextInteractionFlags(Qt.TextSelectableByMouse)
         self.footer.setToolTip("Same seq and no pending operations on two clients: the checksums must be equal")
-
-        buttons = QVBoxLayout()
-        for button in (self.save_btn, self.restore_btn, self.rename_btn, self.delete_btn, self.undo_btn, self.redo_btn):
+        self.footer.setProperty("muted", True)             # small muted footer: client/theme.py
+
+        # One compact row above the list: Save | Restore Rename Delete | Undo Redo.
+        buttons = QHBoxLayout()
+        buttons.setSpacing(4)
+        buttons.addWidget(self.save_btn)
+        buttons.addSpacing(8)
+        for button in (self.restore_btn, self.rename_btn, self.delete_btn):
+            buttons.addWidget(button)
+        buttons.addSpacing(8)
+        for button in (self.undo_btn, self.redo_btn):
             buttons.addWidget(button)
         buttons.addStretch(1)
-        top = QHBoxLayout()
-        top.addWidget(self.table, 1)
-        top.addLayout(buttons)
         layout = QVBoxLayout(self)
         layout.setContentsMargins(6, 4, 6, 4)
-        layout.addLayout(top, 1)
+        layout.addLayout(buttons)
+        layout.addWidget(self.table, 1)
         layout.addWidget(self.footer)
 
@@ -342,6 +363,17 @@ class HistoryPanel(QWidget):
             if keep is not None and keep["id"] == v["id"]:
                 self.table.selectRow(row)
+        self.empty_label.setVisible(self.table.rowCount() == 0)
+        self._place_empty_label()
         self.refresh_buttons()
 
+    def _place_empty_label(self):
+        """Stretch the empty-list text over the table's visible area."""
+        self.empty_label.setGeometry(self.table.viewport().rect())
+
+    def resizeEvent(self, event):
+        """Keep the empty-list text centred when the panel (and so the table) is resized."""
+        super().resizeEvent(event)
+        self._place_empty_label()
+
     def refresh_buttons(self):
         """Enable the buttons the role may use (the server checks again)."""
```

---

## 3. Harshith (P4)

### `client/chat_panel.py`

Messages are bubbles: mine on the right in `primary_soft`, other people's on
the left in `surface_alt`, at most 80 % wide. The time is muted and names are
drawn in the sender's colour, adjusted to 4.5:1 with `theme.readable`. This
fixed unreadable own messages in dark mode (`OWN_BACKGROUND` was a fixed light
blue under light text). Open conversations re-render on a theme switch, and
an empty conversation shows a placeholder line.

```diff
diff --git a/client/chat_panel.py b/client/chat_panel.py
--- a/client/chat_panel.py
+++ b/client/chat_panel.py
@@ -42,4 +42,5 @@ from PyQt5.QtWidgets import (QPlainTextEdit, QTabWidget, QTextBrowser, QVBoxLayo
 
 from common import protocol
+from client import theme
 from client.registry import on_message, on_session_reset, register_panel
 
@@ -47,5 +48,6 @@ ROOM_TAB = "Room"            # label of the room tab
 ROOM_KEY = "room"            # conversation key of the room; DMs use "dm:<name>"
 FALLBACK_COLOR = "#555555"   # name colour when the sender is not in app.users
-OWN_BACKGROUND = "#e8f0fe"   # highlight of my own messages
+OWN_BACKGROUND = "#e8f0fe"   # highlight of my own messages (light theme; see _line_html)
+EMPTY_TEXT = "No messages yet. Say hello to the room."   # shown while a conversation is empty
 BOTTOM_TOLERANCE = 4         # pixels: "at the bottom" if the scrollbar is this close
 
@@ -93,4 +95,5 @@ class ConversationView(QTextBrowser):
         self._ids = set()     # msg_ids already shown
         self._note_count = 0  # source of ids for local notes
+        self.setPlaceholderText(EMPTY_TEXT if key == ROOM_KEY else "No messages yet.")
 
     # -- data ----------------------------------------------------------------
@@ -163,18 +166,23 @@ class ConversationView(QTextBrowser):
         Everything that comes from the network goes through html.escape.
         """
+        t = theme.tokens()                      # colours of the active light/dark theme
         if msg.get("note"):
-            return ('<p style="color:#888888"><i>%s</i></p>' % html.escape(str(msg.get("text", ""))))
+            return ('<p style="color:%s"><i>%s</i></p>' % (t["muted"], html.escape(str(msg.get("text", "")))))
         sender = str(msg.get("from", "?"))
         mine = sender == self.app.me.username
         body = html.escape(str(msg.get("text", ""))).replace("\n", "<br>")
         stamp = format_time(msg.get("ts") or 0.0)
-        color = html.escape(self._color_of(sender), quote=True)
-        line = ('<span style="color:#888888">%s</span> '
-                '<b style="color:%s">%s</b><br>%s'
-                % (stamp, color, html.escape(sender), body))
-        align = "right" if mine else "left"
-        background = ' bgcolor="%s"' % OWN_BACKGROUND if mine else ""
-        return ('<table width="100%%" cellspacing="0" cellpadding="3"><tr>'
-                '<td align="%s"%s>%s</td></tr></table>' % (align, background, line))
+        bubble = t["primary_soft"] if mine else t["surface_alt"]
+        color = html.escape(theme.readable(self._color_of(sender), bubble), quote=True)
+        head = ('<span style="color:%s">%s</span>' % (t["muted"], stamp) if mine else
+                '<b style="color:%s">%s</b>&nbsp;&nbsp;<span style="color:%s">%s</span>'
+                % (color, html.escape(sender), t["muted"], stamp))
+        line = '%s<br><span style="color:%s">%s</span>' % (head, t["text"], body)
+        # A bubble: my own on the right in primary_soft, others on the left; the empty cell
+        # on the other side keeps every bubble at most 80 % wide.
+        cell = '<td width="80%%" bgcolor="%s">%s</td>' % (bubble, line)
+        cells = ('<td width="20%%"></td>' + cell) if mine else (cell + '<td width="20%%"></td>')
+        return ('<table width="100%%" cellspacing="0" cellpadding="7" style="margin-top:6px">'
+                '<tr>%s</tr></table>' % cells)
 
 
@@ -212,4 +220,7 @@ class ChatPanel(QWidget):
             app.net.disconnected.connect(lambda _reason: self._set_link(False))
         self._update_input()
+        # Bubble colours come from the theme: re-render the open conversations when it changes.
+        theme.notifier.changed.connect(
+            lambda _mode: [view.render_all() for view in self._views.values()])
 
     # -- conversations ---------------------------------------------------------
```

### `client/main_window.py`

`auto_arrange_right` (default True keeps the old behaviour): shell.py sets it
to False, so Users stays above Chat at every width. Before, it emptied the
private `_right_docks` list. The View-only banner gets the objectName
`viewOnlyBanner` (theme colours) instead of a fixed yellow.

```diff
diff --git a/client/main_window.py b/client/main_window.py
--- a/client/main_window.py
+++ b/client/main_window.py
@@ -88,5 +88,5 @@ class ViewOnlyBanner(QLabel):
         super().__init__("View only: you can watch and chat, but not draw", parent)
         self.setAlignment(Qt.AlignCenter)
-        self.setStyleSheet("background-color: #ffeb3b; color: #333333; font-weight: bold;")
+        self.setObjectName("viewOnlyBanner")   # colours come from client/theme.py
         self.setAttribute(Qt.WA_TransparentForMouseEvents)
         parent.installEventFilter(self)
@@ -242,4 +242,7 @@ class MainWindow(QMainWindow):
     """The application window, built from the registries."""
 
+    # False: never tab or split the right-hand docks automatically (client/shell.py arranges them).
+    auto_arrange_right = True
+
     # Emitted (GUI thread) with the tool id whenever the active tool changes,
     # whoever changed it. Widgets show the current tool by connecting here.
@@ -394,4 +397,6 @@ class MainWindow(QMainWindow):
         within one range never rearranges anything the user has dragged.
         """
+        if not self.auto_arrange_right:
+            return
         narrow = self.width() < NARROW_WIDTH
         if len(self._right_docks) < 2 or narrow == self._right_tabbed:
```

---

## 4. Note for Harshith: `scripts/p4_test_text.py`

Two format-bar tests fail, the same way on `origin/main` as on this branch:

```
ERROR: test_format_bar_updates_the_selected_text
    self.assertEqual(self.app.active_tool.selected_id, obj_id)
AttributeError: 'SelectTool' object has no attribute 'selected_id'
```

`SelectTool.selected_id` went away with multi-select (PR #37). The format bar
now uses `SelectTool.selected_objects(app)`. The fix is in the test (your
file), for example:

```python
selected = self.app.active_tool.selected_objects(self.app)
self.assertEqual([o["id"] for o in selected], [obj_id])
```

Nothing in the UI polish touches this; run `python scripts/p4_test_text.py`
after the fix (34 tests).

---

## 5. Phase 5: what changes around your widgets (P1 files only)

None of these edit your files. They are listed so you can explain what you see.

* **Contrast.** Every text colour on every background of both themes is at
  least 4.5:1, computed from the tokens and checked by `scripts/a_ui_smoke.py`.
  Fixed in light mode: `muted` #6B7280 to #5F6673 (was 4.43:1 on the window
  background, 4.23:1 on hover), `primary` #3B82F6 to #2563EB (white text on a
  primary button was 3.68:1, now 5.17:1), `primary_hover` and `primary_text`
  to #1D4ED8. Dark mode already passed. Lowest pair now: warning text on its
  soft background, 4.51:1.
* **Theme switch.** A colour your code writes into a widget's own style sheet
  from `theme.tokens()` (P2's command bar lines, P1's RTT label) used to keep
  the old mode's value after View > Toggle dark theme. `theme.retint_widget_styles`
  now swaps text-colour tokens (only `color:`, never backgrounds or user colours).
* **States.** One focus colour (`focus` token) for buttons, inputs, lists,
  tables, the slider and swatches; the primary button gets a 2 px ring. A
  checked tool that is disabled (viewer) is greyed instead of blue.
* **Keyboard.** The main window opens with the focus on the canvas. Tab goes
  top bar > tool rail > canvas > History/Latency tabs > History > Latency >
  Users > Chat > status chip; inside each panel your own order is kept. The
  dock scroll areas no longer take focus. Connect dialog: Tab no longer gets
  stuck on the Host/Join switch, and Left/Right switch the page.
* **Shortcuts.** 30 keys, no key bound twice. Every tool button's tooltip
  names its key. PageUp and PageDown are free.
* **Menus.** Long entries no longer run into their shortcut (View > "Collapse
  or expand the tool rail  Ctrl+1"). The locked docks' greyed-out entries
  (Tools, Chat, Users, ...) are hidden from View; Ctrl+1/2/3 replace them.
* **Sizes.** One font scale (9 / 10 / 12 / 16 pt, plus 22 and 30 pt on the
  host's "room ready" page) and two icon sizes: 20 px for icon-only buttons
  and 16 px next to text. The only exception is the alignment toggles in
  P2's format bar (buttons fixed at 26 x 24), which stay 16 px like the B/I/U
  letters. Every panel header and the bottom tab bar are 36 px; every panel
  has an 8 px inner margin and 8 px between its rows.
* **Removed dead styling** (theme.py): dock title-bar rules (every dock has
  its own header), splitter and progress-bar rules (unused), the duplicate
  FormatBar and status-bar rules, and the decoration of the hidden text tool
  bar. stats_panel.py: the old fixed GREEN / AMBER / RED / GREY / MSG_COLOR.

### Open items, for the owners to decide (not changed)

* **Harshith (P4), `client/main_window.py`, Manage roles:** names are drawn
  in the raw user colour, so in dark mode a blue name (#4363d8) is 3.1:1 on the
  dialog. Suggested one-line fix (adds `theme` to the client imports):

  ```diff
  -            name_label.setStyleSheet("font-weight: bold; color: %s;" % (user.get("color") or "#555555"))
  +            name_label.setStyleSheet("font-weight: bold; color: %s;" % theme.readable(
  +                user.get("color") or "#555555", theme.tokens()["surface"]))
  ```
* **Harshith (P4), `client/tools/text_tool.py`:** the inline text editor keeps
  its own style sheet on purpose: it sits on the white page, so it stays light
  in both themes.
* **Janith (P3), `scripts/p3_test_images.py`:**
  `test_painting_again_after_the_download_shows_the_picture` reads pixel
  (60, 35) of the placeholder. That pixel is on the anti-aliased "loading"
  caption, so it gets (234, 234, 234) instead of `PLACEHOLDER_FILL`
  (236, 236, 236) with this machine's font. It fails the same way on
  `origin/main`. Reading a pixel away from the caption (for example (20, 40),
  as the other placeholder tests do) fixes it.
* **Main window `padding: 0 6px` labels** (`main_window.py` status label, P4;
  `canvas.py` snap label, P2): harmless inline padding; can be left.

---

## 6. Windows re-test checklist (Janith)

On the Windows laptop, from the branch `sharwan/ui-polish` (after `git pull`):

1. **Build.** `scripts\build.bat` (python.org Python 3.10+, not Anaconda). It
   must end with the three .exe files in `dist\windows\` and print no
   missing-module warnings.
2. **Smoke test of the executables.**
   `.build_venv\Scripts\python scripts\a_exe_smoke.py --dist dist\windows`
   must print every check as PASS. It starts the server .exe on a spare port,
   creates a room over a raw socket, and starts both client .exe files.
3. **UI smoke from source.** `python scripts\a_ui_smoke.py` must print
   `UI SMOKE OK`. It also checks contrast, shortcuts, Tab order and dark mode.
   Open the screenshots in `scripts\a_ui_shots\`.
4. **Hotspot test by hand (two laptops, phone hotspot).**
   * Server laptop: run `a_open_firewall.ps1` once, start `WhiteboardServer.exe`,
     then `WhiteboardClient.exe` > Host a room. The "room ready" page shows the
     code and the hotspot IP in large text.
   * Second laptop: `WhiteboardClient.exe` > Join a room, type the IP and
     press Find rooms. The room appears in the list; double-click it, type a
     name, Connect.
   * Draw on both and check that each sees the other's strokes and cursor.
     Chat both ways. Save a version, then Restore it (host).
   * Host: Session > Manage roles, make the second laptop a viewer. It must
     show the yellow "View only" banner and greyed tools, and drawing must do
     nothing.
   * Turn the hotspot off for 10 s, then on again: "Reconnecting..." (amber),
     then "Connected", and the board is the same on both (Debug > Show board
     checksum).
5. **What to look at in the new UI (both themes: View > Toggle dark theme):**
   * the page is white with a soft shadow on a grey (light) or dark surround;
     there is no zoom bar in the top bar any more;
   * no white boxes in dark mode except the page, the pen preview and colour
     swatches; menus, tooltips, the command bar (`/`), Manage roles and the
     shortcuts help (F1) are dark;
   * Tab in the connect dialog goes through every field and does not get stuck
     on Host/Join; Left/Right switch Host and Join;
   * at 1366 x 768 nothing is cut off; Ctrl+1 / Ctrl+2 / Ctrl+3 fold the tool
     rail, the side panel and the bottom panel; all 12 tool keys work
     (P M H E L A R O V T N I);
   * at 150 % Windows scaling the text is sharp and nothing overlaps.

Report any problem with a screenshot and the theme it was in.
