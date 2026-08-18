"""Virtualized, continuously-scrollable table view for a single dataset.

``QTableView`` + a custom ``QAbstractTableModel`` already do the hard part
here: Qt only ever asks ``data()`` for cells that are actually on screen,
so we get virtualized scrolling over an arbitrarily large dataset for
free, and per-column background tinting is just a model role rather than
something we have to hand-paint. The only real work is wiring ``data()``
to a ``DatasetSource``: return a placeholder immediately if a row's block
hasn't loaded yet, kick off a background load, and let a small QTimer
poll the source and emit ``dataChanged`` for whatever finished since the
last tick.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import constants as c
from ..core.dataset_source import DatasetSource
from ..core.h5_model import ColumnLayout, H5Model
from ..theme import Palette, ThemeManager

POLL_MS = 40


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, np.bytes_)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, (np.floating, float)):
        if np.isnan(value):
            return "NaN"
        return f"{value:.6g}"
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (np.bool_, bool)):
        return "True" if value else "False"
    if isinstance(value, np.ndarray):
        if value.size <= 6:
            return np.array2string(value, threshold=6)
        return f"<{'x'.join(str(d) for d in value.shape)} {value.dtype}>"
    return str(value)


class DatasetTableModel(QAbstractTableModel):
    def __init__(self, source: DatasetSource, palette: Palette, parent=None):
        super().__init__(parent)
        self.source = source
        self.layout_info: ColumnLayout = source.layout
        self._set_palette(palette)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.source.row_count

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.layout_info.n_columns

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()

        if role == Qt.ItemDataRole.BackgroundRole:
            return self._column_colors[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            arr, _missing = self.source.get_available(row, row + 1)
            return self._text_color if arr is not None else self._placeholder_color
        if role == Qt.ItemDataRole.DisplayRole:
            arr, _missing = self.source.get_available(row, row + 1)
            if arr is None:
                self.source.ensure_loaded(row, row + 1)
                return "···"
            return _format_cell(arr[0, col])
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.layout_info.labels[section]
        return str(section)

    def apply_palette(self, palette: Palette) -> None:
        self._set_palette(palette)
        rows, cols = self.rowCount(), self.columnCount()
        if rows and cols:
            top_left = self.index(0, 0)
            bottom_right = self.index(rows - 1, cols - 1)
            self.dataChanged.emit(
                top_left, bottom_right, [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole]
            )

    def poll_and_refresh(self) -> None:
        blocks = self.source.poll_updates()
        if not blocks:
            return
        block_size = self.source.block_size
        row_count = self.source.row_count
        col_count = self.layout_info.n_columns
        for block in blocks:
            start = block * block_size
            end = min(start + block_size, row_count)
            if start >= end:
                continue
            self.dataChanged.emit(self.index(start, 0), self.index(end - 1, col_count - 1))

    def _set_palette(self, palette: Palette) -> None:
        self._column_colors = [QColor(palette.column_color(i)) for i in range(self.layout_info.n_columns)]
        self._text_color = QColor(palette.text)
        self._placeholder_color = QColor(palette.subtext)


class DatasetTableView(QWidget):
    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._palette: Palette = theme.palette
        self._source: Optional[DatasetSource] = None
        self._table_model: Optional[DatasetTableModel] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(6)

        bar = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 15pt;")
        self.subtitle_label = QLabel("")
        # The subtitle is one long non-wrapping line ("path · shape ·
        # dtype · N rows") -- without this, a plain QLabel's minimum size
        # hint is however wide its full text needs to be, which forces
        # this whole pane (and therefore the splitter) to never shrink
        # narrower than that, however long the path happens to be. With
        # Ignored, the label can be visually compressed instead of
        # forcing its container to stay wide.
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.subtitle_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        bar.addLayout(title_col)
        bar.addStretch(1)

        self.top_button = QPushButton("Top")
        self.top_button.clicked.connect(self._go_home)
        self.end_button = QPushButton("End")
        self.end_button.clicked.connect(self._go_end)
        bar.addWidget(self.top_button)
        bar.addWidget(self.end_button)
        bar.addSpacing(8)

        bar.addWidget(QLabel("Row"))
        self.goto_entry = QLineEdit()
        self.goto_entry.setFixedWidth(80)
        self.goto_entry.setPlaceholderText("#")
        self.goto_entry.returnPressed.connect(self._on_goto)
        bar.addWidget(self.goto_entry)
        self.go_button = QPushButton("Go")
        self.go_button.clicked.connect(self._on_goto)
        bar.addWidget(self.go_button)

        outer.addLayout(bar)

        self.warning_label = QLabel("")
        self.warning_label.setVisible(False)
        outer.addWidget(self.warning_label)

        self.table = QTableView()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setShowGrid(True)
        vheader = self.table.verticalHeader()
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vheader.setDefaultSectionSize(c.ROW_HEIGHT)
        hheader = self.table.horizontalHeader()
        hheader.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hheader.setDefaultSectionSize(c.MIN_COL_WIDTH)

        self.empty_label = QLabel("Select a dataset from the tree to view its contents")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.table)
        outer.addWidget(self.stack, 1)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_MS)

        theme.register(self._apply_palette)

    # -- public API --------------------------------------------------------

    def load(self, model: H5Model, path: str) -> None:
        self._teardown_source()
        dataset = model.get_dataset(path)
        layout = model.column_layout(path)
        node = model.node_info(path)

        self._source = DatasetSource(dataset, layout)
        self._table_model = DatasetTableModel(self._source, self._palette)
        self.table.setModel(self._table_model)
        self._apply_column_widths(layout)

        self.title_label.setText(node.name)
        shape_txt = "scalar" if node.shape == () else "×".join(str(d) for d in node.shape)
        self.subtitle_label.setText(
            f"{path}    ·    shape {shape_txt}    ·    {node.dtype}    ·    {layout.row_count:,} rows"
        )
        if layout.truncated:
            self.warning_label.setText(
                f"Showing first {layout.n_columns} of {layout.total_columns:,} flattened columns"
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        self.goto_entry.clear()
        self.stack.setCurrentWidget(self.table)

    def clear(self) -> None:
        self._teardown_source()
        self.title_label.setText("")
        self.subtitle_label.setText("")
        self.warning_label.setVisible(False)
        self.stack.setCurrentWidget(self.empty_label)

    def _teardown_source(self) -> None:
        self.table.setModel(None)
        self._table_model = None
        if self._source is not None:
            self._source.close()
            self._source = None

    def _apply_column_widths(self, layout: ColumnLayout) -> None:
        header = self.table.horizontalHeader()
        for i, label in enumerate(layout.labels):
            width = min(c.MAX_COL_WIDTH, max(c.MIN_COL_WIDTH, len(label) * 9 + 30))
            header.resizeSection(i, width)

    # -- navigation ------------------------------------------------------

    def _go_home(self) -> None:
        if self._table_model is not None:
            self.table.verticalScrollBar().setValue(0)

    def _go_end(self) -> None:
        if self._table_model is not None:
            self.table.verticalScrollBar().setValue(self.table.verticalScrollBar().maximum())

    def _on_goto(self) -> None:
        if self._table_model is None:
            return
        text = self.goto_entry.text().strip()
        if not text.isdigit():
            return
        row = max(0, min(int(text), self._table_model.rowCount() - 1))
        self.table.verticalScrollBar().setValue(row)

    def _poll(self) -> None:
        if self._table_model is not None:
            self._table_model.poll_and_refresh()

    # -- theming -----------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        if self._table_model is not None:
            self._table_model.apply_palette(palette)

        self.subtitle_label.setStyleSheet(f"color: {palette.subtext}; font-size: 10pt;")
        warn_color = "#E0A93B" if palette.dark else "#8A5A00"
        self.warning_label.setStyleSheet(f"color: {warn_color}; font-size: 10pt;")
        self.empty_label.setStyleSheet(f"color: {palette.subtext}; font-size: 12pt;")
        self.table.setStyleSheet(
            f"""
            QTableView {{
                background-color: {palette.body_bg};
                gridline-color: {palette.grid_line};
                border: none;
                selection-background-color: transparent;
                selection-color: {palette.text};
            }}
            QHeaderView::section {{
                background-color: {palette.header_bg};
                color: {palette.subtext};
                border: none;
                border-bottom: 2px solid {palette.accent};
                padding: 4px 8px;
            }}
            """
        )
