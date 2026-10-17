from __future__ import annotations


class AppTheme:
    BACKGROUND = "#111827"
    PANEL = "#1B2230"
    PANEL_ALT = "#1F2937"
    PANEL_SOFT = "#243041"
    BORDER = "#2D3748"
    TEXT = "#E5E7EB"
    MUTED = "#94A3B8"
    ACCENT = "#7C3AED"
    SUCCESS = "#22C55E"
    WARNING = "#F59E0B"
    DANGER = "#EF4444"

    @staticmethod
    def stylesheet() -> str:
        return """
            QWidget {
                background-color: #111827;
                color: #E5E7EB;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
            }

            QMainWindow, QDialog, QFrame, QLabel, QLineEdit, QTextEdit, QPlainTextEdit,
            QComboBox, QScrollArea, QListWidget, QWidget {
                background-color: #111827;
                color: #E5E7EB;
            }

            QToolButton, QPushButton {
                background-color: #1F2937;
                color: #E5E7EB;
                border: 1px solid #2D3748;
                border-radius: 8px;
                padding: 6px 12px;
            }

            QToolButton:hover, QPushButton:hover {
                border-color: #4B5563;
            }

            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {
                border: 1px solid #2D3748;
                border-radius: 8px;
                padding: 8px 10px;
                background-color: #0F172A;
            }

            QTabBar::tab {
                background: #1F2937;
                border: 1px solid #2D3748;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 12px;
                color: #E5E7EB;
            }

            QTabBar::tab:selected {
                background: #1B2230;
                border-color: #3B82F6;
            }

            QTabBar::close-button {
                image: none;
                margin: 4px;
            }

            QScrollBar:vertical {
                background: #111827;
                width: 10px;
            }

            QScrollBar::handle:vertical {
                background: #374151;
                border-radius: 5px;
            }
        """
