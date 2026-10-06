"""Resolve an Explorer drop destination without staging downloaded files."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication


class WindowsDropTracker(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._cancelled = False
        self._released = False
        self._destination: tuple[int, int, int, bool] | None = None
        self._user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._sample)
        if self._user32 is not None:
            self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self._user32.GetAsyncKeyState.restype = ctypes.c_short
            self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            self._user32.GetCursorPos.restype = wintypes.BOOL
            self._user32.WindowFromPoint.argtypes = [wintypes.POINT]
            self._user32.WindowFromPoint.restype = wintypes.HWND
            self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            self._user32.GetAncestor.restype = wintypes.HWND
            self._user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self._user32.GetClassNameW.restype = ctypes.c_int

    def begin(self) -> None:
        if self._user32 is None:
            return
        self._user32.GetAsyncKeyState(0x1B)
        QApplication.instance().installEventFilter(self)
        self._timer.start()

    def finish(self) -> tuple[int, int, int, bool] | None:
        if self._user32 is None:
            return None
        self._sample()
        self._timer.stop()
        QApplication.instance().removeEventFilter(self)
        return None if self._cancelled else self._destination

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self._cancelled = True
        return super().eventFilter(watched, event)

    def _sample(self) -> None:
        if self._user32.GetAsyncKeyState(0x1B) & 0x8001:
            self._cancelled = True
        if self._cancelled or self._released:
            return
        button = 0x02 if self._user32.GetSystemMetrics(23) else 0x01
        if self._user32.GetAsyncKeyState(button) & 0x8000:
            return
        self._released = True
        position = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(position)):
            return
        window = self._user32.WindowFromPoint(position)
        root = self._user32.GetAncestor(window, 2)
        class_name = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(root, class_name, len(class_name))
        if class_name.value not in {"CabinetWClass", "ExploreWClass", "Progman", "WorkerW"}:
            return
        self._destination = (
            position.x, position.y, int(root), class_name.value in {"Progman", "WorkerW"},
        )


def resolve_windows_drop_directory(destination: tuple[int, int, int, bool]) -> Path:
    """Run Shell/UI Automation away from the terminal's UI and SSH threads."""
    resources = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    script = resources / "resources" / "windows" / "drop_directory.ps1"
    x, y, window, desktop = destination
    command = "& {\n" + script.read_text(encoding="utf-8") + "\n} " + (
        f"-X {x} -Y {y} -Window {window} -Desktop {int(desktop)}"
    )
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-Command", command],
        capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or not result.stdout.strip():
        raise OSError("Cannot identify the folder at the drop location")
    try:
        directory = Path(base64.b64decode(result.stdout.strip(), validate=True).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise OSError("Invalid drop destination") from exc
    if not directory.is_absolute() or not directory.is_dir():
        raise OSError("The drop destination is not a filesystem directory")
    return directory
