"""Themed replacement for QMessageBox.about() -- a native QMessageBox's
own window chrome (title bar, min/max/close) is OS-drawn and can't be
restyled to match the rest of this frameless app, the same problem
FileOpenDialog solved for the file picker.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..theme import Palette, ThemeManager
from .frameless import FramelessWindowMixin
from .title_bar import BAR_HEIGHT, SimpleTitleBar


class AboutDialog(FramelessWindowMixin, QDialog):
    def __init__(self, theme: ThemeManager, app_title: str, description: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(f"About {app_title}")
        self.setFixedSize(360, 190 + BAR_HEIGHT)
        self._palette: Palette = theme.palette

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = SimpleTitleBar(
            theme,
            f"About {app_title}",
            on_minimize=self.showMinimized,
            on_toggle_maximize=self._toggle_maximize,
            on_close=self.reject,
        )
        outer.addWidget(self.title_bar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 20, 24, 16)
        body_layout.setSpacing(10)

        self.name_label = QLabel(app_title)
        self.name_label.setStyleSheet("font-weight: 600; font-size: 14pt;")
        body_layout.addWidget(self.name_label)

        self.description_label = QLabel(description)
        self.description_label.setWordWrap(True)
        body_layout.addWidget(self.description_label, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.ok_button = QPushButton("OK")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        footer.addWidget(self.ok_button)
        body_layout.addLayout(footer)

        outer.addWidget(body, 1)

        for child in (self.title_bar,):
            child.setCursor(Qt.CursorShape.ArrowCursor)

        self._init_frameless(BAR_HEIGHT)

        self._apply_palette(theme.palette)
        theme.register(self._apply_palette)

    def closeEvent(self, event) -> None:
        self._teardown_frameless()
        super().closeEvent(event)

    def _on_maximize_changed(self, maximized: bool) -> None:
        self.title_bar.set_maximized(maximized)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.ok_button.setFocus()

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"""
            AboutDialog {{ background-color: {palette.window_bg}; color: {palette.text}; }}
            QPushButton {{
                background-color: {palette.button_bg};
                border: none;
                border-radius: 6px;
                padding: 6px 16px;
            }}
            QPushButton:hover {{ background-color: {palette.row_hover}; }}
            QPushButton:default {{ background-color: {palette.accent}; color: white; }}
            """
        )
        self.description_label.setStyleSheet(f"color: {palette.subtext};")
