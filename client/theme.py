# OWNER: P1
"""theme.py: The client's look: colour tokens, the Qt style sheet, light/dark switching
and a decorate pass that gives existing widgets icons and style hooks.

Design: the structure of a diagram editor (light chrome, thin 1 px borders,
a tool rail on the left, panels on the right, slim status bar) with a
friendly touch (8 / 12 px radii, pill badges, round avatars). Every colour
is defined ONCE, in TOKENS; the style sheet, the palette and the painted
icons all read it, so light and dark can never drift apart.

The white board page itself is NOT themed: it is what PNG/PDF export
produces, so it stays white in both modes (P2's canvas paints it).

Public API
    TOKENS                         {"light": {...}, "dark": {...}} colour tokens
    current_mode() / tokens()      the active mode and its token dict
    build_stylesheet(mode)         the whole application QSS for a mode
    build_palette(mode)            the matching QPalette (painted widgets use it)
    apply_theme(app, mode=None, save=True)
                                   font + Fusion style + palette + QSS, remembered
                                   in QSettings under ui/theme
    toggle_theme(app=None)         switch light <-> dark (View > Toggle dark theme)
    decorate_widgets(root)         icons, tooltips and objectNames for existing widgets
    notifier.changed(str)          Qt signal emitted after every theme change
    set_pill(label, kind)          make a QLabel a pill badge (neutral, primary,
                                   success, warning, danger)
    retint_widget_styles(app, old, new)
                                   after a switch, text-colour tokens written into
                                   a widget's own style sheet follow the new mode
    contrast_ratio(fg, bg)         WCAG ratio; every text token pair is >= 4.5:1
                                   (checked by scripts/ui_smoke_test.py)

Sizes: one font scale (SMALL_PT 9, BODY_PT 10, TITLE_PT 12, DISPLAY_PT 16 and,
on the host's "room ready" page only, HEADLINE_PT 22 and HERO_PT 30) and two
icon sizes (ICON_SIZE 20 for icon-only buttons, ICON_TEXT 16 next to text).

Performance: the style sheet is applied only by apply_theme (start-up and
the menu toggle), never while drawing, and nothing here touches the canvas.
Everything runs on the GUI thread.
"""

import re
from string import Template

from PyQt5.QtCore import QObject, QSettings, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt5.QtWidgets import (QApplication, QDockWidget, QGroupBox, QPushButton, QToolBar,
                             QToolButton)

from client import icons, registry

# --- tokens ------------------------------------------------------------------------

# Every colour of the UI. Keys are the same in both modes; QSS uses them as $name.
TOKENS = {
    "light": {
        "bg": "#F4F5F7",            # window background around panels
        "surface": "#FFFFFF",       # panels, menus, dialogs
        "surface_alt": "#F8F9FB",   # table headers, cards inside panels
        "hover": "#EEF0F3",
        "pressed": "#E3E6EA",
        "border": "#E3E6EA",
        "border_strong": "#D5D9DF",  # input and button outlines
        "text": "#1F2430",
        "text_soft": "#4B5563",     # icons, secondary text that must stay readable
        "muted": "#5F6673",          # 4.5:1 or more on bg, surface, surface_alt and hover
        "disabled_text": "#A0A6B1",
        "disabled_bg": "#F1F2F4",
        "primary": "#2563EB",       # white text on it: 5.2:1
        "primary_hover": "#1D4ED8",
        "primary_soft": "#E8F0FE",
        "primary_text": "#1D4ED8",  # primary used as text (stronger, for contrast)
        "focus": "#1D4ED8",         # keyboard focus ring
        "on_primary": "#FFFFFF",
        "success": "#22C55E",
        "success_soft": "#DCFCE7",
        "success_text": "#15803D",
        "warning": "#F59E0B",
        "warning_soft": "#FEF3C7",
        "warning_text": "#B45309",
        "danger": "#EF4444",
        "danger_soft": "#FEE2E2",
        "danger_text": "#B91C1C",
        "input_bg": "#FFFFFF",
        "scroll": "#C9CED6",
        "tooltip_bg": "#1F2430",
        "tooltip_text": "#FFFFFF",
        "surround": "#E9ECEF",      # canvas around the page (P2 canvas.py reads it)
        "page": "#FFFFFF",          # the board page: white in both modes
    },
    "dark": {
        "bg": "#14161A",
        "surface": "#1D2026",
        "surface_alt": "#23272E",
        "hover": "#2A2F37",
        "pressed": "#323843",
        "border": "#2C313A",
        "border_strong": "#3A404B",
        "text": "#E6E8EC",
        "text_soft": "#C3C9D3",
        "muted": "#9AA3B2",
        "disabled_text": "#5C6370",
        "disabled_bg": "#1A1D22",
        "primary": "#60A5FA",
        "primary_hover": "#93C5FD",
        "primary_soft": "#22324D",
        "primary_text": "#93C5FD",
        "focus": "#93C5FD",
        "on_primary": "#0B1220",
        "success": "#4ADE80",
        "success_soft": "#173326",
        "success_text": "#86EFAC",
        "warning": "#FBBF24",
        "warning_soft": "#3A2E12",
        "warning_text": "#FCD34D",
        "danger": "#F87171",
        "danger_soft": "#3F1D1D",
        "danger_text": "#FCA5A5",
        "input_bg": "#181B20",
        "scroll": "#3A404B",
        "tooltip_bg": "#2A2F37",
        "tooltip_text": "#E6E8EC",
        "surround": "#14161A",      # = bg: the white page floats on the dark window
        "page": "#FFFFFF",
    },
}

