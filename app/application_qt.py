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
import re
import shutil
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFont, QIcon, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from builder import MavenBuilder
from config import ConfigurationError, DeploymentConfig, load_config, load_server_parameters
from deployer import FabricDeployer
from git_integration import GitIntegrator, GitOperationError, SourceBranchInfo
from password_protection import (
    PasswordProtectionError,
    is_legacy_protected,
    is_protected,
    protect_text,
    unprotect_text,
)
from qt_ssh_terminal_view import QtSSHTerminalTab
from workflow import WORKFLOW_TYPE_BY_KEY, WORKFLOW_TYPES, WorkflowTask, is_workflow_task, load_workflow_task
from workflow_executor import WorkflowExecutor


_PASSWORD_LINE_PATTERN = re.compile(
    r"^(?P<prefix>[ \t]*PASSWORD[ \t]*=[ \t]*)(?P<value>.*?)(?P<suffix>[ \t]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_HIDDEN_PASSWORD_VALUE = "********"
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
    "DEFAULT_OPEN_PATH": "仅用于手动打开 SSH 终端后自动进入的目录，例如 /opt/app；执行任务时不会使用它。",
    "PARAMETER_FILE": "选一份服务器配置文件，例如 production.txt。任务会用其中的地址、账号和密码连接服务器。",
    "PROJECT_PATH": "填本机项目所在目录，例如 D:\\code\\my-service；构建或 Git 操作会在这里执行。",
    "SOURCE_CODE_PATH": "填要上传的本地文件或目录路径。",
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


class _ChineseDialogButtonFilter(QObject):
    """Use Chinese labels for Qt's standard dialog buttons."""

    _TEXT_BY_BUTTON = {
        QDialogButtonBox.Ok: "确定",
        QDialogButtonBox.Save: "保存",
        QDialogButtonBox.Cancel: "取消",
        QDialogButtonBox.Close: "关闭",
        QDialogButtonBox.Discard: "不保存",
        QDialogButtonBox.Apply: "应用",
        QDialogButtonBox.Reset: "重置",
        QDialogButtonBox.RestoreDefaults: "恢复默认",
        QDialogButtonBox.Yes: "是",
        QDialogButtonBox.No: "否",
        QDialogButtonBox.Abort: "中止",
        QDialogButtonBox.Retry: "重试",
        QDialogButtonBox.Ignore: "忽略",
        QDialogButtonBox.Help: "帮助",
        QDialogButtonBox.Open: "打开",
    }

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Show and isinstance(watched, QDialog):
            for button_box in watched.findChildren(QDialogButtonBox):
                for standard_button, text in self._TEXT_BY_BUTTON.items():
                    button = button_box.button(standard_button)
                    if button is not None:
                        button.setText(text)
        return False


class _WorkerSignals(QObject):
    log = Signal(str)
    status = Signal(str)
    progress = Signal(int, int)
    finished = Signal(str, object)


class _PasswordDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, prompt: str) -> None:
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
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.entry.returnPressed.connect(self.accept)
        self.resize(380, self.sizeHint().height())


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
            or self.parameter_dir != (configuration_root / "parameters").resolve()
            or self.script_dir != (configuration_root / "scripts").resolve()
        ):
            raise ValueError(
                "任务、配置和脚本目录必须位于同一个 conf 目录下，并分别命名为 "
                "tasks、parameters、scripts"
            )
        self.draft_root = configuration_root / ".drafts"
        self.settings_path = configuration_root / "settings.json"
        self.parameter_template_path = parameter_template_path.resolve()
        self.script_template_path = script_template_path.resolve()
        self.application_settings = self._read_settings()
        self.editor_font_size = self._bounded_int("editor_font_size", 10, 8, 24)
        self.auto_save_delay_seconds = self._bounded_float(
            "auto_save_delay_seconds", 1.0, 0.5, 30.0
        )
        startup_page = str(self.application_settings.get("startup_page", "last"))
        saved_view = str(self.application_settings.get("view_mode", "task"))
        self.view_mode = startup_page if startup_page in {"task", "parameter", "script"} else saved_view
        if self.view_mode not in {"task", "parameter", "script"}:
            self.view_mode = "task"
        saved_files = self.application_settings.get("selected_files", {})
        self.last_selected_files = (
            {key: str(value) for key, value in saved_files.items() if key in {"task", "parameter", "script"} and value}
            if isinstance(saved_files, dict) else {}
        )
        self.current_path: Path | None = None
        self._dirty = False
        self._loading_editor = False
        self._changing_selection = False
        self._deploying = False
        self._stop_requested = False
        self._execution_cancel_event = threading.Event()
        self._interaction_panel_user_hidden = False
        self._password_session_unlocked = False
        self._parameter_password_visible = False
        self._parameter_password_ciphertext: str | None = None
        self._visible_parameter_password: str | None = None
        self.ssh_tabs: dict[Path, list[QtSSHTerminalTab]] = {}
        self._ssh_tab_sequence: dict[Path, int] = {}
        self._ssh_tab_names: dict[QtSSHTerminalTab, str] = {}
        self.worker_signals = _WorkerSignals(self)
        self.worker_signals.log.connect(self._append_log)
        self.worker_signals.status.connect(self._set_worker_status)
        self.worker_signals.progress.connect(self._update_progress)
        self.worker_signals.finished.connect(self._worker_finished)
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.setSingleShot(True)
        self.auto_save_timer.timeout.connect(self._write_current_draft)
        self.setWindowTitle("DeployFlow 自动部署工具")
        self.setMinimumSize(800, 520)
        self.resize(1100, 720)
        self._create_widgets()
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
            ("新建", self.create_file),
            ("重命名", self.rename_file),
            ("复制", self.copy_file),
            ("删除", self.delete_file),
            ("保存", self.save_text),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
            self.file_buttons.append(button)
        toolbar.addStretch(1)
        self.password_button = QPushButton("开启隐藏密码")
        self.password_button.clicked.connect(self._toggle_password_visibility)
        toolbar.addWidget(self.password_button)
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._toggle_ssh_connection)
        toolbar.addWidget(self.connect_button)
        self.interaction_button = QPushButton("显示交互窗口")
        self.interaction_button.clicked.connect(self._toggle_interaction_panel)
        toolbar.addWidget(self.interaction_button)
        self.execute_button = QPushButton("执行")
        self.execute_button.clicked.connect(self.develop_method)
        toolbar.addWidget(self.execute_button)
        root_layout.addLayout(toolbar)

        self.main_splitter = QSplitter(Qt.Horizontal)
        sidebar = QFrame()
        sidebar.setObjectName("fileSidebar")
        sidebar.setMinimumWidth(190)
        side_layout = QHBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        nav_layout = QVBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        self.nav_buttons: dict[str, QPushButton] = {}
        for mode, text in (("task", "任务"), ("parameter", "配置"), ("script", "脚本")):
            button = QPushButton(text)
            button.setObjectName("viewModeButton")
            button.setCheckable(True)
            button.setFixedWidth(68)
            button.clicked.connect(lambda _checked=False, value=mode: self.switch_view(value))
            nav_layout.addWidget(button)
            self.nav_buttons[mode] = button
        nav_layout.addStretch(1)
        settings_button = QPushButton("⚙ 设置")
        settings_button.setObjectName("settingsButton")
        settings_button.setFixedWidth(68)
        settings_button.clicked.connect(self._show_settings)
        nav_layout.addWidget(settings_button)
        side_layout.addLayout(nav_layout)
        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.file_list.setDefaultDropAction(Qt.MoveAction)
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDropIndicatorShown(True)
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        self.file_list.model().rowsMoved.connect(self._save_file_order)
        side_layout.addWidget(self.file_list, 1)
        sidebar.setStyleSheet(
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
        self.main_splitter.addWidget(sidebar)

        self.right_splitter = QSplitter(Qt.Vertical)
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_header = QHBoxLayout()
        self.editor_title = QLabel("任务编辑")
        self.editor_title.setStyleSheet("font-weight:600")
        editor_header.addWidget(self.editor_title)
        editor_header.addStretch(1)
        self.add_step_button = QPushButton("增加步骤")
        self.add_step_button.clicked.connect(self._add_step)
        editor_header.addWidget(self.add_step_button)
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
        editor_layout.addWidget(self.editor, 1)
        self.right_splitter.addWidget(editor_container)

        self.interaction_tabs = QTabWidget()
        self.interaction_tabs.setTabsClosable(True)
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
        self.interaction_tabs.addTab(output_container, "执行输出")
        self.interaction_tabs.tabBar().setTabButton(0, QTabBar.RightSide, None)
        self.right_splitter.addWidget(self.interaction_tabs)
        self.right_splitter.setSizes([520, 200])
        self.interaction_tabs.setVisible(False)
        self.main_splitter.addWidget(self.right_splitter)
        self.main_splitter.setSizes([250, 850])
        root_layout.addWidget(self.main_splitter, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("就绪")
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
        return {"task": self.task_dir, "parameter": self.parameter_dir, "script": self.script_dir}[self.view_mode]

    def _view_label(self) -> str:
        return {"task": "任务", "parameter": "配置", "script": "脚本"}[self.view_mode]

    def switch_view(self, view_mode: str, initial: bool = False) -> None:
        if view_mode not in {"task", "parameter", "script"}:
            return
        if self._deploying and not initial:
            QMessageBox.warning(self, "正在部署", "部署完成后才能切换列表")
            return
        if not initial and view_mode == self.view_mode:
            return
        if not initial and (not self._confirm_pending_changes() or not self._conceal_current_parameter_password()):
            return
        self.view_mode = view_mode
        self.current_path = None
        self._set_editor_content("")
        for mode, button in self.nav_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(mode == view_mode)
            del blocker
        self._restore_current_view_file()
        self._update_controls()
        if not initial:
            self.status_label.setText(f"已切换到{self._view_label()}列表")

    def update_file_list(self, select_path: Path | None = None) -> None:
        self._changing_selection = True
        self.file_list.clear()
        extensions = {".txt"} if self.view_mode in {"task", "parameter"} else {".sh", ".bash", ".bat", ".cmd", ".ps1"}
        files = [p for p in self.dir_path.iterdir() if p.is_file() and p.suffix.lower() in extensions]
        order_by_name = {
            name: index
            for index, name in enumerate(self._file_orders().get(self.view_mode, []))
        }
        files.sort(key=lambda path: (order_by_name.get(path.name, len(order_by_name)), path.name.lower()))
        for path in files:
            item = QListWidgetItem(path.stem)
            item.setData(Qt.UserRole, str(path.resolve()))
            self.file_list.addItem(item)
            if select_path is not None and path.resolve() == select_path.resolve():
                self.file_list.setCurrentItem(item)
        self._changing_selection = False

    def _file_orders(self) -> dict[str, list[str]]:
        raw_orders = self.application_settings.get("file_orders", {})
        if not isinstance(raw_orders, dict):
            return {}
        return {
            mode: [Path(str(name)).name for name in names if name]
            for mode, names in raw_orders.items()
            if mode in {"task", "parameter", "script"} and isinstance(names, list)
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
            self.status_label.setText(f"已保存{self._view_label()}排序")

    def _restore_current_view_file(self) -> None:
        name = self.last_selected_files.get(self.view_mode)
        candidate = (self.dir_path / Path(name).name).resolve() if name else None
        selected = candidate if candidate is not None and candidate.is_file() else None
        self.update_file_list(selected)
        if selected is not None:
            self._load_file(selected)
        else:
            self._restore_untitled_draft()

    def _on_file_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if self._changing_selection or current is None:
            return
        path = Path(str(current.data(Qt.UserRole))).resolve()
        if path == self.current_path:
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            self.update_file_list(self.current_path)
            return
        self._load_file(path)

    def _load_file(self, path: Path) -> bool:
        try:
            stored = path.read_text(encoding="utf-8-sig")
            displayed, normalized = self._prepare_parameter_content_for_display(stored)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return False
        draft = self._draft_path(path)
        dirty = False
        if draft.is_file():
            try:
                draft_stored = draft.read_text(encoding="utf-8-sig")
                displayed, normalized = self._prepare_parameter_content_for_display(draft_stored)
                dirty = True
            except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
                QMessageBox.critical(self, "读取暂存失败", str(exc))
                return False
        self.current_path = path.resolve()
        self.last_selected_files[self.view_mode] = path.name
        self._set_editor_content(displayed)
        self._dirty = dirty
        self.editor.document().setModified(dirty)
        self.status_label.setText(f"已加载 {path.name}" + ("（存在暂存内容）" if dirty else ""))
        self._update_controls()
        return True

    def _set_editor_content(self, content: str, preserve_view: bool = False) -> None:
        cursor = self.editor.textCursor()
        position = cursor.position()
        scroll = self.editor.verticalScrollBar().value()
        self._loading_editor = True
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        if preserve_view:
            cursor.setPosition(min(position, len(content)))
            self.editor.setTextCursor(cursor)
            self.editor.verticalScrollBar().setValue(scroll)
        self._loading_editor = False

    def _editor_changed(self) -> None:
        if self._loading_editor:
            return
        self._dirty = True
        self.status_label.setText("内容已修改，等待自动暂存")
        self.auto_save_timer.start(int(self.auto_save_delay_seconds * 1000))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
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
                action = menu.addAction(f"从第 {step_index} 步开始执行")
                action.triggered.connect(
                    lambda _checked=False, value=step_index: self.develop_method(value)
                )
        menu.exec(self.editor.mapToGlobal(position))

    def _workflow_step_at_cursor(self, cursor: QTextCursor) -> int | None:
        blocks = self.editor.document().blockCount()
        current_line = cursor.blockNumber()
        current_text = cursor.block().text().strip()
        headers: list[tuple[int, int]] = []
        for line_number in range(blocks):
            line = self.editor.document().findBlockByNumber(line_number).text()
            match = re.match(r"\s*STEP_(\d+)_TYPE\s*=", line, re.IGNORECASE)
            if match is not None:
                headers.append((line_number, int(match.group(1))))
        if not headers:
            return None
        if not current_text:
            for line_number, step_index in headers:
                if line_number > current_line:
                    return step_index
        selected_step: int | None = None
        for line_number, step_index in headers:
            if line_number > current_line:
                break
            selected_step = step_index
        return selected_step

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
        workflow_match = re.fullmatch(r"STEP_\d+_([A-Z][A-Z0-9_]*)", key)
        if workflow_match:
            field = workflow_match.group(1)
            if field == "TYPE":
                return _WORKFLOW_TYPE_TOOLTIPS.get(
                    value.upper(),
                    "选择这一步要做什么，例如连接服务器、构建、上传或执行命令；选定后再填写下面对应的参数。",
                )
            return _WORKFLOW_PROPERTY_TOOLTIPS.get(
                field,
                "这是当前步骤的自定义参数。请结合此步骤选择的类型和任务注释填写。",
            )
        base_key = re.sub(r"_\d+$", "", key)
        return (
            _PROPERTY_TOOLTIPS.get(key)
            or _PROPERTY_TOOLTIPS.get(base_key)
            or f"自定义属性：{key}。请参考所在任务或脚本中的业务注释。"
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
            content = self._content_for_storage(self.editor.toPlainText())
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        except (OSError, PasswordProtectionError) as exc:
            self.status_label.setText(f"自动暂存失败：{exc}")
            return False
        self.status_label.setText("内容已自动暂存，点击保存后生效")
        return True

    def _restore_untitled_draft(self) -> None:
        draft = self._draft_path(None)
        if not draft.is_file():
            self._set_editor_content("")
            self._dirty = False
            return
        try:
            stored = draft.read_text(encoding="utf-8-sig")
            displayed, _stored = self._prepare_parameter_content_for_display(stored)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, "读取暂存失败", str(exc))
            return
        self._set_editor_content(displayed)
        self._dirty = True
        self.editor.document().setModified(True)

    def _confirm_pending_changes(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "存在未保存内容",
            "是否先保存当前文件？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_text()
        self._delete_draft(self.current_path)
        self._dirty = False
        return True

    def create_file(self) -> None:
        extension = self._choose_script_extension("选择脚本类型") if self.view_mode == "script" else None
        if self.view_mode == "script" and extension is None:
            return
        name, accepted = QInputDialog.getText(self, f"新建{self._view_label()}", f"请输入{self._view_label()}名称：")
        if not accepted:
            return
        file_name = self._validate_file_name(name, extension)
        if file_name is None:
            return
        path = self.dir_path / file_name
        if path.exists():
            QMessageBox.critical(self, "无法新建", f"文件已存在：{file_name}")
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            return
        if self.view_mode == "task":
            content = ""
        elif self.view_mode == "script" and path.suffix.lower() in {".bat", ".cmd"}:
            content = "@echo off\nsetlocal\n\n"
        elif self.view_mode == "script" and path.suffix.lower() == ".ps1":
            content = 'Set-StrictMode -Version Latest\n$ErrorActionPreference = "Stop"\n\n'
        else:
            template = self.parameter_template_path if self.view_mode == "parameter" else self.script_template_path
            try:
                content = template.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, "无法读取模板", f"模板：{template}\n\n{exc}")
                return
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "无法新建", str(exc))
            return
        self.update_file_list(path)
        self._load_file(path)

    def rename_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        name, accepted = QInputDialog.getText(self, f"重命名{self._view_label()}", f"请输入新的{self._view_label()}名称：", text=path.stem)
        if not accepted:
            return
        file_name = self._validate_file_name(name, path.suffix.lower() if self.view_mode == "script" else None)
        if file_name is None:
            return
        target = path.with_name(file_name)
        if target.exists() and target != path:
            QMessageBox.critical(self, "无法重命名", f"文件已存在：{file_name}")
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
            QMessageBox.critical(self, "无法重命名", str(exc))
            return
        self._delete_draft(path)
        if self.view_mode == "parameter":
            self._close_parameter_ssh_tabs(path)
        self.update_file_list(target)
        self._load_file(target)
        self.status_label.setText(f"已重命名为 {target.name}" + (f"，并更新 {len(refs)} 个任务引用" if refs else ""))

    def copy_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        name, accepted = QInputDialog.getText(self, f"复制{self._view_label()}", "请输入副本名称：", text=f"{path.stem}_copy")
        if not accepted:
            return
        file_name = self._validate_file_name(name, path.suffix.lower() if self.view_mode == "script" else None)
        if file_name is None:
            return
        target = path.with_name(file_name)
        if target.exists():
            QMessageBox.critical(self, "无法复制", f"文件已存在：{file_name}")
            return
        if not self._confirm_pending_changes():
            return
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            QMessageBox.critical(self, "无法复制", str(exc))
            return
        self.update_file_list(target)
        self._load_file(target)

    def delete_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        if self.view_mode in {"parameter", "script"}:
            try:
                refs, _updates = self._collect_task_reference_updates(self.view_mode, path)
            except (OSError, UnicodeDecodeError) as exc:
                QMessageBox.critical(self, "无法检查任务引用", str(exc))
                return
            if refs:
                names = "\n".join(f"• {self._task_reference_display_name(value)}" for value in refs[:10])
                QMessageBox.critical(self, "无法删除", f"以下任务仍引用 {path.name}：\n\n{names}")
                return
        if QMessageBox.question(self, "确认删除", f"确定删除 {path.name} 吗？") != QMessageBox.Yes:
            return
        try:
            path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, "无法删除", str(exc))
            return
        self._delete_draft(path)
        if self.view_mode == "parameter":
            self._close_parameter_ssh_tabs(path)
        self.current_path = None
        self.last_selected_files.pop(self.view_mode, None)
        self._set_editor_content("")
        self.update_file_list()
        self._update_controls()
        self.status_label.setText(f"{self._view_label()}已删除")

    def save_text(self, show_message: bool = True) -> bool:
        path = self.current_path
        previous_draft = self._draft_path(path)
        if path is None:
            extension = self._choose_script_extension("选择脚本类型") if self.view_mode == "script" else None
            if self.view_mode == "script" and extension is None:
                return False
            name, accepted = QInputDialog.getText(self, f"保存新{self._view_label()}", f"请输入新{self._view_label()}名称：")
            if not accepted:
                return False
            file_name = self._validate_file_name(name, extension)
            if file_name is None:
                return False
            path = (self.dir_path / file_name).resolve()
            if path.exists():
                QMessageBox.critical(self, "无法保存", f"文件已存在：{file_name}")
                return False
        was_dirty = self._dirty
        try:
            path.write_text(self._content_for_storage(self.editor.toPlainText()), encoding="utf-8")
        except (OSError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
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
        self.status_label.setText(f"已保存 {path.name}")
        self._update_controls()
        if show_message:
            QMessageBox.information(self, "保存成功", f"已保存文件：{path.name}")
        return True

    def _choose_script_extension(self, title: str) -> str | None:
        label, accepted = QInputDialog.getItem(self, title, "请选择脚本类型：", list(_SCRIPT_EXTENSIONS), 0, False)
        return _SCRIPT_EXTENSIONS[label] if accepted else None

    def _validate_file_name(self, value: str | None, extension: str | None = None) -> str | None:
        name = (value or "").strip()
        if not name:
            QMessageBox.critical(self, "名称无效", "文件名称不能为空")
            return None
        if any(char in name for char in '<>:"/\\|?*') or name.rstrip(". ") != name:
            QMessageBox.critical(self, "名称无效", "文件名称包含 Windows 不允许的字符")
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
            QMessageBox.warning(self, "未选择文件", "请先选择一个文件")
        return self.current_path

    def _delete_draft_path(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _delete_draft(self, path: Path | None) -> None:
        self._delete_draft_path(self._draft_path(path))

    def _task_reference_pattern(self, reference_type: str) -> re.Pattern[str]:
        key = r"(?:PARAMETER_FILE|STEP_\d+_PARAMETER_FILE)" if reference_type == "parameter" else r"(?:SCRIPT_FILE|RESTART_LOCAL_SCRIPT_\d+|STEP_\d+_SCRIPT_FILE)"
        return re.compile(rf"^(?P<prefix>[ \t]*{key}[ \t]*=[ \t]*)(?P<value>[^\r\n]*?)(?P<suffix>[ \t]*)$", re.I | re.M)

    def _collect_task_reference_updates(
        self, reference_type: str, referenced_path: Path, replacement_path: Path | None = None
    ) -> tuple[list[Path], list[tuple[Path, str, str]]]:
        referenced_path = referenced_path.resolve()
        pattern = self._task_reference_pattern(reference_type)
        sources = list(self.task_dir.glob("*.txt"))
        draft_dir = self.draft_root / "task"
        if draft_dir.is_dir():
            sources.extend(draft_dir.glob("*.draft"))
        refs: list[Path] = []
        updates: list[tuple[Path, str, str]] = []
        for task_path in sorted(sources, key=lambda p: str(p).lower()):
            content = task_path.read_text(encoding="utf-8-sig")
            matched = False
            def replace(match: re.Match[str]) -> str:
                nonlocal matched
                value = match.group("value").strip()
                if not value:
                    return match.group(0)
                reference = Path(os.path.expandvars(value)).expanduser()
                base = self.parameter_dir if reference_type == "parameter" else self.script_dir
                resolved = reference.resolve() if reference.is_absolute() else (base / reference).resolve()
                if resolved != referenced_path:
                    return match.group(0)
                matched = True
                if replacement_path is None:
                    return match.group(0)
                new_value = str(replacement_path) if reference.is_absolute() else replacement_path.name
                return f"{match.group('prefix')}{new_value}{match.group('suffix')}"
            updated = pattern.sub(replace, content)
            if matched:
                refs.append(task_path)
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
            QMessageBox.critical(self, "无法重命名", f"重命名或更新任务引用失败：{exc}")
            return False
        return True

    def _task_reference_display_name(self, path: Path) -> str:
        if path.parent.resolve() != (self.draft_root / "task").resolve():
            return path.name
        return "未命名任务（暂存）" if path.name == "__untitled__.draft" else f"{path.name.removesuffix('.draft')}（暂存）"

    def _add_step(self) -> None:
        if self.view_mode != "task":
            return
        content = self.editor.toPlainText()
        legacy = bool(re.search(r"(?mi)^\s*(?:PARAMETER_FILE|PROJECT_PATH)\s*=", content))
        if legacy:
            self._add_legacy_restart_step()
        else:
            self._add_workflow_step()

    def _add_workflow_step(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("增加流程步骤")
        root = QVBoxLayout(dialog)
        type_combo = QComboBox()
        for definition in WORKFLOW_TYPES:
            type_combo.addItem(definition.label, definition.key)
        form = QFormLayout()
        root.addWidget(QLabel("步骤类型："))
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
            text = self.editor.toPlainText()
            connections = re.findall(r"(?mi)^\s*STEP_\d+_CONNECTION_NAME\s*=\s*(.+?)\s*$", text)
            artifacts = re.findall(r"(?mi)^\s*STEP_\d+_ARTIFACT_NAME\s*=\s*(.+?)\s*$", text)
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
                    if field.default:
                        control.setCurrentText(field.default)
                else:
                    control = QLineEdit(field.default)
                controls[field.key] = control
                form.addRow(field.label + (" *" if field.required else ""), control)
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
        dialog.resize(680, min(720, dialog.sizeHint().height()))
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
                missing.append(field.label)
        if missing:
            QMessageBox.critical(self, "参数不足", "请填写：" + "、".join(missing))
            return
        indexes = [int(value) for value in re.findall(r"(?mi)^\s*STEP_(\d+)_TYPE\s*=", self.editor.toPlainText())]
        index = max(indexes, default=0) + 1
        block = [f"STEP_{index}_TYPE={definition.key}"]
        block.extend(f"STEP_{index}_{field.key}={values[field.key]}" for field in definition.fields if values[field.key] or field.required)
        updated = self.editor.toPlainText().rstrip()
        self._set_editor_content((updated + "\n\n" if updated else "") + "\n".join(block) + "\n", preserve_view=True)
        self._mark_changed(f"已暂存第 {index} 个流程步骤：{definition.label}")

    def _add_legacy_restart_step(self) -> None:
        scripts = [p.name for p in sorted(self.script_dir.iterdir()) if p.is_file()]
        choices = ["远程命令"] + [f"脚本：{name}" for name in scripts]
        selected, accepted = QInputDialog.getItem(self, "增加执行步骤", "步骤类型：", choices, 0, False)
        if not accepted:
            return
        if selected == "远程命令":
            command, accepted = QInputDialog.getText(self, "执行命令", "请输入远程命令：")
            if not accepted or not command.strip():
                return
            kind, value = "COMMAND", command.strip()
        else:
            kind, value = "LOCAL_SCRIPT", selected.removeprefix("脚本：")
        path, accepted = QInputDialog.getText(self, "执行目录", "指定服务器目录（可留空）：")
        if not accepted:
            return
        indexes = [int(v) for v in re.findall(r"(?mi)^\s*RESTART_(?:COMMAND|LOCAL_SCRIPT)_(\d+)\s*=", self.editor.toPlainText())]
        index = max(indexes, default=0) + 1
        block = f"RESTART_{kind}_{index}={value}\nRESTART_PATH_{index}={path.strip()}\nRESTART_DELAY_{index}=0"
        updated = self.editor.toPlainText().rstrip()
        self._set_editor_content(f"{updated}\n\n{block}\n", preserve_view=True)
        self._mark_changed(f"已暂存第 {index} 个执行步骤")

    def _mark_changed(self, status: str) -> None:
        self._dirty = True
        self.editor.document().setModified(True)
        self._write_current_draft()
        self.status_label.setText(status + "，点击保存后生效")

    def develop_method(self, start_step: int | None = None) -> None:
        if self._deploying:
            self._request_stop_execution()
            return
        if self.view_mode != "task":
            QMessageBox.warning(self, "无法执行", "请先切换到任务列表并选择任务")
            return
        path = self._require_current_path()
        if path is None:
            return
        if self._dirty or self._draft_path(path, "task").is_file():
            QMessageBox.warning(self, "任务尚未保存", "当前任务只有暂存内容，请先点击保存后再执行")
            return
        try:
            content = path.read_text(encoding="utf-8-sig")
            legacy = bool(re.search(r"(?mi)^\s*(?:PARAMETER_FILE|PROJECT_PATH)\s*=", content))
            workflow_task = load_workflow_task(path) if is_workflow_task(path) or not legacy else None
        except (ConfigurationError, OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(self, "任务配置错误", str(exc))
            return
        if workflow_task is not None:
            if start_step is not None:
                steps = tuple(step for step in workflow_task.steps if step.index >= start_step)
                if not steps or steps[0].index != start_step:
                    QMessageBox.warning(self, "无法执行", f"任务中不存在第 {start_step} 步")
                    return
                workflow_task = WorkflowTask(
                    workflow_task.name,
                    workflow_task.file_path,
                    steps,
                )
                confirmation = (
                    f"任务：{workflow_task.name}\n从第 {start_step} 步开始，共执行 {len(steps)} 个步骤。\n\n"
                    "此前步骤将被跳过；如果当前步骤依赖此前建立的连接或产物，执行会直接报错。\n\n"
                    "确定执行吗？"
                )
            else:
                confirmation = f"任务：{workflow_task.name}\n共 {len(workflow_task.steps)} 个步骤\n\n确定执行吗？"
            if QMessageBox.question(self, "确认执行", confirmation) != QMessageBox.Yes:
                return
            self._start_workflow(workflow_task, start_step)
            return
        try:
            config = load_config(path)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "任务配置错误", str(exc))
            return
        source_info: SourceBranchInfo | None = None
        merge = False
        if config.git_integration_enabled:
            try:
                source_info = GitIntegrator().inspect_source(config)
            except GitOperationError as exc:
                QMessageBox.critical(self, "Git 配置错误", str(exc))
                return
            merge = QMessageBox.question(
                self, "是否合并并推送",
                f"IDEA 当前分支：{source_info.branch}\n当前提交：{source_info.commit[:12]}\n"
                f"目标分支：{config.target_branch}\n打包目录：{config.project_path}\n\n"
                "选择“是”执行合并和推送；选择“否”直接部署现有代码。",
            ) == QMessageBox.Yes
        elif QMessageBox.question(self, "确认部署", f"任务：{config.name}\n目标：{config.target}\n\n确定继续吗？") != QMessageBox.Yes:
            return
        self._start_legacy_deployment(config, source_info, merge)

    def _prepare_execution(self, title: str) -> None:
        self._show_interaction_panel(force=True)
        self.interaction_tabs.setCurrentIndex(0)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self._append_log(title)
        self._deploying = True
        self._stop_requested = False
        self._execution_cancel_event.clear()
        self._update_controls()

    def _start_workflow(self, task: WorkflowTask, start_step: int | None = None) -> None:
        start_message = f"从第 {start_step} 步开始执行\n" if start_step is not None else ""
        self._prepare_execution(f"执行任务：{task.name}\n{start_message}共 {len(task.steps)} 个步骤，将按配置顺序执行")
        self.status_label.setText("正在执行……")
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

    def _start_legacy_deployment(self, config: DeploymentConfig, source: SourceBranchInfo | None, merge: bool) -> None:
        self._prepare_execution(f"部署任务：{config.name}\n目标服务器：{config.target}")
        self.status_label.setText("正在部署……")
        threading.Thread(target=self._deployment_worker, args=(config, source, merge), name="deployment-worker", daemon=True).start()

    def _deployment_worker(self, config: DeploymentConfig, source: SourceBranchInfo | None, merge: bool) -> None:
        try:
            if merge:
                if source is None:
                    raise GitOperationError("无法获取 IDEA 当前分支")
                self.worker_signals.status.emit("正在合并并推送代码……")
                GitIntegrator(cancel_event=self._execution_cancel_event).merge_and_push(config, source, self.worker_signals.log.emit)
            else:
                self.worker_signals.log.emit("跳过 Git 合并和推送")
            self.worker_signals.status.emit("正在打包……")
            artifact = MavenBuilder().build(config, self.worker_signals.log.emit, cancel_event=self._execution_cancel_event)
            self.worker_signals.status.emit("正在连接服务器……")
            verified = FabricDeployer(cancel_event=self._execution_cancel_event).deploy(
                config, artifact, self.worker_signals.progress.emit, self.worker_signals.status.emit, self.worker_signals.log.emit
            )
        except Exception as exc:
            kind = "deployment_cancelled" if self._execution_cancel_event.is_set() else "deployment_error"
            self.worker_signals.finished.emit(kind, str(exc))
        else:
            self.worker_signals.finished.emit("deployment_success", (config.target, verified))

    def _request_stop_execution(self) -> None:
        if not self._deploying or self._stop_requested:
            return
        if QMessageBox.question(self, "确认停止任务", "确定要停止当前任务吗？") != QMessageBox.Yes:
            return
        self._stop_requested = True
        self._execution_cancel_event.set()
        self.status_label.setText("正在停止任务……")
        self._append_log("用户请求停止任务，正在结束当前操作……")
        self._update_controls()

    def _set_worker_status(self, value: str) -> None:
        if not self._stop_requested:
            self.status_label.setText(value)

    def _update_progress(self, transferred: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(transferred * 100 / total))
        self.progress_bar.setValue(percent)
        if not self._stop_requested:
            self.status_label.setText("正在上传……")

    def _worker_finished(self, kind: str, payload: object) -> None:
        self._deploying = False
        self._stop_requested = False
        self._execution_cancel_event.clear()
        self._update_controls()
        self.status_label.setText("就绪")
        if kind == "workflow_success":
            self.progress_bar.setValue(100)
            self._append_log("任务执行完成")
            QMessageBox.information(self, "执行完成", f"任务“{payload}”已执行完成")
        elif kind == "workflow_error":
            self._append_log(f"执行失败：{payload}")
            QMessageBox.critical(self, "执行失败", str(payload))
        elif kind == "workflow_cancelled":
            self._append_log("任务已停止")
            QMessageBox.information(self, "任务已停止", "当前任务已停止")
        elif kind == "deployment_success":
            target, verified = payload
            self._append_log("部署完成")
            if verified:
                QMessageBox.information(self, "启动成功", f"项目已成功启动\n目标服务器：{target}")
            else:
                QMessageBox.warning(self, "部署完成", "JAR 已上传且重启命令执行成功，但未配置健康检查。")
        elif kind == "deployment_cancelled":
            self._append_log("部署已停止")
            QMessageBox.information(self, "部署已停止", "当前部署已停止")
        else:
            self._append_log(f"部署失败：{payload}")
            QMessageBox.critical(self, "部署失败", str(payload))
        self._sync_interaction_panel_visibility()

    def _append_log(self, value: str) -> None:
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(value.rstrip() + "\n")
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _toggle_ssh_connection(self) -> None:
        if self.view_mode != "parameter" or self.current_path is None:
            QMessageBox.warning(self, "无法连接", "请在“配置”页面选择服务器配置文件")
            return
        if self._dirty:
            QMessageBox.warning(self, "配置尚未保存", "当前配置只有暂存内容，请先保存")
            return
        try:
            parameters = load_server_parameters(self.current_path)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "服务器配置错误", str(exc))
            return
        default_path: str | None = None
        if len(parameters.default_open_paths) == 1:
            default_path = parameters.default_open_paths[0]
        elif len(parameters.default_open_paths) > 1:
            value, accepted = QInputDialog.getItem(self, "选择默认目录", "请选择本次 SSH 连接进入的目录：", list(parameters.default_open_paths), 0, False)
            if not accepted:
                return
            default_path = value
        parameter_path = self.current_path.resolve()
        tab = QtSSHTerminalTab(
            self.interaction_tabs, parameters, parameter_path, default_path,
            self._on_ssh_state_changed, self._close_ssh_tab,
        )
        tabs = self.ssh_tabs.setdefault(parameter_path, [])
        tabs.append(tab)
        sequence = self._ssh_tab_sequence.get(parameter_path, 0) + 1
        self._ssh_tab_sequence[parameter_path] = sequence
        name = parameters.name if sequence == 1 else f"{parameters.name} ({sequence})"
        self._ssh_tab_names[tab] = name
        index = self.interaction_tabs.addTab(tab, name)
        self._show_interaction_panel(force=True)
        self.interaction_tabs.setCurrentIndex(index)
        tab.start_connection()
        self.status_label.setText(f"正在连接 {parameters.target}……")

    def _on_ssh_state_changed(self, tab: QtSSHTerminalTab) -> None:
        index = self.interaction_tabs.indexOf(tab)
        if index >= 0:
            prefix = {"connecting": "… ", "connected": "● ", "cancelled": "○ ", "error": "× ", "disconnected": "○ "}[tab.state]
            self.interaction_tabs.setTabText(index, prefix + self._ssh_tab_names.get(tab, tab.parameters.name))
        if self.interaction_tabs.currentWidget() is tab:
            if tab.state == "connected":
                self.status_label.setText(f"SSH 已连接：{tab.parameters.target}")
            elif tab.state == "error":
                self.status_label.setText(f"SSH 连接失败：{tab.parameters.target}")
        self._sync_interaction_panel_visibility()

    def _tab_close_requested(self, index: int) -> None:
        widget = self.interaction_tabs.widget(index)
        if isinstance(widget, QtSSHTerminalTab):
            self._close_ssh_tab(widget)

    def _close_ssh_tab(self, tab: QtSSHTerminalTab) -> None:
        tabs = self.ssh_tabs.get(tab.parameter_path, [])
        if tab in tabs:
            tabs.remove(tab)
        if not tabs:
            self.ssh_tabs.pop(tab.parameter_path, None)
        self._ssh_tab_names.pop(tab, None)
        index = self.interaction_tabs.indexOf(tab)
        if index >= 0:
            self.interaction_tabs.removeTab(index)
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

    def _show_interaction_panel(self, force: bool = False) -> None:
        if self._interaction_panel_user_hidden and not force:
            return
        self.interaction_tabs.setVisible(True)
        if force:
            self._interaction_panel_user_hidden = False
        sizes = self.right_splitter.sizes()
        if len(sizes) == 2 and sizes[1] < 100:
            self.right_splitter.setSizes([max(300, sizes[0] - 180), 180])
        self._update_controls()

    def _sync_interaction_panel_visibility(self) -> None:
        active = self._deploying or any(tab.state in {"connecting", "connected"} for tabs in self.ssh_tabs.values() for tab in tabs)
        if active:
            self._show_interaction_panel()
        elif not self._interaction_panel_user_hidden:
            self.interaction_tabs.setVisible(False)
        self._update_controls()

    def _update_controls(self) -> None:
        for button in self.file_buttons:
            button.setEnabled(not self._deploying)
        for button in self.nav_buttons.values():
            button.setEnabled(not self._deploying)
        self.password_button.setVisible(self.view_mode == "parameter")
        if self._password_hiding_enabled():
            self.password_button.setText("隐藏密码" if self._parameter_password_visible else "显示密码")
        else:
            self.password_button.setText("开启隐藏密码")
        self.password_button.setEnabled(not self._deploying and (not self._password_hiding_enabled() or self.current_path is not None))
        self.connect_button.setEnabled(not self._deploying and self.view_mode == "parameter" and self.current_path is not None)
        self.add_step_button.setVisible(self.view_mode == "task")
        self.editor_title.setText(f"{self._view_label()}编辑")
        self.execute_button.setText("停止" if self._deploying else "执行")
        self.execute_button.setEnabled((not self._deploying and self.view_mode == "task") or (self._deploying and not self._stop_requested))
        self.interaction_button.setText("隐藏交互窗口" if self.interaction_tabs.isVisible() else "显示交互窗口")

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

    def _show_settings(self) -> None:
        if self._deploying:
            QMessageBox.warning(self, "正在部署", "部署完成后才能打开系统设置")
            return
        if not self._require_administrator_password() or not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("系统设置")
        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        general = QWidget()
        general_form = QFormLayout(general)
        startup = QComboBox()
        startup_values = {"恢复上次页面": "last", "任务页面": "task", "配置页面": "parameter", "脚本页面": "script"}
        startup.addItems(startup_values)
        current_startup = str(self.application_settings.get("startup_page", "last"))
        startup.setCurrentText(next((label for label, value in startup_values.items() if value == current_startup), "恢复上次页面"))
        password_hiding = QCheckBox("开启服务器密码隐藏")
        password_hiding.setChecked(self._password_hiding_enabled())
        change_password = QPushButton("更改密码")
        change_password.clicked.connect(self._change_administrator_password)
        general_form.addRow("启动时打开：", startup)
        general_form.addRow("密码隐藏：", password_hiding)
        general_form.addRow("管理员密码：", change_password)
        tabs.addTab(general, "常规")
        editor_page = QWidget()
        editor_form = QFormLayout(editor_page)
        font_size = QSpinBox()
        font_size.setRange(8, 24)
        font_size.setValue(self.editor_font_size)
        auto_delay = QDoubleSpinBox()
        auto_delay.setRange(0.5, 30.0)
        auto_delay.setSingleStep(0.5)
        auto_delay.setValue(self.auto_save_delay_seconds)
        editor_form.addRow("编辑器字体大小：", font_size)
        editor_form.addRow("自动暂存等待秒数：", auto_delay)
        tabs.addTab(editor_page, "编辑器与保存")
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        if password_hiding.isChecked() != self._password_hiding_enabled():
            changed = self._enable_password_hiding(False) if password_hiding.isChecked() else self._disable_password_hiding(False)
            if not changed:
                return
        settings = dict(self.application_settings)
        settings.update({
            "startup_page": startup_values[startup.currentText()],
            "editor_font_size": font_size.value(),
            "auto_save_delay_seconds": auto_delay.value(),
        })
        if not self._write_settings(settings):
            QMessageBox.critical(self, "保存失败", "无法保存系统设置")
            return
        self.editor_font_size = font_size.value()
        self.auto_save_delay_seconds = auto_delay.value()
        self.editor.setFont(QFont("Cascadia Mono", self.editor_font_size))
        QMessageBox.information(self, "保存成功", "系统设置已保存")

    def _ask_password(self, title: str, prompt: str) -> str | None:
        dialog = _PasswordDialog(self, title, prompt)
        return dialog.entry.text() if dialog.exec() == QDialog.Accepted else None

    def _prompt_new_administrator_password(self) -> str | None:
        value = self._ask_password("设置管理员密码", "请输入新的管理员密码：")
        if value is None:
            return None
        value = value.strip()
        if not value:
            QMessageBox.critical(self, "密码无效", "管理员密码不能为空")
            return None
        confirmation = self._ask_password("确认管理员密码", "请再次输入新的管理员密码：")
        if confirmation is None or not hmac.compare_digest(value.encode(), confirmation.strip().encode()):
            QMessageBox.critical(self, "密码不一致", "两次输入的管理员密码不一致")
            return None
        return value

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
            QMessageBox.critical(self, "无法验证", str(exc))
            return False
        entered = self._ask_password("管理员验证", "请输入管理员密码：")
        if entered is None:
            return False
        if not hmac.compare_digest(entered.strip().encode(), expected.encode()):
            QMessageBox.critical(self, "密码错误", "管理员密码不正确")
            return False
        self._password_session_unlocked = True
        if is_legacy_protected(encrypted):
            settings = dict(self.application_settings)
            hiding = self._password_settings()
            hiding["unlock_password"] = protect_text(expected)
            settings["password_hiding"] = hiding
            if not self._write_settings(settings):
                QMessageBox.critical(self, "保存失败", "无法升级管理员密码加密格式")
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
            QMessageBox.critical(self, "初始化失败", str(exc))
            return False
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding.setdefault("enabled", False)
        hiding["unlock_password"] = encrypted
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, "初始化失败", "无法保存管理员密码")
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
            QMessageBox.critical(self, "更改失败", str(exc))
            return
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding.setdefault("enabled", False)
        hiding["unlock_password"] = encrypted
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, "更改失败", "无法保存管理员密码")
            return
        self._password_session_unlocked = True
        QMessageBox.information(self, "更改成功", "管理员密码已更改")

    def _parameter_file_updates(self, encrypt: bool) -> list[tuple[Path, str, str]]:
        paths = list(self.parameter_dir.glob("*.txt"))
        draft_dir = self.draft_root / "parameter"
        if draft_dir.is_dir():
            paths.extend(draft_dir.glob("*.draft"))
        updates: list[tuple[Path, str, str]] = []
        for path in sorted(paths, key=lambda p: str(p).lower()):
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
            QMessageBox.critical(self, "密码更新失败", str(exc))
            return False
        return True

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
                QMessageBox.critical(self, "开启失败", str(exc))
                return False
        try:
            updates = self._parameter_file_updates(True)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, "开启失败", str(exc))
            return False
        if not self._apply_password_file_updates(updates):
            return False
        settings = dict(self.application_settings)
        hiding.update({"enabled": True, "unlock_password": encrypted})
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, "开启失败", "无法保存隐藏密码设置")
            return False
        self._password_session_unlocked = True
        if self.current_path is not None:
            self._load_file(self.current_path)
        self._update_controls()
        if show_message:
            QMessageBox.information(self, "开启成功", "已加密服务器密码。重新打开软件后默认隐藏。")
        return True

    def _disable_password_hiding(self, show_message: bool = True) -> bool:
        if not self._password_hiding_enabled():
            return True
        if not self._unlock_password_hiding():
            return False
        try:
            updates = self._parameter_file_updates(False)
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            QMessageBox.critical(self, "关闭失败", str(exc))
            return False
        if not self._apply_password_file_updates(updates):
            return False
        settings = dict(self.application_settings)
        hiding = self._password_settings()
        hiding["enabled"] = False
        settings["password_hiding"] = hiding
        if not self._write_settings(settings):
            QMessageBox.critical(self, "关闭失败", "无法保存密码隐藏设置")
            return False
        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.current_path is not None:
            self._load_file(self.current_path)
        self._update_controls()
        if show_message:
            QMessageBox.information(self, "关闭成功", "服务器密码已恢复为明文保存")
        return True

    def _toggle_password_visibility(self) -> None:
        if not self._password_hiding_enabled():
            self._enable_password_hiding()
            return
        if self.current_path is None:
            QMessageBox.warning(self, "未选择配置", "请先选择一个服务器配置文件")
            return
        if self._parameter_password_visible:
            if self._conceal_current_parameter_password():
                self.status_label.setText("服务器密码已隐藏")
            return
        if not self._unlock_password_hiding():
            return
        content = self.editor.toPlainText()
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            QMessageBox.warning(self, "没有密码配置", "当前配置中没有 PASSWORD 参数")
            return
        encrypted = self._parameter_password_ciphertext or match.group("value").strip()
        try:
            plaintext = unprotect_text(encrypted) if encrypted and is_protected(encrypted) else encrypted
        except PasswordProtectionError as exc:
            QMessageBox.critical(self, "无法显示密码", str(exc))
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
            QMessageBox.critical(self, "无法隐藏密码", str(exc))
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
            raise PasswordProtectionError("服务器密码当前处于隐藏状态，请先点击“显示密码”后再修改")
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
        self.status_label.setText(f"编辑器字体大小：{size}")

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
        sizes = self.application_settings.get("qt_splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self.right_splitter.setSizes([int(sizes[0]), int(sizes[1])])
            except (TypeError, ValueError):
                pass
        if self.application_settings.get("window_state") == "zoomed":
            QTimer.singleShot(0, self.showMaximized)

    def _save_application_state(self) -> None:
        settings = dict(self.application_settings)
        settings.update({
            "editor_font_size": self.editor_font_size,
            "view_mode": self.view_mode,
            "selected_files": dict(self.last_selected_files),
            "window_state": "zoomed" if self.isMaximized() else "normal",
            "qt_window_geometry": bytes(self.saveGeometry()).hex(),
            "qt_window_state": bytes(self.saveState()).hex(),
            "qt_splitter_sizes": self.right_splitter.sizes(),
        })
        self._write_settings(settings)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._deploying:
            QMessageBox.warning(self, "正在部署", "为避免在上传、替换或重启过程中中断操作，请等待本次部署完成后再关闭。")
            event.ignore()
            return
        if not self._confirm_pending_changes() or not self._conceal_current_parameter_password():
            event.ignore()
            return
        for tabs in list(self.ssh_tabs.values()):
            for tab in list(tabs):
                tab.shutdown()
        self._save_application_state()
        event.accept()


def _application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def _initial_data_directory(argument_index: int, directory_name: str) -> Path:
    directory = Path(sys.argv[argument_index]).expanduser().resolve() if len(sys.argv) > argument_index else _application_root() / "conf" / directory_name
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
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeployFlow.DeploymentTool")
        except (AttributeError, OSError):
            pass
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("DeployFlow")
    application.setOrganizationName("DeployFlow")
    application.installEventFilter(_ChineseDialogButtonFilter(application))
    icon = _application_root() / "assets" / "app_icon.ico"
    if icon.is_file():
        application.setWindowIcon(QIcon(str(icon)))
    try:
        window = ApplicationWindow(
            _initial_data_directory(1, "tasks"),
            _initial_data_directory(2, "parameters"),
            _initial_data_directory(3, "scripts"),
            _initial_template(4, "server_parameters.template.txt"),
            _initial_template(5, "remote_script.template.sh"),
        )
    except Exception as exc:
        log_path: Path | None = _application_root() / "startup-error.log"
        try:
            log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            log_path = None
        hint = f"\n\n错误日志：{log_path}" if log_path is not None else ""
        QMessageBox.critical(None, "程序启动失败", f"初始化程序失败：\n{exc}{hint}")
        return
    window.show()
    application.exec()


if __name__ == "__main__":
    main()
