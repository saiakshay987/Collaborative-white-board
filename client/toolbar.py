# OWNER: P2
"""toolbar.py: Tool dock built from the tools registry, plus color/width controls with per-tool memory.

Registered with registry.register_panel as the "tools" panel in the LEFT
dock area (contract G). main_window calls the factory once while it builds
the window, so the dock is created after every module has been imported.

What the dock contains
----------------------
Tools       One checkable button per class in registry.TOOLS, grouped by
            the tool's `group` attribute (groups in first-registration
            order). The tooltip shows label and shortcut, e.g. "Marker (M)".
            Pen, marker and highlighter are separate tools (pen_tool.py).
Color       A 12-color palette plus "More..." (QColorDialog) for any color.
            Greyed out for tools with uses_color = False (the eraser).
Width       A slider 1-40 (contract D: width 1-40) with a live preview
            drawn exactly as the active tool will draw: the marker 1.5x
            wider at 60 %, the highlighter at least 12 wide at 35 %. For the
            eraser the slider is the hit RADIUS and the preview is a ring.

All controls write the shared dict app.pen {color, width, tool, alpha}
(contract F). The drawing tools turn app.pen into the stroke style when a
stroke starts (PenTool.stroke_style), so a change never alters a stroke that
is already being drawn. Shape tools read the same color and width.

Per-tool memory
---------------
Every tool class with a `pen_defaults` dict {color, width} has its own
remembered color and width (_memory). Changing a control saves the value
for the active tool; selecting a tool loads its values into app.pen (the
class defaults the first time). So pen -> highlighter -> pen gives back the
pen's own color and width, and the eraser keeps its own radius. Tools
without pen_defaults (select, text, image) leave app.pen as it is. The
memory lives only in this client for this session.

Choosing a tool
---------------
main_window owns the "Tools" toolbar, the tool keyboard shortcuts and
select_tool(); binding the same keys twice would make Qt treat them as
ambiguous. A click in the dock therefore calls app.window.select_tool(id),
and the dock follows every change, whoever made it, through the window's
tool_selected signal (GUI thread). Before the window exists (the dock is
built inside its constructor) the dock sets app.active_tool itself. A light
GUI-thread timer (SYNC_MS) is a fallback that also catches anything that
assigns app.active_tool directly.

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

# Width limits (contract D: width 1-40).
WIDTH_MIN = 1
WIDTH_MAX = 40

# Tool selected when the dock is built, if it is registered.
DEFAULT_TOOL = "pen"

# How often (ms) the fallback timer checks app.active_tool.
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


def _fmt(value):
    """Show a width without a useless '.0' (4.5 stays 4.5, 9.0 becomes 9)."""
    value = float(value)
    return str(int(value)) if value.is_integer() else "%.1f" % value


class WidthPreview(QWidget):
    """A small square that shows one dab of the active tool, or the eraser's ring.

    dot mode: a dot as wide as the stroke, in its color and opacity (flat
    square for the highlighter's chisel tip). ring mode: an outline circle
    with the eraser's radius. Sizes larger than the widget are clipped to
    it; the label under the slider gives the exact value.
    """

    SIDE = 44

    def __init__(self, parent=None):
        """Create the preview with a black, 3-unit, opaque dot."""
        super().__init__(parent)
        self._color = QColor("#000000")
        self._width = 3.0
        self._alpha = 1.0
        self._square = False
        self._ring = False
        self.setFixedSize(self.SIDE, self.SIDE)

    def set_dot(self, color, width, alpha, square=False):
        """Show a stroke dab of this color, width (diameter) and opacity."""
        self._color, self._width, self._alpha = QColor(color), float(width), float(alpha)
        self._square, self._ring = square, False
        self.update()

    def set_ring(self, radius):
        """Show the eraser's hit circle of this radius."""
        self._width, self._ring = 2.0 * float(radius), True
        self.update()

    def paintEvent(self, ev):
        """Paint a light square background, then the dot or the ring centred in it."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QColor("#f4f4f4"))
            painter.setPen(QPen(QColor("#c8c8c8")))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            d = min(self._width, self.SIDE - 4.0)
            box = QRectF((self.width() - d) / 2.0, (self.height() - d) / 2.0, d, d)
            if self._ring:
                painter.setPen(QPen(QColor("#333333"), 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(box)
                return
            color = QColor(self._color)
            color.setAlphaF(max(0.0, min(1.0, self._alpha)))
            # A white dot would vanish on the light background: outline it.
            painter.setPen(QPen(QColor("#999999")) if color.lightness() > 230 else Qt.NoPen)
            painter.setBrush(color)
            if self._square:
                painter.drawRect(box)
            else:
                painter.drawEllipse(box)
        finally:
            painter.end()


class ToolDock(QWidget):
    """The left dock: tool buttons by group, color palette and width, remembered per tool."""

    def __init__(self, app, parent=None):
        """Build every section from registry.TOOLS and app.pen, then select the default tool."""
        super().__init__(parent)
        self.app = app
        self._instances = {}          # tool id -> instance (only used before the window exists)
        self._tool_buttons = {}       # tool id -> QToolButton
        self._current_id = None       # tool whose settings the controls show
        self._last_tool_id = None     # tool to restore when a viewer becomes editor again
        self._memory = {}             # tool id -> {"color", "width"} last used with that tool
        self._window = None           # MainWindow whose tool_selected we follow

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

        # Role changes come from on_message handlers on the GUI thread.
        app.role_changed.connect(self._apply_role)

        # Start with a tool so the canvas reacts right away.
        first = DEFAULT_TOOL if DEFAULT_TOOL in registry.TOOLS else next(iter(registry.TOOLS), None)
        if first is not None:
            self.select_tool(first)
        else:
            self._show_pen()
        self._apply_role()

        # app.window is assigned only after MainWindow's constructor returns,
        # so connect to its tool_selected signal from the event loop.
        QTimer.singleShot(0, self._connect_window)
        # Fallback: follow app.active_tool however it was changed. GUI thread; cheap.
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

    def _connect_window(self):
        """Follow MainWindow.tool_selected once app.window exists (called again by the timer)."""
        window = getattr(self.app, "window", None)
        if window is None or window is self._window or not hasattr(window, "tool_selected"):
            return
        self._window = window
        window.tool_selected.connect(self._on_tool_changed)

    def select_tool(self, tool_id):
        """Make tool_id the active tool and load its remembered color and width.

        Through main_window.select_tool when the window exists (it checks
        its toolbar action and emits tool_selected); before that, the dock
        keeps one instance per tool id itself.
        """
        cls = registry.TOOLS.get(tool_id)
        if cls is None:
            return
        window = getattr(self.app, "window", None)
        if window is not None and hasattr(window, "select_tool"):
            window.select_tool(tool_id)
        else:
            tool = self._instances.get(tool_id)
            if tool is None:
                tool = self._instances[tool_id] = cls()
            self.app.active_tool = tool
        self._on_tool_changed(tool_id)   # no-op if tool_selected already did it

    def _on_tool_changed(self, tool_id):
        """The active tool is now tool_id: check its button, set the cursor, load its settings.

        Connected to MainWindow.tool_selected, so it runs on the GUI thread
        right when a shortcut or the main toolbar switches tools, before the
        user can start a stroke with the old color.
        """
        if tool_id not in registry.TOOLS:
            return
        self._last_tool_id = tool_id
        self._check_button(tool_id)
        self._set_canvas_cursor(getattr(self.app, "active_tool", None))
        if tool_id == self._current_id:
            return
        self._current_id = tool_id
        self._load_tool_settings(tool_id)

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
        """Fallback timer: connect to the window, and follow an app.active_tool we missed."""
        self._connect_window()
        tool = getattr(self.app, "active_tool", None)
        tool_id = getattr(tool, "id", None)
        if tool_id is not None and tool_id != self._current_id:
            self._on_tool_changed(tool_id)

    # ---------------------------------------------------------- per-tool memory
    def _current_cls(self):
        """Class of the tool the controls belong to, or None."""
        return registry.TOOLS.get(self._current_id)

    def _load_tool_settings(self, tool_id):
        """Put tool_id's remembered (or default) color and width into app.pen.

        For a drawing tool also set app.pen's tool and alpha (contract F
        fields) to its stroke type and opacity. A tool without pen_defaults
        does not use the pen, so app.pen is left as it is.
        """
        cls = registry.TOOLS.get(tool_id)
        defaults = getattr(cls, "pen_defaults", None)
        if defaults is not None:
            saved = self._memory.get(tool_id) or defaults
            self.app.pen["color"] = QColor(saved.get("color", "#000000")).name()
            self.app.pen["width"] = max(WIDTH_MIN, min(WIDTH_MAX, int(saved.get("width", 3))))
            if getattr(cls, "stroke_tool", None):
                self.app.pen["tool"] = cls.stroke_tool
                self.app.pen["alpha"] = float(getattr(cls, "alpha", 1.0))
        self._show_pen()

    def _remember(self):
        """Save app.pen's color and width as the active tool's own settings."""
        if getattr(self._current_cls(), "pen_defaults", None) is not None:
            self._memory[self._current_id] = {"color": self.app.pen.get("color"),
                                              "width": self.app.pen.get("width")}

    # ------------------------------------------------------------ pen controls
    def _build_pen_controls(self):
        """Color palette with 'More...', width slider, its readout and the live preview."""
        box = QGroupBox("Pen")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        # Color palette: 12 swatches in a grid, plus "More..." for any color.
        self._color_box = QWidget()
        cv = QVBoxLayout(self._color_box)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(QLabel("Color"))
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
        cv.addLayout(grid)

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
        cv.addLayout(more_row)
        v.addWidget(self._color_box)

        # Width: slider 1-40, number and a live preview.
        self._width_title = QLabel("Width")
        v.addWidget(self._width_title)
        width_row = QHBoxLayout()
        self._width_slider = QSlider(Qt.Horizontal)
        self._width_slider.setRange(WIDTH_MIN, WIDTH_MAX)
        self._width_slider.valueChanged.connect(self._set_width)
        self._width_label = QLabel()
        self._width_label.setMinimumWidth(22)
        self._preview = WidthPreview()
        width_row.addWidget(self._width_slider, 1)
        width_row.addWidget(self._width_label)
        width_row.addWidget(self._preview)
        v.addLayout(width_row)

        # What the tool will really draw (marker wider, highlighter see-through...).
        self._info_label = QLabel()
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("QLabel { color: #555; }")
        v.addWidget(self._info_label)
        return box

    def _set_color(self, color):
        """Write a '#rrggbb' color into app.pen and remember it for the active tool."""
        self.app.pen["color"] = QColor(color).name()   # normalised lower-case #rrggbb
        self._remember()
        self._show_pen()

    def _pick_color(self):
        """Open QColorDialog for any color (no alpha: opacity comes from the tool).

        The dialog is modal but runs Qt's event loop, so network messages
        keep arriving and the board keeps repainting while it is open.
        """
        color = QColorDialog.getColor(QColor(self.app.pen.get("color", "#000000")), self,
                                      "Pen color")
        if color.isValid():
            self._set_color(color.name())

    def _set_width(self, value):
        """Write the slider value (clamped to 1-40) into app.pen['width'] and remember it."""
        self.app.pen["width"] = max(WIDTH_MIN, min(WIDTH_MAX, int(value)))
        self._remember()
        self._show_pen()

    def _show_pen(self):
        """Make every control show app.pen for the active tool, without writing back."""
        pen = self.app.pen
        cls = self._current_cls()
        label = getattr(cls, "label", None) or "Pen"
        color = QColor(pen.get("color", "#000000")).name()
        width = int(pen.get("width", 3))
        is_eraser = cls is not None and getattr(cls, "pen_defaults", None) is not None \
            and not getattr(cls, "uses_color", True)

        self._pen_box.setTitle("%s settings" % label)
        self._color_box.setEnabled(not is_eraser)

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

        style_fn = getattr(cls, "stroke_style", None)
        if is_eraser:
            self._width_title.setText("Eraser radius")
            self._width_slider.setToolTip("Hit radius in board units (%d-%d)" % (WIDTH_MIN, WIDTH_MAX))
            self._info_label.setText("Erases every object within %d units of the circle's centre."
                                     % width)
            self._preview.set_ring(width)
        elif style_fn is not None:
            style = style_fn(pen)
            self._width_title.setText("Width")
            self._width_slider.setToolTip("Width (%d-%d)" % (WIDTH_MIN, WIDTH_MAX))
            self._info_label.setText("Draws %s wide at %d%% opacity."
                                     % (_fmt(style["width"]), round(style["alpha"] * 100)))
            self._preview.set_dot(style["color"], style["width"], style["alpha"],
                                  square=getattr(cls, "flat_cap", False))
        else:
            self._width_title.setText("Width")
            self._width_slider.setToolTip("Width (%d-%d)" % (WIDTH_MIN, WIDTH_MAX))
            self._info_label.setText("Color and width for shapes and lines.")
            self._preview.set_dot(color, width, 1.0)

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


# The dock built by make_tool_dock (main_window builds it once), so other P2
# code (the command bar in shortcuts.py) can change the pen through it.
_dock = None


def make_tool_dock(app):
    """Panel factory for register_panel: build the ToolDock for AppState app."""
    global _dock
    _dock = ToolDock(app)
    return _dock


def set_pen(app, color=None, width=None):
    """Set the pen color ('#rrggbb') and/or width (1-40) as if chosen in the dock.

    Used by the command bar ('color #ff0000', 'width 5'). Going through the
    dock keeps its swatches, slider and per-tool memory in step. Without a
    dock (tests) app.pen is written directly. GUI thread only.
    """
    dock = _dock
    if dock is not None and dock.app is app:
        if color is not None:
            dock._set_color(color)
        if width is not None:
            dock._set_width(width)
        return
    if color is not None:
        app.pen["color"] = QColor(color).name()
    if width is not None:
        app.pen["width"] = max(WIDTH_MIN, min(WIDTH_MAX, int(width)))


registry.register_panel("tools", make_tool_dock, area="left", title="Tools")