MODES = ("light", "dark")

# Shape and type scale (8 px grid).
RADIUS_CONTROL = 8
RADIUS_CARD = 12
FONT_STACK = ("Inter", "Segoe UI", "Ubuntu", "Cantarell", "Arial")
# The one font scale of the UI (points). Every size in the style sheet and in
# P1's widgets is one of these; nothing else sets a point size.
SMALL_PT = 9        # pills, hints, section titles of the tool rail, graph labels
BODY_PT = 10        # everything else
TITLE_PT = 12       # brand name, metric values
DISPLAY_PT = 16     # connect dialog title
HEADLINE_PT = 22    # the host's IP on the "room ready" page (read out to the lab)
HERO_PT = 30        # the room code on the "room ready" page
# Icon sizes: ICON_SIZE for every icon-only button (tool rail, top bar, panel
# headers, history buttons, send); ICON_TEXT for an icon next to text (push
# buttons with a label, menu entries, pills, dock titles, segments).
ICON_SIZE = 20
ICON_TEXT = 16

# QSettings: same organisation/application as the rest of the client.
SETTINGS_ORG = "CN Lab"
SETTINGS_APP = "LAN Whiteboard"
THEME_KEY = "ui/theme"

# Tool id -> icon name (tool ids and labels come from registry.TOOLS; never changed here).
TOOL_ICONS = {
    "pen": "pen", "marker": "marker", "highlighter": "highlighter", "eraser": "eraser",
    "line": "line", "arrow": "arrow", "rect": "rectangle", "ellipse": "ellipse",
    "select": "select", "text": "text", "sticky": "sticky", "image": "image",
}

# Push buttons (by their text) that get an icon; the second value marks a destructive action.
BUTTON_ICONS = {
    "Undo": ("undo", False), "Redo": ("redo", False), "Rename": ("rename", False),
    "Delete": ("trash", True), "Restore": ("history", False), "Save version": ("save", False),
    "Copy stats": ("copy", False), "Copy code and IP": ("copy", False),
    "Test connection": ("plug", False), "Find rooms": ("zoom_in", False),
}

# Buttons found by objectName (their owners set it) -> (icon, destructive).
NAMED_BUTTON_ICONS = {
    "historySave": ("save", False), "historyRestore": ("history", False),
    "historyRename": ("rename", False), "historyDelete": ("trash", True),
    "historyUndo": ("undo", False), "historyRedo": ("redo", False),
}

# Menu entries (by their text) that get an icon.
MENU_ICONS = {
    "Copy room code": "copy", "Disconnect": "disconnect", "Manage roles": "crown",
    "Grid and snap on/off": "grid", "Export PNG": "image", "Export PDF": "save",
    "Save version": "save", "Image...": "image", "Save board as JSON": "save",
}

# Dock objectName -> icon (shown on dock tabs when docks are tabbed together).
DOCK_ICONS = {
    "dock_tools": "pen", "dock_users": "users", "dock_chat": "chat",
    "dock_history": "history", "dock_stats": "activity",
}

# Text of the format bar's toggle buttons (bold/italic/underline, alignment).
FORMAT_TOGGLES = {"B": None, "I": None, "U": None,
                  "L": "align_left", "C": "align_center", "R": "align_right"}

THEME_MENU_LABEL = "Toggle dark theme"

# Height of every panel header (DockHeader in shell.py) and of the bottom tab bar
# (the tab's 2 px underline is part of it), so all panels line up.
TAB_BAR_H = 36

_mode = "light"


class _Notifier(QObject):
    """Carries the theme-changed signal (a plain QObject, safe to create at import)."""

    changed = pyqtSignal(str)


notifier = _Notifier()


def current_mode():
    """Return the active mode, 'light' or 'dark'."""
    return _mode


def tokens(mode=None):
    """Return the token dict of mode (default: the active mode)."""
    return TOKENS[mode or _mode]


def color(name, mode=None):
    """Return token `name` of mode (default: active) as a QColor."""
    return QColor(tokens(mode)[name])


