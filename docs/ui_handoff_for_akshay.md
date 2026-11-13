<!-- OWNER: P1 -->
# UI handoff: changes in P2, P3 and P4 files

Written by P1 for the UI polish (branch `sharwan/ui-polish`). The owners
(Akshay P2, Janith P3, Harshith P4) approved these changes in the group chat;
they were applied in ONE commit, "[P1 Sharwan] apply approved UI handoff for
P2/P3/P4", so every changed line can be reviewed in the pull request.

Rules kept in every file:
* the `# OWNER:` line is unchanged;
* only colours, styling and widget arrangement change: no message types, no
  registries, no tool ids or labels, no panel names, no method signatures and
  no behaviour (every button, shortcut and signal does what it did before);
* colours come from `client/theme.py`: `theme.tokens()` gives the colours of
  the active light/dark mode, `theme.readable(color, bg)` adjusts a user colour
  to 4.5:1 contrast, and `theme.notifier.changed` fires after a theme switch.
  The style sheet rules for the object names set below live in theme.py.

The exact lines follow, as `git diff` (`-` before, `+` after).


---

## Akshay (P2)

### `client/canvas.py`

The grey around the page comes from the theme (`surround`: #E9ECEF light, the window background in dark). The page itself stays white, and exports never use this colour.

```diff
diff --git a/client/canvas.py b/client/canvas.py
index 2ba53b2..4f4b184 100644
--- a/client/canvas.py
+++ b/client/canvas.py
@@ -56,3 +56,3 @@ from PyQt5.QtWidgets import QLabel, QWidget
 
-from client import render
+from client import render, theme
 from common import config
@@ -65,3 +65,3 @@ ZOOM_STEP = 1.15
 # Colors of the background outside the page, the page and its grid.
-OUTSIDE_COLOR = QColor("#d6d6d6")
+OUTSIDE_COLOR = QColor("#d6d6d6")   # fallback; the surround comes from theme.tokens()["surround"]
 PAGE_COLOR = QColor("#ffffff")
@@ -388,3 +388,3 @@ class CanvasWidget(QWidget):
         try:
-            painter.fillRect(self.rect(), OUTSIDE_COLOR)
+            painter.fillRect(self.rect(), QColor(theme.tokens()["surround"]))   # follows light/dark
             self._paint_page_shadow(painter)      # in screen pixels, before the transform
```

### `client/shortcuts.py`

Command bar: the fixed #fafafa panel is replaced by the theme rule `#command_bar` (surface colour, border, 8 px radius); the hint, result and error lines take the theme's muted / success / danger text colours, readable in both modes.

```diff
diff --git a/client/shortcuts.py b/client/shortcuts.py
index a41dbc4..178fbd0 100644
--- a/client/shortcuts.py
+++ b/client/shortcuts.py
@@ -83,3 +83,3 @@ from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLa
 
-from client import ops, registry, toolbar
+from client import ops, registry, theme, toolbar
 from client.canvas import on_page
@@ -118,5 +118,5 @@ HISTORY_MAX = 100             # commands remembered for Up/Down
 HINT_ROWS = 6                 # at most this many matching commands in the hint list
-ERROR_STYLE = "QLabel { color: #c62828; }"
-INFO_STYLE = "QLabel { color: #2e7d32; }"
-HINT_STYLE = "QLabel { color: #555; }"
+ERROR_STYLE = "QLabel { color: %s; }"   # %s: theme token danger_text (readable in light and dark)
+INFO_STYLE = "QLabel { color: %s; }"    # %s: theme token success_text
+HINT_STYLE = "QLabel { color: %s; }"    # %s: theme token muted
 
@@ -611,5 +611,3 @@ class CommandBar(QFrame):
         self.setFrameShape(QFrame.StyledPanel)
-        self.setAutoFillBackground(True)
-        self.setStyleSheet("#command_bar { background: #fafafa; border: 1px solid #9e9e9e;"
-                           " border-radius: 4px; }")
+        self.setAutoFillBackground(True)   # background, border and radius: #command_bar in client/theme.py
 
@@ -620,3 +618,3 @@ class CommandBar(QFrame):
         self._hints.setTextFormat(Qt.RichText)
-        self._hints.setStyleSheet(HINT_STYLE)
+        self._hints.setStyleSheet(HINT_STYLE % theme.tokens()["muted"])
         layout.addWidget(self._hints)
@@ -712,3 +710,4 @@ class CommandBar(QFrame):
         """Print a result (green) or an error (red) above the line edit."""
-        self._msg.setStyleSheet(ERROR_STYLE if error else INFO_STYLE)
+        self._msg.setStyleSheet(ERROR_STYLE % theme.tokens()["danger_text"] if error
+                                else INFO_STYLE % theme.tokens()["success_text"])
         self._msg.setText(text)
```

### `client/toolbar.py`

Swatches keep only their fill colour inline; shape (round), outline and the ring on the selected swatch come from the theme (`#swatch`, `#inkSwatch`, `#currentSwatch`). The text colour button is 24x24 like the swatches. The width preview is a piece of the white page with a rounded theme border. The viewer hint and the info line use theme colours (`#viewerHint`, `muted`).

```diff
diff --git a/client/toolbar.py b/client/toolbar.py
index 3e0973f..4b5ee1b 100644
--- a/client/toolbar.py
+++ b/client/toolbar.py
@@ -75,3 +75,3 @@ from PyQt5.QtWidgets import (QButtonGroup, QColorDialog, QComboBox, QGridLayout,
 
-from client import ops, registry, render_text
+from client import ops, registry, render_text, theme
 from common import kinds_text, protocol
@@ -118,6 +118,9 @@ def _tool_tooltip(cls):
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
 
@@ -167,5 +170,6 @@ class WidthPreview(QWidget):
             painter.setRenderHint(QPainter.Antialiasing, True)
-            painter.fillRect(self.rect(), QColor("#f4f4f4"))
-            painter.setPen(QPen(QColor("#c8c8c8")))
-            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
+            # A small piece of the (always white) page, framed in the theme's border colour.
+            painter.setPen(QPen(QColor(theme.tokens()["border_strong"])))
+            painter.setBrush(QColor(theme.tokens()["page"]))
+            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
             d = min(self._width, self.SIDE - 4.0)
@@ -228,3 +232,4 @@ class FormatBar(QGroupBox):
         self._ink = QToolButton()
-        self._ink.setFixedSize(QSize(28, 24))
+        self._ink.setObjectName("inkSwatch")        # round, outlined: client/theme.py
+        self._ink.setFixedSize(QSize(24, 24))
         self._ink.setToolTip("Text color")
@@ -282,2 +287,3 @@ class FormatBar(QGroupBox):
             sw.setToolTip(color)
+            sw.setObjectName("swatch")
             sw.setStyleSheet(_swatch_style(color))
@@ -389,4 +395,3 @@ class FormatBar(QGroupBox):
         self._ink_color = values["ink"]             # where the color dialog starts
-        self._ink.setStyleSheet("QToolButton { background: %s; border: 1px solid #888;"
-                                " border-radius: 3px; }" % values["ink"])
+        self._ink.setStyleSheet("QToolButton { background: %s; }" % values["ink"])
         self._ink.setToolTip("Text color %s" % values["ink"])
@@ -493,4 +498,3 @@ class ToolDock(QWidget):
         self._hint.setWordWrap(True)
-        self._hint.setStyleSheet("QLabel { background: #fff3cd; color: #664d03;"
-                                 " border: 1px solid #ffe69c; border-radius: 4px; padding: 6px; }")
+        self._hint.setObjectName("viewerHint")      # warning colours: client/theme.py
         self._hint.hide()
@@ -685,2 +689,3 @@ class ToolDock(QWidget):
             sw.setToolTip(color)
+            sw.setObjectName("swatch")
             sw.setStyleSheet(_swatch_style(color))
@@ -695,2 +700,3 @@ class ToolDock(QWidget):
         self._current_swatch = QLabel()
+        self._current_swatch.setObjectName("currentSwatch")   # round, outlined: client/theme.py
         self._current_swatch.setFixedSize(24, 24)
@@ -724,3 +730,3 @@ class ToolDock(QWidget):
         self._info_label.setWordWrap(True)
-        self._info_label.setStyleSheet("QLabel { color: #555; }")
+        self._info_label.setProperty("muted", True)    # muted text colour: client/theme.py
         v.addWidget(self._info_label)
@@ -775,4 +781,3 @@ class ToolDock(QWidget):
                 self._swatch_group.setExclusive(True)
-        self._current_swatch.setStyleSheet("QLabel { background: %s; border: 1px solid #888;"
-                                           " border-radius: 3px; }" % color)
+        self._current_swatch.setStyleSheet("QLabel { background: %s; }" % color)
         self._current_swatch.setToolTip("Current color %s" % color)
```


---

## Janith (P3)

### `client/history_panel.py`

Buttons move from a tall column to one compact row above the list: Save version (text) | Restore, Rename, Delete | Undo, Redo (icon-only, with tooltips and accessible names; icons by objectName in theme.py). The Name column starts at 240 px instead of stretching over the whole width; rows are 32 px cards without grid lines; the Objects | seq | checksum line is a small muted footer. The same attributes (`save_btn`, `restore_btn`, ...) and slots are used, so `p3_test_history_panel.py` passes unchanged.

```diff
diff --git a/client/history_panel.py b/client/history_panel.py
index 829e418..52325d0 100644
--- a/client/history_panel.py
+++ b/client/history_panel.py
@@ -265,15 +265,26 @@ class HistoryPanel(QWidget):
         header = self.table.horizontalHeader()
-        header.setSectionResizeMode(0, QHeaderView.Stretch)
+        header.setSectionResizeMode(0, QHeaderView.Interactive)   # Name: fixed start width, draggable
+        header.resizeSection(0, 240)
         for col in (1, 2, 3):
             header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
+        header.setStretchLastSection(False)
+        self.table.setShowGrid(False)                              # card-style rows: client/theme.py
+        self.table.verticalHeader().setDefaultSectionSize(32)
 
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
@@ -284,13 +295,19 @@ class HistoryPanel(QWidget):
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
```


---

## Harshith (P4)

### `client/chat_panel.py`

Messages are bubbles: mine on the right in `primary_soft`, others on the left in `surface_alt`, at most 80 % wide; the time is muted and names are drawn in the sender's colour adjusted for contrast. This fixes the unreadable own messages in the dark theme (`OWN_BACKGROUND` was a fixed light blue under light text). Open conversations re-render when the theme changes, and an empty conversation shows a placeholder line.

```diff
diff --git a/client/chat_panel.py b/client/chat_panel.py
index cdf0167..1c7727a 100644
--- a/client/chat_panel.py
+++ b/client/chat_panel.py
@@ -43,2 +43,3 @@ from PyQt5.QtWidgets import (QPlainTextEdit, QTabWidget, QTextBrowser, QVBoxLayo
 from common import protocol
+from client import theme
 from client.registry import on_message, on_session_reset, register_panel
@@ -48,3 +49,4 @@ ROOM_KEY = "room"            # conversation key of the room; DMs use "dm:<name>"
 FALLBACK_COLOR = "#555555"   # name colour when the sender is not in app.users
-OWN_BACKGROUND = "#e8f0fe"   # highlight of my own messages
+OWN_BACKGROUND = "#e8f0fe"   # highlight of my own messages (light theme; see _line_html)
+EMPTY_TEXT = "No messages yet. Say hello to the room."   # shown while a conversation is empty
 BOTTOM_TOLERANCE = 4         # pixels: "at the bottom" if the scrollbar is this close
@@ -94,2 +96,3 @@ class ConversationView(QTextBrowser):
         self._note_count = 0  # source of ids for local notes
+        self.setPlaceholderText(EMPTY_TEXT if key == ROOM_KEY else "No messages yet.")
 
@@ -164,4 +167,5 @@ class ConversationView(QTextBrowser):
         """
+        t = theme.tokens()                      # colours of the active light/dark theme
         if msg.get("note"):
-            return ('<p style="color:#888888"><i>%s</i></p>' % html.escape(str(msg.get("text", ""))))
+            return ('<p style="color:%s"><i>%s</i></p>' % (t["muted"], html.escape(str(msg.get("text", "")))))
         sender = str(msg.get("from", "?"))
@@ -170,10 +174,14 @@ class ConversationView(QTextBrowser):
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
 
@@ -213,2 +221,5 @@ class ChatPanel(QWidget):
         self._update_input()
+        # Bubble colours come from the theme: re-render the open conversations when it changes.
+        theme.notifier.changed.connect(
+            lambda _mode: [view.render_all() for view in self._views.values()])
```

### `client/main_window.py`

`auto_arrange_right` (default True keeps today's behaviour): shell.py sets it to False so Users stays above Chat at every width, instead of emptying the private `_right_docks` list. The View-only banner gets the objectName `viewOnlyBanner` (theme colours) instead of a fixed yellow.

```diff
diff --git a/client/main_window.py b/client/main_window.py
index 4d4b872..9e5beb6 100644
--- a/client/main_window.py
+++ b/client/main_window.py
@@ -89,3 +89,3 @@ class ViewOnlyBanner(QLabel):
         self.setAlignment(Qt.AlignCenter)
-        self.setStyleSheet("background-color: #ffeb3b; color: #333333; font-weight: bold;")
+        self.setObjectName("viewOnlyBanner")   # colours come from client/theme.py
         self.setAttribute(Qt.WA_TransparentForMouseEvents)
@@ -243,2 +243,5 @@ class MainWindow(QMainWindow):
 
+    # False: never tab or split the right-hand docks automatically (client/shell.py arranges them).
+    auto_arrange_right = True
+
     # Emitted (GUI thread) with the tool id whenever the active tool changes,
@@ -395,2 +398,4 @@ class MainWindow(QMainWindow):
         """
+        if not self.auto_arrange_right:
+            return
         narrow = self.width() < NARROW_WIDTH
```


---

## Phase 4 (approved for styling)

### Janith (P3): `client/history_panel.py`, part B

The version list fills the whole width again (Name stretches; By, Time and Objects
fit their contents), and an empty list shows a muted, centred line: "No versions yet.
Save one to keep a snapshot." The line is a label over the table's viewport that
ignores the mouse, so right-click and selection on the table work as before; it is
shown only while the table has no rows. Nothing else changes.

```diff
diff --git a/client/history_panel.py b/client/history_panel.py
index 52325d0..ca5fdf3 100644
--- a/client/history_panel.py
+++ b/client/history_panel.py
@@ -52,2 +52,3 @@ AUTO_NAME_RESTORE = "before restore "   # the names the server gives to its auto
 AUTO_NAME_CLEAR = "before clear"
+EMPTY_TEXT = "No versions yet. Save one to keep a snapshot."   # shown while the list is empty
 
@@ -265,9 +266,12 @@ class HistoryPanel(QWidget):
         header = self.table.horizontalHeader()
-        header.setSectionResizeMode(0, QHeaderView.Interactive)   # Name: fixed start width, draggable
-        header.resizeSection(0, 240)
+        header.setSectionResizeMode(0, QHeaderView.Stretch)       # Name fills the width
         for col in (1, 2, 3):
             header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
-        header.setStretchLastSection(False)
         self.table.setShowGrid(False)                              # card-style rows: client/theme.py
         self.table.verticalHeader().setDefaultSectionSize(32)
+        # Shown over the empty list (muted, centred; clicks go through to the table).
+        self.empty_label = QLabel(EMPTY_TEXT, self.table.viewport())
+        self.empty_label.setAlignment(Qt.AlignCenter)
+        self.empty_label.setProperty("muted", True)
+        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
 
@@ -360,4 +364,15 @@ class HistoryPanel(QWidget):
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
```

### Akshay (P2): `client/canvas.py`, part D (infinite-canvas look)

Approved by Akshay for styling. Not changed: the board model, board units, the
4000 x 3000 board, the protocol, server validation, export, snapshots, snapping
and the grid size. The page rule (clip to the page, clamp every point, no press
outside the page, "not allowed" cursor past the edge) is unchanged.

What changes on screen:
* the whole view is the board's white background (as in an export); the grey
  surround, the page border and the page shadow are gone (the shadow method and
  its constants are removed, and canvas.py no longer imports theme);
* a subtle dot grid covers the view and moves with pan and zoom; spacing is
  grid_size, doubled until dots are at least 16 px apart. The dots are drawn
  into a cached pixmap that is rebuilt only when spacing, pan phase or size
  change, so a repaint while drawing costs about 0.6 ms at 1920 x 1080 (before:
  0.7 to 2.8 ms with the shadow and border);
* panning (pan_by, set_zoom, resizes) is clamped: at most 160 px or a quarter of
  the view (whichever is more) past any page edge, so the drawing cannot be lost;
* new public method fit_content(): zoom and centre on everything drawn (the whole
  page on an empty board), used by the Fit button in the top bar. fit_page() and
  every existing method keep their signature and behaviour.

Exports were compared before and after on a board with every kind of object
(one at the page corner): the PNG (4084 x 3050) and the PDF page are pixel-identical.

```diff
diff --git a/client/canvas.py b/client/canvas.py
index 4f4b184..fd38054 100644
--- a/client/canvas.py
+++ b/client/canvas.py
@@ -47,2 +47,11 @@ The server enforces the same rule on what it accepts (common/kinds_*.py),
 because a client can never be trusted to have clamped anything.
+
+The look (infinite canvas)
+--------------------------
+On screen the board has no frame: the whole view is the board's white
+background (the same white an export has), with a subtle dot grid that
+moves with pan and zoom. Panning is free but clamped to the page plus a
+margin, so the drawing can never be lost; fit_page() and fit_content()
+bring it back into view. The page rule above is unchanged: past the page
+edge the pointer shows "not allowed" and nothing can be drawn there.
 """
@@ -52,7 +61,7 @@ import traceback
 
-from PyQt5.QtCore import QLineF, QPointF, QRectF, QSizeF, Qt, pyqtSignal
-from PyQt5.QtGui import QColor, QCursor, QPainter, QPen
+from PyQt5.QtCore import QLineF, QPointF, QRectF, Qt, pyqtSignal
+from PyQt5.QtGui import QColor, QCursor, QPainter, QPen, QPixmap, QPolygonF
 from PyQt5.QtWidgets import QLabel, QWidget
 
-from client import render, theme
+from client import render
 from common import config
@@ -64,7 +73,19 @@ ZOOM_STEP = 1.15
 
-# Colors of the background outside the page, the page and its grid.
-OUTSIDE_COLOR = QColor("#d6d6d6")   # fallback; the surround comes from theme.tokens()["surround"]
+# Colors of the board background (the whole view, like an export), its line grid and the dots.
 PAGE_COLOR = QColor("#ffffff")
-PAGE_BORDER_COLOR = QColor("#616161")
 GRID_COLOR = QColor("#ececec")
+DOT_COLOR = QColor("#cfd4dc")
+
+# Dot grid: one dot every grid_size board units, doubled until the dots are at
+# least DOT_MIN_PX screen pixels apart, so it never gets dense when zoomed out.
+DOT_MIN_PX = 16
+DOT_PX = 1.6              # dot diameter in screen pixels
+
+# Panning keeps the view on the page: it may go at most PAN_MARGIN_PX past an
+# edge (or PAN_MARGIN_FRAC of the view, if that is more).
+PAN_MARGIN_PX = 160
+PAN_MARGIN_FRAC = 0.25
+
+# fit_content() never zooms in further than this on a small drawing.
+FIT_CONTENT_MAX_ZOOM = 2.0
 
@@ -81,13 +102,2 @@ FIT_MARGIN = 20
 
-# Soft shadow under the page: SHADOW_STEPS translucent rectangles, each
-# SHADOW_GROW pixels bigger than the previous one, shifted by SHADOW_SHIFT
-# (right, down), so the edge fades out. Alpha is per rectangle and adds up.
-SHADOW_STEPS = 7
-SHADOW_GROW = 1.6
-SHADOW_SHIFT = (2.0, 3.0)
-SHADOW_ALPHA = 13
-
-# Width (pixels) of the page border line.
-BORDER_PX = 2
-
 
@@ -241,2 +251,4 @@ class CanvasWidget(QWidget):
         self._fitted = False         # fit_page() already ran once on first show
+        self._dots_key = None        # (spacing, phase, size, ratio) of the cached dot layer
+        self._dots_pix = None        # the dot grid as a transparent pixmap of the view
 
@@ -322,2 +334,3 @@ class CanvasWidget(QWidget):
                               anchor.y() - before.y() * zoom)
+        self._clamp_offset()
         self.update()
@@ -325,4 +338,42 @@ class CanvasWidget(QWidget):
     def pan_by(self, dx, dy):
-        """Move the view by (dx, dy) screen pixels."""
+        """Move the view by (dx, dy) screen pixels (kept on the page, see _clamp_offset)."""
         self.offset = QPointF(self.offset.x() + dx, self.offset.y() + dy)
+        self._clamp_offset()
+        self.update()
+
+    def _clamp_offset(self):
+        """Keep the page in view: at most a margin of empty space past any page edge.
+
+        Per axis, with the page b screen pixels long and the view v long, the
+        page's start may lie between v - margin - b and margin. When the page
+        is smaller than the view (zoomed out) the same formula keeps it fully
+        inside, `margin` away from the edges at most.
+        """
+        out = []
+        for value, view, size in ((self.offset.x(), self.width(), config.BOARD_W),
+                                  (self.offset.y(), self.height(), config.BOARD_H)):
+            margin = max(PAN_MARGIN_PX, PAN_MARGIN_FRAC * view)
+            a, b = view - margin - size * self.zoom, margin
+            out.append(max(min(a, b), min(max(a, b), value)))
+        self.offset = QPointF(out[0], out[1])
+
+    def fit_content(self):
+        """Zoom and centre on everything drawn (the whole page when the board is empty)."""
+        board = getattr(self.app, "board", None)
+        rect = None
+        for obj in (board.objects.values() if board is not None else ()):
+            bounds = object_bounds(obj)
+            if bounds is not None:
+                rect = bounds if rect is None else rect.united(bounds)
+        if rect is None or rect.isEmpty():
+            self.fit_page()
+            return
+        rect = rect.adjusted(-CULL_MARGIN, -CULL_MARGIN, CULL_MARGIN, CULL_MARGIN)
+        w = max(1, self.width() - 2 * FIT_MARGIN)
+        h = max(1, self.height() - 2 * FIT_MARGIN)
+        self.zoom = max(MIN_ZOOM, min(FIT_CONTENT_MAX_ZOOM, w / rect.width(), h / rect.height()))
+        centre = rect.center()
+        self.offset = QPointF(self.width() / 2.0 - centre.x() * self.zoom,
+                              self.height() / 2.0 - centre.y() * self.zoom)
+        self._clamp_offset()
         self.update()
@@ -377,5 +428,10 @@ class CanvasWidget(QWidget):
 
+    def resizeEvent(self, ev):
+        """Keep the page in view when the canvas changes size (panels shown or hidden)."""
+        super().resizeEvent(ev)
+        self._clamp_offset()
+
     # --------------------------------------------------------------- painting
     def paintEvent(self, ev):
-        """Paint the grey surround, the white page, the grid, then the board objects.
+        """Paint the white board background and its dot grid, the line grid, then the board objects.
 
@@ -388,4 +444,5 @@ class CanvasWidget(QWidget):
         try:
-            painter.fillRect(self.rect(), QColor(theme.tokens()["surround"]))   # follows light/dark
-            self._paint_page_shadow(painter)      # in screen pixels, before the transform
+            # The whole view is the board's background: white, like an export.
+            painter.fillRect(self.rect(), PAGE_COLOR)
+            self._paint_dots(painter)             # in screen pixels, before the transform
 
@@ -397,3 +454,2 @@ class CanvasWidget(QWidget):
             visible = self.visible_board_rect()
-            painter.fillRect(page, PAGE_COLOR)
             if self.show_grid:
@@ -412,9 +468,2 @@ class CanvasWidget(QWidget):
 
-            # The border goes on top of the objects so ink at the edge never hides it.
-            border = QPen(PAGE_BORDER_COLOR)
-            border.setWidth(BORDER_PX)
-            border.setCosmetic(True)  # BORDER_PX screen pixels whatever the zoom
-            painter.setPen(border)
-            painter.setBrush(Qt.NoBrush)
-            painter.drawRect(page)
             self._paint_tool_overlay(painter)
@@ -423,17 +472,42 @@ class CanvasWidget(QWidget):
 
-    def _paint_page_shadow(self, painter):
-        """Draw a soft drop shadow around the page, in screen pixels (painter not transformed yet).
+    def _paint_dots(self, painter):
+        """Draw the dot grid over the whole view, in screen pixels (painter not transformed yet).
 
-        Several nearly transparent rectangles of growing size are stacked;
-        where they overlap the shadow is darkest (next to the page) and it
-        fades to nothing outwards. The page is painted over the middle.
+        Dots sit on multiples of the grid spacing in board units, so they move
+        with pan and zoom; the spacing doubles while dots would be closer than
+        DOT_MIN_PX. The dots are drawn once into a pixmap of the view and that
+        pixmap is reused until the spacing, the pan phase or the size changes,
+        so the many repaints while drawing only copy it (about half a millisecond).
         """
-        top_left = self.board_to_screen(QPointF(0, 0))
-        base = QRectF(top_left, QSizeF(config.BOARD_W * self.zoom, config.BOARD_H * self.zoom))
-        base.translate(SHADOW_SHIFT[0], SHADOW_SHIFT[1])
-        painter.setPen(Qt.NoPen)
-        painter.setBrush(QColor(0, 0, 0, SHADOW_ALPHA))
-        for i in range(1, SHADOW_STEPS + 1):
-            grow = i * SHADOW_GROW
-            painter.drawRect(base.adjusted(-grow, -grow, grow, grow))
+        step = self.grid_size if self.grid_size > 0 else float(config.GRID)
+        while step * self.zoom < DOT_MIN_PX:
+            step *= 2
+        px = step * self.zoom
+        first = self.screen_to_board(QPointF(0, 0))
+        x0 = math.floor(first.x() / step) * step * self.zoom + self.offset.x()
+        y0 = math.floor(first.y() / step) * step * self.zoom + self.offset.y()
+        ratio = self.devicePixelRatioF()
+        key = (round(px, 3), round(x0, 1), round(y0, 1), self.width(), self.height(), ratio)
+        if key != self._dots_key:
+            pix = QPixmap(max(1, int(self.width() * ratio)), max(1, int(self.height() * ratio)))
+            pix.setDevicePixelRatio(ratio)
+            pix.fill(Qt.transparent)
+            cols = int(self.width() / px) + 2
+            rows = int(self.height() / px) + 2
+            row = QPolygonF([QPointF(x0 + i * px, 0.0) for i in range(cols)])
+            pen = QPen(DOT_COLOR, DOT_PX)
+            pen.setCapStyle(Qt.RoundCap)
+            dots = QPainter(pix)
+            try:
+                dots.setRenderHint(QPainter.Antialiasing, True)
+                dots.setPen(pen)
+                for j in range(rows):         # one row of points, moved down row by row
+                    dots.save()
+                    dots.translate(0.0, y0 + j * px)
+                    dots.drawPoints(row)
+                    dots.restore()
+            finally:
+                dots.end()
+            self._dots_key, self._dots_pix = key, pix
+        painter.drawPixmap(0, 0, self._dots_pix)
 
@@ -546,3 +620,3 @@ class CanvasWidget(QWidget):
     def _update_page_cursor(self, ev):
-        """Show the "not allowed" cursor while a drawing tool is over the grey area outside the page.
+        """Show the "not allowed" cursor while a drawing tool is past the edge of the page.
```

---

## Not changed, for the owners to decide

* `scripts/p4_test_text.py` (P4): two format-bar tests read `SelectTool.selected_id`,
  which went away with multi-select (PR #37); they fail the same way on
  `origin/main`. `SelectTool.selected_objects(app)` is what the format bar uses now.
* `client/tools/text_tool.py` (P4): the inline text editor keeps its own style
  sheet on purpose: it sits on the white page, so it stays light in both themes.
