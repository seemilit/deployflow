"""PySide6 desktop interface for DeployFlow.

The deployment, configuration, Git, SSH and workflow modules intentionally stay
UI-independent.  This module is the Qt replacement for the former Tk front end.
"""

from __future__ import annotations

import ctypes
import hmac
import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSignalBlocker, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QContextMenuEvent, QFont, QIcon, QKeyEvent, QKeySequence, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QRubberBand,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from config import ConfigurationError, load_server_parameters, mask_ip_address
from i18n import LANGUAGES, current_language, initialize, tr
from i18n.qt import install_qt_translations
from log_format import timestamp_log_text
from i18n.widgets import (
    QAction, QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QTabBar, QTabWidget,
    refresh_translations,
)
from password_protection import (
    PasswordProtectionError,
    is_legacy_protected,
    is_protected,
    protect_text,
    unprotect_text,
)
from qt_ssh_terminal_view import QtSSHTerminalTab
from terminal_directory_cache import start_terminal_cache_cleanup
from workflow import WORKFLOW_TYPE_BY_KEY, WORKFLOW_TYPES, WorkflowTask, load_workflow_task, parse_workflow_document
from workflow_executor import WorkflowExecutor


_PASSWORD_LINE_PATTERN = re.compile(
    r"^(?P<prefix>[ \t]*PASSWORD[ \t]*=[ \t]*)(?P<value>.*?)(?P<suffix>[ \t]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
_HIDDEN_PASSWORD_VALUE = "********"
_THEME_PRESETS = {
    "white": {
        "background": "#f8fafc", "surface": "#ffffff", "foreground": "#111827",
        "muted": "#64748b", "border": "#cbd5e1", "sidebar": "#e5e7eb",
        "hover": "#f3f4f6", "selection": "#dbeafe", "selection_text": "#1d4ed8",
    },
    "dark": {
        "background": "#1e2025", "surface": "#272a30", "foreground": "#e6e8ed",
        "muted": "#a0a7b4", "border": "#3e434d", "sidebar": "#22252b",
        "hover": "#323741", "selection": "#344966", "selection_text": "#edf3ff",
    },
    "paper": {
        "background": "#f4ecd8", "surface": "#fff8e7", "foreground": "#3f2f1f",
        "muted": "#75664d", "border": "#cbbd9e", "sidebar": "#e8dcc0",
        "hover": "#efe4ca", "selection": "#e2c98d", "selection_text": "#3f2f1f",
    },
    "green": {
        "background": "#dfeee0", "surface": "#eff8ee", "foreground": "#203b28",
        "muted": "#52705a", "border": "#aac8ad", "sidebar": "#cfe3d1",
        "hover": "#d9eadb", "selection": "#b6d9ba", "selection_text": "#17371f",
    },
}
_SCRIPT_EXTENSIONS = {
    "Linux / Bash 脚本（.sh，常用）": ".sh",
    "Linux / Bash 脚本（.bash）": ".bash",
    "Windows 批处理脚本（.bat，常用）": ".bat",
    "Windows 命令脚本（.cmd）": ".cmd",
    "PowerShell 脚本（.ps1）": ".ps1",
}
_PROPERTY_TOOLTIPS = {
    "IP_ADDRESS": "填服务器地址，例如 192.168.1.20 或 deploy.example.com。",
    "PORT": "填 SSH 端口。大多数服务器用 22；服务器改过端口时填管理员提供的数字。",
    "USERNAME": "填登录服务器的账号，例如 root、deploy。",
    "PASSWORD": "填这个账号的登录密码；使用私钥登录时可以留空。",
    "KEY_FILENAME": "填私钥文件的完整本地路径，例如 C:\\Keys\\prod.pem。使用密码登录时留空。",
    "AUTH_METHOD": "选择连接服务器时默认使用密码还是 SSH 私钥；两种认证信息都会继续保留。",
    "DEFAULT_OPEN_PATH": "仅用于手动打开 SSH 终端后自动进入的目录，例如 /opt/app；执行任务时不会使用它。",
    "DEFAULT_OPEN_COMMAND": "选择对应的默认访问路径后自动执行这条命令，例如 tail -f app.log；不需要自动执行时留空。",
    "PARAMETER_FILE": "选一份服务器配置文件，例如 production.txt。任务会用其中的地址、账号和密码连接服务器。",
    "PROJECT_PATH": "填本机项目所在目录，例如 D:\\code\\my-service；构建或 Git 操作会在这里执行。",
    "JAR_FILE": "填要部署的 JAR 文件名，例如 app.jar；不填时程序会自动在 target 目录中寻找。",
    "BUILD_TIMEOUT": "填 Maven 打包最长允许多久，单位秒。普通项目可填 900。",
    "IDEA_PROJECT_PATH": "填 IDEA 中正在开发的项目目录；只有需要自动合并和推送 Git 分支时才填写。",
    "TARGET_BRANCH": "填代码最终要合并、推送到的 Git 分支，例如 main 或 release。",
    "BRANCH": "填需要操作的 Git 分支名称，例如 develop。",
    "RESTART_COMMAND": "填上传完成后在服务器执行的命令，例如 systemctl restart my-service。",
    "RESTART_PATH": "填执行重启命令前要进入的服务器目录，例如 /opt/my-service；不需要切换目录可留空。",
    "RESTART_SCRIPT_PATH": "旧版配置：填服务器上已有的重启脚本完整路径，例如 /opt/app/restart.sh。",
    "RUN_SCRIPT_PATH": "旧版配置：填服务器上已有的启动脚本完整路径。",
    "SCRIPT_FILE": "填 conf/scripts 中的脚本文件名，例如 restart.sh；不要填完整路径。",
    "SCRIPT_PATH": "填运行脚本前要进入的目录；不需要切换目录可留空。",
    "RESTART_LOCAL_SCRIPT": "填 conf/scripts 中的本地脚本文件名。程序会把脚本内容发送到服务器执行。",
    "RESTART_DELAY": "填当前步骤完成后等待多久再执行下一步，单位秒；不需要等待填 0。",
    "HEALTH_CHECK_COMMAND": "填检查服务是否已启动的服务器命令，例如 curl -fsS http://127.0.0.1:8080/actuator/health。",
    "HEALTH_CHECK_TIMEOUT": "填最多等服务启动多久，单位秒，例如 120。",
    "HEALTH_CHECK_INTERVAL": "填每隔多久检查一次，单位秒，例如 3。",
    "HEALTH_CHECK_SUCCESS_COUNT": "填连续检查成功几次才算启动完成，通常填 2。",
    "HEALTH_CHECK_EXPECTED_TEXT": "填健康检查输出中必须包含的文字，例如 UP；不限制输出内容可留空。",
    "HEALTH_CHECK_INSTANCE_COMMAND": "填能输出服务实例标识的命令，用来确认重启后的确是新进程；不需要可留空。",
    "ROLLBACK_COMMAND": "填部署失败、恢复旧文件后要执行的命令；不需要额外回滚命令可留空。",
    "ROLLBACK_PATH": "填执行回滚命令前要进入的服务器目录；不需要切换目录可留空。",
}
_WORKFLOW_PROPERTY_TOOLTIPS = {
    "CONNECTION_NAME": "填本任务前面“连接服务器”步骤中起的连接名，例如 prod。后续上传或远程命令用它指定要操作哪台服务器。",
    "PARAMETER_FILE": _PROPERTY_TOOLTIPS["PARAMETER_FILE"],
    "TARGET_PROJECT_PATH": "填接收合并结果的本地 Git 项目目录；代码会被合并到这里当前所在的目标分支。",
    "SOURCE_PROJECT_PATH": "填提供待合并代码的本地 Git 项目目录；程序会读取它当前所在的分支。",
    "TARGET_BRANCH": _PROPERTY_TOOLTIPS["TARGET_BRANCH"],
    "COMMIT_MESSAGE": "填自动提交暂存修改时使用的提交说明；通常保持默认即可。",
    "PROJECT_PATH": _PROPERTY_TOOLTIPS["PROJECT_PATH"],
    "JAR_FILE": _PROPERTY_TOOLTIPS["JAR_FILE"],
    "ARTIFACT_NAME": "给本次构建出的文件起一个内部名字，例如 artifact；上传步骤用这个名字找到构建结果。",
    "BUILD_TIMEOUT": _PROPERTY_TOOLTIPS["BUILD_TIMEOUT"],
    "COMMAND": "填要执行的命令。例如本地命令可填 mvn clean package；远程命令可填 systemctl restart my-service。",
    "PATH": "填命令执行前要进入的目录；不需要切换目录可留空。",
    "TIMEOUT": "填本步骤最长允许执行多久，单位秒；普通命令通常填 300。",
    "SCRIPT_FILE": "选择 conf/scripts 中已有的脚本文件。不要填写完整路径。",
    "ARGUMENTS": "填传给本地脚本的参数，例如 --profile prod；没有参数可留空。",
    "SOURCE_MODE": "选择上传什么：ARTIFACT 用构建产物，LOCAL_FILE 上传单个文件，LOCAL_FOLDER 上传文件夹。",
    "LOCAL_PATH": "当来源选择 LOCAL_FILE 时，填本地文件所在目录。",
    "FILE_NAME": "当来源选择 LOCAL_FILE 时，填要上传的文件名，例如 application.yml。",
    "FOLDER_PATH": "当来源选择 LOCAL_FOLDER 时，填需要上传的本地文件夹完整路径。",
    "FOLDER_MODE": "选择 INCLUDE_FOLDER 会把文件夹本身上传；CONTENTS_ONLY 只上传文件夹中的内容。",
    "REMOTE_PATH": "填服务器接收文件的目录，例如 /opt/my-service。",
    "CREATE_REMOTE_PATH": "服务器目录不存在时，YES 自动创建；NO 则直接报错，避免写错路径。",
    "BACKUP_ENABLED": "YES 表示上传前先备份服务器上的旧文件；NO 不备份。",
    "BACKUP_PATH": "启用备份时，填服务器上存放旧文件的目录，例如 /opt/backups/my-service。",
    "DELAY": "填脚本执行完成后等待多久再继续，单位秒；不等待填 0。",
    "SECONDS": "填等待时长，单位秒。",
    "INTERVAL": "填每隔多久检查一次，单位秒。",
    "SUCCESS_COUNT": "填连续成功多少次才通过检查，通常填 2。",
}
_WORKFLOW_TYPE_TOOLTIPS = {
    "SERVER_PARAMETER": "连接服务器：读取所选服务器配置，建立一个可供后续上传和远程命令使用的 SSH 连接。",
    "MERGE_BRANCH": "合并分支：把源项目当前分支合并到目标项目当前分支，并提交、推送合并结果。",
    "PUSH_BRANCH": "推送分支：提交本地已暂存的修改，然后推送当前分支到远程仓库。",
    "BUILD": "Maven 打包：在本地项目中执行 Maven 构建，并把生成的 JAR 交给后续上传步骤使用。",
    "LOCAL_COMMAND": "执行本地命令：在当前电脑上执行一条 Windows 命令，例如调用工具或处理文件。",
    "LOCAL_SCRIPT": "执行本地脚本：在当前电脑上运行 conf/scripts 中的脚本文件。",
    "UPLOAD": "上传文件：把构建产物、本地文件或文件夹上传到已连接的服务器；可选择自动建目录和备份旧文件。",
    "REMOTE_COMMAND": "执行远程命令：通过已连接的 SSH 服务器执行一条命令，例如重启服务。",
    "REMOTE_SCRIPT": "执行远程脚本：将 conf/scripts 中的脚本内容发送到服务器并执行，不会在服务器保留脚本文件。",
    "WAIT": "等待：暂停指定的秒数后，再执行下一步。",
    "HEALTH_CHECK": "健康检查：循环执行检查命令，直到服务连续返回成功，或等待超时。",
}


class _WorkerSignals(QObject):
    log = Signal(str)
    status = Signal(str)
    progress = Signal(int, int)
    finished = Signal(str, object)


class _ParameterLogWriter(QObject):
    appended = Signal(object, str, int)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[tuple[Path, str, str, int, datetime] | None] = queue.Queue()
        self._sequence_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._next_sequence = 0
        self._committed_sequences: dict[Path, int] = {}
        self._line_starts: dict[Path, bool] = {}
        self.write_error: str | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="parameter-log-writer",
            daemon=True,
        )
        self._thread.start()

    def append(self, log_path: Path, event_type: str, value: str) -> None:
        with self._sequence_lock:
            if self._closed or not value:
                return
            self._next_sequence += 1
            sequence = self._next_sequence
            self._queue.put((log_path, event_type, value, sequence, datetime.now()))

    def read_text(self, log_path: Path) -> tuple[str, int]:
        with self._io_lock:
            content = log_path.read_text(encoding="utf-8-sig")
            sequence = self._committed_sequences.get(log_path, 0)
        return content, sequence

    def shutdown(self) -> None:
        with self._sequence_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)

    def is_finished(self) -> bool:
        return not self._thread.is_alive()

    @staticmethod
    def _format(event_type: str, value: str) -> str:
        if event_type == "output":
            text = _ANSI_ESCAPE_PATTERN.sub("", value)
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return "".join(
                character
                for character in text
                if character in "\n\t" or ord(character) >= 32
            )
        if event_type == "command":
            return tr("\n[执行命令] {0}\n", value.strip())
        return f"\n[{event_type}] {value.strip()}\n"

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                log_path, event_type, value, sequence, recorded_at = item
                text = self._format(event_type, value)
                if not text:
                    continue
                text, line_start = timestamp_log_text(
                    text, recorded_at, line_start=self._line_starts.get(log_path, True),
                )
                with self._io_lock:
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(text)
                    self._committed_sequences[log_path] = sequence
                    self._line_starts[log_path] = line_start
                self.appended.emit(log_path, text, sequence)
            except (OSError, UnicodeError) as exc:
                self.write_error = str(exc)
                self.failed.emit(self.write_error)
            finally:
                self._queue.task_done()