def readable(color, bg, minimum=4.5):
    """Return color ('#rrggbb') lightened or darkened until it reaches `minimum` contrast on bg.

    Used for text drawn in a user's colour (chat names) so it stays readable
    in both themes; the hue is kept, only the lightness moves.
    """
    c, back = QColor(color), QColor(bg)
    if not c.isValid():
        return tokens()["text"]
    lighten = back.lightness() < 128
    for _ in range(20):
        if contrast_ratio(c, back) >= minimum:
            break
        h, s, l, a = c.getHslF()
        l = min(1.0, l + 0.06) if lighten else max(0.0, l - 0.06)
        c = QColor.fromHslF(max(h, 0.0), s, l, a)
    return c.name()


def contrast_ratio(fg, bg):
    """WCAG contrast ratio of two colours (1.0 to 21.0); text needs 4.5 or more."""
    def lum(c):
        """Relative luminance of a QColor."""
        def chan(v):
            """Linearise one sRGB channel (0-255)."""
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        c = QColor(c)
        return 0.2126 * chan(c.red()) + 0.7152 * chan(c.green()) + 0.0722 * chan(c.blue())
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# --- font ----------------------------------------------------------------------------

def pick_font_family():
    """Return the first family of FONT_STACK that is installed (needs a QApplication)."""
    installed = {name.lower(): name for name in QFontDatabase().families()}
    for name in FONT_STACK:
        if name.lower() in installed:
            return installed[name.lower()]
    return QApplication.font().family()


# --- style sheet ---------------------------------------------------------------------

