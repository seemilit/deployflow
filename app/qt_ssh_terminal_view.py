"""PySide6 view for one independent interactive SSH connection."""

from __future__ import annotations

import queue
import re
import errno
import shlex
import threading
import time
import os
import posixpath
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QDir, QEvent, QMimeData, QObject, QPoint, QStandardPaths, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDrag, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from config import ServerParameters, mask_ip_address
from i18n import TranslatedText, render_text, tr
from i18n.widgets import (
    QAction, QCheckBox, QDialog, QGroupBox, QLabel, QLineEdit, QPlainTextEdit,
    QProgressBar, QProgressDialog, QPushButton, QTabBar, QTreeWidget, QTreeWidgetItem,
)
from qt_xterm_terminal import XTermTerminal
from remote_path import (
    PATH_TYPE_CACHE_TTL, RemotePathContext, RemotePathResolver, RemotePathType,
    command_changes_directory,
)
from ssh_terminal import InteractiveSSHSession, SSHSessionError
from windows_drag import WindowsDropTracker, resolve_windows_drop_directory


StateCallback = Callable[["QtSSHTerminalTab"], None]
CloseCallback = Callable[["QtSSHTerminalTab"], None]
LogCallback = Callable[[str, str], None]
ToolModeCallback = Callable[[], None]
MonitorWidthCallback = Callable[[int], None]
_MAX_OUTPUT_CHARACTERS_PER_POLL = 16384
_EVENT_DRAIN_TIME_SLICE_SECONDS = 0.006
_TERMINAL_LOG_FLUSH_INTERVAL_MS = 200
_DIRECTORY_CACHE_TTL_SECONDS = 20.0
_DIRECTORY_CACHE_LIMIT = 120
_REMOTE_FILE_MIME = "application/x-deployflow-remote-files"
_REMOTE_DOWNLOAD_MIME = "application/x-deployflow-remote-download"
_REMOTE_ITEM_BATCH_SIZE = 250
_TERMINAL_INPUT_IDLE_SECONDS = 1.0
_TERMINAL_OUTPUT_IDLE_SECONDS = 0.6


@dataclass
class _RemoteClipboard:
    server: str
    paths: tuple[str, ...]
    cut: bool
    in_flight: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    token: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class _RemotePastePlan:
    source: str
    target: str
    replace: bool = False
    alternate_target: str | None = None


def _add_remote_type_actions(
    menu: QMenu,
    path: str,
    is_directory: bool,
    callback: Callable[[str, str], None],
    executable: bool = False,
) -> None:
    actions = [(tr("跳转目录"), "open")] if is_directory else [(tr("打开文件编辑器"), "edit")]
    if not is_directory:
        actions.append((tr("跳转目录"), "jump_directory"))
        actions.append((tr("修改文件权限…"), "chmod"))
    name = posixpath.basename(path).lower()
    if not is_directory and (name.endswith((".sh", ".bash", ".zsh")) or executable):
        actions.extend([
            (tr("执行脚本"), "script"),
            (tr("执行脚本并附带参数…"), "script_with_args"),
        ])
    if not is_directory and re.search(r"\.(log|out)(?:[.-][\w.-]+)?$", name) and not name.endswith(
        (".gz", ".bz2", ".xz", ".zip")
    ):
        actions.extend([
            (tr("实时跟踪日志（tail -f）"), "tail_follow"),
            (tr("查看末尾 N 行（tail -n）…"), "tail_lines"),
            (tr("查询关键字（grep）…"), "search"),
        ])
    for title, action in actions:
        menu.addAction(title).triggered.connect(
            lambda _checked=False, value=action: callback(value, path)
        )


class RemoteContextMenuBuilder:
    """Build menus from local hints only; callbacks verify before acting."""

    def build(
        self, menu: QMenu, context: RemotePathContext,
        callback: Callable[[str, RemotePathContext], None], can_paste: bool,
    ) -> None:
        path = context.resolved_path or context.normalized_path
        kind = context.path_type

        def add(title: str, action: str, enabled: bool = True) -> None:
            item = menu.addAction(title)
            item.setEnabled(enabled)
            item.triggered.connect(lambda _checked=False: callback(action, context))

        if kind is RemotePathType.UNKNOWN:
            add(tr("跳转目录"), "jump_directory")
        else:
            _add_remote_type_actions(
                menu, path, kind is RemotePathType.DIRECTORY,
                lambda action, _path: callback(action, context),
            )
        add(tr("下载目录") if kind is RemotePathType.DIRECTORY else tr("下载"), "download", path != "/")
        menu.addSeparator()
        concrete = path not in {"/", ".", "..", "~"} and bool(path.strip("/"))
        for title, action in (
            (tr("复制文件/文件夹"), "copy"), (tr("剪切文件/文件夹"), "cut"),
            (tr("重命名"), "rename"), (tr("删除…"), "delete"),
        ):
            add(title, action, concrete)
        add(tr("复制路径"), "copy_path")
        if kind is RemotePathType.DIRECTORY:
            menu.addSeparator()
            add(tr("粘贴文件到此目录"), "paste", can_paste)
            add(tr("新建文件…"), "new_file")
            add(tr("新建文件夹…"), "new_directory")


@dataclass(eq=False)
class _HostKeyRequest:
    attempt: int
    session: InteractiveSSHSession
    hostname: str
    key_type: str
    fingerprint: str
    completed: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class _ThreadEvents(QObject):
    available = Signal()


class _DownloadCancelled(Exception):
    pass


@dataclass(eq=False)
class _DownloadTask:
    task_id: str
    title: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    sftp: object | None = None
    sftp_lock: threading.Lock = field(default_factory=threading.Lock)
    completed: threading.Event = field(default_factory=threading.Event)
    succeeded: bool = False


class _DownloadTaskPanel(QFrame):
    cancel_requested = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._rows: dict[str, tuple[QWidget, QLabel, QProgressBar]] = {}
        self._directions: dict[str, str] = {}
        self._folder_buttons: dict[str, QPushButton] = {}
        self._destinations: dict[str, Path] = {}
        self.setObjectName("downloadTaskPanel")
        self.setFixedWidth(360)
        self.setStyleSheet(
            "QFrame#downloadTaskPanel { background:#ffffff; border:1px solid #94a3b8; "
            "border-radius:4px; }"
            "QFrame#downloadTaskRow { border:0; border-bottom:1px solid #e5e7eb; }"
            "QLabel { border:0; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(10, 6, 6, 6)
        self.title_label = QLabel(tr("传输任务"))
        self.title_label.setStyleSheet("font-weight:600;")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        hide_button = QPushButton("—")
        hide_button.setFixedSize(26, 22)
        hide_button.setToolTip(tr("隐藏传输任务"))
        hide_button.clicked.connect(self.hide)
        title_row.addWidget(hide_button)
        root.addLayout(title_row)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(6, 0, 6, 6)
        self.rows_layout.setSpacing(2)
        self.rows_layout.addStretch(1)
        self.scroll_area.setWidget(self.rows_widget)
        root.addWidget(self.scroll_area, 1)
        self.hide()

    def apply_theme(self, colors: dict[str, str]) -> None:
        self.setStyleSheet(
            f"QFrame#downloadTaskPanel {{ background:{colors['surface']}; color:{colors['foreground']}; "
            f"border:1px solid {colors['border']}; border-radius:4px; }}"
            f"QFrame#downloadTaskRow {{ border:0; border-bottom:1px solid {colors['border']}; }}"
            "QLabel { border:0; }"
        )
        for _row, status, _progress in self._rows.values():
            if status.text() in {tr("等待下载…"), tr("等待上传…")} or status.text().startswith(
                (tr("正在下载："), tr("正在上传："))
            ):
                status.setStyleSheet(f"color:{colors['muted']};")

    def add_task(
        self, task_id: str, title: str, direction: str = "download",
        destination: Path | None = None,
    ) -> None:
        if task_id in self._rows:
            return
        row = QFrame()
        row.setObjectName("downloadTaskRow")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(4, 6, 4, 7)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        self._directions[task_id] = direction
        name_label = QLabel((tr("上传") if direction == "upload" else tr("下载")) + "：" + title)
        name_label.setToolTip(title)
        heading.addWidget(name_label, 1)
        if destination is not None:
            self._destinations[task_id] = destination
            folder_button = QPushButton()
            folder_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
            folder_button.setFixedSize(24, 22)
            folder_button.setToolTip(tr("打开所在文件夹") + "\n" + str(destination))
            folder_button.setEnabled(False)
            folder_button.clicked.connect(
                lambda _checked=False, value=task_id: self._open_task_folder(value)
            )
            self._folder_buttons[task_id] = folder_button
            heading.addWidget(folder_button)
        delete_button = QPushButton("×")
        delete_button.setFixedSize(24, 22)
        delete_button.setToolTip(tr("删除任务并停止传输"))
        delete_button.clicked.connect(
            lambda _checked=False, value=task_id: self._delete_task(value)
        )
        heading.addWidget(delete_button)
        layout.addLayout(heading)
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setFixedHeight(16)
        layout.addWidget(progress)
        status = QLabel(tr("等待上传…") if direction == "upload" else tr("等待下载…"))
        status.setStyleSheet("color:#64748b;")
        layout.addWidget(status)
        for widget in (row, progress):
            widget.setContextMenuPolicy(Qt.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda position, source=widget, value=task_id:
                self._show_task_context_menu(value, source.mapToGlobal(position))
            )
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        self._rows[task_id] = (row, status, progress)
        self._update_size()
        self.show_panel()

    def update_task(self, task_id: str, name: str, current: int, total: int) -> None:
        values = self._rows.get(task_id)
        if values is None:
            return
        _row, status, progress = values
        percent = 100 if total <= 0 else min(100, int(current * 100 / total))
        progress.setValue(percent)
        status.setText(
            tr("正在上传：{0}（{1}%）", name, percent)
            if self._directions.get(task_id) == "upload"
            else tr("正在下载：{0}（{1}%）", name, percent)
        )

    def finish_task(self, task_id: str, succeeded: bool, message: str) -> None:
        values = self._rows.get(task_id)
        if values is None:
            return
        _row, status, progress = values
        if succeeded:
            progress.setValue(100)
            status.setText(tr("上传完成") if self._directions.get(task_id) == "upload" else tr("下载完成"))
            status.setStyleSheet("color:#15803d;")
            if task_id in self._folder_buttons:
                self._folder_buttons[task_id].setEnabled(True)
        else:
            status.setText(message)
            status.setStyleSheet("color:#dc2626;")

    def remove_task(self, task_id: str) -> None:
        values = self._rows.pop(task_id, None)
        if values is None:
            return
        row, _status, _progress = values
        self._directions.pop(task_id, None)
        self._folder_buttons.pop(task_id, None)
        self._destinations.pop(task_id, None)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._update_size()
        if not self._rows:
            self.hide()

    def show_panel(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.move(max(8, parent.width() - self.width() - 12), 52)
        self.show()
        self.raise_()

    def reposition(self) -> None:
        if self.isVisible():
            self.show_panel()

    def _delete_task(self, task_id: str) -> None:
        self.cancel_requested.emit(task_id)
        self.remove_task(task_id)

    def _open_task_folder(self, task_id: str) -> None:
        directory = self._destinations.get(task_id)
        if directory is not None and directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _show_task_context_menu(self, task_id: str, position: QPoint) -> None:
        if task_id not in self._rows:
            return
        menu = QMenu(self)
        if task_id in self._destinations:
            open_folder = menu.addAction(tr("打开所在文件夹"))
            open_folder.setEnabled(self._folder_buttons[task_id].isEnabled())
            open_folder.triggered.connect(lambda: self._open_task_folder(task_id))
            menu.addSeparator()
        menu.addAction(tr("删除")).triggered.connect(lambda: self._delete_task(task_id))
        menu.exec(position)

    def _update_size(self) -> None:
        count = len(self._rows)
        self.title_label.setText(tr("传输任务（{0}）", count))
        self.setFixedHeight(min(330, 42 + max(1, count) * 78))


class _RemoteDownloadMimeData(QMimeData):
    _active: "_RemoteDownloadMimeData | None" = None

    def __init__(
        self, entries: list[tuple[str, bool, str]],
        download: Callable[[list[tuple[str, bool, str]], Path], _DownloadTask | None],
    ) -> None:
        super().__init__()
        self.entries = list(entries)
        self.download = download
        self.accepted = False
        self.setData(_REMOTE_DOWNLOAD_MIME, QByteArray(uuid.uuid4().hex.encode("ascii")))

    @classmethod
    def from_mime(cls, mime: QMimeData) -> "_RemoteDownloadMimeData | None":
        active = cls._active
        if active is not None and mime.hasFormat(_REMOTE_DOWNLOAD_MIME):
            if bytes(mime.data(_REMOTE_DOWNLOAD_MIME)) == bytes(active.data(_REMOTE_DOWNLOAD_MIME)):
                return active
        return None


class _DirectoryNavigation(QWidget):
    path_requested = Signal(str)
    parent_requested = Signal()

    def __init__(self, directory: str, local: bool = False) -> None:
        super().__init__()
        self._local = local
        self._directory = directory
        self._history: list[str] = []
        self._history_index = -1
        self._pending_history_index: int | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.back_button = self._navigation_button(QStyle.SP_ArrowBack, tr("后退"))
        self.forward_button = self._navigation_button(QStyle.SP_ArrowForward, tr("前进"))
        self.parent_button = self._navigation_button(QStyle.SP_ArrowUp, tr("上一级"))
        self.back_button.clicked.connect(lambda: self._navigate_history(-1))
        self.forward_button.clicked.connect(lambda: self._navigate_history(1))
        self.parent_button.clicked.connect(lambda: self.parent_requested.emit())
        for button in (self.back_button, self.forward_button, self.parent_button):
            layout.addWidget(button)
        self.address_stack = QStackedWidget()
        self.address_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.address_stack.setFixedHeight(28)
        self.breadcrumbs = QScrollArea()
        self.breadcrumbs.setWidgetResizable(True)
        self.breadcrumbs.setFrameShape(QFrame.NoFrame)
        self.breadcrumbs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.breadcrumbs.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.breadcrumb_widget = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_widget)
        self.breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_layout.setSpacing(0)
        self.breadcrumbs.setWidget(self.breadcrumb_widget)
        self.address_stack.addWidget(self.breadcrumbs)
        self.entry = QLineEdit()
        self.entry.returnPressed.connect(self._submit_path)
        self.entry.installEventFilter(self)
        self.breadcrumb_widget.installEventFilter(self)
        self.address_stack.addWidget(self.entry)
        layout.addWidget(self.address_stack, 1)
        self.edit_button = self._navigation_button(QStyle.SP_FileDialogDetailedView, tr("输入路径"))
        self.edit_button.clicked.connect(self._edit_path)
        layout.addWidget(self.edit_button)
        self.reset(directory)

    def _navigation_button(self, icon: QStyle.StandardPixmap, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setFixedSize(26, 26)
        button.setToolTip(tooltip)
        return button

    def reset(self, directory: str) -> None:
        self._history.clear()
        self._history_index = -1
        self._pending_history_index = None
        self.set_directory(directory)

    def set_directory(self, directory: str) -> None:
        changed = directory != self._directory or self._history_index < 0
        keep_editing = not changed and self.address_stack.currentIndex() == 1 and self.entry.hasFocus()
        pending = self._pending_history_index
        if pending is not None and self._history[pending] == directory:
            self._history_index = pending
        elif self._history_index < 0 or self._history[self._history_index] != directory:
            self._history = self._history[:self._history_index + 1] + [directory]
            self._history = self._history[-100:]
            self._history_index = len(self._history) - 1
        self._pending_history_index = None
        self._directory = directory
        if not keep_editing:
            self.entry.setText(directory if directory else tr("此电脑"))
            self.address_stack.setCurrentIndex(0)
        self._update_buttons()
        if changed or not self.breadcrumb_layout.count():
            self._build_breadcrumbs()

    def cancel_navigation(self) -> None:
        self._pending_history_index = None
        self.address_stack.setCurrentIndex(0)
        self.set_directory(self._directory)

    def _update_buttons(self) -> None:
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(self._history_index + 1 < len(self._history))
        self.parent_button.setEnabled(bool(self._directory) if self._local else self._directory != "/")

    def _navigate_history(self, offset: int) -> None:
        index = self._history_index + offset
        if 0 <= index < len(self._history):
            self._pending_history_index = index
            self.address_stack.setCurrentIndex(0)
            self.path_requested.emit(self._history[index])

    def request_directory(self, directory: str) -> None:
        self._pending_history_index = None
        self.address_stack.setCurrentIndex(0)
        self.path_requested.emit(directory)

    def _submit_path(self) -> None:
        directory = self.entry.text().strip()
        if self._local and directory == tr("此电脑"):
            directory = ""
        self.request_directory(directory)

    def _edit_path(self) -> None:
        self.entry.setText(self._directory if self._directory else tr("此电脑"))
        self.address_stack.setCurrentIndex(1)
        self.entry.setFocus(Qt.ShortcutFocusReason)
        self.entry.selectAll()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.breadcrumb_widget and event.type() == QEvent.MouseButtonDblClick:
            self._edit_path()
            return True
        if watched is self.entry and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.entry.setText(self._directory if self._directory else tr("此电脑"))
            self.address_stack.setCurrentIndex(0)
            return True
        return super().eventFilter(watched, event)

    def _build_breadcrumbs(self) -> None:
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if self._local:
            parts = [(tr("此电脑"), "")]
            if self._directory:
                path = Path(self._directory)
                parts.extend((value.name or str(value), str(value)) for value in [*reversed(path.parents), path])
        else:
            parts = [("/", "/")] if self._directory.startswith("/") else []
            current = "/" if parts else ""
            for name in self._directory.split("/"):
                if name:
                    current = posixpath.join(current, name)
                    parts.append((name, current))
        for index, (name, directory) in enumerate(parts):
            if index:
                self.breadcrumb_layout.addWidget(QLabel("›"))
            button = QPushButton(name)
            button.setFlat(True)
            button.setToolTip(directory if directory else tr("此电脑"))
            button.clicked.connect(lambda _checked=False, path=directory: self.request_directory(path))
            self.breadcrumb_layout.addWidget(button)
        self.breadcrumb_layout.addStretch(1)
        QTimer.singleShot(0, self._scroll_breadcrumbs_to_end)

    def _scroll_breadcrumbs_to_end(self) -> None:
        scrollbar = self.breadcrumbs.horizontalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class _LocalFileTree(QTreeView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def dragEnterEvent(self, event: QEvent) -> None:
        if _RemoteDownloadMimeData.from_mime(event.mimeData()) is not None:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QEvent) -> None:
        if _RemoteDownloadMimeData.from_mime(event.mimeData()) is not None:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QEvent) -> None:
        remote = _RemoteDownloadMimeData.from_mime(event.mimeData())
        if remote is None:
            super().dropEvent(event)
            return
        index = self.indexAt(event.position().toPoint())
        path = self.model().filePath(index if index.isValid() else self.rootIndex())
        if not path:
            event.ignore()
            return
        directory = Path(path)
        if directory.is_file():
            directory = directory.parent
        if not directory.is_dir():
            event.ignore()
            return
        download, entries = remote.download, list(remote.entries)
        remote.accepted = True
        event.setDropAction(Qt.CopyAction)
        event.accept()
        QTimer.singleShot(0, lambda: download(entries, directory))


class _RemoteFileTree(QTreeWidget):
    files_dropped = Signal(object)
    download_drag_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def startDrag(self, _supported_actions: object) -> None:
        if self.selectedItems():
            self.download_drag_requested.emit()

    def dragEnterEvent(self, event: QEvent) -> None:
        if event.mimeData().hasFormat(_REMOTE_DOWNLOAD_MIME):
            event.ignore()
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QEvent) -> None:
        if event.mimeData().hasFormat(_REMOTE_DOWNLOAD_MIME):
            event.ignore()
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QEvent) -> None:
        if event.mimeData().hasFormat(_REMOTE_DOWNLOAD_MIME):
            event.ignore()
            return
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


class _TransferEvents(QObject):
    remote_loaded = Signal(int, str, object)
    remote_error = Signal(int, str, str)
    directories_loaded = Signal(str, object)
    transfer_progress = Signal(str, int, int)
    operation_finished = Signal(object, bool, object)
    download_progress = Signal(str, str, int, int)
    download_finished = Signal(str, bool, str, bool)
    upload_finished = Signal(str, bool, str, bool)
    external_drop_resolved = Signal(object, object, object, str)
    clipboard_finished = Signal(object, object)
    clipboard_progress = Signal(object, int, int, str)
    paste_preflighted = Signal(object, object, str)
    permissions_loaded = Signal(object, str, int, str)


class _RemoteEditorEvents(QObject):
    loaded = Signal(object)
    saved = Signal(object)
    failed = Signal(str)


