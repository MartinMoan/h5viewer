"""Left-hand explorer pane: a lazily-populated group/dataset tree, similar
in spirit to VS Code's file explorer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeView, QVBoxLayout, QWidget

from .. import icons
from ..core.h5_model import DATASET, GROUP, H5Model, NodeInfo
from ..theme import Palette, ThemeManager

ICON_SIZE = 15
NODE_ROLE = Qt.ItemDataRole.UserRole + 1
DUMMY_ROLE = Qt.ItemDataRole.UserRole + 2


def _shape_summary(node: NodeInfo) -> str:
    """Used by the group overview panel and the file-open dialog -- not by
    this tree itself. A previous version of this tree also showed this as
    a second column next to each row, but sizing that column to fit its
    widest value (a dataset's full "shape · dtype" string) padded out
    every row to match, including plain group rows that only needed "N
    items" -- compounding with indentation into a sidebar that ballooned
    on deep hierarchies for no real benefit, since this info is already
    shown when a dataset is actually selected."""
    if node.kind == DATASET:
        shape = "scalar" if node.shape == () else "×".join(str(d) for d in node.shape)
        return f"{shape}  {node.dtype}"
    if node.n_children == 1:
        return "1 item"
    return f"{node.n_children} items"


class HierarchyTree(QWidget):
    def __init__(self, theme: ThemeManager, on_select: Callable[[NodeInfo], None], parent=None):
        super().__init__(parent)
        self._on_select = on_select
        self._model_ref: Optional[H5Model] = None
        self._items: dict[str, QStandardItem] = {}
        self._palette: Palette = theme.palette
        # Otherwise transparent, so the top/left layout margin below would
        # expose App's own (darker) background rather than the tree's own
        # panel color -- reading as the whole pane having shifted position
        # rather than just the tree content getting a bit of breathing
        # room within it.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 10, 0, 4)

        self.model = QStandardItemModel()

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setFrameShape(QTreeView.Shape.NoFrame)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # ResizeToContents (not Stretch): names should always be shown in
        # full, never truncated to fit the pane -- the sidebar grows to
        # match instead (see _apply_min_width below).
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(self.tree)

        self.tree.expanded.connect(self._on_expanded)
        self.tree.collapsed.connect(self._on_collapsed)
        self.tree.selectionModel().selectionChanged.connect(self._on_selection_changed)

        theme.register(self._apply_palette)

    # -- public API ------------------------------------------------------

    def load_file(self, model: H5Model) -> None:
        self.clear()
        self._model_ref = model
        root_info = model.root_info()

        root_item = self._make_name_item(root_info, Path(model.path).name)
        self.model.appendRow(root_item)
        self._items["/"] = root_item

        self._populate_children("/", root_item)
        self.tree.expand(root_item.index())
        self.tree.setCurrentIndex(root_item.index())  # triggers _on_selection_changed
        self._apply_min_width()

    def clear(self) -> None:
        self.model.removeRows(0, self.model.rowCount())
        self._items.clear()
        self._model_ref = None

    def select_path(self, path: str) -> None:
        """Programmatically reveal and select ``path``, lazily expanding
        any ancestor groups that haven't been populated yet. Used when a
        child row is clicked in the group overview panel."""
        if self._model_ref is None or "/" not in self._items:
            return

        parts = [p for p in path.split("/") if p]
        current = "/"
        for i in range(len(parts) + 1):
            if i > 0:
                current = "/" + "/".join(parts[:i])
            item = self._items.get(current)
            if item is None:
                return
            if item.rowCount() == 1 and item.child(0).data(DUMMY_ROLE):
                item.removeRow(0)
                self._populate_children(current, item)

        item = self._items.get(path)
        if item is None:
            return
        index = item.index()
        parent = index.parent()
        while parent.isValid():
            self.tree.expand(parent)
            parent = parent.parent()
        self.tree.setCurrentIndex(index)  # triggers _on_selection_changed
        self.tree.scrollTo(index)
        self._apply_min_width()

    # -- internals ---------------------------------------------------------

    def _icon_color(self, kind: str) -> str:
        return self._palette.subtext if kind == GROUP else self._palette.accent

    def _make_name_item(self, node: NodeInfo, label: str) -> QStandardItem:
        item = QStandardItem(label)
        item.setEditable(False)
        item.setData(node, NODE_ROLE)
        item.setIcon(icons.icon(node.kind, self._icon_color(node.kind), ICON_SIZE))
        return item

    def _populate_children(self, path: str, parent_item: QStandardItem) -> None:
        if self._model_ref is None:
            return
        for child in self._model_ref.list_children(path):
            name_item = self._make_name_item(child, child.name)
            parent_item.appendRow(name_item)
            self._items[child.path] = name_item
            if child.kind == GROUP and (child.n_children or 0) > 0:
                dummy = QStandardItem("")
                dummy.setEditable(False)
                dummy.setData(True, DUMMY_ROLE)
                name_item.appendRow([dummy])

    def _on_expanded(self, index) -> None:
        item = self.model.itemFromIndex(index)
        if item.rowCount() == 1 and item.child(0).data(DUMMY_ROLE):
            item.removeRow(0)
            node = item.data(NODE_ROLE)
            if node is not None:
                self._populate_children(node.path, item)
        self._apply_min_width()

    def _on_collapsed(self, _index) -> None:
        self._apply_min_width()

    def _apply_min_width(self) -> None:
        # A hard minimumWidth, not an imperative QSplitter.setSizes() call:
        # setSizes() turned out to get silently overridden by QSplitter's
        # own internal re-layout whenever a child's content changes (the
        # very same event that triggers this), no matter how it was
        # sequenced. minimumWidth is a real constraint QSplitter cannot
        # violate (with setChildrenCollapsible(False) on the parent), so
        # the pane growing to fit falls out naturally instead of being
        # fought for imperatively.
        #
        # The width itself is measured directly with QFontMetrics rather
        # than trusted from header.sectionSize(): a ResizeToContents
        # column, it turns out, adaptively *shrinks* to whatever viewport
        # it's actually given rather than reporting the true width its
        # content needs, so reading it back mid-squeeze just measures the
        # squeeze. Only currently-expanded rows count, since collapsed
        # subtrees aren't visible. Only ever grows, and capped so one
        # deep hierarchy can't swallow the whole window.
        fm = QFontMetrics(self.tree.font())
        indent = self.tree.indentation()
        max_name_px = 0

        def visit(parent_item: Optional[QStandardItem], depth: int) -> None:
            nonlocal max_name_px
            row_count = parent_item.rowCount() if parent_item is not None else self.model.rowCount()
            for r in range(row_count):
                name_item = parent_item.child(r, 0) if parent_item is not None else self.model.item(r, 0)
                if name_item is None or name_item.data(DUMMY_ROLE):
                    continue
                name_px = depth * indent + ICON_SIZE + 16 + fm.horizontalAdvance(name_item.text())
                max_name_px = max(max_name_px, name_px)
                if self.tree.isExpanded(name_item.index()):
                    visit(name_item, depth + 1)

        visit(None, 0)

        width = max_name_px + self.tree.verticalScrollBar().sizeHint().width() + 24
        width = min(width, 700)
        if width > self.minimumWidth():
            self.setMinimumWidth(width)

    def _on_selection_changed(self, _selected, _deselected) -> None:
        indexes = self.tree.selectionModel().selectedRows(0)
        if not indexes:
            return
        item = self.model.itemFromIndex(indexes[0])
        node = item.data(NODE_ROLE)
        if node is not None:
            self._on_select(node)

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(f"HierarchyTree {{ background-color: {palette.base_bg}; }}")
        self.tree.setStyleSheet(f"""
            QTreeView {{
                background-color: {palette.base_bg};
                color: {palette.text};
                border: none;
                outline: 0;
                font-size: 11pt;
            }}
            QTreeView::item {{
                height: 26px;
            }}
            QTreeView::item:hover {{
                background-color: {palette.row_hover};
            }}
            QTreeView::item:selected {{
                background-color: {palette.selection};
                color: {palette.text};
            }}
        """)
        for item in self._items.values():
            node = item.data(NODE_ROLE)
            if node is not None:
                item.setIcon(icons.icon(node.kind, self._icon_color(node.kind), ICON_SIZE))