_QSS = Template("""
/* ---- windows and dialogs ---- */
QMainWindow { background: $bg; }
QMainWindow::separator { background: $bg; width: 3px; height: 3px; }
QMainWindow::separator:hover { background: $primary_soft; }
QDialog { background: $surface; }

QToolTip {
    background: $tooltip_bg; color: $tooltip_text; border: 1px solid $border_strong;
    border-radius: 6px; padding: 4px 8px;
}

/* ---- menu bar and menus ---- */
QMenuBar { background: $surface; border-bottom: 1px solid $border; padding: 2px 6px; }
QMenuBar::item { background: transparent; padding: 4px 10px; border-radius: 6px; }
QMenuBar::item:selected { background: $hover; }
QMenuBar::item:pressed { background: $primary_soft; color: $primary_text; }
QMenu {
    background: $surface; border: 1px solid $border; border-radius: ${rc}px; padding: 4px;
}
/* No padding / radius on QMenu::item: with a box rule Qt leaves the icon column out of
   the menu width and long entries run into their shortcut ("tool rail" / "Ctrl+1"). */
QMenu::item:selected { background: $primary_soft; color: $primary_text; }
QMenu::item:disabled { color: $disabled_text; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }

/* ---- tool bars ---- */
QToolBar { background: $surface; border: none; border-bottom: 1px solid $border; padding: 4px 6px; spacing: 4px; }
QToolBar::separator { background: $border; width: 1px; margin: 4px 6px; }
QToolBar QToolButton { padding: 4px; }

/* ---- tabs (chat conversations, tabbed docks, connect dialog) ---- */
QTabWidget::pane { border: 1px solid $border; border-radius: ${rc}px; background: $surface; top: -1px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent; color: $muted; padding: 6px 12px; margin-right: 2px;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: $text; }
QTabBar::tab:selected { color: $primary_text; border-bottom: 2px solid $primary; }
QTabBar::tab:focus { color: $text; background: $hover; }
QTabBar::tab:disabled { color: $disabled_text; }
QTabBar::close-button { image: url($close_icon); subcontrol-position: right; border-radius: 4px; }
QTabBar::close-button:hover { background: $hover; }

/* ---- push buttons: default, primary, flat, checkable, danger ---- */
QPushButton {
    background: $surface; color: $text; border: 1px solid $border_strong;
    border-radius: ${rc}px; padding: 6px 14px; min-height: 18px;
}
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $pressed; }
QPushButton:focus { border: 1px solid $focus; }
QPushButton:checked { background: $primary_soft; color: $primary_text; border-color: $primary; }
QPushButton:disabled, QPushButton:checked:disabled {
    background: $disabled_bg; color: $disabled_text; border-color: $border;
}
QPushButton[primary="true"] {
    background: $primary; color: $on_primary; border-color: $primary; font-weight: 600;
}
QPushButton[primary="true"]:hover {
    background: $primary_hover; border-color: $primary_hover;
}
QPushButton[primary="true"]:pressed { background: $primary_hover; border-color: $primary_hover; }
QPushButton[primary="true"]:focus { border: 2px solid $text; }
QPushButton[primary="true"]:disabled {
    background: $disabled_bg; color: $disabled_text; border-color: $border;
}
QPushButton[danger="true"] { color: $danger_text; }
QPushButton[danger="true"]:hover { background: $danger_soft; border-color: $danger; }
QPushButton[danger="true"]:disabled { color: $disabled_text; background: $disabled_bg; }
QPushButton[flat="true"] { background: transparent; border-color: transparent; }
QPushButton[flat="true"]:hover { background: $hover; }
QPushButton[flat="true"]:pressed { background: $pressed; }
QPushButton[flat="true"]:focus { border-color: $focus; }

/* ---- tool buttons (tool rail, format toggles, small actions) ---- */
QToolButton {
    background: transparent; color: $text; border: 1px solid transparent;
    border-radius: ${rc}px; padding: 4px;
}
QToolButton:hover { background: $hover; }
QToolButton:pressed { background: $pressed; }
QToolButton:checked { background: $primary_soft; color: $primary_text; }
QToolButton:focus { border: 1px solid $focus; }
QToolButton:disabled { color: $disabled_text; }
QToolButton:checked:disabled { background: $disabled_bg; color: $disabled_text; }
QToolButton#toolButton { padding: 6px 8px; text-align: left; }
QToolButton#toolButton:checked { font-weight: 600; }
QToolButton#formatToggle { border: 1px solid $border_strong; padding: 2px; }
QToolButton[railCompact="true"] { padding: 0px; }
QToolButton#formatToggle:checked { border-color: $primary; }

/* ---- text inputs, spin boxes, combo boxes ---- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: $input_bg; color: $text; border: 1px solid $border_strong;
    border-radius: ${rc}px; padding: 5px 8px;
    selection-background-color: $primary; selection-color: $on_primary;
}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QSpinBox:hover, QComboBox:hover {
    border-color: $muted;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: $focus;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled,
QComboBox:disabled {
    background: $disabled_bg; color: $disabled_text; border-color: $border;
}
QLineEdit[error="true"] { border-color: $danger; }
QTextBrowser { background: $surface; border: 1px solid $border; border-radius: ${rc}px; padding: 4px; }
QSpinBox { padding-right: 20px; }
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 18px; border: none; background: transparent;
}
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: ${rc}px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: ${rc}px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: $hover; }
QSpinBox::up-arrow { image: url($chev_up); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url($chev_down); width: 10px; height: 10px; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; }
QComboBox::down-arrow { image: url($chev_down); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: $surface; color: $text; border: 1px solid $border; padding: 4px; outline: 0;
    selection-background-color: $primary_soft; selection-color: $primary_text;
}

/* ---- slider ---- */
QSlider::groove:horizontal { height: 4px; background: $border_strong; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $primary; border-radius: 2px; }
QSlider::handle:horizontal {
    background: $surface; border: 2px solid $primary; width: 12px; height: 12px;
    margin: -6px 0; border-radius: 8px;
}
QSlider::handle:horizontal:hover { background: $primary_soft; }
QSlider:focus { border: 1px solid $focus; border-radius: 6px; }
QSlider::sub-page:horizontal:disabled { background: $disabled_text; }
QSlider::handle:horizontal:disabled { border-color: $disabled_text; background: $disabled_bg; }

/* ---- scroll bars: thin and rounded ---- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 3px; min-width: 24px; }
QScrollBar::handle:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ---- lists and tables ---- */
QListView, QListWidget, QTableView, QTableWidget, QTreeView {
    background: $surface; alternate-background-color: $surface_alt; color: $text;
    border: 1px solid $border; border-radius: ${rc}px; gridline-color: $border; outline: 0;
    selection-background-color: $primary_soft; selection-color: $primary_text;
}
QListView:focus, QListWidget:focus, QTableView:focus, QTableWidget:focus, QTreeView:focus {
    border-color: $focus;
}
QListView::item, QTableView::item { padding: 4px 6px; border: none; }
QListView::item:hover, QTableView::item:hover { background: $hover; }
QListView::item:selected, QTableView::item:selected { background: $primary_soft; color: $primary_text; }
QHeaderView { background: transparent; border: none; }
QHeaderView::section {
    background: $surface_alt; color: $muted; border: none; border-bottom: 1px solid $border;
    padding: 6px 8px; font-weight: 600;
}
QTableCornerButton::section { background: $surface_alt; border: none; }

/* ---- group boxes: section titles of the tool rail ---- */
QGroupBox {
    background: transparent; border: none; border-top: 1px solid $border;
    margin-top: 16px; padding-top: 8px; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left; left: 0px; padding: 0 4px 0 0;
    color: $muted;
}
FormatBar {
    background: $surface_alt; border: 1px solid $border; border-radius: ${rk}px;
    margin-top: 18px; padding: 10px 8px 8px 8px; font-size: ${body}pt;
}
FormatBar::title { left: 4px; }

/* ---- status bar ---- */
QStatusBar { background: $surface; border-top: 1px solid $border; color: $muted; min-height: 28px; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: $muted; }

/* ---- cards ---- */
QFrame[card="true"] { background: $surface; border: 1px solid $border; border-radius: ${rk}px; }

/* ---- small text roles and pill badges ---- */
QLabel[muted="true"] { color: $muted; font-size: ${small}pt; }
QLabel[section="true"] { font-weight: 600; }
QLabel[pill], QToolButton[pill] {
    border-radius: 10px; padding: 2px 8px; font-size: ${small}pt; font-weight: 600;
}
QLabel[pill="neutral"], QToolButton[pill="neutral"] { background: $surface_alt; color: $text_soft; border: 1px solid $border; }
QLabel[pill="primary"], QToolButton[pill="primary"] { background: $primary_soft; color: $primary_text; border: 1px solid $primary_soft; }
QLabel[pill="success"], QToolButton[pill="success"] { background: $success_soft; color: $success_text; border: 1px solid $success_soft; }
QLabel[pill="warning"], QToolButton[pill="warning"] { background: $warning_soft; color: $warning_text; border: 1px solid $warning_soft; }
QLabel[pill="danger"], QToolButton[pill="danger"] { background: $danger_soft; color: $danger_text; border: 1px solid $danger_soft; }
QToolButton#statsChip { padding: 1px 8px; }
QToolButton#statsChip:hover { border-color: $primary; }

/* ---- shell: top bar, compact rail, dock scroll areas, compact history buttons ---- */
QToolBar#toolbar_top { padding: 4px 10px; spacing: 4px; }
QLabel#brand { font-weight: 700; font-size: ${title}pt; }
QScrollArea#dockScroll { background: transparent; border: none; }
QScrollArea#dockScroll > QWidget > QWidget { background: transparent; }
ToolDock QGroupBox { font-size: ${small}pt; border: none; margin-top: 14px; padding-top: 4px; }
ToolDock FormatBar { border: 1px solid $border; }

/* ---- styling hooks set by the owners' files (approved UI handoff) ---- */
#command_bar { background: $surface; border: 1px solid $border_strong; border-radius: ${rc}px; }
#command_bar QLabel { background: transparent; }
QToolButton#swatch, QToolButton#inkSwatch {
    border: 1px solid $border_strong; border-radius: 12px; padding: 0px;
}
QToolButton#swatch:hover, QToolButton#inkSwatch:hover { border-color: $muted; }
QToolButton#swatch:checked {
    border: 2px solid $primary; padding: 2px; background-clip: content; border-radius: 12px;
}
QToolButton#swatch:disabled { border: 1px dashed $disabled_text; }
QToolButton#swatch:focus, QToolButton#inkSwatch:focus { border: 2px solid $focus; }
QLabel#currentSwatch { border: 1px solid $border_strong; border-radius: 12px; }
QLabel#viewerHint {
    background: $warning_soft; color: $warning_text; border: 1px solid $warning_soft;
    border-radius: ${rc}px; padding: 8px;
}
HistoryPanel QTableWidget { gridline-color: transparent; }
HistoryPanel QTableWidget::item { border-bottom: 1px solid $border; padding: 4px 10px; }
HistoryPanel QTableWidget::item:selected { background: $primary_soft; color: $primary_text; }
HistoryPanel QPushButton { padding: 4px 8px; min-height: 16px; }
HistoryPanel QPushButton#historySave { padding: 4px 12px; }

/* ---- panels (phase 3): tool rail titles, users, chat, stats tiles ---- */
ToolDock QGroupBox::title { left: 7px; }
QListWidget#usersList { border: none; background: transparent; padding: 0px; }
QListWidget#usersList::item, QListWidget#usersList::item:hover,
QListWidget#usersList::item:selected { background: transparent; border: none; }
ChatInput { border-radius: ${rk}px; padding: 6px 10px; }
ConversationView { border: none; background: $surface; }
QToolButton#sendButton { background: $primary; border: none; border-radius: 18px; }
QToolButton#sendButton:hover { background: $primary_hover; }
QToolButton#sendButton:pressed { background: $primary_hover; }
QToolButton#sendButton:disabled { background: $disabled_bg; }
QFrame#metricTile { background: $surface_alt; }
QLabel#metricValue { font-size: ${title}pt; font-weight: 600; }

/* ---- connect dialog: landing card (phase 4) ---- */
QDialog#connectDialog { background: $bg; }
QFrame#connectCard { background: $surface; border: 1px solid $border; border-radius: ${rk}px; }
QLabel#connectTitle { font-size: ${display}pt; font-weight: 700; }
QFrame#segmented { background: $surface_alt; border: 1px solid $border; border-radius: ${rc}px; }
QPushButton#segment {
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 8px 12px; min-height: 22px; font-weight: 600; color: $text_soft;
}
QPushButton#segment:hover { background: $hover; }
QPushButton#segment:checked { background: $surface; color: $primary_text; border-color: $border_strong; }
QTabWidget#connectTabs::pane { border: none; background: transparent; }
QLineEdit#roomCodeInput { font-weight: 600; }
QToolButton#copyCode { padding: 6px; }
QLabel[tone="error"], QLabel[tone="warning"], QLabel[tone="info"], QLabel[tone="success"] {
    border-radius: ${rc}px;
}
QLabel[tone="error"] { background: $danger_soft; color: $danger_text; }
QLabel[tone="warning"] { background: $warning_soft; color: $warning_text; }
QLabel[tone="success"] { background: $success_soft; color: $success_text; }
QLabel[tone="info"] { background: $primary_soft; color: $primary_text; }
QLabel[toneText="error"], QLabel[toneText="danger"] { color: $danger_text; }
QLabel[toneText="muted"] { color: $muted; }
QLabel[toneText="warning"] { color: $warning_text; }
QLabel[toneText="success"] { color: $success_text; }

/* ---- dock headers, compact rail, bottom tabs (phase 4B) ---- */
QWidget#dockHeader { background: $surface; border-bottom: 1px solid $border; }
QWidget#dockHeader QToolButton#dockCollapse:focus { border: 1px solid $focus; }
QLabel#dockTitle { font-weight: 600; }
QToolButton#dockCollapse { padding: 2px; border-radius: 6px; }
QFrame#railDivider { background: $border; border: none; }
QWidget#compactRail { background: transparent; }
QMainWindow > QTabBar { background: $surface; border-bottom: 1px solid $border; }
QMainWindow > QTabBar::tab { min-height: ${tabh}px; padding: 0px 14px; }

/* ---- toasts (phase 4) ---- */
QFrame#toast { background: $surface; border: 1px solid $border_strong; border-radius: ${rk}px; }
QFrame#toast QLabel { background: transparent; color: $text; }
QFrame#toast[kind="info"] { border-left: 4px solid $primary; }
QFrame#toast[kind="success"] { border-left: 4px solid $success; }
QFrame#toast[kind="warning"] { border-left: 4px solid $warning; }
QFrame#toast[kind="error"] { border-left: 4px solid $danger; }
QLabel#viewOnlyBanner { background: $warning_soft; color: $warning_text; font-weight: 600; }
""")


