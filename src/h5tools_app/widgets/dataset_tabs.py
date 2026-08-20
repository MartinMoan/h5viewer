"""VS Code-style tab strip for viewing multiple open datasets at once.

Single-clicking a dataset in the tree opens it in a "preview" tab --
shown with its title in italics -- that the next single-clicked dataset
reuses/replaces, exactly like VS Code's editor preview tab. Double-
clicking a dataset (or double-clicking the tab of one already open as
the preview) "pins" it as a permanent tab instead, so it's no longer
replaced by the next preview; a dataset not already open anywhere then
always gets its own new tab rather than reusing an existing one -- see
``open_dataset`` for the exact rules, which mirror VS Code's explorer
behavior move for move.

App only ever talks to this widget, never to an individual tab's
``DatasetTableView`` directly -- ``context_changed``/``error_message``
re-emit whichever tab is currently active's own signals, the same two
signals ``DatasetTableView`` used to expose directly before there were
multiple of them.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QLabel,
    QStackedWidget,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.h5_model import H5Model, NodeInfo
from ..theme import Palette, ThemeManager
from .dataset_table import DatasetTableView


class _PreviewTabBar(QTabBar):
    """Plain QTabBar, except the tab whose widget is currently the
    "preview" tab (see DatasetTabsView) is painted in italics --
    mirroring VS Code's italicized title for its single preview editor
    tab. QTabBar has no public per-tab font API, so each tab's label is
    painted by hand instead of left to the style: the tab's shape
    (background/border; the close button is a separate overlaid child
    widget Qt manages on its own and is unaffected by any of this) is
    still drawn by the style as usual, just with its text blanked out
    first so the style doesn't also draw it once in the tab bar's one
    shared font.
    """

    def __init__(self, is_preview: Callable[[int], bool], parent=None):
        super().__init__(parent)
        self._is_preview = is_preview
        self._text_color = QColor(Qt.GlobalColor.black)
        self._selected_text_color = QColor(Qt.GlobalColor.black)

    def set_colors(self, text: str, selected_text: str) -> None:
        self._text_color = QColor(text)
        self._selected_text_color = QColor(selected_text)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            label = option.text
            # Computed from the *populated* option (icon/close-button
            # reservations depend on it) -- blanking option.text happens
            # only afterwards, just before drawing the shape.
            text_rect = self.style().subElementRect(QStyle.SubElement.SE_TabBarTabText, option, self)
            option.text = ""
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)

            font = QFont(self.font())
            font.setItalic(self._is_preview(index))
            painter.setFont(font)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            painter.setPen(self._selected_text_color if selected else self._text_color)
            elided = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, elided)


class DatasetTabsView(QWidget):
    context_changed = Signal(str)
    error_message = Signal(str)

    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._palette: Palette = theme.palette
        # Keyed by each tab's DatasetTableView instance, not by tab index
        # -- indices shift every time an earlier tab closes, so anything
        # that needs to survive that has to be keyed off something stable.
        self._tab_paths: dict[QWidget, str] = {}
        self._tab_contexts: dict[QWidget, str] = {}
        self._preview_view: Optional[QWidget] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.empty_label = QLabel("Select a dataset from the tree to view its contents")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._tabs = QTabWidget()
        self._tab_bar = _PreviewTabBar(self._is_preview_index)
        self._tabs.setTabBar(self._tab_bar)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(self._on_current_changed)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self._tabs)
        outer.addWidget(self.stack, 1)

        self._update_empty_state()
        theme.register(self._apply_palette)

    # -- public API --------------------------------------------------------

    def open_dataset(self, model: H5Model, node: NodeInfo, path: str, *, permanent: bool) -> None:
        """Opens ``path``, following VS Code's preview/pin rules:

        * Already open somewhere (preview or permanent)? Just switch to
          that tab. If this is also a ``permanent`` open of the current
          preview tab, pin it in place instead of leaving it as preview.
        * Not open, and ``permanent``? Always a brand-new tab -- never
          touches whatever the current preview tab happens to be.
        * Not open, and not ``permanent`` (a plain single click)? Reuses
          the existing preview tab if there is one (replacing its
          content/title), otherwise opens a new preview tab.

        Raises ``H5ModelError`` (from the underlying ``DatasetTableView
        .load()``) if the dataset can't be loaded -- before any tab is
        created or changed, so a failed open leaves every existing tab
        untouched. Callers should show the error to the user themselves
        (see ``App._open_dataset``).
        """
        existing = self._view_for_path(path)
        if existing is not None:
            self._tabs.setCurrentWidget(existing)
            if permanent and existing is self._preview_view:
                self._pin(existing)
            return

        if permanent:
            self._open_new_tab(model, node, path, preview=False)
        elif self._preview_view is not None:
            self._replace_preview(model, node, path)
        else:
            self._open_new_tab(model, node, path, preview=True)

    def clear_all(self) -> None:
        """Tears down every open tab -- used when a different file is
        opened, or the app is closing, so no DatasetSource/QTimer left
        over from the previous file keeps running in the background."""
        for view in list(self._tab_paths.keys()):
            self._close_view(view)
        self._update_empty_state()

    # -- internals -----------------------------------------------------------

    def _view_for_path(self, path: str) -> Optional[QWidget]:
        for view, p in self._tab_paths.items():
            if p == path:
                return view
        return None

    def _is_preview_index(self, index: int) -> bool:
        view = self._tabs.widget(index)
        return view is not None and view is self._preview_view

    def _open_new_tab(self, model: H5Model, node: NodeInfo, path: str, *, preview: bool) -> None:
        view = DatasetTableView(self._theme)
        view.load(model, path)  # may raise H5ModelError -- before any tab bookkeeping changes
        view.context_changed.connect(lambda text, v=view: self._on_view_context(v, text))
        view.error_message.connect(lambda msg, v=view: self._on_view_error(v, msg))

        index = self._tabs.addTab(view, node.name)
        self._tabs.setTabToolTip(index, path)
        self._tab_paths[view] = path
        if preview:
            if self._preview_view is not None:
                # open_dataset only calls this with preview=True when
                # there's no existing preview tab, but guard anyway so
                # there's never more than one at once.
                self._pin(self._preview_view)
            self._preview_view = view
        self._tabs.setCurrentWidget(view)
        self._update_empty_state()
        self._tab_bar.update()

    def _replace_preview(self, model: H5Model, node: NodeInfo, path: str) -> None:
        view = self._preview_view
        assert view is not None
        view.load(model, path)  # may raise H5ModelError -- tab title/path untouched if it does
        self._tab_paths[view] = path
        index = self._tabs.indexOf(view)
        self._tabs.setTabText(index, node.name)
        self._tabs.setTabToolTip(index, path)
        self._tabs.setCurrentWidget(view)

    def _pin(self, view: QWidget) -> None:
        if self._preview_view is view:
            self._preview_view = None
            self._tab_bar.update()

    def _on_tab_close_requested(self, index: int) -> None:
        view = self._tabs.widget(index)
        if view is not None:
            self._close_view(view)
        self._update_empty_state()

    def _close_view(self, view: QWidget) -> None:
        index = self._tabs.indexOf(view)
        if index >= 0:
            self._tabs.removeTab(index)
        if view is self._preview_view:
            self._preview_view = None
        self._tab_paths.pop(view, None)
        self._tab_contexts.pop(view, None)
        view.clear()  # tears down its DatasetSource/timer before it's gone
        view.deleteLater()

    def _on_view_context(self, view: QWidget, text: str) -> None:
        self._tab_contexts[view] = text
        if self._tabs.currentWidget() is view:
            self.context_changed.emit(text)

    def _on_view_error(self, view: QWidget, msg: str) -> None:
        if self._tabs.currentWidget() is view:
            self.error_message.emit(msg)

    def _on_current_changed(self, index: int) -> None:
        view = self._tabs.widget(index)
        self.context_changed.emit(self._tab_contexts.get(view, ""))

    def _update_empty_state(self) -> None:
        self.stack.setCurrentWidget(self._tabs if self._tabs.count() else self.empty_label)

    # -- theming -------------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.empty_label.setStyleSheet(f"color: {palette.subtext}; font-size: 12pt;")
        self._tab_bar.set_colors(palette.subtext, palette.text)
        self._tabs.setStyleSheet(
            f"""
            QTabWidget::pane {{
                border: none;
                border-top: 1px solid {palette.grid_line};
                background-color: {palette.body_bg};
                top: -1px;
            }}
            QTabBar::tab {{
                background-color: {palette.header_bg};
                padding: 6px 14px;
                border: none;
                border-right: 1px solid {palette.grid_line};
            }}
            QTabBar::tab:selected {{
                background-color: {palette.body_bg};
                border-bottom: 2px solid {palette.accent};
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {palette.row_hover};
            }}
            """
        )
