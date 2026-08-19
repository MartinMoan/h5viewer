"""Shared plumbing for a frameless, hand-chrome'd top-level window: manual
maximize/restore (not ``showMaximized()`` -- see ``_toggle_maximize``
below) and edge-resize via a per-widget-scoped event filter.

A plain mixin, not a ``QObject``/``QWidget`` subclass itself -- Qt's
meta-object system doesn't support combining two QObject-derived base
classes, so this only works mixed in alongside a *single* QWidget-derived
base: ``class MyWindow(FramelessWindowMixin, QWidget): ...``, calling
``self._init_frameless(bar_height)`` near the end of ``__init__`` (after
every child widget that should be resize-cursor-aware already exists) and
``self._teardown_frameless()`` from ``closeEvent``.

Originally lived only in ``App`` (the main window); extracted once a
second frameless window (the graph window, ``widgets/graph_window.py``)
needed the exact same behavior -- including a hard-won fix: the resize
event filter must be installed per-widget on this window's own
descendants, never ``QApplication``-wide. A QApplication-wide filter
observes literally every event for every object in the whole process,
which turned out to include QWebEngineView's internally-managed objects
even though those belong to a completely unrelated top-level window --
confirmed by direct testing, a *trivial, do-nothing* QApplication-wide
filter reproduces an unconditional segfault the moment any QWebEngineView
exists anywhere in the app. Installing per-widget means this filter is
only ever invoked for events belonging to this window's own widget tree.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget

_RESIZE_ZONE = 6  # px from a window edge that counts as "start a resize drag"


class FramelessWindowMixin:
    def _init_frameless(self, bar_height: int) -> None:
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self._bar_height = bar_height
        self._maximized = False
        self._restore_geometry: Optional[QRect] = None
        self._override_cursor_active = False

        # With zero layout margin, every pixel of the window is covered by
        # some child widget -- there's no bare surface left for a plain
        # mouseMoveEvent override to see hover motion on, so every
        # descendant needs mouse tracking on and the filter installed
        # directly, regardless of which child is actually under the
        # cursor. See the module docstring for why this is per-widget,
        # not QApplication-wide.
        self._filtered_widgets = [self] + self.findChildren(QWidget)
        for w in self._filtered_widgets:
            w.setMouseTracking(True)
            w.installEventFilter(self)

    def _teardown_frameless(self) -> None:
        for w in self._filtered_widgets:
            w.removeEventFilter(self)

    # -- maximize/restore --------------------------------------------------

    def _toggle_maximize(self) -> None:
        # Not showMaximized(): on a frameless X11 window that reliably
        # ends up offset from the screen edge, since the WM's maximize
        # calculation assumes decorations that don't exist here. Doing it
        # ourselves with the screen's exact available geometry sidesteps
        # that entirely.
        #
        # setGeometry(rect) below is already a single atomic call (move
        # and resize together, not two separate calls) -- setUpdatesEnabled
        # (False) suppresses intermediate repaints while the platform
        # window catches up to the new geometry, so only the final,
        # fully-laid-out frame ever hits the screen.
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
        self._on_maximize_changed(self._maximized)

    def _on_maximize_changed(self, maximized: bool) -> None:
        """Override to update a title bar's restore/maximize icon."""

    # -- edge resize ---------------------------------------------------

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
        # Standard Qt cursor shapes, not custom-drawn ones -- see App's
        # original history for why (a custom-bitmap attempt made things
        # worse under WSLg, not better).
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
        if not self.rect().contains(pos) or pos.y() < self._bar_height:
            return Qt.Edges()
        return self._edges_at(pos.x(), pos.y())

    def _set_resize_cursor(self, cursor: Optional[Qt.CursorShape]) -> None:
        # QApplication.override cursor rather than setCursor() on whatever
        # widget happens to be under the pointer: the widget under the
        # pointer changes constantly as the mouse crosses child boundaries
        # near an edge, and there's no single owner to reliably reset
        # afterwards.
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
