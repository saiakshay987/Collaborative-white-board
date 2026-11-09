# OWNER: P4
"""main_window.py: Main window: assembles the canvas, toolbar, menus and registered panels.

First simple version written by P1; P4 takes it over. Everything is built
from client/registry.py, so features appear without editing this file:
    registry.TOOLS         -> "Tools" toolbar (one checkable action per tool)
    registry.MENU_ACTIONS  -> menu bar
    registry.SHORTCUTS     -> window-wide shortcuts
    registry.PANELS        -> dock widgets
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QAction, QActionGroup, QDockWidget, QLabel, QMainWindow, QShortcut

from client import canvas as canvas_mod
from client import registry

# registry area names -> Qt dock areas.
_AREAS = {
    "left": Qt.LeftDockWidgetArea,
    "right": Qt.RightDockWidgetArea,
    "bottom": Qt.BottomDockWidgetArea,
    "top": Qt.TopDockWidgetArea,
}


class MainWindow(QMainWindow):
    """The application window, built from the registries."""

    def __init__(self, app):
        """Build canvas, toolbar, menus, shortcuts and docks for AppState app."""
        super().__init__()
        self.app = app
        self.setWindowTitle("LAN Whiteboard")
        self.resize(1200, 800)
        self._build_canvas()
        self._build_toolbar()
        self._build_menus()
        self._build_shortcuts()
        self._build_docks()
        self.statusBar().showMessage("Ready")

    def _build_canvas(self):
        """Central widget: P2's CanvasWidget, or a placeholder until it exists."""
        cls = getattr(canvas_mod, "CanvasWidget", None)
        if cls is not None:
            widget = cls(self.app)
            self.app.canvas = widget
        else:
            widget = QLabel("Canvas not implemented yet (P2)")
            widget.setAlignment(Qt.AlignCenter)
        self.setCentralWidget(widget)

    def _build_toolbar(self):
        """One checkable action per registered tool; exactly one is active."""
        bar = self.addToolBar("Tools")
        group = QActionGroup(self)
        group.setExclusive(True)
        for tool_id, cls in registry.TOOLS.items():
            action = QAction(getattr(cls, "label", tool_id), self, checkable=True)
            if getattr(cls, "shortcut", None):
                action.setShortcut(QKeySequence(cls.shortcut))
            action.triggered.connect(lambda _checked, c=cls: self.select_tool(c))
            group.addAction(action)
            bar.addAction(action)

    def select_tool(self, cls):
        """Make a new instance of tool class cls the active tool."""
        self.app.active_tool = cls()

    def _build_menus(self):
        """Create menus in first-registration order and add their actions."""
        menus = {}
        for entry in registry.MENU_ACTIONS:
            menu = menus.get(entry["menu"])
            if menu is None:
                menu = menus[entry["menu"]] = self.menuBar().addMenu(entry["menu"])
            action = menu.addAction(entry["label"])
            if entry["shortcut"]:
                action.setShortcut(QKeySequence(entry["shortcut"]))
            action.triggered.connect(lambda _checked, fn=entry["fn"]: fn(self.app))

    def _build_shortcuts(self):
        """Window-wide keyboard shortcuts."""
        for entry in registry.SHORTCUTS:
            sc = QShortcut(QKeySequence(entry["keyseq"]), self)
            sc.activated.connect(lambda fn=entry["fn"]: fn(self.app))

    def _build_docks(self):
        """One dock widget per registered panel, plus a View menu toggle each."""
        view = None
        for panel in registry.PANELS.values():
            dock = QDockWidget(panel["title"], self)
            dock.setObjectName("dock_" + panel["name"])
            dock.setWidget(panel["factory"](self.app))
            self.addDockWidget(_AREAS[panel["area"]], dock)
            if view is None:
                view = self.menuBar().addMenu("View")
            view.addAction(dock.toggleViewAction())
