"""Thin bottom status strip: current file path and a transient message
slot (used for load errors / confirmations)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..theme import Palette, ThemeManager


class StatusBar(QWidget):
    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self._palette: Palette = theme.palette

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)

        self.path_label = QLabel("No file open")
        layout.addWidget(self.path_label)

        # Persistent (not auto-clearing like message_label below) slot for
        # the currently-selected dataset's "name · path · shape · dtype ·
        # N rows" summary -- lives here instead of its own title row above
        # the dataset table so that pane's content can sit flush with the
        # sidebar's top. Fed by DatasetTableView.context_changed, wired up
        # in App. Supports the rich-text a truncation warning needs.
        self.context_label = QLabel("")
        self.context_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.context_label)
        layout.addStretch(1)

        self.message_label = QLabel("")
        layout.addWidget(self.message_label)

        theme.register(self._apply_palette)

    def set_path(self, path: str | None) -> None:
        self.path_label.setText(path or "No file open")

    def set_context(self, text: str) -> None:
        self.context_label.setText(f"    ·    {text}" if text else "")

    def set_message(self, text: str, is_error: bool = False) -> None:
        color = "#FF6B6B" if is_error else self._palette.subtext
        self.message_label.setStyleSheet(f"color: {color};")
        self.message_label.setText(text)
        if text:
            QTimer.singleShot(5000, lambda: self.message_label.setText(""))

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.path_label.setStyleSheet(f"color: {palette.subtext}; font-size: 9pt;")
        self.context_label.setStyleSheet(f"color: {palette.subtext}; font-size: 9pt;")