def build_stylesheet(mode):
    """Return the complete application style sheet for mode ('light' or 'dark').

    The chevrons and the close cross are painted icons written once to temp
    PNGs (icons.write_png), because QSS can only load images from files.
    """
    t = dict(TOKENS[mode])
    t.update(rc=RADIUS_CONTROL, rk=RADIUS_CARD, small=SMALL_PT, body=BODY_PT, title=TITLE_PT,
             display=DISPLAY_PT, tabh=TAB_BAR_H - 2,
             chev_down=icons.write_png("chevron_down", 12, t["muted"]),
             chev_up=icons.write_png("chevron_up", 12, t["muted"]),
             close_icon=icons.write_png("close", 12, t["muted"]))
    return _QSS.substitute(t)


def build_palette(mode):
    """Return a QPalette matching the tokens of mode.

    Widgets that paint themselves (graphs, list delegates, QTextBrowser HTML)
    read the palette, not the style sheet, so both must agree.
    """
    t = TOKENS[mode]
    pal = QPalette()
    roles = {
        QPalette.Window: t["surface"], QPalette.WindowText: t["text"],
        QPalette.Base: t["input_bg"], QPalette.AlternateBase: t["surface_alt"],
        QPalette.Text: t["text"], QPalette.Button: t["surface"], QPalette.ButtonText: t["text"],
        QPalette.BrightText: t["danger"], QPalette.Highlight: t["primary"],
        QPalette.HighlightedText: t["on_primary"], QPalette.ToolTipBase: t["tooltip_bg"],
        QPalette.ToolTipText: t["tooltip_text"], QPalette.Link: t["primary_text"],
        QPalette.LinkVisited: t["primary_text"], QPalette.Light: t["surface"],
        QPalette.Midlight: t["border"], QPalette.Mid: t["border_strong"],
        QPalette.Dark: t["muted"], QPalette.Shadow: t["text"],
    }
    if hasattr(QPalette, "PlaceholderText"):          # Qt 5.12+
        roles[QPalette.PlaceholderText] = t["muted"]
    for role, value in roles.items():
        pal.setColor(QPalette.All, role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(t["disabled_text"]))
    pal.setColor(QPalette.Disabled, QPalette.Base, QColor(t["disabled_bg"]))
    pal.setColor(QPalette.Disabled, QPalette.Button, QColor(t["disabled_bg"]))
    return pal


