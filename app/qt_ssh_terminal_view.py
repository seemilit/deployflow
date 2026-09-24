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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QTabBar,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from config import ServerParameters
from qt_xterm_terminal import XTermTerminal
from ssh_terminal import InteractiveSSHSession, SSHSessionError


StateCallback = Callable[["QtSSHTerminalTab"], None]
CloseCallback = Callable[["QtSSHTerminalTab"], None]
LogCallback = Callable[[str, str], None]
ToolModeCallback = Callable[[], None]
MonitorWidthCallback = Callable[[int], None]
_MAX_OUTPUT_CHARACTERS_PER_POLL = 16384
_EVENT_DRAIN_TIME_SLICE_SECONDS = 0.006
_TERMINAL_LOG_FLUSH_INTERVAL_MS = 200
_DIRECTORY_CACHE_TTL_SECONDS = 5.0
_DIRECTORY_CACHE_LIMIT = 120
_REMOTE_ITEM_BATCH_SIZE = 250
_REMOTE_FILE_MIME = "application/x-deployflow-remote-files"
_TERMINAL_INPUT_IDLE_SECONDS = 1.0
_TERMINAL_OUTPUT_IDLE_SECONDS = 0.6


@dataclass
class _RemoteClipboard:
    token: str
    server: str
    paths: tuple[str, ...]
    cut: bool
    in_flight: bool = False


def _add_remote_type_actions(
    menu: QMenu,
    path: str,
    is_directory: bool,
    callback: Callable[[str, str], None],
) -> None:
    actions = [("打开目录", "open")] if is_directory else [("打开文件编辑器", "edit")]
    if not is_directory:
        actions.append(("修改文件权限…", "chmod"))
    name = posixpath.basename(path).lower()
    if not is_directory and name.endswith((".sh", ".bash", ".zsh")):
        actions.extend([
            ("执行脚本", "script"),
        ])
    if not is_directory and re.search(r"\.(log|out)(?:[.-][\w.-]+)?$", name) and not name.endswith(
        (".gz", ".bz2", ".xz", ".zip")
    ):
        actions.extend([
            ("实时跟踪日志（tail -f）", "tail_follow"),
            ("查看末尾 N 行（tail -n）…", "tail_lines"),
            ("查询关键字（grep）…", "search"),
        ])
    for title, action in actions:
        menu.addAction(title).triggered.connect(
            lambda _checked=False, value=action: callback(value, path)
        )


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


class _DownloadTaskPanel(QFrame):
    cancel_requested = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._rows: dict[str, tuple[QWidget, QLabel, QProgressBar]] = {}
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
        self.title_label = QLabel("下载任务")
        self.title_label.setStyleSheet("font-weight:600;")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        hide_button = QPushButton("—")
        hide_button.setFixedSize(26, 22)
        hide_button.setToolTip("隐藏下载任务")
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

    def add_task(self, task_id: str, title: str) -> None:
        if task_id in self._rows:
            return
        row = QFrame()
        row.setObjectName("downloadTaskRow")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(4, 6, 4, 7)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        name_label = QLabel(title)
        name_label.setToolTip(title)
        heading.addWidget(name_label, 1)
        delete_button = QPushButton("×")
        delete_button.setFixedSize(24, 22)
        delete_button.setToolTip("删除任务并停止下载")
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
        status = QLabel("等待下载…")
        status.setStyleSheet("color:#64748b;")
        layout.addWidget(status)
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
        status.setText(f"正在下载：{name}（{percent}%）")

    def finish_task(self, task_id: str, succeeded: bool, message: str) -> None:
        values = self._rows.get(task_id)
        if values is None:
            return
        _row, status, progress = values
        if succeeded:
            progress.setValue(100)
            status.setText("下载完成")
            status.setStyleSheet("color:#15803d;")
        else:
            status.setText(message)
            status.setStyleSheet("color:#dc2626;")

    def remove_task(self, task_id: str) -> None:
        values = self._rows.pop(task_id, None)
        if values is None:
            return
        row, _status, _progress = values
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

    def _update_size(self) -> None:
        count = len(self._rows)
        self.title_label.setText(f"下载任务（{count}）")
        self.setFixedHeight(min(330, 42 + max(1, count) * 78))


