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

Edge-resize has no reserved margin -- the content sits flush with the
window edges (VS Code-style), which means there's no bare App-widget
surface left anywhere for a plain ``mousePressEvent`` override to catch
resize drags on. Instead, a ``QApplication``-wide event filter inspects
every mouse press/move in the whole app and checks the *global* cursor
position against this window's edges, regardless of which child widget
the event actually landed on. The title bar (and everything in it -- the
window control buttons, the menu items) is deliberately excluded from
this: it already handles drag-to-move and its own clicks, and with zero
margin its top ~6px overlaps those controls, so treating that row as
resize territory too would make clicking near the top of a button
sometimes start a resize instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QCursor
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
from .widgets.dataset_table import DatasetTableView
from .widgets.file_open_dialog import FileOpenDialog
from .widgets.group_panel import GroupPanel
from .widgets.hierarchy_tree import HierarchyTree
from .widgets.status_bar import StatusBar
from .widgets.title_bar import BAR_HEIGHT, TitleBar

_DEFAULT_W, _DEFAULT_H = 1320, 840
_MIN_W, _MIN_H = 860, 560
_RESIZE_ZONE = 6  # px from a window edge that counts as "start a resize drag"


class App(QWidget):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)
        self.setMouseTracking(True)

        self.model: Optional[H5Model] = None
        self._maximized = False
        self._restore_geometry: Optional[QRect] = None
        self._override_cursor_active = False
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
        self.table_view.context_changed.connect(self.status_bar.set_context)

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

        # With zero margins, every pixel of the window is covered by some
        # child widget -- there's no bare App surface left for a plain
        # mouseMoveEvent override to see hover motion on. Qt only
        # dispatches MouseMove without a button held to widgets that opt
        # in, so every descendant needs tracking on for the QApplication-
        # wide event filter (below) to observe hover position anywhere in
        # the window, regardless of which child is actually under the
        # cursor.
        for w in self.findChildren(QWidget):
            w.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)

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

        self.tree = HierarchyTree(self.theme, on_select=self._on_node_selected)
        self.splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.group_panel = GroupPanel(self.theme, on_child_activate=self._activate_path)
        self.table_view = DatasetTableView(self.theme)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.group_panel)
        self.right_stack.addWidget(self.table_view)
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
            self.table_view.clear()
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
            try:
                self.table_view.load(self.model, node.path)
            except H5ModelError as exc:
                self.status_bar.set_message(str(exc), is_error=True)
                return
            self.right_stack.setCurrentWidget(self.table_view)
        else:
            # Not table_view.clear() -- that would tear down its loaded
            # DatasetSource for no reason just because the user is
            # momentarily looking at a different node; only the status-bar
            # context line (which described the dataset table, now hidden)
            # needs to go quiet.
            self.status_bar.set_context("")
            self.group_panel.show_node(self.model, node)
            self.right_stack.setCurrentWidget(self.group_panel)

    def _activate_path(self, path: str) -> None:
        self.tree.select_path(path)

    def closeEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        self.table_view.clear()
        if self.model is not None:
            self.model.close()
        super().closeEvent(event)

    # -- window chrome: maximize/restore ---------------------------------

    def _toggle_maximize(self) -> None:
        # Not showMaximized(): on a frameless X11 window that reliably
        # ends up offset from the screen edge, since the WM's maximize
        # calculation assumes decorations that don't exist here. Doing it
        # ourselves with the screen's exact available geometry sidesteps
        # that entirely.
        #
        # setGeometry(rect) below is already a single atomic call (move
        # and resize together, not two separate calls) -- the "moves,
        # then resizes" look reported on X11/WSLg is Qt painting the
        # in-between frames as the platform window catches up to the new
        # geometry, not two separate geometry changes on our end.
        # setUpdatesEnabled(False) suppresses those intermediate repaints
        # so only the final, fully-laid-out frame ever hits the screen --
        # the whole splitter/tree/table subtree would otherwise be
        # relaid-out and repainted at least once mid-transition for
        # nothing, which is also most of where the sluggishness comes
        # from, not the geometry change itself.
        self.setUpdatesEnabled(False)
        try:
            if self._maximized:
                if self._restore_geometry is not None:
                    self.setGeometry(self._restore_geometry)
                self._maximized = False
            else:
                screen = self.screen() or QApplication.primaryScreen()
                self._restore_geometry = self.geometry()
                self.setGeometry(screen.availableGeometry())
                self._maximized = True
        finally:
            self.setUpdatesEnabled(True)
        self.title_bar.set_maximized(self._maximized)

    # -- window chrome: edge resize --------------------------------------
    #
    # There's no reserved margin any more (see __init__), so no bare
    # App-widget surface exists for a plain mousePressEvent/mouseMoveEvent
    # override to catch resize drags on -- every pixel belongs to some
    # child widget. Instead a QApplication-wide event filter (installed in
    # __init__, see eventFilter below) inspects every mouse press/move in
    # the whole app and checks the *global* cursor position against this
    # window's edges, regardless of which child widget the event actually
    # landed on. The title bar row is excluded entirely: it already
    # handles drag-to-move and its own clicks, and with zero margin its
    # top few px overlap those controls, so treating that row as resize
    # territory too would make clicking near the top of a button
    # sometimes start a resize instead.

    def _edges_at(self, x: int, y: int) -> Qt.Edges:
        m = _RESIZE_ZONE
        edges = Qt.Edges()
        if x <= m:
            edges |= Qt.Edge.LeftEdge
        if x >= self.width() - m:
            edges |= Qt.Edge.RightEdge
        if y <= m:
            edges |= Qt.Edge.TopEdge
        if y >= self.height() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _cursor_for_edges(self, edges: Qt.Edges) -> Optional[Qt.CursorShape]:
        # Standard Qt cursor shapes, not custom-drawn ones: this defers to
        # whatever the platform provides for "resize" cursors. A prior
        # attempt drew custom bitmaps instead, reasoning that WSLg's X11
        # session has no configured cursor theme so Qt falls back to its
        # own bundled bitmaps -- but that made things worse, not better,
        # so back to the platform default here.
        has_left = bool(edges & Qt.Edge.LeftEdge)
        has_right = bool(edges & Qt.Edge.RightEdge)
        has_top = bool(edges & Qt.Edge.TopEdge)
        has_bottom = bool(edges & Qt.Edge.BottomEdge)
        if (has_left and has_top) or (has_right and has_bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (has_right and has_top) or (has_left and has_bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if has_left or has_right:
            return Qt.CursorShape.SizeHorCursor
        if has_top or has_bottom:
            return Qt.CursorShape.SizeVerCursor
        return None

    def _edges_for_global_pos(self, global_pos) -> Qt.Edges:
        if self._maximized:
            return Qt.Edges()
        pos = self.mapFromGlobal(global_pos)
        if not self.rect().contains(pos) or pos.y() < BAR_HEIGHT:
            return Qt.Edges()
        return self._edges_at(pos.x(), pos.y())

    def _set_resize_cursor(self, cursor: Optional[Qt.CursorShape]) -> None:
        # QApplication.override cursor rather than setCursor() on whatever
        # widget happens to be under the pointer: the widget under the
        # pointer changes constantly as the mouse crosses child boundaries
        # near an edge, and there's no single owner to reliably reset
        # afterwards the way there was when App had its own bare margin.
        if cursor is not None:
            if self._override_cursor_active:
                QApplication.changeOverrideCursor(QCursor(cursor))
            else:
                QApplication.setOverrideCursor(QCursor(cursor))
                self._override_cursor_active = True
        elif self._override_cursor_active:
            QApplication.restoreOverrideCursor()
            self._override_cursor_active = False

    def eventFilter(self, obj, event) -> bool:
        etype = event.type()
        if etype == QEvent.Type.MouseMove and isinstance(obj, QWidget) and obj.window() is self:
            edges = self._edges_for_global_pos(event.globalPosition().toPoint())
            self._set_resize_cursor(self._cursor_for_edges(edges))
        elif (
            etype == QEvent.Type.MouseButtonPress
            and isinstance(obj, QWidget)
            and obj.window() is self
            and event.button() == Qt.MouseButton.LeftButton
        ):
            edges = self._edges_for_global_pos(event.globalPosition().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    return True
        return super().eventFilter(obj, event)

    def leaveEvent(self, event) -> None:
        # Fires when the pointer leaves the window's total bounds (not
        # when it moves between children within it), which is the right
        # moment to make sure a resize cursor doesn't get stuck on.
        self._set_resize_cursor(None)
        super().leaveEvent(event)

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
    app = QApplication.instance() or QApplication([])
    window = App(initial_path)
    window.show()
    app.exec()
