"""VS Code-style tab strip for viewing multiple open groups/datasets at
once.

Single-clicking a node in the tree opens it in a "preview" tab -- shown
with its title in italics -- that the next single-clicked node reuses/
replaces, exactly like VS Code's editor preview tab. Double-clicking a
node (or double-clicking the tab of one already open as the preview)
"pins" it as a permanent tab instead, so it's no longer replaced by the
next preview; a node not already open anywhere then always gets its own
new tab rather than reusing an existing one -- see ``open_node`` for the
exact rules, which mirror VS Code's explorer behavior move for move.

Both datasets (a ``DatasetTableView`` tab) and groups (a ``GroupPanel``
tab) live in the same strip -- selecting a group no longer swaps out
whatever dataset tabs are already open the way a separate stacked pane
used to; it just becomes another tab alongside them, so a pinned dataset
tab is never hidden just because the user is browsing the hierarchy.

App only ever talks to this widget, never to an individual tab's content
widget directly -- ``context_changed``/``error_message`` re-emit
whichever tab is currently active's own signals, the same two signals
``DatasetTableView`` used to expose directly before there were multiple
tabs (of possibly differing kinds) in play.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QLabel,
    QStackedWidget,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..core.h5_model import DATASET, GROUP, H5Model, NodeInfo
from ..theme import Palette, ThemeManager
from .dataset_table import DatasetTableView
from .group_panel import GroupPanel


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

    def __init__(
        self,
        theme: ThemeManager,
        on_child_activate: Callable[[str], None],
        on_child_double_activate: Callable[[str], None],
        parent=None,
    ):
        super().__init__(parent)
        self._theme = theme
        self._palette: Palette = theme.palette
        # Forwarded to every GroupPanel tab this view creates -- a child
        # row clicked inside one drives the tree/this view exactly like
        # clicking that same node in the sidebar would (see App).
        self._on_child_activate = on_child_activate
        self._on_child_double_activate = on_child_double_activate
        # Keyed by each tab's content widget instance, not by tab index
        # -- indices shift every time an earlier tab closes, so anything
        # that needs to survive that has to be keyed off something stable.
        self._tab_paths: dict[QWidget, str] = {}
        self._tab_contexts: dict[QWidget, str] = {}
        self._preview_view: Optional[QWidget] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.empty_label = QLabel("Select a group or dataset from the tree to view its contents")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._tabs = QTabWidget()
        self._tab_bar = _PreviewTabBar(self._is_preview_index)
        self._tabs.setTabBar(self._tab_bar)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(True)
        self._tabs.currentChanged.connect(self._on_current_changed)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self._tabs)
        outer.addWidget(self.stack, 1)

        self._update_empty_state()
        theme.register(self._apply_palette)

    # -- public API --------------------------------------------------------

    def open_node(self, model: H5Model, node: NodeInfo, *, permanent: bool) -> None:
        """Opens ``node``, following VS Code's preview/pin rules:

        * Already open somewhere (preview or permanent)? Just switch to
          that tab. If this is also a ``permanent`` open of the current
          preview tab, pin it in place instead of leaving it as preview.
        * Not open, and ``permanent``? Always a brand-new tab -- never
          touches whatever the current preview tab happens to be.
        * Not open, and not ``permanent`` (a plain single click)? Reuses
          the existing preview tab if there is one (replacing its
          content/title -- swapping its widget type too, if the preview
          tab currently holds the other kind of content), otherwise opens
          a new preview tab.

        Raises ``H5ModelError`` (from the underlying ``DatasetTableView
        .load()``) if a dataset can't be loaded -- before any tab is
        created or changed, so a failed open leaves every existing tab
        untouched. Callers should show the error to the user themselves
        (see ``App._open_node``).
        """
        existing = self._view_for_path(node.path)
        if existing is not None:
            self._tabs.setCurrentWidget(existing)
            if permanent and existing is self._preview_view:
                self._pin(existing)
            return

        if permanent:
            self._open_new_tab(model, node, preview=False)
        elif self._preview_view is not None:
            self._replace_preview(model, node)
        else:
            self._open_new_tab(model, node, preview=True)

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

    def _make_view(self, model: H5Model, node: NodeInfo) -> QWidget:
        if node.kind == DATASET:
            view = DatasetTableView(self._theme)
            view.load(model, node.path)  # may raise H5ModelError
            view.context_changed.connect(lambda text, v=view: self._on_view_context(v, text))
            view.error_message.connect(lambda msg, v=view: self._on_view_error(v, msg))
            return view
        view = GroupPanel(
            self._theme,
            on_child_activate=self._on_child_activate,
            on_child_double_activate=self._on_child_double_activate,
        )
        view.show_node(model, node)
        return view

    def _refresh_view(self, view: QWidget, model: H5Model, node: NodeInfo) -> None:
        if isinstance(view, DatasetTableView):
            view.load(model, node.path)  # may raise H5ModelError
        else:
            view.show_node(model, node)

    @staticmethod
    def _tab_title(model: H5Model, node: NodeInfo) -> str:
        if node.kind != DATASET and node.name == "/":
            return Path(model.path).name
        return node.name

    def _tab_icon_color(self, node: NodeInfo) -> str:
        return self._palette.accent if node.kind == DATASET else self._palette.subtext

    def _make_close_button(self, view: QWidget) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("tabCloseButton")
        btn.setAutoRaise(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("Close")
        btn.setIconSize(QSize(10, 10))
        btn.setIcon(icons.icon(icons.CLOSE, self._palette.subtext, 10))
        btn.clicked.connect(lambda: self._close_tab(view))
        return btn

    def _close_tab(self, view: QWidget) -> None:
        self._close_view(view)
        self._update_empty_state()

    def _open_new_tab(self, model: H5Model, node: NodeInfo, *, preview: bool) -> None:
        view = self._make_view(model, node)  # may raise H5ModelError -- before any tab bookkeeping changes
        index = self._tabs.addTab(view, self._tab_title(model, node))
        self._tabs.setTabToolTip(index, node.path)
        self._tabs.setTabIcon(index, icons.icon(node.kind, self._tab_icon_color(node), 14))
        self._tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, self._make_close_button(view))
        self._tab_paths[view] = node.path
        if preview:
            if self._preview_view is not None:
                # open_node only calls this with preview=True when
                # there's no existing preview tab, but guard anyway so
                # there's never more than one at once.
                self._pin(self._preview_view)
            self._preview_view = view
        self._tabs.setCurrentWidget(view)
        self._update_empty_state()
        self._tab_bar.update()
        self._refresh_tab_titles()

    def _replace_preview(self, model: H5Model, node: NodeInfo) -> None:
        old_view = self._preview_view
        assert old_view is not None
        same_kind = isinstance(old_view, DatasetTableView) == (node.kind == DATASET)

        if same_kind:
            self._refresh_view(old_view, model, node)  # may raise -- tab bookkeeping untouched if it does
            view = old_view
            self._tab_paths.pop(old_view, None)
        else:
            # The preview tab is switching between a dataset and a group
            # (or vice versa) -- its widget type has to change, so the
            # old one is swapped out for a freshly made one of the right
            # kind, in the same tab position.
            view = self._make_view(model, node)  # may raise -- old_view/tab untouched if it does
            index = self._tabs.indexOf(old_view)
            self._tabs.removeTab(index)
            self._tab_paths.pop(old_view, None)
            self._tab_contexts.pop(old_view, None)
            old_view.deleteLater()
            self._tabs.insertTab(index, view, "")
            self._tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, self._make_close_button(view))
            self._preview_view = view

        index = self._tabs.indexOf(view)
        self._tab_paths[view] = node.path
        self._tabs.setTabText(index, self._tab_title(model, node))
        self._tabs.setTabToolTip(index, node.path)
        self._tabs.setTabIcon(index, icons.icon(node.kind, self._tab_icon_color(node), 14))
        self._tabs.setCurrentWidget(view)
        self._refresh_tab_titles()

    def _pin(self, view: QWidget) -> None:
        if self._preview_view is view:
            self._preview_view = None
            self._tab_bar.update()

    def _close_view(self, view: QWidget) -> None:
        index = self._tabs.indexOf(view)
        if index >= 0:
            self._tabs.removeTab(index)
        if view is self._preview_view:
            self._preview_view = None
        self._tab_paths.pop(view, None)
        self._tab_contexts.pop(view, None)
        if isinstance(view, DatasetTableView):
            view.clear()  # tears down its DatasetSource/timer before it's gone
        view.deleteLater()
        self._refresh_tab_titles()

    def _refresh_tab_titles(self) -> None:
        """Disambiguates dataset tabs that share a bare name (e.g. two
        "readings" datasets under different groups) by prefixing each
        with just enough of its group path to tell them apart -- up to,
        but not including, the group they have in common. Tabs whose
        name is unique among currently open dataset tabs, and every
        group tab, are untouched."""
        by_name: dict[str, list[str]] = {}
        for view, path in self._tab_paths.items():
            if isinstance(view, DatasetTableView):
                by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)

        for i in range(self._tabs.count()):
            view = self._tabs.widget(i)
            if not isinstance(view, DatasetTableView):
                continue
            path = self._tab_paths.get(view)
            if path is None:
                continue
            colliding = by_name.get(path.rsplit("/", 1)[-1], [])
            title = self._disambiguated_title(path, colliding) if len(colliding) > 1 else path.rsplit("/", 1)[-1]
            self._tabs.setTabText(i, title)

    @staticmethod
    def _disambiguated_title(path: str, colliding_paths: list[str]) -> str:
        part_lists = [[p for p in cp.split("/") if p] for cp in colliding_paths]
        common = 0
        for segment in zip(*part_lists):
            if len(set(segment)) > 1:
                break
            common += 1
        my_parts = [p for p in path.split("/") if p]
        return "/".join(my_parts[common:])

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
        for i in range(self._tabs.count()):
            view = self._tabs.widget(i)
            kind = DATASET if isinstance(view, DatasetTableView) else GROUP
            color = palette.accent if kind == DATASET else palette.subtext
            self._tabs.setTabIcon(i, icons.icon(kind, color, 14))
            btn = self._tab_bar.tabButton(i, QTabBar.ButtonPosition.RightSide)
            if isinstance(btn, QToolButton):
                btn.setIcon(icons.icon(icons.CLOSE, palette.subtext, 10))
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
                padding: 6px 14px 6px 14px;
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
            QToolButton#tabCloseButton {{
                border: none;
                background: transparent;
                border-radius: 3px;
                margin: 0px 4px 0px 4px;
            }}
            QToolButton#tabCloseButton:hover {{
                background-color: {palette.row_hover};
            }}
            """
        )