class _RemoteFileTree(QTreeWidget):
    files_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, event: QEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QEvent) -> None:
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
    operation_finished = Signal(str, bool, str)
    download_progress = Signal(str, str, int, int)
    download_finished = Signal(str, bool, str, bool)
    clipboard_finished = Signal(object, object)
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
        self.setWindowTitle(f"远程文件编辑 — {path}[*]")
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
        self.status = QLabel("正在读取…")
        footer.addWidget(self.status, 1)
        self.save_button = QPushButton("保存 (Ctrl+S)")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save)
        footer.addWidget(self.save_button)
        close_button = QPushButton("关闭")
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
            raise SSHSessionError("编辑器仅支持 4 MB 以内的文本文件，请下载后编辑。")
        with sftp.open(path, "rb") as remote:
            prefetch = getattr(remote, "prefetch", None)
            if callable(prefetch) and file_size:
                try:
                    prefetch(file_size, max_concurrent_requests=16)
                except TypeError:
                    prefetch(file_size)
            data = remote.read(self._MAX_BYTES + 1)
        if len(data) > self._MAX_BYTES:
            raise SSHSessionError("文件过大，请下载后编辑。")
        return data

    def _load_worker(self) -> None:
        sftp = None
        try:
            # A new SFTP channel on the active SSH transport needs no second
            # TCP connection or authentication, so the editor opens promptly.
            sftp = self._session.open_isolated_sftp()
            sftp.get_channel().settimeout(20)
            path = sftp.normalize(self._path)
            data = self._read_bytes(sftp, path)
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                encoding = "utf-16"
            elif data.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            else:
                encoding = "utf-8"
            value = data.decode(encoding)
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
                raise SSHSessionError("该文件不是可编辑的文本文件。")
            self._events.loaded.emit((path, data, value, encoding))
        except Exception as exc:
            self._events.failed.emit(f"读取失败：{exc}")
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
        self.status.setText(f"{self._encoding} · 修改后保存到服务器")

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
            QMessageBox.warning(self, "无法保存", "内容超过 4 MB，请下载后编辑。")
            self._close_after_save = False
            return
        self._busy = True
        self.editor.setReadOnly(True)
        self.save_button.setEnabled(False)
        self.status.setText("正在保存…")
        threading.Thread(target=self._save_worker, args=(data,), name="sftp-edit-save", daemon=True).start()

    def _save_worker(self, data: bytes) -> None:
        sftp = None
        temporary = None
        try:
            sftp = self._session.open_isolated_sftp()
            sftp.get_channel().settimeout(20)
            attributes = sftp.stat(self._path)
            if self._read_bytes(sftp, self._path, attributes) != self._original:
                raise SSHSessionError("服务器文件已被其他操作修改，本次未覆盖。请保留当前内容，重新打开文件后处理。")
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
                raise SSHSessionError("保存期间服务器文件发生变化，本次未覆盖。")
            # Do not truncate the original if writing or atomic replacement fails.
            sftp.posix_rename(temporary, self._path)
            temporary = None
            self._events.saved.emit(data)
        except Exception as exc:
            self._events.failed.emit(f"保存失败：{exc}")
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
        self.status.setText("已保存到服务器")
        self.file_saved.emit(self._path)
        if self._close_after_save:
            self.close()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._close_after_save = False
        self.editor.setReadOnly(not self._loaded)
        self.save_button.setEnabled(self._loaded)
        self.status.setText(message)
        QMessageBox.warning(self, "远程文件编辑", message)

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event: QEvent) -> None:
        if self._busy:
            self.raise_()
            QMessageBox.information(self, "正在处理", "正在读取或保存文件，请稍候再关闭。")
            event.ignore()
            return
        if self.editor.document().isModified():
            self.show()
            self.raise_()
            answer = QMessageBox.question(
                self, "尚未保存", "是否将修改保存到服务器？",
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
        self._log_reader = log_reader
        self._displayed_log_text = ""
        self._download_task_panel = download_task_panel
        self._loading = False
        self._busy = False
        self._pending_remote_directory: str | None = None
        self._editors: dict[str, _RemoteFileEditor] = {}
        self._directory_history: list[str] = []
        self._tree_loading: set[str] = set()
        self._tree_loaded: set[str] = set()
        self._file_cache: dict[str, list[tuple[object, ...]]] = {}
        self._file_types: dict[str, dict[str, bool]] = {}
        self._file_cache_times: dict[str, float] = {}
        self._directory_cache: dict[str, list[str]] = {}
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
        self._download_tasks_lock = threading.Lock()
        self._download_slots = threading.Semaphore(3)
        self._last_local_directory = str(Path.home())
        self._events = _TransferEvents(self)
        self._events.remote_loaded.connect(self._display_remote_files)
        self._events.remote_error.connect(self._display_remote_error)
        self._events.directories_loaded.connect(self._display_tree_directories)
        self._events.transfer_progress.connect(self._update_transfer_progress)
        self._events.operation_finished.connect(self._finish_operation)
        self._events.download_progress.connect(self._update_download_task)
        self._events.download_finished.connect(self._finish_download_task)
        self._events.clipboard_finished.connect(self._finish_clipboard)
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
        self.setWindowTitle("SFTP 文件传输")
        if not embedded:
            self.resize(1080, 680)
        self._create_widgets(initial_remote_directory)
        self._refresh_remote_directory()

    def set_session(self, session: InteractiveSSHSession) -> None:
        if session is self._session:
            return
        self._render_defer_timer.stop()
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
        self._reset_directory_tree()

    def refresh(self) -> None:
        self._refresh_remote_directory()

    def open_directory(self, directory: str) -> None:
        if self._loading or self._busy:
            self._pending_remote_directory = directory
            return
        self.remote_directory_entry.setText(directory)
        self._refresh_remote_directory()

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
        raise SSHSessionError("无法读取服务器目录")

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
        self.file_tabs.addTab("文件")
        self.file_tabs.addTab("日志")
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
        self.remote_directory_entry = QLineEdit(initial_remote_directory or "/")
        self.remote_directory_entry.setFrame(False)
        self.remote_directory_entry.returnPressed.connect(self._refresh_remote_directory)
        remote_path_row.addWidget(self.remote_directory_entry, 1)
        self.history_button = QPushButton("历史")
        self.history_button.clicked.connect(self._show_directory_history)
        remote_path_row.addWidget(self.history_button)
        self.remote_directory_entry.setPlaceholderText("服务器目录（回车打开）")
        remote_path_row.insertWidget(0, QLabel("服务器："))

        splitter = QSplitter(Qt.Horizontal)
        self.directory_tree = QTreeWidget(self)
        self.directory_tree.setHeaderHidden(True)
        self.directory_tree.setMinimumWidth(150)
        self.directory_tree.setMaximumWidth(360)
        self.directory_tree.itemExpanded.connect(self._directory_tree_expanded)
        self.directory_tree.itemClicked.connect(self._directory_tree_clicked)
        self.directory_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.directory_tree.customContextMenuRequested.connect(self._show_directory_context_menu)
        for shortcut, handler in (
            (QKeySequence.Copy, lambda: self._directory_path_action("copy")),
            (QKeySequence.Cut, lambda: self._directory_path_action("cut")),
            (QKeySequence.Paste, lambda: self._directory_path_action("paste")),
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
            self.directory_tree.hide()
            self._create_local_browser(splitter)

        self.remote_tree = _RemoteFileTree()
        self.remote_tree.setHeaderLabels(
            ["文件名", "大小", "类型", "修改时间", "权限", "用户/用户组"]
        )
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setAlternatingRowColors(True)
        self.remote_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_tree.setSortingEnabled(True)
        self.remote_tree.itemDoubleClicked.connect(self._remote_item_activated)
        self.remote_tree.files_dropped.connect(self._upload_paths)
        self.remote_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self._show_remote_context_menu)
        for shortcut, handler in (
            (QKeySequence.Copy, lambda: self._copy_remote_items(False)),
            (QKeySequence.Cut, lambda: self._copy_remote_items(True)),
            (QKeySequence.Paste, self._paste_remote_items),
            (QKeySequence.Delete, self._delete_remote_items),
            (QKeySequence.SelectAll, self.remote_tree.selectAll),
        ):
            action = QAction(self.remote_tree)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _checked=False, call=handler: call())
            self.remote_tree.addAction(action)
        if self._embedded:
            splitter.addWidget(self.remote_tree)
        else:
            remote_panel = QWidget()
            remote_layout = QVBoxLayout(remote_panel)
            remote_layout.setContentsMargins(0, 0, 0, 0)
            remote_layout.addLayout(remote_path_row)
            remote_layout.addWidget(self.remote_tree, 1)
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
        self.status_label = QLabel("准备就绪")
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        if not self._embedded:
            close_button = QPushButton("关闭")
            close_button.clicked.connect(self.close)
            footer.addWidget(close_button)
        file_root.addLayout(footer)

        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(6, 6, 6, 6)
        log_layout.setSpacing(4)
        self.log_title = QLabel(
            f"本次连接日志 · {datetime.now().strftime('%Y-%m-%d')}"
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
        self._tree_items = {"/": root_item}

    def _create_local_browser(self, splitter: QSplitter) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("本地："))
        self.local_directory_entry = QLineEdit(self._last_local_directory)
        self.local_directory_entry.returnPressed.connect(self._open_local_directory)
        path_row.addWidget(self.local_directory_entry, 1)
        parent_button = QPushButton("上级")
        parent_button.clicked.connect(self._local_parent)
        path_row.addWidget(parent_button)
        choose_button = QPushButton("选择")
        choose_button.clicked.connect(self._choose_local_directory)
        path_row.addWidget(choose_button)
        layout.addLayout(path_row)
        self.local_model = QFileSystemModel(self)
        self.local_model.setReadOnly(True)
        self.local_model.setRootPath(self._last_local_directory)
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(self._last_local_directory))
        self.local_tree.setRootIsDecorated(False)
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_tree.setDragEnabled(True)
        self.local_tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.local_tree.setDefaultDropAction(Qt.CopyAction)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.setColumnWidth(0, 220)
        self.local_tree.doubleClicked.connect(self._local_item_activated)
        self.local_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self._show_local_context_menu)
        layout.addWidget(self.local_tree, 1)
        self.local_upload_button = QPushButton("上传选中项 →")
        self.local_upload_button.clicked.connect(self._upload_local_selection)
        layout.addWidget(self.local_upload_button)
        splitter.addWidget(panel)

    def _open_local_directory(self) -> None:
        path = Path(self.local_directory_entry.text()).expanduser()
        if not path.is_dir():
            QMessageBox.warning(self, "目录不存在", "请选择有效的本地文件夹。")
            return
        self._last_local_directory = str(path.resolve())
        self.local_directory_entry.setText(self._last_local_directory)
        self.local_tree.setRootIndex(self.local_model.setRootPath(self._last_local_directory))

    def _local_parent(self) -> None:
        self.local_directory_entry.setText(str(Path(self.local_directory_entry.text()).parent))
        self._open_local_directory()

    def _choose_local_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择本地目录", self._last_local_directory)
        if path:
            self.local_directory_entry.setText(path)
            self._open_local_directory()

    def _local_item_activated(self, index: object) -> None:
        if self.local_model.isDir(index):
            self.local_directory_entry.setText(self.local_model.filePath(index))
            self._open_local_directory()
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
        upload = menu.addAction("上传选中项到右侧目录")
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
            content = "当前连接暂无可读取的日志。"
        else:
            try:
                content = self._log_reader()
            except (OSError, UnicodeError) as exc:
                content = f"读取日志失败：{exc}"
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
                self, "选择要上传的文件", self._last_local_directory
            )
            if files:
                self._last_local_directory = str(Path(files[0]).parent)
                self._upload_paths([Path(path) for path in files])
        else:
            folder = QFileDialog.getExistingDirectory(
                self, "选择要上传的文件夹", self._last_local_directory
            )
            if folder:
                self._last_local_directory = str(Path(folder).parent)
                self._upload_paths([Path(folder)])

    def _go_remote_parent(self) -> None:
        current = self.remote_directory_entry.text().strip() or "/"
        self.remote_directory_entry.setText(posixpath.dirname(posixpath.normpath(current)) or "/")
        self._refresh_remote_directory()

    def _show_directory_history(self) -> None:
        menu = QMenu(self)
        if not self._directory_history:
            empty_action = menu.addAction("暂无历史目录")
            empty_action.setEnabled(False)
        actions: dict[object, str] = {}
        for directory in reversed(self._directory_history[-20:]):
            action = menu.addAction(directory)
            actions[action] = directory
        selected = menu.exec(
            self.history_button.mapToGlobal(QPoint(0, self.history_button.height()))
        )
        if selected in actions:
            self.remote_directory_entry.setText(actions[selected])
            self._refresh_remote_directory()

    def _directory_tree_expanded(self, item: QTreeWidgetItem) -> None:
        directory = item.data(0, Qt.UserRole)
        if isinstance(directory, str):
            self._request_tree_directory(directory)

    def _directory_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        directory = item.data(0, Qt.UserRole)
        if not isinstance(directory, str):
            return
        self.remote_directory_entry.setText(directory)
        self._refresh_remote_directory()

    def _request_tree_directory(self, directory: str, force: bool = False) -> None:
        directory = posixpath.normpath(directory or "/")
        if force:
            self._directory_cache.pop(directory, None)
            self._tree_loaded.discard(directory)
        if directory in self._tree_loaded:
            return
        cached = self._directory_cache.get(directory)
        if cached is not None:
            self._display_tree_directories(directory, (list(cached), ""))
            return
        if directory in self._tree_loading:
            return
        self._tree_loading.add(directory)
        threading.Thread(
            target=self._load_tree_directory_worker,
            args=(directory,),
            name="sftp-directory-tree",
            daemon=True,
        ).start()

    def _load_tree_directory_worker(self, directory: str) -> None:
        try:
            directories = self._tree_browse_call(
                lambda sftp: sorted(
                    attribute.filename
                    for attribute in self._list_directory_attributes(sftp, directory)
                    if stat.S_ISDIR(attribute.st_mode or 0)
                )
            )
        except Exception as exc:
            self._events.directories_loaded.emit(directory, ([], str(exc)))
        else:
            self._events.directories_loaded.emit(directory, (directories, ""))

    def _display_tree_directories(self, directory: str, payload: object) -> None:
        self._tree_loading.discard(directory)
        directories, error = payload
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
        self._locate_directory_tree_path(
            self.remote_directory_entry.text().strip() or "/"
        )

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
        for index in range(item.childCount()):
            self._remove_tree_item_index(item.child(index))

    def _select_directory_tree_path(self, directory: str) -> None:
        item = self._find_directory_tree_item(posixpath.normpath(directory or "/"))
        if item is not None:
            self.directory_tree.setCurrentItem(item)

    def _remote_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        remote_path = str(item.data(0, Qt.UserRole))
        if bool(item.data(0, Qt.UserRole + 1)):
            self.remote_directory_entry.setText(remote_path)
            self._refresh_remote_directory()

    def _force_refresh_remote_directory(self) -> None:
        self._refresh_remote_directory(force=True)

    def _refresh_remote_directory(self, force: bool = False) -> None:
        directory = posixpath.normpath(
            self.remote_directory_entry.text().strip() or "/"
        )
        if force:
            self._invalidate_directory(directory)
        cached = self._file_cache.get(directory)
        if cached is not None:
            if directory != self._displayed_directory:
                self._render_remote_files(directory, list(cached))
            else:
                self.remote_directory_entry.setText(directory)
                self.status_label.setText(f"服务器目录：{len(cached)} 项")
            cache_age = time.monotonic() - self._file_cache_times.get(directory, 0.0)
            if not force and cache_age < _DIRECTORY_CACHE_TTL_SECONDS:
                return
        if self._loading or self._busy:
            self._pending_remote_directory = directory
            if cached is None:
                self.status_label.setText("等待读取服务器目录…")
            else:
                self.status_label.setText(f"已显示缓存：{len(cached)} 项，等待刷新…")
            return
        self._loading = True
        self._browse_request_id += 1
        request_id = self._browse_request_id
        self._active_browse_request = request_id
        self.status_label.setText(
            f"已显示缓存：{len(cached)} 项，正在刷新…"
            if cached is not None else "正在读取服务器目录…"
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
        current = posixpath.normpath(self.remote_directory_entry.text().strip() or "/")
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

    def cached_path_type(self, path: str) -> bool | None:
        path = posixpath.normpath(path)
        if path in self._file_types:
            return True
        return self._file_types.get(posixpath.dirname(path), {}).get(posixpath.basename(path))

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
        self.remote_directory_entry.setText(directory)
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
                "文件夹" if is_directory else "文件",
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
        current = posixpath.normpath(self.remote_directory_entry.text().strip() or "/")
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
                f"正在显示服务器目录：{end}/{len(items)} 项"
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
        if self._embedded:
            self._select_directory_tree_path(directory)
            QTimer.singleShot(
                0, lambda value=directory: self._locate_directory_tree_path(value)
            )
        self.status_label.setText(f"服务器目录：{len(items)} 项")

    def _invalidate_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory or "/")
        if self._deferred_render is not None and (
            self._deferred_render[0] == directory
            or self._deferred_render[0].startswith(directory.rstrip("/") + "/")
        ):
            self._render_defer_timer.stop()
            self._deferred_render = None
        affected = {directory} | {
            path for path in self._file_cache
            if path.startswith(directory.rstrip("/") + "/")
        }
        for path in affected:
            self._file_cache.pop(path, None)
            self._file_types.pop(path, None)
            self._file_cache_times.pop(path, None)
            self._directory_cache.pop(path, None)
            self._tree_loaded.discard(path)

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
        self.status_label.setText("读取失败")
        QMessageBox.critical(self, "无法读取服务器目录", message)
        QTimer.singleShot(0, self._open_pending_directory)

    def _upload_paths(self, paths: object) -> None:
        sources = [Path(path) for path in paths if Path(path).is_file() or Path(path).is_dir()]
        if not sources or self._busy:
            return
        remote_directory = self.remote_directory_entry.text().strip()
        if not remote_directory:
            QMessageBox.warning(self, "未填写目录", "请填写服务器目标目录")
            return
        self._operation_refresh_directory = posixpath.normpath(remote_directory)
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText("准备上传…")
        threading.Thread(
            target=self._upload_worker,
            args=(sources, remote_directory),
            name="sftp-upload",
            daemon=True,
        ).start()

    def _upload_worker(self, sources: list[Path], remote_directory: str) -> None:
        try:
            sftp = self._session.open_isolated_sftp()
            try:
                self._ensure_remote_directory(sftp, remote_directory)
                files: list[tuple[Path, str]] = []
                for source in sources:
                    if source.is_file():
                        files.append((source, posixpath.join(remote_directory, source.name)))
                        continue
                    target_root = posixpath.join(remote_directory, source.name)
                    for root, directories, names in os.walk(source):
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
                    file_size = local_file.stat().st_size

                    def progress(current: int, _file_total: int) -> None:
                        nonlocal last_percent
                        total_current = completed + current
                        percent = 100 if total <= 0 else int(total_current * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            self._events.transfer_progress.emit(local_file.name, total_current, total)

                    sftp.put(str(local_file), remote_file, callback=progress, confirm=True)
                    completed += file_size
                    self._events.transfer_progress.emit(local_file.name, completed, total)
            finally:
                sftp.close()
        except Exception as exc:
            self._events.operation_finished.emit("上传", False, str(exc))
        else:
            self._events.operation_finished.emit("上传", True, "上传完成")

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
        directory = self._displayed_directory or self.remote_directory_entry.text().strip() or "/"
        if len(entries) == 1:
            path, is_directory, _name = entries[0]
            _add_remote_type_actions(menu, path, is_directory, self._handle_path_action)
            if is_directory:
                paste_into = menu.addAction("粘贴到此文件夹")
                paste_into.setEnabled(self._remote_clipboard() is not None)
                paste_into.triggered.connect(
                    lambda _checked=False: self._paste_remote_items(path)
                )
            menu.addSeparator()
        if entries:
            menu.addAction("复制\tCtrl+C").triggered.connect(
                lambda: self._copy_remote_paths(paths, False)
            )
            menu.addAction("剪切\tCtrl+X").triggered.connect(
                lambda: self._copy_remote_paths(paths, True)
            )
            menu.addAction("下载选中项").triggered.connect(self._download_selected_remote_items)
            rename_action = menu.addAction("重命名")
            rename_action.setEnabled(len(entries) == 1)
            rename_action.triggered.connect(self._rename_remote_item)
            menu.addAction("删除\tDelete").triggered.connect(
                lambda: self._delete_remote_paths(paths)
            )
            menu.addSeparator()
        paste_action = menu.addAction("粘贴到当前目录\tCtrl+V")
        paste_action.setEnabled(self._remote_clipboard() is not None)
        paste_action.triggered.connect(lambda: self._paste_remote_items(directory))
        menu.addAction("新建文件…").triggered.connect(lambda: self._create_remote_file(directory))
        menu.addAction("新建文件夹…").triggered.connect(lambda: self._create_remote_directory(directory))
        menu.addSeparator()
        menu.addAction("上传文件…").triggered.connect(lambda: self._select_upload())
        menu.addAction("上传文件夹…").triggered.connect(lambda: self._select_upload(folder=True))
        menu.addAction("返回上级目录").triggered.connect(self._go_remote_parent)
        menu.addAction("刷新").triggered.connect(self._force_refresh_remote_directory)
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
            for title, action in (("打开目录", "open"), ("复制", "copy"), ("剪切", "cut"), ("删除…", "delete")):
                entry = menu.addAction(title)
                entry.setEnabled(action == "open" or bool(path.strip("/")))
                entry.triggered.connect(
                    lambda _checked=False, value=action: self._directory_path_action(value, path)
                )
            menu.addSeparator()
        paste_action = menu.addAction("粘贴到此目录")
        paste_action.setEnabled(self._remote_clipboard() is not None)
        paste_action.triggered.connect(lambda: self._paste_remote_items(path))
        menu.addAction("新建文件…").triggered.connect(lambda: self._create_remote_file(path))
        menu.addAction("新建文件夹…").triggered.connect(lambda: self._create_remote_directory(path))
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
        if action in {"script", "tail_follow", "tail_lines", "search"} and any(
            ord(char) < 32 or ord(char) == 127 for char in path
        ):
            QMessageBox.warning(self, "无法执行", "文件路径包含控制字符，不能发送到交互终端")
            return
        quoted = shlex.quote(path)
        if action == "open":
            self.open_directory(path)
        elif action == "edit":
            self._edit_remote_path(path)
        elif action == "chmod":
            self._edit_remote_permissions(path)
        elif action == "script":
            shell = "zsh" if path.lower().endswith(".zsh") else "bash"
            self.command_requested.emit(
                f"cd -- {shlex.quote(posixpath.dirname(path))} && {shell} -- {quoted}"
            )
        elif action == "tail_follow":
            self.command_requested.emit(f"tail -f -- {quoted}")
        elif action == "tail_lines":
            count, accepted = QInputDialog.getInt(
                self, "查看日志", "显示末尾多少行：", 100, 1, 1000000,
            )
            if accepted and self._session is session and session.connected:
                self.command_requested.emit(f"tail -n {count} -- {quoted}")
        elif action == "search":
            keyword, accepted = QInputDialog.getText(self, "查询日志", "关键字（按原文匹配）：")
            if accepted and keyword and self._session is session and session.connected:
                if any(ord(char) < 32 or ord(char) == 127 for char in keyword):
                    QMessageBox.warning(self, "关键字无效", "关键字不能包含换行或控制字符")
                    return
                self.command_requested.emit(f"grep -nF -- {shlex.quote(keyword)} {quoted}")

    def _remote_clipboard(self) -> _RemoteClipboard | None:
        value = SftpTransferDialog._clipboard
        mime = QApplication.clipboard().mimeData()
        if (
            value is not None and value.paths and not value.in_flight
            and value.server == self._server_key and mime is not None
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
            QMessageBox.warning(self, "无法复制或剪切", "请选择具体的文件或文件夹，不能选择服务器根目录")
            return
        value = _RemoteClipboard(uuid.uuid4().hex, self._server_key, tuple(paths), cut)
        SftpTransferDialog._clipboard = value
        mime = QMimeData()
        mime.setData(_REMOTE_FILE_MIME, value.token.encode("ascii"))
        mime.setText("\n".join(paths))
        QApplication.clipboard().setMimeData(mime)
        self.status_label.setText(f"已{'剪切' if cut else '复制'} {len(paths)} 项，请进入目标目录后粘贴")

    def _paste_remote_items(self, directory: str | None = None) -> None:
        if self._busy or not self._session.connected:
            return
        value = self._remote_clipboard()
        if value is None:
            return
        directory = directory or self._displayed_directory or self.remote_directory_entry.text().strip() or "/"
        value.in_flight = True
        started = self._start_simple_operation(
            "移动" if value.cut else "复制", self._paste_worker,
            self._session, value, directory,
            affected_directories={directory, *(posixpath.dirname(path) for path in value.paths)},
        )
        if started:
            self.progress.setRange(0, 0)
        else:
            value.in_flight = False

    def _paste_worker(
        self, operation: str, session: InteractiveSSHSession,
        clipboard: _RemoteClipboard, directory: str,
    ) -> None:
        completed: list[str] = []

        def paste_all(sftp: object) -> None:
            destination = sftp.normalize(directory)
            if not stat.S_ISDIR(sftp.stat(destination).st_mode):
                raise SSHSessionError("粘贴目标必须是文件夹")
            targets: list[tuple[str, str]] = []
            target_names: set[str] = set()
            # Check the entire selection before changing any files.
            for source in clipboard.paths:
                source = posixpath.normpath(source)
                if not source.startswith("/") or not source.strip("/"):
                    raise SSHSessionError("不能复制或移动服务器根目录")
                attributes = sftp.lstat(source)
                canonical = posixpath.join(
                    sftp.normalize(posixpath.dirname(source)), posixpath.basename(source)
                )
                target = posixpath.join(destination, posixpath.basename(source))
                if target == canonical or (
                    stat.S_ISDIR(attributes.st_mode)
                    and (destination == canonical or destination.startswith(canonical.rstrip("/") + "/"))
                ):
                    raise SSHSessionError("不能粘贴到原位置，也不能把文件夹粘贴进它自己的子目录")
                if target in target_names:
                    raise SSHSessionError("所选项目中有同名文件，请分别粘贴")
                target_names.add(target)
                try:
                    sftp.lstat(target)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                else:
                    raise SSHSessionError(f"目标已存在，未覆盖：{target}")
                targets.append((source, target))
            for source, target in targets:
                if clipboard.cut:
                    # SFTP rename preserves the source if moving fails.
                    try:
                        sftp.rename(source, target)
                    except OSError as exc:
                        raise SSHSessionError(
                            f"无法移动 {source}，源文件未删除。请检查权限、同名文件，"
                            f"以及目标是否跨文件系统：{exc}"
                        ) from exc
                else:
                    staging = posixpath.join(destination, f".deployflow-copy-{uuid.uuid4().hex}")
                    sftp.mkdir(staging, 0o700)
                    try:
                        payload = posixpath.join(staging, "content")
                        session.copy_remote_path(source, payload)
                        sftp.rename(payload, target)
                    finally:
                        self._remove_remote_path(sftp, staging)
                completed.append(source)

        def finish_clipboard() -> None:
            self._events.clipboard_finished.emit(clipboard, completed)

        self._run_simple_operation(operation, paste_all, session, finish_clipboard)

    def _finish_clipboard(self, clipboard: _RemoteClipboard, completed: object) -> None:
        clipboard.in_flight = False
        if not clipboard.cut:
            return
        clipboard.paths = tuple(path for path in clipboard.paths if path not in completed)
        mime = QApplication.clipboard().mimeData()
        if (
            SftpTransferDialog._clipboard is clipboard and not clipboard.paths
            and mime is not None
            and bytes(mime.data(_REMOTE_FILE_MIME)) == clipboard.token.encode("ascii")
        ):
            SftpTransferDialog._clipboard = None
            QApplication.clipboard().clear()

    def _download_selected_remote_items(self) -> None:
        entries = self._selected_remote_items()
        if not entries or self._busy:
            if not entries:
                QMessageBox.warning(self, "未选择文件", "请先选择要下载的服务器文件或文件夹")
            return
        destination = QFileDialog.getExistingDirectory(
            self,
            "选择下载保存目录",
            self._last_local_directory,
        )
        if not destination:
            return
        target_directory = Path(destination)
        self._last_local_directory = destination
        conflicts = [
            name for _remote_path, _is_directory, name in entries
            if (target_directory / name).exists()
        ]
        if conflicts and QMessageBox.question(
            self,
            "确认覆盖",
            "本地存在同名内容，下载后将覆盖其中的同名文件。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        task_id = str(time.time_ns())
        title = entries[0][2] if len(entries) == 1 else f"{entries[0][2]} 等 {len(entries)} 项"
        task = _DownloadTask(task_id=task_id, title=title)
        with self._download_tasks_lock:
            self._download_tasks[task_id] = task
        if self._download_task_panel is not None:
            self._download_task_panel.add_task(task_id, title)
        self.status_label.setText(f"已创建下载任务：{title}")
        threading.Thread(
            target=self._download_worker,
            args=(task, entries, target_directory),
            name=f"sftp-download-{task_id}",
            daemon=True,
        ).start()

    def _download_worker(
        self,
        task: _DownloadTask,
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
            sftp = self._session.open_isolated_sftp()
            with task.sftp_lock:
                task.sftp = sftp
            try:
                if task.cancel_event.is_set():
                    raise _DownloadCancelled()
                files: list[tuple[str, Path, int]] = []
                for remote_path, is_directory, name in entries:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
                    local_path = destination / name
                    if is_directory:
                        self._collect_remote_files(
                            sftp,
                            remote_path,
                            local_path,
                            files,
                            task.cancel_event,
                        )
                    else:
                        size = int(sftp.stat(remote_path).st_size)
                        files.append((remote_path, local_path, size))
                total = sum(size for _remote, _local, size in files)
                completed = 0
                last_percent = -1
                for remote_file, local_file, file_size in files:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled()
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
                task.task_id, False, "下载已取消", True
            )
        except Exception as exc:
            if task.cancel_event.is_set():
                self._events.download_finished.emit(
                    task.task_id, False, "下载已取消", True
                )
            else:
                self._events.download_finished.emit(
                    task.task_id, False, f"下载失败：{exc}", False
                )
        else:
            self._events.download_finished.emit(
                task.task_id, True, "下载完成", False
            )
        finally:
            if slot_acquired:
                self._download_slots.release()
            for partial_file in partial_files:
                try:
                    partial_file.unlink(missing_ok=True)
                except OSError:
                    pass

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
            remote_path = posixpath.join(remote_directory, attribute.filename)
            local_path = local_directory / attribute.filename
            if stat.S_ISDIR(attribute.st_mode or 0):
                cls._collect_remote_files(
                    sftp,
                    remote_path,
                    local_path,
                    files,
                    cancel_event,
                )
            else:
                files.append((remote_path, local_path, int(attribute.st_size or 0)))

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
        directory = directory or self._displayed_directory or self.remote_directory_entry.text().strip() or "/"
        name, accepted = QInputDialog.getText(self, "新建文件", "文件名称（例如 application.conf）：")
        if not accepted or self._session is not session or not session.connected:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, "名称无效", "请输入有效的文件名称，不能包含路径分隔符或控制字符")
            return
        remote_path = posixpath.join(directory, name)
        self._start_simple_operation(
            "新建文件", self._create_file_worker, remote_path,
            affected_directories={directory},
        )

    def _create_remote_directory(self, directory: str | None = None) -> None:
        if self._busy or not self._session.connected:
            return
        session = self._session
        directory = directory or self._displayed_directory or self.remote_directory_entry.text().strip() or "/"
        name, accepted = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if not accepted or self._session is not session or not session.connected:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, "名称无效", "名称不能为空，也不能包含 /、\\ 或使用 .、..")
            return
        remote_path = posixpath.join(directory, name)
        self._start_simple_operation(
            "新建文件夹", self._mkdir_worker, remote_path,
            affected_directories={posixpath.dirname(remote_path)},
        )

    def _rename_remote_item(self) -> None:
        entries = self._selected_remote_items()
        if self._busy:
            return
        if len(entries) != 1:
            QMessageBox.warning(self, "无法重命名", "请只选择一个文件或文件夹")
            return
        source, _is_directory, old_name = entries[0]
        name, accepted = QInputDialog.getText(
            self, "重命名", "新名称：", text=old_name
        )
        if not accepted:
            return
        name = name.strip()
        if not self._valid_remote_name(name):
            QMessageBox.warning(self, "名称无效", "名称不能为空，也不能包含 /、\\ 或使用 .、..")
            return
        if name == old_name:
            return
        target = posixpath.join(posixpath.dirname(source), name)
        self._start_simple_operation("重命名", self._rename_worker, source, target)

    def _delete_remote_items(self) -> None:
        entries = self._selected_remote_items()
        self._delete_remote_paths([path for path, _is_directory, _name in entries])

    def _delete_remote_paths(self, paths: list[str]) -> None:
        if not paths or self._busy or not self._session.connected:
            return
        session = self._session
        paths = list(dict.fromkeys(posixpath.normpath(path) for path in paths))
        if any(not path.startswith("/") or not path.strip("/") for path in paths):
            QMessageBox.warning(self, "无法删除", "不能删除服务器根目录，请选择具体的文件或文件夹")
            return
        names = "\n".join(f"• {path}" for path in paths[:8])
        if len(paths) > 8:
            names += f"\n……共 {len(paths)} 项"
        if QMessageBox.warning(
            self,
            "确认删除",
            f"将永久删除服务器上的以下内容，文件夹会连同内部内容一起删除：\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if self._session is not session or not session.connected:
            return
        self._start_simple_operation(
            "删除", self._delete_worker, paths,
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
        self._operation_refresh_directory = posixpath.normpath(
            self.remote_directory_entry.text().strip() or "/"
        )
        self._operation_affected_directories = {
            posixpath.normpath(path) for path in (affected_directories or set())
        }
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText(f"正在{operation}…")
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
                    raise SSHSessionError("请选择普通文件")
            finally:
                sftp.close()
        except Exception as exc:
            error = str(exc) or "读取文件权限失败"
        self._events.permissions_loaded.emit(session, path, mode, error)

    def _show_permissions_dialog(
        self, session: InteractiveSSHSession, path: str, mode: int, error: str,
    ) -> None:
        if session is not self._session or not session.connected or self._busy:
            return
        if error:
            QMessageBox.warning(self, "读取权限失败", error)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("修改文件权限")
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
        for title, shift in (("所有者", 6), ("组", 3), ("其他", 0)):
            group = QGroupBox(title)
            row = QHBoxLayout(group)
            for label, value in (("读取", 4), ("写入", 2), ("执行", 1)):
                bit = value << shift
                checkbox = QCheckBox(label)
                checkbox.setChecked(bool(mode & bit))
                checkboxes.append((checkbox, bit))
                row.addWidget(checkbox)
            layout.addWidget(group)
        buttons = QHBoxLayout()
        accept_button = QPushButton("确定")
        accept_button.setDefault(True)
        accept_button.clicked.connect(dialog.accept)
        cancel_button = QPushButton("取消")
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
            "修改文件权限", self._chmod_worker, path, permissions,
            affected_directories={posixpath.dirname(path)},
        )

    def _chmod_worker(self, operation: str, remote_path: str, permissions: int) -> None:
        def set_permissions(sftp: object) -> None:
            attributes = sftp.stat(remote_path)
            if not stat.S_ISREG(attributes.st_mode):
                raise SSHSessionError("只能修改普通文件的权限")
            sftp.chmod(remote_path, (stat.S_IMODE(attributes.st_mode) & 0o7000) | (permissions & 0o777))

        self._run_simple_operation(operation, set_permissions)

    def _rename_worker(self, operation: str, source: str, target: str) -> None:
        self._run_simple_operation(operation, lambda sftp: sftp.rename(source, target))

    def _delete_worker(self, operation: str, paths: list[str]) -> None:
        def remove_all(sftp: object) -> None:
            for path in paths:
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
            succeeded, message = False, str(exc)
        else:
            succeeded, message = True, f"{operation}完成"
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
        self.progress.setValue(percent)
        self.status_label.setText(f"正在传输：{name}（{percent}%）")

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
        current_directory = posixpath.normpath(self.remote_directory_entry.text().strip() or "/")
        if current_directory in affected and self._session.connected:
            self._refresh_remote_directory(force=True)
        if succeeded:
            self.progress.setValue(100)
            if operation == "下载":
                self.status_label.setText("下载完成")
            else:
                self.status_label.setText(f"{operation}完成，已刷新服务器目录")
        else:
            self.status_label.setText(f"{operation}失败")
            QMessageBox.critical(self, f"{operation}失败", message)

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
    ) -> None:
        super().__init__(parent)
        self.parameters = parameters
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
        self._directory_input_revision = 0
        self._directory_verified_revision = -1
        self._directory_running = False
        self._terminal_menu: QMenu | None = None
        self._terminal_menu_request = ""
        self._file_context_requests: queue.Queue[tuple[InteractiveSSHSession, int, str, str, str | None] | None] = queue.Queue(maxsize=1)
        self._file_context_thread: threading.Thread | None = None
        self._process_operations: set[int] = set()
        self._monitor_running = False
        self._pending_monitor_status: tuple[int, InteractiveSSHSession, str] | None = None
        self._previous_cpu_total: int | None = None
        self._previous_cpu_idle: int | None = None
        self._terminal_log_timer = QTimer(self)
        self._terminal_log_timer.setSingleShot(True)
        self._terminal_log_timer.setInterval(_TERMINAL_LOG_FLUSH_INTERVAL_MS)
        self._terminal_log_timer.timeout.connect(self._flush_terminal_log)
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
        self._set_state("disconnected")
        QTimer.singleShot(0, self._apply_terminal_resize)

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return bool(self._session is not None and self._session.connected)

    def _create_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(6, 5, 6, 5)
        header.addWidget(QLabel(self.parameters.target))
        self.status_label = QLabel()
        header.addWidget(self.status_label, 1)
        self.ssh_tool_button = QPushButton("终端模式")
        self.ssh_tool_button.clicked.connect(self._tool_mode_requested)
        header.addWidget(self.ssh_tool_button)
        self.action_button = QPushButton()
        self.action_button.clicked.connect(self._handle_action)
        header.addWidget(self.action_button)
        self.file_transfer_button = QPushButton("文件传输")
        self.file_transfer_button.clicked.connect(self._open_file_transfer)
        header.addWidget(self.file_transfer_button)
        self.download_task_button = QPushButton("下载任务")
        self.download_task_button.clicked.connect(
            lambda: self.download_task_panel.show_panel()
        )
        header.addWidget(self.download_task_button)
        close_button = QPushButton("关闭标签")
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
        system_title = QLabel("服务器状态")
        system_title.setStyleSheet("font-weight:600; padding:4px 0;")
        system_layout.addWidget(system_title)
        self.system_label = QLabel("操作系统：--")
        self.system_label.setWordWrap(True)
        self.kernel_label = QLabel("内核版本：--")
        self.kernel_label.setWordWrap(True)
        self.uptime_label = QLabel("运行时间：--")
        self.load_label = QLabel("系统负载：--")
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
        disk_title = QLabel("磁盘占用")
        disk_title.setStyleSheet("font-weight:600; padding-top:6px;")
        system_layout.addWidget(disk_title)
        self.disk_table = QTreeWidget()
        self.disk_table.setColumnCount(3)
        self.disk_table.setHeaderLabels(["挂载路径", "已用 / 总量", "占用"])
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
        process_title = QLabel("运行中的程序（资源占用前 12）")
        process_title.setStyleSheet("font-weight:600; padding-top:6px;")
        system_layout.addWidget(process_title)
        self.process_table = QTreeWidget()
        self.process_table.setColumnCount(4)
        self.process_table.setHeaderLabels(["服务", "端口", "CPU", "内存"])
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
        self.monitor_status_label = QLabel("等待连接")
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
        self.output_text = XTermTerminal()
        self.output_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_text.customContextMenuRequested.connect(
            self._show_terminal_context_menu
        )
        self.output_text.textCommitted.connect(self._send_raw)
        self.output_text.binaryCommitted.connect(self._send_binary)
        self.output_text.shellIdentified.connect(self._register_terminal_shell)
        self.output_text.terminalResized.connect(self._terminal_resized)
        self.output_text.loadFailed.connect(self._append)
        terminal_layout.addWidget(self.output_text, 1)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(4, 5, 4, 5)
        input_row.addWidget(QLabel("整行命令："))
        self.command_entry = QLineEdit()
        self.command_entry.returnPressed.connect(self._send_command)
        self.command_entry.installEventFilter(self)
        input_row.addWidget(self.command_entry, 1)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self._send_command)
        input_row.addWidget(self.send_button)
        self.interrupt_button = QPushButton("中断")
        self.interrupt_button.clicked.connect(self._interrupt)
        input_row.addWidget(self.interrupt_button)
        clear_button = QPushButton("清屏")
        clear_button.clicked.connect(self.clear)
        input_row.addWidget(clear_button)
        self.file_panel_button = QPushButton("▲")
        self.file_panel_button.setFixedWidth(32)
        self.file_panel_button.setToolTip("展开服务器文件管理")
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

    def set_tool_mode(self, active: bool, enabled: bool = True) -> None:
        self.ssh_tool_button.setText("返回编辑" if active else "终端模式")
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
        self._append(f"[SSH] 正在连接 {self.parameters.target}\n")
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
            self._queue_event("connect_error", (attempt, session, f"SSH 连接失败：{exc}"))
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
                        chunks.append("\r\n[SSH] 连接建立后立即关闭，请检查服务器状态\r\n")
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
                        f"\r\n[SSH] 连接已关闭：{error}\r\n"
                        if error else "\r\n[SSH] 连接已关闭\r\n"
                    )
                    self._set_state("disconnected")
            elif event_type == "host_key":
                request = payload
                if isinstance(request, _HostKeyRequest) and not request.completed.is_set():
                    approved = False
                    if self._is_current(request.attempt, request.session):
                        approved = QMessageBox.question(
                            self,
                            "确认服务器主机密钥",
                            "这是第一次连接该服务器，尚未保存它的主机密钥。\n\n"
                            f"服务器：{request.hostname}\n"
                            f"密钥类型：{request.key_type}\n"
                            f"指纹：{request.fingerprint}\n\n"
                            "请确认该指纹与服务器管理员提供的一致。是否信任并保存？",
                            QMessageBox.Yes | QMessageBox.No,
                            QMessageBox.No,
                        ) == QMessageBox.Yes
                    request.approved = approved
                    request.completed.set()
            elif event_type == "terminal_selection":
                attempt, session, request, path, is_directory, error = payload
                if self._is_current(attempt, session):
                    self._populate_terminal_path_menu(request, path, is_directory, error)
            elif event_type == "terminal_directory":
                attempt, session, revision, directory = payload
                if self._is_current(attempt, session):
                    self._directory_running = False
                    if revision != self._directory_input_revision:
                        QTimer.singleShot(0, self._request_terminal_directory)
                    elif directory is not None and self._terminal_refresh_delay() <= 0:
                        self._directory_verified_revision = revision
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
                            self, "停止失败", f"无法停止 {service_name}：\n{error}"
                        )
                    else:
                        QMessageBox.information(
                            self,
                            "已发送停止指令",
                            f"已向 {service_name}（PID {pid}）发送停止指令",
                        )
                        QTimer.singleShot(800, self._request_system_status)
            elif event_type == "process_restarted":
                attempt, session, pid, service_name, message, error = payload
                if self._is_current(attempt, session):
                    self._process_operations.discard(pid)
                    if error:
                        self._log_event("重启服务失败", f"{service_name}（PID {pid}）：{error}")
                        QMessageBox.critical(self, "重启失败", f"{service_name}：\n{error}")
                    else:
                        self._log_event("重启服务", f"{service_name}：{message}")
                        QMessageBox.information(self, "重启结果", f"{service_name}\n{message}")
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
            self.command_entry.clear()
            self._process_operations.clear()
            if self._terminal_menu is not None:
                self._terminal_menu.close()
        status, action = {
            "connecting": ("正在连接…", "取消连接"),
            "connected": ("已连接", "断开"),
            "cancelled": ("已取消", "重新连接"),
            "error": ("连接失败", "重新连接"),
            "disconnected": ("未连接", "连接"),
        }[state]
        self.status_label.setText(status)
        self.action_button.setText(action)
        self._flush_terminal_log()
        self._log_event("连接状态", status)
        if state == "connected":
            self._monitor_timer.start()
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
        self._append("[SSH] 已取消连接\n")
        self._set_state("cancelled")

    def disconnect(self) -> None:
        self._attempt += 1
        self._reject_host_key_requests()
        session, self._session = self._session, None
        if session is not None:
            self._close_session_async(session)
        self._append("[SSH] 已断开连接\n")
        self._set_state("disconnected")

    def focus_terminal(self) -> None:
        if self.connected:
            self.output_text.focus_terminal()

    def clear(self) -> None:
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
        self.system_label.setText("操作系统：--")
        self.kernel_label.setText("内核版本：--")
        self.uptime_label.setText("运行时间：--")
        self.load_label.setText("系统负载：--")
        self.cpu_progress.setValue(0)
        self.cpu_progress.setFormat("CPU：--")
        self.memory_progress.setValue(0)
        self.memory_progress.setFormat("内存：--")
        self.swap_progress.setValue(0)
        self.swap_progress.setFormat("交换区：--")
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

        self.system_label.setText(f"操作系统：{system_value}")
        self.kernel_label.setText(f"内核版本：{kernel_value}")
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        uptime_parts = []
        if days:
            uptime_parts.append(f"{days} 天")
        if hours or days:
            uptime_parts.append(f"{hours} 小时")
        uptime_parts.append(f"{minutes} 分钟")
        self.uptime_label.setText("运行时间：" + " ".join(uptime_parts))
        self.load_label.setText(f"系统负载：{load_value}")

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
            f"CPU：{cpu_percent}%" if cpu_percent is not None else "CPU：采集中"
        )

        memory_total = memory.get("MemTotal", 0)
        memory_used = max(0, memory_total - memory.get("MemAvailable", memory_total))
        memory_percent = round(memory_used * 100 / memory_total) if memory_total else 0
        self.memory_progress.setValue(memory_percent)
        self.memory_progress.setFormat(
            f"内存：{memory_percent}%  {self._format_kib(memory_used)}/{self._format_kib(memory_total)}"
        )
        swap_total = memory.get("SwapTotal", 0)
        swap_used = max(0, swap_total - memory.get("SwapFree", swap_total))
        swap_percent = round(swap_used * 100 / swap_total) if swap_total else 0
        self.swap_progress.setValue(swap_percent)
        self.swap_progress.setFormat(
            f"交换区：{swap_percent}%  {self._format_kib(swap_used)}/{self._format_kib(swap_total)}"
            if swap_total
            else "交换区：未启用"
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
            item.setToolTip(0, f"PID：{pid}\n启动命令：{command}")
            item.setData(0, Qt.UserRole, pid)
            self.process_table.addTopLevelItem(item)
        self.monitor_status_label.setText(
            "更新时间：" + time.strftime("%H:%M:%S")
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
        menu.setStyleSheet(
            "QMenu { background:#ffffff; color:#111827; border:1px solid #cbd5e1; }"
            "QMenu::item { padding:6px 22px; }"
            "QMenu::item:selected { background:#eaf3ff; color:#111827; }"
        )
        stop_action = menu.addAction("停止此服务")
        restart_action = menu.addAction("重启")
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
        port_text = f"\n监听端口：{ports}" if ports and ports != "--" else ""
        if QMessageBox.question(
            self, "确认重启服务",
            f"确定重启“{service_name}”吗？\nPID：{pid}{port_text}\n\n"
            "将读取完整启动命令，停止原进程后在原工作目录后台启动。\n"
            "不需要服务器安装 Python 3，期间会短暂中断服务。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if not self._is_current(attempt, session) or not self.connected or pid in self._process_operations:
            return
        self._process_operations.add(pid)
        self.monitor_status_label.setText(f"正在重启：{service_name}…")
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
        port_text = f"\n监听端口：{ports}" if ports and ports != "--" else ""
        if QMessageBox.question(
            self,
            "确认停止服务",
            f"确定停止服务“{service_name}”吗？\nPID：{pid}{port_text}\n\n"
            "将发送正常终止信号，受系统管理的服务可能会自动重启。",
        ) != QMessageBox.Yes:
            return
        if not self._is_current(attempt, session) or not self.connected:
            QMessageBox.warning(self, "无法停止", "SSH 连接已断开")
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
        command = self.command_entry.text()
        self._last_terminal_input_at = time.monotonic()
        try:
            self._session.send_line(command)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
            return
        self._directory_input_revision += 1
        QTimer.singleShot(150, self._request_terminal_directory)
        if command.strip():
            self._flush_terminal_log()
            self._log_event("command", command)
            if not self._history or self._history[-1] != command:
                self._history.append(command)
                self._history = self._history[-200:]
            self._history_index = len(self._history)
        self.command_entry.clear()

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

    def _register_terminal_shell(self, token: str, pid: int) -> None:
        session = self._session
        if session is not None and session.register_shell(token, pid):
            self._request_terminal_directory()

    def _request_terminal_directory(self) -> None:
        session = self._session
        if (
            self._closed or self._directory_running or session is None
            or not self.connected
            or not self._file_panel_open or not self.isVisible()
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
        directory = session.query_working_directory()
        self._queue_event("terminal_directory", (attempt, session, revision, directory))

    def _sync_terminal_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory)
        if directory == self._terminal_directory:
            return
        self._terminal_directory = directory
        if self._file_panel_open and self._file_manager is not None:
            self._file_manager.open_directory(directory)

    def _toggle_file_manager(self) -> None:
        session = self._session
        if session is None or not self.connected:
            QMessageBox.warning(self, "无法打开文件管理", "请先连接 SSH 服务器")
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
        self.file_panel_button.setToolTip("收起服务器文件管理")
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
            self._file_manager.command_requested.connect(self._execute_selected_command)
            self._file_manager.directory_changed.connect(
                lambda directory: self._sync_file_directory(self._file_manager, directory)
            )
            self.terminal_file_splitter.addWidget(self._file_manager)
            self.terminal_file_splitter.setStretchFactor(1, 0)
            if not self._file_panel_open:
                self._file_manager.hide()
        else:
            self._file_manager.set_session(self._session)
        return self._file_manager

    def _hide_file_manager(self) -> None:
        self._directory_timer.stop()
        if self._file_manager is not None:
            self._file_manager.hide()
        self._file_panel_open = False
        if hasattr(self, "file_panel_button"):
            self.file_panel_button.setText("▲")
            self.file_panel_button.setToolTip("展开服务器文件管理")
        QTimer.singleShot(0, self._refresh_terminal_layout)

    def _open_file_transfer(self) -> None:
        if self._session is None or not self.connected:
            QMessageBox.warning(self, "无法打开文件传输", "请先连接 SSH 服务器")
            return
        if self._transfer_dialog is None:
            directory = (
                self._file_manager.remote_directory_entry.text()
                if self._file_manager is not None else self.default_open_path
            )
            self._transfer_dialog = SftpTransferDialog(
                self, self._session, directory,
                log_reader=self._log_reader,
                download_task_panel=self.download_task_panel,
                server_key=self.parameters.target,
            )
            self._transfer_dialog.command_requested.connect(self._execute_selected_command)
            self._transfer_dialog.directory_changed.connect(
                lambda directory: self._sync_file_directory(self._transfer_dialog, directory)
            )
        else:
            self._transfer_dialog.set_session(self._session)
            self._transfer_dialog.refresh()
        self._transfer_dialog.show()
        self._transfer_dialog.raise_()
        self._transfer_dialog.activateWindow()

    def _sync_file_directory(self, source: SftpTransferDialog, directory: str) -> None:
        for manager in (self._file_manager, self._transfer_dialog):
            if manager is None or manager is source:
                continue
            manager._invalidate_directory(directory)
            current = posixpath.normpath(manager.remote_directory_entry.text().strip() or "/")
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
            self._file_manager.open_directory(directory)

    def _execute_selected_command(self, command: str) -> None:
        if not self.connected or self._session is None:
            return
        self._last_terminal_input_at = time.monotonic()
        try:
            # Clear an unfinished shell input line before sending the selection.
            self._session.send_raw("\x15" + command + "\r")
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
            return
        self._directory_input_revision += 1
        QTimer.singleShot(150, self._request_terminal_directory)
        self._flush_terminal_log()
        self._log_event("command", command)
        if not self._history or self._history[-1] != command:
            self._history.append(command)
            self._history = self._history[-200:]
        self._history_index = len(self._history)
        self.focus_terminal()

    def _send_raw(self, value: str) -> None:
        if not value or self._session is None:
            return
        self._last_terminal_input_at = time.monotonic()
        try:
            if value == "\x03":
                self._session.interrupt()
                self.command_entry.clear()
            else:
                self._session.send_raw(value)
            if "\r" in value or "\n" in value:
                self._directory_input_revision += 1
                QTimer.singleShot(150, self._request_terminal_directory)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _send_binary(self, value: bytes) -> None:
        if not value or self._session is None:
            return
        self._last_terminal_input_at = time.monotonic()
        try:
            self._session.send_bytes(value)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _show_terminal_context_menu(self, position: QPoint) -> None:
        selection = self.output_text.selected_text()
        text = selection.strip()
        single_line = bool(text) and not any(ord(char) < 32 or ord(char) == 127 for char in text)
        path = text
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1]
        if self._terminal_menu is not None:
            self._terminal_menu.close()
        menu = QMenu(self)
        request = uuid.uuid4().hex
        self._terminal_menu = menu
        self._terminal_menu_request = request
        menu.setProperty("emptySelection", not bool(text))
        copy_action = menu.addAction("复制文本")
        copy_action.setEnabled(bool(text))
        copy_action.triggered.connect(lambda: QApplication.clipboard().setText(selection))
        paste_action = menu.addAction("粘贴文本")
        paste_action.setEnabled(self.connected)
        paste_action.triggered.connect(
            lambda: self.output_text.paste(QApplication.clipboard().text())
        )
        if single_line:
            execute_action = menu.addAction("执行选中的命令")
            execute_action.setEnabled(self.connected)
            execute_action.triggered.connect(lambda: self._execute_selected_command(text))

        def closed() -> None:
            if self._terminal_menu is menu:
                self._terminal_menu = None
                self._terminal_menu_request = ""
            menu.deleteLater()

        menu.aboutToHide.connect(closed)
        if self.connected and self._session is not None and (not text or (single_line and len(path) <= 4096)):
            menu.addSeparator()
            cached = self._cached_terminal_selection(path if text else "")
            if cached is not None:
                self._populate_terminal_path_menu(request, cached[0], cached[1], "")
            else:
                placeholder = menu.addAction("正在识别远程路径…")
                placeholder.setObjectName("remotePathPlaceholder")
                placeholder.setEnabled(False)
                known_directory = (
                    self._terminal_directory
                    if self._directory_verified_revision == self._directory_input_revision
                    else None
                )
                self._queue_file_context_probe(
                    self._session, self._attempt, request,
                    path if text else "", known_directory,
                )
        menu.popup(self.output_text.mapToGlobal(position))

    def _queue_file_context_probe(
        self, session: InteractiveSSHSession, attempt: int, request: str,
        selection: str, known_directory: str | None,
    ) -> None:
        if self._file_context_thread is None or not self._file_context_thread.is_alive():
            self._file_context_thread = threading.Thread(
                target=self._file_context_worker,
                name="ssh-file-context",
                daemon=True,
            )
            self._file_context_thread.start()
        try:
            self._file_context_requests.put_nowait(
                (session, attempt, request, selection, known_directory)
            )
        except queue.Full:
            try:
                self._file_context_requests.get_nowait()
            except queue.Empty:
                pass
            self._file_context_requests.put_nowait(
                (session, attempt, request, selection, known_directory)
            )

    def _file_context_worker(self) -> None:
        while True:
            work = self._file_context_requests.get()
            if work is None or self._closed:
                return
            self._resolve_terminal_selection(*work)

    def _cached_terminal_selection(self, selection: str) -> tuple[str, bool] | None:
        if selection.startswith("~"):
            return None
        if not selection.startswith("/") and (
            self._terminal_directory is None
            or self._directory_verified_revision != self._directory_input_revision
        ):
            return None
        path = posixpath.normpath(posixpath.join(self._terminal_directory or "/", selection))
        if path == self._terminal_directory or path == "/":
            return path, True
        for manager in (self._file_manager, self._transfer_dialog):
            if manager is not None and manager._session is self._session:
                is_directory = manager.cached_path_type(path)
                if is_directory is not None:
                    return path, is_directory
        return None

    def _resolve_terminal_selection(
        self, session: InteractiveSSHSession, attempt: int, request: str,
        selection: str, known_directory: str | None,
    ) -> None:
        path, is_directory, error = "", False, ""
        try:
            sftp = session.open_isolated_sftp(allow_shared_fallback=False)
            try:
                sftp.get_channel().settimeout(6)
                directory = known_directory
                if not selection.startswith(("/", "~")):
                    pid = session.shell_process_id()
                    if pid is not None:
                        directory = sftp.readlink(f"/proc/{pid}/cwd")
                if selection == "~" or selection.startswith("~/"):
                    path = posixpath.join(sftp.normalize("."), selection[2:])
                elif selection.startswith("/"):
                    path = selection
                else:
                    if directory is None:
                        raise SSHSessionError("暂未读到终端当前目录，请选中完整路径")
                    path = posixpath.join(directory, selection)
                path = posixpath.normpath(path)
                attributes = sftp.stat(path)
                is_directory = stat.S_ISDIR(attributes.st_mode)
                if not is_directory and not stat.S_ISREG(attributes.st_mode):
                    raise SSHSessionError("该路径不是普通文件或文件夹")
            finally:
                sftp.close()
        except Exception as exc:
            error = str(exc)
        self._queue_event(
            "terminal_selection", (attempt, session, request, path, is_directory, error)
        )

    def _populate_terminal_path_menu(
        self, request: str, path: str, is_directory: bool, error: str,
    ) -> None:
        menu = self._terminal_menu
        if request != self._terminal_menu_request or menu is None:
            return
        for action in menu.actions():
            if action.objectName() == "remotePathPlaceholder":
                menu.removeAction(action)
                action.deleteLater()
        if error:
            action = menu.addAction("未识别到可操作的文件或目录")
            action.setToolTip(error)
            action.setEnabled(False)
            return
        if not menu.property("emptySelection"):
            _add_remote_type_actions(menu, path, is_directory, self._terminal_path_action)
            menu.addSeparator()
            for title, action in (("复制文件/文件夹", "copy"), ("剪切文件/文件夹", "cut"), ("删除…", "delete")):
                item = menu.addAction(title)
                item.setEnabled(bool(path.strip("/")))
                item.triggered.connect(
                    lambda _checked=False, value=action: self._terminal_path_action(value, path)
                )
        if is_directory:
            paste_action = menu.addAction("粘贴文件到此目录")
            paste_action.setEnabled(self._ensure_file_manager()._remote_clipboard() is not None)
            paste_action.triggered.connect(lambda: self._terminal_path_action("paste", path))
            menu.addAction("新建文件…").triggered.connect(
                lambda: self._terminal_path_action("new_file", path)
            )
            menu.addAction("新建文件夹…").triggered.connect(
                lambda: self._terminal_path_action("new_directory", path)
            )
        menu.adjustSize()
        available = menu.screen().availableGeometry()
        menu.move(
            max(available.left(), min(menu.x(), available.right() - menu.width() + 1)),
            max(available.top(), min(menu.y(), available.bottom() - menu.height() + 1)),
        )

    def _terminal_path_action(self, action: str, path: str) -> None:
        if not self.connected or self._session is None:
            return
        if action == "open":
            self._open_terminal_directory(path)
            return
        manager = self._ensure_file_manager()
        if action in {"copy", "cut"}:
            manager._copy_remote_paths([path], action == "cut")
        elif action == "paste":
            manager._paste_remote_items(path)
        elif action == "delete":
            manager._delete_remote_paths([path])
        elif action == "new_file":
            manager._create_remote_file(path)
        elif action == "new_directory":
            manager._create_remote_directory(path)
        else:
            manager._handle_path_action(action, path)

    def _append(self, value: str) -> None:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        self._feed_terminal(normalized.replace("\n", "\r\n"))

    def _feed_terminal(self, value: str) -> None:
        if not value:
            return
        self._last_terminal_output_at = time.monotonic()
        self._queue_terminal_log(value)
        self.output_text.write(value)

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
