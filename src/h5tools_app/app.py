"""Main application window: wires the hierarchy tree, dataset table and
group overview panel together around a single open HDF5 file.

The window is frameless (``Qt.FramelessWindowHint``) with a hand-drawn
title bar (``widgets/title_bar.py``), because the native window frame is
drawn by the OS/window manager and looks different (and, on WSLg,
noticeably dated) per platform -- the opposite of what this rewrite is
for. Move uses Qt's native, OS-assisted ``startSystemMove``, not
hand-rolled geometry math. Maximize is the one exception: Qt's own
``showMaximized()`` on a frameless X11 window doesn't reliably know there
are no decorations to account for, and ends up positioning the window
offset from the screen edge -- so maximize/restore is done manually here
by setting geometry to the screen's available rect instead.

Edge-resize and manual maximize/restore are shared with the graph window
via ``FramelessWindowMixin`` (``widgets/frameless.py``) -- see that
module's docstring for the full story, including why the resize event
filter must be installed per-widget on this window's own descendants
rather than QApplication-wide (it broke QWebEngineView).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLayout,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .constants import APP_NAME
from .core.h5_model import DATASET, H5Model, H5ModelError, NodeInfo
from .theme import Palette, ThemeManager
from .widgets.dataset_tabs import DatasetTabsView
from .widgets.file_open_dialog import FileOpenDialog
from .widgets.frameless import FramelessWindowMixin
from .widgets.group_panel import GroupPanel
from .widgets.hierarchy_tree import HierarchyTree
from .widgets.status_bar import StatusBar
from .widgets.title_bar import BAR_HEIGHT, TitleBar

_DEFAULT_W, _DEFAULT_H = 1320, 840
_MIN_W, _MIN_H = 860, 560


class App(FramelessWindowMixin, QWidget):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        self.model: Optional[H5Model] = None
        self.theme = ThemeManager(QApplication.instance())
        self.theme.set_mode("dark")

        self._outer_layout = QVBoxLayout(self)
        outer = self._outer_layout
        # Flush with the window edges (VS Code-style) -- no reserved
        # border. See the module docstring for how edge-resize works
        # without one.
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Qt's default top-level-layout behavior propagates this layout's
        # computed size hints up to the *window's* own min/max size
        # whenever any descendant's geometry changes -- e.g. a splitter
        # drag. We already manage this window's geometry entirely by hand
        # (resize/setGeometry/startSystemMove/startSystemResize, all the
        # way up in _toggle_maximize et al.), so Qt's layout engine also
        # trying to influence window size on top of that is exactly the
        # kind of conflict that could make the window jump during an
        # unrelated internal layout change. Opt out of it entirely.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        self.title_bar = TitleBar(
            self.theme, APP_NAME,
            on_open_file=self._open_file_dialog,
            on_set_appearance=self.theme.set_mode,
            on_minimize=self.showMinimized,
            on_toggle_maximize=self._toggle_maximize,
            on_close=self.close,
        )
        outer.addWidget(self.title_bar)

        self._build_body(outer)

        self.status_bar = StatusBar(self.theme)
        outer.addWidget(self.status_bar)
        self.dataset_tabs.context_changed.connect(self.status_bar.set_context)
        self.dataset_tabs.error_message.connect(lambda msg: self.status_bar.set_message(msg, is_error=True))

        # A resize cursor set on this widget (see mouseMoveEvent below,
        # only meant for the few pixels of bare margin around the window
        # edge) is otherwise *inherited* by every descendant that doesn't
        # set its own cursor -- which is everything here. Trying to time a
        # reset via leaveEvent wasn't reliable through several layers of
        # nested widgets; explicitly giving each direct child its own
        # ArrowCursor breaks the inheritance chain at the source instead,
        # so nothing below this level can ever show the wrong cursor
        # regardless of what App.cursor() currently is.
        for child in (self.title_bar, self.splitter, self.status_bar):
            child.setCursor(Qt.CursorShape.ArrowCursor)

        self._init_frameless(BAR_HEIGHT)

        self._center_on_screen()
        self.theme.register(self._apply_palette)

        if initial_path:
            self.open_file(initial_path)

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)

    # -- layout ------------------------------------------------------

    def _build_body(self, outer: QVBoxLayout) -> None:
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)

        self.tree = HierarchyTree(
            self.theme, on_select=self._on_node_selected, on_activate=self._on_node_activated
        )
        self.splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.group_panel = GroupPanel(
            self.theme,
            on_child_activate=self._activate_path,
            on_child_double_activate=self._activate_path_permanent,
        )
        self.dataset_tabs = DatasetTabsView(self.theme)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.group_panel)
        self.right_stack.addWidget(self.dataset_tabs)
        right_layout.addWidget(self.right_stack)
        self.splitter.addWidget(right)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, _DEFAULT_W - 280])

        outer.addWidget(self.splitter, 1)

    # -- file lifecycle ------------------------------------------------

    def _open_file_dialog(self) -> None:
        start_dir = str(Path(self.model.path).parent) if self.model is not None else str(Path.home())
        dialog = FileOpenDialog(self.theme, start_dir=start_dir, parent=self)
        path = dialog.get_path()
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        try:
            new_model = H5Model(path)
        except Exception as exc:  # noqa: BLE001 - surface any h5py/OS error to the user
            self.status_bar.set_message(f"Could not open file: {exc}", is_error=True)
            return

        if self.model is not None:
            self.dataset_tabs.clear_all()
            self.model.close()

        self.model = new_model
        self.tree.load_file(self.model)
        self.status_bar.set_path(self.model.path)
        self.status_bar.set_message("File loaded")
        # So the user can start navigating the hierarchy with arrow keys
        # right away, without first having to click into the sidebar.
        self.tree.tree.setFocus()

    def _on_node_selected(self, node: NodeInfo) -> None:
        if self.model is None:
            return
        if node.kind == DATASET:
            self._open_dataset(node, permanent=False)
        else:
            # Not dataset_tabs.clear_all() -- that would tear down every
            # loaded tab for no reason just because the user is
            # momentarily looking at a different node; only the
            # status-bar context line (which described whichever dataset
            # tab was active, now hidden) needs to go quiet.
            self.status_bar.set_context("")
            self.group_panel.show_node(self.model, node)
            self.right_stack.setCurrentWidget(self.group_panel)

    def _on_node_activated(self, node: NodeInfo) -> None:
        # Tree double-click -- see HierarchyTree's on_activate. Groups
        # already expand/collapse on double-click via Qt's own default
        # QTreeView behavior; this only has anything to do for datasets.
        if self.model is None or node.kind != DATASET:
            return
        self._open_dataset(node, permanent=True)

    def _open_dataset(self, node: NodeInfo, permanent: bool) -> None:
        try:
            self.dataset_tabs.open_dataset(self.model, node, node.path, permanent=permanent)
        except H5ModelError as exc:
            self.status_bar.set_message(str(exc), is_error=True)
            return
        self.right_stack.setCurrentWidget(self.dataset_tabs)

    def _activate_path(self, path: str) -> None:
        self.tree.select_path(path)

    def _activate_path_permanent(self, path: str) -> None:
        # Group panel's child-row double-click -- mirrors
        # _on_node_activated for datasets reached this way instead of via
        # the tree directly. select_path already fires _on_node_selected
        # (a preview open) through the tree's own selectionChanged; this
        # then pins/promotes that same dataset to a permanent tab, the
        # same order double-clicking a tree row happens in.
        self.tree.select_path(path)
        if self.model is None:
            return
        node = self.model.node_info(path)
        if node.kind == DATASET:
            self._open_dataset(node, permanent=True)

    def closeEvent(self, event) -> None:
        self._teardown_frameless()
        self.dataset_tabs.clear_all()
        if self.model is not None:
            self.model.close()
        super().closeEvent(event)

    # -- window chrome -----------------------------------------------------
    # Maximize/restore and edge-resize live in FramelessWindowMixin, shared
    # with the graph window -- see widgets/frameless.py.

    def _on_maximize_changed(self, maximized: bool) -> None:
        self.title_bar.set_maximized(maximized)

    # -- theming ---------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        # header_bg, not window_bg: there's no reserved margin any more
        # for this to show through, but it's still App's own background
        # underneath every child widget, so keeping it matched to the
        # title/status bar avoids even a one-frame flash of a mismatched
        # color during resize/relayout.
        self.setStyleSheet(
            f"""
            App {{ background-color: {palette.header_bg}; }}
            QWidget {{ color: {palette.text}; }}
            QSplitter::handle {{
                background-color: {palette.splitter};
            }}
            QSplitter::handle:hover {{
                background-color: {palette.accent};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background-color: {palette.accent};
                min-height: 24px;
                border-radius: 3px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
                border: none;
                background: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 6px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal {{
                background-color: {palette.accent};
                min-width: 24px;
                border-radius: 3px;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
                border: none;
                background: none;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
            """
        )


def main(initial_path: Optional[str] = None) -> None:
    # Required by Qt WebEngine (see widgets/graph_window.py) *before* the
    # QApplication exists -- without it, QWebEngineView's separate GPU/
    # renderer processes don't share GL contexts with the main process,
    # which showed up here as sporadic Chromium GPU-command-buffer errors
    # and crashes around graph windows. Harmless no-op for the rest of the
    # app if WebEngine's system dependencies aren't installed.
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    window = App(initial_path)
    window.show()
    app.exec()