class _WorkflowGraphicsView(QGraphicsView):
    """Scrollable workflow canvas with cursor-centered wheel zoom."""

    _MIN_ZOOM = 0.35
    _MAX_ZOOM = 4.0
    step_clicked = Signal(int, bool)
    steps_box_selected = Signal(object, bool)
    selection_cleared = Signal()
    step_double_clicked = Signal(int)
    step_context_requested = Signal(int, object)
    step_drag_target_changed = Signal(int, object)
    step_drag_finished = Signal()
    step_move_requested = Signal(int, int)
    save_requested = Signal()
    zoom_changed = Signal(float)

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self._zoom_factor = 1.0
        self._pressed_step: int | None = None
        self._press_position: QPoint | None = None
        self._drag_offset = QPoint()
        self._dragging_step = False
        self._pan_origin: QPoint | None = None
        self._pan_position: QPoint | None = None
        self._panning = False
        self._rubber_origin: QPoint | None = None
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._drag_badge = QLabel(self.viewport())
        self._drag_badge.setStyleSheet(
            "QLabel { background: rgba(255, 247, 237, 220); color: #9a3412; "
            "border: 2px solid #f97316; border-radius: 8px; padding: 4px; }"
        )
        self._drag_badge.setAlignment(Qt.AlignCenter)
        self._drag_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._drag_badge.hide()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setInteractive(False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._set_canvas_cursor(Qt.OpenHandCursor)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            self._pan_origin = event.position().toPoint()
            self._pan_position = self._pan_origin
            self._panning = False
            self.setFocus(Qt.MouseFocusReason)
            self._set_canvas_cursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            if self._pan_origin is not None:
                event.accept()
                return
            self._dragging_step = False
            self._pressed_step = self._step_at(event.position().toPoint())
            self._press_position = event.position().toPoint()
            if self._pressed_step is None:
                self._rubber_origin = self._press_position
                self._rubber_band.setGeometry(
                    QRect(self._rubber_origin, self._rubber_origin)
                )
                self._rubber_band.show()
                self._set_canvas_cursor(Qt.CrossCursor)
            else:
                drag_visual = self._step_visual(self._pressed_step)
                if drag_visual is not None:
                    label, visual_rect = drag_visual
                    self._drag_badge.setText(label)
                    self._drag_badge.setGeometry(visual_rect)
                    self._drag_offset = self._press_position - visual_rect.topLeft()
                self._set_canvas_cursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pan_origin is not None and event.buttons() & Qt.RightButton:
            position = event.position().toPoint()
            if (
                not self._panning
                and (position - self._pan_origin).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._panning = True
            if self._panning and self._pan_position is not None:
                delta = position - self._pan_position
                horizontal = self.horizontalScrollBar()
                vertical = self.verticalScrollBar()
                horizontal.setValue(horizontal.value() - delta.x())
                vertical.setValue(vertical.value() - delta.y())
                self._pan_position = position
            self._set_canvas_cursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if self._rubber_origin is not None and event.buttons() & Qt.LeftButton:
            self._rubber_band.setGeometry(
                QRect(self._rubber_origin, event.position().toPoint()).normalized()
            )
            self._set_canvas_cursor(Qt.CrossCursor)
            event.accept()
            return
        if (
            self._pressed_step is not None
            and self._press_position is not None
            and event.buttons() & Qt.LeftButton
            and (
                event.position().toPoint() - self._press_position
            ).manhattanLength() >= 5
        ):
            self._dragging_step = True
            scroll_bar = self.verticalScrollBar()
            cursor_y = event.position().toPoint().y()
            if cursor_y < 28:
                scroll_bar.setValue(scroll_bar.value() - 22)
            elif cursor_y > self.viewport().height() - 28:
                scroll_bar.setValue(scroll_bar.value() + 22)
            target_step = self._drop_target_at(event.position().toPoint())
            if target_step == self._pressed_step:
                target_step = None
            self.step_drag_target_changed.emit(self._pressed_step, target_step)
            badge_position = event.position().toPoint() - self._drag_offset
            badge_position.setX(min(
                max(4, badge_position.x()),
                self.viewport().width() - self._drag_badge.width() - 4,
            ))
            badge_position.setY(min(
                max(4, badge_position.y()),
                self.viewport().height() - self._drag_badge.height() - 4,
            ))
            self._drag_badge.move(badge_position)
            self._drag_badge.show()
            self._drag_badge.raise_()
            self._set_canvas_cursor(Qt.SizeVerCursor)
            event.accept()
            return
        super().mouseMoveEvent(event)
        if not event.buttons() & Qt.LeftButton:
            self._set_canvas_cursor(
                Qt.ArrowCursor
                if self._step_at(event.position().toPoint()) is not None
                else Qt.OpenHandCursor
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            position = event.position().toPoint()
            clicked = self._pan_origin is not None and not self._panning
            self._pan_origin = None
            self._pan_position = None
            self._panning = False
            self._set_canvas_cursor(
                Qt.ArrowCursor
                if self._step_at(position) is not None
                else Qt.OpenHandCursor
            )
            event.accept()
            if clicked:
                self._request_context_menu(position)
            return
        if event.button() == Qt.LeftButton:
            position = event.position().toPoint()
            if self._dragging_step and self._pressed_step is not None:
                target_step = self._drop_target_at(position)
                source_step = self._pressed_step
                self._drag_badge.hide()
                self.step_drag_finished.emit()
                self._dragging_step = False
                self._pressed_step = None
                self._press_position = None
                self._set_canvas_cursor(
                    Qt.ArrowCursor
                    if self._step_at(position) is not None
                    else Qt.OpenHandCursor
                )
                if target_step is not None and target_step != source_step:
                    self.step_move_requested.emit(source_step, target_step)
                event.accept()
                return
            if self._rubber_origin is not None:
                selection_rect = QRect(self._rubber_origin, position).normalized()
                self._rubber_band.hide()
                self._rubber_origin = None
                additive = bool(event.modifiers() & Qt.ControlModifier)
                if selection_rect.width() < 5 and selection_rect.height() < 5:
                    if not additive:
                        self.selection_cleared.emit()
                else:
                    scene_rect = self.mapToScene(selection_rect).boundingRect()
                    selected_steps = sorted({
                        step_index
                        for item in self.scene().items()
                        for step_index in [item.data(Qt.UserRole)]
                        if isinstance(step_index, int)
                        and item.sceneBoundingRect().intersects(scene_rect)
                    })
                    self.steps_box_selected.emit(selected_steps, additive)
                self._pressed_step = None
                self._press_position = None
                self._set_canvas_cursor(
                    Qt.ArrowCursor
                    if self._step_at(position) is not None
                    else Qt.OpenHandCursor
                )
                event.accept()
                return
            self._set_canvas_cursor(
                Qt.ArrowCursor
                if self._step_at(position) is not None
                else Qt.OpenHandCursor
            )
            if self._press_position is not None and (position - self._press_position).manhattanLength() < 5:
                released_step = self._step_at(position)
                if self._pressed_step is not None and released_step == self._pressed_step:
                    additive = bool(event.modifiers() & Qt.ControlModifier)
                    self.step_clicked.emit(self._pressed_step, additive)
                elif self._pressed_step is None and released_step is None:
                    self.selection_cleared.emit()
            self._pressed_step = None
            self._press_position = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _set_canvas_cursor(self, cursor: Qt.CursorShape) -> None:
        self.setCursor(cursor)
        self.viewport().setCursor(cursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            self.mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            step_index = self._step_at(event.position().toPoint())
            if step_index is not None:
                self.step_double_clicked.emit(step_index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        # Mouse menus are opened on release only when no panning occurred.
        if event.reason() == QContextMenuEvent.Keyboard:
            self._request_context_menu(event.pos())
        event.accept()

    def _request_context_menu(self, position: QPoint) -> None:
        step_index = self._step_at(position)
        if step_index is not None:
            self.step_context_requested.emit(step_index, self.viewport().mapToGlobal(position))

    def _step_at(self, position: QPoint) -> int | None:
        item = self.itemAt(position)
        if item is None:
            return None
        step_index = item.data(Qt.UserRole)
        return step_index if isinstance(step_index, int) else None

    def _step_visual(self, step_index: int) -> tuple[str, QRect] | None:
        candidates = [
            item
            for item in self.scene().items()
            if item.data(Qt.UserRole) == step_index
        ]
        if not candidates:
            return None
        node_item = max(
            candidates,
            key=lambda item: item.sceneBoundingRect().width()
            * item.sceneBoundingRect().height(),
        )
        visual_rect = self.mapFromScene(node_item.sceneBoundingRect()).boundingRect()
        label = node_item.toolTip() or tr("第 {0} 步", step_index)
        return label, visual_rect

    def _drop_target_at(self, position: QPoint) -> int | None:
        step_index = self._step_at(position)
        if step_index is not None:
            return step_index
        scene_y = self.mapToScene(position).y()
        positions: dict[int, float] = {}
        for item in self.scene().items():
            value = item.data(Qt.UserRole)
            if isinstance(value, int):
                positions[value] = item.sceneBoundingRect().center().y()
        preceding = [
            (center_y, value)
            for value, center_y in positions.items()
            if center_y <= scene_y
        ]
        return max(preceding)[1] if preceding else None

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        cursor_position = event.position().toPoint()
        before = self.mapToScene(cursor_position)
        self.zoom(1.15 if delta > 0 else 1 / 1.15)
        after = self.mapToScene(cursor_position)
        self.translate(after.x() - before.x(), after.y() - before.y())
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.Save):
            self.save_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def zoom(self, factor: float) -> None:
        target = max(self._MIN_ZOOM, min(self._MAX_ZOOM, self._zoom_factor * factor))
        applied = target / self._zoom_factor
        if applied != 1:
            self.scale(applied, applied)
            self._zoom_factor = target
            self.zoom_changed.emit(target)

    def fit_scene(self) -> None:
        self.resetTransform()
        scene_width = self.sceneRect().width()
        if scene_width > 0:
            width_scale = max(0.01, (self.viewport().width() - 12) / scene_width)
            self.scale(width_scale, width_scale)
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
        self._zoom_factor = 1.0


class _WorkflowDiagramPanel(QWidget):
    """Embedded visual preview of the ordered workflow steps being edited."""

    _NODE_COLORS = {
        "SERVER_PARAMETER": "#2563eb",
        "BUILD": "#7c3aed",
        "UPLOAD": "#059669",
        "HEALTH_CHECK": "#dc2626",
        "REMOTE_COMMAND": "#d97706",
        "REMOTE_SCRIPT": "#d97706",
    }
    step_clicked = Signal(int)
    selection_changed = Signal(object)
    selection_cleared = Signal()
    step_double_clicked = Signal(int)
    step_context_requested = Signal(int, object)
    step_move_requested = Signal(int, int)
    save_requested = Signal()
    zoom_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.scene = QGraphicsScene(self)
        self.view = _WorkflowGraphicsView(self.scene)
        self._selected_steps: set[int] = set()
        self._drag_source_step: int | None = None
        self._drag_target_step: int | None = None
        self._drag_indicator = None
        self._step_items: dict[int, tuple[object, object, str]] = {}
        self._steps: list[tuple[int, str]] = []
        self._theme_colors = dict(_THEME_PRESETS["white"])
        self.view.step_clicked.connect(self._select_step)
        self.view.steps_box_selected.connect(self._select_steps)
        self.view.selection_cleared.connect(self._clear_selection_from_view)
        self.view.step_double_clicked.connect(self.step_double_clicked)
        self.view.step_context_requested.connect(self._show_step_context_menu)
        self.view.step_drag_target_changed.connect(self._set_drag_target)
        self.view.step_drag_finished.connect(self._finish_step_drag)
        self.view.step_move_requested.connect(self.step_move_requested)
        self.view.save_requested.connect(self.save_requested)
        self.view.zoom_changed.connect(self._zoom_changed)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor(self._theme_colors["background"])))
        layout.addWidget(self.view, 1)
        self.set_steps([])

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._theme_colors = dict(colors)
        self.view.setBackgroundBrush(QBrush(QColor(colors["background"])))
        self.set_steps(self._steps, preserve_view=True)

    def set_zoom(self, zoom: float) -> None:
        self._saved_zoom = min(4.0, max(0.35, zoom))
        self._fit_scene()

    def _zoom_changed(self, zoom: float) -> None:
        self._saved_zoom = zoom
        self.zoom_changed.emit(zoom)

    def set_steps(self, steps: list[tuple[int, str]], preserve_view: bool = False) -> None:
        self._steps = list(steps)
        selected_steps = set(self._selected_steps)
        center = self.view.mapToScene(self.view.viewport().rect().center())
        self.scene.clear()
        self._step_items.clear()
        self._drag_source_step = None
        self._drag_target_step = None
        self._drag_indicator = None
        self._draw(steps)
        self._selected_steps.clear()
        self._set_selected_steps(selected_steps)
        if preserve_view:
            QTimer.singleShot(0, lambda position=center: self.view.centerOn(position))
        else:
            QTimer.singleShot(0, self._fit_scene)

    def _select_step(self, step_index: int, additive: bool) -> None:
        selected_steps = set(self._selected_steps)
        if additive:
            if step_index in selected_steps:
                selected_steps.remove(step_index)
            else:
                selected_steps.add(step_index)
        else:
            selected_steps = {step_index}
        self._set_selected_steps(selected_steps)
        self.step_clicked.emit(step_index)
        self.selection_changed.emit(self.selected_steps())

    def _select_steps(self, step_indexes: object, additive: bool) -> None:
        selected_steps = {
            int(value) for value in step_indexes
        } if isinstance(step_indexes, (list, tuple, set)) else set()
        if additive:
            selected_steps.update(self._selected_steps)
        self._set_selected_steps(selected_steps)
        self.selection_changed.emit(self.selected_steps())

    def selected_step(self) -> int | None:
        steps = self.selected_steps()
        return max(steps) if steps else None

    def selected_steps(self) -> list[int]:
        return sorted(step for step in self._selected_steps if step in self._step_items)

    def clear_selection(self) -> None:
        self._set_selected_steps(set())

    def set_selected_steps(self, step_indexes: list[int] | tuple[int, ...]) -> None:
        self._set_selected_steps(set(step_indexes))
        self.selection_changed.emit(self.selected_steps())

    def _clear_selection_from_view(self) -> None:
        self.clear_selection()
        self.selection_changed.emit([])
        self.selection_cleared.emit()

    def _show_step_context_menu(self, step_index: int, position: object) -> None:
        if step_index not in self._selected_steps:
            self._set_selected_steps({step_index})
            self.selection_changed.emit(self.selected_steps())
        self.step_context_requested.emit(step_index, position)

    def _set_selected_steps(self, step_indexes: set[int]) -> None:
        self._selected_steps = {
            step_index for step_index in step_indexes if step_index in self._step_items
        }
        self._apply_step_styles()

    def _set_drag_target(self, source_step: int, target_step: object) -> None:
        self._drag_source_step = source_step
        self._drag_target_step = target_step if isinstance(target_step, int) else None
        self._apply_step_styles()

    def _finish_step_drag(self) -> None:
        self._drag_source_step = None
        self._drag_target_step = None
        self._apply_step_styles()

    def _apply_step_styles(self) -> None:
        dark_theme = self._theme_colors == _THEME_PRESETS["dark"]
        if self._drag_indicator is not None:
            self.scene.removeItem(self._drag_indicator)
            self._drag_indicator = None
        for step_index, (rect, text, color) in self._step_items.items():
            if step_index == self._drag_target_step:
                rect.setPen(QPen(QColor("#d97706"), 3))
                rect.setBrush(QBrush(QColor("#fef3c7")))
                text.setDefaultTextColor(QColor("#92400e"))
            elif step_index in self._selected_steps:
                rect.setPen(QPen(QColor("#7fa7dc" if dark_theme else "#2563eb"), 3))
                rect.setBrush(QBrush(QColor(self._theme_colors["selection"] if dark_theme else "#dbeafe")))
                text.setDefaultTextColor(QColor(self._theme_colors["selection_text"] if dark_theme else "#1d4ed8"))
            else:
                rect.setPen(QPen(QColor(color), 2))
                rect.setBrush(QBrush(QColor(self._theme_colors["surface"])))
                text.setDefaultTextColor(QColor(color))
            opacity = 0.42 if step_index == self._drag_source_step else 1.0
            rect.setOpacity(opacity)
            text.setOpacity(opacity)
        if self._drag_target_step in self._step_items:
            target_rect = self._step_items[self._drag_target_step][0].sceneBoundingRect()
            indicator_y = target_rect.bottom() + 24
            indicator_pen = QPen(QColor("#f97316"), 4)
            indicator_pen.setCapStyle(Qt.RoundCap)
            self._drag_indicator = self.scene.addLine(
                target_rect.left() + 12,
                indicator_y,
                target_rect.right() - 12,
                indicator_y,
                indicator_pen,
            )
            self._drag_indicator.setZValue(20)

    def _draw(self, steps: list[tuple[int, str]]) -> None:
        node_width, node_height = 360, 64
        center_x, top, spacing = 380, 50, 48
        dark_theme = QColor(self._theme_colors["background"]).lightness() < 128
        nodes: list[tuple[str, str, int | None]] = [
            (tr("开始"), "#2dd4bf" if dark_theme else "#0f766e", None)
        ]
        for index, step_type in steps:
            definition = WORKFLOW_TYPE_BY_KEY.get(step_type)
            label = tr(definition.label) if definition is not None else step_type
            fallback_color = self._theme_colors["foreground"] if dark_theme else "#475569"
            nodes.append((
                tr("第 {0} 步：{1}", index, label),
                self._NODE_COLORS.get(step_type, fallback_color), index,
            ))
        nodes.append((tr("结束"), self._theme_colors["foreground"], None))

        connector_color = self._theme_colors["muted"]
        pen = QPen(QColor(connector_color), 2)
        for index in range(len(nodes) - 1):
            start_y = top + index * (node_height + spacing) + node_height
            end_y = start_y + spacing
            self.scene.addLine(center_x, start_y, center_x, end_y - 9, pen)
            arrow = QPolygonF([
                QPointF(center_x, end_y),
                QPointF(center_x - 7, end_y - 10),
                QPointF(center_x + 7, end_y - 10),
            ])
            self.scene.addPolygon(
                arrow, QPen(QColor(connector_color)), QBrush(QColor(connector_color))
            )

        for index, (label, color, step_index) in enumerate(nodes):
            y = top + index * (node_height + spacing)
            path = QPainterPath()
            path.addRoundedRect(center_x - node_width / 2, y, node_width, node_height, 10, 10)
            rect = self.scene.addPath(
                path, QPen(QColor(color), 2),
                QBrush(QColor(self._theme_colors["surface"])),
            )
            text = self.scene.addText(label, QFont("Microsoft YaHei", 10))
            rect.setCursor(Qt.ArrowCursor)
            text.setCursor(Qt.ArrowCursor)
            text.setDefaultTextColor(QColor(color))
            text.setPos(center_x - text.boundingRect().width() / 2, y + 19)
            rect.setToolTip(label)
            if step_index is not None:
                rect.setData(Qt.UserRole, step_index)
                text.setData(Qt.UserRole, step_index)
                self._step_items[step_index] = (rect, text, color)

        bottom = top + len(nodes) * (node_height + spacing)
        self.scene.setSceneRect(0, 0, 760, bottom + 40)

    def _fit_scene(self) -> None:
        blocker = QSignalBlocker(self.view)
        self.view.fit_scene()
        if getattr(self, "_saved_zoom", 1.0) != 1.0:
            self.view.zoom(self._saved_zoom)
        del blocker


class _SquareCheckBox(QCheckBox):
    def paintEvent(self, _event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().color(
            QPalette.Text if self.isEnabled() else QPalette.PlaceholderText
        )
        top = max(0, (self.height() - 15) // 2)
        box = QRect(1, top, 14, 14)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(self.palette().color(QPalette.Base)))
        painter.drawRect(box)
        if self.isChecked():
            painter.setPen(QPen(color, 2))
            painter.drawLine(4, top + 7, 7, top + 10)
            painter.drawLine(7, top + 10, 13, top + 4)
        painter.setPen(color)
        painter.drawText(
            QRect(22, 0, max(0, self.width() - 22), self.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.text(),
        )


class _ServerParameterForm(QScrollArea):
    """Visual editor backed by the existing server parameter text format."""

    content_changed = Signal()
    direct_connect_requested = Signal(str, str)
    _FIELDS = (
        ("IP_ADDRESS", "服务器地址", "例如 192.168.1.20 或 deploy.example.com"),
        ("PORT", "SSH 端口", "通常为 22"),
        ("USERNAME", "登录账号", "例如 root 或 deploy"),
        ("PASSWORD", "登录密码", "使用私钥登录时可以留空"),
        ("KEY_FILENAME", "SSH 私钥", "本地私钥文件的完整路径，可留空"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._ip_hiding = False
        self._ip_masked = False
        self._ip_address = ""
        self._selected_path_row: QFrame | None = None
        self._path_row_targets: dict[QObject, QFrame] = {}
        self._path_rows: list[
            tuple[QFrame, QLabel, QCheckBox, QCheckBox, QLineEdit, QLineEdit]
        ] = []
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(14)

        connection_card = QFrame()
        connection_card.setObjectName("parameterCard")
        connection_layout = QVBoxLayout(connection_card)
        connection_layout.addWidget(QLabel(tr("服务器连接")))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self.controls: dict[str, QLineEdit] = {}
        self.field_labels: dict[str, QLabel] = {}
        for key, label_text, placeholder in self._FIELDS:
            if key == "PASSWORD":
                auth_mode = QFrame()
                self.auth_mode = auth_mode
                auth_mode.setObjectName("authModeToggle")
                auth_mode.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                auth_mode.setFixedHeight(30)
                auth_layout = QHBoxLayout(auth_mode)
                auth_layout.setContentsMargins(0, 0, 0, 0)
                auth_layout.setSpacing(0)
                self.password_auth_button = QPushButton(tr("密码登录"))
                self.password_auth_button.setObjectName("passwordAuthButton")
                self.key_auth_button = QPushButton(tr("私钥登录"))
                self.key_auth_button.setObjectName("keyAuthButton")
                for button in (self.password_auth_button, self.key_auth_button):
                    button.setCheckable(True)
                    button.setFixedSize(max(88, button.fontMetrics().horizontalAdvance(button.text()) + 24), 28)
                self.auth_button_group = QButtonGroup(self)
                self.auth_button_group.setExclusive(True)
                self.auth_button_group.addButton(self.password_auth_button)
                self.auth_button_group.addButton(self.key_auth_button)
                self.password_auth_button.setChecked(True)
                self.password_auth_button.toggled.connect(self._auth_mode_changed)
                self.key_auth_button.toggled.connect(self._auth_mode_changed)
                auth_layout.addWidget(self.password_auth_button)
                auth_layout.addWidget(self.key_auth_button)
                form.addRow(tr("认证方式"), auth_mode)
            label = QLabel(tr(label_text))
            self.field_labels[key] = label
            tooltip = tr(_PROPERTY_TOOLTIPS.get(key, ""))
            label.setToolTip(tooltip)
            control = QLineEdit()
            control.setPlaceholderText(tr(placeholder))
            control.setToolTip(tooltip)
            control.textChanged.connect(self._notify_changed)
            if key == "IP_ADDRESS":
                control.editingFinished.connect(self._refresh_ip_display)
            self.controls[key] = control
            form.addRow(label, control)
        connection_layout.addLayout(form)
        root.addWidget(connection_card)

        paths_card = QFrame()
        paths_card.setObjectName("parameterCard")
        paths_root = QVBoxLayout(paths_card)
        paths_header = QHBoxLayout()
        paths_header.addWidget(QLabel(tr("默认访问位置")))
        paths_header.addStretch(1)
        add_path_button = QPushButton(tr("增加路径"))
        add_path_button.clicked.connect(lambda: self._add_path_row(notify=True))
        paths_header.addWidget(add_path_button)
        paths_root.addLayout(paths_header)
        hint = QLabel(tr("连接 SSH 时进入所选目录，并按需自动执行对应命令。命令可以留空。"))
        hint.setObjectName("parameterHint")
        paths_root.addWidget(hint)
        self.paths_layout = QVBoxLayout()
        self.paths_layout.setSpacing(8)
        paths_root.addLayout(self.paths_layout)
        root.addWidget(paths_card)
        root.addStretch(1)
        self.setWidget(page)
        self._theme_colors = dict(_THEME_PRESETS["white"])
        self.apply_theme(self._theme_colors)

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._theme_colors = dict(colors)
        background = colors["background"]
        surface = colors["surface"]
        foreground = colors["foreground"]
        muted = colors["muted"]
        border = colors["border"]
        selection = colors["selection"]
        selection_text = colors["selection_text"]
        self.setStyleSheet(
            f"QScrollArea {{ background:{background}; border:1px solid {border}; }}"
            f"QWidget {{ background:{background}; color:{foreground}; }}"
            f"QFrame#parameterCard {{ background:{surface}; border:1px solid {border}; border-radius:6px; }}"
            f"QFrame#parameterCard QLabel {{ background:{surface}; color:{foreground}; }}"
            f"QFrame#parameterPathRow {{ background:{surface}; border:1px solid transparent; border-radius:4px; }}"
            f"QFrame#parameterPathRow[selected=\"true\"] {{ background:{selection}; border:1px solid #2563eb; }}"
            f"QFrame#parameterPathRow[selected=\"true\"] QLabel {{ background:{selection}; color:{selection_text}; }}"
            f"QLabel#parameterHint {{ color:{muted}; }}"
            f"QLineEdit {{ min-height:26px; padding:2px 7px; color:{foreground}; background:{surface}; border:1px solid {border}; border-radius:4px; }}"
            f"QCheckBox#pathOption {{ color:{foreground}; background:transparent; border:0; padding:2px 4px; }}"
        )
        if hasattr(self, "auth_mode"):
            self.auth_mode.setStyleSheet(
                f"QFrame#authModeToggle {{ background:{surface}; border:1px solid {border}; border-radius:4px; }}"
                f"QPushButton {{ border:0; color:{foreground}; background:{surface}; }}"
                f"QPushButton#passwordAuthButton {{ border-right:1px solid {border}; border-top-left-radius:3px; border-bottom-left-radius:3px; }}"
                "QPushButton#keyAuthButton { border-top-right-radius:3px; border-bottom-right-radius:3px; }"
                f"QPushButton:checked {{ background:{selection}; color:{selection_text}; font-weight:600; }}"
            )

    def _menu_style(self) -> str:
        colors = self._theme_colors
        return (
            f"QMenu {{ background:{colors['surface']}; color:{colors['foreground']}; border:1px solid {colors['border']}; }}"
            "QMenu::item { background:transparent; padding:6px 24px; }"
            f"QMenu::item:selected {{ background:{colors['selection']}; color:{colors['selection_text']}; }}"
            f"QMenu::item:disabled {{ color:{colors['muted']}; }}"
        )

    def set_ip_hiding(self, enabled: bool) -> None:
        self._ip_hiding = enabled
        self._refresh_ip_display()

    def _refresh_ip_display(self) -> None:
        control = self.controls["IP_ADDRESS"]
        if not self._ip_masked:
            self._ip_address = control.text()
        displayed = mask_ip_address(self._ip_address.strip(), self._ip_hiding)
        self._ip_masked = displayed != self._ip_address.strip()
        blocker = QSignalBlocker(control)
        control.setText(displayed if self._ip_masked else self._ip_address)
        control.setReadOnly(self._ip_masked)
        control.setToolTip(
            tr("关闭系统设置中的 IP 隐藏后可修改服务器地址。")
            if self._ip_masked else tr(_PROPERTY_TOOLTIPS["IP_ADDRESS"])
        )
        del blocker

    def set_content(self, content: str) -> None:
        values: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().upper()] = value.strip()

        self._loading = True
        self._ip_masked = False
        for key, _label, _placeholder in self._FIELDS:
            self.controls[key].setText(values.get(key, "22" if key == "PORT" else ""))
        self.controls["PASSWORD"].setReadOnly(
            values.get("PASSWORD", "") == _HIDDEN_PASSWORD_VALUE
        )
        auth_method = values.get("AUTH_METHOD", "").strip().upper()
        use_private_key = auth_method == "KEY" or (
            auth_method not in {"PASSWORD", "KEY"}
            and bool(values.get("KEY_FILENAME", "").strip())
        )
        self.key_auth_button.setChecked(use_private_key)
        self.password_auth_button.setChecked(not use_private_key)
        self._update_auth_field_visibility()
        self._clear_path_rows()
        entries: list[tuple[int, str, str, bool, bool]] = []
        legacy_path = values.get("DEFAULT_OPEN_PATH", "")
        legacy_command = values.get("DEFAULT_OPEN_COMMAND", "")
        if legacy_path or legacy_command:
            command_enabled_value = values.get("DEFAULT_OPEN_COMMAND_ENABLED", "")
            entries.append((
                0,
                legacy_path,
                legacy_command,
                values.get("DEFAULT_OPEN_PATH_SELECTED", "").upper() == "YES",
                command_enabled_value.upper() == "YES"
                if command_enabled_value else bool(legacy_command),
            ))
        indexed: dict[int, dict[str, str]] = {}
        for key, value in values.items():
            path_match = re.fullmatch(r"DEFAULT_OPEN_PATH_(\d+)", key)
            command_match = re.fullmatch(r"DEFAULT_OPEN_COMMAND_(\d+)", key)
            selected_match = re.fullmatch(r"DEFAULT_OPEN_PATH_SELECTED_(\d+)", key)
            enabled_match = re.fullmatch(r"DEFAULT_OPEN_COMMAND_ENABLED_(\d+)", key)
            if path_match:
                indexed.setdefault(int(path_match.group(1)), {})["path"] = value
            elif command_match:
                indexed.setdefault(int(command_match.group(1)), {})["command"] = value
            elif selected_match:
                indexed.setdefault(int(selected_match.group(1)), {})["selected"] = value
            elif enabled_match:
                indexed.setdefault(int(enabled_match.group(1)), {})["enabled"] = value
        for index, pair in sorted(indexed.items()):
            command = pair.get("command", "")
            enabled_value = pair.get("enabled", "")
            entries.append((
                index,
                pair.get("path", ""),
                command,
                pair.get("selected", "").upper() == "YES",
                enabled_value.upper() == "YES" if enabled_value else bool(command),
            ))
        for _index, path, command, selected, command_enabled in entries:
            self._add_path_row(path, command, selected, command_enabled)
        self._refresh_ip_display()
        self._loading = False

    def to_content(self) -> str:
        auth_method = "KEY" if self.key_auth_button.isChecked() else "PASSWORD"
        lines = [
            "# 服务器连接配置",
            f"IP_ADDRESS={(self._ip_address if self._ip_masked else self.controls['IP_ADDRESS'].text()).strip()}",
            f"PORT={self.controls['PORT'].text().strip()}",
            f"USERNAME={self.controls['USERNAME'].text().strip()}",
            f"AUTH_METHOD={auth_method}",
            f"PASSWORD={self.controls['PASSWORD'].text()}",
            f"KEY_FILENAME={self.controls['KEY_FILENAME'].text().strip()}",
            "",
            "# 手动连接 SSH 时使用的默认访问位置和可选命令",
        ]
        for index, (_row, _label, selected, command_enabled, path, command) in enumerate(
            self._path_rows, start=1
        ):
            lines.append(f"DEFAULT_OPEN_PATH_{index}={path.text().strip()}")
            lines.append(
                f"DEFAULT_OPEN_PATH_SELECTED_{index}={'YES' if selected.isChecked() else 'NO'}"
            )
            lines.append(f"DEFAULT_OPEN_COMMAND_{index}={command.text().strip()}")
            lines.append(
                f"DEFAULT_OPEN_COMMAND_ENABLED_{index}={'YES' if command_enabled.isChecked() else 'NO'}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def _add_path_row(
        self,
        path: str = "",
        command: str = "",
        selected: bool = False,
        command_enabled: bool = False,
        notify: bool = False,
    ) -> None:
        row = QFrame()
        row.setObjectName("parameterPathRow")
        row.setProperty("selected", False)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 5, 6, 5)
        label = QLabel()
        selected_check = _SquareCheckBox(tr("默认路径"))
        selected_check.setObjectName("pathOption")
        selected_check.setChecked(selected)
        command_enabled_check = _SquareCheckBox(tr("执行命令"))
        command_enabled_check.setObjectName("pathOption")
        has_command = bool(command.strip())
        command_enabled_check.setChecked(command_enabled and has_command)
        command_enabled_check.setEnabled(has_command)
        path_entry = QLineEdit(path)
        path_entry.setPlaceholderText(tr("服务器目录，例如 /opt/app"))
        path_entry.setToolTip(tr(_PROPERTY_TOOLTIPS["DEFAULT_OPEN_PATH"]))
        command_entry = QLineEdit(command)
        command_entry.setPlaceholderText(tr("进入目录后自动执行的命令，可留空"))
        command_entry.setToolTip(tr(_PROPERTY_TOOLTIPS["DEFAULT_OPEN_COMMAND"]))
        direct_connect_button = QPushButton(tr("直接连接"))
        direct_connect_button.setEnabled(bool(path.strip()))
        direct_connect_button.clicked.connect(
            lambda _checked=False, target=row: self._request_path_row_connection(target)
        )
        remove_button = QPushButton(tr("删除"))
        remove_button.clicked.connect(lambda _checked=False, target=row: self._remove_path_row(target))
        path_entry.textChanged.connect(
            lambda value, target=direct_connect_button: self._path_value_changed(
                target, value
            )
        )
        command_entry.textChanged.connect(
            lambda value, target=command_enabled_check: self._path_command_changed(
                target, value
            )
        )
        selected_check.toggled.connect(
            lambda checked, target=selected_check: self._default_path_toggled(target, checked)
        )
        command_enabled_check.toggled.connect(self._notify_changed)
        layout.addWidget(label)
        layout.addWidget(selected_check)
        layout.addWidget(command_enabled_check)
        layout.addWidget(path_entry, 1)
        layout.addSpacing(8)
        layout.addWidget(command_entry, 1)
        layout.addWidget(direct_connect_button)
        layout.addWidget(remove_button)
        for target in (
            row,
            label,
            selected_check,
            command_enabled_check,
            path_entry,
            command_entry,
            direct_connect_button,
            remove_button,
        ):
            target.installEventFilter(self)
            self._path_row_targets[target] = row
        self.paths_layout.addWidget(row)
        self._path_rows.append((
            row, label, selected_check, command_enabled_check, path_entry, command_entry
        ))
        self._renumber_path_rows()
        if selected:
            self._default_path_toggled(selected_check, True)
        if notify:
            self._notify_changed()

    def _remove_path_row(self, target: QFrame) -> None:
        for row_data in list(self._path_rows):
            if row_data[0] is target:
                self._path_rows.remove(row_data)
                for watched, row in list(self._path_row_targets.items()):
                    if row is target:
                        self._path_row_targets.pop(watched, None)
                if self._selected_path_row is target:
                    self._selected_path_row = None
                target.deleteLater()
                break
        self._renumber_path_rows()
        self._notify_changed()

    def _clear_path_rows(self) -> None:
        for row, _label, _selected, _enabled, _path, _command in self._path_rows:
            row.deleteLater()
        self._path_rows.clear()
        self._path_row_targets.clear()
        self._selected_path_row = None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        row = self._path_row_targets.get(watched)
        if row is not None:
            if event.type() == QEvent.MouseButtonPress:
                if event.button() in (Qt.LeftButton, Qt.RightButton):
                    self._select_path_row(row)
            elif event.type() == QEvent.MouseButtonDblClick:
                if event.button() == Qt.LeftButton:
                    self._select_path_row(row)
                    self._request_path_row_connection(row)
                    return True
            elif event.type() == QEvent.ContextMenu:
                self._select_path_row(row)
                self._show_path_row_context_menu(row, event.globalPos())
                return True
        return super().eventFilter(watched, event)

    def _select_path_row(self, row: QFrame) -> None:
        if self._selected_path_row is row:
            return
        if self._selected_path_row is not None:
            self._selected_path_row.setProperty("selected", False)
            self._refresh_path_row_style(self._selected_path_row)
        self._selected_path_row = row
        row.setProperty("selected", True)
        self._refresh_path_row_style(row)

    @staticmethod
    def _refresh_path_row_style(row: QFrame) -> None:
        row.style().unpolish(row)
        row.style().polish(row)
        row.update()

    def _show_path_row_context_menu(self, row: QFrame, position: QPoint) -> None:
        for target, _label, _selected, _enabled, path, command in self._path_rows:
            if target is not row:
                continue
            menu = QMenu(self)
            menu.setStyleSheet(self._menu_style())
            connect_action = menu.addAction(tr("直接连接"))
            connect_action.setEnabled(bool(path.text().strip()))
            connect_action.triggered.connect(
                lambda _checked=False, target=row:
                self._request_path_row_connection(target)
            )
            menu.exec(position)
            return

    def _request_path_row_connection(self, row: QFrame) -> None:
        for target, _label, _selected, command_enabled, path, command in self._path_rows:
            if target is not row:
                continue
            command_value = command.text().strip() if command_enabled.isChecked() else ""
            self.direct_connect_requested.emit(path.text().strip(), command_value)
            return

    def _renumber_path_rows(self) -> None:
        for index, (_row, label, _selected, _enabled, _path, _command) in enumerate(
            self._path_rows, start=1
        ):
            label.setText(tr("位置 {0}", index))

    def _default_path_toggled(self, target: QCheckBox, checked: bool) -> None:
        if checked:
            for _row, _label, selected, _enabled, _path, _command in self._path_rows:
                if selected is target:
                    continue
                blocker = QSignalBlocker(selected)
                selected.setChecked(False)
                del blocker
        self._notify_changed()

    def _path_command_changed(self, target: QCheckBox, value: str) -> None:
        has_command = bool(value.strip())
        if not has_command and target.isChecked():
            blocker = QSignalBlocker(target)
            target.setChecked(False)
            del blocker
        target.setEnabled(has_command)
        self._notify_changed()

    def _path_value_changed(self, target: QPushButton, value: str) -> None:
        target.setEnabled(bool(value.strip()))
        self._notify_changed()

    def _notify_changed(self, _value: object = None) -> None:
        if not self._loading:
            self.content_changed.emit()

    def _auth_mode_changed(self, checked: bool) -> None:
        if not checked:
            return
        self._update_auth_field_visibility()
        self._notify_changed()

    def _update_auth_field_visibility(self) -> None:
        password_login = self.password_auth_button.isChecked()
        self.field_labels["PASSWORD"].setVisible(password_login)
        self.controls["PASSWORD"].setVisible(password_login)
        self.field_labels["KEY_FILENAME"].setVisible(not password_login)
        self.controls["KEY_FILENAME"].setVisible(not password_login)


class _PasswordDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, prompt: str, require_confirmation: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt))
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.Password)
        row.addWidget(self.entry, 1)
        reveal = QPushButton("👁")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda checked: self.entry.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        row.addWidget(reveal)
        layout.addLayout(row)
        self.confirmation_entry: QLineEdit | None = None
        if require_confirmation:
            layout.addWidget(QLabel(tr("确认密码：")))
            confirmation_row = QHBoxLayout()
            self.confirmation_entry = QLineEdit()
            self.confirmation_entry.setEchoMode(QLineEdit.Password)
            confirmation_row.addWidget(self.confirmation_entry, 1)
            confirmation_reveal = QPushButton("👁")
            confirmation_reveal.setCheckable(True)
            confirmation_reveal.toggled.connect(
                lambda checked: self.confirmation_entry.setEchoMode(
                    QLineEdit.Normal if checked else QLineEdit.Password
                )
            )
            confirmation_row.addWidget(confirmation_reveal)
            layout.addLayout(confirmation_row)
            self.confirmation_entry.returnPressed.connect(self.accept)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.entry.returnPressed.connect(self.accept)
        self.resize(380, self.sizeHint().height())

    def accept(self) -> None:
        if self.confirmation_entry is not None:
            value = self.entry.text().strip()
            confirmation = self.confirmation_entry.text().strip()
            if not value:
                QMessageBox.warning(self, tr("密码无效"), tr("管理员密码不能为空"))
                self.entry.setFocus()
                return
            if not confirmation:
                QMessageBox.warning(self, tr("请确认密码"), tr("请再次输入管理员密码"))
                self.confirmation_entry.setFocus()
                return
            if not hmac.compare_digest(value.encode(), confirmation.encode()):
                QMessageBox.warning(self, tr("密码不一致"), tr("两次输入的管理员密码不一致"))
                self.confirmation_entry.setFocus()
                self.confirmation_entry.selectAll()
                return
        super().accept()


class ApplicationWindow(QMainWindow):
    def __init__(
        self,
        task_dir: Path,
        parameter_dir: Path,
        script_dir: Path,
        parameter_template_path: Path,
        script_template_path: Path,
    ) -> None:
        super().__init__()
        self.task_dir = task_dir.resolve()
        self.parameter_dir = parameter_dir.resolve()
        self.script_dir = script_dir.resolve()
        configuration_root = self.task_dir.parent
        if (
            self.task_dir.name.lower() != "tasks"
            or self.parameter_dir != (configuration_root / "host").resolve()
            or self.script_dir != (configuration_root / "scripts").resolve()
        ):
            raise ValueError(
                tr("任务、配置和脚本目录必须位于同一个 conf 目录下，并分别命名为 "
                "tasks、host、scripts")
            )
        self.draft_root = configuration_root / ".drafts"
        self.history_root = configuration_root / ".history"
        self.execution_log_root = configuration_root / ".logs"
        self.execution_log_root.mkdir(parents=True, exist_ok=True)
        self.settings_path = configuration_root / "settings.json"
        self.parameter_template_path = parameter_template_path.resolve()
        self.script_template_path = script_template_path.resolve()
        self.application_settings = self._read_settings()
        language = initialize(self.application_settings.get("language"))
        install_qt_translations(QApplication.instance())
        if self.application_settings.get("language") != language:
            self._write_settings({**self.application_settings, "language": language})
        self.editor_font_size = self._bounded_int("editor_font_size", 10, 8, 24)
        self.auto_save_delay_seconds = self._bounded_float(
            "auto_save_delay_seconds", 1.0, 0.5, 30.0
        )
        self.workflow_zoom = self._bounded_float("workflow_zoom", 1.0, 0.35, 4.0)
        self.ssh_monitor_panel_width = self._bounded_int(
            "ssh_monitor_panel_width", 275, 220, 400
        )
        self.ssh_console_height = self._bounded_int(
            "ssh_console_height", 380, 160, 900
        )
        self.file_sidebar_width = self._bounded_int(
            "file_sidebar_width", 250, 190, 700
        )
        saved_workflow_zooms = self.application_settings.get("workflow_task_zooms", {})
        self.workflow_task_zooms: dict[str, float] = {}
        if isinstance(saved_workflow_zooms, dict):
            for task_name, zoom in saved_workflow_zooms.items():
                try:
                    self.workflow_task_zooms[str(task_name)] = min(4.0, max(0.35, float(zoom)))
                except (TypeError, ValueError):
                    continue
        startup_page = str(self.application_settings.get("startup_page", "last"))
        saved_view = str(self.application_settings.get("view_mode", "task"))
        self.view_mode = startup_page if startup_page in {"task", "parameter", "script", "log"} else saved_view
        if self.view_mode not in {"task", "parameter", "script", "log"}:
            self.view_mode = "task"
        saved_files = self.application_settings.get("selected_files", {})
        self.last_selected_files = (
            {key: str(value) for key, value in saved_files.items() if key in {"task", "parameter", "script", "log"} and value}
            if isinstance(saved_files, dict) else {}
        )
        self.current_path: Path | None = None
        self._structured_workflow_steps: list[dict[str, object]] | None = None
        self._dirty = False
        self._loading_editor = False
        self._changing_selection = False
        self._deploying = False
        self._stop_requested = False
        self._execution_cancel_event = threading.Event()
        self._interaction_panel_user_hidden = False
        self._ssh_tool_mode = False
        self._ssh_tool_restore_main_sizes: list[int] = []
        self._ssh_tool_restore_right_sizes: list[int] = []
        self._ssh_tool_restore_interaction_visible = False
        self._password_session_unlocked = False
        self._parameter_password_visible = False
        self._parameter_password_ciphertext: str | None = None
        self._visible_parameter_password: str | None = None
        self._active_execution_log: Path | None = None
        self._active_execution_task_path: Path | None = None
        self._active_parameter_log: Path | None = None
        self._active_parameter_path: Path | None = None
        self._displayed_log_sequences: dict[Path, int] = {}
        self._execution_log_write_failed = False
        self.ssh_tabs: dict[Path, list[QtSSHTerminalTab]] = {}
        self._ssh_tab_sequence: dict[Path, int] = {}
        self._ssh_tab_names: dict[QtSSHTerminalTab, str] = {}
        self.worker_signals = _WorkerSignals(self)
        self.worker_signals.log.connect(self._append_log)
        self.worker_signals.status.connect(self._set_worker_status)
        self.worker_signals.progress.connect(self._update_progress)
        self.worker_signals.finished.connect(self._worker_finished)
        self._parameter_log_writer = _ParameterLogWriter(self)
        self._parameter_log_writer.appended.connect(self._parameter_log_appended)
        self._parameter_log_writer.failed.connect(self._parameter_log_failed)
        self._closing_for_logs = False
        self._log_shutdown_timer = QTimer(self)
        self._log_shutdown_timer.setInterval(50)
        self._log_shutdown_timer.timeout.connect(self._poll_log_shutdown)
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.setSingleShot(True)
        self.auto_save_timer.timeout.connect(self._write_current_draft)
        self.history_timer = QTimer(self)
        self.history_timer.setSingleShot(True)
        self.history_timer.setInterval(5 * 60 * 1000)
        self.history_timer.timeout.connect(self._write_current_history)
        self.layout_save_timer = QTimer(self)
        self.layout_save_timer.setSingleShot(True)
        self.layout_save_timer.setInterval(400)
        self.layout_save_timer.timeout.connect(self._save_layout_dimensions)
        self.setWindowTitle(tr("DeployFlow 自动部署工具"))
        self.setMinimumSize(800, 520)
        self.resize(1100, 720)
        self._create_widgets()
        self._apply_theme()
        self._restore_window_state()
        self.switch_view(self.view_mode, initial=True)

    def _create_widgets(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.file_buttons: list[QPushButton] = []
        for text, handler in (
            (tr("新建"), self.create_file),
            (tr("重命名"), self.rename_file),
            (tr("复制"), self.copy_file),
            (tr("删除"), self.delete_file),
            (tr("保存"), self.save_text),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
            self.file_buttons.append(button)
        toolbar.addStretch(1)
        self.password_button = QPushButton(tr("开启隐藏密码"))
        self.password_button.clicked.connect(self._toggle_password_visibility)
        toolbar.addWidget(self.password_button)
        self.connect_button = QPushButton(tr("连接"))
        self.connect_button.clicked.connect(self._toggle_ssh_connection)
        toolbar.addWidget(self.connect_button)
        self.interaction_button = QPushButton(tr("显示交互窗口"))
        self.interaction_button.clicked.connect(self._toggle_interaction_panel)
        toolbar.addWidget(self.interaction_button)
        self.execute_button = QPushButton(tr("执行"))
        self.execute_button.clicked.connect(self._execute_selected_or_all)
        toolbar.addWidget(self.execute_button)
        root_layout.addLayout(toolbar)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("fileSidebar")
        self.sidebar.setMinimumWidth(190)
        side_layout = QHBoxLayout(self.sidebar)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        nav_layout = QVBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        self.nav_buttons: dict[str, QPushButton] = {}
        for mode, text in (("task", tr("任务")), ("parameter", tr("配置")), ("script", tr("脚本")), ("log", tr("日志"))):
            button = QPushButton(text)
            button.setObjectName("viewModeButton")
            button.setCheckable(True)
            button.setFixedWidth(68)
            button.clicked.connect(lambda _checked=False, value=mode: self.switch_view(value))
            nav_layout.addWidget(button)
            self.nav_buttons[mode] = button
        nav_layout.addStretch(1)
        self.language_button = QPushButton(tr("语言"))
        self.language_button.setObjectName("settingsButton")
        self.language_button.setIconSize(QSize(16, 16))
        self.language_button.clicked.connect(self._show_language_settings)
        nav_layout.addWidget(self.language_button)
        self.theme_button = QPushButton(tr("主题"))
        self.theme_button.setObjectName("settingsButton")
        self.theme_button.setFixedWidth(68)
        self.theme_button.setIconSize(QSize(16, 16))
        self.theme_button.clicked.connect(lambda _checked=False: self._show_settings("theme"))
        nav_layout.addWidget(self.theme_button)
        self.settings_button = QPushButton(tr("设置"))
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setFixedWidth(68)
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.clicked.connect(lambda _checked=False: self._show_settings("general"))
        nav_layout.addWidget(self.settings_button)
        navigation_buttons = [*self.nav_buttons.values(), self.language_button, self.theme_button, self.settings_button]
        navigation_width = max(68, *(button.fontMetrics().horizontalAdvance(button.text()) + 40 for button in navigation_buttons))
        for button in navigation_buttons:
            button.setFixedWidth(navigation_width)
        side_layout.addLayout(nav_layout)
        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.file_list.setDefaultDropAction(Qt.MoveAction)
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDropIndicatorShown(True)
        self.file_list.setSelectionMode(
            QAbstractItemView.ExtendedSelection
            if self.view_mode == "log"
            else QAbstractItemView.SingleSelection
        )
        self.select_all_logs_action = QAction(self.file_list)
        self.select_all_logs_action.setShortcut(QKeySequence.SelectAll)
        self.select_all_logs_action.setShortcutContext(Qt.WidgetShortcut)
        self.select_all_logs_action.setEnabled(self.view_mode == "log")
        self.select_all_logs_action.triggered.connect(
            lambda _checked=False: self.file_list.selectAll()
        )
        self.file_list.addAction(self.select_all_logs_action)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_file_context_menu)
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        self.file_list.model().rowsMoved.connect(self._save_file_order)
        side_layout.addWidget(self.file_list, 1)
        self.sidebar.setStyleSheet(
            "QFrame#fileSidebar { background:#e5e7eb; border:1px solid #4b5563; }"
            "QPushButton#viewModeButton { background:#e5e7eb; border:0; color:#111827; "
            "font-weight:600; padding:8px; text-align:left; }"
            "QPushButton#viewModeButton:hover { background:#f3f4f6; }"
            "QPushButton#viewModeButton:checked { background:#ffffff; }"
            "QPushButton#viewModeButton:disabled { color:#6b7280; }"
            "QPushButton#settingsButton { background:#e5e7eb; border:0; color:#111827; "
            "font-weight:600; padding:8px; text-align:left; }"
            "QPushButton#settingsButton:hover { background:#f3f4f6; }"
            "QListWidget#fileList { background:#ffffff; border:0; color:#111827; outline:0; padding:0; }"
            "QListWidget#fileList::item { min-height:26px; padding:0 8px; }"
            "QListWidget#fileList::item:selected { background:#dbeafe; color:#111827; }"
        )
        self.main_splitter.addWidget(self.sidebar)

        self.right_splitter = QSplitter(Qt.Vertical)
        self.editor_container = QWidget()
        editor_layout = QVBoxLayout(self.editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_header = QHBoxLayout()
        self.add_step_button = QPushButton(tr("增加步骤"))
        self.add_step_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.add_step_button.setFixedSize(max(88, self.add_step_button.fontMetrics().horizontalAdvance(self.add_step_button.text()) + 24), 30)
        self.add_step_button.clicked.connect(self._add_step)
        editor_header.addWidget(self.add_step_button)
        self.view_toggle = QFrame()
        self.view_toggle.setObjectName("workflowViewToggle")
        self.view_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.view_toggle.setFixedHeight(30)
        toggle_layout = QHBoxLayout(self.view_toggle)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        toggle_layout.setSpacing(0)
        toggle_layout.setAlignment(Qt.AlignLeft)
        self.flow_view_button = QPushButton(tr("视图窗"))
        self.flow_view_button.setObjectName("flowViewToggleButton")
        self.flow_view_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.flow_view_button.setFixedSize(max(68, self.flow_view_button.fontMetrics().horizontalAdvance(self.flow_view_button.text()) + 24), 28)
        self.flow_view_button.setCheckable(True)
        self.flow_view_button.toggled.connect(self._sync_editor_display)
        toggle_layout.addWidget(self.flow_view_button)
        self.parameter_view_button = QPushButton(tr("参数窗"))
        self.parameter_view_button.setObjectName("parameterViewToggleButton")
        self.parameter_view_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.parameter_view_button.setFixedSize(max(68, self.parameter_view_button.fontMetrics().horizontalAdvance(self.parameter_view_button.text()) + 24), 28)
        self.parameter_view_button.setCheckable(True)
        self.parameter_view_button.toggled.connect(self._sync_editor_display)
        toggle_layout.addWidget(self.parameter_view_button)
        self.view_toggle.setStyleSheet(
            "QFrame#workflowViewToggle { border:1px solid #94a3b8; border-radius:4px; }"
            "QPushButton { border:0; background:#ffffff; }"
            "QPushButton#flowViewToggleButton { border-right:1px solid #94a3b8; border-top-left-radius:3px; border-bottom-left-radius:3px; }"
            "QPushButton#parameterViewToggleButton { border-top-right-radius:3px; border-bottom-right-radius:3px; }"
            "QPushButton:checked { background:#dbeafe; color:#1d4ed8; font-weight:600; }"
        )
        editor_header.addWidget(self.view_toggle)
        editor_header.addStretch(1)
        self.history_button = QPushButton(tr("历史版本"))
        self.history_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.history_button.setFixedSize(max(88, self.history_button.fontMetrics().horizontalAdvance(self.history_button.text()) + 24), 30)
        self.history_button.clicked.connect(self._show_current_file_history)
        editor_header.addWidget(self.history_button)
        editor_layout.addLayout(editor_header)
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setFont(QFont("Cascadia Mono", self.editor_font_size))
        self.editor.setStyleSheet(
            "QPlainTextEdit { background:#ffffff; color:#1f2937; border:1px solid #d1d5db; padding:8px; }"
        )
        self.editor.setMouseTracking(True)
        self.editor.viewport().setMouseTracking(True)
        self.editor.viewport().installEventFilter(self)
        self.editor.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        self.editor.textChanged.connect(self._editor_changed)
        self.editor_splitter = QSplitter(Qt.Horizontal)
        self.workflow_panel = _WorkflowDiagramPanel()
        self.workflow_panel.selection_changed.connect(self._focus_workflow_steps)
        self.workflow_panel.step_double_clicked.connect(self._edit_workflow_step)
        self.workflow_panel.step_context_requested.connect(self._show_workflow_step_context_menu)
        self.workflow_panel.step_move_requested.connect(self._move_workflow_step)
        self.workflow_panel.save_requested.connect(self.save_text)
        self.workflow_panel.zoom_changed.connect(self._save_workflow_zoom)
        self.workflow_panel.set_zoom(self.workflow_zoom)
        self.server_parameter_form = _ServerParameterForm()
        self.server_parameter_form.set_ip_hiding(
            self.application_settings.get("hide_ip_address") is True
        )
        self.server_parameter_form.content_changed.connect(self._parameter_form_changed)
        self.server_parameter_form.direct_connect_requested.connect(
            self._direct_ssh_connection
        )
        self.editor_splitter.addWidget(self.workflow_panel)
        self.editor_splitter.addWidget(self.server_parameter_form)
        self.editor_splitter.addWidget(self.editor)
        self.editor_splitter.setSizes([700, 0, 300])
        editor_layout.addWidget(self.editor_splitter, 1)
        self.parameter_view_button.setChecked(True)
        self.flow_view_button.setChecked(True)
        self._sync_editor_display()
        self.right_splitter.addWidget(self.editor_container)

        self.interaction_tabs = QTabWidget()
        self.interaction_tabs.setTabsClosable(False)
        self.interaction_tabs.tabCloseRequested.connect(self._tab_close_requested)
        output_container = QWidget()
        output_layout = QVBoxLayout(output_container)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_text.setFont(QFont("Cascadia Mono", 10))
        self.log_text.setStyleSheet("QPlainTextEdit { background:#111827; color:#e5e7eb; border:0; padding:8px; }")
        output_layout.addWidget(self.log_text)
        self.interaction_tabs.addTab(output_container, tr("执行输出"))
        self._refresh_ssh_tab_buttons()
        self.right_splitter.addWidget(self.interaction_tabs)
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 0)
        self.right_splitter.splitterMoved.connect(self._layout_dimension_changed)
        self.right_splitter.setSizes([370, self.ssh_console_height])
        self.interaction_tabs.setVisible(False)
        self.main_splitter.addWidget(self.right_splitter)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.splitterMoved.connect(self._layout_dimension_changed)
        self.main_splitter.setSizes([self.file_sidebar_width, 850])
        root_layout.addWidget(self.main_splitter, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel(tr("就绪"))
        status_row.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedWidth(220)
        status_row.addWidget(self.progress_bar)
        root_layout.addLayout(status_row)
        self.setCentralWidget(root)

        save_action = QAction(self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.save_text)
        self.addAction(save_action)
        zoom_in = QAction(self)
        zoom_in.setShortcut(QKeySequence.ZoomIn)
        zoom_in.triggered.connect(lambda: self._zoom_editor(1))
        self.addAction(zoom_in)
        zoom_out = QAction(self)
        zoom_out.setShortcut(QKeySequence.ZoomOut)
        zoom_out.triggered.connect(lambda: self._zoom_editor(-1))
        self.addAction(zoom_out)

    @property
    def dir_path(self) -> Path:
        return {
            "task": self.task_dir,
            "parameter": self.parameter_dir,
            "script": self.script_dir,
            "log": self.execution_log_root,
        }[self.view_mode]

    def _view_label(self) -> str:
        return {"task": tr("任务"), "parameter": tr("配置"), "script": tr("脚本"), "log": tr("日志")}[self.view_mode]

    def switch_view(self, view_mode: str, initial: bool = False) -> None:
        if view_mode not in {"task", "parameter", "script", "log"}:
            return
        if self._deploying and not initial:
            QMessageBox.warning(self, tr("正在部署"), tr("部署完成后才能切换列表"))
            return
        if not initial and view_mode == self.view_mode:
            return
        if not initial and (not self._confirm_pending_changes() or not self._conceal_current_parameter_password()):
            return
        if self.view_mode == "task" and view_mode != "task":
            self._active_execution_log = None
            self._active_execution_task_path = None
        if self.view_mode == "parameter" and view_mode != "parameter":
            self._active_parameter_log = None
            self._active_parameter_path = None
        self.view_mode = view_mode
        self.file_list.setSelectionMode(
            QAbstractItemView.ExtendedSelection
            if view_mode == "log"
            else QAbstractItemView.SingleSelection
        )
        self.select_all_logs_action.setEnabled(view_mode == "log")
        self.current_path = None
        self._structured_workflow_steps = [] if view_mode == "task" else None
        self._dirty = False
        self._set_editor_content("")
        for mode, button in self.nav_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(mode == view_mode)
            del blocker
        self._restore_current_view_file()
        self._update_controls()
        if not initial:
            self.status_label.setText(tr("已切换到{0}列表", self._view_label()))

    def update_file_list(self, select_path: Path | None = None) -> None:
        self._changing_selection = True
        self.file_list.clear()
        extensions = (
            {".txt"}
            if self.view_mode in {"task", "parameter"}
            else {".log"}
            if self.view_mode == "log"
            else {".sh", ".bash", ".bat", ".cmd", ".ps1"}
        )
        files = (
            [p for p in self.dir_path.rglob("*.log") if p.is_file()]
            if self.view_mode == "log"
            else [
                p for p in self.dir_path.iterdir()
                if p.is_file() and p.suffix.lower() in extensions
            ]
        )
        order_by_name = {
            name: index
            for index, name in enumerate(self._file_orders().get(self.view_mode, []))
        }
        if self.view_mode == "log":
            files.sort(key=lambda path: (path.parent.name, path.name.lower()), reverse=True)
        else:
            files.sort(key=lambda path: (order_by_name.get(path.name, len(order_by_name)), path.name.lower()))
        for path in files:
            item = QListWidgetItem(
                self._execution_log_label(path) if self.view_mode == "log" else path.stem
            )
            item.setData(Qt.UserRole, str(path.resolve()))
            self.file_list.addItem(item)
            if select_path is not None and path.resolve() == select_path.resolve():
                self.file_list.setCurrentItem(item)
        self._changing_selection = False

    @staticmethod
    def _execution_log_label(path: Path) -> str:
        return f"{path.parent.name}  {path.stem}"

    def _file_orders(self) -> dict[str, list[str]]:
        raw_orders = self.application_settings.get("file_orders", {})
        if not isinstance(raw_orders, dict):
            return {}
        return {
            mode: [Path(str(name)).name for name in names if name]
            for mode, names in raw_orders.items()
            if mode in {"task", "parameter", "script", "log"} and isinstance(names, list)
        }

    def _save_file_order(self, *_arguments: object) -> None:
        if self._changing_selection:
            return
        file_order = [
            Path(str(self.file_list.item(index).data(Qt.UserRole))).name
            for index in range(self.file_list.count())
        ]
        settings = dict(self.application_settings)
        orders = self._file_orders()
        orders[self.view_mode] = file_order
        settings["file_orders"] = orders
        if self._write_settings(settings):
            self.status_label.setText(tr("已保存{0}排序", self._view_label()))

    def _restore_current_view_file(self) -> None:
        name = self.last_selected_files.get(self.view_mode)
        candidate = (
            (self.dir_path / name).resolve()
            if name and self.view_mode == "log"
            else (self.dir_path / Path(name).name).resolve()
            if name
            else None
        )
        selected = candidate if candidate is not None and candidate.is_file() else None
        self.update_file_list(selected)
        if selected is not None:
            if not self._load_file(selected):
                self._restore_file_list_selection(None)
                self._restore_untitled_draft()
        elif self.view_mode == "log":
            self._structured_workflow_steps = None
            self._set_editor_content("")
            self.editor.setReadOnly(True)
            self._dirty = False
        else:
            self._restore_untitled_draft()

    def _on_file_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if self._changing_selection or current is None:
            return
        path = Path(str(current.data(Qt.UserRole))).resolve()
        if path == self.current_path:
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            QTimer.singleShot(
                0,
                lambda item=previous: self._restore_file_list_selection(item),
            )
            return
        if not self._load_file(path):
            self._restore_file_list_selection(previous)

    def _restore_file_list_selection(self, item: QListWidgetItem | None) -> None:
        blocker = QSignalBlocker(self.file_list)
        self.file_list.clearSelection()
        if item is not None:
            item.setSelected(True)
            self.file_list.setCurrentItem(item)
            self.file_list.scrollToItem(item)
        else:
            self.file_list.setCurrentItem(None)
        del blocker

    def _show_file_context_menu(self, position: QPoint) -> None:
        item = self.file_list.itemAt(position)
        if item is None:
            return
        path = Path(str(item.data(Qt.UserRole))).resolve()
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())
        if self.view_mode == "log":
            if not item.isSelected():
                self.file_list.clearSelection()
                item.setSelected(True)
                self.file_list.setCurrentItem(item)
            selected_paths = self._selected_execution_logs()
            delete_action = menu.addAction(
                tr("删除选中的 {0} 个日志", len(selected_paths))
                if len(selected_paths) > 1
                else tr("删除日志")
            )
            delete_action.triggered.connect(
                lambda _checked=False, targets=tuple(selected_paths):
                self._delete_execution_logs(targets)
            )
            menu.exec(self.file_list.viewport().mapToGlobal(position))
            return
        history_action = menu.addAction(tr("历史版本"))
        history_action.setEnabled(any(self._history_directory(path).glob("*.txt")))
        history_action.triggered.connect(
            lambda _checked=False, target=path: self._show_file_history(target)
        )
        menu.exec(self.file_list.viewport().mapToGlobal(position))

    def _selected_execution_logs(self) -> list[Path]:
        return [
            Path(str(item.data(Qt.UserRole))).resolve()
            for item in self.file_list.selectedItems()
        ]

    def _delete_execution_logs(self, paths: tuple[Path, ...] | list[Path]) -> None:
        paths = list(dict.fromkeys(path.resolve() for path in paths))
        if not paths:
            return
        message = (
            tr("确定删除选中的 {0} 个日志吗？", len(paths))
            if len(paths) > 1
            else tr("确定删除日志“{0}”吗？", self._execution_log_label(paths[0]))
        )
        if QMessageBox.question(
            self, tr("确认删除"), message
        ) != QMessageBox.Yes:
            return
        failed: list[str] = []
        for path in paths:
            try:
                path.unlink()
                if path.parent != self.execution_log_root:
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            except OSError as exc:
                failed.append(f"{path.name}：{exc}")
        if self.current_path in paths:
            self.current_path = None
            self.last_selected_files.pop("log", None)
            self._set_editor_content("")
            self.editor.setReadOnly(True)
        self.update_file_list()
        if failed:
            QMessageBox.critical(self, tr("部分日志删除失败"), "\n".join(failed))
        deleted_count = len(paths) - len(failed)
        self.status_label.setText(tr("已删除 {0} 个日志", deleted_count))

    def _show_file_history(self, path: Path) -> None:
        history_mode = self._history_mode(path)
        view_label = {"task": tr("任务"), "parameter": tr("配置"), "script": tr("脚本")}[history_mode]
        if self.current_path == path and self._dirty:
            if not self._write_current_history():
                return
        versions = sorted(self._history_directory(path).glob("*.txt"), reverse=True)
        if not versions:
            QMessageBox.information(self, tr("历史版本"), tr("当前{0}还没有历史版本", view_label))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("{0}历史版本 - {1}", view_label, path.stem))
        dialog.resize(760, 500)
        root = QVBoxLayout(dialog)
        splitter = QSplitter(Qt.Horizontal)
        version_list = QListWidget()
        version_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        select_all_action = QAction(version_list)
        select_all_action.setShortcut(QKeySequence.SelectAll)
        select_all_action.setShortcutContext(Qt.WidgetShortcut)
        select_all_action.triggered.connect(
            lambda _checked=False: version_list.selectAll()
        )
        version_list.addAction(select_all_action)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFont("Cascadia Mono", self.editor_font_size))
        splitter.addWidget(version_list)
        splitter.addWidget(preview)
        splitter.setSizes([220, 540])
        root.addWidget(splitter, 1)
        actions = QHBoxLayout()
        restore_button = QPushButton(tr("恢复此版本"))
        delete_button = QPushButton(tr("删除版本"))
        close_button = QPushButton(tr("关闭"))
        actions.addStretch(1)
        actions.addWidget(restore_button)
        actions.addWidget(delete_button)
        actions.addWidget(close_button)
        root.addLayout(actions)

        def selected_versions() -> list[Path]:
            return [
                Path(str(item.data(Qt.UserRole)))
                for item in version_list.selectedItems()
            ]

        def selected_version() -> Path | None:
            versions = selected_versions()
            return versions[0] if len(versions) == 1 else None

        def show_preview() -> None:
            selections = selected_versions()
            delete_button.setEnabled(bool(selections))
            if not selections:
                preview.clear()
                restore_button.setEnabled(False)
                return
            if len(selections) > 1:
                preview.setPlainText(tr("已选择 {0} 个历史版本，可批量删除。", len(selections)))
                restore_button.setEnabled(False)
                return
            version = selections[0]
            try:
                preview.setPlainText(
                    self._history_preview(
                        version.read_text(encoding="utf-8-sig"), history_mode,
                        self.application_settings.get("hide_ip_address") is True,
                    )
                )
                restore_button.setEnabled(True)
            except (ConfigurationError, OSError, UnicodeDecodeError) as exc:
                preview.setPlainText(tr("无法读取历史版本：{0}", exc))
                restore_button.setEnabled(False)

        def reload_versions() -> None:
            version_list.clear()
            current_versions = sorted(
                self._history_directory(path).glob("*.txt"), reverse=True
            )
            for history_path in current_versions:
                item = QListWidgetItem(self._history_version_label(history_path))
                item.setData(Qt.UserRole, str(history_path))
                version_list.addItem(item)
            if version_list.count():
                version_list.setCurrentRow(0)
            else:
                show_preview()

        def restore_version() -> None:
            version = selected_version()
            if version is None:
                return
            if QMessageBox.question(self, tr("确认恢复"), tr("确定恢复所选历史版本吗？")) != QMessageBox.Yes:
                return
            try:
                current_content = path.read_text(encoding="utf-8-sig")
                restored_content = version.read_text(encoding="utf-8-sig")
                if history_mode == "task":
                    parse_workflow_document(restored_content)
            except (ConfigurationError, OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, tr("恢复失败"), str(exc))
                return
            if self.current_path == path and self._dirty:
                if not self._write_current_history():
                    return
            elif not self._write_history_snapshot(path, current_content):
                return
            try:
                path.write_text(restored_content, encoding="utf-8")
            except OSError as exc:
                QMessageBox.critical(self, tr("恢复失败"), str(exc))
                return
            if self.current_path == path:
                if history_mode == "parameter":
                    self._close_parameter_ssh_tabs(path)
                self._delete_draft(path)
                self._load_file(path)
            self.status_label.setText(tr("已恢复{0}：{1}", view_label, path.stem))
            reload_versions()

        def delete_version() -> None:
            versions = selected_versions()
            if not versions:
                return
            message = (
                tr("确定删除历史版本“{0}”吗？", self._history_version_label(versions[0]))
                if len(versions) == 1
                else tr("确定删除选中的 {0} 个历史版本吗？", len(versions))
            )
            if QMessageBox.question(
                self, tr("确认删除"), message
            ) != QMessageBox.Yes:
                return
            failures: list[str] = []
            for version in versions:
                try:
                    version.unlink()
                except OSError as exc:
                    failures.append(f"{self._history_version_label(version)}：{exc}")
            reload_versions()
            if failures:
                QMessageBox.critical(self, tr("部分删除失败"), "\n".join(failures))
            else:
                self.status_label.setText(tr("已删除 {0} 个历史版本", len(versions)))

        def show_version_context_menu(position: QPoint) -> None:
            item = version_list.itemAt(position)
            if item is None:
                return
            if not item.isSelected():
                version_list.clearSelection()
                version_list.setCurrentItem(item)
            menu = QMenu(dialog)
            menu.setStyleSheet(self._menu_style())
            if len(selected_versions()) == 1:
                restore_action = menu.addAction(tr("恢复此版本"))
                restore_action.triggered.connect(restore_version)
                menu.addSeparator()
            delete_action = menu.addAction(tr("删除此版本"))
            delete_action.triggered.connect(delete_version)
            menu.exec(version_list.viewport().mapToGlobal(position))

        version_list.itemSelectionChanged.connect(show_preview)
        version_list.setContextMenuPolicy(Qt.CustomContextMenu)
        version_list.customContextMenuRequested.connect(show_version_context_menu)
        restore_button.clicked.connect(restore_version)
        delete_button.clicked.connect(delete_version)
        close_button.clicked.connect(dialog.reject)
        reload_versions()
        dialog.exec()

    def _show_current_file_history(self) -> None:
        if self.current_path is not None:
            self._show_file_history(self.current_path)

    @staticmethod
    def _history_version_label(path: Path) -> str:
        try:
            value = datetime.strptime(path.stem, "%Y%m%d-%H%M%S-%f")
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except ValueError:
            return path.stem

    @staticmethod
    def _history_preview(content: str, history_mode: str, hide_ip: bool = False) -> str:
        if history_mode == "parameter":
            return ApplicationWindow._parameter_history_preview(content, hide_ip)
        if history_mode != "task":
            return content
        steps = parse_workflow_document(content)["steps"]
        lines: list[str] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            definition = WORKFLOW_TYPE_BY_KEY.get(str(step.get("type", "")).upper())
            lines.append(tr("第 {0} 步：{1}", index, tr(definition.label) if definition else step.get('type', '')))
            properties = step.get("properties", {})
            if definition is not None and isinstance(properties, dict):
                for field in definition.fields:
                    value = str(properties.get(field.key, "")).strip()
                    if value:
                        lines.append(f"  {tr(field.label)}：{value}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _parameter_history_preview(content: str, hide_ip: bool = False) -> str:
        values: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().upper()] = value.strip()

        lines: list[str] = []
        connection: list[tuple[str, str]] = []
        for key, label in (
            ("IP_ADDRESS", tr("服务器地址")),
            ("PORT", tr("SSH 端口")),
            ("USERNAME", tr("登录账号")),
        ):
            value = values.get(key, "").strip()
            if value:
                if key == "IP_ADDRESS":
                    value = mask_ip_address(value, hide_ip)
                connection.append((label, value))
        password = values.get("PASSWORD", "").strip()
        key_filename = values.get("KEY_FILENAME", "").strip()
        auth_method = values.get("AUTH_METHOD", "").strip().upper()
        if auth_method or password or key_filename:
            if not auth_method:
                auth_method = "KEY" if key_filename else "PASSWORD"
            auth_label = {"PASSWORD": tr("密码登录"), "KEY": tr("SSH 私钥登录"), "AUTO": tr("自动选择")}.get(
                auth_method, auth_method
            )
            connection.append((tr("认证方式"), auth_label))
        if password:
            connection.append((tr("登录密码"), tr("已配置")))
        if key_filename:
            connection.append((tr("SSH 私钥"), key_filename))
        if connection:
            lines.append(tr("服务器连接"))
            lines.extend(f"  {label}：{value}" for label, value in connection)

        entries: list[tuple[int, str, str, bool, bool]] = []
        legacy_path = values.get("DEFAULT_OPEN_PATH", "").strip()
        legacy_command = values.get("DEFAULT_OPEN_COMMAND", "").strip()
        if legacy_path:
            legacy_enabled = values.get("DEFAULT_OPEN_COMMAND_ENABLED", "").upper()
            entries.append((
                0,
                legacy_path,
                legacy_command,
                values.get("DEFAULT_OPEN_PATH_SELECTED", "").upper() == "YES",
                legacy_enabled == "YES" if legacy_enabled else bool(legacy_command),
            ))
        indexed: dict[int, dict[str, str]] = {}
        patterns = (
            (r"DEFAULT_OPEN_PATH_(\d+)", "path"),
            (r"DEFAULT_OPEN_COMMAND_(\d+)", "command"),
            (r"DEFAULT_OPEN_PATH_SELECTED_(\d+)", "selected"),
            (r"DEFAULT_OPEN_COMMAND_ENABLED_(\d+)", "enabled"),
        )
        for key, value in values.items():
            for pattern, field in patterns:
                match = re.fullmatch(pattern, key)
                if match:
                    indexed.setdefault(int(match.group(1)), {})[field] = value
                    break
        for index, entry in sorted(indexed.items()):
            path = entry.get("path", "").strip()
            if not path:
                continue
            command = entry.get("command", "").strip()
            enabled = entry.get("enabled", "").upper()
            entries.append((
                index,
                path,
                command,
                entry.get("selected", "").upper() == "YES",
                enabled == "YES" if enabled else bool(command),
            ))
        for display_index, (_index, path, command, selected, enabled) in enumerate(entries, start=1):
            if lines:
                lines.append("")
            lines.append(tr("访问位置 {0}", display_index))
            lines.append(tr("  服务器目录：{0}", path))
            if selected:
                lines.append(tr("  默认连接路径：是"))
            if command:
                lines.append(tr("  执行命令：{0}", command))
                lines.append(tr("  连接后自动执行：{0}", tr('是') if enabled else tr('否')))
        return "\n".join(lines).rstrip()

    def _structured_workflow_storage(self) -> str:
        if self._structured_workflow_steps is None:
            raise ConfigurationError("当前没有可保存的 JSON 工作流")
        return json.dumps(
            {"version": 2, "steps": self._structured_workflow_steps or []},
            ensure_ascii=False,
            indent=2,
        ) + "\n"

    def _structured_workflow_summary(
        self, selected_steps: list[int] | tuple[int, ...] | set[int] | None = None
    ) -> str:
        selected = set(selected_steps) if selected_steps is not None else None
        lines: list[str] = []
        for index, step in enumerate(self._structured_workflow_steps or [], start=1):
            if selected is not None and index not in selected:
                continue
            step_type = str(step.get("type", "")).upper()
            definition = WORKFLOW_TYPE_BY_KEY.get(step_type)
            lines.append(tr("第 {0} 步：{1}", index, tr(definition.label) if definition else step_type))
            properties = step.get("properties", {})
            if not isinstance(properties, dict):
                continue
            for field in definition.fields if definition is not None else ():
                value = str(properties.get(field.key, "")).strip()
                if value:
                    lines.append(f"  {tr(field.label)}：{value}")
            lines.append("")
        return "\n".join(lines).rstrip() + ("\n" if lines else "")

    def _load_file(self, path: Path) -> bool:
        self.history_timer.stop()
        if self.view_mode == "log":
            try:
                resolved_path = path.resolve()
                content, sequence = self._parameter_log_writer.read_text(resolved_path)
            except (OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, tr("读取日志失败"), str(exc))
                return False
            self.current_path = resolved_path
            self._displayed_log_sequences[resolved_path] = sequence
            self.last_selected_files[self.view_mode] = str(
                path.resolve().relative_to(self.execution_log_root)
            )
            self._structured_workflow_steps = None
            self._set_editor_content(content)
            self.editor.setReadOnly(True)
            self._dirty = False
            self.editor.document().setModified(False)
            self.status_label.setText(tr("已加载日志 {0}", self._execution_log_label(path)))
            self._update_controls()
            return True
        draft = self._draft_path(path)
        dirty = False
        try:
            stored = path.read_text(encoding="utf-8-sig")
            if self.view_mode == "task":
                parse_workflow_document(stored)
            if draft.is_file():
                stored = draft.read_text(encoding="utf-8-sig")
                dirty = True
            if self.view_mode == "task":
                steps = parse_workflow_document(stored)["steps"]
                displayed = ""
            else:
                steps = None
                displayed, _normalized = self._prepare_parameter_content_for_display(stored)
        except (ConfigurationError, OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, tr("读取失败"), str(exc))
            return False
        self._structured_workflow_steps = steps
        if steps is not None:
            displayed = self._structured_workflow_summary()
        self.current_path = path.resolve()
        self.last_selected_files[self.view_mode] = path.name
        if self.view_mode == "task":
            blocker = QSignalBlocker(self.workflow_panel)
            self.workflow_panel.set_zoom(
                self.workflow_task_zooms.get(path.name, self.workflow_zoom)
            )
            del blocker
        self._set_editor_content(displayed)
        self.editor.setReadOnly(self._structured_workflow_steps is not None)
        self._dirty = dirty
        self.editor.document().setModified(dirty)
        if self.view_mode == "task":
            self._ensure_task_execution_log(self.current_path)
        elif self.view_mode == "parameter":
            self._ensure_parameter_log(self.current_path)
        self.status_label.setText(tr("已加载 {0}", path.name) + (tr("（存在暂存内容）") if dirty else ""))
        self._update_controls()
        return True

    def _set_editor_content(self, content: str, preserve_view: bool = False) -> None:
        cursor = self.editor.textCursor()
        position = cursor.position()
        scroll = self.editor.verticalScrollBar().value()
        self._loading_editor = True
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        if self.view_mode == "parameter" and hasattr(self, "server_parameter_form"):
            self.server_parameter_form.set_content(content)
        if preserve_view:
            cursor.setPosition(min(position, len(content)))
            self.editor.setTextCursor(cursor)
            self.editor.verticalScrollBar().setValue(scroll)
        self._loading_editor = False
        if not preserve_view and hasattr(self, "workflow_panel"):
            self.workflow_panel.clear_selection()
        self._refresh_workflow_diagram(preserve_view=preserve_view)
        if (
            preserve_view
            and self.view_mode == "task"
            and self._structured_workflow_steps is not None
        ):
            selected_steps = self.workflow_panel.selected_steps()
            if selected_steps:
                self._set_structured_workflow_display(selected_steps)

    def _editor_changed(self) -> None:
        if self._loading_editor:
            return
        if not self._dirty and self.current_path is not None:
            self._ensure_current_history_baseline()
        self._dirty = True
        self._refresh_workflow_diagram()
        self.status_label.setText(tr("内容已修改，等待自动暂存"))
        self.auto_save_timer.start(int(self.auto_save_delay_seconds * 1000))
        if self.current_path is not None:
            self.history_timer.start()

    def _parameter_form_changed(self) -> None:
        if self._loading_editor or self.view_mode != "parameter":
            return
        self._loading_editor = True
        self.editor.setPlainText(self.server_parameter_form.to_content())
        self.editor.document().setModified(True)
        self._loading_editor = False
        if not self._dirty and self.current_path is not None:
            self._ensure_current_history_baseline()
        self._dirty = True
        self.status_label.setText(tr("配置已修改，等待自动暂存"))
        self.auto_save_timer.start(int(self.auto_save_delay_seconds * 1000))
        if self.current_path is not None:
            self.history_timer.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.editor.viewport() and event.type() == QEvent.Wheel:
            if event.modifiers() & Qt.ControlModifier:
                delta = event.angleDelta().y() or event.pixelDelta().y()
                if delta:
                    self._zoom_editor(1 if delta > 0 else -1)
                event.accept()
                return True
        if watched is self.editor.viewport() and event.type() == QEvent.ToolTip:
            cursor = self.editor.cursorForPosition(event.pos())
            key = self._property_key_at_cursor(cursor)
            line = cursor.block().text()
            value = line.split("=", 1)[1].strip() if "=" in line else ""
            tooltip = self._property_tooltip(key, value)
            if tooltip:
                QToolTip.showText(event.globalPos(), tooltip, self.editor)
                return True
            QToolTip.hideText()
        return super().eventFilter(watched, event)

    def _show_editor_context_menu(self, position: object) -> None:
        menu = self.editor.createStandardContextMenu()
        if self.view_mode == "task" and not self._deploying:
            cursor = self.editor.cursorForPosition(position)
            step_index = self._workflow_step_at_cursor(cursor)
            if step_index is not None:
                menu.addSeparator()
                action = menu.addAction(tr("从此位置开始执行"))
                action.triggered.connect(
                    lambda _checked=False, value=step_index: self.develop_method(value)
                )
                single_action = menu.addAction(tr("单独执行此步骤"))
                single_action.triggered.connect(
                    lambda _checked=False, value=step_index: self.develop_method(value, True)
                )
        menu.exec(self.editor.mapToGlobal(position))

    def _workflow_step_at_cursor(self, cursor: QTextCursor) -> int | None:
        if self._structured_workflow_steps is None:
            return None
        selected = set(self.workflow_panel.selected_steps())
        line_number = 0
        for index in range(1, len(self._structured_workflow_steps) + 1):
            if selected and index not in selected:
                continue
            line_count = len(self._structured_workflow_summary([index]).splitlines())
            if line_number <= cursor.blockNumber() < line_number + line_count:
                return index
            line_number += line_count + 1
        return None

    @staticmethod
    def _property_key_at_cursor(cursor: QTextCursor) -> str | None:
        line = cursor.block().text()
        position = cursor.positionInBlock()
        match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line.upper())
        if match is not None and match.start(1) <= position <= match.end(1):
            return match.group(1)
        return None

    @staticmethod
    def _property_tooltip(key: str | None, value: str = "") -> str | None:
        if not key:
            return None
        base_key = re.sub(r"_\d+$", "", key)
        return tr(
            _PROPERTY_TOOLTIPS.get(key)
            or _PROPERTY_TOOLTIPS.get(base_key)
            or tr("自定义属性：{0}。请参考所在任务或脚本中的业务注释。", key)
        )

    def _draft_path(self, path: Path | None, view_mode: str | None = None) -> Path:
        mode = view_mode or self.view_mode
        return self.draft_root / mode / f"{path.name if path is not None else '__untitled__'}.draft"

    def _write_current_draft(self) -> bool:
        if not self._dirty:
            return True
        path = self._draft_path(self.current_path)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (
                self._structured_workflow_storage()
                if self.view_mode == "task"
                else self._content_for_storage(self.editor.toPlainText())
            )
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        except (ConfigurationError, OSError, PasswordProtectionError) as exc:
            self.status_label.setText(tr("自动暂存失败：{0}", exc))
            return False
        self.status_label.setText(tr("内容已自动暂存，点击保存后生效"))
        return True

    def _history_mode(self, path: Path) -> str:
        parent = path.resolve().parent
        if parent == self.task_dir:
            return "task"
        if parent == self.parameter_dir:
            return "parameter"
        if parent == self.script_dir:
            return "script"
        return self.view_mode

    def _history_directory(self, path: Path) -> Path:
        directory = {
            "task": "tasks", "parameter": "parameters", "script": "scripts"
        }[self._history_mode(path)]
        return self.history_root / directory / path.name

    def _ensure_current_history_baseline(self) -> bool:
        if self.current_path is None:
            return True
        try:
            content = self.current_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            self.status_label.setText(tr("历史版本保存失败：{0}", exc))
            return False
        return self._write_history_snapshot(self.current_path, content)

    def _write_current_history(self) -> bool:
        if self.current_path is None or not self._dirty:
            return True
        try:
            content = (
                self._structured_workflow_storage()
                if self.view_mode == "task"
                else self._content_for_storage(self.editor.toPlainText())
            )
        except (ConfigurationError, PasswordProtectionError) as exc:
            self.status_label.setText(tr("历史版本保存失败：{0}", exc))
            return False
        saved = self._write_history_snapshot(self.current_path, content)
        if saved:
            self.status_label.setText(tr("{0}已自动保存历史版本", self._view_label()))
        return saved

    def _write_history_snapshot(self, path: Path, content: str) -> bool:
        history_dir = self._history_directory(path)
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            versions = sorted(history_dir.glob("*.txt"), reverse=True)
            if versions and versions[0].read_text(encoding="utf-8-sig") == content:
                return True
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = history_dir / f"{timestamp}.txt"
            temporary = history_dir / f".{timestamp}.tmp"
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
        except (OSError, UnicodeDecodeError) as exc:
            self.status_label.setText(tr("历史版本保存失败：{0}", exc))
            return False
        return True

    def _restore_untitled_draft(self) -> None:
        draft = self._draft_path(None)
        self._structured_workflow_steps = [] if self.view_mode == "task" else None
        self._set_editor_content("")
        self.editor.setReadOnly(self.view_mode == "task")
        self._dirty = False
        if not draft.is_file():
            return
        try:
            stored = draft.read_text(encoding="utf-8-sig")
            if self.view_mode == "task":
                self._structured_workflow_steps = parse_workflow_document(stored)["steps"]
                displayed = self._structured_workflow_summary()
            else:
                displayed, _stored = self._prepare_parameter_content_for_display(stored)
        except (ConfigurationError, OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, tr("读取暂存失败"), str(exc))
            return
        self._set_editor_content(displayed)
        self._dirty = True
        self.editor.document().setModified(True)

    def _confirm_pending_changes(self) -> bool:
        if not self._dirty:
            return True
        if not self._write_current_history():
            return False
        answer = QMessageBox.question(
            self,
            tr("存在未保存内容"),
            tr("是否先保存当前文件？"),
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_text()
        self._delete_draft(self.current_path)
        self._dirty = False
        self.history_timer.stop()
        return True

    def create_file(self) -> None:
        extension = self._choose_script_extension(tr("选择脚本类型")) if self.view_mode == "script" else None
        if self.view_mode == "script" and extension is None:
            return
        name, accepted = QInputDialog.getText(self, tr("新建{0}", self._view_label()), tr("请输入{0}名称：", self._view_label()))
        if not accepted:
            return
        file_name = self._validate_file_name(name, extension)
        if file_name is None:
            return
        path = self.dir_path / file_name
        if path.exists():
            QMessageBox.critical(self, tr("无法新建"), tr("文件已存在：{0}", file_name))
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            return
        if self.view_mode == "task":
            content = json.dumps({"version": 2, "steps": []}, indent=2) + "\n"
        elif self.view_mode == "script" and path.suffix.lower() in {".bat", ".cmd"}:
            content = "@echo off\nsetlocal\n\n"
        elif self.view_mode == "script" and path.suffix.lower() == ".ps1":
            content = 'Set-StrictMode -Version Latest\n$ErrorActionPreference = "Stop"\n\n'
        else:
            template = self.parameter_template_path if self.view_mode == "parameter" else self.script_template_path
            try:
                content = template.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, tr("无法读取模板"), tr("模板：{0}\n\n{1}", template, exc))
                return
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, tr("无法新建"), str(exc))
            return
        self.update_file_list(path)
        self._load_file(path)

    def rename_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        name, accepted = QInputDialog.getText(self, tr("重命名{0}", self._view_label()), tr("请输入新的{0}名称：", self._view_label()), text=path.stem)
        if not accepted:
            return
        file_name = self._validate_file_name(name, path.suffix.lower() if self.view_mode == "script" else None)
        if file_name is None:
            return
        target = path.with_name(file_name)
        if target.exists() and target != path:
            QMessageBox.critical(self, tr("无法重命名"), tr("文件已存在：{0}", file_name))
            return
        if not self._confirm_pending_changes():
            return
        updates: list[tuple[Path, str, str]] = []
        refs: list[Path] = []
        try:
            if self.view_mode in {"parameter", "script"}:
                refs, updates = self._collect_task_reference_updates(self.view_mode, path, target)
            if not self._rename_with_reference_updates(path, target, updates):
                return
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(self, tr("无法重命名"), str(exc))
            return
        self._delete_draft(path)
        if self.view_mode == "parameter":
            self._close_parameter_ssh_tabs(path)
        elif self.view_mode == "task" and path.name in self.workflow_task_zooms:
            self.workflow_task_zooms[target.name] = self.workflow_task_zooms.pop(path.name)
            settings = dict(self.application_settings)
            settings["workflow_task_zooms"] = dict(self.workflow_task_zooms)
            self._write_settings(settings)
        self.update_file_list(target)
        self._load_file(target)
        self.status_label.setText(tr("已重命名为 {0}", target.name) + (tr("，并更新 {0} 个任务引用", len(refs)) if refs else ""))

    def copy_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        name, accepted = QInputDialog.getText(self, tr("复制{0}", self._view_label()), tr("请输入副本名称："), text=f"{path.stem}_copy")
        if not accepted:
            return
        file_name = self._validate_file_name(name, path.suffix.lower() if self.view_mode == "script" else None)
        if file_name is None:
            return
        target = path.with_name(file_name)
        if target.exists():
            QMessageBox.critical(self, tr("无法复制"), tr("文件已存在：{0}", file_name))
            return
        if not self._confirm_pending_changes():
            return
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            QMessageBox.critical(self, tr("无法复制"), str(exc))
            return
        self.update_file_list(target)
        self._load_file(target)

    def delete_file(self) -> None:
        if self.view_mode == "log":
            self._delete_execution_logs(self._selected_execution_logs())
            return
        path = self._require_current_path()
        if path is None:
            return
        if self.view_mode in {"parameter", "script"}:
            try:
                refs, _updates = self._collect_task_reference_updates(self.view_mode, path)
            except (OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, tr("无法检查任务引用"), str(exc))
                return
            if refs:
                names = "\n".join(f"• {self._task_reference_display_name(value)}" for value in refs[:10])
                QMessageBox.critical(self, tr("无法删除"), tr("以下任务仍引用 {0}：\n\n{1}", path.name, names))
                return
        if QMessageBox.question(self, tr("确认删除"), tr("确定删除 {0} 吗？", path.name)) != QMessageBox.Yes:
            return
        try:
            path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, tr("无法删除"), str(exc))
            return
        self._delete_draft(path)
        if self.view_mode == "parameter":
            self._close_parameter_ssh_tabs(path)
        elif self.view_mode == "task" and self.workflow_task_zooms.pop(path.name, None) is not None:
            settings = dict(self.application_settings)
            settings["workflow_task_zooms"] = dict(self.workflow_task_zooms)
            self._write_settings(settings)
        self.current_path = None
        self._structured_workflow_steps = [] if self.view_mode == "task" else None
        self._dirty = False
        self.last_selected_files.pop(self.view_mode, None)
        self._set_editor_content("")
        self.update_file_list()
        self._update_controls()
        self.status_label.setText(tr("{0}已删除", self._view_label()))

    def save_text(self, show_message: bool = True) -> bool:
        path = self.current_path
        previous_draft = self._draft_path(path)
        if path is None:
            extension = self._choose_script_extension(tr("选择脚本类型")) if self.view_mode == "script" else None
            if self.view_mode == "script" and extension is None:
                return False
            name, accepted = QInputDialog.getText(self, tr("保存新{0}", self._view_label()), tr("请输入新{0}名称：", self._view_label()))
            if not accepted:
                return False
            file_name = self._validate_file_name(name, extension)
            if file_name is None:
                return False
            path = (self.dir_path / file_name).resolve()
            if path.exists():
                QMessageBox.critical(self, tr("无法保存"), tr("文件已存在：{0}", file_name))
                return False
        was_dirty = self._dirty
        if was_dirty and self.current_path is not None:
            if not self._write_current_history():
                return False
        try:
            content = (
                self._structured_workflow_storage()
                if self.view_mode == "task"
                else self._content_for_storage(self.editor.toPlainText())
            )
            path.write_text(content, encoding="utf-8")
        except (ConfigurationError, OSError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, tr("保存失败"), str(exc))
            return False
        self._delete_draft_path(previous_draft)
        self._delete_draft(path)
        if self.current_path is None:
            self.current_path = path
            self.last_selected_files[self.view_mode] = path.name
            self.update_file_list(path)
        if was_dirty and self.view_mode == "parameter":
            self._close_parameter_ssh_tabs(path)
        self._dirty = False
        self.editor.document().setModified(False)
        self.auto_save_timer.stop()
        self.history_timer.stop()
        self.status_label.setText(tr("已保存 {0}", path.stem))
        self._update_controls()
        if show_message:
            QMessageBox.information(self, tr("保存成功"), tr("已保存：{0}", path.stem))
        return True

    def _choose_script_extension(self, title: str) -> str | None:
        extensions = {tr(label): extension for label, extension in _SCRIPT_EXTENSIONS.items()}
        label, accepted = QInputDialog.getItem(self, title, tr("请选择脚本类型："), list(extensions), 0, False)
        return extensions[label] if accepted else None

    def _validate_file_name(self, value: str | None, extension: str | None = None) -> str | None:
        name = (value or "").strip()
        if not name:
            QMessageBox.critical(self, tr("名称无效"), tr("文件名称不能为空"))
            return None
        if any(char in name for char in '<>:"/\\|?*') or name.rstrip(". ") != name:
            QMessageBox.critical(self, tr("名称无效"), tr("文件名称包含 Windows 不允许的字符"))
            return None
        if extension:
            if not extension.startswith("."):
                extension = f".{extension}"
            name = f"{Path(name).stem}{extension}"
        elif not name.lower().endswith(".txt"):
            name += ".txt"
        return name

    def _require_current_path(self) -> Path | None:
        if self.current_path is None:
            QMessageBox.warning(self, tr("未选择文件"), tr("请先选择一个文件"))
        return self.current_path

    def _delete_draft_path(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _delete_draft(self, path: Path | None) -> None:
        self._delete_draft_path(self._draft_path(path))

    def _collect_task_reference_updates(
        self, reference_type: str, referenced_path: Path, replacement_path: Path | None = None
    ) -> tuple[list[Path], list[tuple[Path, str, str]]]:
        referenced_path = referenced_path.resolve()
        sources = list(self.task_dir.glob("*.txt"))
        draft_dir = self.draft_root / "task"
        if draft_dir.is_dir():
            sources.extend(draft_dir.glob("*.draft"))
        active_sources = set(sources)
        if replacement_path is not None:
            history_dir = self.history_root / "tasks"
            if history_dir.is_dir():
                sources.extend(history_dir.glob("*/*.txt"))
        reference_key = "PARAMETER_FILE" if reference_type == "parameter" else "SCRIPT_FILE"
        refs: list[Path] = []
        updates: list[tuple[Path, str, str]] = []
        for task_path in sorted(sources, key=lambda p: str(p).lower()):
            content = task_path.read_text(encoding="utf-8-sig")
            try:
                document = parse_workflow_document(content)
            except ConfigurationError:
                continue
            matched = False
            for step in document["steps"]:
                if not isinstance(step, dict):
                    continue
                properties = step.get("properties")
                if not isinstance(properties, dict):
                    continue
                for key, value in properties.items():
                    if str(key).upper() != reference_key:
                        continue
                    if not isinstance(value, str) or not value.strip():
                        continue
                    reference = Path(os.path.expandvars(value.strip())).expanduser()
                    base = self.parameter_dir if reference_type == "parameter" else self.script_dir
                    resolved = reference.resolve() if reference.is_absolute() else (base / reference).resolve()
                    if resolved != referenced_path:
                        continue
                    matched = True
                    if replacement_path is not None:
                        properties[key] = str(replacement_path) if reference.is_absolute() else replacement_path.name
            if matched:
                if task_path in active_sources:
                    refs.append(task_path)
                updated = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
                if updated != content:
                    updates.append((task_path, content, updated))
        return refs, updates

    def _rename_with_reference_updates(self, path: Path, target: Path, updates: list[tuple[Path, str, str]]) -> bool:
        changed: list[tuple[Path, str]] = []
        renamed = False
        try:
            path.rename(target)
            renamed = True
            for task_path, original, updated in updates:
                changed.append((task_path, original))
                task_path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            for task_path, original in reversed(changed):
                try:
                    task_path.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            if renamed and target.exists() and not path.exists():
                try:
                    target.rename(path)
                except OSError:
                    pass
            QMessageBox.critical(self, tr("无法重命名"), tr("重命名或更新任务引用失败：{0}", exc))
            return False
        return True

    def _task_reference_display_name(self, path: Path) -> str:
        if path.parent.resolve() != (self.draft_root / "task").resolve():
            return path.name
        return tr("未命名任务（暂存）") if path.name == "__untitled__.draft" else tr("{0}（暂存）", path.name.removesuffix('.draft'))

    def _add_step(self) -> None:
        if self.view_mode == "task":
            self._add_workflow_step(insert_after=self.workflow_panel.selected_step())

    def _workflow_steps_from_editor(self) -> list[tuple[int, str]]:
        return [
            (index, str(step.get("type", "")).upper())
            for index, step in enumerate(self._structured_workflow_steps or [], start=1)
        ]

    def _refresh_workflow_diagram(self, preserve_view: bool = True) -> None:
        if hasattr(self, "workflow_panel"):
            self.workflow_panel.set_steps(
                self._workflow_steps_from_editor(), preserve_view=preserve_view
            )

    def _focus_workflow_step(self, step_index: int) -> None:
        if self.view_mode == "task" and self._structured_workflow_steps is not None:
            self._set_structured_workflow_display([step_index])

    def _focus_workflow_steps(self, step_indexes: object) -> None:
        steps = sorted({int(value) for value in step_indexes}) if isinstance(
            step_indexes, (list, tuple, set)
        ) else []
        if not steps:
            self._show_all_workflow_steps()
        elif self._structured_workflow_steps is not None:
            self._set_structured_workflow_display(steps)

    def _show_all_workflow_steps(self) -> None:
        if self.view_mode == "task" and self._structured_workflow_steps is not None:
            self._set_structured_workflow_display()

    def _move_workflow_step(self, source_step: int, target_step: int) -> None:
        if self.view_mode != "task" or self._deploying or self._structured_workflow_steps is None:
            return
        steps = self._workflow_steps_from_editor()
        indexes = [index for index, _step_type in steps]
        if source_step not in indexes or target_step not in indexes:
            return
        reordered_indexes = list(indexes)
        reordered_indexes.remove(source_step)
        reordered_indexes.insert(
            reordered_indexes.index(target_step) + 1,
            source_step,
        )
        if reordered_indexes == indexes:
            return
        new_indexes = {
            old_index: new_index
            for new_index, old_index in enumerate(reordered_indexes, start=1)
        }
        moved_step_index = new_indexes[source_step]
        self._structured_workflow_steps = [
            self._structured_workflow_steps[index - 1] for index in reordered_indexes
        ]
        content = self._structured_workflow_summary()
        self._set_editor_content(content, preserve_view=True)
        self._mark_changed(
            tr("已将原第 {0} 步移动到第 {1} 步", source_step, moved_step_index)
        )
        self.workflow_panel.set_selected_steps([moved_step_index])

    def _set_structured_workflow_display(
        self, step_indexes: list[int] | tuple[int, ...] | set[int] | None = None
    ) -> None:
        self._loading_editor = True
        self.editor.setPlainText(self._structured_workflow_summary(step_indexes))
        self.editor.document().setModified(self._dirty)
        self._loading_editor = False

    def _workflow_step_values(self, step_index: int) -> dict[str, str]:
        if self._structured_workflow_steps is not None:
            if not 1 <= step_index <= len(self._structured_workflow_steps):
                return {}
            step = self._structured_workflow_steps[step_index - 1]
            properties = step.get("properties", {})
            values = {
                str(key).upper(): str(value)
                for key, value in properties.items()
            } if isinstance(properties, dict) else {}
            values["TYPE"] = str(step.get("type", "")).upper()
            return values
        return {}

    def _edit_workflow_step(self, step_index: int) -> None:
        if self.view_mode == "task":
            self._add_workflow_step(step_index)

    def _show_workflow_step_context_menu(self, step_index: int, position: object) -> None:
        if self.view_mode != "task" or self._deploying:
            return
        if not 1 <= step_index <= len(self._structured_workflow_steps or []):
            return
        menu = QMenu(self)
        selected_steps = self.workflow_panel.selected_steps()
        if len(selected_steps) > 1:
            selected_action = menu.addAction(tr("执行选中的 {0} 个步骤", len(selected_steps)))
            selected_action.triggered.connect(
                lambda _checked=False, values=tuple(selected_steps):
                self.develop_method(selected_steps=values)
            )
        else:
            action = menu.addAction(tr("从此位置开始执行"))
            action.triggered.connect(
                lambda _checked=False, value=step_index: self.develop_method(value)
            )
            single_action = menu.addAction(tr("单独执行此步骤"))
            single_action.triggered.connect(
                lambda _checked=False, value=step_index: self.develop_method(value, True)
            )
        menu.addSeparator()
        insert_action = menu.addAction(tr("在此后插入步骤"))
        insert_action.triggered.connect(
            lambda _checked=False, value=step_index: self._add_workflow_step(insert_after=value)
        )
        delete_steps = tuple(selected_steps) if step_index in selected_steps else (step_index,)
        delete_action = menu.addAction(
            tr("删除选中的 {0} 个步骤", len(delete_steps))
            if len(delete_steps) > 1 else tr("删除此步骤")
        )
        delete_action.triggered.connect(
            lambda _checked=False, values=delete_steps: self._delete_workflow_steps(values)
        )
        menu.exec(position)

    def _delete_workflow_steps(self, step_indexes: tuple[int, ...]) -> None:
        if self.view_mode != "task" or self._deploying or self._structured_workflow_steps is None:
            return
        indexes = {
            index for index in step_indexes
            if 1 <= index <= len(self._structured_workflow_steps)
        }
        if not indexes:
            return
        if QMessageBox.question(
            self,
            tr("删除步骤"),
            tr("确定删除第 {0} 步吗？", "、".join(map(str, sorted(indexes)))),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._structured_workflow_steps = [
            step for index, step in enumerate(self._structured_workflow_steps, start=1)
            if index not in indexes
        ]
        self.workflow_panel.clear_selection()
        self._set_editor_content(self._structured_workflow_summary(), preserve_view=True)
        self._mark_changed(tr("已删除 {0} 个步骤", len(indexes)))

    def _sync_editor_display(self, _checked: bool = False) -> None:
        task_view = self.view_mode == "task"
        parameter_view = self.view_mode == "parameter"
        show_editor = self.view_mode in {"script", "log"} or (
            task_view and self.parameter_view_button.isChecked()
        )
        show_flow = task_view and self.flow_view_button.isChecked()
        self.editor.setVisible(show_editor)
        self.workflow_panel.setVisible(show_flow)
        self.server_parameter_form.setVisible(parameter_view)
        if parameter_view:
            self.editor_splitter.setSizes([0, 1000, 0])
        elif show_editor and show_flow:
            self.editor_splitter.setSizes([700, 0, 300])
        elif show_flow:
            self.editor_splitter.setSizes([1000, 0, 0])
        elif show_editor:
            self.editor_splitter.setSizes([0, 0, 1000])

    def _save_workflow_zoom(self, zoom: float) -> None:
        settings = dict(self.application_settings)
        if self.view_mode == "task" and self.current_path is not None:
            self.workflow_task_zooms[self.current_path.name] = zoom
            settings["workflow_task_zooms"] = dict(self.workflow_task_zooms)
        else:
            self.workflow_zoom = zoom
            settings["workflow_zoom"] = zoom
        self._write_settings(settings)

    def _add_workflow_step(
        self, step_index: int | None = None, insert_after: int | None = None
    ) -> None:
        if self._structured_workflow_steps is None:
            return
        if step_index is not None and not 1 <= step_index <= len(self._structured_workflow_steps):
            return
        existing_values = self._workflow_step_values(step_index) if step_index is not None else {}
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("编辑流程步骤") if step_index is not None else tr("增加流程步骤"))
        dialog.setFixedWidth(720)
        root = QVBoxLayout(dialog)
        type_combo = QComboBox()
        for definition in WORKFLOW_TYPES:
            type_combo.addItem(tr(definition.label), definition.key)
        existing_type = existing_values.get("TYPE")
        if existing_type:
            type_index = type_combo.findData(existing_type)
            if type_index >= 0:
                type_combo.setCurrentIndex(type_index)
        form = QFormLayout()
        root.addWidget(QLabel(tr("步骤类型：")))
        root.addWidget(type_combo)
        root.addLayout(form)
        controls: dict[str, QWidget] = {}

        def clear_form() -> None:
            while form.rowCount():
                form.removeRow(0)
            controls.clear()

        def refresh() -> None:
            clear_form()
            definition = WORKFLOW_TYPE_BY_KEY[str(type_combo.currentData())]
            connections = [
                str(step.get("properties", {}).get("CONNECTION_NAME", "")).strip()
                for step in self._structured_workflow_steps
                if str(step.get("type", "")).upper() == "SERVER_PARAMETER"
            ]
            artifacts = [
                str(step.get("properties", {}).get("ARTIFACT_NAME", "")).strip()
                for step in self._structured_workflow_steps
                if str(step.get("type", "")).upper() == "BUILD"
            ]
            for field in definition.fields:
                values: list[str] | None = None
                if field.selector == "parameter":
                    values = [p.name for p in sorted(self.parameter_dir.glob("*.txt"))]
                elif field.selector in {"remote_script", "local_script", "script"}:
                    values = [p.name for p in sorted(self.script_dir.iterdir()) if p.is_file()]
                elif field.selector == "connection":
                    values = connections
                elif field.selector == "artifact":
                    values = artifacts
                elif field.selector in {
                    "yes_no", "boolean", "create_remote_path", "backup_enabled"
                }:
                    values = ["NO", "YES"]
                elif field.selector in {"source", "upload_source"}:
                    values = ["ARTIFACT", "LOCAL_FILE", "LOCAL_FOLDER"]
                elif field.selector == "folder_mode":
                    values = ["INCLUDE_FOLDER", "CONTENTS_ONLY"]
                if values is not None:
                    control = QComboBox()
                    control.setEditable(field.selector not in {"yes_no", "boolean", "source"})
                    control.addItems(values)
                    control.setCurrentText(existing_values.get(field.key, field.default))
                else:
                    control = QLineEdit(existing_values.get(field.key, field.default))
                controls[field.key] = control
                form.addRow(tr(field.label) + (" *" if field.required else ""), control)
            if definition.key == "UPLOAD":
                def update_upload_controls() -> None:
                    source = controls["SOURCE_MODE"].currentText()
                    controls["ARTIFACT_NAME"].setEnabled(source == "ARTIFACT")
                    controls["LOCAL_PATH"].setEnabled(source == "LOCAL_FILE")
                    controls["FILE_NAME"].setEnabled(source == "LOCAL_FILE")
                    controls["FOLDER_PATH"].setEnabled(source == "LOCAL_FOLDER")
                    controls["FOLDER_MODE"].setEnabled(source == "LOCAL_FOLDER")
                    controls["BACKUP_PATH"].setEnabled(
                        controls["BACKUP_ENABLED"].currentText() == "YES"
                    )
                controls["SOURCE_MODE"].currentTextChanged.connect(update_upload_controls)
                controls["BACKUP_ENABLED"].currentTextChanged.connect(update_upload_controls)
                update_upload_controls()
            dialog.adjustSize()

        type_combo.currentIndexChanged.connect(refresh)
        refresh()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        dialog.resize(720, min(720, dialog.sizeHint().height()))
        if dialog.exec() != QDialog.Accepted:
            return
        definition = WORKFLOW_TYPE_BY_KEY[str(type_combo.currentData())]
        values: dict[str, str] = {}
        missing: list[str] = []
        for field in definition.fields:
            control = controls[field.key]
            value = control.currentText().strip() if isinstance(control, QComboBox) else control.text().strip()
            values[field.key] = value
            if field.required and not value:
                missing.append(tr(field.label))
        if missing:
            QMessageBox.critical(self, tr("参数不足"), tr("请填写：") + "、".join(missing))
            return
        index = (
            step_index if step_index is not None
            else insert_after + 1 if insert_after is not None
            else len(self._structured_workflow_steps) + 1
        )
        step_object = {
            "type": definition.key,
            "properties": {
                field.key: values[field.key]
                for field in definition.fields
                if values[field.key] or field.required
            },
        }
        if step_index is not None:
            self._structured_workflow_steps[step_index - 1] = step_object
        elif insert_after is not None:
            self._structured_workflow_steps.insert(insert_after, step_object)
        else:
            self._structured_workflow_steps.append(step_object)
        content = self._structured_workflow_summary()
        self.editor.setReadOnly(True)
        status = tr("已暂存第 {0} 步：{1}", index, tr(definition.label))
        self._set_editor_content(content, preserve_view=True)
        self._mark_changed(status)

    def _mark_changed(self, status: str) -> None:
        if not self._dirty and self.current_path is not None:
            self._ensure_current_history_baseline()
        self._dirty = True
        self.editor.document().setModified(True)
        self._write_current_draft()
        if self.current_path is not None:
            self.history_timer.start()
        self.status_label.setText(status + tr("，点击保存后生效"))

    def _execute_selected_or_all(self) -> None:
        if self._deploying:
            self.develop_method()
            return
        selected_steps = tuple(self.workflow_panel.selected_steps())
        self.develop_method(selected_steps=selected_steps or None, confirm_execution=False)

    def develop_method(
        self,
        start_step: int | None = None,
        single_step: bool = False,
        selected_steps: tuple[int, ...] | None = None,
        *,
        confirm_execution: bool = True,
    ) -> None:
        if isinstance(start_step, bool):
            start_step = None
        if self._deploying:
            self._request_stop_execution()
            return
        if self.view_mode != "task":
            QMessageBox.warning(self, tr("无法执行"), tr("请先切换到任务列表并选择任务"))
            return
        path = self._require_current_path()
        if path is None:
            return
        if self._dirty or self._draft_path(path, "task").is_file():
            QMessageBox.warning(self, tr("任务尚未保存"), tr("当前任务只有暂存内容，请先点击保存后再执行"))
            return
        try:
            workflow_task = load_workflow_task(path)
        except (ConfigurationError, OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(self, tr("任务配置错误"), str(exc))
            return
        selected_indexes = None
        if selected_steps:
            selected_indexes = tuple(sorted(set(selected_steps)))
            available_indexes = {step.index for step in workflow_task.steps}
            missing_indexes = [
                index for index in selected_indexes if index not in available_indexes
            ]
            if missing_indexes:
                QMessageBox.warning(
                    self,
                    tr("无法执行"),
                    tr("任务中不存在步骤：") + "、".join(map(str, missing_indexes)),
                )
                return
            steps = tuple(
                step for step in workflow_task.steps if step.index in selected_indexes
            )
            workflow_task = WorkflowTask(
                workflow_task.name,
                workflow_task.file_path,
                steps,
            )
            confirmation = tr(
                "任务：{0}\n执行选中的步骤：{1}\n\n确定执行吗？",
                workflow_task.name,
                "、".join(map(str, selected_indexes)),
            )
        elif start_step is not None:
            steps = tuple(
                step for step in workflow_task.steps
                if step.index == start_step or (not single_step and step.index >= start_step)
            )
            if not steps or steps[0].index != start_step:
                QMessageBox.warning(self, tr("无法执行"), tr("任务中不存在第 {0} 步", start_step))
                return
            workflow_task = WorkflowTask(
                workflow_task.name,
                workflow_task.file_path,
                steps,
            )
            confirmation = (
                tr("任务：{0}\n单独执行第 {1} 步\n\n确定执行吗？", workflow_task.name, start_step)
                if single_step else tr(
                    "任务：{0}\n从第 {1} 步开始，共执行 {2} 个步骤\n\n确定执行吗？",
                    workflow_task.name, start_step, len(workflow_task.steps),
                )
            )
        else:
            confirmation = tr("任务：{0}\n共 {1} 个步骤\n\n确定执行吗？", workflow_task.name, len(workflow_task.steps))
        if confirm_execution and QMessageBox.question(self, tr("确认执行"), confirmation) != QMessageBox.Yes:
            return
        self._start_workflow(workflow_task, start_step, single_step, selected_indexes)
        return

    def _prepare_execution(self, title: str) -> None:
        self._show_interaction_panel(force=True)
        self.interaction_tabs.setCurrentIndex(0)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        if self.current_path is not None:
            self._ensure_task_execution_log(self.current_path)
        self._append_log(
            tr("========== 执行时间：{0} ==========", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        self._append_log(title)
        self._deploying = True
        self._stop_requested = False
        self._execution_cancel_event.clear()
        self._update_controls()

    def _start_workflow(
        self,
        task: WorkflowTask,
        start_step: int | None = None,
        single_step: bool = False,
        selected_steps: tuple[int, ...] | None = None,
    ) -> None:
        start_message = (
            tr("执行选中的步骤：") + "、".join(map(str, selected_steps)) + "\n"
            if selected_steps
            else tr("单独执行第 {0} 步\n", start_step)
            if single_step and start_step is not None
            else tr("从此位置开始执行\n")
            if start_step is not None
            else ""
        )
        self._prepare_execution(tr("执行任务：{0}\n{1}共 {2} 个步骤，将按配置顺序执行", task.name, start_message, len(task.steps)))
        self.status_label.setText(tr("正在执行……"))
        threading.Thread(target=self._workflow_worker, args=(task,), name="workflow-worker", daemon=True).start()

    def _workflow_worker(self, task: WorkflowTask) -> None:
        try:
            WorkflowExecutor(self.parameter_dir, self.script_dir, cancel_event=self._execution_cancel_event).execute(
                task, self.worker_signals.log.emit, self.worker_signals.status.emit, self.worker_signals.progress.emit
            )
        except Exception as exc:
            kind = "workflow_cancelled" if self._execution_cancel_event.is_set() else "workflow_error"
            self.worker_signals.finished.emit(kind, str(exc))
        else:
            self.worker_signals.finished.emit("workflow_success", task.name)

    def _request_stop_execution(self) -> None:
        if not self._deploying or self._stop_requested:
            return
        if QMessageBox.question(self, tr("确认停止任务"), tr("确定要停止当前任务吗？")) != QMessageBox.Yes:
            return
        self._stop_requested = True
        self._execution_cancel_event.set()
        self.status_label.setText(tr("正在停止任务……"))
        self._append_log(tr("用户请求停止任务，正在结束当前操作……"))
        self._update_controls()

    def _set_worker_status(self, value: str) -> None:
        if not self._stop_requested:
            self.status_label.setText(value)

    def _update_progress(self, transferred: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(transferred * 100 / total))
        self.progress_bar.setValue(percent)
        if not self._stop_requested:
            self.status_label.setText(tr("正在上传……"))

    def _worker_finished(self, kind: str, payload: object) -> None:
        self._deploying = False
        self._stop_requested = False
        self._execution_cancel_event.clear()
        self._update_controls()
        self.status_label.setText(tr("就绪"))
        if kind == "workflow_success":
            self.progress_bar.setValue(100)
            self._append_log(tr("任务执行完成"))
            self._show_topmost_message(
                QMessageBox.Information,
                tr("执行完成"),
                tr("任务“{0}”已执行完成", payload),
            )
        elif kind == "workflow_error":
            self._append_log(tr("执行失败：{0}", payload))
            QMessageBox.critical(self, tr("执行失败"), str(payload))
        elif kind == "workflow_cancelled":
            self._append_log(tr("任务已停止"))
            QMessageBox.information(self, tr("任务已停止"), tr("当前任务已停止"))
        self._sync_interaction_panel_visibility()

    def _ensure_task_execution_log(self, task_path: Path) -> None:
        task_path = task_path.resolve()
        if (
            self._active_execution_task_path == task_path
            and self._active_execution_log is not None
            and self._active_execution_log.is_file()
        ):
            return
        self._execution_log_write_failed = False
        created_at = datetime.now()
        date_dir = self.execution_log_root / created_at.strftime("%Y-%m-%d")
        time_value = created_at.strftime("%H-%M-%S-%f")
        path = date_dir / f"{task_path.stem}_{time_value}.log"
        try:
            date_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                timestamp_log_text(
                    tr("打开任务：{0}\n打开时间：{1}\n", task_path.stem, created_at.strftime('%Y-%m-%d %H:%M:%S')),
                    created_at,
                )[0],
                encoding="utf-8",
            )
        except OSError:
            self._active_execution_log = None
            self._active_execution_task_path = None
            self._execution_log_write_failed = True
            return
        self._active_execution_log = path
        self._active_execution_task_path = task_path

    def _ensure_parameter_log(self, parameter_path: Path) -> Path | None:
        parameter_path = parameter_path.resolve()
        if (
            self._active_parameter_path == parameter_path
            and self._active_parameter_log is not None
            and self._active_parameter_log.is_file()
        ):
            return self._active_parameter_log
        created_at = datetime.now()
        date_dir = self.execution_log_root / created_at.strftime("%Y-%m-%d")
        time_value = created_at.strftime("%H-%M-%S-%f")
        path = date_dir / f"{parameter_path.stem}_{time_value}.log"
        try:
            date_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                timestamp_log_text(
                    tr("打开配置：{0}\n打开时间：{1}\n", parameter_path.stem, created_at.strftime('%Y-%m-%d %H:%M:%S')),
                    created_at,
                )[0],
                encoding="utf-8",
            )
        except OSError as exc:
            self._active_parameter_log = None
            self._active_parameter_path = None
            self.status_label.setText(tr("配置日志保存失败：{0}", exc))
            return None
        self._active_parameter_log = path
        self._active_parameter_path = parameter_path
        return path

    def _append_parameter_log(
        self, log_path: Path | None, event_type: str, value: str
    ) -> None:
        if log_path is None or not value:
            return
        self._parameter_log_writer.append(log_path, event_type, value)

    def _parameter_log_appended(
        self, log_path: Path, text: str, sequence: int
    ) -> None:
        if self.view_mode == "log" and self.current_path == log_path:
            if sequence <= self._displayed_log_sequences.get(log_path, 0):
                return
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(text)
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()
            self._displayed_log_sequences[log_path] = sequence

    def _parameter_log_failed(self, error: str) -> None:
        self.status_label.setText(tr("配置日志保存失败：{0}", error))

    def _show_topmost_message(
        self, icon: QMessageBox.Icon, title: str, message: str
    ) -> None:
        QApplication.alert(self, 0)
        dialog = QMessageBox(icon, title, message, QMessageBox.Ok, None)
        dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        QTimer.singleShot(
            0,
            lambda target=dialog: (target.raise_(), target.activateWindow()),
        )
        dialog.exec()

    def _append_log(self, value: str) -> None:
        line = timestamp_log_text(value.rstrip() + "\n")[0]
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(line)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()
        if self._active_execution_log is not None:
            try:
                with self._active_execution_log.open("a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError as exc:
                self._active_execution_log = None
                if not self._execution_log_write_failed:
                    self._execution_log_write_failed = True
                    self.status_label.setText(tr("执行日志保存失败：{0}", exc))

    def _toggle_ssh_connection(self) -> None:
        self._open_ssh_connection()

    def _direct_ssh_connection(self, path: str, command: str) -> None:
        path = path.strip()
        if not path:
            return
        self._open_ssh_connection(path, command.strip() or None)

    def _open_ssh_connection(
        self,
        direct_path: str | None = None,
        direct_command: str | None = None,
        *,
        parameter_path: Path | None = None,
    ) -> None:
        if parameter_path is None:
            if self.view_mode != "parameter" or self.current_path is None:
                QMessageBox.warning(self, tr("无法连接"), tr("请在“配置”页面选择服务器配置文件"))
                return
            parameter_path = self.current_path
        parameter_path = parameter_path.resolve()
        editing_target = (
            self.view_mode == "parameter" and self.current_path is not None
            and self.current_path.resolve() == parameter_path
        )
        if editing_target and self._dirty and not self.save_text(show_message=False):
            return
        try:
            parameters = load_server_parameters(parameter_path)
        except ConfigurationError as exc:
            QMessageBox.critical(self, tr("服务器配置错误"), str(exc))
            return
        default_path: str | None = None
        default_command: str | None = None
        if direct_path is not None:
            default_path = direct_path
            default_command = direct_command
        else:
            selected_index = parameters.default_open_path_index
            if selected_index is not None and selected_index < len(parameters.default_open_paths):
                default_path = parameters.default_open_paths[selected_index]
                command_enabled = (
                    selected_index < len(parameters.default_open_command_enabled)
                    and parameters.default_open_command_enabled[selected_index]
                )
                if command_enabled and selected_index < len(parameters.default_open_commands):
                    default_command = parameters.default_open_commands[selected_index]
        parameter_log = self._ensure_parameter_log(parameter_path)
        self._append_parameter_log(
            parameter_log, tr("连接服务器"), parameters.target
        )
        if default_path:
            self._append_parameter_log(parameter_log, tr("进入目录"), default_path)
        if default_command:
            self._append_parameter_log(parameter_log, tr("自动执行命令"), default_command)
        tab = QtSSHTerminalTab(
            self.interaction_tabs, parameters, parameter_path, default_path, default_command,
            self._on_ssh_state_changed, self._close_ssh_tab,
            lambda event_type, value, target=parameter_log:
            self._append_parameter_log(target, event_type, value),
            lambda target=parameter_log: (
                self._parameter_log_writer.read_text(target)[0]
                if target is not None and target.is_file() else ""
            ),
            self._toggle_ssh_tool_mode,
            self.ssh_monitor_panel_width,
            self._save_ssh_monitor_panel_width,
            ip_hiding=self.application_settings.get("hide_ip_address") is True,
        )
        tab.apply_theme(self._theme_colors())
        tabs = self.ssh_tabs.setdefault(parameter_path, [])
        tabs.append(tab)
        sequence = self._ssh_tab_sequence.get(parameter_path, 0) + 1
        self._ssh_tab_sequence[parameter_path] = sequence
        name = parameters.name if sequence == 1 else f"{parameters.name} ({sequence})"
        self._ssh_tab_names[tab] = name
        updates_enabled = self.right_splitter.updatesEnabled()
        self.right_splitter.setUpdatesEnabled(False)
        try:
            tab.set_tool_mode(
                self._ssh_tool_mode, self._ssh_tool_mode or not self._deploying,
            )
            index = self.interaction_tabs.addTab(tab, name)
            self._refresh_ssh_tab_buttons()
            self.interaction_tabs.setCurrentIndex(index)
            self._show_interaction_panel(force=True)
        finally:
            self.right_splitter.setUpdatesEnabled(updates_enabled)
        tab.start_connection()
        self.status_label.setText(tr("正在连接 {0}……", tab.display_target))

    def _refresh_ssh_tab_buttons(self) -> None:
        bar = self.interaction_tabs.tabBar()
        for index in range(self.interaction_tabs.count()):
            old = bar.tabButton(index, QTabBar.RightSide)
            bar.setTabButton(index, QTabBar.RightSide, None)
            if old is not None:
                old.deleteLater()
            widget = self.interaction_tabs.widget(index)
            buttons = QWidget(bar)
            layout = QHBoxLayout(buttons)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            if isinstance(widget, QtSSHTerminalTab):
                close_button = QPushButton("×")
                close_button.setFlat(True)
                close_button.setFixedSize(22, 22)
                close_button.setToolTip(tr("关闭此连接"))
                close_button.clicked.connect(lambda _checked=False, tab=widget: self._close_ssh_tab(tab))
                layout.addWidget(close_button)
            if index == self.interaction_tabs.count() - 1:
                add_button = QPushButton("＋")
                add_button.setFlat(True)
                add_button.setFixedSize(24, 22)
                add_button.setToolTip(tr("选择服务器，打开新连接"))
                add_button.clicked.connect(self._choose_ssh_server)
                layout.addWidget(add_button)
            buttons.adjustSize()
            bar.setTabButton(index, QTabBar.RightSide, buttons)

    def _choose_ssh_server(self) -> None:
        paths = sorted(self.parameter_dir.glob("*.txt"), key=lambda path: path.name.casefold())
        if not paths:
            QMessageBox.information(self, tr("暂无服务器"), tr("请先在配置页面新建服务器配置。"))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("选择服务器"))
        dialog.resize(420, 360)
        layout = QVBoxLayout(dialog)
        servers = QListWidget()
        for path in paths:
            item = QListWidgetItem(path.stem)
            item.setData(Qt.UserRole, path)
            servers.addItem(item)
        servers.setCurrentRow(0)
        layout.addWidget(servers, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        connect_button = QPushButton(tr("连接"))
        connect_button.clicked.connect(dialog.accept)
        row.addWidget(connect_button)
        cancel_button = QPushButton(tr("取消"))
        cancel_button.clicked.connect(dialog.reject)
        row.addWidget(cancel_button)
        layout.addLayout(row)
        servers.itemDoubleClicked.connect(lambda _item: dialog.accept())
        if dialog.exec() == QDialog.Accepted and servers.currentItem() is not None:
            self._open_ssh_connection(parameter_path=servers.currentItem().data(Qt.UserRole))

    def _on_ssh_state_changed(self, tab: QtSSHTerminalTab) -> None:
        index = self.interaction_tabs.indexOf(tab)
        if index < 0:
            return
        prefix = {"connecting": "… ", "connected": "● ", "cancelled": "○ ", "error": "× ", "disconnected": "○ "}[tab.state]
        self.interaction_tabs.setTabText(index, prefix + self._ssh_tab_names.get(tab, tab.parameters.name))
        if self.interaction_tabs.currentWidget() is tab:
            if tab.state == "connected":
                self.status_label.setText(tr("SSH 已连接：{0}", tab.display_target))
            elif tab.state == "error":
                self.status_label.setText(tr("SSH 连接失败：{0}", tab.display_target))
        self._sync_interaction_panel_visibility()

    def _tab_close_requested(self, index: int) -> None:
        widget = self.interaction_tabs.widget(index)
        if isinstance(widget, QtSSHTerminalTab):
            self._close_ssh_tab(widget)

    def _close_ssh_tab(self, tab: QtSSHTerminalTab) -> None:
        if not tab.close_file_editors():
            return
        tabs = self.ssh_tabs.get(tab.parameter_path, [])
        if tab in tabs:
            tabs.remove(tab)
        if not tabs:
            self.ssh_tabs.pop(tab.parameter_path, None)
        self._ssh_tab_names.pop(tab, None)
        index = self.interaction_tabs.indexOf(tab)
        if index >= 0:
            self.interaction_tabs.removeTab(index)
        self._refresh_ssh_tab_buttons()
        tab.shutdown()
        tab.deleteLater()
        self._sync_interaction_panel_visibility()

    def _close_parameter_ssh_tabs(self, path: Path) -> None:
        for tab in list(self.ssh_tabs.get(path.resolve(), [])):
            self._close_ssh_tab(tab)

    def _toggle_interaction_panel(self) -> None:
        if self.interaction_tabs.isVisible():
            self.interaction_tabs.setVisible(False)
            self._interaction_panel_user_hidden = True
        else:
            self._show_interaction_panel(force=True)
        self._update_controls()

    def _save_ssh_monitor_panel_width(self, width: int) -> None:
        width = min(400, max(220, int(width)))
        if width == self.ssh_monitor_panel_width:
            return
        self.ssh_monitor_panel_width = width
        settings = dict(self.application_settings)
        settings["ssh_monitor_panel_width"] = width
        self._write_settings(settings)

    def _layout_dimension_changed(self, _position: int, _index: int) -> None:
        if not self._ssh_tool_mode:
            self.layout_save_timer.start()

    def _save_layout_dimensions(self) -> None:
        if self._ssh_tool_mode:
            return
        main_sizes = self.main_splitter.sizes()
        right_sizes = self.right_splitter.sizes()
        if len(main_sizes) == 2 and self.sidebar.isVisible():
            self.file_sidebar_width = min(700, max(190, int(main_sizes[0])))
        if (
            len(right_sizes) == 2
            and self.interaction_tabs.isVisible()
            and right_sizes[1] > 0
        ):
            self.ssh_console_height = min(900, max(160, int(right_sizes[1])))
        settings = dict(self.application_settings)
        settings["file_sidebar_width"] = self.file_sidebar_width
        settings["ssh_console_height"] = self.ssh_console_height
        self._write_settings(settings)

    def _toggle_ssh_tool_mode(self) -> None:
        self._set_ssh_tool_mode(not self._ssh_tool_mode)

    def _set_ssh_tool_mode(self, enabled: bool) -> None:
        ssh_tabs = [
            tab
            for tabs in self.ssh_tabs.values()
            for tab in tabs
            if self.interaction_tabs.indexOf(tab) >= 0
        ]
        if enabled and not ssh_tabs:
            QMessageBox.information(self, tr("SSH 工具"), tr("请先连接一台服务器"))
            return
        if enabled == self._ssh_tool_mode:
            return
        if enabled:
            self._ssh_tool_restore_main_sizes = self.main_splitter.sizes()
            self._ssh_tool_restore_right_sizes = self.right_splitter.sizes()
            self._ssh_tool_restore_interaction_visible = self.interaction_tabs.isVisible()
            self._ssh_tool_mode = True
            self.sidebar.setVisible(False)
            self.editor_container.setVisible(False)
            self.interaction_tabs.setVisible(True)
            self.main_splitter.setSizes([0, max(1, self.main_splitter.width())])
            self.right_splitter.setSizes([0, max(1, self.right_splitter.height())])
            current = self.interaction_tabs.currentWidget()
            if not isinstance(current, QtSSHTerminalTab):
                target = next((tab for tab in ssh_tabs if tab.connected), ssh_tabs[-1])
                self.interaction_tabs.setCurrentWidget(target)
                current = target
            if isinstance(current, QtSSHTerminalTab):
                current.focus_terminal()
        else:
            self._ssh_tool_mode = False
            self.sidebar.setVisible(True)
            self.editor_container.setVisible(True)
            if self._ssh_tool_restore_main_sizes:
                self.main_splitter.setSizes(self._ssh_tool_restore_main_sizes)
            if self._ssh_tool_restore_right_sizes:
                self.right_splitter.setSizes(self._ssh_tool_restore_right_sizes)
            self.interaction_tabs.setVisible(
                self._ssh_tool_restore_interaction_visible
            )
        self._update_controls()

    def _show_interaction_panel(self, force: bool = False) -> None:
        if self._interaction_panel_user_hidden and not force:
            return
        self.interaction_tabs.setVisible(True)
        if force:
            self._interaction_panel_user_hidden = False
        if self._ssh_tool_mode:
            self.right_splitter.setSizes([0, max(1, self.right_splitter.height())])
            self._update_controls()
            return
        sizes = self.right_splitter.sizes()
        if len(sizes) == 2 and sizes[1] < self.ssh_console_height:
            height_delta = self.ssh_console_height - sizes[1]
            self.right_splitter.setSizes(
                [max(300, sizes[0] - height_delta), self.ssh_console_height]
            )
        self._update_controls()

    def _sync_interaction_panel_visibility(self) -> None:
        if self._ssh_tool_mode:
            if any(self.ssh_tabs.values()):
                self.interaction_tabs.setVisible(True)
                self.right_splitter.setSizes(
                    [0, max(1, self.right_splitter.height())]
                )
                self._update_controls()
            else:
                self._ssh_tool_restore_interaction_visible = False
                self._set_ssh_tool_mode(False)
            return
        active = self._deploying or any(self.ssh_tabs.values())
        if active:
            if not self.interaction_tabs.isVisible():
                self._show_interaction_panel()
        elif not self._interaction_panel_user_hidden:
            self.interaction_tabs.setVisible(False)
        self._update_controls()

    def _update_controls(self) -> None:
        for index, button in enumerate(self.file_buttons):
            button.setEnabled(
                not self._deploying and (self.view_mode != "log" or index == 3)
            )
            button.setVisible(not self._ssh_tool_mode)
        for button in self.nav_buttons.values():
            button.setEnabled(not self._deploying)
        self.password_button.setVisible(
            not self._ssh_tool_mode and self.view_mode == "parameter"
        )
        if self._password_hiding_enabled():
            self.password_button.setText(tr("隐藏密码") if self._parameter_password_visible else tr("显示密码"))
        else:
            self.password_button.setText(tr("开启隐藏密码"))
        self.password_button.setEnabled(not self._deploying and (not self._password_hiding_enabled() or self.current_path is not None))
        self.connect_button.setVisible(not self._ssh_tool_mode)
        self.connect_button.setEnabled(not self._deploying and self.view_mode == "parameter" and self.current_path is not None)
        self.add_step_button.setVisible(self.view_mode == "task")
        self.history_button.setVisible(self.view_mode != "log")
        self.history_button.setEnabled(
            not self._deploying and self.current_path is not None
        )
        for button in (self.parameter_view_button, self.flow_view_button):
            button.setVisible(self.view_mode == "task")
            button.setEnabled(not self._deploying)
        self._sync_editor_display()
        self.execute_button.setText(tr("停止") if self._deploying else tr("执行"))
        self.execute_button.setEnabled((not self._deploying and self.view_mode == "task") or (self._deploying and not self._stop_requested))
        self.execute_button.setVisible(not self._ssh_tool_mode)
        self.interaction_button.setVisible(not self._ssh_tool_mode)
        self.interaction_button.setText(tr("隐藏交互窗口") if self.interaction_tabs.isVisible() else tr("显示交互窗口"))
        for tabs in self.ssh_tabs.values():
            for tab in tabs:
                tab.set_tool_mode(
                    self._ssh_tool_mode,
                    self._ssh_tool_mode or not self._deploying,
                )

    def _read_settings(self) -> dict[str, object]:
        if not self.settings_path.is_file():
            return {}
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_settings(self, settings: dict[str, object]) -> bool:
        temporary = self.settings_path.with_name(f".{self.settings_path.name}.tmp")
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.settings_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self.application_settings = settings
        return True

    def _bounded_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            return min(high, max(low, int(self.application_settings.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _bounded_float(self, key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(self.application_settings.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(high, max(low, value)) if math.isfinite(value) else default

    @staticmethod
    def _mix_color(first: QColor, second: QColor, amount: float) -> str:
        amount = min(1.0, max(0.0, amount))
        return QColor(
            round(first.red() * (1 - amount) + second.red() * amount),
            round(first.green() * (1 - amount) + second.green() * amount),
            round(first.blue() * (1 - amount) + second.blue() * amount),
        ).name()

    def _theme_colors(self) -> dict[str, str]:
        theme = str(self.application_settings.get("theme", "white"))
        if theme in _THEME_PRESETS:
            return dict(_THEME_PRESETS[theme])
        background = QColor(str(self.application_settings.get("theme_background", "#f8fafc")))
        foreground = QColor(str(self.application_settings.get("theme_foreground", "#111827")))
        if not background.isValid():
            background = QColor("#f8fafc")
        if not foreground.isValid():
            foreground = QColor("#111827")
        white = QColor("#ffffff")
        return {
            "background": background.name(),
            "surface": self._mix_color(background, white, 0.12 if background.lightness() < 128 else 0.42),
            "foreground": foreground.name(),
            "muted": self._mix_color(background, foreground, 0.58),
            "border": self._mix_color(background, foreground, 0.28),
            "sidebar": self._mix_color(background, foreground, 0.08),
            "hover": self._mix_color(background, foreground, 0.13),
            "selection": self._mix_color(background, QColor("#2563eb"), 0.48),
            "selection_text": "#ffffff" if QColor("#2563eb").lightness() < 160 else foreground.name(),
        }

    @staticmethod
    def _set_palette_colors(palette: QPalette, colors: dict[str, str]) -> None:
        background = QColor(colors["background"])
        surface = QColor(colors["surface"])
        foreground = QColor(colors["foreground"])
        muted = QColor(colors["muted"])
        palette.setColor(QPalette.Window, background)
        palette.setColor(QPalette.WindowText, foreground)
        palette.setColor(QPalette.Base, surface)
        palette.setColor(QPalette.AlternateBase, QColor(colors["hover"]))
        palette.setColor(QPalette.Text, foreground)
        palette.setColor(QPalette.Button, QColor(colors["sidebar"]))
        palette.setColor(QPalette.ButtonText, foreground)
        palette.setColor(QPalette.ToolTipBase, surface)
        palette.setColor(QPalette.ToolTipText, foreground)
        palette.setColor(QPalette.PlaceholderText, muted)
        palette.setColor(QPalette.Highlight, QColor(colors["selection"]))
        palette.setColor(QPalette.HighlightedText, QColor(colors["selection_text"]))
        if colors == _THEME_PRESETS["dark"]:
            palette.setColor(QPalette.Light, QColor(colors["border"]))
            palette.setColor(QPalette.Midlight, QColor(colors["hover"]))
            palette.setColor(QPalette.Mid, QColor(colors["border"]))
            palette.setColor(QPalette.Dark, background)
            palette.setColor(QPalette.Shadow, QColor("#16181c"))
            palette.setColor(QPalette.Link, QColor("#93b4e4"))
            palette.setColor(QPalette.LinkVisited, QColor("#b6a7d9"))
        palette.setColor(QPalette.Disabled, QPalette.Text, muted)
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, muted)
        palette.setColor(QPalette.Disabled, QPalette.WindowText, muted)

    def _menu_style(self) -> str:
        colors = self._theme_colors()
        return (
            f"QMenu {{ background:{colors['surface']}; color:{colors['foreground']}; border:1px solid {colors['border']}; }}"
            "QMenu::item { background:transparent; padding:6px 24px; }"
            f"QMenu::item:selected {{ background:{colors['selection']}; color:{colors['selection_text']}; }}"
            f"QMenu::item:disabled {{ color:{colors['muted']}; }}"
        )

    @staticmethod
    def _navigation_icon(kind: str, color: str) -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        center = QPointF(8, 8)
        if kind == "language":
            painter.drawEllipse(center, 6, 6)
            painter.drawEllipse(center, 2.75, 6)
            painter.drawLine(QPointF(2, 8), QPointF(14, 8))
        elif kind == "theme":
            painter.drawEllipse(center, 2.75, 2.75)
            for index in range(8):
                angle = math.radians(index * 45)
                painter.drawLine(
                    QPointF(8 + 5 * math.cos(angle), 8 + 5 * math.sin(angle)),
                    QPointF(8 + 6.5 * math.cos(angle), 8 + 6.5 * math.sin(angle)),
                )
        else:
            outline = QPainterPath()
            for index in range(8):
                for offset, radius in ((-22.5, 4.8), (-12, 6.5), (12, 6.5), (22.5, 4.8)):
                    angle = math.radians(index * 45 + offset)
                    point = QPointF(8 + radius * math.cos(angle), 8 + radius * math.sin(angle))
                    if index == 0 and offset == -22.5:
                        outline.moveTo(point)
                    else:
                        outline.lineTo(point)
            outline.closeSubpath()
            painter.drawPath(outline)
            painter.drawEllipse(center, 2.2, 2.2)
        painter.end()
        return QIcon(pixmap)

    def _apply_theme(self) -> None:
        colors = self._theme_colors()
        self.language_button.setIcon(self._navigation_icon("language", colors["foreground"]))
        self.theme_button.setIcon(self._navigation_icon("theme", colors["foreground"]))
        self.settings_button.setIcon(self._navigation_icon("settings", colors["foreground"]))
        application = QApplication.instance()
        if application is not None:
            application.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if QColor(colors["background"]).lightness() < 128 else Qt.ColorScheme.Light
            )
            palette = application.style().standardPalette()
            self._set_palette_colors(palette, colors)
            application.setPalette(palette)
            application.setStyleSheet(
                f"QToolTip {{ color:{colors['foreground']}; background:{colors['surface']}; "
                f"border:1px solid {colors['border']}; padding:4px; }}"
            )
        self.sidebar.setStyleSheet(
            f"QFrame#fileSidebar {{ background:{colors['sidebar']}; border:1px solid {colors['border']}; }}"
            f"QPushButton#viewModeButton {{ background:{colors['sidebar']}; border:0; color:{colors['foreground']}; font-weight:600; padding:8px; text-align:left; }}"
            f"QPushButton#viewModeButton:hover {{ background:{colors['hover']}; }}"
            f"QPushButton#viewModeButton:checked {{ background:{colors['surface']}; }}"
            f"QPushButton#viewModeButton:disabled {{ color:{colors['muted']}; }}"
            f"QPushButton#settingsButton {{ background:{colors['sidebar']}; border:0; color:{colors['foreground']}; font-weight:600; padding:8px; text-align:left; }}"
            f"QPushButton#settingsButton:hover {{ background:{colors['hover']}; }}"
            f"QListWidget#fileList {{ background:{colors['surface']}; border:0; color:{colors['foreground']}; outline:0; padding:0; }}"
            "QListWidget#fileList::item { min-height:26px; padding:0 8px; }"
            f"QListWidget#fileList::item:selected {{ background:{colors['selection']}; color:{colors['selection_text']}; }}"
        )
        self.view_toggle.setStyleSheet(
            f"QFrame#workflowViewToggle {{ border:1px solid {colors['border']}; border-radius:4px; }}"
            f"QPushButton {{ border:0; color:{colors['foreground']}; background:{colors['surface']}; }}"
            f"QPushButton#flowViewToggleButton {{ border-right:1px solid {colors['border']}; border-top-left-radius:3px; border-bottom-left-radius:3px; }}"
            "QPushButton#parameterViewToggleButton { border-top-right-radius:3px; border-bottom-right-radius:3px; }"
            f"QPushButton:checked {{ background:{colors['selection']}; color:{colors['selection_text']}; font-weight:600; }}"
        )
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background:{colors['surface']}; color:{colors['foreground']}; "
            f"border:1px solid {colors['border']}; padding:8px; }}"
        )
        self.workflow_panel.apply_theme(colors)
        self.server_parameter_form.apply_theme(colors)
        for tabs in self.ssh_tabs.values():
            for tab in tabs:
                tab.apply_theme(colors)
        self.update()

    def _show_language_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("语言"))
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        choices = QComboBox()
        for code, name in LANGUAGES.items():
            choices.addItem(name, code)
        saved = str(self.application_settings.get("language", current_language()))
        choices.setCurrentIndex(max(0, choices.findData(saved)))
        form.addRow(tr("语言："), choices)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        settings = {**self.application_settings, "language": choices.currentData()}
        if not self._write_settings(settings):
            QMessageBox.critical(self, tr("保存失败"), tr("无法保存语言设置"))
            return
        initialize(settings["language"])
        install_qt_translations(QApplication.instance())
        refresh_translations()
        self._refresh_language_display()
        QMessageBox.information(self, tr("保存成功"), tr("语言已切换并保存。"))

    def _refresh_language_display(self) -> None:
        navigation_buttons = [*self.nav_buttons.values(), self.language_button, self.theme_button, self.settings_button]
        width = max(68, *(button.fontMetrics().horizontalAdvance(button.text()) + 40 for button in navigation_buttons))
        for button in navigation_buttons:
            button.setFixedWidth(width)
        for button, minimum in (
            (self.add_step_button, 88), (self.history_button, 88),
            (self.flow_view_button, 68), (self.parameter_view_button, 68),
            (self.server_parameter_form.password_auth_button, 88),
            (self.server_parameter_form.key_auth_button, 88),
        ):
            button.setFixedWidth(max(minimum, button.fontMetrics().horizontalAdvance(button.text()) + 24))
        self._refresh_workflow_diagram(preserve_view=True)
        if self.view_mode == "task" and self._structured_workflow_steps is not None:
            modified = self.editor.document().isModified()
            cursor = self.editor.textCursor()
            position, anchor = cursor.position(), cursor.anchor()
            scroll = self.editor.verticalScrollBar().value()
            horizontal = self.editor.horizontalScrollBar().value()
            self._set_structured_workflow_display(self.workflow_panel.selected_steps() or None)
            self.editor.document().setModified(modified)
            cursor = self.editor.textCursor()
            end = self.editor.document().characterCount() - 1
            cursor.setPosition(min(anchor, end))
            cursor.setPosition(min(position, end), QTextCursor.KeepAnchor)
            self.editor.setTextCursor(cursor)
            self.editor.verticalScrollBar().setValue(scroll)
            self.editor.horizontalScrollBar().setValue(horizontal)

    def _show_settings(self, initial_tab: str = "general") -> None:
        if self._deploying:
            QMessageBox.warning(self, tr("正在部署"), tr("部署完成后才能打开系统设置"))
            return
        theme_only = initial_tab == "theme"
        if not theme_only and (
            not self._require_administrator_password()
            or not self._confirm_pending_changes()
            or not self._conceal_current_parameter_password()
        ):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("主题设置") if theme_only else tr("系统设置"))
        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        general = QWidget()
        general_form = QFormLayout(general)
        startup = QComboBox()
        startup_values = {
            tr("恢复上次页面"): "last",
            tr("任务页面"): "task",
            tr("配置页面"): "parameter",
            tr("脚本页面"): "script",
            tr("日志页面"): "log",
        }
        startup.addItems(startup_values)
        current_startup = str(self.application_settings.get("startup_page", "last"))
        startup.setCurrentText(next((label for label, value in startup_values.items() if value == current_startup), tr("恢复上次页面")))
        password_hiding = QCheckBox(tr("开启服务器密码隐藏"))
        password_hiding.setChecked(self._password_hiding_enabled())
        ip_hiding = QCheckBox(tr("隐藏IP"))
        ip_hiding.setChecked(self.application_settings.get("hide_ip_address") is True)
        change_password = QPushButton(tr("更改密码"))
        change_password.clicked.connect(self._change_administrator_password)
        general_form.addRow(tr("启动时打开："), startup)
        general_form.addRow(tr("密码隐藏："), password_hiding)
        general_form.addRow(ip_hiding)
        general_form.addRow(tr("管理员密码："), change_password)
        if not theme_only:
            tabs.addTab(general, tr("常规"))
        theme_page = QWidget()
        theme_form = QFormLayout(theme_page)
        theme_box = QComboBox()
        theme_values = {
            tr("简约白"): "white",
            tr("深邃黑"): "dark",
            tr("书页黄"): "paper",
            tr("护眼绿"): "green",
            tr("自定义"): "custom",
        }
        theme_box.addItems(theme_values)
        current_theme = str(self.application_settings.get("theme", "white"))
        if current_theme not in {*_THEME_PRESETS, "custom"}:
            current_theme = "white"
        theme_box.setCurrentText(next(
            label for label, value in theme_values.items() if value == current_theme
        ))
        initial_colors = self._theme_colors()
        theme_state = {
            "background": initial_colors["background"],
            "foreground": initial_colors["foreground"],
        }
        background_button = QPushButton()
        foreground_button = QPushButton()
        background_label = QLabel(tr("背景颜色："))
        foreground_label = QLabel(tr("字体颜色："))

        def update_color_button(button: QPushButton, color_name: str) -> None:
            color = QColor(color_name)
            button.setText("")
            button.setFixedHeight(28)
            button.setToolTip(tr("点击打开调色板"))
            button.setStyleSheet(
                f"QPushButton {{ background:{color.name()}; "
                "border:1px solid #64748b; border-radius:4px; }"
            )

        def refresh_theme_colors() -> None:
            update_color_button(background_button, theme_state["background"])
            update_color_button(foreground_button, theme_state["foreground"])

        def refresh_custom_controls() -> None:
            visible = theme_values.get(theme_box.currentText()) == "custom"
            background_label.setVisible(visible)
            background_button.setVisible(visible)
            foreground_label.setVisible(visible)
            foreground_button.setVisible(visible)

        def preset_changed(label: str) -> None:
            preset = theme_values.get(label, "white")
            if preset in _THEME_PRESETS:
                theme_state["background"] = _THEME_PRESETS[preset]["background"]
                theme_state["foreground"] = _THEME_PRESETS[preset]["foreground"]
                refresh_theme_colors()
            refresh_custom_controls()

        def choose_theme_color(key: str, title: str) -> None:
            selected = QColorDialog.getColor(QColor(theme_state[key]), dialog, title)
            if not selected.isValid():
                return
            theme_state[key] = selected.name()
            blocker = QSignalBlocker(theme_box)
            theme_box.setCurrentText(tr("自定义"))
            del blocker
            refresh_theme_colors()
            refresh_custom_controls()

        theme_box.currentTextChanged.connect(preset_changed)
        background_button.clicked.connect(
            lambda: choose_theme_color("background", tr("选择主题背景颜色"))
        )
        foreground_button.clicked.connect(
            lambda: choose_theme_color("foreground", tr("选择主题字体颜色"))
        )
        refresh_theme_colors()
        theme_form.addRow(tr("主题："), theme_box)
        theme_form.addRow(background_label, background_button)
        theme_form.addRow(foreground_label, foreground_button)
        refresh_custom_controls()
        editor_page = QWidget()
        editor_form = QFormLayout(editor_page)
        auto_delay = QDoubleSpinBox()
        auto_delay.setRange(0.5, 30.0)
        auto_delay.setSingleStep(0.5)
        auto_delay.setValue(self.auto_save_delay_seconds)
        editor_form.addRow(tr("自动暂存等待秒数："), auto_delay)
        if not theme_only:
            tabs.addTab(editor_page, tr("编辑器与保存"))
            layout.addWidget(tabs)
        else:
            layout.addWidget(theme_page)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        if not theme_only and password_hiding.isChecked() != self._password_hiding_enabled():
            changed = self._enable_password_hiding(False) if password_hiding.isChecked() else self._disable_password_hiding(False)
            if not changed:
                return
        settings = dict(self.application_settings)
        if theme_only:
            settings.update({
                "theme": theme_values[theme_box.currentText()],
                "theme_background": theme_state["background"],
                "theme_foreground": theme_state["foreground"],
            })
        if not theme_only:
            settings.update({
                "startup_page": startup_values[startup.currentText()],
                "auto_save_delay_seconds": auto_delay.value(),
                "hide_ip_address": ip_hiding.isChecked(),
            })
        if not self._write_settings(settings):
            QMessageBox.critical(self, tr("保存失败"), tr("无法保存系统设置"))
            return
        if not theme_only:
            self.auto_save_delay_seconds = auto_delay.value()
            self.server_parameter_form.set_ip_hiding(ip_hiding.isChecked())
            for connections in self.ssh_tabs.values():
                for tab in connections:
                    tab.set_ip_hiding(ip_hiding.isChecked())
            current_tab = self.interaction_tabs.currentWidget()
            if isinstance(current_tab, QtSSHTerminalTab):
                self._on_ssh_state_changed(current_tab)
        self._apply_theme()
        QMessageBox.information(self, tr("保存成功"), tr("系统设置已保存"))

    def _ask_password(self, title: str, prompt: str) -> str | None:
        dialog = _PasswordDialog(self, title, prompt)
        return dialog.entry.text() if dialog.exec() == QDialog.Accepted else None

    def _prompt_new_administrator_password(self) -> str | None:
        dialog = _PasswordDialog(self, tr("设置管理员密码"), tr("新密码："), require_confirmation=True)
        return dialog.entry.text().strip() if dialog.exec() == QDialog.Accepted else None

    def _password_settings(self) -> dict[str, object]:
        value = self.application_settings.get("password_hiding")
        return dict(value) if isinstance(value, dict) else {}

    def _password_hiding_enabled(self) -> bool:
        value = self._password_settings()
        return value.get("enabled") is True and bool(str(value.get("unlock_password", "")).strip())

    def _unlock_password_hiding(self, force: bool = False) -> bool:
        if self._password_session_unlocked and not force:
            return True
        encrypted = str(self._password_settings().get("unlock_password", "")).strip()
        try:
            expected = unprotect_text(encrypted).strip()
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, tr("无法验证"), str(exc))
            return False
        entered = self._ask_password(tr("管理员验证"), tr("请输入管理员密码："))
        if entered is None:
            return False
        if not hmac.compare_digest(entered.strip().encode(), expected.encode()):
            QMessageBox.critical(self, tr("密码错误"), tr("管理员密码不正确"))
            return False
        self._password_session_unlocked = True
        if is_legacy_protected(encrypted):
            settings = dict(self.application_settings)
            hiding = self._password_settings()
            hiding["unlock_password"] = protect_text(expected)
            settings["password_hiding"] = hiding
            if not self._write_settings(settings):
                QMessageBox.critical(self, tr("保存失败"), tr("无法升级管理员密码加密格式"))
                return False
        return True

    def _require_administrator_password(self) -> bool:
        encrypted = str(self._password_settings().get("unlock_password", "")).strip()
        if encrypted:
            return self._unlock_password_hiding(force=True)
        value = self._prompt_new_administrator_password()
        if value is None:
            return False
        try:
            encrypted = protect_text(value)
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, tr("初始化失败"), str(exc))
            return False
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding.setdefault("enabled", False)
        hiding["unlock_password"] = encrypted
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, tr("初始化失败"), tr("无法保存管理员密码"))
            return False
        self._password_session_unlocked = True
        return True

    def _change_administrator_password(self) -> None:
        if str(self._password_settings().get("unlock_password", "")).strip() and not self._unlock_password_hiding():
            return
        value = self._prompt_new_administrator_password()
        if value is None:
            return
        try:
            encrypted = protect_text(value)
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, tr("更改失败"), str(exc))
            return
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding.setdefault("enabled", False)
        hiding["unlock_password"] = encrypted
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, tr("更改失败"), tr("无法保存管理员密码"))
            return
        self._password_session_unlocked = True
        QMessageBox.information(self, tr("更改成功"), tr("管理员密码已更改"))

    def _parameter_file_updates(self, encrypt: bool) -> list[tuple[Path, str, str]]:
        paths = list(self.parameter_dir.glob("*.txt"))
        draft_dir = self.draft_root / "parameter"
        if draft_dir.is_dir():
            paths.extend(draft_dir.glob("*.draft"))
        history_dir = self.history_root / "parameters"
        if history_dir.is_dir():
            paths.extend(history_dir.rglob("*.txt"))
        updates: list[tuple[Path, str, str]] = []
        for path in sorted(paths, key=lambda p: str(p).lower()):
            if path.is_dir():
                continue
            content = path.read_text(encoding="utf-8-sig")
            match = _PASSWORD_LINE_PATTERN.search(content)
            if match is None:
                continue
            value = match.group("value").strip()
            if not value:
                continue
            if encrypt:
                if is_protected(value):
                    if not is_legacy_protected(value):
                        continue
                    value = unprotect_text(value)
                replacement = protect_text(value)
            else:
                if not is_protected(value):
                    continue
                replacement = unprotect_text(value)
            updates.append((path, content, self._replace_password_value(content, replacement)))
        return updates

    def _apply_password_file_updates(self, updates: list[tuple[Path, str, str]]) -> bool:
        written: list[tuple[Path, str]] = []
        try:
            for path, original, updated in updates:
                temporary = path.with_name(f".{path.name}.password.tmp")
                temporary.write_text(updated, encoding="utf-8")
                temporary.replace(path)
                written.append((path, original))
        except OSError as exc:
            for path, original in reversed(written):
                try:
                    path.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            QMessageBox.critical(self, tr("密码更新失败"), str(exc))
            return False
        return True

    def _restore_password_file_updates(self, updates: list[tuple[Path, str, str]]) -> None:
        for path, original, _updated in reversed(updates):
            temporary = path.with_name(f".{path.name}.password.restore.tmp")
            try:
                temporary.write_text(original, encoding="utf-8")
                temporary.replace(path)
            except OSError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _enable_password_hiding(self, show_message: bool = True) -> bool:
        if not self._confirm_pending_changes():
            return False
        hiding = self._password_settings()
        encrypted = str(hiding.get("unlock_password", "")).strip()
        if encrypted:
            if not self._unlock_password_hiding():
                return False
        else:
            value = self._prompt_new_administrator_password()
            if value is None:
                return False
            try:
                encrypted = protect_text(value)
            except PasswordProtectionError as exc:
                QMessageBox.critical(self, tr("开启失败"), str(exc))
                return False
        try:
            updates = self._parameter_file_updates(True)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, tr("开启失败"), str(exc))
            return False
        if not self._apply_password_file_updates(updates):
            return False
        settings = dict(self.application_settings)
        hiding.update({"enabled": True, "unlock_password": encrypted})
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            self._restore_password_file_updates(updates)
            QMessageBox.critical(self, tr("开启失败"), tr("无法保存隐藏密码设置"))
            return False
        self._password_session_unlocked = True
        if self.current_path is not None:
            self._load_file(self.current_path)
        self._update_controls()
        if show_message:
            QMessageBox.information(self, tr("开启成功"), tr("已加密服务器密码。重新打开软件后默认隐藏。"))
        return True

    def _disable_password_hiding(self, show_message: bool = True) -> bool:
        if not self._password_hiding_enabled():
            return True
        if not self._unlock_password_hiding():
            return False
        try:
            updates = self._parameter_file_updates(False)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, tr("关闭失败"), str(exc))
            return False
        if not self._apply_password_file_updates(updates):
            return False
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding["enabled"] = False
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            self._restore_password_file_updates(updates)
            QMessageBox.critical(self, tr("关闭失败"), tr("无法保存密码隐藏设置"))
            return False
        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.current_path is not None:
            self._load_file(self.current_path)
        self._update_controls()
        if show_message:
            QMessageBox.information(self, tr("关闭成功"), tr("服务器密码已恢复为明文保存"))
        return True

    def _toggle_password_visibility(self) -> None:
        if not self._password_hiding_enabled():
            self._enable_password_hiding()
            return
        if self.current_path is None:
            QMessageBox.warning(self, tr("未选择配置"), tr("请先选择一个服务器配置文件"))
            return
        if self._parameter_password_visible:
            if self._conceal_current_parameter_password():
                self.status_label.setText(tr("服务器密码已隐藏"))
            return
        if not self._unlock_password_hiding():
            return
        content = self.editor.toPlainText()
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            QMessageBox.warning(self, tr("没有密码配置"), tr("当前配置中没有 PASSWORD 参数"))
            return
        encrypted = self._parameter_password_ciphertext or match.group("value").strip()
        try:
            plaintext = unprotect_text(encrypted) if encrypted and is_protected(encrypted) else encrypted
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, tr("无法显示密码"), str(exc))
            return
        self._parameter_password_ciphertext = encrypted or None
        self._visible_parameter_password = plaintext
        self._parameter_password_visible = True
        self._set_editor_content(self._replace_password_value(content, plaintext), preserve_view=True)
        self._update_controls()

    def _conceal_current_parameter_password(self) -> bool:
        if not self._parameter_password_visible:
            return True
        try:
            stored = self._content_for_storage(self.editor.toPlainText())
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, tr("无法隐藏密码"), str(exc))
            return False
        self._parameter_password_visible = False
        self._visible_parameter_password = None
        self._set_editor_content(self._replace_password_value(stored, _HIDDEN_PASSWORD_VALUE), preserve_view=True)
        self._update_controls()
        return True

    def _prepare_parameter_content_for_display(self, content: str) -> tuple[str, str]:
        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.view_mode != "parameter" or not self._password_hiding_enabled():
            return content, content
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None or not match.group("value").strip():
            return content, content
        value = match.group("value").strip()
        encrypted = protect_text(unprotect_text(value)) if is_legacy_protected(value) else (value if is_protected(value) else protect_text(value))
        self._parameter_password_ciphertext = encrypted
        return self._replace_password_value(content, _HIDDEN_PASSWORD_VALUE), self._replace_password_value(content, encrypted)

    def _content_for_storage(self, content: str) -> str:
        if self.view_mode != "parameter" or not self._password_hiding_enabled():
            return content
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            self._parameter_password_ciphertext = None
            return content
        value = match.group("value").strip()
        if self._parameter_password_visible:
            encrypted = self._parameter_password_ciphertext if self._parameter_password_ciphertext and value == self._visible_parameter_password else (protect_text(value) if value else "")
            self._parameter_password_ciphertext = encrypted or None
            self._visible_parameter_password = value
            return self._replace_password_value(content, encrypted)
        if value == _HIDDEN_PASSWORD_VALUE:
            return self._replace_password_value(content, self._parameter_password_ciphertext or "")
        if is_protected(value):
            self._parameter_password_ciphertext = value
            return content
        if value and not self._password_session_unlocked:
            raise PasswordProtectionError(tr("服务器密码当前处于隐藏状态，请先点击“显示密码”后再修改"))
        encrypted = protect_text(value) if value else ""
        self._parameter_password_ciphertext = encrypted or None
        return self._replace_password_value(content, encrypted)

    @staticmethod
    def _replace_password_value(content: str, value: str) -> str:
        match = _PASSWORD_LINE_PATTERN.search(content)
        return content if match is None else content[:match.start("value")] + value + content[match.end("value"):]

    def _zoom_editor(self, step: int) -> None:
        size = min(24, max(8, self.editor_font_size + step))
        if size == self.editor_font_size:
            return
        self.editor_font_size = size
        self.editor.setFont(QFont("Cascadia Mono", size))
        settings = dict(self.application_settings)
        settings["editor_font_size"] = size
        self._write_settings(settings)
        self.status_label.setText(tr("编辑器字体大小：{0}", size))

    def _restore_window_state(self) -> None:
        geometry = self.application_settings.get("qt_window_geometry")
        if isinstance(geometry, str):
            try:
                self.restoreGeometry(bytes.fromhex(geometry))
            except ValueError:
                pass
        state = self.application_settings.get("qt_window_state")
        if isinstance(state, str):
            try:
                self.restoreState(bytes.fromhex(state))
            except ValueError:
                pass
        self.main_splitter.setSizes([self.file_sidebar_width, 1000])
        self.right_splitter.setSizes([1000, self.ssh_console_height])
        if self.application_settings.get("window_state") == "zoomed":
            QTimer.singleShot(0, self.showMaximized)

    def _save_application_state(self) -> None:
        settings = dict(self.application_settings)
        settings.update({
            "editor_font_size": self.editor_font_size,
            "workflow_zoom": self.workflow_zoom,
            "workflow_task_zooms": dict(self.workflow_task_zooms),
            "ssh_monitor_panel_width": self.ssh_monitor_panel_width,
            "ssh_console_height": self.ssh_console_height,
            "file_sidebar_width": self.file_sidebar_width,
            "view_mode": self.view_mode,
            "selected_files": dict(self.last_selected_files),
            "window_state": "zoomed" if self.isMaximized() else "normal",
            "qt_window_geometry": bytes(self.saveGeometry()).hex(),
            "qt_window_state": bytes(self.saveState()).hex(),
            "qt_splitter_sizes": (
                self._ssh_tool_restore_right_sizes
                if self._ssh_tool_mode and self._ssh_tool_restore_right_sizes
                else self.right_splitter.sizes()
            ),
        })
        self._write_settings(settings)

    def _poll_log_shutdown(self) -> None:
        if self._parameter_log_writer.is_finished():
            self._log_shutdown_timer.stop()
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing_for_logs:
            if not self._parameter_log_writer.is_finished():
                event.ignore()
                return
            self._log_shutdown_timer.stop()
            self.setEnabled(True)
            if self._parameter_log_writer.write_error:
                QMessageBox.warning(
                    self, tr("配置日志保存失败"),
                    tr("部分日志未能写入磁盘：\n") + self._parameter_log_writer.write_error,
                )
            event.accept()
            return
        if self._deploying:
            QMessageBox.warning(self, tr("正在部署"), tr("为避免在上传、替换或重启过程中中断操作，请等待本次部署完成后再关闭。"))
            event.ignore()
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            event.ignore()
            return
        for tabs in list(self.ssh_tabs.values()):
            for tab in list(tabs):
                if not tab.close_file_editors():
                    event.ignore()
                    return
        for tabs in list(self.ssh_tabs.values()):
            for tab in list(tabs):
                tab.shutdown()
        self._parameter_log_writer.shutdown()
        self.layout_save_timer.stop()
        self._save_layout_dimensions()
        self._save_application_state()
        self._closing_for_logs = True
        self.auto_save_timer.stop()
        self.history_timer.stop()
        self.status_label.setText(tr("正在保存剩余日志，完成后自动退出…"))
        self.setEnabled(False)
        self._log_shutdown_timer.start()
        event.ignore()


