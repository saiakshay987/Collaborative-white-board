<!-- OWNER: P1 -->
# UI handoff notes (optional suggestions for other owners)

Written by P1 during the UI polish (branch `sharwan/ui-polish`). **Nothing here
is applied**: each owner decides whether to take a change into their own file.
Every snippet is small and keeps the public API (message types, registries,
tool ids, labels, panel names, method signatures) exactly as it is. The theme
lives in `client/theme.py`: `theme.tokens()` returns the colours of the active
mode and `theme.notifier.changed` (a Qt signal) fires after every theme switch.

The notes for Akshay (P2) are completed at the end of the polish (phase 5);
the notes below are the ones found so far.

---

## For Harshith (P4)

### 1. Own chat messages are unreadable in the dark theme (`client/chat_panel.py`)

`OWN_BACKGROUND = "#e8f0fe"` is a light blue. In the dark theme the message text
is light (it comes from the palette), so my own messages are light on light.
QSS cannot reach colours written into the message HTML.

The one-line change, line 176 in `ConversationView._line_html`:

```python
# before
        background = ' bgcolor="%s"' % OWN_BACKGROUND if mine else ""
# after
        background = ' bgcolor="%s"' % theme.tokens()["primary_soft"] if mine else ""
```

It needs this import next to the other `from client ...` imports (line 44):

```python
from client import theme
```

Optional, so messages already on screen change colour at once when the theme is
switched (otherwise they change at the next re-render), at the end of
`ChatPanel.__init__` (after `self._update_input()`, line 213):

```python
        theme.notifier.changed.connect(
            lambda _mode: [view.render_all() for view in self._views.values()])
```

`primary_soft` is `#E8F0FE` in light (the same colour as today) and `#22324D` in
dark, so the light theme looks exactly as it does now.

### 2. Let the shell keep Users above Chat (`client/main_window.py`)

`MainWindow._update_right_docks` tabs or splits the right-hand docks whenever the
window crosses `NARROW_WIDTH`. The UI polish keeps Users above Chat at every width
(the lab rubric wants both visible), so `client/shell.py` currently empties the
private list `window._right_docks` once, which makes the method return early.
A cleaner switch, in `MainWindow`:

```python
class MainWindow(QMainWindow):
    """The application window, built from the registries."""

    # False: never tab or split the right-hand docks automatically (client/shell.py arranges them).
    auto_arrange_right = True
```

and as the first line of `_update_right_docks` (line 396, before `narrow = ...`):

```python
        if not self.auto_arrange_right:
            return
```

shell.py would then set `window.auto_arrange_right = False` instead of touching
`_right_docks`.

### 3. View-only banner colour (`client/main_window.py`)

The banner sets a fixed yellow (line 90). The shell clears it and gives it the
objectName `viewOnlyBanner`, which the theme styles. Doing it in place:

```python
# before
        self.setStyleSheet("background-color: #ffeb3b; color: #333333; font-weight: bold;")
# after
        self.setObjectName("viewOnlyBanner")   # colours come from client/theme.py
```

### 4. `scripts/p4_test_text.py` fails on main

Two format-bar tests read `SelectTool.selected_id`, which went away with the
multi-select change (PR #37). They fail the same way on `origin/main`, with or
without the UI polish. `SelectTool.selected_objects(app)` is what the format
bar itself uses now.