# --- applying ----------------------------------------------------------------------

def saved_mode():
    """Return the mode stored in QSettings (ui/theme), or 'light'."""
    value = QSettings(SETTINGS_ORG, SETTINGS_APP).value(THEME_KEY, "light")
    return value if value in MODES else "light"


def apply_theme(app, mode=None, save=True):
    """Apply mode ('light'/'dark'; None = the saved one) to the whole application.

    Sets the Fusion style (the same look on Windows and Ubuntu), the font
    (first installed of FONT_STACK, 10 pt), the palette and the style sheet;
    then re-paints the icons of every decorated window for the new colours,
    stores the choice (unless save is False, e.g. for screenshots) and emits
    notifier.changed. Called at start-up and by the menu toggle only.
    """
    global _mode
    mode = mode if mode in MODES else saved_mode()
    old, _mode = _mode, mode
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    font = QFont(pick_font_family())
    font.setPointSize(BODY_PT)
    app.setFont(font)
    app.setPalette(build_palette(mode))
    app.setStyleSheet(build_stylesheet(mode))
    if old != mode:
        retint_widget_styles(app, old, mode)
    for widget in app.topLevelWidgets():
        if widget.property("wbDecorated"):
            decorate_widgets(widget)
    if save:
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(THEME_KEY, mode)
    notifier.changed.emit(mode)


