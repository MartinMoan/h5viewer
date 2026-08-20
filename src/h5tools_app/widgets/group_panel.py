"""Main-pane content shown when a group (or the file root) is selected
instead of a dataset: a breadcrumb-ish overview of its attributes and
immediate children.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .. import icons
from ..core.h5_model import GROUP, H5Model, NodeInfo
from ..theme import Palette, ThemeManager
from .hierarchy_tree import _shape_summary

ICON_SIZE = 17


class _ClickableRow(QFrame):
    clicked = Signal()
    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class GroupPanel(QWidget):
    def __init__(
        self,
        theme: ThemeManager,
        on_child_activate: Callable[[str], None],
        on_child_double_activate: Optional[Callable[[str], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._on_child_activate = on_child_activate
        # Same single-click/double-click distinction HierarchyTree makes
        # (see its on_select/on_activate) -- optional so this panel still
        # works standalone without it.
        self._on_child_double_activate = on_child_double_activate
        self._palette: Palette = theme.palette
        self._last: Optional[tuple[H5Model, NodeInfo]] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(2)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 16pt;")
        # Ignored so a long path/attribute line can't force this whole
        # pane (and therefore the splitter) to never shrink narrower than
        # its full text width -- same reasoning as the dataset table's
        # subtitle, see dataset_table.py.
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.subtitle_label)
        outer.addSpacing(8)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 0, 2, 0)
        self.body_layout.setSpacing(3)
        self.body_layout.addStretch(1)  # keeps rows top-aligned as they're inserted before it
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        theme.register(self._apply_palette)

    def show_node(self, model: H5Model, node: NodeInfo) -> None:
        self._last = (model, node)
        self._render(model, node)

    def _render(self, model: H5Model, node: NodeInfo) -> None:
        while self.body_layout.count() > 1:
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.title_label.setText(node.name if node.name != "/" else Path(model.path).name)
        self.subtitle_label.setText(f"{node.path}    ·    {_shape_summary(node)}")

        i = 0
        attrs = model.get_attrs(node.path)
        if attrs:
            i = self._add_section_header("Attributes", i)
            for key, value in attrs.items():
                i = self._add_attr_row(key, value, i)

        children = model.list_children(node.path)
        i = self._add_section_header("Contents" if children else "", i)
        if not children:
            empty = QLabel("This group is empty.")
            empty.setStyleSheet(f"color: {self._palette.subtext};")
            self.body_layout.insertWidget(i, empty)
            return

        for child in children:
            i = self._add_child_row(child, i)

    def _add_section_header(self, text: str, index: int) -> int:
        if not text:
            return index
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {self._palette.subtext}; font-weight: 600; font-size: 9pt; margin-top: 8px;"
        )
        self.body_layout.insertWidget(index, label)
        return index + 1

    def _add_attr_row(self, key: str, value: str, index: int) -> int:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 1, 4, 1)
        key_label = QLabel(key)
        key_label.setFixedWidth(140)
        key_label.setStyleSheet("font-weight: 600;")
        value_label = QLabel(value)
        value_label.setWordWrap(True)
        row_layout.addWidget(key_label)
        row_layout.addWidget(value_label, 1)
        self.body_layout.insertWidget(index, row)
        return index + 1

    def _add_child_row(self, child: NodeInfo, index: int) -> int:
        row = _ClickableRow()
        row.setObjectName("childRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 12, 8)

        icon_label = QLabel()
        color = self._palette.subtext if child.kind == GROUP else self._palette.accent
        icon_label.setPixmap(icons.pixmap(child.kind, color, ICON_SIZE))
        row_layout.addWidget(icon_label)

        name_label = QLabel(child.name)
        row_layout.addWidget(name_label, 1)

        info_label = QLabel(_shape_summary(child))
        info_label.setStyleSheet(f"color: {self._palette.subtext}; font-size: 9pt;")
        row_layout.addWidget(info_label)

        row.clicked.connect(lambda p=child.path: self._on_child_activate(p))
        if self._on_child_double_activate is not None:
            row.doubleClicked.connect(lambda p=child.path: self._on_child_double_activate(p))
        self.body_layout.insertWidget(index, row)
        return index + 1

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.subtitle_label.setStyleSheet(f"color: {palette.subtext}; font-size: 10pt;")
        self.setStyleSheet(
            f"""
            QFrame#childRow {{
                background-color: {palette.header_bg};
                border-radius: 8px;
            }}
            QFrame#childRow:hover {{
                background-color: {palette.row_hover};
            }}
            """
        )
        if self._last is not None:
            self._render(*self._last)
