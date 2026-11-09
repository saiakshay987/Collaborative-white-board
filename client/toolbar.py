# OWNER: P2
"""toolbar.py: Tool dock built from the tools registry, plus pen color/width controls.

Registered with registry.register_panel as the "tools" panel in the LEFT
dock area (contract G). main_window calls the factory once while it builds
the window, so the dock is created after every module has been imported.

What the dock contains
----------------------
Tools       One checkable button per class in registry.TOOLS, grouped by
            the tool's `group` attribute (groups in first-registration
            order). The tooltip shows label and shortcut, e.g. "Pen (P)".
Pen style   pen / marker / highlighter. Each style has its own opacity
            (STYLE_ALPHA); the "Opacity" readout shows the current value.
Color       A 12-color palette plus "More..." (QColorDialog) for any color.
Width       A slider 1-40 (contract D: stroke width 1-40) with a live
            preview dot drawn in the current color, width and opacity.

All pen controls write the shared dict app.pen {color, width, tool, alpha}
(contract F). draw_sync.StrokeStreamer copies app.pen when a stroke starts,
so a change never alters a stroke already being drawn.

Choosing a tool
---------------
select_tool(app, tool_id) puts one instance of that tool class in
app.active_tool (instances are kept, so a tool keeps its own state) and
sets the canvas cursor to tool.cursor(). The canvas forwards mouse events to
whatever app.active_tool is.

Keyboard shortcuts are NOT bound here: main_window's "Tools" toolbar
already binds each tool's shortcut, and binding the same key twice makes Qt
treat it as ambiguous so neither fires. Because main_window can change
app.active_tool on its own, a light GUI-thread timer (SYNC_MS) keeps the
checked button in step with app.active_tool.

Viewer role
-----------
When app.me.role is 'viewer' (app.role_changed is emitted from the JOIN_OK
and USER_LIST handlers, on the GUI thread), every tool button and every pen
control is disabled, app.active_tool is cleared and a hint is shown. When
the role changes back the previous tool is restored. This is only for the
user: the SERVER still checks permissions.can(role, 'draw') on every board
operation and answers ERROR FORBIDDEN.
"""

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (QButtonGroup, QColorDialog, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QSizePolicy, QSlider, QToolButton,
                             QVBoxLayout, QWidget)

from client import registry

# 12 palette colors, '#rrggbb' as the contract requires for object colors.
PALETTE = (
    "#000000", "#555555", "#ffffff", "#e53935",
    "#fb8c00", "#fdd835", "#43a047", "#00897b",
    "#1e88e5", "#3949ab", "#8e24aa", "#6d4c41",
)
PALETTE_COLUMNS = 4

# Pen styles (the stroke "tool" field, contract D) and the opacity each uses.
# A highlighter is see-through so the drawing under it stays readable.
STYLES = (("pen", "Pen"), ("marker", "Marker"), ("highlighter", "Highlighter"))
STYLE_ALPHA = {"pen": 1.0, "marker": 0.8, "highlighter": 0.35}

# Stroke width limits (contract D: width 1-40).
WIDTH_MIN = 1
WIDTH_MAX = 40

# Tool selected when the dock is built, if it is registered.
DEFAULT_TOOL = "pen"

# How often (ms) the dock checks whether main_window's toolbar changed the tool.
SYNC_MS = 200

VIEWER_HINT = ("View only: the host made you a viewer.\n"
               "You can pan, zoom and chat, but not draw.")


def _tool_tooltip(cls):
    """Return 'Label (Shortcut)' for a tool class, or just the label without a shortcut."""
    label = getattr(cls, "label", None) or getattr(cls, "id", "?")
    shortcut = getattr(cls, "shortcut", None)
    return "%s (%s)" % (label, shortcut) if shortcut else label


def _swatch_style(color, checked_border="#1565c0"):
    """Style sheet for a color swatch button: filled square, thick border when checked."""
    return ("QToolButton { background: %s; border: 1px solid #888; border-radius: 3px; }"
            "QToolButton:checked { border: 3px solid %s; }"
            "QToolButton:disabled { border: 1px dashed #bbb; }" % (color, checked_border))