# Text-colour tokens that owners' widgets may write into their own style sheet.
TEXT_TOKENS = ("text", "text_soft", "muted", "primary_text", "success_text", "warning_text",
               "danger_text", "disabled_text")
_COLOR_DECL = re.compile(r"(?<![-\w])(color\s*:\s*)(#[0-9a-fA-F]{6})\b")


def retint_widget_styles(app, old, new):
    """Swap theme text colours inside widgets' OWN style sheets from mode old to mode new.

    A widget's own style sheet beats the application one, so a label that
    was given "color: <muted>" when it was built (for example the command
    bar's hint line in P2's shortcuts.py) would keep the old mode's colour
    after a switch and fail the 4.5:1 contrast. Only `color:` declarations
    whose value is one of TEXT_TOKENS of the old mode are changed; backgrounds
    (colour swatches, sticky colours) and user colours are left alone.
    """
    swap = {TOKENS[old][k].lower(): TOKENS[new][k] for k in TEXT_TOKENS}

    def fix(match):
        """Replace one color value if it is an old-mode text token."""
        return match.group(1) + swap.get(match.group(2).lower(), match.group(2))

    for widget in app.allWidgets():
        sheet = widget.styleSheet()
        if sheet and "color" in sheet:
            new_sheet = _COLOR_DECL.sub(fix, sheet)
            if new_sheet != sheet:
                widget.setStyleSheet(new_sheet)


def toggle_theme(app=None):
    """Switch between light and dark and remember the choice; returns the new mode."""
    app = app or QApplication.instance()
    apply_theme(app, "dark" if _mode == "light" else "light")
    return _mode