def _application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def _default_data_root() -> Path:
    root = _application_root() / "conf"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _initial_data_directory(directory_name: str) -> Path:
    directory = _default_data_root() / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _initial_template(argument_index: int, name: str) -> Path:
    if len(sys.argv) > argument_index:
        value = Path(sys.argv[argument_index]).expanduser().resolve()
        if value.is_file():
            return value
    return _application_root() / "templates" / name


def main() -> None:
    if sys.platform == "win32":
        # Initialize GPU composition before showing the window, rather than
        # recreating its native surface when the first SSH WebEngine tab opens.
        os.environ.setdefault("QT_WIDGETS_RHI", "1")
        if os.environ.get("QSG_RHI_BACKEND"):
            os.environ.setdefault("QT_WIDGETS_RHI_BACKEND", os.environ["QSG_RHI_BACKEND"])
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeployFlow.DeploymentTool")
        except (AttributeError, OSError):
            pass
    application = QApplication.instance() or QApplication(sys.argv)
    application.setStyle("Fusion")
    application.styleHints().setColorScheme(Qt.ColorScheme.Light)
    palette = application.style().standardPalette()
    ApplicationWindow._set_palette_colors(palette, _THEME_PRESETS["white"])
    application.setPalette(palette)
    application.setApplicationName("DeployFlow")
    application.setOrganizationName("DeployFlow")
    icon = _application_root() / "assets" / "app_icon.ico"
    if icon.is_file():
        application.setWindowIcon(QIcon(str(icon)))
    try:
        start_terminal_cache_cleanup(_application_root() / "conf")
        window = ApplicationWindow(
            _initial_data_directory("tasks"),
            _initial_data_directory("host"),
            _initial_data_directory("scripts"),
            _initial_template(4, "server_parameters.template.txt"),
            _initial_template(5, "remote_script.template.sh"),
        )
    except Exception as exc:
        log_path: Path | None = None
        try:
            log_path = _default_data_root() / "startup-error.log"
            log_path.write_text(timestamp_log_text(traceback.format_exc())[0], encoding="utf-8")
        except OSError:
            log_path = None
        hint = tr("\n\n错误日志：{0}", log_path) if log_path is not None else ""
        QMessageBox.critical(None, tr("程序启动失败"), tr("初始化程序失败：\n{0}{1}", exc, hint))
        return
    window.show()
    application.exec()


if __name__ == "__main__":
    main()