class _RemoteFileEditor(QDialog):
    file_saved = Signal(str)
    _MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, parent: QWidget, session: InteractiveSSHSession, path: str) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        self._session = session
        self._path = path
        self._original = b""
        self._encoding = "utf-8"
        self._newline = "\n"
        self._busy = True
        self._loaded = False
        self._close_after_save = False
        self._events = _RemoteEditorEvents(self)
        self._events.loaded.connect(self._on_loaded)
        self._events.saved.connect(self._on_saved)
        self._events.failed.connect(self._on_failed)
        self.setWindowTitle(tr("远程文件编辑 — {0}[*]", path))
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(path))
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Cascadia Mono", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setReadOnly(True)
        self.editor.document().modificationChanged.connect(self.setWindowModified)
        layout.addWidget(self.editor, 1)
        footer = QHBoxLayout()
        self.status = QLabel(tr("正在读取…"))
        footer.addWidget(self.status, 1)
        self.save_button = QPushButton(tr("保存 (Ctrl+S)"))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save)
        footer.addWidget(self.save_button)
        close_button = QPushButton(tr("关闭"))
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)
        save_action = QAction(self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        save_action.triggered.connect(self.save)
        self.addAction(save_action)
        threading.Thread(target=self._load_worker, name="sftp-edit-read", daemon=True).start()

    def _read_bytes(
        self, sftp: object, path: str, attributes: object | None = None
    ) -> bytes:
        attributes = attributes or sftp.stat(path)
        file_size = int(attributes.st_size or 0)
        if file_size > self._MAX_BYTES:
            raise SSHSessionError(tr("编辑器仅支持 4 MB 以内的文本文件，请下载后编辑。"))
        with sftp.open(path, "rb") as remote:
            prefetch = getattr(remote, "prefetch", None)
            if callable(prefetch) and file_size:
                try:
                    prefetch(file_size, max_concurrent_requests=16)
                except TypeError:
                    prefetch(file_size)
            data = remote.read(self._MAX_BYTES + 1)
        if len(data) > self._MAX_BYTES:
            raise SSHSessionError(tr("文件过大，请下载后编辑。"))
        return data

    def _load_worker(self) -> None:
        sftp = None
        try:
            # A new SFTP channel on the active SSH transport needs no second
            # TCP connection or authentication, so the editor opens promptly.
            sftp = self._session.open_isolated_sftp()
            sftp.get_channel().settimeout(20)
            path = posixpath.normpath(self._path)
            link_attributes = sftp.lstat(path)
            if stat.S_ISLNK(link_attributes.st_mode or 0):
                target = sftp.readlink(path)
                path = target if target.startswith("/") else posixpath.join(posixpath.dirname(path), target)
            path = sftp.normalize(path)
            data = self._read_bytes(sftp, path)
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                encoding = "utf-16"
            elif data.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            else:
                encoding = "utf-8"
            value = data.decode(encoding)
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
                raise SSHSessionError(tr("该文件不是可编辑的文本文件。"))
            self._events.loaded.emit((path, data, value, encoding))
        except Exception as exc:
            self._events.failed.emit(tr("读取失败：{0}", exc))
        finally:
            if sftp is not None:
                sftp.close()

    def _on_loaded(self, payload: object) -> None:
        self._path, self._original, value, self._encoding = payload
        self._newline = "\r\n" if "\r\n" in value else ("\r" if "\r" in value else "\n")
        self.editor.setUpdatesEnabled(False)
        self.editor.setUndoRedoEnabled(False)
        try:
            self.editor.setPlainText(value)
        finally:
            self.editor.setUndoRedoEnabled(True)
            self.editor.setUpdatesEnabled(True)
        self.editor.document().setModified(False)
        self._busy = False
        self._loaded = True
        self.editor.setReadOnly(False)
        self.save_button.setEnabled(True)
        self.status.setText(tr("{0} · 修改后保存到服务器", self._encoding))

    def save(self) -> None:
        if self._busy or not self._loaded:
            return
        if not self.editor.document().isModified():
            return
        value = self.editor.toPlainText().replace("\n", self._newline)
        # Preserve the byte order of an existing UTF-16 file, including its BOM.
        if self._encoding == "utf-16":
            codec = "utf-16-le" if self._original.startswith(b"\xff\xfe") else "utf-16-be"
            data = self._original[:2] + value.encode(codec)
        else:
            data = value.encode(self._encoding)
        if len(data) > self._MAX_BYTES:
            QMessageBox.warning(self, tr("无法保存"), tr("内容超过 4 MB，请下载后编辑。"))
            self._close_after_save = False
            return
        self._busy = True
        self.editor.setReadOnly(True)
        self.save_button.setEnabled(False)
        self.status.setText(tr("正在保存…"))
        threading.Thread(target=self._save_worker, args=(data,), name="sftp-edit-save", daemon=True).start()

    def _save_worker(self, data: bytes) -> None:
        sftp = None
        temporary = None
        try:
            sftp = self._session.open_isolated_sftp()
            sftp.get_channel().settimeout(20)
            attributes = sftp.stat(self._path)
            if self._read_bytes(sftp, self._path, attributes) != self._original:
                raise SSHSessionError(tr("服务器文件已被其他操作修改，本次未覆盖。请保留当前内容，重新打开文件后处理。"))
            candidate = posixpath.join(posixpath.dirname(self._path), f".deployflow-edit-{uuid.uuid4().hex}")
            with sftp.open(candidate, "wx") as remote:
                temporary = candidate
                sftp.chmod(temporary, 0o600)
                remote.write(data)
                remote.flush()
            new_attributes = sftp.stat(temporary)
            if (new_attributes.st_uid, new_attributes.st_gid) != (attributes.st_uid, attributes.st_gid):
                sftp.chown(temporary, attributes.st_uid, attributes.st_gid)
            sftp.chmod(temporary, stat.S_IMODE(attributes.st_mode))
            if self._read_bytes(sftp, self._path) != self._original:
                raise SSHSessionError(tr("保存期间服务器文件发生变化，本次未覆盖。"))
            # Do not truncate the original if writing or atomic replacement fails.
            sftp.posix_rename(temporary, self._path)
            temporary = None
            self._events.saved.emit(data)
        except Exception as exc:
            self._events.failed.emit(tr("保存失败：{0}", exc))
        finally:
            if sftp is not None:
                if temporary is not None:
                    try:
                        sftp.remove(temporary)
                    except Exception:
                        pass
                sftp.close()

    def _on_saved(self, data: object) -> None:
        self._original = data
        self._busy = False
        self.editor.setReadOnly(False)
        self.save_button.setEnabled(True)
        self.editor.document().setModified(False)
        self.status.setText(tr("已保存到服务器"))
        self.file_saved.emit(self._path)
        if self._close_after_save:
            self.close()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._close_after_save = False
        self.editor.setReadOnly(not self._loaded)
        self.save_button.setEnabled(self._loaded)
        self.status.setText(message)
        QMessageBox.warning(self, tr("远程文件编辑"), message)

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event: QEvent) -> None:
        if self._busy:
            self.raise_()
            QMessageBox.information(self, tr("正在处理"), tr("正在读取或保存文件，请稍候再关闭。"))
            event.ignore()
            return
        if self.editor.document().isModified():
            self.show()
            self.raise_()
            answer = QMessageBox.question(
                self, tr("尚未保存"), tr("是否将修改保存到服务器？"),
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save:
                self._close_after_save = True
                self.save()
                event.ignore()
                return
        event.accept()
        self.done(QDialog.Rejected)


class SftpTransferDialog(QDialog):
    """FinalShell-style remote file browser for an SSH session."""

    directory_changed = Signal(str)
    directory_listed = Signal(str)
    command_requested = Signal(str)
    _clipboard: _RemoteClipboard | None = None

    def __init__(
        self,
        parent: QWidget,
        session: InteractiveSSHSession,
        initial_remote_directory: str | None,
        embedded: bool = False,
        log_reader: Callable[[], str] | None = None,
        download_task_panel: _DownloadTaskPanel | None = None,
        server_key: str = "",
        render_allowed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._server_key = server_key or str(id(session))
        self._embedded = embedded
        self._render_allowed = render_allowed
        self._deferred_render: tuple[str, list[tuple[object, ...]]] | None = None
        self._render_defer_timer = QTimer(self)
        self._render_defer_timer.setSingleShot(True)
        self._render_defer_timer.setInterval(250)
        self._render_defer_timer.timeout.connect(self._resume_deferred_render)
        self._tree_locate_timer = QTimer(self)
        self._tree_locate_timer.setSingleShot(True)
        self._tree_locate_timer.setInterval(800)
        self._tree_locate_timer.timeout.connect(self._locate_current_tree_path)
        self._log_reader = log_reader
        self._displayed_log_text = ""
        self._download_task_panel = download_task_panel
        self._loading = False
        self._busy = False
        self._pending_remote_directory: str | None = None
        self._remote_navigation_target = posixpath.normpath(initial_remote_directory or "/")
        self._editors: dict[str, _RemoteFileEditor] = {}
        self._directory_history: list[str] = []
        self._tree_loading: set[str] = set()
        self._tree_loaded: set[str] = set()
        self._tree_request_id = 0
        self._tree_requests: dict[str, int] = {}
        self._file_cache: dict[str, list[tuple[object, ...]]] = {}
        self._file_types: dict[str, dict[str, bool]] = {}
        self._file_cache_times: dict[str, float] = {}
        self._directory_cache: dict[str, list[str]] = {}
        self._visible_cut_paths: set[str] = set()
        self._browse_request_id = 0
        self._active_browse_request = 0
        self._browse_sftp: object | None = None
        self._browse_sftp_lock = threading.RLock()
        self._tree_sftp: object | None = None
        self._tree_sftp_lock = threading.RLock()
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self._displayed_directory: str | None = None
        self._display_batch_id = 0
        self._operation_refresh_directory: str | None = None
        self._operation_affected_directories: set[str] = set()
        self._download_tasks: dict[str, _DownloadTask] = {}
        self._upload_task: _DownloadTask | None = None
        self._download_tasks_lock = threading.Lock()
        self._download_slots = threading.Semaphore(3)
        self._paste_progress_dialog: QProgressDialog | None = None
        local_directory = session.local_data_directory / "downloads"
        local_directory.mkdir(parents=True, exist_ok=True)
        self._last_local_directory = str(local_directory)
        self._local_browser_directory = str(local_directory)
        self._events = _TransferEvents(self)
        self._events.remote_loaded.connect(self._display_remote_files)
        self._events.remote_error.connect(self._display_remote_error)
        self._events.directories_loaded.connect(self._display_tree_directories)
        self._events.transfer_progress.connect(self._update_transfer_progress)
        self._events.operation_finished.connect(self._finish_operation)
        self._events.download_progress.connect(self._update_download_task)
        self._events.download_finished.connect(self._finish_download_task)
        self._events.upload_finished.connect(self._finish_upload_task)
        self._events.external_drop_resolved.connect(self._finish_external_download_drop)
        self._events.clipboard_finished.connect(self._finish_clipboard)
        self._events.clipboard_progress.connect(self._update_clipboard_progress)
        self._events.paste_preflighted.connect(self._handle_paste_preflight)
        self._events.permissions_loaded.connect(self._show_permissions_dialog)
        self._log_refresh_timer = QTimer(self)
        self._log_refresh_timer.setInterval(600)
        self._log_refresh_timer.timeout.connect(self._refresh_log_view)
        if self._download_task_panel is not None:
            self._download_task_panel.cancel_requested.connect(
                self.cancel_download_task
            )
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.setWindowTitle(tr("SFTP 文件传输"))
        if not embedded:
            self.resize(1080, 680)
        self._create_widgets(initial_remote_directory)
        self._theme_colors = {
            "surface": "#ffffff", "foreground": "#111827", "border": "#cbd5e1",
            "hover": "#f3f4f6", "selection": "#dbeafe", "selection_text": "#1d4ed8",
        }
        self.apply_theme(self._theme_colors)
        self._refresh_remote_directory()

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._theme_colors = dict(colors)
        self.file_tabs.setStyleSheet(
            f"QTabBar::tab {{ min-width:58px; padding:6px 10px; color:{colors['foreground']}; "
            f"background:{colors['hover']}; border:1px solid {colors['border']}; border-bottom:0; }}"
            f"QTabBar::tab:selected {{ background:{colors['surface']}; color:{colors['foreground']}; "
            "border-top:2px solid #3b82f6; font-weight:600; }"
        )

    def set_session(self, session: InteractiveSSHSession) -> None:
        if session is self._session:
            return
        self._render_defer_timer.stop()
        self._tree_locate_timer.stop()
        self._deferred_render = None
        self.cancel_all_downloads()
        self.release_browse_channel()
        self._session = session
        self._file_cache.clear()
        self._file_types.clear()
        self._file_cache_times.clear()
        self._directory_cache.clear()
        self._browse_request_id += 1
        self._active_browse_request = self._browse_request_id
        self._display_batch_id += 1
        self._loading = False
        self._pending_remote_directory = None
        self._displayed_directory = None
        self.remote_navigation.reset(self._remote_navigation_target)
        self._reset_directory_tree()

    def refresh(self) -> None:
        self._refresh_remote_directory()

    def open_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory.strip() or "/")
        self._remote_navigation_target = directory
        if self._loading or self._busy:
            self._pending_remote_directory = directory
            return
        self.remote_directory_entry.setText(directory)
        self._refresh_remote_directory()

    def current_directory(self) -> str:
        return self._displayed_directory or self._remote_navigation_target

    def _open_pending_directory(self) -> None:
        if self._pending_remote_directory is None or self._loading or self._busy:
            return
        directory, self._pending_remote_directory = self._pending_remote_directory, None
        self.open_directory(directory)

    def close_editors(self) -> bool:
        for editor in list(self._editors.values()):
            if not editor.close():
                return False
        return True

    def _edit_remote_file(self) -> None:
        entries = self._selected_remote_items()
        if len(entries) != 1 or entries[0][1] or self._busy:
            return
        self._edit_remote_path(entries[0][0])

    def _edit_remote_path(self, path: str) -> None:
        if self._busy or not self._session.connected:
            return
        editor = self._editors.get(path)
        if editor is None:
            editor = _RemoteFileEditor(self, self._session, path)
            self._editors[path] = editor
            editor.file_saved.connect(self._remote_file_saved)
            editor.finished.connect(lambda _result, key=path: self._release_editor(key))
        editor.show()
        editor.raise_()
        editor.activateWindow()

    def _release_editor(self, path: str) -> None:
        editor = self._editors.pop(path, None)
        if editor is not None:
            editor.deleteLater()

    def _remote_file_saved(self, path: str) -> None:
        self._invalidate_directory(posixpath.dirname(path))
        self._refresh_remote_directory(force=True)
        self.directory_changed.emit(posixpath.dirname(path))

    def release_browse_channel(self) -> None:
        for lock, attribute in (
            (self._browse_sftp_lock, "_browse_sftp"),
            (self._tree_sftp_lock, "_tree_sftp"),
        ):
            with lock:
                sftp = getattr(self, attribute)
                setattr(self, attribute, None)
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass

    def _browse_call(self, action: Callable[[object], object]) -> object:
        return self._sftp_channel_call(action, "_browse_sftp", self._browse_sftp_lock)

    def _tree_browse_call(self, action: Callable[[object], object]) -> object:
        return self._sftp_channel_call(action, "_tree_sftp", self._tree_sftp_lock)

    def _sftp_channel_call(
        self,
        action: Callable[[object], object],
        channel_attribute: str,
        channel_lock: threading.RLock,
    ) -> object:
        with channel_lock:
            for attempt in range(2):
                try:
                    sftp = getattr(self, channel_attribute)
                    if sftp is None:
                        # Browsing must not compete with the interactive terminal.
                        sftp = self._session.open_isolated_sftp(allow_shared_fallback=False)
                        setattr(self, channel_attribute, sftp)
                    return action(sftp)
                except Exception:
                    sftp = getattr(self, channel_attribute)
                    setattr(self, channel_attribute, None)
                    if sftp is not None:
                        try:
                            sftp.close()
                        except Exception:
                            pass
                    if attempt:
                        raise
        raise SSHSessionError(tr("无法读取服务器目录"))

    @staticmethod
    def _list_directory_attributes(sftp: object, directory: str) -> list[object]:
        iterator = getattr(sftp, "listdir_iter", None)
        if callable(iterator):
            return list(iterator(directory, read_aheads=64))
        return list(sftp.listdir_attr(directory))

    def _create_widgets(self, initial_remote_directory: str | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.file_tabs = QTabBar()
        self.file_tabs.setExpanding(False)
        self.file_tabs.addTab(tr("文件"))
        self.file_tabs.addTab(tr("日志"))
        self.file_tabs.setStyleSheet(
            "QTabBar::tab { min-width:58px; padding:6px 10px; background:#f3f4f6; "
            "border:1px solid #cbd5e1; border-bottom:0; }"
            "QTabBar::tab:selected { background:#ffffff; color:#111827; "
            "border-top:2px solid #3b82f6; font-weight:600; }"
        )
        self.file_tabs.currentChanged.connect(self._file_tab_changed)
        root.addWidget(self.file_tabs)

        self.content_stack = QStackedWidget()
        self.file_page = QWidget()
        file_root = QVBoxLayout(self.file_page)
        file_root.setContentsMargins(0, 0, 0, 0)
        file_root.setSpacing(0)
        self.content_stack.addWidget(self.file_page)
        root.addWidget(self.content_stack, 1)

        remote_path_row = QHBoxLayout()
        remote_path_row.setContentsMargins(6, 4, 6, 4)
        remote_path_row.setSpacing(4)
        self.remote_navigation = _DirectoryNavigation(self._remote_navigation_target)
        self.remote_directory_entry = self.remote_navigation.entry
        self.remote_navigation.path_requested.connect(self.open_directory)
        self.remote_navigation.parent_requested.connect(self._go_remote_parent)
        remote_path_row.addWidget(self.remote_navigation, 1)
        self.history_button = QPushButton(tr("历史"))
        self.history_button.clicked.connect(self._show_directory_history)
        remote_path_row.addWidget(self.history_button)
        self.remote_directory_entry.setPlaceholderText(tr("服务器目录（回车打开）"))
        remote_path_row.insertWidget(0, QLabel(tr("服务器：")))

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        self.directory_tree = QTreeWidget(self)
        self.directory_tree.setHeaderHidden(True)
        self.directory_tree.setMinimumWidth(150)
        self.directory_tree.setMaximumWidth(280)
        self.directory_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.directory_tree.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.directory_tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.directory_tree.header().setStretchLastSection(False)
        self.directory_tree.header().setResizeContentsPrecision(100)
        self.directory_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.directory_tree.itemExpanded.connect(self._directory_tree_expanded)
        self.directory_tree.itemClicked.connect(self._directory_tree_clicked)
        self.directory_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.directory_tree.customContextMenuRequested.connect(self._show_directory_context_menu)
        for shortcut, handler in (
            (QKeySequence.Copy, lambda: self._directory_path_action("copy")),
            (QKeySequence.Cut, lambda: self._directory_path_action("cut")),
            (QKeySequence.Delete, lambda: self._directory_path_action("delete")),
        ):
            action = QAction(self.directory_tree)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _checked=False, call=handler: call())
            self.directory_tree.addAction(action)
        root_item = QTreeWidgetItem(["/"])
        root_item.setData(0, Qt.UserRole, "/")
        root_item.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
        root_item.addChild(QTreeWidgetItem([""]))
        self.directory_tree.addTopLevelItem(root_item)
        self._tree_items["/"] = root_item
        if self._embedded:
            file_root.addLayout(remote_path_row)
            splitter.addWidget(self.directory_tree)
        else:
            self._create_local_browser(splitter)

        self.remote_tree = _RemoteFileTree()
        self.remote_tree.setMinimumWidth(200)
        self.remote_tree.setHeaderLabels(
            [tr("文件名"), tr("大小"), tr("类型"), tr("修改时间"), tr("权限"), tr("用户/用户组")]
        )
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setAlternatingRowColors(True)
        self.remote_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_tree.setSortingEnabled(True)
        self.remote_tree.itemDoubleClicked.connect(self._remote_item_activated)
        self.remote_tree.files_dropped.connect(self._upload_paths)
        self.remote_tree.download_drag_requested.connect(self._start_remote_download_drag)
        self.remote_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self._show_remote_context_menu)
        for shortcut, handler in (
            (QKeySequence.Copy, lambda: self._copy_remote_items(False)),
            (QKeySequence.Cut, lambda: self._copy_remote_items(True)),
            (QKeySequence.Delete, self._delete_remote_items),
            (QKeySequence("F2"), self._rename_remote_item),
            (QKeySequence.SelectAll, self.remote_tree.selectAll),
        ):
            action = QAction(self.remote_tree)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _checked=False, call=handler: call())
            self.remote_tree.addAction(action)
        paste_action = QAction(self.file_page)
        paste_action.setShortcut(QKeySequence.Paste)
        paste_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        paste_action.triggered.connect(
            lambda: self._directory_path_action("paste")
            if self.directory_tree.hasFocus() else self._paste_remote_items()
        )
        self.file_page.addAction(paste_action)
        if self._embedded:
            splitter.addWidget(self.remote_tree)
        else:
            remote_panel = QWidget()
            remote_layout = QVBoxLayout(remote_panel)
            remote_layout.setContentsMargins(0, 0, 0, 0)
            remote_layout.addLayout(remote_path_row)
            remote_splitter = QSplitter(Qt.Horizontal)
            remote_splitter.setChildrenCollapsible(False)
            remote_splitter.setHandleWidth(6)
            remote_splitter.addWidget(self.directory_tree)
            remote_splitter.addWidget(self.remote_tree)
            remote_splitter.setStretchFactor(0, 0)
            remote_splitter.setStretchFactor(1, 1)
            remote_splitter.setSizes([150, 430])
            remote_layout.addWidget(remote_splitter, 1)
            splitter.addWidget(remote_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([190, 850] if self._embedded else [500, 580])
        file_root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(6, 3, 6, 3)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(240)
        self.progress.setFixedHeight(16)
        footer.addWidget(self.progress, 1)
        self.status_label = QLabel(tr("准备就绪"))
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        if not self._embedded:
            close_button = QPushButton(tr("关闭"))
            close_button.clicked.connect(self.close)
            footer.addWidget(close_button)
        file_root.addLayout(footer)

        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(6, 6, 6, 6)
        log_layout.setSpacing(4)
        self.log_title = QLabel(
            tr("本次连接日志 · {0}", datetime.now().strftime('%Y-%m-%d'))
        )
        self.log_title.setStyleSheet("font-weight:600;")
        log_layout.addWidget(self.log_title)
        self.connection_log_text = QPlainTextEdit()
        self.connection_log_text.setReadOnly(True)
        self.connection_log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.connection_log_text.setFont(QFont("Cascadia Mono", 9))
        log_layout.addWidget(self.connection_log_text, 1)
        self.content_stack.addWidget(log_page)

    def _reset_directory_tree(self) -> None:
        if not hasattr(self, "directory_tree"):
            return
        root_item = self.directory_tree.topLevelItem(0)
        if root_item is None:
            return
        root_item.takeChildren()
        root_item.addChild(QTreeWidgetItem([""]))
        root_item.setExpanded(False)
        self._tree_loading.clear()
        self._tree_loaded.clear()
        self._tree_requests.clear()
        self._tree_items = {"/": root_item}

    def _create_local_browser(self, splitter: QSplitter) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(6, 4, 6, 4)
        path_row.setSpacing(4)
        path_row.addWidget(QLabel(tr("本地：")))
        self.local_navigation = _DirectoryNavigation(self._local_browser_directory, local=True)
        self.local_directory_entry = self.local_navigation.entry
        self.local_navigation.path_requested.connect(self._navigate_local_directory)
        self.local_navigation.parent_requested.connect(self._local_parent)
        path_row.addWidget(self.local_navigation, 1)
        choose_button = QPushButton(tr("选择"))
        choose_button.clicked.connect(self._choose_local_directory)
        path_row.addWidget(choose_button)
        layout.addLayout(path_row)
        self.local_model = QFileSystemModel(self)
        self.local_model.setReadOnly(True)
        self.local_model.setRootPath("")
        self.local_model.setRootPath(self._local_browser_directory)
        browser_splitter = QSplitter(Qt.Horizontal)
        browser_splitter.setChildrenCollapsible(False)
        browser_splitter.setHandleWidth(6)
        navigation_panel = QWidget()
        navigation_panel.setMinimumWidth(120)
        navigation_panel.setMaximumWidth(240)
        navigation_layout = QVBoxLayout(navigation_panel)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(2)
        self.local_shortcuts = QTreeWidget()
        self.local_shortcuts.setHeaderHidden(True)
        self.local_shortcuts.setRootIsDecorated(False)
        shortcuts = [(tr("此电脑"), "")]
        for name, location in (
            (tr("桌面"), QStandardPaths.DesktopLocation),
            (tr("下载"), QStandardPaths.DownloadLocation),
            (tr("文档"), QStandardPaths.DocumentsLocation),
            (tr("主目录"), QStandardPaths.HomeLocation),
        ):
            path = QStandardPaths.writableLocation(location)
            if path:
                shortcuts.append((name, path))
        for name, path in shortcuts:
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.UserRole, path)
            item.setToolTip(0, path if path else tr("此电脑"))
            item.setIcon(0, self.style().standardIcon(QStyle.SP_ComputerIcon if not path else QStyle.SP_DirIcon))
            self.local_shortcuts.addTopLevelItem(item)
        self.local_shortcuts.setFixedHeight(len(shortcuts) * (self.fontMetrics().height() + 8) + 4)
        self.local_shortcuts.itemClicked.connect(
            lambda item, _column: self.local_navigation.request_directory(str(item.data(0, Qt.UserRole)))
        )
        navigation_layout.addWidget(self.local_shortcuts)
        navigation_layout.addWidget(QLabel(tr("此电脑")))
        self.local_directory_model = QFileSystemModel(self)
        self.local_directory_model.setReadOnly(True)
        self.local_directory_model.setFilter(QDir.AllDirs | QDir.Drives | QDir.NoDotAndDotDot)
        self.local_directory_model.setRootPath("")
        self.local_directory_tree = _LocalFileTree()
        self.local_directory_tree.setModel(self.local_directory_model)
        self.local_directory_tree.setDragEnabled(True)
        self.local_directory_tree.setHeaderHidden(True)
        self.local_directory_tree.setMinimumWidth(100)
        self.local_directory_tree.header().setStretchLastSection(False)
        self.local_directory_tree.header().setResizeContentsPrecision(100)
        self.local_directory_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.local_directory_tree.setUniformRowHeights(True)
        for column in range(1, self.local_directory_model.columnCount()):
            self.local_directory_tree.hideColumn(column)
        self.local_directory_tree.clicked.connect(
            lambda index: self.local_navigation.request_directory(self.local_directory_model.filePath(index))
        )
        navigation_layout.addWidget(self.local_directory_tree, 1)
        browser_splitter.addWidget(navigation_panel)
        self.local_tree = _LocalFileTree()
        self.local_tree.setMinimumWidth(180)
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(self._local_browser_directory))
        self.local_tree.setRootIsDecorated(False)
        self.local_tree.setItemsExpandable(False)
        self.local_tree.setExpandsOnDoubleClick(False)
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_tree.setDragEnabled(True)
        self.local_tree.setDragDropMode(QAbstractItemView.DragDrop)
        self.local_tree.setDefaultDropAction(Qt.CopyAction)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.setColumnWidth(0, 220)
        self.local_tree.doubleClicked.connect(self._local_item_activated)
        self.local_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self._show_local_context_menu)
        browser_splitter.addWidget(self.local_tree)
        browser_splitter.setStretchFactor(0, 0)
        browser_splitter.setStretchFactor(1, 1)
        browser_splitter.setSizes([150, 350])
        layout.addWidget(browser_splitter, 1)
        self.local_upload_button = QPushButton(tr("上传选中项 →"))
        self.local_upload_button.clicked.connect(self._upload_local_selection)
        layout.addWidget(self.local_upload_button)
        splitter.addWidget(panel)
        self._sync_local_directory_tree()

    def _open_local_directory(self) -> None:
        directory = self.local_directory_entry.text().strip()
        self._navigate_local_directory("" if directory == tr("此电脑") else directory)

    def _navigate_local_directory(self, directory: str) -> None:
        if not directory:
            self._local_browser_directory = ""
            self.local_tree.setRootIndex(self.local_model.index(""))
            self.local_navigation.set_directory("")
            self._sync_local_directory_tree()
            return
        path = Path(directory).expanduser()
        if not path.is_dir():
            QMessageBox.warning(self, tr("目录不存在"), tr("请选择有效的本地文件夹。"))
            self.local_navigation.cancel_navigation()
            self._sync_local_directory_tree()
            return
        self._last_local_directory = str(path.resolve())
        self._local_browser_directory = self._last_local_directory
        self.local_tree.setRootIndex(self.local_model.setRootPath(self._last_local_directory))
        self.local_navigation.set_directory(self._local_browser_directory)
        self._sync_local_directory_tree()

    def _sync_local_directory_tree(self) -> None:
        if not self._local_browser_directory:
            self.local_directory_tree.setCurrentIndex(self.local_directory_model.index(""))
            self.local_directory_tree.clearSelection()
            self.local_shortcuts.setCurrentItem(self.local_shortcuts.topLevelItem(0))
            return
        self.local_shortcuts.clearSelection()
        index = self.local_directory_model.index(self._local_browser_directory)
        if not index.isValid():
            return
        parent = index.parent()
        while parent.isValid():
            self.local_directory_tree.setExpanded(parent, True)
            parent = parent.parent()
        self.local_directory_tree.setCurrentIndex(index)
        self.local_directory_tree.scrollTo(index)

    def _local_parent(self) -> None:
        if not self._local_browser_directory:
            return
        path = Path(self._local_browser_directory)
        directory = "" if path.parent == path else str(path.parent)
        self.local_navigation.request_directory(directory)

    def _choose_local_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("选择本地目录"), self._last_local_directory)
        if path:
            self.local_navigation.request_directory(path)

    def _local_item_activated(self, index: object) -> None:
        if self.local_model.isDir(index):
            self.local_navigation.request_directory(self.local_model.filePath(index))
        else:
            self._upload_local_selection()

    def _upload_local_selection(self) -> None:
        self._upload_paths([
            Path(self.local_model.filePath(index))
            for index in self.local_tree.selectionModel().selectedRows(0)
        ])

    def _show_local_context_menu(self, position: QPoint) -> None:
        index = self.local_tree.indexAt(position)
        if index.isValid() and not self.local_tree.selectionModel().isSelected(index):
            self.local_tree.setCurrentIndex(index)
        menu = QMenu(self)
        upload = menu.addAction(tr("上传选中项到右侧目录"))
        upload.setEnabled(bool(self.local_tree.selectionModel().selectedRows(0)) and not self._busy)
        if menu.exec(self.local_tree.viewport().mapToGlobal(position)) is upload:
            self._upload_local_selection()

    def _file_tab_changed(self, index: int) -> None:
        self.content_stack.setCurrentIndex(index)
        if index == 1:
            self._refresh_log_view()
            self._log_refresh_timer.start()
        else:
            self._log_refresh_timer.stop()

    def _refresh_log_view(self) -> None:
        if self.file_tabs.currentIndex() != 1:
            return
        if self._log_reader is None:
            content = tr("当前连接暂无可读取的日志。")
        else:
            try:
                content = self._log_reader()
            except (OSError, UnicodeError) as exc:
                content = tr("读取日志失败：{0}", exc)
        if content == self._displayed_log_text:
            return
        editor = self.connection_log_text
        scrollbar = editor.verticalScrollBar()
        follow = scrollbar.value() >= scrollbar.maximum() - 2
        if content.startswith(self._displayed_log_text):
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(content[len(self._displayed_log_text):])
            editor.setTextCursor(cursor)
        else:
            editor.setPlainText(content)
        self._displayed_log_text = content
        if follow:
            editor.verticalScrollBar().setValue(editor.verticalScrollBar().maximum())

    def _select_upload(self, folder: bool = False) -> None:
        if self._busy:
            return
        if not folder:
            files, _selected_filter = QFileDialog.getOpenFileNames(
                self, tr("选择要上传的文件"), self._last_local_directory
            )
            if files:
                self._last_local_directory = str(Path(files[0]).parent)
                self._upload_paths([Path(path) for path in files])
        else:
            folder = QFileDialog.getExistingDirectory(
                self, tr("选择要上传的文件夹"), self._last_local_directory
            )
            if folder:
                self._last_local_directory = str(Path(folder).parent)
                self._upload_paths([Path(folder)])

    def _go_remote_parent(self) -> None:
        current = self.current_directory()
        self.remote_navigation.request_directory(posixpath.dirname(posixpath.normpath(current)) or "/")

    def _show_directory_history(self) -> None:
        menu = QMenu(self)
        if not self._directory_history:
            empty_action = menu.addAction(tr("暂无历史目录"))
            empty_action.setEnabled(False)
        actions: dict[object, str] = {}
        for directory in reversed(self._directory_history[-20:]):
            action = menu.addAction(directory)
            actions[action] = directory
        selected = menu.exec(
            self.history_button.mapToGlobal(QPoint(0, self.history_button.height()))
        )
        if selected in actions:
            self.remote_navigation.request_directory(actions[selected])

    def _directory_tree_expanded(self, item: QTreeWidgetItem) -> None:
        directory = item.data(0, Qt.UserRole)
        if isinstance(directory, str):
            self._request_tree_directory(directory)

    def _directory_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        directory = item.data(0, Qt.UserRole)
        if not isinstance(directory, str):
            return
        self.remote_navigation.request_directory(directory)

    def _request_tree_directory(self, directory: str, force: bool = False) -> None:
        directory = posixpath.normpath(directory or "/")
        if force:
            self._directory_cache.pop(directory, None)
            self._tree_loaded.discard(directory)
            self._tree_requests.pop(directory, None)
            self._tree_loading.discard(directory)
        if directory in self._tree_loaded:
            return
        cached = self._directory_cache.get(directory)
        if cached is not None:
            self._display_tree_directories(directory, (list(cached), ""))
            return
        if directory in self._tree_loading:
            return
        self._tree_loading.add(directory)
        self._tree_request_id += 1
        request_id = self._tree_request_id
        self._tree_requests[directory] = request_id
        threading.Thread(
            target=self._load_tree_directory_worker,
            args=(directory, request_id),
            name="sftp-directory-tree",
            daemon=True,
        ).start()

    def _load_tree_directory_worker(self, directory: str, request_id: int) -> None:
        try:
            directories = self._tree_browse_call(
                lambda sftp: sorted(
                    attribute.filename
                    for attribute in self._list_directory_attributes(sftp, directory)
                    if stat.S_ISDIR(attribute.st_mode or 0)
                )
            )
        except Exception as exc:
            self._events.directories_loaded.emit(directory, (request_id, [], str(exc)))
        else:
            self._events.directories_loaded.emit(directory, (request_id, directories, ""))

    def _display_tree_directories(self, directory: str, payload: object) -> None:
        if len(payload) == 3:
            request_id, directories, error = payload
            if self._tree_requests.get(directory) != request_id:
                return
        else:
            directories, error = payload
        self._tree_requests.pop(directory, None)
        self._tree_loading.discard(directory)
        if error:
            return
        directories = list(directories)
        self._directory_cache[directory] = directories
        parent_item = self._find_directory_tree_item(directory)
        if parent_item is None:
            return
        self._tree_loaded.add(directory)
        for child_index in range(parent_item.childCount()):
            self._remove_tree_item_index(parent_item.child(child_index))
        parent_item.takeChildren()
        items: list[QTreeWidgetItem] = []
        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        for name in directories:
            path = posixpath.join(directory, name)
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.UserRole, path)
            item.setIcon(0, folder_icon)
            item.addChild(QTreeWidgetItem([""]))
            self._tree_items[path] = item
            items.append(item)
        self.directory_tree.setUpdatesEnabled(False)
        try:
            parent_item.addChildren(items)
        finally:
            self.directory_tree.setUpdatesEnabled(True)
        if directory == "/":
            parent_item.setExpanded(True)
        self._schedule_tree_location()

    def _schedule_tree_location(self, directory: str | None = None) -> None:
        self._pending_tree_location = directory or self.current_directory()
        self._tree_locate_timer.start()

    def _locate_current_tree_path(self) -> None:
        self._locate_directory_tree_path(getattr(self, "_pending_tree_location", "/"))

    def _locate_directory_tree_path(self, directory: str) -> None:
        target = posixpath.normpath(directory or "/")
        current = "/"
        while True:
            if self._find_directory_tree_item(current) is None:
                return
            if current not in self._tree_loaded:
                self._request_tree_directory(current)
                return
            if current == target:
                self._select_directory_tree_path(target)
                return
            relative = posixpath.relpath(target, current)
            if relative == ".." or relative.startswith("../"):
                return
            current = posixpath.join(current, relative.split("/", 1)[0])

    def _find_directory_tree_item(self, directory: str) -> QTreeWidgetItem | None:
        return self._tree_items.get(posixpath.normpath(directory or "/"))

    def _remove_tree_item_index(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if isinstance(path, str):
            self._tree_items.pop(path, None)
            self._tree_loaded.discard(path)
            self._tree_loading.discard(path)
            self._tree_requests.pop(path, None)
        for index in range(item.childCount()):
            self._remove_tree_item_index(item.child(index))

    def _select_directory_tree_path(self, directory: str) -> None:
        item = self._find_directory_tree_item(posixpath.normpath(directory or "/"))
        if item is not None:
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            self.directory_tree.setCurrentItem(item)
            self.directory_tree.scrollToItem(item)

    def _remote_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        remote_path = str(item.data(0, Qt.UserRole))
        if bool(item.data(0, Qt.UserRole + 1)):
            self.remote_navigation.request_directory(remote_path)

    def _force_refresh_remote_directory(self) -> None:
        self._refresh_remote_directory(force=True)

    def _refresh_remote_directory(self, force: bool = False) -> None:
        directory = self._remote_navigation_target
        if force:
            self._invalidate_directory(directory)
        cached = self._file_cache.get(directory)
        if cached is not None:
            if directory != self._displayed_directory:
                self._render_remote_files(directory, list(cached))
            else:
                self.remote_navigation.set_directory(directory)
                self.status_label.setText(tr("服务器目录：{0} 项", len(cached)))
            cache_age = time.monotonic() - self._file_cache_times.get(directory, 0.0)
            if not force and cache_age < _DIRECTORY_CACHE_TTL_SECONDS:
                return
        if self._loading or self._busy:
            self._pending_remote_directory = directory
            if cached is None:
                self.status_label.setText(tr("等待读取服务器目录…"))
            else:
                self.status_label.setText(tr("已显示缓存：{0} 项，等待刷新…", len(cached)))
            return
        self._loading = True
        self._browse_request_id += 1
        request_id = self._browse_request_id
        self._active_browse_request = request_id
        self.status_label.setText(
            tr("已显示缓存：{0} 项，正在刷新…", len(cached))
            if cached is not None else tr("正在读取服务器目录…")
        )
        threading.Thread(
            target=self._load_remote_worker,
            args=(request_id, directory),
            name="sftp-list-directory",
            daemon=True,
        ).start()

    def _load_remote_worker(self, request_id: int, directory: str) -> None:
        try:
            entries = self._browse_call(
                lambda sftp: [
                    (
                        attribute.filename,
                        stat.S_ISDIR(attribute.st_mode or 0),
                        int(attribute.st_size or 0),
                        int(attribute.st_mode or 0),
                        int(attribute.st_mtime or 0),
                        int(attribute.st_uid or 0),
                        int(attribute.st_gid or 0),
                    )
                    for attribute in self._list_directory_attributes(sftp, directory)
                ]
            )
        except Exception as exc:
            self._events.remote_error.emit(request_id, directory, str(exc))
        else:
            self._events.remote_loaded.emit(request_id, directory, entries)

    def _display_remote_files(
        self, request_id: int, directory: str, entries: object
    ) -> None:
        if request_id != self._active_browse_request:
            return
        if self._render_allowed is not None and not self._render_allowed():
            QTimer.singleShot(
                250,
                lambda: self._display_remote_files(request_id, directory, entries),
            )
            return
        directory = posixpath.normpath(directory or "/")
        entries = list(entries)
        self._cache_remote_files(directory, entries)
        self._loading = False
        pending = self._pending_remote_directory
        if pending is not None and posixpath.normpath(pending or "/") != directory:
            QTimer.singleShot(0, self._open_pending_directory)
            return
        current = self._remote_navigation_target
        if current != directory:
            self._pending_remote_directory = current
            QTimer.singleShot(0, self._open_pending_directory)
            return
        self._render_remote_files(directory, entries)
        QTimer.singleShot(0, self._open_pending_directory)

    def _cache_remote_files(
        self, directory: str, entries: list[tuple[object, ...]]
    ) -> None:
        self._file_cache[directory] = entries
        self._file_types[directory] = {
            str(entry[0]): bool(entry[1]) for entry in entries
            if stat.S_ISDIR(int(entry[3])) or stat.S_ISREG(int(entry[3]))
        }
        self._file_cache_times[directory] = time.monotonic()
        self._directory_cache[directory] = sorted(
            str(entry[0]) for entry in entries if bool(entry[1])
        )
        if directory not in self._tree_loaded and self._find_directory_tree_item(directory) is not None:
            self._display_tree_directories(directory, (list(self._directory_cache[directory]), ""))
        while len(self._file_cache) > _DIRECTORY_CACHE_LIMIT:
            candidates = [
                path for path in self._file_cache_times
                if path != self._displayed_directory
            ]
            if not candidates:
                break
            oldest = min(candidates, key=self._file_cache_times.get)
            self._file_cache.pop(oldest, None)
            self._file_types.pop(oldest, None)
            self._file_cache_times.pop(oldest, None)
            self._directory_cache.pop(oldest, None)
        self.directory_listed.emit(directory)

    def cached_path_type(self, path: str) -> bool | None:
        path = posixpath.normpath(path)
        now = time.monotonic()
        parent = posixpath.dirname(path)
        if now - self._file_cache_times.get(parent, 0.0) < PATH_TYPE_CACHE_TTL:
            kind = self._file_types.get(parent, {}).get(posixpath.basename(path))
            if kind is not None:
                return kind
        if path in self._file_types and now - self._file_cache_times.get(path, 0.0) < PATH_TYPE_CACHE_TTL:
            return True
        return None

    def _render_remote_files(
        self, directory: str, entries: list[tuple[object, ...]]
    ) -> None:
        if self._render_allowed is not None and not self._render_allowed():
            self._display_batch_id += 1
            self._deferred_render = (directory, entries)
            self._render_defer_timer.start()
            return
        self._deferred_render = None
        self._render_defer_timer.stop()
        directory = posixpath.normpath(directory or "/")
        self._displayed_directory = directory
        self.remote_navigation.set_directory(directory)
        if directory in self._directory_history:
            self._directory_history.remove(directory)
        self._directory_history.append(directory)
        self._directory_history = self._directory_history[-30:]
        tree = self.remote_tree
        header = tree.header()
        sort_column = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        tree.setUpdatesEnabled(False)
        tree.setSortingEnabled(False)
        self._visible_cut_paths.clear()
        tree.clear()
        tree.setUpdatesEnabled(True)
        self._display_batch_id += 1
        batch_id = self._display_batch_id
        ordered = sorted(entries, key=lambda item: (not item[1], item[0].lower()))
        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        self._build_remote_item_batch(
            batch_id, directory, ordered, [], "", folder_icon, file_icon,
            sort_column, sort_order, 0,
        )

    def _build_remote_item_batch(
        self, batch_id: int, directory: str, entries: list[tuple[object, ...]],
        items: list[QTreeWidgetItem], longest_name: str, folder_icon: object,
        file_icon: object, sort_column: int, sort_order: Qt.SortOrder, offset: int,
    ) -> None:
        if batch_id != self._display_batch_id:
            return
        if self._render_allowed is not None and not self._render_allowed():
            QTimer.singleShot(
                250,
                lambda: self._build_remote_item_batch(
                    batch_id, directory, entries, items, longest_name,
                    folder_icon, file_icon, sort_column, sort_order, offset,
                ),
            )
            return
        end = min(len(entries), offset + _REMOTE_ITEM_BATCH_SIZE)
        for name, is_directory, size, mode, modified, uid, gid in entries[offset:end]:
            if len(name) > len(longest_name):
                longest_name = name
            path = posixpath.join(directory, name)
            item = QTreeWidgetItem([
                name,
                "" if is_directory else self._format_size(size),
                tr("文件夹") if is_directory else tr("文件"),
                (
                    datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
                    if modified else ""
                ),
                stat.filemode(mode),
                f"{uid}/{gid}",
            ])
            item.setIcon(0, folder_icon if is_directory else file_icon)
            item.setData(0, Qt.UserRole, path)
            item.setData(0, Qt.UserRole + 1, is_directory)
            item.setData(0, Qt.UserRole + 2, mode)
            items.append(item)
        if end < len(entries):
            QTimer.singleShot(
                0,
                lambda: self._build_remote_item_batch(
                    batch_id, directory, entries, items, longest_name,
                    folder_icon, file_icon, sort_column, sort_order, end,
                ),
            )
            return
        self._append_remote_item_batch(
            batch_id, directory, items, longest_name,
            sort_column, sort_order, 0,
        )

    def _resume_deferred_render(self) -> None:
        pending = self._deferred_render
        if pending is None:
            return
        directory, entries = pending
        current = self._remote_navigation_target
        if current != posixpath.normpath(directory):
            self._deferred_render = None
            return
        self._render_remote_files(directory, entries)

    def _append_remote_item_batch(
        self,
        batch_id: int,
        directory: str,
        items: list[QTreeWidgetItem],
        longest_name: str,
        sort_column: int,
        sort_order: Qt.SortOrder,
        offset: int,
    ) -> None:
        if batch_id != self._display_batch_id:
            return
        if self._render_allowed is not None and not self._render_allowed():
            QTimer.singleShot(
                250,
                lambda: self._append_remote_item_batch(
                    batch_id, directory, items, longest_name,
                    sort_column, sort_order, offset,
                ),
            )
            return
        end = min(len(items), offset + _REMOTE_ITEM_BATCH_SIZE)
        self.remote_tree.setUpdatesEnabled(False)
        try:
            self.remote_tree.addTopLevelItems(items[offset:end])
        finally:
            self.remote_tree.setUpdatesEnabled(True)
        if end < len(items):
            self.status_label.setText(
                tr("正在显示服务器目录：{0}/{1} 项", end, len(items))
            )
            QTimer.singleShot(
                0,
                lambda: self._append_remote_item_batch(
                    batch_id, directory, items, longest_name,
                    sort_column, sort_order, end,
                ),
            )
            return
        self.remote_tree.setColumnWidth(
            0,
            min(
                420,
                max(
                    180,
                    self.remote_tree.fontMetrics().horizontalAdvance(longest_name) + 48,
                ),
            ),
        )
        self.remote_tree.setSortingEnabled(True)
        self.remote_tree.sortItems(sort_column, sort_order)
        self._update_cut_item_visuals()
        self._select_directory_tree_path(directory)
        self._schedule_tree_location(directory)
        self.status_label.setText(tr("服务器目录：{0} 项", len(items)))

    def _invalidate_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory or "/")
        prefix = directory.rstrip("/") + "/"
        if self._remote_navigation_target == directory or self._remote_navigation_target.startswith(prefix):
            if self._loading:
                self._browse_request_id += 1
                self._active_browse_request = self._browse_request_id
                self._loading = False
        if self._displayed_directory == directory or (self._displayed_directory or "").startswith(prefix):
            self._display_batch_id += 1
        if self._deferred_render is not None and (
            self._deferred_render[0] == directory
            or self._deferred_render[0].startswith(directory.rstrip("/") + "/")
        ):
            self._render_defer_timer.stop()
            self._deferred_render = None
        affected = {directory} | {
            path for path in set(self._file_cache) | set(self._directory_cache) | self._tree_loaded | self._tree_loading
            if path.startswith(prefix)
        }
        for path in affected:
            self._file_cache.pop(path, None)
            self._file_types.pop(path, None)
            self._file_cache_times.pop(path, None)
            self._directory_cache.pop(path, None)
            self._tree_loaded.discard(path)
            self._tree_loading.discard(path)
            self._tree_requests.pop(path, None)

    def _display_remote_error(
        self, request_id: int, directory: str, message: str
    ) -> None:
        if request_id != self._active_browse_request:
            return
        self._loading = False
        pending = self._pending_remote_directory
        if pending is not None and posixpath.normpath(pending or "/") != directory:
            QTimer.singleShot(0, self._open_pending_directory)
            return
        self.status_label.setText(tr("读取失败"))
        self._pending_remote_directory = None
        self._remote_navigation_target = self._displayed_directory or self.remote_navigation._directory
        self.remote_navigation.cancel_navigation()
        QMessageBox.critical(self, tr("无法读取服务器目录"), message)
        QTimer.singleShot(0, self._open_pending_directory)

    def _upload_paths(self, paths: object) -> None:
        sources = [Path(path) for path in paths if Path(path).is_file() or Path(path).is_dir()]
        if not sources or self._busy:
            return
        remote_directory = self.current_directory()
        if not remote_directory:
            QMessageBox.warning(self, tr("未填写目录"), tr("请填写服务器目标目录"))
            return
        self._operation_refresh_directory = posixpath.normpath(remote_directory)
        title = sources[0].name if len(sources) == 1 else tr("{0} 等 {1} 项", sources[0].name, len(sources))
        task = _DownloadTask(task_id=uuid.uuid4().hex, title=title)
        self._upload_task = task
        if self._download_task_panel is not None:
            self._download_task_panel.add_task(task.task_id, title, direction="upload")
            self.progress.hide()
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText(tr("准备上传…"))
        threading.Thread(
            target=self._upload_worker,
            args=(task, self._session, sources, remote_directory),
            name="sftp-upload",
            daemon=True,
        ).start()

    def _upload_worker(
        self, task: _DownloadTask, session: InteractiveSSHSession,
        sources: list[Path], remote_directory: str,
    ) -> None:
        try:
            if task.cancel_event.is_set():
                raise _DownloadCancelled()
            sftp = session.open_isolated_sftp(allow_shared_fallback=False)
            with task.sftp_lock:
                task.sftp = sftp
            try:
                if task.cancel_event.is_set():
                    raise _DownloadCancelled()
                self._ensure_remote_directory(sftp, remote_directory)
                files: list[tuple[Path, str]] = []
                for source in sources:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    if source.is_file():
                        files.append((source, posixpath.join(remote_directory, source.name)))
                        continue
                    target_root = posixpath.join(remote_directory, source.name)
                    for root, directories, names in os.walk(source):
                        if task.cancel_event.is_set():
                            raise _DownloadCancelled()
                        local_root = Path(root)
                        relative = local_root.relative_to(source)
                        remote_root = posixpath.join(target_root, *relative.parts)
                        self._ensure_remote_directory(sftp, remote_root)
                        for directory in directories:
                            self._ensure_remote_directory(sftp, posixpath.join(remote_root, directory))
                        files.extend((local_root / name, posixpath.join(remote_root, name)) for name in names)
                total = sum(local_file.stat().st_size for local_file, _remote_file in files)
                completed = 0
                last_percent = -1
                for local_file, remote_file in files:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    file_size = local_file.stat().st_size

                    def progress(current: int, _file_total: int) -> None:
                        nonlocal last_percent
                        if task.cancel_event.is_set():
                            raise _DownloadCancelled()
                        total_current = completed + current
                        percent = 100 if total <= 0 else int(total_current * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            self._events.transfer_progress.emit(local_file.name, total_current, total)

                    self._upload_file_safely(sftp, local_file, remote_file, progress, task.cancel_event)
                    completed += file_size
                    self._events.transfer_progress.emit(local_file.name, completed, total)
            finally:
                with task.sftp_lock:
                    task.sftp = None
                try:
                    sftp.close()
                except Exception:
                    pass
        except Exception as exc:
            cancelled = task.cancel_event.is_set() or isinstance(exc, _DownloadCancelled)
            self._events.upload_finished.emit(
                task.task_id, False, tr("操作已取消") if cancelled else str(exc), cancelled
            )
        else:
            task.succeeded = True
            self._events.upload_finished.emit(task.task_id, True, tr("上传完成"), False)
        finally:
            task.completed.set()

    @staticmethod
    def _upload_file_safely(
        sftp: object, local_file: Path, remote_file: str,
        progress: Callable[[int, int], None],
        cancel_event: threading.Event | None = None,
    ) -> None:
        temporary = posixpath.join(
            posixpath.dirname(remote_file),
            f".{posixpath.basename(remote_file)}.deployflow-upload-{uuid.uuid4().hex}.tmp",
        )
        try:
            sftp.put(str(local_file), temporary, callback=progress, confirm=True)
            expected_size = local_file.stat().st_size
            if int(sftp.stat(temporary).st_size) != expected_size:
                raise SSHSessionError(tr("上传校验失败：远程临时文件大小不一致"))
            if cancel_event is not None and cancel_event.is_set():
                raise _DownloadCancelled()
            rename = getattr(sftp, "posix_rename", None)
            if callable(rename):
                rename(temporary, remote_file)
            else:
                sftp.rename(temporary, remote_file)
            temporary = ""
        finally:
            if temporary:
                try:
                    sftp.remove(temporary)
                except Exception:
                    pass

    def _selected_remote_items(self) -> list[tuple[str, bool, str]]:
        return [
            (
                str(item.data(0, Qt.UserRole)),
                bool(item.data(0, Qt.UserRole + 1)),
                item.text(0),
            )
            for item in self.remote_tree.selectedItems()
        ]

    def _show_remote_context_menu(self, position: QPoint) -> None:
        item = self.remote_tree.itemAt(position)
        if item is None:
            self.remote_tree.clearSelection()
        elif not item.isSelected():
            self.remote_tree.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        entries = self._selected_remote_items()
        paths = [entry[0] for entry in entries]
        directory = self.current_directory()
        if len(entries) == 1:
            path, is_directory, _name = entries[0]
            selected_item = self.remote_tree.selectedItems()[0]
            mode = int(selected_item.data(0, Qt.UserRole + 2) or 0)
            _add_remote_type_actions(
                menu, path, is_directory, self._handle_path_action,
                executable=bool(mode & 0o111),
            )
            if is_directory:
                paste_into = menu.addAction(tr("粘贴到此文件夹"))
                paste_into.setEnabled(self._remote_clipboard() is not None)
                paste_into.triggered.connect(
                    lambda _checked=False: self._paste_remote_items(path)
                )
            menu.addSeparator()
        if entries:
            menu.addAction(tr("复制\tCtrl+C")).triggered.connect(
                lambda: self._copy_remote_paths(paths, False)
            )
            menu.addAction(tr("剪切\tCtrl+X")).triggered.connect(
                lambda: self._copy_remote_paths(paths, True)
            )
            menu.addAction(tr("下载选中项")).triggered.connect(self._download_selected_remote_items)
            rename_action = menu.addAction(tr("重命名"))
            rename_action.setEnabled(len(entries) == 1)
            rename_action.triggered.connect(self._rename_remote_item)
            menu.addAction(tr("删除\tDelete")).triggered.connect(
                lambda: self._delete_remote_paths(paths)
            )
            menu.addSeparator()
        paste_action = menu.addAction(tr("粘贴到当前目录\tCtrl+V"))
        paste_action.setEnabled(self._remote_clipboard() is not None)
        paste_action.triggered.connect(lambda: self._paste_remote_items(directory))
        menu.addAction(tr("新建文件…")).triggered.connect(lambda: self._create_remote_file(directory))
        menu.addAction(tr("新建文件夹…")).triggered.connect(lambda: self._create_remote_directory(directory))
        menu.addSeparator()
        menu.addAction(tr("上传文件…")).triggered.connect(lambda: self._select_upload())
        menu.addAction(tr("上传文件夹…")).triggered.connect(lambda: self._select_upload(folder=True))
        menu.addAction(tr("返回上级目录")).triggered.connect(self._go_remote_parent)
        menu.addAction(tr("刷新")).triggered.connect(self._force_refresh_remote_directory)
        if self._busy or not self._session.connected:
            for action in menu.actions():
                action.setEnabled(False)
        menu.exec(self.remote_tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _show_directory_context_menu(self, position: QPoint) -> None:
        item = self.directory_tree.itemAt(position)
        if item is not None:
            self.directory_tree.setCurrentItem(item)
        else:
            self.directory_tree.clearSelection()
        path = str(item.data(0, Qt.UserRole) or "") if item is not None else self._displayed_directory
        if not path:
            return
        menu = QMenu(self)
        if item is not None:
            for title, action in ((tr("跳转目录"), "open"), (tr("复制"), "copy"), (tr("剪切"), "cut"), (tr("删除…"), "delete")):
                entry = menu.addAction(title)
                entry.setEnabled(action == "open" or bool(path.strip("/")))
                entry.triggered.connect(
                    lambda _checked=False, value=action: self._directory_path_action(value, path)
                )
            menu.addSeparator()
        paste_action = menu.addAction(tr("粘贴到此目录"))
        paste_action.setEnabled(self._remote_clipboard() is not None)
        paste_action.triggered.connect(lambda: self._paste_remote_items(path))
        menu.addAction(tr("新建文件…")).triggered.connect(lambda: self._create_remote_file(path))
        menu.addAction(tr("新建文件夹…")).triggered.connect(lambda: self._create_remote_directory(path))
        if self._busy or not self._session.connected:
            for action in menu.actions():
                action.setEnabled(False)
        menu.exec(self.directory_tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _directory_path_action(self, action: str, path: str | None = None) -> None:
        if path is None:
            items = self.directory_tree.selectedItems()
            path = str(items[0].data(0, Qt.UserRole) or "") if items else ""
        if not path or self._busy or not self._session.connected:
            return
        if action == "open":
            self.open_directory(path)
        elif action in {"copy", "cut"}:
            self._copy_remote_paths([path], action == "cut")
        elif action == "paste":
            self._paste_remote_items(path)
        elif action == "delete":
            self._delete_remote_paths([path])

    def _handle_path_action(self, action: str, path: str) -> None:
        if self._busy or not self._session.connected:
            return
        session = self._session
        if action in {"script", "script_with_args", "tail_follow", "tail_lines", "search"} and any(
            ord(char) < 32 or ord(char) == 127 for char in path
        ):
            QMessageBox.warning(self, tr("无法执行"), tr("文件路径包含控制字符，不能发送到交互终端"))
            return
        quoted = shlex.quote(path)
        if action == "open":
            self.open_directory(path)
        elif action == "jump_directory":
            self.open_directory(posixpath.dirname(path) or "/")
        elif action == "edit":
            self._edit_remote_path(path)
        elif action == "chmod":
            self._edit_remote_permissions(path)
        elif action in {"script", "script_with_args"}:
            arguments: list[str] = []
            if action == "script_with_args":
                value, accepted = QInputDialog.getText(
                    self, tr("执行脚本"), tr("后续参数（例如：restart）："),
                )
                if not accepted:
                    return
                try:
                    arguments = shlex.split(value)
                except ValueError:
                    QMessageBox.warning(self, tr("参数格式错误"), tr("请检查引号是否成对"))
                    return
            if self._session is not session or not session.connected:
                return
            if path.lower().endswith((".sh", ".bash", ".zsh")):
                shell = "zsh" if path.lower().endswith(".zsh") else "bash"
                command = f"{shell} -- {quoted}"
            else:
                command = shlex.quote(f"./{posixpath.basename(path)}")
            if arguments:
                command += " " + " ".join(shlex.quote(argument) for argument in arguments)
            self.command_requested.emit(
                f"cd -- {shlex.quote(posixpath.dirname(path))} && {command}"
            )
        elif action == "tail_follow":
            self.command_requested.emit(f"tail -f -- {quoted}")
        elif action == "tail_lines":
            count, accepted = QInputDialog.getInt(
                self, tr("查看日志"), tr("显示末尾多少行："), 100, 1, 1000000,
            )
            if accepted and self._session is session and session.connected:
                self.command_requested.emit(f"tail -n {count} -- {quoted}")
        elif action == "search":
            keyword, accepted = QInputDialog.getText(self, tr("查询日志"), tr("关键字（按原文匹配）："))
            if accepted and keyword and self._session is session and session.connected:
                if any(ord(char) < 32 or ord(char) == 127 for char in keyword):
                    QMessageBox.warning(self, tr("关键字无效"), tr("关键字不能包含换行或控制字符"))
                    return
                self.command_requested.emit(f"grep -nF -- {shlex.quote(keyword)} {quoted}")

    def _remote_clipboard(self) -> _RemoteClipboard | None:
        value = SftpTransferDialog._clipboard
        mime = QApplication.clipboard().mimeData()
        if (
            value is not None and value.paths and not value.in_flight
            and value.server == self._server_key
            and mime is not None
            and bytes(mime.data(_REMOTE_FILE_MIME)) == value.token.encode("ascii")
        ):
            return value
        return None

    def _copy_remote_items(self, cut: bool) -> None:
        self._copy_remote_paths([entry[0] for entry in self._selected_remote_items()], cut)

    def _copy_remote_paths(self, paths: list[str], cut: bool) -> None:
        if self._busy or not paths or not self._session.connected:
            return
        paths = list(dict.fromkeys(posixpath.normpath(path) for path in paths))
        if any(not path.startswith("/") or not path.strip("/") for path in paths):
            QMessageBox.warning(self, tr("无法复制或剪切"), tr("请选择具体的文件或文件夹，不能选择服务器根目录"))
            return
        value = _RemoteClipboard(self._server_key, tuple(paths), cut)
        SftpTransferDialog._clipboard = value
        mime = QMimeData()
        mime.setData(_REMOTE_FILE_MIME, value.token.encode("ascii"))
        QApplication.clipboard().setMimeData(mime)
        self._update_cut_item_visuals()
        self.status_label.setText(tr("已{0} {1} 项，请进入目标目录后粘贴", tr('剪切') if cut else tr('复制'), len(paths)))

    def _paste_remote_items(self, directory: str | None = None) -> None:
        if self._busy or not self._session.connected:
            return
        value = self._remote_clipboard()
        if value is None:
            return
        directory = directory or self.current_directory()
        value.cancel_event.clear()
        value.in_flight = True
        threading.Thread(
            target=self._paste_preflight_worker,
            args=(value, directory),
            name="sftp-paste-preflight",
            daemon=True,
        ).start()

    def _paste_preflight_worker(self, clipboard: _RemoteClipboard, directory: str) -> None:
        plans: list[_RemotePastePlan] = []
        error = ""
        try:
            sftp = self._session.open_isolated_sftp()
            try:
                destination = sftp.normalize(directory)
                if not stat.S_ISDIR(sftp.stat(destination).st_mode):
                    raise SSHSessionError(tr("粘贴目标必须是文件夹"))
                target_names: set[str] = set()
                existing_names = {
                    str(attribute.filename) for attribute in sftp.listdir_attr(destination)
                }
                for source in clipboard.paths:
                    source = posixpath.normpath(source)
                    attributes = sftp.lstat(source)
                    canonical = posixpath.join(
                        sftp.normalize(posixpath.dirname(source)), posixpath.basename(source)
                    )
                    target = posixpath.join(destination, posixpath.basename(source))
                    if target == canonical or (
                        stat.S_ISDIR(attributes.st_mode)
                        and (destination == canonical or destination.startswith(canonical.rstrip("/") + "/"))
                    ):
                        raise SSHSessionError(tr("不能粘贴到原位置，也不能粘贴到自身的子目录"))
                    if target in target_names:
                        raise SSHSessionError(tr("所选项目中存在同名文件，请分开粘贴"))
                    target_names.add(target)
                    try:
                        sftp.lstat(target)
                    except OSError as exc:
                        if exc.errno != errno.ENOENT:
                            raise
                    else:
                        alternate = self._next_copy_target(destination, posixpath.basename(source), existing_names)
                        existing_names.add(posixpath.basename(alternate))
                        plans.append(_RemotePastePlan(source, target, True, alternate))
                        continue
                    plans.append(_RemotePastePlan(source, target))
            finally:
                sftp.close()
        except Exception as exc:
            error = str(exc)
        self._events.paste_preflighted.emit(clipboard, plans, error)

    def _handle_paste_preflight(
        self, clipboard: _RemoteClipboard, plans: object, error: str,
    ) -> None:
        if (
            SftpTransferDialog._clipboard is not clipboard
            or clipboard.server != self._server_key
        ):
            return
        if error:
            clipboard.in_flight = False
            self.status_label.setText(tr("粘贴失败：{0}", error))
            return
        resolved = list(plans)
        conflicts = [plan for plan in resolved if plan.replace]
        apply_choice: str | None = None
        for conflict in conflicts:
            choice = apply_choice
            if choice is None:
                dialog = QMessageBox(self)
                dialog.setIcon(QMessageBox.Warning)
                dialog.setWindowTitle(tr("文件已存在"))
                dialog.setText(tr("目标位置已经存在：{0}", posixpath.basename(conflict.target)))
                replace_button = dialog.addButton(tr("替换"), QMessageBox.AcceptRole)
                keep_button = dialog.addButton(tr("保留两个"), QMessageBox.ActionRole)
                skip_button = dialog.addButton(tr("跳过"), QMessageBox.RejectRole)
                cancel_button = dialog.addButton(tr("取消"), QMessageBox.DestructiveRole)
                apply_all = QCheckBox(tr("对后续冲突执行此操作"))
                dialog.setCheckBox(apply_all)
                dialog.exec()
                clicked = dialog.clickedButton()
                choice = (
                    "replace" if clicked is replace_button else
                    "keep" if clicked is keep_button else
                    "skip" if clicked is skip_button else "cancel"
                )
                if apply_all.isChecked() and choice != "cancel":
                    apply_choice = choice
            if choice == "cancel":
                clipboard.in_flight = False
                return
            index = resolved.index(conflict)
            if choice == "skip":
                resolved.pop(index)
            elif choice == "keep":
                resolved[index] = _RemotePastePlan(
                    conflict.source,
                    conflict.alternate_target or conflict.target,
                )
        if not resolved:
            clipboard.in_flight = False
            return
        directory = posixpath.dirname(resolved[0].target)
        started = self._start_simple_operation(
            tr("移动") if clipboard.cut else tr("复制"), self._paste_worker,
            self._session, clipboard, tuple(resolved),
            affected_directories={directory, *(posixpath.dirname(path) for path in clipboard.paths)},
        )
        if started:
            self.progress.setRange(0, 0)
            dialog = QProgressDialog(
                tr("正在{0} 0/{1} 项…", tr('移动') if clipboard.cut else tr('复制'), len(resolved)),
                tr("取消"), 0, len(resolved), self,
            )
            dialog.setWindowTitle(tr("文件操作进度"))
            dialog.setWindowModality(Qt.NonModal)
            dialog.setMinimumDuration(0)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.canceled.connect(clipboard.cancel_event.set)
            self._paste_progress_dialog = dialog
            dialog.show()
        else:
            clipboard.in_flight = False

    def _update_clipboard_progress(
        self, clipboard: _RemoteClipboard, completed: int, total: int, name: str,
    ) -> None:
        dialog = self._paste_progress_dialog
        if dialog is None or SftpTransferDialog._clipboard is not clipboard:
            return
        dialog.setValue(completed)
        dialog.setLabelText(
            tr("正在{0} {1}/{2} 项：{3}", tr('移动') if clipboard.cut else tr('复制'), completed, total, name)
        )

    @staticmethod
    def _next_copy_target(directory: str, name: str, occupied_names: set[str]) -> str:
        stem, extension = posixpath.splitext(name)
        index = 1
        while True:
            suffix = " - 副本" if index == 1 else f" - 副本 ({index})"
            candidate = posixpath.join(directory, f"{stem}{suffix}{extension}")
            if posixpath.basename(candidate) not in occupied_names:
                return candidate
            index += 1

    def _paste_worker(
        self, operation: str, session: InteractiveSSHSession,
        clipboard: _RemoteClipboard, plans: tuple[_RemotePastePlan, ...],
    ) -> None:
        completed: list[str] = []

        def paste_all(sftp: object) -> None:
            for index, plan in enumerate(plans):
                if clipboard.cancel_event.is_set():
                    raise SSHSessionError(tr("操作已取消"))
                source, target = plan.source, plan.target
                self._events.clipboard_progress.emit(
                    clipboard, index, len(plans), posixpath.basename(source),
                )
                if clipboard.cut:
                    if plan.replace:
                        self._remove_remote_path(sftp, target)
                    # SFTP rename preserves the source if moving fails.
                    try:
                        sftp.rename(source, target)
                    except OSError as exc:
                        raise SSHSessionError(
                            tr("无法移动 {0}，源文件未删除。请检查权限、同名文件，以及目标是否跨文件系统：{1}", source, exc)
                        ) from exc
                else:
                    staging = posixpath.join(
                        posixpath.dirname(target), f".deployflow-copy-{uuid.uuid4().hex}"
                    )
                    sftp.mkdir(staging, 0o700)
                    try:
                        payload = posixpath.join(staging, "content")
                        session.copy_remote_path(source, payload, clipboard.cancel_event)
                        if clipboard.cancel_event.is_set():
                            raise SSHSessionError(tr("操作已取消"))
                        if plan.replace:
                            self._remove_remote_path(sftp, target)
                        sftp.rename(payload, target)
                    finally:
                        try:
                            self._remove_remote_path(sftp, staging)
                        except Exception as exc:
                            if clipboard.cancel_event.is_set():
                                raise SSHSessionError(
                                    tr("操作已取消，但临时文件清理失败：{0}（{1}）", staging, exc)
                                ) from exc
                            raise
                completed.append(source)
                self._events.clipboard_progress.emit(
                    clipboard, index + 1, len(plans), posixpath.basename(source),
                )

        def finish_clipboard() -> None:
            self._events.clipboard_finished.emit(clipboard, completed)

        self._run_simple_operation(operation, paste_all, session, finish_clipboard)

    def _finish_clipboard(self, clipboard: _RemoteClipboard, completed: object) -> None:
        dialog = self._paste_progress_dialog
        self._paste_progress_dialog = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        clipboard.in_flight = False
        if not clipboard.cut:
            return
        clipboard.paths = tuple(path for path in clipboard.paths if path not in completed)
        if SftpTransferDialog._clipboard is clipboard and not clipboard.paths:
            SftpTransferDialog._clipboard = None
        self._update_cut_item_visuals()

    def _update_cut_item_visuals(self) -> None:
        clipboard = self._remote_clipboard()
        cut_paths = set(clipboard.paths) if clipboard is not None and clipboard.cut else set()
        if cut_paths == self._visible_cut_paths:
            return
        for index in range(self.remote_tree.topLevelItemCount()):
            item = self.remote_tree.topLevelItem(index)
            path = str(item.data(0, Qt.UserRole) or "")
            if path in cut_paths or path in self._visible_cut_paths:
                item.setForeground(0, QColor("#94a3b8") if path in cut_paths else QColor("#111827"))
        self._visible_cut_paths = cut_paths

    def _download_selected_remote_items(self) -> None:
        entries = self._selected_remote_items()
        if not entries or self._busy:
            if not entries:
                QMessageBox.warning(self, tr("未选择文件"), tr("请先选择要下载的服务器文件或文件夹"))
            return
        self._download_remote_items(entries)

    def _start_remote_download_drag(self) -> None:
        entries = self._selected_remote_items()
        if not entries or self._busy or not self._session.connected:
            return
        try:
            for _path, _directory, name in entries:
                self._safe_download_name(name)
        except SSHSessionError as exc:
            QMessageBox.warning(self, tr("无法下载"), str(exc))
            return
        session = self._session
        mime = _RemoteDownloadMimeData(entries, self._download_remote_items)
        drag = QDrag(self.remote_tree)
        drag.setMimeData(mime)
        icon = self.style().standardIcon(QStyle.SP_DirIcon if entries[0][1] else QStyle.SP_FileIcon)
        pixmap = icon.pixmap(32, 32)
        drag.setPixmap(pixmap)
        if os.name == "nt":
            # Explorer cannot accept our private MIME; resolve its actual folder
            # after release instead of advertising cached file URLs.
            drag.setDragCursor(pixmap, Qt.IgnoreAction)
        tracker = WindowsDropTracker(self)
        _RemoteDownloadMimeData._active = mime
        tracker.begin()
        try:
            drag.exec(Qt.CopyAction)
        finally:
            destination = tracker.finish()
            accepted = mime.accepted
            _RemoteDownloadMimeData._active = None
            tracker.deleteLater()
            drag.deleteLater()
        if accepted or destination is None:
            return
        threading.Thread(
            target=self._resolve_external_download_drop,
            args=(session, entries, destination),
            name="sftp-drop-destination", daemon=True,
        ).start()

    def _resolve_external_download_drop(
        self, session: InteractiveSSHSession, entries: list[tuple[str, bool, str]],
        destination: tuple[int, int, int, bool],
    ) -> None:
        try:
            directory = resolve_windows_drop_directory(destination)
            error = ""
        except Exception as exc:
            directory = None
            error = str(exc)
        try:
            self._events.external_drop_resolved.emit(session, entries, directory, error)
        except RuntimeError:
            pass

    def _finish_external_download_drop(
        self, session: InteractiveSSHSession, entries: list[tuple[str, bool, str]],
        directory: Path | None, error: str,
    ) -> None:
        if session is not self._session or not session.connected:
            return
        if error or directory is None:
            QMessageBox.warning(
                self, tr("无法下载"),
                tr("无法识别拖入的文件夹，请拖到资源管理器中已打开的目录或文件夹图标上。"),
            )
            return
        self._download_remote_items(entries, directory)

    def _download_remote_items(
        self, entries: list[tuple[str, bool, str]], destination: Path | None = None,
    ) -> _DownloadTask | None:
        if not entries or self._busy or not self._session.connected:
            return
        if destination is None and not self._embedded and self._local_browser_directory:
            destination = Path(self._local_browser_directory)
        if destination is None:
            destination = QFileDialog.getExistingDirectory(
                self,
                tr("选择下载保存目录"),
                self._last_local_directory,
            )
            if not destination:
                return
        target_directory = Path(destination)
        try:
            for _path, _directory, name in entries:
                self._safe_download_name(name)
            target_directory.mkdir(parents=True, exist_ok=True)
        except (OSError, SSHSessionError) as exc:
            QMessageBox.warning(self, tr("无法下载"), str(exc))
            return
        self._last_local_directory = str(target_directory)
        conflicts = [
            name for _remote_path, _is_directory, name in entries
            if (target_directory / name).exists()
        ]
        if conflicts and QMessageBox.question(
            self,
            tr("确认覆盖"),
            tr("本地存在同名内容，下载后将覆盖其中的同名文件。是否继续？"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        task_id = str(time.time_ns())
        title = entries[0][2] if len(entries) == 1 else tr("{0} 等 {1} 项", entries[0][2], len(entries))
        task = _DownloadTask(task_id=task_id, title=title)
        with self._download_tasks_lock:
            self._download_tasks[task_id] = task
        if self._download_task_panel is not None:
            self._download_task_panel.add_task(task_id, title, destination=target_directory)
        self.status_label.setText(tr("已创建下载任务：{0}", title))
        threading.Thread(
            target=self._download_worker,
            args=(task, self._session, entries, target_directory),
            name=f"sftp-download-{task_id}",
            daemon=True,
        ).start()
        return task

    def _download_worker(
        self,
        task: _DownloadTask,
        session: InteractiveSSHSession,
        entries: list[tuple[str, bool, str]],
        destination: Path,
    ) -> None:
        partial_files: set[Path] = set()
        slot_acquired = False
        try:
            while not task.cancel_event.is_set():
                if self._download_slots.acquire(timeout=0.1):
                    slot_acquired = True
                    break
            if task.cancel_event.is_set():
                raise _DownloadCancelled()
            sftp = session.open_isolated_sftp(allow_shared_fallback=False)
            with task.sftp_lock:
                task.sftp = sftp
            try:
                if task.cancel_event.is_set():
                    raise _DownloadCancelled()
                files: list[tuple[str, Path, int]] = []
                for remote_path, is_directory, name in entries:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    local_path = destination / self._safe_download_name(name)
                    root_attributes = sftp.lstat(remote_path)
                    if stat.S_ISLNK(root_attributes.st_mode or 0):
                        raise SSHSessionError(tr("不支持下载软链接：{0}", remote_path))
                    is_directory = stat.S_ISDIR(root_attributes.st_mode or 0)
                    if not is_directory and not stat.S_ISREG(root_attributes.st_mode or 0):
                        raise SSHSessionError(tr("不支持下载特殊远程文件：{0}", remote_path))
                    if is_directory:
                        self._collect_remote_files(
                            sftp,
                            remote_path,
                            local_path,
                            files,
                            task.cancel_event,
                        )
                    else:
                        size = int(root_attributes.st_size)
                        files.append((remote_path, local_path, size))
                total = sum(size for _remote, _local, size in files)
                completed = 0
                last_percent = -1
                for remote_file, local_file, file_size in files:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    if local_file.exists():
                        raise SSHSessionError(tr("本地目标已存在，已拒绝覆盖：{0}", local_file))
                    local_file.parent.mkdir(parents=True, exist_ok=True)
                    partial_file = local_file.with_name(
                        f".{local_file.name}.{task.task_id}.part"
                    )
                    partial_files.add(partial_file)

                    def progress(current: int, _file_total: int) -> None:
                        nonlocal last_percent
                        if task.cancel_event.is_set():
                            raise _DownloadCancelled()
                        total_current = completed + current
                        percent = 100 if total <= 0 else int(total_current * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            self._events.download_progress.emit(
                                task.task_id,
                                local_file.name,
                                total_current,
                                total,
                            )

                    sftp.get(remote_file, str(partial_file), callback=progress)
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    os.replace(partial_file, local_file)
                    partial_files.discard(partial_file)
                    completed += file_size
                    self._events.download_progress.emit(
                        task.task_id, local_file.name, completed, total
                    )
                if task.cancel_event.is_set():
                    raise _DownloadCancelled()
            finally:
                with task.sftp_lock:
                    task.sftp = None
                try:
                    sftp.close()
                except Exception:
                    pass
        except _DownloadCancelled:
            self._events.download_finished.emit(
                task.task_id, False, tr("下载已取消"), True
            )
        except Exception as exc:
            if task.cancel_event.is_set():
                self._events.download_finished.emit(
                    task.task_id, False, tr("下载已取消"), True
                )
            else:
                self._events.download_finished.emit(
                    task.task_id, False, tr("下载失败：{0}", exc), False
                )
        else:
            task.succeeded = True
            self._events.download_finished.emit(
                task.task_id, True, tr("下载完成"), False
            )
        finally:
            if slot_acquired:
                self._download_slots.release()
            for partial_file in partial_files:
                try:
                    partial_file.unlink(missing_ok=True)
                except OSError:
                    pass
            task.completed.set()

    @classmethod
    def _safe_download_name(cls, name: str) -> str:
        invalid = '<>:"/\\|?*'
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
        if (
            not name or name in {".", ".."} or Path(name).name != name
            or any(character in invalid or ord(character) < 32 for character in name)
            or name.rstrip(". ").upper().split(".", 1)[0] in reserved
        ):
            raise SSHSessionError(tr("远程文件名不安全，已拒绝下载：{0!r}", name))
        return name

    @classmethod
    def _collect_remote_files(
        cls,
        sftp: object,
        remote_directory: str,
        local_directory: Path,
        files: list[tuple[str, Path, int]],
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            raise _DownloadCancelled()
        local_directory.mkdir(parents=True, exist_ok=True)
        for attribute in sftp.listdir_attr(remote_directory):
            if cancel_event.is_set():
                raise _DownloadCancelled()
            name = cls._safe_download_name(str(attribute.filename))
            remote_path = posixpath.join(remote_directory, name)
            local_path = local_directory / name
            if stat.S_ISDIR(attribute.st_mode or 0):
                cls._collect_remote_files(
                    sftp,
                    remote_path,
                    local_path,
                    files,
                    cancel_event,
                )
            elif stat.S_ISREG(attribute.st_mode or 0):
                files.append((remote_path, local_path, int(attribute.st_size or 0)))
            else:
                raise SSHSessionError(tr("不支持下载特殊远程文件：{0}", remote_path))

    def _update_download_task(
        self,
        task_id: str,
        name: str,
        current: int,
        total: int,
    ) -> None:
        if self._download_task_panel is not None:
            self._download_task_panel.update_task(task_id, name, current, total)

    def _finish_download_task(
        self,
        task_id: str,
        succeeded: bool,
        message: str,
        cancelled: bool,
    ) -> None:
        with self._download_tasks_lock:
            self._download_tasks.pop(task_id, None)
        if self._download_task_panel is not None:
            if cancelled:
                self._download_task_panel.remove_task(task_id)
            else:
                self._download_task_panel.finish_task(task_id, succeeded, message)
        self.status_label.setText(message)

    def cancel_download_task(self, task_id: str) -> None:
        task = self._upload_task
        if task is not None and task.task_id == task_id:
            task.cancel_event.set()
            with task.sftp_lock:
                sftp = task.sftp
            if sftp is not None:
                threading.Thread(
                    target=self._close_download_channel, args=(sftp,),
                    name=f"sftp-upload-cancel-{task_id}", daemon=True,
                ).start()
            if self._download_task_panel is not None:
                self._download_task_panel.remove_task(task_id)
            return
        with self._download_tasks_lock:
            task = self._download_tasks.pop(task_id, None)
        if task is None:
            return
        task.cancel_event.set()
        with task.sftp_lock:
            sftp = task.sftp
        if sftp is not None:
            threading.Thread(
                target=self._close_download_channel,
                args=(sftp,),
                name=f"sftp-download-cancel-{task_id}",
                daemon=True,
            ).start()
        if self._download_task_panel is not None:
            self._download_task_panel.remove_task(task_id)

    def cancel_all_downloads(self) -> None:
        with self._download_tasks_lock:
            task_ids = list(self._download_tasks)
        if self._upload_task is not None:
            task_ids.append(self._upload_task.task_id)
        for task_id in task_ids:
            self.cancel_download_task(task_id)

    @staticmethod
    def _close_download_channel(sftp: object) -> None:
        try:
            sftp.close()
        except Exception:
            pass

    def _create_remote_file(self, directory: str | None = None) -> None:
        if self._busy or not self._session.connected:
            return
        session = self._session
        directory = directory or self.current_directory()
        name, accepted = QInputDialog.getText(self, tr("新建文件"), tr("文件名称（例如 application.conf）："))
        if not accepted or self._session is not session or not session.connected:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, tr("名称无效"), tr("请输入有效的文件名称，不能包含路径分隔符或控制字符"))
            return
        remote_path = posixpath.join(directory, name)
        self._start_simple_operation(
            tr("新建文件"), self._create_file_worker, remote_path,
            affected_directories={directory},
        )

    def _create_remote_directory(self, directory: str | None = None) -> None:
        if self._busy or not self._session.connected:
            return
        session = self._session
        directory = directory or self.current_directory()
        name, accepted = QInputDialog.getText(self, tr("新建文件夹"), tr("文件夹名称："))
        if not accepted or self._session is not session or not session.connected:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, tr("名称无效"), tr("名称不能为空，也不能包含 /、\\ 或使用 .、.."))
            return
        remote_path = posixpath.join(directory, name)
        self._start_simple_operation(
            tr("新建文件夹"), self._mkdir_worker, remote_path,
            affected_directories={posixpath.dirname(remote_path)},
        )

    def _rename_remote_item(self) -> None:
        entries = self._selected_remote_items()
        if self._busy:
            return
        if len(entries) != 1:
            QMessageBox.warning(self, tr("无法重命名"), tr("请只选择一个文件或文件夹"))
            return
        self._rename_remote_path(entries[0][0])

    def _rename_remote_path(self, source: str) -> None:
        if self._busy or not self._session.connected:
            return
        source = posixpath.normpath(source)
        if not source.startswith("/") or not source.strip("/"):
            return
        old_name = posixpath.basename(source)
        name, accepted = QInputDialog.getText(
            self, tr("重命名"), tr("新名称："), text=old_name
        )
        if not accepted:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, tr("名称无效"), tr("名称不能为空，也不能包含 /、\\ 或使用 .、.."))
            return
        if name == old_name:
            return
        target = posixpath.join(posixpath.dirname(source), name)
        self._start_simple_operation(
            tr("重命名"), self._rename_worker, source, target,
            affected_directories={posixpath.dirname(source)},
        )

    def _delete_remote_items(self) -> None:
        entries = self._selected_remote_items()
        self._delete_remote_paths([path for path, _is_directory, _name in entries])

    def _delete_remote_paths(self, paths: list[str], verified_directory: bool | None = None) -> None:
        if not paths or self._busy or not self._session.connected:
            return
        session = self._session
        paths = list(dict.fromkeys(posixpath.normpath(path) for path in paths))
        if any(not path.startswith("/") or not path.strip("/") for path in paths):
            QMessageBox.warning(self, tr("无法删除"), tr("不能删除服务器根目录，请选择具体的文件或文件夹"))
            return
        names = "\n".join(f"• {path}" for path in paths[:8])
        if len(paths) > 8:
            names += tr("\n……共 {0} 项", len(paths))
        message = (
            tr("确认删除目录：\n{0}\n\n此操作会删除目录内所有文件。", names)
            if verified_directory is True
            else tr("确认删除文件：\n{0}", names)
            if verified_directory is False
            else tr("将永久删除服务器上的以下内容，文件夹会连同内部内容一起删除：\n\n{0}", names)
        )
        if QMessageBox.warning(
            self,
            tr("确认删除"),
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if self._session is not session or not session.connected:
            return
        self._start_simple_operation(
            tr("删除"), self._delete_worker, paths, verified_directory,
            affected_directories={posixpath.dirname(path) for path in paths},
        )

    def _start_simple_operation(
        self,
        operation: str,
        worker: Callable[..., None],
        *args: object,
        affected_directories: set[str] | None = None,
    ) -> bool:
        if self._busy or not self._session.connected:
            return False
        self._operation_refresh_directory = self.current_directory()
        self._operation_affected_directories = {
            posixpath.normpath(path) for path in (affected_directories or set())
        }
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText(tr("正在{0}…", operation))
        threading.Thread(
            target=worker,
            args=(operation, *args),
            name=f"sftp-{operation}",
            daemon=True,
        ).start()
        return True

    def _mkdir_worker(self, operation: str, remote_path: str) -> None:
        self._run_simple_operation(operation, lambda sftp: sftp.mkdir(remote_path))

    def _create_file_worker(self, operation: str, remote_path: str) -> None:
        def create_file(sftp: object) -> None:
            with sftp.open(remote_path, "wx"):
                pass

        self._run_simple_operation(operation, create_file)

    def _edit_remote_permissions(self, path: str) -> None:
        session = self._session
        if self._busy or not session.connected:
            return
        threading.Thread(
            target=self._load_permissions_worker, args=(session, path),
            name="sftp-read-permissions", daemon=True,
        ).start()

    def _load_permissions_worker(self, session: InteractiveSSHSession, path: str) -> None:
        mode, error = 0, ""
        try:
            sftp = session.open_isolated_sftp()
            try:
                sftp.get_channel().settimeout(8)
                mode = sftp.stat(path).st_mode
                if not stat.S_ISREG(mode):
                    raise SSHSessionError(tr("请选择普通文件"))
            finally:
                sftp.close()
        except Exception as exc:
            error = str(exc) or tr("读取文件权限失败")
        self._events.permissions_loaded.emit(session, path, mode, error)

    def _show_permissions_dialog(
        self, session: InteractiveSSHSession, path: str, mode: int, error: str,
    ) -> None:
        if session is not self._session or not session.connected or self._busy:
            return
        if error:
            QMessageBox.warning(self, tr("读取权限失败"), error)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("修改文件权限"))
        dialog.setMinimumWidth(280)
        layout = QVBoxLayout(dialog)
        name = QLabel(posixpath.basename(path))
        name.setTextFormat(Qt.PlainText)
        name.setWordWrap(True)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        name.setToolTip(path)
        layout.addWidget(name)
        checkboxes: list[tuple[QCheckBox, int]] = []
        for title, shift in ((tr("所有者"), 6), (tr("组"), 3), (tr("其他"), 0)):
            group = QGroupBox(title)
            row = QHBoxLayout(group)
            for label, value in ((tr("读取"), 4), (tr("写入"), 2), (tr("执行"), 1)):
                bit = value << shift
                checkbox = QCheckBox(label)
                checkbox.setChecked(bool(mode & bit))
                checkboxes.append((checkbox, bit))
                row.addWidget(checkbox)
            layout.addWidget(group)
        buttons = QHBoxLayout()
        accept_button = QPushButton(tr("确定"))
        accept_button.setDefault(True)
        accept_button.clicked.connect(dialog.accept)
        cancel_button = QPushButton(tr("取消"))
        cancel_button.clicked.connect(dialog.reject)
        buttons.addWidget(accept_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)
        accepted = dialog.exec() == QDialog.Accepted
        permissions = sum(bit for checkbox, bit in checkboxes if checkbox.isChecked())
        dialog.deleteLater()
        if not accepted or session is not self._session or not session.connected:
            return
        if permissions == (mode & 0o777):
            return
        self._start_simple_operation(
            tr("修改文件权限"), self._chmod_worker, path, permissions,
            affected_directories={posixpath.dirname(path)},
        )

    def _chmod_worker(self, operation: str, remote_path: str, permissions: int) -> None:
        def set_permissions(sftp: object) -> None:
            attributes = sftp.stat(remote_path)
            if not stat.S_ISREG(attributes.st_mode):
                raise SSHSessionError(tr("只能修改普通文件的权限"))
            sftp.chmod(remote_path, (stat.S_IMODE(attributes.st_mode) & 0o7000) | (permissions & 0o777))

        self._run_simple_operation(operation, set_permissions)

    def _rename_worker(self, operation: str, source: str, target: str) -> None:
        self._run_simple_operation(operation, lambda sftp: sftp.rename(source, target))

    def _delete_worker(
        self, operation: str, paths: list[str], verified_directory: bool | None = None,
    ) -> None:
        def remove_all(sftp: object) -> None:
            for path in paths:
                if verified_directory is False:
                    # A confirmed file must never turn into a recursive directory deletion.
                    sftp.remove(path)
                else:
                    self._remove_remote_path(sftp, path)

        self._run_simple_operation(operation, remove_all)

    def _run_simple_operation(
        self,
        operation: str,
        action: Callable[[object], None],
        session: InteractiveSSHSession | None = None,
        finished: Callable[[], None] | None = None,
    ) -> None:
        try:
            sftp = (session or self._session).open_isolated_sftp()
            try:
                sftp.get_channel().settimeout(30)
                action(sftp)
            finally:
                sftp.close()
        except Exception as exc:
            succeeded = False
            message = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], TranslatedText) else str(exc)
        else:
            succeeded, message = True, tr("{0}完成", operation)
        if finished is not None:
            finished()
        self._events.operation_finished.emit(operation, succeeded, message)

    @classmethod
    def _remove_remote_path(cls, sftp: object, path: str) -> None:
        attributes = sftp.lstat(path)
        if stat.S_ISDIR(attributes.st_mode):
            for child in sftp.listdir_attr(path):
                cls._remove_remote_path(sftp, posixpath.join(path, child.filename))
            sftp.rmdir(path)
        else:
            sftp.remove(path)

    @staticmethod
    def _valid_remote_name(name: str) -> bool:
        return bool(
            name and name not in {".", ".."} and "/" not in name and "\\" not in name
            and not any(ord(char) < 32 or ord(char) == 127 for char in name)
        )

    @staticmethod
    def _ensure_remote_directory(sftp: object, directory: str) -> None:
        normalized = posixpath.normpath(directory.replace("\\", "/"))
        if not normalized or normalized == ".":
            return
        current = "" if normalized.startswith("/") else ""
        for part in (value for value in normalized.split("/") if value and value != "."):
            current = f"{current}/{part}" if current else (f"/{part}" if normalized.startswith("/") else part)
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)

    def _update_transfer_progress(self, name: str, current: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(current * 100 / total))
        if self._upload_task is not None and self._download_task_panel is not None:
            self._download_task_panel.update_task(self._upload_task.task_id, name, current, total)
        else:
            self.progress.setValue(percent)
        self.status_label.setText(tr("正在传输：{0}（{1}%）", name, percent))

    def _finish_upload_task(
        self, task_id: str, succeeded: bool, message: str, cancelled: bool,
    ) -> None:
        if self._upload_task is None or self._upload_task.task_id != task_id:
            return
        self._upload_task = None
        if self._download_task_panel is not None:
            if cancelled:
                self._download_task_panel.remove_task(task_id)
            else:
                self._download_task_panel.finish_task(task_id, succeeded, message)
        self._finish_operation(tr("上传"), succeeded, message)

    def _finish_operation(self, operation: str, succeeded: bool, message: str) -> None:
        self._set_busy(False)
        self.progress.setRange(0, 100)
        refresh_directory = self._operation_refresh_directory
        self._operation_refresh_directory = None
        affected = self._operation_affected_directories
        self._operation_affected_directories = set()
        if refresh_directory:
            affected.add(refresh_directory)
        # Partial moves/copies/deletes also change directory contents.
        for directory in affected:
            self._invalidate_directory(directory)
            self.directory_changed.emit(directory)
        current_directory = self.current_directory()
        if current_directory in affected and self._session.connected:
            self._refresh_remote_directory(force=True)
        if self._session.connected:
            for directory in affected:
                item = self._find_directory_tree_item(directory)
                if directory != self._remote_navigation_target and item is not None and item.isExpanded():
                    self._request_tree_directory(directory, force=True)
        if succeeded:
            self.progress.setValue(100)
            if render_text(operation) == tr("下载"):
                self.status_label.setText(tr("下载完成"))
            else:
                self.status_label.setText(tr("{0}完成，已刷新服务器目录", operation))
        else:
            if render_text(message) in {"操作已取消", tr("操作已取消")}:
                self.status_label.setText(tr("{0}已取消", operation))
            else:
                self.status_label.setText(tr("{0}失败", operation))
                QMessageBox.critical(self, tr("{0}失败", operation), render_text(message))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not self._embedded:
            self.local_upload_button.setEnabled(not busy)
        if not busy:
            QTimer.singleShot(0, self._open_pending_directory)

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        if self.file_tabs.currentIndex() == 1:
            self._refresh_log_view()
            self._log_refresh_timer.start()

    def hideEvent(self, event: QEvent) -> None:
        self._log_refresh_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: QEvent) -> None:
        if not self.close_editors():
            event.ignore()
            return
        self._log_refresh_timer.stop()
        self._browse_request_id += 1
        self._active_browse_request = self._browse_request_id
        self._display_batch_id += 1
        threading.Thread(
            target=self.release_browse_channel,
            name="sftp-browser-close",
            daemon=True,
        ).start()
        super().closeEvent(event)

    def reject(self) -> None:
        # Escape must use the same unsaved-editor checks as the close button.
        if self.close_editors():
            super().reject()

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return "0 B"


