"""PySide6 view for one independent interactive SSH connection."""

from __future__ import annotations

import queue
import threading
import time
import os
import posixpath
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pyte
from PySide6.QtCore import QDir, QEvent, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QFileSystemModel,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QSplitter,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import ServerParameters
from ssh_terminal import InteractiveSSHSession, SSHSessionError


StateCallback = Callable[["QtSSHTerminalTab"], None]
CloseCallback = Callable[["QtSSHTerminalTab"], None]
_TERMINAL_HISTORY_LINES = 2000
_MAX_OUTPUT_CHARACTERS_PER_POLL = 262144
_RENDER_INTERVAL_MS = 50


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


class _TerminalOutput(QPlainTextEdit):
    keyPressed = Signal(object)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.keyPressed.emit(event)


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
    remote_loaded = Signal(str, object)
    remote_error = Signal(str)
    upload_progress = Signal(str, int, int)
    upload_finished = Signal(bool, str)


class SftpTransferDialog(QDialog):
    """Side-by-side local and remote file browser for an SSH session."""

    def __init__(
        self,
        parent: QWidget,
        session: InteractiveSSHSession,
        initial_remote_directory: str | None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._loading = False
        self._uploading = False
        self._events = _TransferEvents(self)
        self._events.remote_loaded.connect(self._display_remote_files)
        self._events.remote_error.connect(self._display_remote_error)
        self._events.upload_progress.connect(self._update_upload_progress)
        self._events.upload_finished.connect(self._finish_upload)
        self.setWindowTitle("SFTP 文件传输")
        self.resize(1080, 680)
        self._create_widgets(initial_remote_directory)
        self._refresh_remote_directory()

    def _create_widgets(self, initial_remote_directory: str | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(QLabel("左侧选择本地文件，点击“上传选中项”或拖到右侧服务器目录即可上传。"))

        splitter = QSplitter(Qt.Horizontal)
        local_panel = QWidget()
        local_layout = QVBoxLayout(local_panel)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_path_row = QHBoxLayout()
        self.local_directory_entry = QLineEdit(str(Path.home()))
        self.local_directory_entry.returnPressed.connect(self._set_local_directory)
        local_path_row.addWidget(self.local_directory_entry, 1)
        local_choose = QPushButton("选择本地目录")
        local_choose.clicked.connect(self._choose_local_directory)
        local_path_row.addWidget(local_choose)
        local_layout.addLayout(local_path_row)
        self.local_model = QFileSystemModel(self)
        self.local_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)
        self.local_model.setRootPath(str(Path.home()))
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(str(Path.home())))
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.setDragEnabled(True)
        self.local_tree.doubleClicked.connect(self._local_item_activated)
        for column in (1, 2, 3):
            self.local_tree.hideColumn(column)
        local_layout.addWidget(self.local_tree, 1)
        splitter.addWidget(local_panel)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.addStretch(1)
        self.upload_button = QPushButton("上传选中项  →")
        self.upload_button.clicked.connect(self._upload_selected_local_items)
        controls_layout.addWidget(self.upload_button)
        controls_layout.addStretch(1)
        splitter.addWidget(controls)

        remote_panel = QWidget()
        remote_layout = QVBoxLayout(remote_panel)
        remote_layout.setContentsMargins(0, 0, 0, 0)
        remote_path_row = QHBoxLayout()
        self.remote_directory_entry = QLineEdit(initial_remote_directory or "/")
        self.remote_directory_entry.returnPressed.connect(self._refresh_remote_directory)
        remote_path_row.addWidget(self.remote_directory_entry, 1)
        remote_up = QPushButton("上级")
        remote_up.clicked.connect(self._go_remote_parent)
        remote_path_row.addWidget(remote_up)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_remote_directory)
        remote_path_row.addWidget(refresh)
        remote_layout.addLayout(remote_path_row)
        self.remote_tree = _RemoteFileTree()
        self.remote_tree.setHeaderLabels(["服务器文件", "类型", "大小"])
        self.remote_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_tree.itemDoubleClicked.connect(self._remote_item_activated)
        self.remote_tree.files_dropped.connect(self._upload_paths)
        remote_layout.addWidget(self.remote_tree, 1)
        splitter.addWidget(remote_panel)
        splitter.setSizes([470, 130, 470])
        root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        footer.addWidget(self.progress, 1)
        self.status_label = QLabel("准备就绪")
        footer.addWidget(self.status_label)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        root.addLayout(footer)

    def _choose_local_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择本地目录", self.local_directory_entry.text())
        if directory:
            self.local_directory_entry.setText(directory)
            self._set_local_directory()

    def _set_local_directory(self) -> None:
        directory = Path(self.local_directory_entry.text()).expanduser()
        if not directory.is_dir():
            QMessageBox.warning(self, "目录不存在", "请选择存在的本地目录")
            return
        resolved = str(directory.resolve())
        self.local_directory_entry.setText(resolved)
        self.local_tree.setRootIndex(self.local_model.index(resolved))

    def _local_item_activated(self, index: object) -> None:
        path = Path(self.local_model.filePath(index))
        if path.is_file():
            self._upload_paths([path])
        elif path.is_dir():
            self.local_directory_entry.setText(str(path))
            self.local_tree.setRootIndex(index)

    def _upload_selected_local_items(self) -> None:
        selection = self.local_tree.selectionModel()
        if selection is None:
            return
        paths = [Path(self.local_model.filePath(index)) for index in selection.selectedRows(0)]
        self._upload_paths(paths)

    def _go_remote_parent(self) -> None:
        current = self.remote_directory_entry.text().strip() or "/"
        self.remote_directory_entry.setText(posixpath.dirname(posixpath.normpath(current)) or "/")
        self._refresh_remote_directory()

    def _remote_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        remote_path = str(item.data(0, Qt.UserRole))
        if bool(item.data(0, Qt.UserRole + 1)):
            self.remote_directory_entry.setText(remote_path)
            self._refresh_remote_directory()

    def _refresh_remote_directory(self) -> None:
        if self._loading or self._uploading:
            return
        directory = self.remote_directory_entry.text().strip() or "/"
        self._loading = True
        self.status_label.setText("正在读取服务器目录…")
        threading.Thread(
            target=self._load_remote_worker,
            args=(directory,),
            name="sftp-list-directory",
            daemon=True,
        ).start()

    def _load_remote_worker(self, directory: str) -> None:
        try:
            sftp = self._session.open_sftp()
            try:
                entries = [
                    (attribute.filename, stat.S_ISDIR(attribute.st_mode), int(attribute.st_size))
                    for attribute in sftp.listdir_attr(directory)
                ]
            finally:
                sftp.close()
        except Exception as exc:
            self._events.remote_error.emit(str(exc))
        else:
            self._events.remote_loaded.emit(directory, entries)

    def _display_remote_files(self, directory: str, entries: object) -> None:
        self._loading = False
        self.remote_directory_entry.setText(directory)
        self.remote_tree.clear()
        for name, is_directory, size in sorted(entries, key=lambda item: (not item[1], item[0].lower())):
            path = posixpath.join(directory, name)
            item = QTreeWidgetItem([name, "文件夹" if is_directory else "文件", "" if is_directory else self._format_size(size)])
            item.setData(0, Qt.UserRole, path)
            item.setData(0, Qt.UserRole + 1, is_directory)
            self.remote_tree.addTopLevelItem(item)
        self.status_label.setText(f"服务器目录：{len(entries)} 项")

    def _display_remote_error(self, message: str) -> None:
        self._loading = False
        self.status_label.setText("读取失败")
        QMessageBox.critical(self, "无法读取服务器目录", message)

    def _upload_paths(self, paths: object) -> None:
        sources = [Path(path) for path in paths if Path(path).is_file() or Path(path).is_dir()]
        if not sources or self._uploading:
            return
        remote_directory = self.remote_directory_entry.text().strip()
        if not remote_directory:
            QMessageBox.warning(self, "未填写目录", "请填写服务器目标目录")
            return
        self._uploading = True
        self.upload_button.setEnabled(False)
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
            sftp = self._session.open_sftp()
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
                            self._events.upload_progress.emit(local_file.name, total_current, total)

                    sftp.put(str(local_file), remote_file, callback=progress, confirm=True)
                    completed += file_size
                    self._events.upload_progress.emit(local_file.name, completed, total)
            finally:
                sftp.close()
        except Exception as exc:
            self._events.upload_finished.emit(False, str(exc))
        else:
            self._events.upload_finished.emit(True, "上传完成")

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

    def _update_upload_progress(self, name: str, current: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(current * 100 / total))
        self.progress.setValue(percent)
        self.status_label.setText(f"正在上传：{name}（{percent}%）")

    def _finish_upload(self, succeeded: bool, message: str) -> None:
        self._uploading = False
        self.upload_button.setEnabled(True)
        if succeeded:
            self.progress.setValue(100)
            self.status_label.setText("上传完成，已刷新服务器目录")
            self._refresh_remote_directory()
        else:
            self.status_label.setText("上传失败")
            QMessageBox.critical(self, "上传失败", message)

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
        state_changed: StateCallback,
        close_requested: CloseCallback,
    ) -> None:
        super().__init__(parent)
        self.parameters = parameters
        self.parameter_path = parameter_path.resolve()
        self.default_open_path = default_open_path
        self._state_changed = state_changed
        self._close_requested = close_requested
        self._state = "disconnected"
        self._attempt = 0
        self._session: InteractiveSSHSession | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
        self._thread_events = _ThreadEvents(self)
        self._thread_events.available.connect(self._drain_events)
        self._terminal_columns = 160
        self._terminal_rows = 48
        self._terminal_screen = pyte.HistoryScreen(
            self._terminal_columns,
            self._terminal_rows,
            history=_TERMINAL_HISTORY_LINES,
        )
        self._terminal_stream = pyte.Stream(self._terminal_screen)
        self._history: list[str] = []
        self._history_index = 0
        self._host_key_requests: set[_HostKeyRequest] = set()
        self._host_key_requests_lock = threading.Lock()
        self._history_cache_key: tuple[int, int, int, int] | None = None
        self._history_cache_lines: list[str] = []
        self._rendered_lines: list[str] = []
        self._last_render_time = 0.0
        self._closed = False
        self._uploading = False
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_terminal)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_terminal_resize)
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
        self.action_button = QPushButton()
        self.action_button.clicked.connect(self._handle_action)
        header.addWidget(self.action_button)
        self.file_transfer_button = QPushButton("文件传输")
        self.file_transfer_button.clicked.connect(self._open_file_transfer)
        header.addWidget(self.file_transfer_button)
        close_button = QPushButton("关闭标签")
        close_button.clicked.connect(lambda: self._close_requested(self))
        header.addWidget(close_button)
        root.addLayout(header)

        self.output_text = _TerminalOutput()
        self.output_text.setReadOnly(True)
        self.output_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output_text.setFont(QFont("Cascadia Mono", 10))
        self.output_text.setStyleSheet(
            "QPlainTextEdit { background:#111827; color:#e5e7eb; "
            "selection-background-color:#374151; selection-color:#ffffff; "
            "border:0; padding:8px; }"
        )
        self.output_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_text.customContextMenuRequested.connect(
            self._show_terminal_context_menu
        )
        self.output_text.keyPressed.connect(self._handle_terminal_key)
        root.addWidget(self.output_text, 1)

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
        root.addLayout(input_row)
        self._update_upload_controls()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.command_entry and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Up:
                self._history_previous()
                return True
            if key == Qt.Key_Down:
                self._history_next()
                return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
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
            while not self._closed and attempt == self._attempt and session is self._session:
                try:
                    self._events.put(("output", (attempt, session, value)), timeout=0.1)
                    self._thread_events.available.emit()
                    return
                except queue.Full:
                    continue

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
            session.connect(self.parameters, self.default_open_path)
        except SSHSessionError as exc:
            self._queue_event("connect_error", (attempt, session, str(exc)))
        except Exception as exc:
            self._queue_event("connect_error", (attempt, session, f"SSH 连接失败：{exc}"))
        else:
            self._queue_event("connected", (attempt, session))

    def _queue_event(self, event_type: str, payload: object) -> bool:
        while not self._closed:
            try:
                self._events.put((event_type, payload), timeout=0.1)
                self._thread_events.available.emit()
                return True
            except queue.Full:
                continue
        return False

    def _drain_events(self) -> None:
        started_at = time.monotonic()
        processed = 0
        characters = 0
        chunks: list[str] = []
        while processed < 256 and characters < _MAX_OUTPUT_CHARACTERS_PER_POLL:
            if time.monotonic() - started_at >= 0.012:
                break
            try:
                event_type, payload = self._events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if event_type == "output":
                attempt, session, value = payload
                if self._is_current(attempt, session):
                    value = str(value)
                    chunks.append(value)
                    characters += len(value)
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
        if chunks:
            self._feed_terminal("".join(chunks))
        if not self._events.empty():
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
        status, action = {
            "connecting": ("正在连接…", "取消连接"),
            "connected": ("已连接", "断开"),
            "cancelled": ("已取消", "重新连接"),
            "error": ("连接失败", "重新连接"),
            "disconnected": ("未连接", "连接"),
        }[state]
        self.status_label.setText(status)
        self.action_button.setText(action)
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
            self.output_text.setFocus()

    def clear(self) -> None:
        self._terminal_screen = pyte.HistoryScreen(
            self._terminal_columns, self._terminal_rows, history=_TERMINAL_HISTORY_LINES
        )
        self._terminal_stream = pyte.Stream(self._terminal_screen)
        self._history_cache_key = None
        self._history_cache_lines = []
        self._rendered_lines = []
        self._render_terminal()
        if self.connected and self._session is not None:
            try:
                self._session.send_raw("\x0c")
            except SSHSessionError:
                pass

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._attempt += 1
        self._render_timer.stop()
        self._resize_timer.stop()
        self._reject_host_key_requests()
        session, self._session = self._session, None
        if session is not None:
            self._close_session_async(session)

    def _close_session_async(self, session: InteractiveSSHSession) -> None:
        threading.Thread(
            target=session.close,
            kwargs={"notify": False},
            name=f"ssh-close-{self.parameters.name}",
            daemon=True,
        ).start()

    def _send_command(self) -> None:
        if not self.connected or self._session is None:
            return
        command = self.command_entry.text()
        try:
            self._session.send_line(command)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
            return
        if command.strip():
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
            try:
                self._session.interrupt()
                self.command_entry.clear()
            except SSHSessionError as exc:
                self._append(f"[SSH] {exc}\n")

    def _update_upload_controls(self) -> None:
        self.file_transfer_button.setEnabled(self.connected)

    def _open_file_transfer(self) -> None:
        session = self._session
        if session is None or not self.connected:
            QMessageBox.warning(self, "无法打开文件传输", "请先连接 SSH 服务器")
            return
        SftpTransferDialog(self, session, self.default_open_path).exec()

    def _handle_terminal_key(self, event: QKeyEvent) -> None:
        if not self.connected or self._session is None:
            return
        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier and event.key() == Qt.Key_C:
            if self.output_text.textCursor().hasSelection():
                self.output_text.copy()
            else:
                self._interrupt()
            return
        if modifiers & Qt.ControlModifier and event.key() == Qt.Key_V:
            self._send_raw(QApplication.clipboard().text())
            return
        if modifiers & Qt.ControlModifier and ord("A") <= event.key() <= ord("Z"):
            sequence = chr(event.key() - ord("A") + 1)
        else:
            sequence = {
                Qt.Key_Tab: "\t", Qt.Key_Return: "\r", Qt.Key_Enter: "\r",
                Qt.Key_Backspace: "\x7f", Qt.Key_Delete: "\x1b[3~",
                Qt.Key_Left: "\x1b[D", Qt.Key_Right: "\x1b[C",
                Qt.Key_Up: "\x1b[A", Qt.Key_Down: "\x1b[B",
                Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F",
                Qt.Key_Escape: "\x1b", Qt.Key_PageUp: "\x1b[5~",
                Qt.Key_PageDown: "\x1b[6~", Qt.Key_F1: "\x1bOP",
                Qt.Key_F2: "\x1bOQ", Qt.Key_F3: "\x1bOR", Qt.Key_F4: "\x1bOS",
                Qt.Key_F5: "\x1b[15~", Qt.Key_F6: "\x1b[17~",
                Qt.Key_F7: "\x1b[18~", Qt.Key_F8: "\x1b[19~",
                Qt.Key_F9: "\x1b[20~", Qt.Key_F10: "\x1b[21~",
                Qt.Key_F11: "\x1b[23~", Qt.Key_F12: "\x1b[24~",
            }.get(event.key())
            if sequence is None and event.text() and ord(event.text()[0]) >= 32:
                sequence = event.text()
        if sequence:
            self._send_raw(sequence)

    def _send_raw(self, value: str) -> None:
        if not value or self._session is None:
            return
        try:
            self._session.send_raw(value)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _show_terminal_context_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        paste_action = menu.addAction("粘贴")
        selected = menu.exec(self.output_text.mapToGlobal(position))
        if selected is copy_action:
            self.output_text.copy()
        elif selected is paste_action:
            self._send_raw(QApplication.clipboard().text())

    def _append(self, value: str) -> None:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        self._feed_terminal(normalized.replace("\n", "\r\n"))

    def _feed_terminal(self, value: str) -> None:
        if not value:
            return
        self._terminal_stream.feed(value)
        elapsed_ms = (time.monotonic() - self._last_render_time) * 1000
        if elapsed_ms >= _RENDER_INTERVAL_MS:
            self._render_terminal()
        elif not self._render_timer.isActive():
            self._render_timer.start(max(1, int(_RENDER_INTERVAL_MS - elapsed_ms)))

    def _apply_terminal_resize(self) -> None:
        metrics = self.output_text.fontMetrics()
        character_width = max(1, metrics.horizontalAdvance("0"))
        line_height = max(1, metrics.lineSpacing())
        viewport = self.output_text.viewport().size()
        columns = max(20, (viewport.width() - 16) // character_width)
        rows = max(4, (viewport.height() - 16) // line_height)
        if (columns, rows) == (self._terminal_columns, self._terminal_rows):
            return
        self._terminal_columns, self._terminal_rows = columns, rows
        self._terminal_screen.resize(lines=rows, columns=columns)
        if self._session is not None:
            try:
                self._session.resize_pty(columns, rows)
            except SSHSessionError as exc:
                self._append(f"[SSH] {exc}\n")
                return
        self._render_terminal()

    def _history_line_text(self, line: object) -> str:
        getter = getattr(line, "get", None)
        if getter is None:
            return str(line).rstrip()
        return "".join(
            getattr(getter(column), "data", " ") if getter(column) is not None else " "
            for column in range(self._terminal_columns)
        ).rstrip()

    def _render_terminal(self) -> None:
        if self._closed:
            return
        self._render_timer.stop()
        self._last_render_time = time.monotonic()
        scrollbar = self.output_text.verticalScrollBar()
        follow = scrollbar.value() >= scrollbar.maximum() - 1
        old_value = scrollbar.value()
        history = getattr(self._terminal_screen, "history", None)
        history_top = list(getattr(history, "top", ()))
        history_key = (
            self._terminal_columns,
            len(history_top),
            id(history_top[0]) if history_top else 0,
            id(history_top[-1]) if history_top else 0,
        )
        if history_key != self._history_cache_key:
            self._history_cache_lines = [self._history_line_text(line) for line in history_top]
            self._history_cache_key = history_key
        display = list(self._terminal_screen.display) or [""]
        cursor_y = min(max(0, self._terminal_screen.cursor.y), len(display) - 1)
        cursor_x = max(0, self._terminal_screen.cursor.x)
        last_content = max((i for i, line in enumerate(display) if line.rstrip()), default=0)
        screen_lines = [line.rstrip() for line in display[: max(cursor_y, last_content) + 1]]
        if len(screen_lines[cursor_y]) <= cursor_x:
            screen_lines[cursor_y] += " " * (cursor_x - len(screen_lines[cursor_y]) + 1)
        lines = self._history_cache_lines + screen_lines
        cursor_line = len(self._history_cache_lines) + cursor_y
        if lines != self._rendered_lines:
            self.output_text.setPlainText("\n".join(lines))
            self._rendered_lines = lines
        cursor = self.output_text.document().findBlockByNumber(cursor_line)
        text_cursor = QTextCursor(cursor)
        text_cursor.setPosition(cursor.position() + cursor_x)
        text_cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = text_cursor
        selection.format = QTextCharFormat()
        selection.format.setBackground(Qt.lightGray)
        selection.format.setForeground(Qt.black)
        self.output_text.setExtraSelections([selection])
        if follow:
            self.output_text.setTextCursor(text_cursor)
            self.output_text.ensureCursorVisible()
        else:
            scrollbar.setValue(old_value)
