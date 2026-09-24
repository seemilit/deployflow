"""Qt WebEngine wrapper around the bundled xterm.js terminal."""

from __future__ import annotations

import base64
import binascii
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


_WRITE_INTERVAL_MS = 8
_MAX_WRITE_BATCH_CHARACTERS = 65536


def _resource_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "resources" / "xterm"
    return Path(__file__).resolve().parent / "resources" / "xterm"


class _XTermBridge(QObject):
    writeRequested = Signal(str)
    clearRequested = Signal()
    focusRequested = Signal()
    fitRequested = Signal()
    pasteTextRequested = Signal(str)
    inputReceived = Signal(str)
    binaryReceived = Signal(bytes)
    terminalResized = Signal(int, int)
    ready = Signal()
    selectionChanged = Signal(str)
    pasteRequested = Signal()
    shellIdentified = Signal(str, int)

    @Slot(str, int)
    def identifyShell(self, token: str, pid: int) -> None:
        self.shellIdentified.emit(token, pid)

    @Slot(str)
    def sendInput(self, value: str) -> None:
        self.inputReceived.emit(value)

    @Slot(str)
    def sendBinary(self, value: str) -> None:
        try:
            payload = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            return
        self.binaryReceived.emit(payload)

    @Slot(int, int)
    def resizeTerminal(self, columns: int, rows: int) -> None:
        self.terminalResized.emit(columns, rows)

    @Slot()
    def terminalReady(self) -> None:
        self.ready.emit()

    @Slot(str)
    def updateSelection(self, value: str) -> None:
        self.selectionChanged.emit(value)

    @Slot(str)
    def copyText(self, value: str) -> None:
        QApplication.clipboard().setText(value)

    @Slot()
    def requestPaste(self) -> None:
        self.pasteRequested.emit()


class XTermTerminal(QWidget):
    """Embeddable xterm.js terminal with a small Qt signal interface."""

    textCommitted = Signal(str)
    binaryCommitted = Signal(bytes)
    terminalResized = Signal(int, int)
    loadFailed = Signal(str)
    shellIdentified = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ready = False
        self._closed = False
        self._selection = ""
        self._columns = 160
        self._rows = 48
        self._pending_output: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.web_view = QWebEngineView(self)
        self.web_view.setPage(QWebEnginePage(self.web_view))
        self.web_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.web_view.customContextMenuRequested.connect(
            self.customContextMenuRequested.emit
        )
        settings = self.web_view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False
        )
        layout.addWidget(self.web_view)

        self._bridge = _XTermBridge(self)
        self._bridge.inputReceived.connect(self.textCommitted)
        self._bridge.binaryReceived.connect(self.binaryCommitted)
        self._bridge.terminalResized.connect(self._terminal_resized)
        self._bridge.ready.connect(self._terminal_ready)
        self._bridge.selectionChanged.connect(self._selection_changed)
        self._bridge.pasteRequested.connect(self._paste_from_clipboard)
        self._bridge.shellIdentified.connect(self.shellIdentified)
        self._channel = QWebChannel(self.web_view.page())
        self._channel.registerObject("terminalBridge", self._bridge)
        self.web_view.page().setWebChannel(self._channel)

        self._write_timer = QTimer(self)
        self._write_timer.setSingleShot(True)
        self._write_timer.setInterval(_WRITE_INTERVAL_MS)
        self._write_timer.timeout.connect(self._flush_output)
        self.web_view.loadFinished.connect(self._load_finished)
        html_path = _resource_directory() / "terminal.html"
        self.web_view.setUrl(QUrl.fromLocalFile(str(html_path)))

    def write(self, value: str) -> None:
        if not value or self._closed:
            return
        self._pending_output.append(value)
        if self._ready and not self._write_timer.isActive():
            self._write_timer.start()

    def clear(self) -> None:
        self._pending_output.clear()
        if self._ready:
            self._bridge.clearRequested.emit()

    def fit(self) -> None:
        if self._ready:
            self._bridge.fitRequested.emit()

    def terminal_size(self) -> tuple[int, int]:
        return self._columns, self._rows

    def selected_text(self) -> str:
        return self._selection

    def has_selection(self) -> bool:
        return bool(self._selection)

    def copy(self) -> None:
        if self._selection:
            QApplication.clipboard().setText(self._selection)

    def paste(self, value: str) -> None:
        if value and self._ready:
            self._bridge.pasteTextRequested.emit(value)

    def focus_terminal(self) -> None:
        self.web_view.setFocus()
        if self._ready:
            self._bridge.focusRequested.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._write_timer.stop()
        self._pending_output.clear()
        self.web_view.stop()

    def _load_finished(self, succeeded: bool) -> None:
        if not succeeded and not self._closed:
            self.loadFailed.emit("xterm.js 终端页面加载失败")

    def _terminal_ready(self) -> None:
        if self._closed:
            return
        self._ready = True
        self.fit()
        self._flush_output()

    def _terminal_resized(self, columns: int, rows: int) -> None:
        columns = max(20, int(columns))
        rows = max(4, int(rows))
        if (columns, rows) == (self._columns, self._rows):
            return
        self._columns, self._rows = columns, rows
        self.terminalResized.emit(columns, rows)

    def _selection_changed(self, value: str) -> None:
        self._selection = value

    def _paste_from_clipboard(self) -> None:
        value = QApplication.clipboard().text()
        if value:
            self.paste(value)

    def _flush_output(self) -> None:
        self._write_timer.stop()
        if self._closed or not self._ready or not self._pending_output:
            return
        chunks, self._pending_output = self._pending_output, []
        value = "".join(chunks)
        self._bridge.writeRequested.emit(value[:_MAX_WRITE_BATCH_CHARACTERS])
        if len(value) > _MAX_WRITE_BATCH_CHARACTERS:
            self._pending_output.append(value[_MAX_WRITE_BATCH_CHARACTERS:])
            self._write_timer.start()