class QtSSHTerminalTab(QWidget):
    """A Qt terminal tab backed by the existing InteractiveSSHSession."""

    def __init__(
        self,
        parent: QWidget | None,
        parameters: ServerParameters,
        parameter_path: Path,
        default_open_path: str | None,
        default_open_command: str | None,
        state_changed: StateCallback,
        close_requested: CloseCallback,
        log_event: LogCallback,
        log_reader: Callable[[], str],
        tool_mode_requested: ToolModeCallback,
        monitor_panel_width: int,
        monitor_width_changed: MonitorWidthCallback,
        ip_hiding: bool = False,
    ) -> None:
        super().__init__(parent)
        self.parameters = parameters
        self._ip_hiding = ip_hiding
        self.parameter_path = parameter_path.resolve()
        self.default_open_path = default_open_path
        self.default_open_command = default_open_command
        self._state_changed = state_changed
        self._close_requested = close_requested
        self._log_event = log_event
        self._log_reader = log_reader
        self._tool_mode_requested = tool_mode_requested
        self._monitor_panel_width = max(220, int(monitor_panel_width))
        self._monitor_width_changed = monitor_width_changed
        self._state = "disconnected"
        self._attempt = 0
        self._session: InteractiveSSHSession | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
        self._deferred_output_event: tuple[int, InteractiveSSHSession, str] | None = None
        self._event_notification_lock = threading.Lock()
        self._event_drain_pending = False
        self._thread_events = _ThreadEvents(self)
        self._thread_events.available.connect(self._drain_events, Qt.QueuedConnection)
        self._terminal_columns = 160
        self._terminal_rows = 48
        self._history: list[str] = []
        self._history_index = 0
        self._host_key_requests: set[_HostKeyRequest] = set()
        self._host_key_requests_lock = threading.Lock()
        self._last_terminal_input_at = 0.0
        self._last_terminal_output_at = 0.0
        self._terminal_log_chunks: list[str] = []
        self._closed = False
        self._uploading = False
        self._file_manager: SftpTransferDialog | None = None
        self._transfer_dialog: SftpTransferDialog | None = None
        self._file_panel_open = False
        self._terminal_directory: str | None = None
        self._terminal_directory_jump: tuple[int, str] | None = None
        self._terminal_jump_echo: str | None = None
        self._terminal_jump_echo_buffer = ""
        self._directory_input_revision = 0
        self._directory_verified_revision = -1
        self._directory_running = False
        self._terminal_paste_running = False
        self._terminal_menu: QMenu | None = None
        self._file_context_requests: queue.Queue[tuple[InteractiveSSHSession, int, RemotePathContext, int, int] | None] = queue.Queue(maxsize=1)
        self._file_context_thread: threading.Thread | None = None
        self._path_resolver = RemotePathResolver()
        self._terminal_path_action_running = False
        self._terminal_input_line = ""
        self._terminal_input_uncertain = False
        self._directory_last_checked = 0.0
        self._process_operations: set[int] = set()
        self._monitor_running = False
        self._pending_monitor_status: tuple[int, InteractiveSSHSession, str] | None = None
        self._previous_cpu_total: int | None = None
        self._previous_cpu_idle: int | None = None
        self._terminal_log_timer = QTimer(self)
        self._terminal_log_timer.setSingleShot(True)
        self._terminal_log_timer.setInterval(_TERMINAL_LOG_FLUSH_INTERVAL_MS)
        self._terminal_log_timer.timeout.connect(self._flush_terminal_log)
        self._terminal_jump_echo_timer = QTimer(self)
        self._terminal_jump_echo_timer.setSingleShot(True)
        self._terminal_jump_echo_timer.setInterval(2000)
        self._terminal_jump_echo_timer.timeout.connect(self._flush_terminal_jump_echo)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_terminal_resize)
        self._directory_timer = QTimer(self)
        self._directory_timer.setInterval(750)
        self._directory_timer.timeout.connect(self._request_terminal_directory)
        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(3000)
        self._monitor_timer.timeout.connect(self._request_system_status)
        self._monitor_defer_timer = QTimer(self)
        self._monitor_defer_timer.setSingleShot(True)
        self._monitor_defer_timer.timeout.connect(self._request_system_status)
        self._monitor_apply_timer = QTimer(self)
        self._monitor_apply_timer.setSingleShot(True)
        self._monitor_apply_timer.timeout.connect(self._apply_pending_system_status)
        self._monitor_width_timer = QTimer(self)
        self._monitor_width_timer.setSingleShot(True)
        self._monitor_width_timer.setInterval(400)
        self._monitor_width_timer.timeout.connect(self._save_monitor_panel_width)
        self._create_widgets()
        self._theme_colors = {
            "surface": "#ffffff", "foreground": "#111827", "muted": "#64748b",
            "border": "#cbd5e1", "hover": "#eaf3ff", "selection": "#dbeafe",
            "selection_text": "#111827",
        }
        self.apply_theme(self._theme_colors)
        self._set_state("disconnected")
        QTimer.singleShot(0, self._apply_terminal_resize)

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return bool(self._session is not None and self._session.connected)

    @property
    def display_target(self) -> str:
        address = mask_ip_address(self.parameters.ip_address, self._ip_hiding)
        return f"{self.parameters.username}@{address}:{self.parameters.port}"

    def set_ip_hiding(self, enabled: bool) -> None:
        self._ip_hiding = enabled
        self.target_label.setText(self.display_target)

    def _create_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(6, 5, 6, 5)
        self.target_label = QLabel(self.display_target)
        header.addWidget(self.target_label)
        self.status_label = QLabel()
        header.addWidget(self.status_label, 1)
        self.ssh_tool_button = QPushButton(tr("终端模式"))
        self.ssh_tool_button.clicked.connect(self._tool_mode_requested)
        header.addWidget(self.ssh_tool_button)
        self.action_button = QPushButton()
        self.action_button.clicked.connect(self._handle_action)
        header.addWidget(self.action_button)
        self.file_transfer_button = QPushButton(tr("文件传输"))
        self.file_transfer_button.clicked.connect(self._open_file_transfer)
        header.addWidget(self.file_transfer_button)
        self.download_task_button = QPushButton(tr("上传/下载"))
        self.download_task_button.clicked.connect(
            lambda: self.download_task_panel.show_panel()
        )
        header.addWidget(self.download_task_button)
        close_button = QPushButton(tr("关闭标签"))
        close_button.clicked.connect(lambda: self._close_requested(self))
        header.addWidget(close_button)
        root.addLayout(header)

        self.body_splitter = QSplitter(Qt.Horizontal)
        self.system_panel = QWidget()
        self.system_panel.setMinimumWidth(220)
        self.system_panel.setMaximumWidth(400)
        system_layout = QVBoxLayout(self.system_panel)
        system_layout.setContentsMargins(6, 6, 6, 6)
        system_layout.setSpacing(6)
        system_title = QLabel(tr("服务器状态"))
        system_title.setStyleSheet("font-weight:600; padding:4px 0;")
        system_layout.addWidget(system_title)
        self.system_label = QLabel(tr("操作系统：--"))
        self.system_label.setWordWrap(True)
        self.kernel_label = QLabel(tr("内核版本：--"))
        self.kernel_label.setWordWrap(True)
        self.uptime_label = QLabel(tr("运行时间：--"))
        self.load_label = QLabel(tr("系统负载：--"))
        system_layout.addWidget(self.system_label)
        system_layout.addWidget(self.kernel_label)
        system_layout.addWidget(self.uptime_label)
        system_layout.addWidget(self.load_label)
        self.cpu_progress = QProgressBar()
        self.memory_progress = QProgressBar()
        self.swap_progress = QProgressBar()
        for progress in (
            self.cpu_progress,
            self.memory_progress,
            self.swap_progress,
        ):
            progress.setRange(0, 100)
            progress.setFixedHeight(22)
            progress.setTextVisible(True)
            system_layout.addWidget(progress)
        disk_title = QLabel(tr("磁盘占用"))
        disk_title.setStyleSheet("font-weight:600; padding-top:6px;")
        system_layout.addWidget(disk_title)
        self.disk_table = QTreeWidget()
        self.disk_table.setColumnCount(3)
        self.disk_table.setHeaderLabels([tr("挂载路径"), tr("已用 / 总量"), tr("占用")])
        self.disk_table.setRootIsDecorated(False)
        self.disk_table.setAlternatingRowColors(True)
        self.disk_table.setStyleSheet(
            "QTreeWidget::item:hover { background:#eaf3ff; color:#111827; }"
            "QTreeWidget::item:selected { background:#dbeafe; color:#111827; }"
        )
        self.disk_table.setColumnWidth(0, 100)
        self.disk_table.setColumnWidth(1, 105)
        self.disk_table.setColumnWidth(2, 48)
        system_layout.addWidget(self.disk_table, 1)
        process_title = QLabel(tr("运行中的程序（资源占用前 12）"))
        process_title.setStyleSheet("font-weight:600; padding-top:6px;")
        system_layout.addWidget(process_title)
        self.process_table = QTreeWidget()
        self.process_table.setColumnCount(4)
        self.process_table.setHeaderLabels([tr("服务"), tr("端口"), "CPU", tr("内存")])
        self.process_table.setRootIsDecorated(False)
        self.process_table.setAlternatingRowColors(True)
        self.process_table.setStyleSheet(
            "QTreeWidget::item:hover { background:#eaf3ff; color:#111827; }"
            "QTreeWidget::item:selected { background:#dbeafe; color:#111827; }"
        )
        self.process_table.setColumnWidth(0, 112)
        self.process_table.setColumnWidth(1, 60)
        self.process_table.setColumnWidth(2, 42)
        self.process_table.setColumnWidth(3, 42)
        self.process_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.process_table.customContextMenuRequested.connect(
            self._show_process_context_menu
        )
        system_layout.addWidget(self.process_table, 1)
        self.monitor_status_label = QLabel(tr("等待连接"))
        self.monitor_status_label.setStyleSheet("color:#64748b;")
        system_layout.addWidget(self.monitor_status_label)
        self.system_panel.setVisible(False)
        self.body_splitter.addWidget(self.system_panel)

        terminal_container = QWidget()
        terminal_container_layout = QVBoxLayout(terminal_container)
        terminal_container_layout.setContentsMargins(0, 0, 0, 0)
        terminal_container_layout.setSpacing(0)
        self.terminal_file_splitter = QSplitter(Qt.Vertical)
        terminal_content = QWidget()
        terminal_layout = QVBoxLayout(terminal_content)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.setSpacing(0)
        self.output_text = XTermTerminal(
            paste_handler=self._paste_remote_clipboard_from_terminal,
            cache_directory=self.parameter_path.parent.parent,
        )
        self.output_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_text.customContextMenuRequested.connect(
            self._show_terminal_context_menu
        )
        self.output_text.textCommitted.connect(self._send_raw)
        self.output_text.binaryCommitted.connect(self._send_binary)
        self.output_text.shellIdentified.connect(self._register_terminal_shell)
        self.output_text.directoryActivated.connect(self._jump_terminal_directory)
        self.output_text.terminalResized.connect(self._terminal_resized)
        self.output_text.loadFailed.connect(self._append)
        terminal_layout.addWidget(self.output_text, 1)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(4, 5, 4, 5)
        input_row.addWidget(QLabel(tr("整行命令：")))
        self.command_entry = QLineEdit()
        self.command_entry.returnPressed.connect(self._send_command)
        self.command_entry.installEventFilter(self)
        input_row.addWidget(self.command_entry, 1)
        self.send_button = QPushButton(tr("发送"))
        self.send_button.clicked.connect(self._send_command)
        input_row.addWidget(self.send_button)
        self.interrupt_button = QPushButton(tr("中断"))
        self.interrupt_button.clicked.connect(self._interrupt)
        input_row.addWidget(self.interrupt_button)
        clear_button = QPushButton(tr("清屏"))
        clear_button.clicked.connect(self.clear)
        input_row.addWidget(clear_button)
        self.file_panel_button = QPushButton("▲")
        self.file_panel_button.setFixedWidth(32)
        self.file_panel_button.setToolTip(tr("展开服务器文件管理"))
        self.file_panel_button.clicked.connect(self._toggle_file_manager)
        input_row.addWidget(self.file_panel_button)
        terminal_layout.addLayout(input_row)
        self.terminal_file_splitter.addWidget(terminal_content)
        self.terminal_file_splitter.setStretchFactor(0, 1)
        terminal_container_layout.addWidget(self.terminal_file_splitter)
        self.body_splitter.addWidget(terminal_container)
        self.body_splitter.setStretchFactor(0, 0)
        self.body_splitter.setStretchFactor(1, 1)
        self.body_splitter.splitterMoved.connect(self._monitor_splitter_moved)
        self.body_splitter.setSizes([0, 900])
        root.addWidget(self.body_splitter, 1)
        self.download_task_panel = _DownloadTaskPanel(self)
        self._update_upload_controls()

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._theme_colors = dict(colors)
        table_style = (
            f"QTreeWidget::item:hover {{ background:{colors['hover']}; color:{colors['foreground']}; }}"
            f"QTreeWidget::item:selected {{ background:{colors['selection']}; color:{colors['selection_text']}; }}"
        )
        self.disk_table.setStyleSheet(table_style)
        self.process_table.setStyleSheet(table_style)
        self.monitor_status_label.setStyleSheet(f"color:{colors['muted']};")
        self.download_task_panel.apply_theme(colors)
        for manager in (self._file_manager, self._transfer_dialog):
            if manager is not None:
                manager.apply_theme(colors)

    def _menu_style(self) -> str:
        colors = self._theme_colors
        return (
            f"QMenu {{ background:{colors['surface']}; color:{colors['foreground']}; border:1px solid {colors['border']}; }}"
            "QMenu::item { padding:6px 22px; }"
            f"QMenu::item:selected {{ background:{colors['selection']}; color:{colors['selection_text']}; }}"
        )

    def set_tool_mode(self, active: bool, enabled: bool = True) -> None:
        self.ssh_tool_button.setText(tr("返回编辑") if active else tr("终端模式"))
        self.ssh_tool_button.setEnabled(enabled)
        if active != (not self.system_panel.isHidden()):
            self.system_panel.setVisible(active)
            self.body_splitter.setSizes(
                [self._monitor_panel_width, 900] if active else [0, 900]
            )
            QTimer.singleShot(0, self._refresh_terminal_layout)

    def _monitor_splitter_moved(self, _position: int, _index: int) -> None:
        if not self.system_panel.isVisible():
            return
        width = self.system_panel.width()
        if width < 220:
            return
        self._monitor_panel_width = width
        self._monitor_width_timer.start()

    def _save_monitor_panel_width(self) -> None:
        self._monitor_width_changed(self._monitor_panel_width)

    def _refresh_terminal_layout(self) -> None:
        if self._closed:
            return
        self._resize_timer.start(80)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.command_entry and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Tab and self.connected:
                pending = self.command_entry.text()
                if pending:
                    self._send_raw(pending)
                    self.command_entry.clear()
                self._send_raw("\t")
                self.output_text.focus_terminal()
                return True
            if key == Qt.Key_Up:
                self._history_previous()
                return True
            if key == Qt.Key_Down:
                self._history_next()
                return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "download_task_panel"):
            self.download_task_panel.reposition()
        if not self._closed:
            self._resize_timer.start(80)

    def start_connection(self) -> None:
        if self._state == "connecting" or self.connected:
            return
        self._resize_timer.stop()
        self._apply_terminal_resize()
        old_session = self._session
        if old_session is not None:
            self._close_session_async(old_session)
        self._attempt += 1
        attempt = self._attempt
        session: InteractiveSSHSession

        def emit_output(value: str) -> None:
            for offset in range(0, len(value), _MAX_OUTPUT_CHARACTERS_PER_POLL):
                chunk = value[offset : offset + _MAX_OUTPUT_CHARACTERS_PER_POLL]
                if not self._queue_event("output", (attempt, session, chunk)):
                    return

        def emit_closed(error: str | None) -> None:
            self._queue_event("closed", (attempt, session, error))

        def confirm_host_key(hostname: str, key_type: str, fingerprint: str) -> bool:
            return self._request_host_key_confirmation(
                attempt, session, hostname, key_type, fingerprint
            )

        session = InteractiveSSHSession(
            output=emit_output,
            closed=emit_closed,
            confirm_host_key=confirm_host_key,
            known_hosts_path=self.parameter_path.parent.parent / "known_hosts",
        )
        session.resize_pty(self._terminal_columns, self._terminal_rows)
        self._session = session
        self._set_state("connecting")
        self._append(tr("[SSH] 正在连接 {0}\n", self.parameters.target))
        threading.Thread(
            target=self._connect_worker,
            args=(session, attempt),
            name=f"ssh-connect-{self.parameters.name}",
            daemon=True,
        ).start()

    def _connect_worker(self, session: InteractiveSSHSession, attempt: int) -> None:
        try:
            session.connect(
                self.parameters,
                self.default_open_path,
                self.default_open_command,
                track_directory=True,
            )
        except SSHSessionError as exc:
            self._queue_event("connect_error", (attempt, session, str(exc)))
        except Exception as exc:
            self._queue_event("connect_error", (attempt, session, tr("SSH 连接失败：{0}", exc)))
        else:
            self._queue_event("connected", (attempt, session))

    def _queue_event(self, event_type: str, payload: object) -> bool:
        while True:
            queued = False
            with self._event_notification_lock:
                if self._closed:
                    return False
                if event_type == "output":
                    attempt, session, _value = payload
                    if not self._is_current(attempt, session):
                        return False
                try:
                    self._events.put_nowait((event_type, payload))
                    queued = True
                except queue.Full:
                    pass
            if queued:
                self._notify_events_available()
                return True
            time.sleep(0.01)

    def _notify_events_available(self) -> None:
        with self._event_notification_lock:
            if self._closed or self._event_drain_pending:
                return
            self._event_drain_pending = True
            self._thread_events.available.emit()

    def _drain_events(self) -> None:
        if self._closed:
            with self._event_notification_lock:
                self._event_drain_pending = False
            return
        started_at = time.monotonic()
        processed = 0
        characters = 0
        chunks: list[str] = []
        while processed < 256 and characters < _MAX_OUTPUT_CHARACTERS_PER_POLL:
            if time.monotonic() - started_at >= _EVENT_DRAIN_TIME_SLICE_SECONDS:
                break
            if self._deferred_output_event is not None:
                event_type = "output"
                payload = self._deferred_output_event
                self._deferred_output_event = None
            else:
                try:
                    event_type, payload = self._events.get_nowait()
                except queue.Empty:
                    break
            processed += 1
            if event_type == "output":
                attempt, session, value = payload
                if self._is_current(attempt, session):
                    value = str(value)
                    remaining = _MAX_OUTPUT_CHARACTERS_PER_POLL - characters
                    chunks.append(value[:remaining])
                    characters += min(len(value), remaining)
                    if len(value) > remaining:
                        self._deferred_output_event = (
                            attempt, session, value[remaining:]
                        )
                        break
            elif event_type == "connected":
                attempt, session = payload
                if self._is_current(attempt, session):
                    if session.connected:
                        self._set_state("connected")
                        self.focus_terminal()
                    else:
                        self._session = None
                        chunks.append(tr("\r\n[SSH] 连接建立后立即关闭，请检查服务器状态\r\n"))
                        self._set_state("error")
                        self._close_session_async(session)
            elif event_type == "connect_error":
                attempt, session, error = payload
                if self._is_current(attempt, session):
                    self._session = None
                    chunks.append(f"\r\n[SSH] {error}\r\n")
                    self._set_state("error")
            elif event_type == "closed":
                attempt, session, error = payload
                if self._is_current(attempt, session):
                    self._session = None
                    chunks.append(
                        tr("\r\n[SSH] 连接已关闭：{0}\r\n", error)
                        if error else tr("\r\n[SSH] 连接已关闭\r\n")
                    )
                    self._set_state("disconnected")
            elif event_type == "host_key":
                request = payload
                if isinstance(request, _HostKeyRequest) and not request.completed.is_set():
                    approved = False
                    if self._is_current(request.attempt, request.session):
                        approved = QMessageBox.question(
                            self,
                            tr("确认服务器主机密钥"),
                            tr("这是第一次连接该服务器，尚未保存它的主机密钥。\n\n服务器：{0}\n密钥类型：{1}\n指纹：{2}\n\n请确认该指纹与服务器管理员提供的一致。是否信任并保存？", request.hostname, request.key_type, request.fingerprint),
                            QMessageBox.Yes | QMessageBox.No,
                            QMessageBox.No,
                        ) == QMessageBox.Yes
                    request.approved = approved
                    request.completed.set()
            elif event_type == "terminal_selection":
                attempt, session, context, revision, generation, verified, error = payload
                if (
                    self._is_current(attempt, session) and not error and verified is not None
                    and generation == self._path_resolver.generation
                    and (context.resolved_path is not None or revision == self._directory_input_revision)
                ):
                    self._path_resolver.remember(verified.resolved_path, verified.verified_type)
                    if context.resolved_path is None and verified.working_directory and revision == self._directory_input_revision:
                        self._directory_verified_revision = revision
                        self._directory_last_checked = time.monotonic()
                        self._sync_terminal_directory(verified.working_directory)
            elif event_type == "terminal_path_action":
                attempt, session, action, context, revision, generation, verified, error = payload
                if self._is_current(attempt, session) and self.connected:
                    self._terminal_path_action_running = False
                    self.status_label.setText(tr("已连接"))
                    if (context.resolved_path is None or action == "jump_directory") and revision != self._directory_input_revision:
                        QMessageBox.warning(self, tr("未执行操作"), tr("查询期间终端目录已变化，请重新选择路径"))
                    elif error:
                        QMessageBox.warning(self, tr("无法操作远程路径"), error)
                    elif verified is not None:
                        if generation == self._path_resolver.generation:
                            self._path_resolver.remember(verified.resolved_path, verified.verified_type)
                        if context.resolved_path is None and verified.working_directory and revision == self._directory_input_revision:
                            self._directory_verified_revision = revision
                            self._directory_last_checked = time.monotonic()
                            self._sync_terminal_directory(verified.working_directory)
                        self._dispatch_terminal_path_action(action, verified)
            elif event_type == "terminal_paste_directory":
                attempt, session, clipboard, revision, directory, error = payload
                if self._is_current(attempt, session):
                    self._terminal_paste_running = False
                    manager = self._ensure_file_manager()
                    if manager._remote_clipboard() is not clipboard:
                        continue
                    if revision != self._directory_input_revision:
                        QMessageBox.warning(self, tr("未执行粘贴"), tr("查询期间终端执行了新命令，请在目标目录重新粘贴"))
                    elif error or not directory:
                        QMessageBox.warning(self, tr("无法粘贴文件"), error or tr("无法确认终端当前目录，请在下方文件窗口打开目标目录后粘贴"))
                    else:
                        manager._paste_remote_items(directory)
            elif event_type == "terminal_directory":
                attempt, session, revision, directory = payload
                if self._is_current(attempt, session):
                    self._directory_running = False
                    if revision != self._directory_input_revision:
                        QTimer.singleShot(0, self._request_terminal_directory)
                    elif directory is not None and self._terminal_refresh_delay() <= 0:
                        self._directory_verified_revision = revision
                        self._directory_last_checked = time.monotonic()
                        self._sync_terminal_directory(directory)
            elif event_type == "system_status":
                attempt, session, value, error = payload
                if self._is_current(attempt, session):
                    self._monitor_running = False
                    if error:
                        self.monitor_status_label.setText(str(error))
                    else:
                        self._pending_monitor_status = (attempt, session, str(value))
                        self._apply_pending_system_status()
            elif event_type == "process_stopped":
                attempt, session, pid, service_name, error = payload
                if self._is_current(attempt, session):
                    self._process_operations.discard(pid)
                    if error:
                        QMessageBox.critical(
                            self, tr("停止失败"), tr("无法停止 {0}：\n{1}", service_name, error)
                        )
                    else:
                        QMessageBox.information(
                            self,
                            tr("已发送停止指令"),
                            tr("已向 {0}（PID {1}）发送停止指令", service_name, pid),
                        )
                        QTimer.singleShot(800, self._request_system_status)
            elif event_type == "process_restarted":
                attempt, session, pid, service_name, message, error = payload
                if self._is_current(attempt, session):
                    self._process_operations.discard(pid)
                    if error:
                        self._log_event(tr("重启服务失败"), f"{service_name}（PID {pid}）：{error}")
                        QMessageBox.critical(self, tr("重启失败"), f"{service_name}：\n{error}")
                    else:
                        self._log_event(tr("重启服务"), f"{service_name}：{message}")
                        QMessageBox.information(
                            self, tr("重启结果"),
                            tr("{0}\n{1}\n\n此结果不能证明服务已恢复；请再检查服务状态、监听端口和日志。", service_name, message),
                        )
                    QTimer.singleShot(800, self._request_system_status)
        if chunks:
            self._feed_terminal("".join(chunks))
        with self._event_notification_lock:
            has_more_events = (
                self._deferred_output_event is not None
                or not self._events.empty()
            )
            if not has_more_events:
                self._event_drain_pending = False
        if has_more_events:
            QTimer.singleShot(0, self._drain_events)

    def _is_current(self, attempt: int, session: InteractiveSSHSession) -> bool:
        return attempt == self._attempt and session is self._session

    def _request_host_key_confirmation(
        self,
        attempt: int,
        session: InteractiveSSHSession,
        hostname: str,
        key_type: str,
        fingerprint: str,
    ) -> bool:
        request = _HostKeyRequest(attempt, session, hostname, key_type, fingerprint)
        with self._host_key_requests_lock:
            if self._closed:
                return False
            self._host_key_requests.add(request)
        if not self._queue_event("host_key", request):
            request.completed.set()
        request.completed.wait(timeout=120)
        with self._host_key_requests_lock:
            self._host_key_requests.discard(request)
        return request.completed.is_set() and request.approved

    def _reject_host_key_requests(self) -> None:
        with self._host_key_requests_lock:
            requests = list(self._host_key_requests)
            self._host_key_requests.clear()
        for request in requests:
            request.approved = False
            request.completed.set()

    def _handle_action(self) -> None:
        if self._state == "connecting":
            self.cancel_connection()
        elif self.connected:
            self.disconnect()
        else:
            self.start_connection()

    def _set_state(self, state: str) -> None:
        self._state = state
        if state != "connected":
            self._flush_terminal_jump_echo()
            self.output_text.begin_directory_context()
            self._terminal_paste_running = False
            self._terminal_directory_jump = None
            self.command_entry.clear()
            self._process_operations.clear()
            self._path_resolver.clear()
            self.output_text.set_path_hints([])
            self._terminal_path_action_running = False
            self._terminal_input_line = ""
            self._terminal_input_uncertain = False
            self._directory_last_checked = 0.0
            if self._terminal_menu is not None:
                self._terminal_menu.close()
        status, action = {
            "connecting": (tr("正在连接…"), tr("取消连接")),
            "connected": (tr("已连接"), tr("断开")),
            "cancelled": (tr("已取消"), tr("重新连接")),
            "error": (tr("连接失败"), tr("重新连接")),
            "disconnected": (tr("未连接"), tr("连接")),
        }[state]
        self.status_label.setText(status)
        self.action_button.setText(action)
        self._flush_terminal_log()
        self._log_event(tr("连接状态"), status)
        if state == "connected":
            self._directory_timer.start()
            self._monitor_timer.start()
            QTimer.singleShot(0, self._request_terminal_directory)
            QTimer.singleShot(0, self._request_system_status)
        else:
            self._directory_timer.stop()
            self._directory_running = False
            self._terminal_directory = None
            self._directory_verified_revision = -1
            self._hide_file_manager()
            if self._file_manager is not None:
                self._file_manager.cancel_all_downloads()
                threading.Thread(
                    target=self._file_manager.release_browse_channel,
                    name="sftp-browser-disconnect",
                    daemon=True,
                ).start()
            self._monitor_timer.stop()
            self._monitor_defer_timer.stop()
            self._monitor_apply_timer.stop()
            self._pending_monitor_status = None
            self._monitor_running = False
            self._previous_cpu_total = None
            self._previous_cpu_idle = None
            self._reset_system_status(status)
        enabled = state == "connected"
        self.command_entry.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.interrupt_button.setEnabled(enabled)
        self._update_upload_controls()
        self._state_changed(self)

    def cancel_connection(self) -> None:
        if self._state != "connecting":
            return
        self._attempt += 1
        self._reject_host_key_requests()
        session, self._session = self._session, None
        if session is not None:
            self._close_session_async(session)
        self._append(tr("[SSH] 已取消连接\n"))
        self._set_state("cancelled")

    def disconnect(self) -> None:
        self._attempt += 1
        self._reject_host_key_requests()
        session, self._session = self._session, None
        if session is not None:
            self._close_session_async(session)
        self._append(tr("[SSH] 已断开连接\n"))
        self._set_state("disconnected")

    def focus_terminal(self) -> None:
        if self.connected:
            self.output_text.focus_terminal()

    def clear(self) -> None:
        self._flush_terminal_jump_echo()
        self.output_text.clear()
        if self.connected and self._session is not None:
            try:
                self._session.send_raw("\x0c")
            except SSHSessionError:
                pass

    def shutdown(self) -> None:
        with self._event_notification_lock:
            if self._closed:
                return
            self._closed = True
        if self._terminal_menu is not None:
            self._terminal_menu.close()
        try:
            self._file_context_requests.get_nowait()
        except queue.Empty:
            pass
        try:
            self._file_context_requests.put_nowait(None)
        except queue.Full:
            pass
        self._flush_pending_output_log()
        self._flush_terminal_jump_echo()
        self._attempt += 1
        self.output_text.shutdown()
        self._terminal_log_timer.stop()
        self._flush_terminal_log()
        self._resize_timer.stop()
        self._directory_timer.stop()
        self._monitor_timer.stop()
        self._monitor_defer_timer.stop()
        self._monitor_apply_timer.stop()
        self._pending_monitor_status = None
        self._monitor_width_timer.stop()
        self._reject_host_key_requests()
        for manager in (self._file_manager, self._transfer_dialog):
            if manager is None:
                continue
            manager.cancel_all_downloads()
            manager.hide()
            threading.Thread(
                target=manager.release_browse_channel,
                name="sftp-browser-shutdown",
                daemon=True,
            ).start()
        session, self._session = self._session, None
        if session is not None:
            self._close_session_async(session)

    def _flush_pending_output_log(self) -> None:
        pending = self._deferred_output_event
        self._deferred_output_event = None
        if pending is not None:
            attempt, session, value = pending
            if self._is_current(attempt, session):
                self._terminal_log_chunks.append(value)
        while True:
            try:
                event_type, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if event_type == "output":
                attempt, session, value = payload
                if self._is_current(attempt, session):
                    self._terminal_log_chunks.append(str(value))
        self._flush_terminal_log()

    def _close_session_async(self, session: InteractiveSSHSession) -> None:
        threading.Thread(
            target=session.close,
            kwargs={"notify": False},
            name=f"ssh-close-{self.parameters.name}",
            daemon=True,
        ).start()

    def _request_system_status(self) -> None:
        session = self._session
        if self._monitor_running or session is None or not self.connected:
            return
        delay = self._terminal_refresh_delay()
        if delay > 0:
            self._monitor_defer_timer.start(max(50, int(delay * 1000)))
            return
        self._monitor_defer_timer.stop()
        self._monitor_running = True
        attempt = self._attempt
        threading.Thread(
            target=self._system_status_worker,
            args=(session, attempt),
            name=f"ssh-monitor-{self.parameters.name}",
            daemon=True,
        ).start()

    def _system_status_worker(
        self, session: InteractiveSSHSession, attempt: int
    ) -> None:
        try:
            value = session.query_system_status()
        except SSHSessionError as exc:
            self._queue_event("system_status", (attempt, session, "", str(exc)))
        else:
            self._queue_event("system_status", (attempt, session, value, ""))

    def _apply_pending_system_status(self) -> None:
        pending = self._pending_monitor_status
        if pending is None:
            return
        attempt, session, value = pending
        if not self._is_current(attempt, session):
            self._pending_monitor_status = None
            return
        delay = self._terminal_refresh_delay()
        if delay > 0 or not self._events.empty() or self._deferred_output_event is not None:
            self._monitor_apply_timer.start(max(250, int(delay * 1000)))
            return
        self._pending_monitor_status = None
        self._update_system_status(value)

    def _reset_system_status(self, status: str) -> None:
        self.system_label.setText(tr("操作系统：--"))
        self.kernel_label.setText(tr("内核版本：--"))
        self.uptime_label.setText(tr("运行时间：--"))
        self.load_label.setText(tr("系统负载：--"))
        self.cpu_progress.setValue(0)
        self.cpu_progress.setFormat("CPU：--")
        self.memory_progress.setValue(0)
        self.memory_progress.setFormat(tr("内存：--"))
        self.swap_progress.setValue(0)
        self.swap_progress.setFormat(tr("交换区：--"))
        self.disk_table.clear()
        self.process_table.clear()
        self.monitor_status_label.setText(status)

    def _update_system_status(self, value: str) -> None:
        memory: dict[str, int] = {}
        disks: list[tuple[str, int, int, int]] = []
        processes: list[tuple[str, str, str, str, str]] = []
        process_ports: dict[str, set[str]] = {}
        uptime_seconds = 0
        load_value = "--"
        system_value = "--"
        kernel_value = "--"
        cpu_total: int | None = None
        cpu_idle: int | None = None
        reading_disks = False
        reading_processes = False
        reading_ports = False
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line == "DF_BEGIN":
                reading_disks = True
                reading_processes = False
                reading_ports = False
                continue
            if line == "PS_BEGIN":
                reading_disks = False
                reading_processes = True
                reading_ports = False
                continue
            if line == "PORT_BEGIN":
                reading_disks = False
                reading_processes = False
                reading_ports = True
                continue
            if reading_ports:
                pids = re.findall(r"pid=(\d+)", line)
                ports = re.findall(r":(\d+)\b", line)
                if pids and ports:
                    port = ports[0]
                    for pid in pids:
                        process_ports.setdefault(pid, set()).add(port)
                continue
            if reading_processes:
                parts = line.split(maxsplit=4)
                if len(parts) >= 4:
                    pid, cpu, memory_percent, name = parts[:4]
                    command = parts[4] if len(parts) > 4 else name
                    processes.append((pid, name, cpu, memory_percent, command))
                continue
            if reading_disks:
                if line.lower().startswith("filesystem"):
                    continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        total = int(parts[1])
                        used = int(parts[2])
                        percent = int(parts[4].rstrip("%"))
                    except ValueError:
                        continue
                    disks.append((parts[-1].replace("\\040", " "), used, total, percent))
                continue
            if line.startswith("SYSTEM "):
                system_value = line.partition(" ")[2] or "--"
            elif line.startswith("KERNEL "):
                kernel_value = line.partition(" ")[2] or "--"
            elif line.startswith("UPTIME "):
                try:
                    uptime_seconds = int(line.split(maxsplit=1)[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("LOAD "):
                load_value = line.split(maxsplit=1)[1]
            elif line.startswith("cpu "):
                try:
                    values = [int(item) for item in line.split()[1:]]
                except ValueError:
                    continue
                cpu_total = sum(values)
                cpu_idle = values[3] + (values[4] if len(values) > 4 else 0)
            elif ":" in line:
                key, raw_value = line.split(":", 1)
                try:
                    memory[key] = int(raw_value.strip().split()[0])
                except (IndexError, ValueError):
                    pass

        self.system_label.setText(tr("操作系统：{0}", system_value))
        self.kernel_label.setText(tr("内核版本：{0}", kernel_value))
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        uptime_parts = []
        if days:
            uptime_parts.append(tr("{0} 天", days))
        if hours or days:
            uptime_parts.append(tr("{0} 小时", hours))
        uptime_parts.append(tr("{0} 分钟", minutes))
        self.uptime_label.setText(tr("运行时间：") + " ".join(uptime_parts))
        self.load_label.setText(tr("系统负载：{0}", load_value))

        cpu_percent: int | None = None
        if cpu_total is not None and cpu_idle is not None:
            if self._previous_cpu_total is not None and self._previous_cpu_idle is not None:
                total_delta = cpu_total - self._previous_cpu_total
                idle_delta = cpu_idle - self._previous_cpu_idle
                if total_delta > 0:
                    cpu_percent = max(
                        0, min(100, round((total_delta - idle_delta) * 100 / total_delta))
                    )
            self._previous_cpu_total = cpu_total
            self._previous_cpu_idle = cpu_idle
        self.cpu_progress.setValue(cpu_percent or 0)
        self.cpu_progress.setFormat(
            f"CPU：{cpu_percent}%" if cpu_percent is not None else tr("CPU：采集中")
        )

        memory_total = memory.get("MemTotal", 0)
        memory_used = max(0, memory_total - memory.get("MemAvailable", memory_total))
        memory_percent = round(memory_used * 100 / memory_total) if memory_total else 0
        self.memory_progress.setValue(memory_percent)
        self.memory_progress.setFormat(
            tr("内存：{0}%  {1}/{2}", memory_percent, self._format_kib(memory_used), self._format_kib(memory_total))
        )
        swap_total = memory.get("SwapTotal", 0)
        swap_used = max(0, swap_total - memory.get("SwapFree", swap_total))
        swap_percent = round(swap_used * 100 / swap_total) if swap_total else 0
        self.swap_progress.setValue(swap_percent)
        self.swap_progress.setFormat(
            tr("交换区：{0}%  {1}/{2}", swap_percent, self._format_kib(swap_used), self._format_kib(swap_total))
            if swap_total
            else tr("交换区：未启用")
        )

        self.disk_table.clear()
        for mount, used, total, percent in disks:
            item = QTreeWidgetItem(
                [
                    mount,
                    f"{self._format_kib(used)} / {self._format_kib(total)}",
                    f"{percent}%",
                ]
            )
            self.disk_table.addTopLevelItem(item)
        self.process_table.clear()
        for pid, name, cpu, memory_percent, command in processes:
            service_name = self._process_display_name(name, command)
            ports = sorted(
                process_ports.get(pid, set()),
                key=lambda value: int(value),
            )
            item = QTreeWidgetItem(
                [
                    service_name,
                    ", ".join(ports) if ports else "--",
                    f"{cpu}%",
                    f"{memory_percent}%",
                ]
            )
            item.setToolTip(0, tr("PID：{0}\n启动命令：{1}", pid, command))
            item.setData(0, Qt.UserRole, pid)
            self.process_table.addTopLevelItem(item)
        self.monitor_status_label.setText(
            tr("更新时间：") + time.strftime("%H:%M:%S")
        )

    @staticmethod
    def _format_kib(value: int) -> str:
        size = float(max(0, value))
        for unit in ("KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.0f}{unit}" if unit == "KB" else f"{size:.1f}{unit}"
            size /= 1024
        return "0KB"

    @staticmethod
    def _process_display_name(name: str, command: str) -> str:
        spring_name = re.search(
            r"-Dspring\.application\.name=(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
            command,
        )
        if spring_name:
            return next(value for value in spring_name.groups() if value)
        jar = re.search(r"(?:^|\s)-jar\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
        if jar:
            jar_path = next(value for value in jar.groups() if value)
            return posixpath.basename(jar_path.rstrip("/")) or name
        if "org.apache.catalina.startup.Bootstrap" in command:
            catalina_base = re.search(r"-Dcatalina\.base=(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
            if catalina_base:
                base_path = next(value for value in catalina_base.groups() if value)
                return f"Tomcat · {posixpath.basename(base_path.rstrip('/'))}"
            return "Tomcat"
        if name.startswith("python"):
            script = re.search(r"(?:^|\s)([^\s]+\.py)(?:\s|$)", command)
            if script:
                return posixpath.basename(script.group(1))
        if name in {"node", "nodejs"}:
            script = re.search(r"(?:^|\s)([^\s]+\.js)(?:\s|$)", command)
            if script:
                return posixpath.basename(script.group(1))
        return name

    def _show_process_context_menu(self, position: QPoint) -> None:
        item = self.process_table.itemAt(position)
        if item is None:
            return
        self.process_table.setCurrentItem(item)
        pid = str(item.data(0, Qt.UserRole) or "")
        if not pid.isdigit() or int(pid) <= 1:
            return
        service_name, ports = item.text(0), item.text(1)
        session, attempt = self._session, self._attempt
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())
        stop_action = menu.addAction(tr("停止此服务"))
        restart_action = menu.addAction(tr("重启（不建议）"))
        for action in (stop_action, restart_action):
            action.setEnabled(self.connected and int(pid) not in self._process_operations)
        selected = menu.exec(self.process_table.viewport().mapToGlobal(position))
        menu.deleteLater()
        if session is None or not self._is_current(attempt, session):
            return
        if selected is stop_action:
            self._confirm_terminate_process(int(pid), service_name, ports)
        elif selected is restart_action:
            self._confirm_restart_process(int(pid), service_name, ports)

    def _confirm_restart_process(
        self, pid: int, service_name: str, ports: str,
    ) -> None:
        session, attempt = self._session, self._attempt
        if session is None or not self.connected or pid in self._process_operations:
            return
        port_text = tr("\n监听端口：{0}", ports) if ports and ports != "--" else ""
        if QMessageBox.question(
            self, tr("确认重启服务"),
            tr("确定重启“{0}”吗？\nPID：{1}{2}\n\n此操作会读取进程命令和工作目录，停止原进程后尝试重新启动。\n原启动脚本、日志重定向和管道可能无法还原；日志可能写到 /tmp 下的补充日志，服务也会短暂中断。\n\n不建议使用此方式重启服务。请优先使用原启动脚本或 systemctl restart。\n\n仍要继续吗？", service_name, pid, port_text),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if not self._is_current(attempt, session) or not self.connected or pid in self._process_operations:
            return
        self._process_operations.add(pid)
        self.monitor_status_label.setText(tr("正在重启：{0}…", service_name))
        threading.Thread(
            target=self._restart_process_worker,
            args=(session, attempt, pid, service_name),
            name=f"ssh-restart-{pid}", daemon=True,
        ).start()

    def _restart_process_worker(
        self, session: InteractiveSSHSession, attempt: int, pid: int, service_name: str,
    ) -> None:
        message, error = "", ""
        try:
            message = session.restart_process(pid)
        except Exception as exc:
            error = str(exc)
        self._queue_event(
            "process_restarted", (attempt, session, pid, service_name, message, error)
        )

    def _confirm_terminate_process(
        self, pid: int, service_name: str, ports: str
    ) -> None:
        session, attempt = self._session, self._attempt
        if session is None or not self.connected or pid in self._process_operations:
            return
        port_text = tr("\n监听端口：{0}", ports) if ports and ports != "--" else ""
        if QMessageBox.question(
            self,
            tr("确认停止服务"),
            tr("确定停止服务“{0}”吗？\nPID：{1}{2}\n\n将发送正常终止信号，受系统管理的服务可能会自动重启。", service_name, pid, port_text),
        ) != QMessageBox.Yes:
            return
        if not self._is_current(attempt, session) or not self.connected:
            QMessageBox.warning(self, tr("无法停止"), tr("SSH 连接已断开"))
            return
        self._process_operations.add(pid)
        threading.Thread(
            target=self._terminate_process_worker,
            args=(session, attempt, pid, service_name),
            name=f"ssh-stop-{pid}",
            daemon=True,
        ).start()

    def _terminate_process_worker(
        self,
        session: InteractiveSSHSession,
        attempt: int,
        pid: int,
        service_name: str,
    ) -> None:
        try:
            session.terminate_process(pid)
        except Exception as exc:
            error = str(exc)
        else:
            error = ""
        self._queue_event(
            "process_stopped", (attempt, session, pid, service_name, error)
        )

    def _send_command(self) -> None:
        if not self.connected or self._session is None:
            return
        self._flush_terminal_jump_echo()
        command = self.command_entry.text()
        self._last_terminal_input_at = time.monotonic()
        try:
            self._session.send_line(command)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
            return
        self._command_submitted(command)
        if command.strip():
            self._flush_terminal_log()
            self._log_event("command", command)
            if not self._history or self._history[-1] != command:
                self._history.append(command)
                self._history = self._history[-200:]
            self._history_index = len(self._history)
        self.command_entry.clear()

    def _command_submitted(self, command: str, uncertain: bool = False) -> None:
        self._terminal_input_line = ""
        self._terminal_input_uncertain = False
        if uncertain or command_changes_directory(command):
            self._terminal_directory_jump = None
            self._directory_input_revision += 1
            self.output_text.begin_directory_context()
            self.output_text.set_path_hints([])
            QTimer.singleShot(150, self._request_terminal_directory)

    def _track_terminal_input(self, value: str) -> None:
        for character in value:
            if character in "\r\n":
                self._command_submitted(self._terminal_input_line, self._terminal_input_uncertain)
                self._terminal_input_line = ""
                self._terminal_input_uncertain = False
            elif character in {"\x03", "\x15"}:
                self._terminal_input_line = ""
                self._terminal_input_uncertain = False
            elif character in {"\x08", "\x7f"}:
                self._terminal_input_line = self._terminal_input_line[:-1]
            elif ord(character) < 32:
                # Completion, history and cursor edits cannot be reconstructed reliably.
                self._terminal_input_uncertain = True
            elif len(self._terminal_input_line) < 4096:
                self._terminal_input_line += character
            else:
                self._terminal_input_uncertain = True

    def _history_previous(self) -> None:
        if self._history:
            self._history_index = max(0, self._history_index - 1)
            self.command_entry.setText(self._history[self._history_index])
            self.command_entry.end(False)

    def _history_next(self) -> None:
        if self._history:
            self._history_index = min(len(self._history), self._history_index + 1)
            value = self._history[self._history_index] if self._history_index < len(self._history) else ""
            self.command_entry.setText(value)
            self.command_entry.end(False)

    def _interrupt(self) -> None:
        if self.connected and self._session is not None:
            self._last_terminal_input_at = time.monotonic()
            try:
                self._session.interrupt()
                self.command_entry.clear()
            except SSHSessionError as exc:
                self._append(f"[SSH] {exc}\n")

    def _update_upload_controls(self) -> None:
        self.file_transfer_button.setEnabled(self.connected)
        self.file_panel_button.setEnabled(self.connected)

    def _register_terminal_shell(self, token: str, pid: int, directory: str, context_id: int) -> None:
        session = self._session
        if session is not None and session.register_shell(token, pid):
            if directory.startswith("/") and not any(ord(char) < 32 or ord(char) == 127 for char in directory):
                self.output_text.set_context_directory(context_id, posixpath.normpath(directory))
            self._request_terminal_directory()

    def _request_terminal_directory(self) -> None:
        session = self._session
        if (
            self._closed or self._directory_running or session is None
            or not self.connected
            or not self.isVisible()
        ):
            return
        if (
            self._directory_verified_revision == self._directory_input_revision
            and time.monotonic() - self._directory_last_checked < 5.0
        ):
            return
        if self._terminal_refresh_delay() > 0:
            return
        self._directory_running = True
        threading.Thread(
            target=self._terminal_directory_worker,
            args=(session, self._attempt, self._directory_input_revision),
            name=f"ssh-directory-{self.parameters.name}",
            daemon=True,
        ).start()

    def _terminal_directory_worker(
        self, session: InteractiveSSHSession, attempt: int, revision: int,
    ) -> None:
        try:
            directory = session.query_working_directory()
        except Exception:
            directory = None
        self._queue_event("terminal_directory", (attempt, session, revision, directory))

    def _sync_terminal_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory)
        self.output_text.set_working_directory(directory)
        if self._terminal_directory_jump is not None:
            revision, target = self._terminal_directory_jump
            if revision != self._directory_input_revision:
                self._terminal_directory_jump = None
            elif directory == target:
                self._terminal_directory_jump = None
                self._terminal_directory = directory
                self._refresh_terminal_path_hints()
                self._open_terminal_directory(directory)
                return
        if directory == self._terminal_directory:
            self._refresh_terminal_path_hints()
            return
        self._terminal_directory = directory
        self._refresh_terminal_path_hints()
        if self._file_panel_open and self._file_manager is not None:
            self._file_manager.open_directory(directory)

    def _toggle_file_manager(self) -> None:
        session = self._session
        if session is None or not self.connected:
            QMessageBox.warning(self, tr("无法打开文件管理"), tr("请先连接 SSH 服务器"))
            return
        if self._file_panel_open:
            self._hide_file_manager()
            return
        self._ensure_file_manager()
        if self._terminal_directory is not None:
            self._file_manager.open_directory(self._terminal_directory)
        self._file_manager.show()
        self._file_manager.refresh()
        self._file_panel_open = True
        self._directory_timer.start()
        self._request_terminal_directory()
        self.file_panel_button.setText("▼")
        self.file_panel_button.setToolTip(tr("收起服务器文件管理"))
        total_height = max(560, self.terminal_file_splitter.height())
        panel_height = min(360, max(260, total_height // 3))
        self.terminal_file_splitter.setSizes(
            [total_height - panel_height, panel_height]
        )
        QTimer.singleShot(0, self._refresh_terminal_layout)

    def _ensure_file_manager(self) -> SftpTransferDialog:
        if self._file_manager is None:
            self._file_manager = SftpTransferDialog(
                self,
                self._session,
                self._terminal_directory or self.default_open_path,
                embedded=True,
                log_reader=self._log_reader,
                download_task_panel=self.download_task_panel,
                server_key=self.parameters.target,
                render_allowed=self._can_render_remote_files,
            )
            self._file_manager.apply_theme(self._theme_colors)
            self._file_manager.command_requested.connect(self._execute_selected_command)
            self._file_manager.directory_changed.connect(
                lambda directory: self._sync_file_directory(self._file_manager, directory)
            )
            self._file_manager.directory_listed.connect(lambda _directory: self._refresh_terminal_path_hints())
            self.terminal_file_splitter.addWidget(self._file_manager)
            self.terminal_file_splitter.setStretchFactor(1, 0)
            if not self._file_panel_open:
                self._file_manager.hide()
        else:
            self._file_manager.set_session(self._session)
        return self._file_manager

    def _hide_file_manager(self) -> None:
        if self._file_manager is not None:
            self._file_manager.hide()
        self._file_panel_open = False
        if hasattr(self, "file_panel_button"):
            self.file_panel_button.setText("▲")
            self.file_panel_button.setToolTip(tr("展开服务器文件管理"))
        QTimer.singleShot(0, self._refresh_terminal_layout)

    def _open_file_transfer(self) -> None:
        if self._session is None or not self.connected:
            QMessageBox.warning(self, tr("无法打开文件传输"), tr("请先连接 SSH 服务器"))
            return
        if self._transfer_dialog is None:
            directory = (
                self._file_manager.current_directory()
                if self._file_manager is not None else self.default_open_path
            )
            self._transfer_dialog = SftpTransferDialog(
                self, self._session, directory,
                log_reader=self._log_reader,
                download_task_panel=self.download_task_panel,
                server_key=self.parameters.target,
            )
            self._transfer_dialog.apply_theme(self._theme_colors)
            self._transfer_dialog.command_requested.connect(self._execute_selected_command)
            self._transfer_dialog.directory_changed.connect(
                lambda directory: self._sync_file_directory(self._transfer_dialog, directory)
            )
            self._transfer_dialog.directory_listed.connect(lambda _directory: self._refresh_terminal_path_hints())
        else:
            self._transfer_dialog.set_session(self._session)
            self._transfer_dialog.refresh()
        self._transfer_dialog.show()
        self._transfer_dialog.raise_()
        self._transfer_dialog.activateWindow()

    def _refresh_terminal_path_hints(self) -> None:
        directory = self._terminal_directory
        entries = []
        latest = 0.0
        if directory is not None and self._directory_verified_revision == self._directory_input_revision:
            for manager in (self._file_manager, self._transfer_dialog):
                if manager is not None and manager._session is self._session:
                    recorded_at = manager._file_cache_times.get(directory, 0.0)
                    if recorded_at > latest and time.monotonic() - recorded_at < PATH_TYPE_CACHE_TTL:
                        latest = recorded_at
                        entries = manager._file_cache.get(directory, [])
        self.output_text.set_path_hints([str(entry[0]) for entry in entries])

    def _sync_file_directory(self, source: SftpTransferDialog, directory: str) -> None:
        self._path_resolver.invalidate_directory(directory)
        for manager in (self._file_manager, self._transfer_dialog):
            if manager is None or manager is source:
                continue
            manager._invalidate_directory(directory)
            current = manager.current_directory()
            if manager.isVisible() and current == directory:
                manager.refresh()

    def close_file_editors(self) -> bool:
        return all(
            manager.close_editors()
            for manager in (self._file_manager, self._transfer_dialog)
            if manager is not None
        )

    def _open_terminal_directory(self, directory: str) -> None:
        if not self.connected:
            return
        if not self._file_panel_open:
            self._toggle_file_manager()
        if self._file_manager is not None:
            self._file_manager.file_tabs.setCurrentIndex(0)
            self._file_manager.open_directory(directory)

    def _execute_selected_command(self, command: str, *, hide_echo: bool = False) -> bool:
        if not self.connected or self._session is None:
            return False
        self._flush_terminal_jump_echo()
        if hide_echo:
            self._terminal_jump_echo = command
            self._terminal_jump_echo_timer.start()
        self._last_terminal_input_at = time.monotonic()
        try:
            # Clear an unfinished shell input line before sending the selection.
            clear_line = "\x15" if (
                not hide_echo or self._terminal_input_line or self._terminal_input_uncertain
            ) else ""
            self._session.send_raw(clear_line + command + "\r")
        except SSHSessionError as exc:
            self._flush_terminal_jump_echo()
            self._append(f"[SSH] {exc}\n")
            return False
        self._command_submitted(command)
        self._flush_terminal_log()
        self._log_event("command", command)
        if not self._history or self._history[-1] != command:
            self._history.append(command)
            self._history = self._history[-200:]
        self._history_index = len(self._history)
        self.focus_terminal()
        return True

    def _send_raw(self, value: str) -> None:
        if not value or self._session is None:
            return
        # Terminal replies are not user input and must not cancel echo hiding.
        if re.fullmatch(r"\x1b\[(?:[0-9;?]*[Rcn]|[IO])", value) is None:
            self._flush_terminal_jump_echo()
        self._last_terminal_input_at = time.monotonic()
        try:
            if value == "\x03":
                self._session.interrupt()
                self.command_entry.clear()
            else:
                self._session.send_raw(value)
            self._track_terminal_input(value)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _send_binary(self, value: bytes) -> None:
        if not value or self._session is None:
            return
        self._flush_terminal_jump_echo()
        self._last_terminal_input_at = time.monotonic()
        try:
            self._session.send_bytes(value)
            self._terminal_input_uncertain = True
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _paste_remote_clipboard_from_terminal(self) -> bool:
        clipboard = SftpTransferDialog._clipboard
        mime = QApplication.clipboard().mimeData()
        if (
            clipboard is None or not clipboard.paths or mime is None
            or bytes(mime.data(_REMOTE_FILE_MIME)) != clipboard.token.encode("ascii")
        ):
            return False
        if not self.connected or self._session is None:
            return True
        if clipboard.server != self.parameters.target:
            QMessageBox.warning(self, tr("无法粘贴文件"), tr("请在复制文件的同一台服务器上粘贴"))
            return True
        if clipboard.in_flight or self._terminal_paste_running:
            return True
        self._terminal_paste_running = True
        threading.Thread(
            target=self._resolve_terminal_paste_directory,
            args=(self._session, self._attempt, clipboard, self._directory_input_revision),
            name="ssh-paste-directory", daemon=True,
        ).start()
        return True

    def _resolve_terminal_paste_directory(
        self, session: InteractiveSSHSession, attempt: int,
        clipboard: _RemoteClipboard, revision: int,
    ) -> None:
        directory, error = None, ""
        try:
            directory = session.query_working_directory()
        except Exception as exc:
            error = str(exc)
        self._queue_event(
            "terminal_paste_directory",
            (attempt, session, clipboard, revision, directory, error),
        )

    def _show_terminal_context_menu(self, position: QPoint) -> None:
        selection = self.output_text.context_text()
        text = selection.strip()
        single_line = bool(text) and not any(ord(char) < 32 or ord(char) == 127 for char in text)
        if self._terminal_menu is not None:
            self._terminal_menu.close()
        menu = QMenu(self)
        self._terminal_menu = menu
        copy_action = menu.addAction(tr("复制文本"))
        copy_action.setEnabled(bool(text))
        copy_action.triggered.connect(lambda: QApplication.clipboard().setText(selection))
        mime = QApplication.clipboard().mimeData()
        paste_action = menu.addAction(
            tr("粘贴文件到终端当前目录")
            if mime is not None and mime.hasFormat(_REMOTE_FILE_MIME) else tr("粘贴文本")
        )
        paste_action.setEnabled(self.connected)
        paste_action.triggered.connect(self.output_text.paste_clipboard)
        if single_line and self.output_text.has_selection():
            execute_action = menu.addAction(tr("执行选中的命令"))
            execute_action.setEnabled(self.connected)
            execute_action.triggered.connect(lambda: self._execute_selected_command(text))

        def closed() -> None:
            if self._terminal_menu is menu:
                self._terminal_menu = None
            menu.deleteLater()

        menu.aboutToHide.connect(closed)
        context = None
        session, attempt, revision = self._session, self._attempt, self._directory_input_revision
        if self.connected and session is not None and single_line:
            cwd = self._terminal_directory if self._directory_verified_revision == revision else None
            context = self._path_resolver.guess(selection, cwd)
            if context is not None:
                if context.resolved_path is not None and context.verified_type is None:
                    for manager in (self._file_manager, self._transfer_dialog):
                        if manager is not None and manager._session is session:
                            cached = manager.cached_path_type(context.resolved_path)
                            if cached is not None:
                                kind = RemotePathType.DIRECTORY if cached else RemotePathType.FILE
                                self._path_resolver.remember(context.resolved_path, kind)
                                context = self._path_resolver.guess(selection, cwd)
                                break
                clipboard = SftpTransferDialog._clipboard
                can_paste = bool(
                    clipboard is not None and not clipboard.in_flight
                    and clipboard.server == self.parameters.target and mime is not None
                    and bytes(mime.data(_REMOTE_FILE_MIME)) == clipboard.token.encode("ascii")
                )
                menu.addSeparator()
                RemoteContextMenuBuilder().build(
                    menu, context,
                    lambda action, value: self._terminal_path_action(
                        action, value, session, attempt, revision
                    ),
                    can_paste,
                )
        menu.popup(self.output_text.mapToGlobal(position))
        if context is not None and context.path_type is RemotePathType.UNKNOWN:
            QTimer.singleShot(0, lambda: self._queue_file_context_probe(
                session, attempt, context, revision
            ))

    def _queue_file_context_probe(
        self, session: InteractiveSSHSession, attempt: int,
        context: RemotePathContext, revision: int,
    ) -> None:
        if not self._is_current(attempt, session) or self._closed:
            return
        if self._file_context_thread is None or not self._file_context_thread.is_alive():
            self._file_context_thread = threading.Thread(
                target=self._file_context_worker, name="ssh-file-context", daemon=True,
            )
            self._file_context_thread.start()
        work = (session, attempt, context, revision, self._path_resolver.generation)
        try:
            self._file_context_requests.put_nowait(work)
        except queue.Full:
            try:
                self._file_context_requests.get_nowait()
            except queue.Empty:
                pass
            self._file_context_requests.put_nowait(work)

    def _file_context_worker(self) -> None:
        sftp = None
        active_session = None
        last_used = 0.0
        try:
            while not self._closed:
                try:
                    work = self._file_context_requests.get(timeout=5)
                except queue.Empty:
                    if sftp is not None and (
                        active_session is not self._session or time.monotonic() - last_used >= 30
                    ):
                        try:
                            sftp.close()
                        except Exception:
                            pass
                        sftp = None
                        active_session = None
                    continue
                if work is None:
                    return
                session, attempt, context, revision, generation = work
                # Background hints yield to command input/output and never own the PTY.
                while self._terminal_refresh_delay() > 0 and not self._closed and self._is_current(attempt, session):
                    threading.Event().wait(0.1)
                if self._closed:
                    return
                if not self._is_current(attempt, session):
                    continue
                if context.resolved_path is None and revision != self._directory_input_revision:
                    continue
                verified, error = None, ""
                try:
                    if active_session is not session or sftp is None:
                        if sftp is not None:
                            sftp.close()
                        sftp = session.open_isolated_sftp(allow_shared_fallback=False)
                        sftp.get_channel().settimeout(6)
                        active_session = session
                    verified = self._verify_terminal_context(session, context, sftp)
                    last_used = time.monotonic()
                except Exception as exc:
                    error = str(exc)
                    if sftp is not None:
                        try:
                            sftp.close()
                        except Exception:
                            pass
                    sftp = None
                    active_session = None
                self._queue_event(
                    "terminal_selection",
                    (attempt, session, context, revision, generation, verified, error),
                )
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass

    @staticmethod
    def _verify_terminal_context(
        session: InteractiveSSHSession, context: RemotePathContext, sftp: object,
    ) -> RemotePathContext:
        directory = None
        if context.resolved_path is None and not context.normalized_path.startswith(("/", "~")):
            pid = session.shell_process_id()
            if pid is not None:
                directory = sftp.readlink(f"/proc/{pid}/cwd")
        return RemotePathResolver.verify(context, sftp, directory)

    def _jump_terminal_directory(self, text: str, directory: str) -> None:
        session = self._session
        if not self.connected or session is None:
            return
        revision = self._directory_input_revision
        context = self._path_resolver.guess(text, directory or None)
        if context is not None:
            if context.resolved_path is None and not context.normalized_path.startswith(("/", "~")):
                QMessageBox.warning(self, tr("无法操作远程路径"), tr("未记录这段输出的目录，请使用完整路径"))
                return
            self._terminal_path_action("jump_directory", context, session, self._attempt, revision)

    def _terminal_path_action(
        self, action: str, context: RemotePathContext,
        session: InteractiveSSHSession, attempt: int, revision: int,
    ) -> None:
        if action == "copy_path":
            QApplication.clipboard().setText(context.resolved_path or context.normalized_path)
            return
        if not self._is_current(attempt, session) or not self.connected or self._terminal_path_action_running:
            return
        if context.resolved_path is None and revision != self._directory_input_revision:
            QMessageBox.warning(self, tr("未执行操作"), tr("终端目录已变化，请重新选择路径"))
            return
        self._terminal_path_action_running = True
        self.status_label.setText(tr("正在检查远程路径…"))
        threading.Thread(
            target=self._verify_terminal_path_action,
            args=(session, attempt, action, context, revision, self._path_resolver.generation),
            name="ssh-path-action", daemon=True,
        ).start()

    def _verify_terminal_path_action(
        self, session: InteractiveSSHSession, attempt: int, action: str,
        context: RemotePathContext, revision: int, generation: int,
    ) -> None:
        verified, error = None, ""
        try:
            sftp = session.open_isolated_sftp(allow_shared_fallback=False)
            try:
                sftp.get_channel().settimeout(8)
                verified = self._verify_terminal_context(session, context, sftp)
                if action == "jump_directory":
                    path = verified.resolved_path
                    if verified.verified_type is RemotePathType.DIRECTORY:
                        path = sftp.normalize(path)
                    else:
                        path = posixpath.join(sftp.normalize(posixpath.dirname(path) or "/"), posixpath.basename(path))
                    verified = replace(verified, resolved_path=path)
            finally:
                sftp.close()
        except FileNotFoundError:
            error = tr("远程路径已不存在：{0}", context.resolved_path or context.normalized_path)
        except Exception as exc:
            error = str(exc)
        self._queue_event(
            "terminal_path_action",
            (attempt, session, action, context, revision, generation, verified, error),
        )

    def _dispatch_terminal_path_action(self, action: str, context: RemotePathContext) -> None:
        path = context.resolved_path
        is_directory = context.verified_type is RemotePathType.DIRECTORY
        if action == "jump_directory":
            if not is_directory:
                path = posixpath.dirname(path) or "/"
            if self._execute_selected_command(f"cd -- {shlex.quote(path)}", hide_echo=True):
                self._terminal_directory_jump = (self._directory_input_revision, path)
                self._open_terminal_directory(path)
                self.output_text.scroll_to_bottom()
            return
        if action in {"open", "edit"} and is_directory:
            self._open_terminal_directory(path)
            return
        manager = self._ensure_file_manager()
        if action in {"open", "edit"}:
            manager._handle_path_action("edit", path)
        elif action == "download":
            manager._download_remote_items([(path, is_directory, posixpath.basename(path))])
        elif action == "rename":
            manager._rename_remote_path(path)
        elif action in {"copy", "cut"}:
            manager._copy_remote_paths([path], action == "cut")
        elif action == "delete":
            manager._delete_remote_paths([path], verified_directory=is_directory)
        elif action in {"paste", "new_file", "new_directory"}:
            if not is_directory:
                QMessageBox.warning(self, tr("无法操作远程路径"), tr("该操作需要选择文件夹"))
                return
            if action == "paste":
                manager._paste_remote_items(path)
            elif action == "new_file":
                manager._create_remote_file(path)
            else:
                manager._create_remote_directory(path)
        elif is_directory:
            QMessageBox.warning(self, tr("无法操作远程路径"), tr("该操作需要选择普通文件"))
        else:
            manager._handle_path_action(action, path)

    def _append(self, value: str) -> None:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        displayed = normalized
        if self._ip_hiding and self.parameters.ip_address:
            displayed = displayed.replace(
                self.parameters.ip_address, mask_ip_address(self.parameters.ip_address)
            )
        self._feed_terminal(
            normalized.replace("\n", "\r\n"), displayed.replace("\n", "\r\n")
        )

    def _feed_terminal(self, value: str, display_value: str | None = None) -> None:
        if not value:
            return
        self._last_terminal_output_at = time.monotonic()
        self._queue_terminal_log(value)
        self.output_text.write(self._filter_terminal_jump_echo(
            value if display_value is None else display_value
        ))

    def _filter_terminal_jump_echo(self, value: str) -> str:
        if self._terminal_jump_echo is None:
            return value
        self._terminal_jump_echo_buffer += value
        visible: list[str] = []
        while "\n" in self._terminal_jump_echo_buffer:
            echo, remaining = self._terminal_jump_echo_buffer.split("\n", 1)
            plain = self._terminal_echo_text(echo)
            command = self._terminal_jump_echo
            prefix = plain[:-len(command)] if plain.endswith(command) else None
            # Readline can ring the bell on Ctrl+U, redraw the prompt or insert
            # terminal title/control sequences before echoing the command.
            matches = prefix is not None and (
                not prefix.strip() or re.fullmatch(r"\[[^\r\n]*\][#$] *", prefix) is not None
            )
            if matches:
                self._terminal_jump_echo_timer.stop()
                self._terminal_jump_echo = None
                self._terminal_jump_echo_buffer = ""
                modes = "".join(re.findall(r"\x1b\[\?2004[hl]", echo))
                return "".join(visible) + modes + "\r\x1b[2K" + remaining
            # Preserve unrelated output without abandoning the expected echo.
            visible.append(echo + "\n")
            self._terminal_jump_echo_buffer = remaining
        if len(self._terminal_jump_echo_buffer) >= 16384:
            visible.append(self._terminal_jump_echo_buffer)
            self._terminal_jump_echo_buffer = ""
            self._terminal_jump_echo = None
            self._terminal_jump_echo_timer.stop()
        return "".join(visible)

    @staticmethod
    def _terminal_echo_text(value: str) -> str:
        value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", value)
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        return value.replace("\x07", "").replace("\r", "")

    def _flush_terminal_jump_echo(self) -> None:
        self._terminal_jump_echo_timer.stop()
        buffered = self._terminal_jump_echo_buffer
        self._terminal_jump_echo = None
        self._terminal_jump_echo_buffer = ""
        if buffered:
            self.output_text.write(buffered)

    def _terminal_refresh_delay(self) -> float:
        now = time.monotonic()
        return max(
            0.0,
            _TERMINAL_INPUT_IDLE_SECONDS - (now - self._last_terminal_input_at),
            _TERMINAL_OUTPUT_IDLE_SECONDS - (now - self._last_terminal_output_at),
        )

    def _can_render_remote_files(self) -> bool:
        return (
            self._file_panel_open and self.isVisible()
            and self._terminal_refresh_delay() <= 0
            and self._events.empty() and self._deferred_output_event is None
        )

    def _queue_terminal_log(self, value: str) -> None:
        self._terminal_log_chunks.append(value)
        if not self._terminal_log_timer.isActive():
            self._terminal_log_timer.start()

    def _flush_terminal_log(self) -> None:
        self._terminal_log_timer.stop()
        if not self._terminal_log_chunks:
            return
        chunks, self._terminal_log_chunks = self._terminal_log_chunks, []
        self._log_event("output", "".join(chunks))

    def _apply_terminal_resize(self) -> None:
        if self._closed or not self.output_text.isVisible():
            return
        self.output_text.fit()
        columns, rows = self.output_text.terminal_size()
        self._terminal_resized(columns, rows)

    def _terminal_resized(self, columns: int, rows: int) -> None:
        if self._closed or (columns, rows) == (
            self._terminal_columns,
            self._terminal_rows,
        ):
            return
        self._terminal_columns, self._terminal_rows = columns, rows
        if self._session is not None:
            try:
                self._session.resize_pty(columns, rows)
            except SSHSessionError as exc:
                self._append(f"[SSH] {exc}\n")