class WidthPreview(QWidget):
    """A small square that paints a dot as wide as the pen, in its color and opacity.

    It shows what one dab of the pen will look like before drawing. Widths
    larger than the widget are clipped to it; the number label next to the
    slider gives the exact value.
    """

    SIDE = 44

    def __init__(self, parent=None):
        """Create the preview with a black, 3-unit, opaque dot."""
        super().__init__(parent)
        self._color = QColor("#000000")
        self._width = 3
        self._alpha = 1.0
        self.setFixedSize(self.SIDE, self.SIDE)

    def set_pen(self, color, width, alpha):
        """Update the dot from app.pen values and repaint."""
        self._color = QColor(color)
        self._width = width
        self._alpha = alpha
        self.update()

    def paintEvent(self, ev):
        """Paint a light square background and a centred dot of diameter = width."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QColor("#f4f4f4"))
            painter.setPen(QPen(QColor("#c8c8c8")))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            color = QColor(self._color)
            color.setAlphaF(max(0.0, min(1.0, float(self._alpha))))
            # A white dot would vanish on the light background: outline it.
            painter.setPen(QPen(QColor("#999999")) if color.lightness() > 230 else Qt.NoPen)
            painter.setBrush(color)
            d = float(min(self._width, self.SIDE - 4))
            painter.drawEllipse(QRectF((self.width() - d) / 2.0, (self.height() - d) / 2.0, d, d))
        finally:
            painter.end()


class ToolDock(QWidget):
    """The left dock: tool buttons by group, pen style, color palette and width."""

    def __init__(self, app, parent=None):
        """Build every section from registry.TOOLS and app.pen, then select the default tool."""
        super().__init__(parent)
        self.app = app
        self._instances = {}          # tool id -> tool instance (kept between switches)
        self._tool_buttons = {}       # tool id -> QToolButton
        self._last_tool_id = None     # tool to restore when a viewer becomes editor again
        self._custom_color = None     # color chosen with "More..." that is not in PALETTE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        self._hint = QLabel(VIEWER_HINT)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("QLabel { background: #fff3cd; color: #664d03;"
                                 " border: 1px solid #ffe69c; border-radius: 4px; padding: 6px; }")
        self._hint.hide()
        layout.addWidget(self._hint)

        self._tools_box = self._build_tools()
        layout.addWidget(self._tools_box)
        self._pen_box = self._build_pen_controls()
        layout.addWidget(self._pen_box)
        layout.addStretch(1)

        self._show_pen(self.app.pen)

        # Role changes come from on_message handlers on the GUI thread.
        app.role_changed.connect(self._apply_role)

        # Start with a tool so the canvas reacts right away.
        first = DEFAULT_TOOL if DEFAULT_TOOL in registry.TOOLS else next(iter(registry.TOOLS), None)
        if first is not None:
            self.select_tool(first)
        self._apply_role()

        # Keep the checked button in step when main_window's toolbar or a
        # shortcut changes app.active_tool. Runs on the GUI thread; cheap.
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_from_app)
        self._sync_timer.start(SYNC_MS)

    # ---------------------------------------------------------------- tools
    def _build_tools(self):
        """One group box per tool group, two checkable buttons per row, all in one exclusive group."""
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)

        groups = {}                   # group name -> [tool id], first-registration order
        for tool_id, cls in registry.TOOLS.items():
            groups.setdefault(getattr(cls, "group", None) or "other", []).append(tool_id)

        if not groups:
            outer.addWidget(QLabel("No tools registered."))
        for group_name, ids in groups.items():
            gbox = QGroupBox(group_name.capitalize())
            grid = QGridLayout(gbox)
            grid.setSpacing(4)
            for i, tool_id in enumerate(ids):
                cls = registry.TOOLS[tool_id]
                btn = QToolButton()
                btn.setText(getattr(cls, "label", None) or tool_id)
                btn.setToolTip(_tool_tooltip(cls))
                btn.setCheckable(True)
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                btn.clicked.connect(lambda _checked, t=tool_id: self.select_tool(t))
                self._tool_group.addButton(btn)
                self._tool_buttons[tool_id] = btn
                grid.addWidget(btn, i // 2, i % 2)
            outer.addWidget(gbox)
        return box

    def select_tool(self, tool_id):
        """Make tool_id the active tool: app.active_tool, app.pen['tool'] stays, cursor updated.

        One instance per tool id is kept so a tool's own state (e.g. the
        pen's StrokeStreamer) is not rebuilt on every switch.
        """
        cls = registry.TOOLS.get(tool_id)
        if cls is None:
            return
        tool = self._instances.get(tool_id)
        if tool is None:
            tool = self._instances[tool_id] = cls()
        self.app.active_tool = tool
        self._last_tool_id = tool_id
        self._check_button(tool_id)
        self._set_canvas_cursor(tool)

    def _check_button(self, tool_id):
        """Check the button of tool_id without emitting clicked (no feedback loop)."""
        btn = self._tool_buttons.get(tool_id)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def _set_canvas_cursor(self, tool):
        """Show tool.cursor() over the canvas (a Qt.CursorShape or a QCursor)."""
        canvas = getattr(self.app, "canvas", None)
        if canvas is None:
            return
        shape = None
        if tool is not None and hasattr(tool, "cursor"):
            try:
                shape = tool.cursor()
            except Exception:  # noqa: BLE001  (a broken tool must not break the dock)
                shape = None
        if tool is None:
            canvas.setCursor(Qt.ArrowCursor)
        else:
            canvas.setCursor(shape if shape is not None else Qt.CrossCursor)

    def _sync_from_app(self):
        """If something else changed app.active_tool, check the matching button.

        main_window's toolbar makes a fresh instance of the class, so we
        adopt that instance as the one kept for its id.
        """
        tool = getattr(self.app, "active_tool", None)
        if tool is None:
            return
        tool_id = getattr(tool, "id", None)
        if tool_id in self._tool_buttons and self._instances.get(tool_id) is not tool:
            self._instances[tool_id] = tool
            self._last_tool_id = tool_id
            self._check_button(tool_id)

    # ------------------------------------------------------------ pen controls
    def _build_pen_controls(self):
        """Pen style buttons, opacity readout, color palette with 'More...' and width slider."""
        box = QGroupBox("Pen")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        # Style: pen / marker / highlighter (exclusive).
        style_row = QHBoxLayout()
        self._style_group = QButtonGroup(self)
        self._style_group.setExclusive(True)
        self._style_buttons = {}
        for style_id, text in STYLES:
            btn = QToolButton()
            btn.setText(text)
            btn.setCheckable(True)
            btn.setToolTip("%s, opacity %d%%" % (text, round(STYLE_ALPHA[style_id] * 100)))
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _checked, s=style_id: self._set_style(s))
            self._style_group.addButton(btn)
            self._style_buttons[style_id] = btn
            style_row.addWidget(btn)
        v.addLayout(style_row)

        self._opacity_label = QLabel()
        v.addWidget(self._opacity_label)

        # Color palette: 12 swatches in a grid, plus "More..." for any color.
        v.addWidget(QLabel("Color"))
        grid = QGridLayout()
        grid.setSpacing(4)
        self._swatch_group = QButtonGroup(self)
        self._swatch_group.setExclusive(True)
        self._swatches = {}
        for i, color in enumerate(PALETTE):
            sw = QToolButton()
            sw.setCheckable(True)
            sw.setFixedSize(QSize(24, 24))
            sw.setToolTip(color)
            sw.setStyleSheet(_swatch_style(color))
            sw.clicked.connect(lambda _checked, c=color: self._set_color(c))
            self._swatch_group.addButton(sw)
            self._swatches[color] = sw
            grid.addWidget(sw, i // PALETTE_COLUMNS, i % PALETTE_COLUMNS)
        grid.setColumnStretch(PALETTE_COLUMNS, 1)   # keep the swatches packed to the left
        v.addLayout(grid)

        more_row = QHBoxLayout()
        self._current_swatch = QLabel()
        self._current_swatch.setFixedSize(24, 24)
        self._current_swatch.setToolTip("Current color")
        more = QToolButton()
        more.setText("More...")
        more.setToolTip("Pick any color")
        more.clicked.connect(self._pick_color)
        more_row.addWidget(self._current_swatch)
        more_row.addWidget(more, 1)
        v.addLayout(more_row)

        # Width: slider 1-40, number and a live preview dot.
        v.addWidget(QLabel("Width"))
        width_row = QHBoxLayout()
        self._width_slider = QSlider(Qt.Horizontal)
        self._width_slider.setRange(WIDTH_MIN, WIDTH_MAX)
        self._width_slider.setToolTip("Stroke width (%d-%d)" % (WIDTH_MIN, WIDTH_MAX))
        self._width_slider.valueChanged.connect(self._set_width)
        self._width_label = QLabel()
        self._width_label.setMinimumWidth(22)
        self._preview = WidthPreview()
        width_row.addWidget(self._width_slider, 1)
        width_row.addWidget(self._width_label)
        width_row.addWidget(self._preview)
        v.addLayout(width_row)
        return box

    def _set_style(self, style_id):
        """Write the pen style and its opacity into app.pen."""
        self.app.pen["tool"] = style_id
        self.app.pen["alpha"] = STYLE_ALPHA.get(style_id, 1.0)
        self._show_pen(self.app.pen)

    def _set_color(self, color):
        """Write a '#rrggbb' color into app.pen."""
        self.app.pen["color"] = QColor(color).name()   # normalised lower-case #rrggbb
        self._show_pen(self.app.pen)

    def _pick_color(self):
        """Open QColorDialog for any color (no alpha: opacity comes from the pen style).

        The dialog is modal but runs Qt's event loop, so network messages
        keep arriving and the board keeps repainting while it is open.
        """
        color = QColorDialog.getColor(QColor(self.app.pen.get("color", "#000000")), self,
                                      "Pen color")
        if color.isValid():
            self._set_color(color.name())

    def _set_width(self, value):
        """Write the slider value (clamped to 1-40) into app.pen['width']."""
        self.app.pen["width"] = max(WIDTH_MIN, min(WIDTH_MAX, int(value)))
        self._show_pen(self.app.pen)

    def _show_pen(self, pen):
        """Make every pen control show the values in pen (app.pen) without writing back."""
        color = QColor(pen.get("color", "#000000")).name()
        width = int(pen.get("width", 3))
        style = pen.get("tool", "pen")
        alpha = float(pen.get("alpha", STYLE_ALPHA.get(style, 1.0)))

        btn = self._style_buttons.get(style)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        self._opacity_label.setText("Opacity: %d%%" % round(alpha * 100))

        # Check the matching swatch, or none when the color came from "More...".
        sw = self._swatches.get(color)
        if sw is not None:
            sw.setChecked(True)
        else:
            checked = self._swatch_group.checkedButton()
            if checked is not None:
                # An exclusive group refuses to uncheck its last button.
                self._swatch_group.setExclusive(False)
                checked.setChecked(False)
                self._swatch_group.setExclusive(True)
        self._current_swatch.setStyleSheet("QLabel { background: %s; border: 1px solid #888;"
                                           " border-radius: 3px; }" % color)
        self._current_swatch.setToolTip("Current color %s" % color)

        if self._width_slider.value() != width:
            self._width_slider.blockSignals(True)   # showing, not changing
            self._width_slider.setValue(width)
            self._width_slider.blockSignals(False)
        self._width_label.setText(str(width))
        self._preview.set_pen(color, width, alpha)

    # ------------------------------------------------------------------ role
    def _apply_role(self):
        """Enable or disable drawing for app.me.role (UI only; the server enforces it).

        viewer: disable all tool buttons and pen controls, clear
        app.active_tool so the canvas ignores clicks, and show the hint.
        Any other role (or not joined yet): enable everything and restore
        the last tool.
        """
        viewer = getattr(self.app.me, "role", None) == "viewer"
        self._hint.setVisible(viewer)
        self._tools_box.setEnabled(not viewer)
        self._pen_box.setEnabled(not viewer)
        if viewer:
            if self.app.active_tool is not None:
                self._last_tool_id = getattr(self.app.active_tool, "id", self._last_tool_id)
            self.app.active_tool = None
            self._set_canvas_cursor(None)
        elif self.app.active_tool is None and self._last_tool_id is not None:
            self.select_tool(self._last_tool_id)


def make_tool_dock(app):
    """Panel factory for register_panel: build the ToolDock for AppState app."""
    return ToolDock(app)


registry.register_panel("tools", make_tool_dock, area="left", title="Tools")