def repolish(widget):
    """Re-evaluate the style sheet of widget after one of its properties changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_pill(label, kind="neutral"):
    """Show a QLabel as a pill badge of kind neutral/primary/success/warning/danger."""
    if label.property("pill") != kind:
        label.setProperty("pill", kind)
        repolish(label)


# --- decorate pass -----------------------------------------------------------------

def _tool_tables():
    """Return ({label: tool id}, {tool id: tooltip}) from registry.TOOLS."""
    by_label, tips = {}, {}
    for tool_id, cls in registry.TOOLS.items():
        label = getattr(cls, "label", None) or tool_id
        by_label[label] = tool_id
        shortcut = getattr(cls, "shortcut", None)
        tips[tool_id] = "%s (%s)" % (label, shortcut) if shortcut else label
    return by_label, tips


def _state_icon(name, size=ICON_SIZE):
    """Icon `name` coloured for normal / checked / disabled / hover in the active mode."""
    t = tokens()
    return icons.get_state_icon(name, size, normal=t["text_soft"], checked=t["primary_text"],
                                disabled=t["disabled_text"], active=t["text"])


def _set_name(widget, name):
    """Give widget an objectName for QSS, but never rename one its owner already named."""
    if not widget.objectName() or widget.objectName() == name:
        widget.setObjectName(name)


def _safely(fn, *args):
    """Run one decorate step; a missing or changed widget must never break start-up."""
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001  (decoration is optional, the app is not)
        print("[theme] decorate step %s skipped: %s" % (fn.__name__, exc))


def _decorate_tool_buttons(root, by_label, tips):
    """Tool rail buttons (found by their label): icon, tooltip with shortcut, objectName.

    The buttons of main_window's text tool bar (toolbar_tools) are skipped: shell.py
    hides that bar for good and only keeps its actions for the shortcuts.
    """
    hidden_bar = root.findChild(QToolBar, "toolbar_tools")
    for btn in root.findChildren(QToolButton):
        tool_id = by_label.get(btn.text())
        if tool_id is None or (hidden_bar is not None and hidden_bar.isAncestorOf(btn)):
            continue
        btn.setIcon(_state_icon(TOOL_ICONS.get(tool_id, tool_id)))
        btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        # shell.py makes the rail icon-only; a later re-decoration must not undo that.
        btn.setToolButtonStyle(Qt.ToolButtonIconOnly if btn.property("railCompact")
                               else Qt.ToolButtonTextBesideIcon)
        btn.setProperty("toolId", tool_id)
        if not btn.toolTip():
            btn.setToolTip(tips[tool_id])
        _set_name(btn, "toolButton")


def _decorate_small_buttons(root):
    """Colour swatches and format toggles: objectNames (and icons for alignment)."""
    for btn in root.findChildren(QToolButton):
        tip = btn.toolTip()
        if btn.isCheckable() and not btn.text() and tip.startswith("#") and len(tip) == 7:
            _set_name(btn, "swatch")
        elif btn.isCheckable() and btn.text() in FORMAT_TOGGLES and isinstance(
                btn.parent(), QGroupBox):
            _set_name(btn, "formatToggle")
            icon = FORMAT_TOGGLES[btn.text()]
            if icon:
                # ICON_TEXT, not ICON_SIZE: P2 fixes these buttons at 26 x 24 px, and the
                # alignment icons match the B / I / U letters next to them.
                btn.setIcon(_state_icon(icon, ICON_TEXT))
                btn.setIconSize(QSize(ICON_TEXT, ICON_TEXT))
                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)


def _decorate_push_buttons(root):
    """Push buttons: the dialog's default button becomes primary; known ones get icons.

    Primary is a property set here once, not the QSS ':default' state: Qt
    hands ':default' to whichever auto-default button has focus, so Cancel
    would turn blue while focused.
    """
    for btn in root.findChildren(QPushButton):
        if btn.isDefault() and not btn.property("primary"):
            btn.setProperty("primary", True)
            repolish(btn)
        entry = NAMED_BUTTON_ICONS.get(btn.objectName()) or BUTTON_ICONS.get(btn.text())
        if entry is None:
            continue
        name, danger = entry
        # Icon-only buttons (History's restore, rename, ...) get the full icon size.
        size = ICON_TEXT if btn.text() else ICON_SIZE
        btn.setIcon(_state_icon(name, size))
        btn.setIconSize(QSize(size, size))
        if danger and not btn.property("danger"):
            btn.setProperty("danger", True)
            repolish(btn)


def _decorate_menus(root):
    """Icons on the menu entries listed in MENU_ICONS, and the theme toggle's moon/sun."""
    bar = root.menuBar() if hasattr(root, "menuBar") else None
    if bar is None:
        return
    t = tokens()
    for top in bar.actions():
        menu = top.menu()
        if menu is None:
            continue
        for action in menu.actions():
            text = action.text()
            if text == THEME_MENU_LABEL:
                name = "sun" if _mode == "dark" else "moon"
            else:
                name = MENU_ICONS.get(text)
            if name:
                action.setIcon(icons.get_icon(name, ICON_TEXT, t["text_soft"]))


def _decorate_docks(root):
    """Dock icons (visible on dock tabs) by the docks' objectNames."""
    t = tokens()
    for dock in root.findChildren(QDockWidget):
        name = DOCK_ICONS.get(dock.objectName())
        if name:
            dock.setWindowIcon(icons.get_icon(name, ICON_TEXT, t["text_soft"]))


def decorate_widgets(root):
    """Walk the widget tree of root once and add icons, tooltips and objectNames.

    Widgets are found by what they already show (a tool's label, a button's
    text, a dock's objectName), so other owners' files are not edited. Each
    step is wrapped so a widget that is missing or renamed only loses its
    decoration. Safe to call again: apply_theme does so after a theme change
    to repaint the icons in the new colours.
    """
    by_label, tips = _tool_tables()
    _safely(_decorate_tool_buttons, root, by_label, tips)
    _safely(_decorate_small_buttons, root)
    _safely(_decorate_push_buttons, root)
    _safely(_decorate_menus, root)
    _safely(_decorate_docks, root)
    root.setProperty("wbDecorated", True)


# --- View > Toggle dark theme -----------------------------------------------------

def _menu_toggle(app):
    """Menu action: switch the theme and confirm with a toast."""
    mode = toggle_theme(QApplication.instance())
    app.toast("Dark theme on." if mode == "dark" else "Light theme on.")


registry.register_menu_action("View", THEME_MENU_LABEL, _menu_toggle)
