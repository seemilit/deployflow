from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
import ctypes
import hmac
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk


try:
    from builder import MavenBuilder
    from config import (
        ConfigurationError,
        DeploymentConfig,
        load_config,
        load_server_parameters,
    )
    from deployer import FabricDeployer
    from git_integration import GitIntegrator, GitOperationError, SourceBranchInfo
    from password_protection import (
        PasswordProtectionError,
        is_legacy_protected,
        is_protected,
        protect_text,
        unprotect_text,
    )
    from ssh_terminal_view import SSHTerminalTab
    from workflow import (
        WORKFLOW_TYPE_BY_KEY,
        WORKFLOW_TYPES,
        WorkflowTask,
        is_workflow_task,
        load_workflow_task,
    )
    from workflow_executor import WorkflowExecutor
except Exception as startup_import_error:
    startup_traceback = traceback.format_exc()
    startup_root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    startup_log_path = startup_root / "startup-error.log"
    try:
        startup_log_path.write_text(startup_traceback, encoding="utf-8")
    except OSError:
        startup_log_path = Path.cwd() / "startup-error.log"
        try:
            startup_log_path.write_text(startup_traceback, encoding="utf-8")
        except OSError:
            startup_log_path = None
    try:
        startup_error_window = tk.Tk()
        startup_error_window.withdraw()
        log_hint = (
            f"\n\n错误日志：{startup_log_path}"
            if startup_log_path is not None
            else "\n\n错误日志也无法写入，请检查程序目录权限。"
        )
        messagebox.showerror(
            "程序启动失败",
            f"加载程序模块失败：\n{startup_import_error}{log_hint}",
            parent=startup_error_window,
        )
        startup_error_window.destroy()
    except Exception:
        pass
    raise SystemExit(1) from startup_import_error


_PASSWORD_LINE_PATTERN = re.compile(
    r"^(?P<prefix>[ \t]*PASSWORD[ \t]*=[ \t]*)(?P<value>.*?)(?P<suffix>[ \t]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_HIDDEN_PASSWORD_VALUE = "********"


class Application(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        task_dir: Path,
        parameter_dir: Path,
        script_dir: Path,
        parameter_template_path: Path,
        script_template_path: Path,
    ):
        super().__init__(master, padding=8)
        self.master = master
        self.task_dir = task_dir.resolve()
        self.parameter_dir = parameter_dir.resolve()
        self.script_dir = script_dir.resolve()
        configuration_root = self.task_dir.parent
        expected_parameter_dir = (configuration_root / "parameters").resolve()
        expected_script_dir = (configuration_root / "scripts").resolve()
        if (
            self.task_dir.name.lower() != "tasks"
            or
            self.parameter_dir != expected_parameter_dir
            or self.script_dir != expected_script_dir
        ):
            raise ValueError(
                "任务、配置和脚本目录必须位于同一个 conf 目录下，并分别命名为 "
                "tasks、parameters、scripts"
            )
        self.draft_root = configuration_root / ".drafts"
        self.settings_path = self.task_dir.parent / "settings.json"
        self.application_settings = self._read_settings()
        self._password_session_unlocked = False
        self._parameter_password_visible = False
        self._parameter_password_ciphertext: str | None = None
        self._visible_parameter_password: str | None = None
        self.editor_font_size = self._load_editor_font_size()
        self.auto_save_delay_seconds = self._load_auto_save_delay()
        saved_view_mode = str(self.application_settings.get("view_mode", "task"))
        startup_page = str(
            self.application_settings.get("startup_page", "last")
        )
        if startup_page in {"task", "parameter", "script"}:
            saved_view_mode = startup_page
        self.view_mode = (
            saved_view_mode
            if saved_view_mode in {"task", "parameter", "script"}
            else "task"
        )
        self.dir_path = {
            "task": self.task_dir,
            "parameter": self.parameter_dir,
            "script": self.script_dir,
        }[self.view_mode]
        saved_files = self.application_settings.get("selected_files", {})
        self.last_selected_files: dict[str, str] = (
            {
                key: str(value)
                for key, value in saved_files.items()
                if key in {"task", "parameter", "script"} and value
            }
            if isinstance(saved_files, dict)
            else {}
        )
        self._normal_window_geometry = str(
            self.application_settings.get("window_geometry", "1100x720")
        )
        self.parameter_template_path = parameter_template_path.resolve()
        self.script_template_path = script_template_path.resolve()
        self.current_path: Path | None = None
        self.parameter_selector_button: ttk.Button | None = None
        self.script_selector_button: ttk.Button | None = None
        self._dirty = False
        self._auto_save_after_id: str | None = None
        self._loading_editor = False
        self._changing_selection = False
        self._deploying = False
        self._execution_cancel_event = threading.Event()
        self._stop_requested = False
        self._settings_page_visible = False
        self._interaction_panel_visible = False
        self._interaction_panel_user_hidden = False
        self._interaction_panel_available = False
        self._events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=2048)
        self.ssh_tabs: dict[Path, list[SSHTerminalTab]] = {}
        self._ssh_tab_sequence: dict[Path, int] = {}
        self._ssh_tab_names: dict[SSHTerminalTab, str] = {}

        self.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._create_widgets()
        self._restore_current_view_file()
        self.master.bind("<Configure>", self._remember_window_geometry, add="+")
        self.master.bind("<Control-s>", self._save_shortcut, add="+")
        self.master.bind("<Control-S>", self._save_shortcut, add="+")
        self.after_idle(self._restore_window_state)
        self.after(100, self._poll_events)

    def _create_widgets(self) -> None:
        toolbar = ttk.Frame(self)
        self.toolbar = toolbar
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(5, weight=1)

        ttk.Button(toolbar, text="新建", command=self.create_file).grid(
            row=0, column=0, padx=(0, 4)
        )
        ttk.Button(toolbar, text="重命名", command=self.rename_file).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(toolbar, text="复制", command=self.copy_file).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(toolbar, text="删除", command=self.delete_file).grid(
            row=0, column=3, padx=4
        )
        ttk.Button(toolbar, text="保存", command=self.save_text).grid(
            row=0, column=4, padx=4
        )

        self.password_visibility_button = ttk.Button(
            toolbar,
            text="开启隐藏密码",
            command=self._toggle_password_visibility,
        )
        self.password_visibility_button.grid(row=0, column=6, padx=(8, 4))

        self.connect_button = ttk.Button(
            toolbar, text="连接", command=self._toggle_ssh_connection
        )
        self.connect_button.grid(row=0, column=7, padx=4)

        self.interaction_panel_button = ttk.Button(
            toolbar,
            text="隐藏交互窗口",
            command=self._toggle_interaction_panel,
        )
        self.interaction_panel_button.grid(row=0, column=8, padx=4)

        self.deploy_button = ttk.Button(
            toolbar, text="执行", command=self.develop_method
        )
        self.deploy_button.grid(row=0, column=9, padx=(4, 0))

        panes = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.main_panes = panes
        panes.grid(row=1, column=0, sticky="nsew")
        sidebar_border_color = "#4b5563"

        sidebar_border = tk.Frame(
            panes,
            background=sidebar_border_color,
            borderwidth=0,
            highlightthickness=0,
        )
        sidebar_border.columnconfigure(0, weight=1)
        sidebar_border.rowconfigure(0, weight=1)

        left_panel = tk.Frame(
            sidebar_border,
            background="#e5e7eb",
            borderwidth=0,
            highlightthickness=0,
        )
        left_panel.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=1,
            pady=1,
        )
        left_panel.columnconfigure(1, weight=1)
        left_panel.rowconfigure(0, weight=1)

        mode_border = tk.Frame(
            left_panel,
            background=sidebar_border_color,
            borderwidth=0,
            highlightthickness=0,
        )
        mode_border.grid(row=0, column=0, sticky="ns")
        mode_border.columnconfigure(0, weight=1)
        mode_border.rowconfigure(0, weight=1)

        mode_frame = tk.Frame(mode_border, background="#e5e7eb")
        mode_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=0,
            pady=0,
        )
        mode_frame.rowconfigure(3, weight=1)
        self.task_mode_button = tk.Button(
            mode_frame,
            text="任务",
            width=7,
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=8,
            command=lambda: self.switch_view("task"),
        )
        self.task_mode_button.grid(row=0, column=0, sticky="ew")
        self.parameter_mode_button = tk.Button(
            mode_frame,
            text="配置",
            width=7,
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=8,
            command=lambda: self.switch_view("parameter"),
        )
        self.parameter_mode_button.grid(row=1, column=0, sticky="ew")
        self.script_mode_button = tk.Button(
            mode_frame,
            text="脚本",
            width=7,
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=8,
            command=lambda: self.switch_view("script"),
        )
        self.script_mode_button.grid(row=2, column=0, sticky="ew")
        self.settings_button = tk.Button(
            mode_frame,
            text="⚙ 设置",
            width=7,
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=8,
            command=self._show_system_settings,
        )
        self.settings_button.grid(row=4, column=0, sticky="sew")

        list_frame = tk.Frame(
            left_panel,
            background=sidebar_border_color,
            borderwidth=0,
            highlightthickness=0,
        )
        self.list_frame = list_frame
        list_frame.grid(row=0, column=1, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        list_content = tk.Frame(
            list_frame,
            background="#ffffff",
            borderwidth=0,
            highlightthickness=0,
        )
        list_content.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=0,
            pady=0,
        )
        list_content.columnconfigure(0, weight=1)
        list_content.rowconfigure(0, weight=1)

        tree_style = ttk.Style(self)
        tree_style.configure(
            "FileList.Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            borderwidth=0,
            relief=tk.FLAT,
            rowheight=26,
        )
        tree_style.layout(
            "FileList.Treeview",
            [("Treeview.treearea", {"sticky": "nsew"})],
        )
        tree_style.map(
            "FileList.Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", "#111827")],
        )
        self.tree = ttk.Treeview(
            list_content,
            show="tree",
            selectmode="browse",
            style="FileList.Treeview",
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scrollbar = ttk.Scrollbar(
            list_content, orient=tk.VERTICAL, command=self.tree.yview
        )
        tree_scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        right_panel = tk.PanedWindow(
            panes,
            orient=tk.VERTICAL,
            borderwidth=0,
            background="#d1d5db",
            sashwidth=7,
            sashrelief=tk.RAISED,
            showhandle=True,
            handlesize=10,
            handlepad=4,
            opaqueresize=True,
        )
        self.right_panel = right_panel

        self.editor_panel = ttk.LabelFrame(right_panel, text="任务编辑")
        self.editor_panel.columnconfigure(0, weight=1)
        self.editor_panel.rowconfigure(1, weight=1)
        self.task_editor_toolbar = ttk.Frame(self.editor_panel, padding=(6, 4))
        self.task_editor_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.script_selector_button = ttk.Button(
            self.task_editor_toolbar,
            text="增加步骤",
            width=10,
            command=self._add_workflow_step,
        )
        self.script_selector_button.grid(row=0, column=0, sticky="w")
        self.text = tk.Text(
            self.editor_panel,
            undo=True,
            autoseparators=True,
            maxundo=-1,
            wrap="none",
            font=("Cascadia Mono", self.editor_font_size),
            background="#ffffff",
            foreground="#1f2937",
            insertbackground="#111827",
            selectbackground="#bfdbfe",
            selectforeground="#111827",
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#d1d5db",
            highlightcolor="#3b82f6",
            padx=12,
            pady=10,
            spacing1=2,
            spacing3=2,
            exportselection=False,
        )
        self.text.grid(row=1, column=0, sticky="nsew")
        editor_scrollbar = ttk.Scrollbar(
            self.editor_panel, orient=tk.VERTICAL, command=self.text.yview
        )
        editor_scrollbar.grid(row=1, column=1, sticky="ns")
        editor_horizontal_scrollbar = ttk.Scrollbar(
            self.editor_panel, orient=tk.HORIZONTAL, command=self.text.xview
        )
        editor_horizontal_scrollbar.grid(row=2, column=0, sticky="ew")
        self.text.configure(
            yscrollcommand=editor_scrollbar.set,
            xscrollcommand=editor_horizontal_scrollbar.set,
        )
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<Control-MouseWheel>", self._on_editor_zoom)

        log_panel = ttk.LabelFrame(right_panel, text="SSH 交互终端 / 执行输出")
        self.log_panel = log_panel
        log_panel.columnconfigure(0, weight=1)
        log_panel.rowconfigure(0, weight=1)

        self.terminal_notebook = ttk.Notebook(log_panel)
        self.terminal_notebook.grid(row=0, column=0, sticky="nsew")
        deployment_output_frame = ttk.Frame(self.terminal_notebook)
        deployment_output_frame.columnconfigure(0, weight=1)
        deployment_output_frame.rowconfigure(0, weight=1)
        self.terminal_notebook.add(deployment_output_frame, text="执行输出")
        self.deployment_output_frame = deployment_output_frame

        self.log_text = tk.Text(
            deployment_output_frame,
            height=10,
            state=tk.DISABLED,
            wrap="word",
            font=("Cascadia Mono", 10),
            background="#111827",
            foreground="#e5e7eb",
            selectbackground="#374151",
            selectforeground="#ffffff",
            relief=tk.FLAT,
            borderwidth=0,
            padx=8,
            pady=8,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(
            deployment_output_frame,
            orient=tk.VERTICAL,
            command=self.log_text.yview,
        )
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        right_panel.add(self.editor_panel, minsize=120, stretch="always")
        self.settings_page = self._create_system_settings_page(panes)
        panes.add(sidebar_border, weight=1)
        panes.add(right_panel, weight=4)

        status_frame = ttk.Frame(self)
        status_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        status_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").grid(
            row=0, column=0, sticky="ew"
        )
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            status_frame,
            variable=self.progress_var,
            maximum=100,
            length=220,
        )
        self.progress_bar.grid(row=0, column=1, padx=(8, 4))
        self.progress_text_var = tk.StringVar(value="0%")
        ttk.Label(
            status_frame,
            textvariable=self.progress_text_var,
            width=6,
            anchor="e",
        ).grid(row=0, column=2)
        self._update_view_controls()

    def update_tree(self, select_path: Path | None = None) -> None:
        self.tree.delete(*self.tree.get_children())

        files = sorted(
            (
                path
                for path in self.dir_path.iterdir()
                if path.is_file()
                and (
                    (
                        self.view_mode in {"task", "parameter"}
                        and path.suffix.lower() == ".txt"
                    )
                    or (
                        self.view_mode == "script"
                        and path.suffix.lower() in {
                            ".sh", ".bash", ".bat", ".cmd", ".ps1"
                        }
                    )
                )
            ),
            key=lambda path: path.name.lower(),
        )
        for path in files:
            item = self.tree.insert("", tk.END, text=path.stem, values=(str(path),))
            if select_path is not None and path.resolve() == select_path.resolve():
                self.tree.selection_set(item)
                self.tree.focus(item)

    def _restore_current_view_file(self) -> None:
        file_name = self.last_selected_files.get(self.view_mode)
        selected_path: Path | None = None
        if file_name:
            candidate = self.dir_path / Path(file_name).name
            if candidate.is_file():
                selected_path = candidate.resolve()
        self.update_tree(selected_path)
        if selected_path is not None:
            self._load_file(selected_path)
        else:
            self._restore_untitled_draft()

    def switch_view(self, view_mode: str) -> None:
        if view_mode not in {
            "task",
            "parameter",
            "script",
        }:
            return
        if view_mode == self.view_mode and not self._settings_page_visible:
            return
        if self._deploying:
            messagebox.showwarning("正在部署", "部署完成后才能切换列表")
            return
        if self._settings_page_visible:
            self._hide_system_settings()
            if view_mode == self.view_mode:
                self._update_view_controls()
                self.status_var.set(f"已返回{self._view_label()}列表")
                return
        if not self._confirm_pending_changes():
            return
        if not self._conceal_current_parameter_password():
            return

        self.view_mode = view_mode
        self.dir_path = {
            "task": self.task_dir,
            "parameter": self.parameter_dir,
            "script": self.script_dir,
        }[view_mode]
        self.current_path = None
        self._set_editor_content("")
        self._restore_current_view_file()
        self._update_view_controls()
        self.status_var.set(f"已切换到{self._view_label()}列表")

    def _update_view_controls(self) -> None:
        active_background = "#ffffff"
        inactive_background = "#e5e7eb"
        hover_background = "#f3f4f6"

        self.task_mode_button.configure(
            state=tk.DISABLED if self._deploying else tk.NORMAL,
            background=(
                active_background
                if self.view_mode == "task" and not self._settings_page_visible
                else inactive_background
            ),
            activebackground=(
                active_background
                if self.view_mode == "task" and not self._settings_page_visible
                else hover_background
            ),
            foreground="#111827",
            disabledforeground="#6b7280" if self._deploying else "#111827",
            relief=tk.FLAT,
        )
        self.parameter_mode_button.configure(
            state=tk.DISABLED if self._deploying else tk.NORMAL,
            background=(
                active_background
                if self.view_mode == "parameter" and not self._settings_page_visible
                else inactive_background
            ),
            activebackground=(
                active_background
                if self.view_mode == "parameter" and not self._settings_page_visible
                else hover_background
            ),
            foreground="#111827",
            disabledforeground="#6b7280" if self._deploying else "#111827",
            relief=tk.FLAT,
        )
        self.script_mode_button.configure(
            state=tk.DISABLED if self._deploying else tk.NORMAL,
            background=(
                active_background
                if self.view_mode == "script" and not self._settings_page_visible
                else inactive_background
            ),
            activebackground=(
                active_background
                if self.view_mode == "script" and not self._settings_page_visible
                else hover_background
            ),
            foreground="#111827",
            disabledforeground="#6b7280" if self._deploying else "#111827",
            relief=tk.FLAT,
        )
        self.settings_button.configure(
            state=tk.DISABLED if self._deploying else tk.NORMAL,
            background=(
                active_background
                if self._settings_page_visible
                else inactive_background
            ),
            activebackground=(
                active_background
                if self._settings_page_visible
                else hover_background
            ),
            foreground="#111827",
            disabledforeground="#6b7280" if self._deploying else "#111827",
            relief=tk.FLAT,
        )

        if self.view_mode == "parameter":
            self.password_visibility_button.grid()
            password_button_text = "开启隐藏密码"
            if self._password_hiding_enabled():
                password_button_text = (
                    "隐藏密码" if self._parameter_password_visible else "显示密码"
                )
            can_toggle_password = (
                not self._deploying
                and (
                    not self._password_hiding_enabled()
                    or self.current_path is not None
                )
            )
            self.password_visibility_button.configure(
                text=password_button_text,
                state=tk.NORMAL if can_toggle_password else tk.DISABLED,
            )
        else:
            self.password_visibility_button.grid_remove()

        selected_parameter = (
            self.current_path.resolve()
            if self.view_mode == "parameter" and self.current_path is not None
            else None
        )
        can_use_connect = (
            not self._deploying
            and selected_parameter is not None
        )
        self.connect_button.configure(
            text="连接",
            state=tk.NORMAL if can_use_connect else tk.DISABLED,
        )

        if self.view_mode == "task":
            self.editor_panel.configure(text="任务编辑")
            self.deploy_button.configure(
                text="停止" if self._deploying else "执行",
                state=(
                    tk.DISABLED
                    if self._deploying and self._stop_requested
                    else tk.NORMAL
                ),
            )
        else:
            self.editor_panel.configure(text=f"{self._view_label()}编辑")
            self.deploy_button.configure(
                text="停止" if self._deploying else "执行",
                state=(
                    tk.DISABLED
                    if not self._deploying or self._stop_requested
                    else tk.NORMAL
                ),
            )
        self._update_interaction_panel_button()

    def _view_label(self) -> str:
        return {"task": "任务", "parameter": "配置", "script": "脚本"}[
            self.view_mode
        ]

    def _has_interaction_activity(self) -> bool:
        return self._deploying or any(
            terminal_tab.state in {"connecting", "connected"}
            for terminal_tabs in self.ssh_tabs.values()
            for terminal_tab in terminal_tabs
        )

    def _show_interaction_panel(self, force: bool = False) -> None:
        self._interaction_panel_available = True
        if self._interaction_panel_visible:
            self._update_interaction_panel_button()
            return
        if self._interaction_panel_user_hidden and not force:
            return
        self.right_panel.add(self.log_panel, minsize=100, stretch="always")
        self._interaction_panel_visible = True
        if force:
            self._interaction_panel_user_hidden = False
        sash_position = self.application_settings.get("editor_log_sash")
        try:
            if sash_position is not None:
                sash_y = max(100, int(sash_position))

                def restore_sash() -> None:
                    try:
                        if self._interaction_panel_visible:
                            self.right_panel.sash_place(0, 0, sash_y)
                    except tk.TclError:
                        pass

                self.after_idle(restore_sash)
        except (tk.TclError, TypeError, ValueError):
            pass
        self._update_interaction_panel_button()

    def _hide_interaction_panel(self, manual: bool = False) -> None:
        if self._interaction_panel_visible:
            try:
                self.right_panel.forget(self.log_panel)
            except tk.TclError:
                pass
            self._interaction_panel_visible = False
        if manual:
            self._interaction_panel_user_hidden = True
        self._update_interaction_panel_button()

    def _toggle_interaction_panel(self) -> None:
        if self._interaction_panel_visible:
            self._hide_interaction_panel(manual=True)
        else:
            self._show_interaction_panel(force=True)

    def _sync_interaction_panel_visibility(self) -> None:
        if self._has_interaction_activity():
            self._show_interaction_panel()
        else:
            self._interaction_panel_user_hidden = False
            self._hide_interaction_panel()

    def _update_interaction_panel_button(self) -> None:
        if not self._interaction_panel_available:
            self.interaction_panel_button.grid_remove()
            return
        self.interaction_panel_button.configure(
            text=(
                "隐藏交互窗口"
                if self._interaction_panel_visible
                else "显示交互窗口"
            )
        )
        self.interaction_panel_button.grid()

    def _choose_ssh_default_open_path(
        self,
        paths: tuple[str, ...],
        connection_name: str | None = None,
    ) -> str | None:
        dialog = tk.Toplevel(self.master)
        dialog.title(
            f"选择默认目录 - {connection_name}"
            if connection_name
            else "选择默认目录"
        )
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        prompt = (
            f"请选择任务连接“{connection_name}”使用的服务器目录："
            if connection_name
            else "请选择本次 SSH 连接进入的服务器目录："
        )
        ttk.Label(frame, text=prompt).grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, 8),
        )
        selected_path = tk.StringVar(value=paths[0])
        path_selector = ttk.Combobox(
            frame,
            textvariable=selected_path,
            values=paths,
            state="readonly",
            width=60,
        )
        path_selector.grid(row=1, column=0, sticky="ew")

        result: list[str | None] = [None]

        def confirm() -> None:
            result[0] = selected_path.get()
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="确定", command=confirm).grid(row=0, column=0)
        ttk.Button(buttons, text="取消", command=dialog.destroy).grid(
            row=0,
            column=1,
            padx=(8, 0),
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.master.winfo_rootx() + max(
            0,
            (self.master.winfo_width() - width) // 2,
        )
        y = self.master.winfo_rooty() + max(
            0,
            (self.master.winfo_height() - height) // 2,
        )
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        path_selector.focus_set()
        self.master.wait_window(dialog)
        return result[0]

    def _toggle_ssh_connection(self) -> None:
        selected_parameter = (
            self.current_path.resolve()
            if self.view_mode == "parameter" and self.current_path is not None
            else None
        )
        if selected_parameter is None:
            messagebox.showwarning("无法连接", "请在“配置”页面选择服务器配置文件")
            return

        if self._dirty:
            messagebox.showwarning(
                "配置尚未保存",
                "当前配置只有暂存内容，请先点击保存后再连接服务器",
            )
            return

        try:
            parameters = load_server_parameters(selected_parameter)
        except ConfigurationError as exc:
            messagebox.showerror("服务器配置错误", str(exc))
            return

        default_open_path: str | None = None
        if len(parameters.default_open_paths) == 1:
            default_open_path = parameters.default_open_paths[0]
        elif len(parameters.default_open_paths) > 1:
            default_open_path = self._choose_ssh_default_open_path(
                parameters.default_open_paths
            )
            if default_open_path is None:
                return

        terminal_tab = SSHTerminalTab(
            self.terminal_notebook,
            parameters=parameters,
            parameter_path=selected_parameter,
            default_open_path=default_open_path,
            state_changed=self._on_ssh_tab_state_changed,
            close_requested=self._close_ssh_tab,
        )
        terminal_tabs = self.ssh_tabs.setdefault(selected_parameter, [])
        terminal_tabs.append(terminal_tab)
        sequence = self._ssh_tab_sequence.get(selected_parameter, 0) + 1
        self._ssh_tab_sequence[selected_parameter] = sequence
        tab_name = parameters.name if sequence == 1 else f"{parameters.name} ({sequence})"
        self._ssh_tab_names[terminal_tab] = tab_name
        self.terminal_notebook.add(terminal_tab, text=tab_name)
        self._show_interaction_panel(force=True)
        self.terminal_notebook.select(terminal_tab)
        terminal_tab.start_connection()
        self.status_var.set(f"正在连接 {parameters.target}……")
        self._update_view_controls()

    def _on_ssh_tab_state_changed(self, terminal_tab: SSHTerminalTab) -> None:
        prefixes = {
            "connecting": "… ",
            "connected": "● ",
            "cancelled": "○ ",
            "error": "× ",
            "disconnected": "○ ",
        }
        try:
            self.terminal_notebook.tab(
                terminal_tab,
                text=(
                    f"{prefixes[terminal_tab.state]}"
                    f"{self._ssh_tab_names.get(terminal_tab, terminal_tab.parameters.name)}"
                ),
            )
        except tk.TclError:
            pass

        try:
            is_active_tab = (
                str(self.terminal_notebook.select()) == str(terminal_tab)
            )
        except tk.TclError:
            is_active_tab = False
        if is_active_tab:
            if terminal_tab.state == "connected":
                self.status_var.set(f"SSH 已连接：{terminal_tab.parameters.target}")
            elif terminal_tab.state == "error":
                self.status_var.set(f"SSH 连接失败：{terminal_tab.parameters.target}")
            elif terminal_tab.state == "cancelled":
                self.status_var.set("已取消 SSH 连接")
        self._sync_interaction_panel_visibility()
        self._update_view_controls()

    def _close_ssh_tab(self, terminal_tab: SSHTerminalTab) -> None:
        parameter_path = terminal_tab.parameter_path
        terminal_tabs = self.ssh_tabs.get(parameter_path)
        if terminal_tabs is not None and terminal_tab in terminal_tabs:
            terminal_tabs.remove(terminal_tab)
            if not terminal_tabs:
                self.ssh_tabs.pop(parameter_path, None)
        self._ssh_tab_names.pop(terminal_tab, None)
        terminal_tab.close()
        try:
            self.terminal_notebook.forget(terminal_tab)
        except tk.TclError:
            pass
        terminal_tab.destroy()
        self._sync_interaction_panel_visibility()
        self._update_view_controls()

    def _close_parameter_ssh_tab(self, parameter_path: Path) -> bool:
        resolved_path = parameter_path.resolve()
        terminal_tabs = list(self.ssh_tabs.get(resolved_path, []))
        if not terminal_tabs:
            return False
        for terminal_tab in terminal_tabs:
            self._close_ssh_tab(terminal_tab)
        return True

    def _read_settings(self) -> dict[str, object]:
        if not self.settings_path.is_file():
            return {}
        try:
            loaded = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _password_hiding_enabled(self) -> bool:
        settings = self.application_settings.get("password_hiding")
        return bool(
            isinstance(settings, dict)
            and settings.get("enabled") is True
            and str(settings.get("unlock_password", "")).strip()
        )

    def _ask_password(self, title: str, prompt: str) -> str | None:
        dialog = tk.Toplevel(self.master)
        dialog.title(title)
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=prompt).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 8),
        )

        password_var = tk.StringVar()
        password_entry = ttk.Entry(
            frame,
            textvariable=password_var,
            show="*",
            width=36,
        )
        password_entry.grid(row=1, column=0, sticky="ew")

        def toggle_visibility() -> None:
            password_entry.configure(show="" if password_entry.cget("show") else "*")

        ttk.Button(
            frame,
            text="👁",
            width=3,
            command=toggle_visibility,
        ).grid(row=1, column=1, padx=(6, 0))

        result: list[str | None] = [None]

        def confirm() -> None:
            result[0] = password_var.get()
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="确定", command=confirm).grid(row=0, column=0)
        ttk.Button(buttons, text="取消", command=cancel).grid(
            row=0,
            column=1,
            padx=(8, 0),
        )

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.master.winfo_rootx() + max(
            0,
            (self.master.winfo_width() - width) // 2,
        )
        y = self.master.winfo_rooty() + max(
            0,
            (self.master.winfo_height() - height) // 2,
        )
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        password_entry.focus_set()
        self.master.wait_window(dialog)
        return result[0]

    def _toggle_password_visibility(self) -> None:
        if self.view_mode != "parameter":
            return
        if not self._password_hiding_enabled():
            self._enable_password_hiding()
            return
        if self.current_path is None:
            messagebox.showwarning("未选择配置", "请先选择一个服务器配置文件")
            return
        if self._parameter_password_visible:
            if self._conceal_current_parameter_password():
                self.status_var.set("服务器密码已隐藏")
            return
        if not self._password_session_unlocked and not self._unlock_password_hiding():
            return
        self._show_current_parameter_password()

    def _enable_password_hiding(self, show_message: bool = True) -> bool:
        if not self._confirm_pending_changes():
            return False

        password_hiding_settings = self.application_settings.get("password_hiding")
        encrypted_unlock_password = (
            str(password_hiding_settings.get("unlock_password", "")).strip()
            if isinstance(password_hiding_settings, dict)
            else ""
        )
        if encrypted_unlock_password:
            if not self._unlock_password_hiding():
                return False
        else:
            administrator_password = self._prompt_new_administrator_password()
            if administrator_password is None:
                return False
            try:
                encrypted_unlock_password = protect_text(administrator_password)
            except PasswordProtectionError as exc:
                messagebox.showerror("开启失败", str(exc))
                return False

        try:
            updates = self._encrypted_parameter_file_updates()
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            messagebox.showerror("开启失败", str(exc))
            return False

        written: list[tuple[Path, str]] = []
        try:
            for path, original, encrypted in updates:
                temporary_path = path.with_name(f".{path.name}.password.tmp")
                temporary_path.write_text(encrypted, encoding="utf-8")
                temporary_path.replace(path)
                written.append((path, original))

            settings = dict(self.application_settings)
            password_hiding_settings = settings.get("password_hiding")
            password_hiding_settings = (
                dict(password_hiding_settings)
                if isinstance(password_hiding_settings, dict)
                else {}
            )
            password_hiding_settings.update(
                {
                    "enabled": True,
                    "unlock_password": encrypted_unlock_password,
                }
            )
            settings["password_hiding"] = password_hiding_settings
            if not self._write_settings(settings):
                raise OSError("无法保存隐藏密码设置")
        except OSError as exc:
            rollback_errors: list[str] = []
            for path, original in reversed(written):
                try:
                    path.write_text(original, encoding="utf-8")
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path.name}: {rollback_exc}")
            detail = (
                "\n恢复文件时仍有错误：" + "；".join(rollback_errors)
                if rollback_errors
                else ""
            )
            messagebox.showerror("开启失败", f"{exc}{detail}")
            return False

        self._password_session_unlocked = True
        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.current_path is not None:
            self._load_file(self.current_path)
        self._update_view_controls()
        self.status_var.set("已开启隐藏密码，服务器密码已加密保存")
        if show_message:
            messagebox.showinfo(
                "开启成功",
                "已加密服务器密码。重新打开软件后，密码默认隐藏。",
            )
        return True

    def _prompt_new_administrator_password(self) -> str | None:
        administrator_password = self._ask_password(
            "设置管理员密码",
            "请输入新的管理员密码：",
        )
        if administrator_password is None:
            return None
        administrator_password = administrator_password.strip()
        if not administrator_password:
            messagebox.showerror("密码无效", "管理员密码不能为空")
            return None
        confirmation = self._ask_password(
            "确认管理员密码",
            "请再次输入新的管理员密码：",
        )
        if confirmation is None:
            return None
        confirmation = confirmation.strip()
        if not hmac.compare_digest(
            administrator_password.encode("utf-8"),
            confirmation.encode("utf-8"),
        ):
            messagebox.showerror("密码不一致", "两次输入的管理员密码不一致")
            return None
        return administrator_password

    def _encrypted_parameter_file_updates(self) -> list[tuple[Path, str, str]]:
        paths = list(self.parameter_dir.glob("*.txt"))
        draft_directory = self.draft_root / "parameter"
        if draft_directory.is_dir():
            paths.extend(draft_directory.glob("*.draft"))
        updates: list[tuple[Path, str, str]] = []
        for path in sorted(paths, key=lambda value: str(value).lower()):
            content = path.read_text(encoding="utf-8-sig")
            match = _PASSWORD_LINE_PATTERN.search(content)
            if match is None:
                continue
            value = match.group("value").strip()
            if not value:
                continue
            if is_protected(value):
                if not is_legacy_protected(value):
                    continue
                value = unprotect_text(value)
            encrypted = self._replace_password_value(
                content,
                protect_text(value),
            )
            updates.append((path, content, encrypted))
        return updates

    def _unlock_password_hiding(self, force: bool = False) -> bool:
        if self._password_session_unlocked and not force:
            return True
        settings = self.application_settings.get("password_hiding")
        if not isinstance(settings, dict):
            messagebox.showerror("设置错误", "隐藏密码设置不存在")
            return False
        encrypted_password = str(settings.get("unlock_password", "")).strip()
        try:
            expected_password = unprotect_text(encrypted_password)
        except PasswordProtectionError as exc:
            messagebox.showerror("无法验证", str(exc))
            return False
        entered_password = self._ask_password(
            "管理员验证",
            "请输入管理员密码：",
        )
        if entered_password is None:
            return False
        entered_password = entered_password.strip()
        expected_password = expected_password.strip()
        if not hmac.compare_digest(
            entered_password.encode("utf-8"),
            expected_password.encode("utf-8"),
        ):
            messagebox.showerror("密码错误", "管理员密码不正确")
            return False
        if is_legacy_protected(encrypted_password):
            try:
                upgraded_password = protect_text(expected_password)
            except PasswordProtectionError as exc:
                messagebox.showerror("升级失败", str(exc))
                return False
            settings = dict(self.application_settings)
            password_hiding_settings = dict(settings["password_hiding"])
            password_hiding_settings["unlock_password"] = upgraded_password
            settings["password_hiding"] = password_hiding_settings
            if not self._write_settings(settings):
                messagebox.showerror("保存失败", "无法升级管理员密码加密格式")
                return False
        self._password_session_unlocked = True
        return True

    def _require_system_settings_password(self) -> bool:
        password_hiding_settings = self.application_settings.get("password_hiding")
        encrypted_password = (
            str(password_hiding_settings.get("unlock_password", "")).strip()
            if isinstance(password_hiding_settings, dict)
            else ""
        )
        if encrypted_password:
            if not self._unlock_password_hiding(force=True):
                return False
            return self._upgrade_parameter_password_encryption()

        administrator_password = self._prompt_new_administrator_password()
        if administrator_password is None:
            return False
        try:
            encrypted_password = protect_text(administrator_password)
        except PasswordProtectionError as exc:
            messagebox.showerror("初始化失败", str(exc))
            return False

        settings = dict(self.application_settings)
        password_hiding_settings = settings.get("password_hiding")
        password_hiding_settings = (
            dict(password_hiding_settings)
            if isinstance(password_hiding_settings, dict)
            else {}
        )
        password_hiding_settings.setdefault("enabled", False)
        password_hiding_settings["unlock_password"] = encrypted_password
        settings["password_hiding"] = password_hiding_settings
        if not self._write_settings(settings):
            messagebox.showerror("初始化失败", "无法保存管理员密码")
            return False
        self._password_session_unlocked = True
        messagebox.showinfo("初始化成功", "管理员密码已设置")
        return True

    def _upgrade_parameter_password_encryption(self) -> bool:
        if not self._password_hiding_enabled():
            return True
        try:
            updates = self._encrypted_parameter_file_updates()
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            messagebox.showerror("升级失败", str(exc))
            return False
        written: list[tuple[Path, str]] = []
        try:
            for path, original, encrypted in updates:
                temporary_path = path.with_name(f".{path.name}.password.tmp")
                temporary_path.write_text(encrypted, encoding="utf-8")
                temporary_path.replace(path)
                written.append((path, original))
        except OSError as exc:
            for path, original in reversed(written):
                try:
                    path.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            messagebox.showerror("升级失败", f"无法升级服务器密码加密格式：{exc}")
            return False
        if self.view_mode == "parameter" and self.current_path is not None:
            self._load_file(self.current_path)
        return True

    def _change_administrator_password(self) -> None:
        password_hiding_settings = self.application_settings.get("password_hiding")
        has_password = bool(
            isinstance(password_hiding_settings, dict)
            and str(password_hiding_settings.get("unlock_password", "")).strip()
        )
        if has_password and not self._unlock_password_hiding():
            return
        administrator_password = self._prompt_new_administrator_password()
        if administrator_password is None:
            return
        try:
            encrypted_password = protect_text(administrator_password)
        except PasswordProtectionError as exc:
            messagebox.showerror("更改失败", str(exc))
            return

        settings = dict(self.application_settings)
        password_hiding_settings = settings.get("password_hiding")
        password_hiding_settings = (
            dict(password_hiding_settings)
            if isinstance(password_hiding_settings, dict)
            else {"enabled": False}
        )
        password_hiding_settings["unlock_password"] = encrypted_password
        settings["password_hiding"] = password_hiding_settings
        if not self._write_settings(settings):
            messagebox.showerror("更改失败", "无法保存管理员密码")
            return
        self._password_session_unlocked = True
        messagebox.showinfo("更改成功", "管理员密码已更改")

    def _decrypted_parameter_file_updates(self) -> list[tuple[Path, str, str]]:
        paths = list(self.parameter_dir.glob("*.txt"))
        draft_directory = self.draft_root / "parameter"
        if draft_directory.is_dir():
            paths.extend(draft_directory.glob("*.draft"))
        updates: list[tuple[Path, str, str]] = []
        for path in sorted(paths, key=lambda value: str(value).lower()):
            content = path.read_text(encoding="utf-8-sig")
            match = _PASSWORD_LINE_PATTERN.search(content)
            if match is None:
                continue
            value = match.group("value").strip()
            if not value or not is_protected(value):
                continue
            decrypted = self._replace_password_value(
                content,
                unprotect_text(value),
            )
            updates.append((path, content, decrypted))
        return updates

    def _disable_password_hiding(self, show_message: bool = True) -> bool:
        if not self._password_hiding_enabled():
            return True
        if not self._unlock_password_hiding():
            return False
        try:
            updates = self._decrypted_parameter_file_updates()
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            messagebox.showerror("关闭失败", str(exc))
            return False

        written: list[tuple[Path, str]] = []
        try:
            for path, original, decrypted in updates:
                temporary_path = path.with_name(f".{path.name}.password.tmp")
                temporary_path.write_text(decrypted, encoding="utf-8")
                temporary_path.replace(path)
                written.append((path, original))

            settings = dict(self.application_settings)
            password_hiding_settings = settings.get("password_hiding")
            password_hiding_settings = (
                dict(password_hiding_settings)
                if isinstance(password_hiding_settings, dict)
                else {}
            )
            password_hiding_settings["enabled"] = False
            settings["password_hiding"] = password_hiding_settings
            if not self._write_settings(settings):
                raise OSError("无法保存密码隐藏设置")
        except OSError as exc:
            rollback_errors: list[str] = []
            for path, original in reversed(written):
                try:
                    path.write_text(original, encoding="utf-8")
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path.name}: {rollback_exc}")
            detail = (
                "\n恢复文件时仍有错误：" + "；".join(rollback_errors)
                if rollback_errors
                else ""
            )
            messagebox.showerror("关闭失败", f"{exc}{detail}")
            return False

        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.view_mode == "parameter" and self.current_path is not None:
            self._load_file(self.current_path)
        self._update_view_controls()
        self.status_var.set("密码隐藏已关闭，服务器密码已恢复为明文保存")
        if show_message:
            messagebox.showinfo("关闭成功", "服务器密码已恢复为明文保存")
        return True

    def _show_current_parameter_password(self) -> None:
        content = self.text.get("1.0", "end-1c")
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            messagebox.showwarning("没有密码配置", "当前配置中没有 PASSWORD 参数")
            return
        displayed_value = match.group("value").strip()
        encrypted_value = self._parameter_password_ciphertext
        try:
            if encrypted_value:
                plaintext = unprotect_text(encrypted_value)
            elif displayed_value == _HIDDEN_PASSWORD_VALUE:
                plaintext = ""
            elif is_protected(displayed_value):
                encrypted_value = displayed_value
                plaintext = unprotect_text(displayed_value)
            else:
                plaintext = displayed_value
        except PasswordProtectionError as exc:
            messagebox.showerror("无法显示密码", str(exc))
            return
        updated = self._replace_password_value(content, plaintext)
        was_dirty = self._dirty
        self._set_editor_content(updated, preserve_view=True)
        self._dirty = was_dirty
        if was_dirty:
            self._schedule_auto_save()
        self._parameter_password_visible = True
        self._parameter_password_ciphertext = encrypted_value
        self._visible_parameter_password = plaintext
        self.status_var.set("服务器密码已显示，本次启动不再重复验证")
        self._update_view_controls()

    def _conceal_current_parameter_password(self) -> bool:
        if (
            self.view_mode != "parameter"
            or not self._password_hiding_enabled()
            or not self._parameter_password_visible
        ):
            return True
        content = self.text.get("1.0", "end-1c")
        try:
            storage_content = self._content_for_storage(content)
        except PasswordProtectionError as exc:
            messagebox.showerror("无法隐藏密码", str(exc))
            return False
        match = _PASSWORD_LINE_PATTERN.search(storage_content)
        encrypted_value = match.group("value").strip() if match is not None else ""
        display_value = _HIDDEN_PASSWORD_VALUE if encrypted_value else ""
        updated = self._replace_password_value(content, display_value)
        was_dirty = self._dirty
        self._set_editor_content(updated, preserve_view=True)
        self._dirty = was_dirty
        if was_dirty:
            self._schedule_auto_save()
        self._parameter_password_visible = False
        self._visible_parameter_password = None
        self._parameter_password_ciphertext = encrypted_value or None
        self._update_view_controls()
        return True

    def _prepare_parameter_content_for_display(
        self,
        content: str,
    ) -> tuple[str, str]:
        self._parameter_password_visible = False
        self._parameter_password_ciphertext = None
        self._visible_parameter_password = None
        if self.view_mode != "parameter" or not self._password_hiding_enabled():
            return content, content
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            return content, content
        value = match.group("value").strip()
        if not value:
            return content, content
        if is_legacy_protected(value):
            encrypted_value = protect_text(unprotect_text(value))
        else:
            encrypted_value = value if is_protected(value) else protect_text(value)
        self._parameter_password_ciphertext = encrypted_value
        stored = self._replace_password_value(content, encrypted_value)
        displayed = self._replace_password_value(content, _HIDDEN_PASSWORD_VALUE)
        return displayed, stored

    def _content_for_storage(self, content: str) -> str:
        if self.view_mode != "parameter" or not self._password_hiding_enabled():
            return content
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            self._parameter_password_ciphertext = None
            return content
        value = match.group("value").strip()
        if self._parameter_password_visible:
            plaintext = value
            if (
                self._parameter_password_ciphertext
                and plaintext == self._visible_parameter_password
            ):
                encrypted_value = self._parameter_password_ciphertext
            else:
                encrypted_value = protect_text(plaintext) if plaintext else ""
            self._parameter_password_ciphertext = encrypted_value or None
            self._visible_parameter_password = plaintext
            return self._replace_password_value(content, encrypted_value)
        if value == _HIDDEN_PASSWORD_VALUE:
            return self._replace_password_value(
                content,
                self._parameter_password_ciphertext or "",
            )
        if is_protected(value):
            self._parameter_password_ciphertext = value
            return content
        if value and not self._password_session_unlocked:
            raise PasswordProtectionError(
                "服务器密码当前处于隐藏状态，请先点击“显示密码”后再修改"
            )
        encrypted_value = protect_text(value) if value else ""
        self._parameter_password_ciphertext = encrypted_value or None
        return self._replace_password_value(content, encrypted_value)

    @staticmethod
    def _replace_password_value(content: str, value: str) -> str:
        match = _PASSWORD_LINE_PATTERN.search(content)
        if match is None:
            return content
        return (
            content[: match.start("value")]
            + value
            + content[match.end("value") :]
        )

    def _load_editor_font_size(self) -> int:
        default_size = 10
        try:
            font_size = int(
                self.application_settings.get("editor_font_size", default_size)
            )
        except (ValueError, TypeError):
            return default_size
        return min(24, max(8, font_size))

    def _load_auto_save_delay(self) -> float:
        try:
            delay = float(
                self.application_settings.get("auto_save_delay_seconds", 1)
            )
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(delay):
            return 1.0
        return min(30.0, max(0.5, delay))

    def _write_settings(self, settings: dict[str, object]) -> bool:
        temporary_path = self.settings_path.with_name(
            f".{self.settings_path.name}.tmp"
        )
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.settings_path)
            self.application_settings = settings
            return True
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _save_editor_font_size(self) -> None:
        settings = dict(self.application_settings)
        settings["editor_font_size"] = self.editor_font_size
        self._write_settings(settings)

    def _create_system_settings_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, padding=20)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        ttk.Label(
            page,
            text="系统设置",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 14))

        notebook = ttk.Notebook(page)
        notebook.grid(row=1, column=0, sticky="nsew")

        general_page = ttk.Frame(notebook, padding=16)
        general_page.columnconfigure(1, weight=1)
        notebook.add(general_page, text="常规")
        ttk.Label(general_page, text="启动时打开：").grid(
            row=0,
            column=0,
            sticky="w",
            pady=5,
        )
        self.settings_startup_page_var = tk.StringVar()
        startup_labels = {
            "恢复上次页面": "last",
            "任务页面": "task",
            "配置页面": "parameter",
            "脚本页面": "script",
        }
        self._startup_page_by_label = startup_labels
        startup_selector = ttk.Combobox(
            general_page,
            textvariable=self.settings_startup_page_var,
            values=list(startup_labels),
            state="readonly",
            width=22,
        )
        startup_selector.grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(general_page, text="密码隐藏：").grid(
            row=1,
            column=0,
            sticky="w",
            pady=5,
        )
        self.settings_password_hiding_var = tk.BooleanVar()
        ttk.Checkbutton(
            general_page,
            text="开启服务器密码隐藏",
            variable=self.settings_password_hiding_var,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(general_page, text="管理员密码：").grid(
            row=2,
            column=0,
            sticky="w",
            pady=5,
        )
        ttk.Button(
            general_page,
            text="更改密码",
            command=self._change_administrator_password,
        ).grid(row=2, column=1, sticky="w", pady=5)

        editor_page = ttk.Frame(notebook, padding=16)
        editor_page.columnconfigure(1, weight=1)
        notebook.add(editor_page, text="编辑器与保存")
        ttk.Label(editor_page, text="编辑器字体大小：").grid(
            row=0,
            column=0,
            sticky="w",
            pady=5,
        )
        self.settings_font_size_var = tk.IntVar()
        ttk.Spinbox(
            editor_page,
            from_=8,
            to=24,
            textvariable=self.settings_font_size_var,
            width=10,
        ).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(editor_page, text="自动暂存等待秒数：").grid(
            row=1,
            column=0,
            sticky="w",
            pady=5,
        )
        self.settings_auto_save_delay_var = tk.DoubleVar()
        ttk.Spinbox(
            editor_page,
            from_=0.5,
            to=30,
            increment=0.5,
            textvariable=self.settings_auto_save_delay_var,
            width=10,
        ).grid(row=1, column=1, sticky="w", pady=5)

        ttk.Button(
            page,
            text="保存设置",
            command=self._save_system_settings,
        ).grid(row=2, column=0, sticky="e", pady=(14, 0))
        return page

    def _show_system_settings(self) -> None:
        if self._settings_page_visible:
            return
        if self._deploying:
            messagebox.showwarning("正在部署", "部署完成后才能打开系统设置")
            return
        if not self._require_system_settings_password():
            return
        if not self._confirm_pending_changes():
            return
        if not self._conceal_current_parameter_password():
            return

        startup_page = str(
            self.application_settings.get("startup_page", "last")
        )
        startup_label = next(
            (
                label
                for label, value in self._startup_page_by_label.items()
                if value == startup_page
            ),
            "恢复上次页面",
        )
        self.settings_startup_page_var.set(startup_label)
        self.settings_password_hiding_var.set(self._password_hiding_enabled())
        self.settings_font_size_var.set(self.editor_font_size)
        self.settings_auto_save_delay_var.set(self.auto_save_delay_seconds)

        self.main_panes.forget(self.right_panel)
        self.main_panes.add(self.settings_page, weight=4)
        self.toolbar.grid_remove()
        self._settings_page_visible = True
        self._update_view_controls()
        self.status_var.set("系统设置")

    def _hide_system_settings(self) -> None:
        if not self._settings_page_visible:
            return
        self.main_panes.forget(self.settings_page)
        self.main_panes.add(self.right_panel, weight=4)
        self.toolbar.grid()
        self._settings_page_visible = False

    def _save_system_settings(self) -> None:
        try:
            font_size = int(self.settings_font_size_var.get())
            auto_save_delay = float(self.settings_auto_save_delay_var.get())
        except (tk.TclError, TypeError, ValueError):
            messagebox.showerror("设置无效", "字体大小和自动暂存时间必须填写数字")
            return
        if not 8 <= font_size <= 24:
            messagebox.showerror("设置无效", "编辑器字体大小必须是 8 到 24")
            return
        if not math.isfinite(auto_save_delay) or not 0.5 <= auto_save_delay <= 30:
            messagebox.showerror("设置无效", "自动暂存等待时间必须是 0.5 到 30 秒")
            return

        startup_page = self._startup_page_by_label.get(
            self.settings_startup_page_var.get(),
            "last",
        )
        password_hiding_enabled = self.settings_password_hiding_var.get()
        if password_hiding_enabled != self._password_hiding_enabled():
            changed = (
                self._enable_password_hiding(show_message=False)
                if password_hiding_enabled
                else self._disable_password_hiding(show_message=False)
            )
            if not changed:
                self.settings_password_hiding_var.set(
                    self._password_hiding_enabled()
                )
                return
        settings = dict(self.application_settings)
        settings.update(
            {
                "editor_font_size": font_size,
                "auto_save_delay_seconds": auto_save_delay,
                "startup_page": startup_page,
            }
        )
        if not self._write_settings(settings):
            messagebox.showerror("保存失败", "无法保存系统设置")
            return
        self.editor_font_size = font_size
        self.auto_save_delay_seconds = auto_save_delay
        self.text.configure(font=("Cascadia Mono", self.editor_font_size))
        self.status_var.set("系统设置已保存")
        messagebox.showinfo("保存成功", "系统设置已保存")

    def _remember_window_geometry(self, event: tk.Event) -> None:
        if event.widget is not self.master:
            return
        try:
            if self.master.state() == "normal":
                self._normal_window_geometry = self.master.geometry()
        except tk.TclError:
            pass

    def _restore_window_state(self) -> None:
        geometry = str(self.application_settings.get("window_geometry", "")).strip()
        if re.fullmatch(r"\d+x\d+(?:[+-]\d+){0,2}", geometry):
            self.master.geometry(geometry)

        self.master.update_idletasks()
        sash_position = self.application_settings.get("editor_log_sash")
        try:
            if sash_position is not None:
                self.right_panel.sash_place(0, 0, max(100, int(sash_position)))
        except (tk.TclError, TypeError, ValueError):
            pass

        if self.application_settings.get("window_state") == "zoomed":
            try:
                self.master.state("zoomed")
            except tk.TclError:
                pass

    def _save_application_state(self) -> None:
        settings = dict(self.application_settings)
        try:
            window_state = self.master.state()
        except tk.TclError:
            window_state = "normal"
        settings.update(
            {
                "editor_font_size": self.editor_font_size,
                "window_geometry": self._normal_window_geometry,
                "window_state": "zoomed" if window_state == "zoomed" else "normal",
                "view_mode": self.view_mode,
                "selected_files": dict(self.last_selected_files),
            }
        )
        try:
            settings["editor_log_sash"] = self.right_panel.sash_coord(0)[1]
        except tk.TclError:
            pass
        self._write_settings(settings)

    def _on_editor_zoom(self, event: tk.Event) -> str:
        if event.delta == 0:
            return "break"
        step = 1 if event.delta > 0 else -1
        new_size = min(24, max(8, self.editor_font_size + step))
        if new_size != self.editor_font_size:
            self.editor_font_size = new_size
            self.text.configure(font=("Cascadia Mono", self.editor_font_size))
            self._save_editor_font_size()
            self.status_var.set(f"编辑器字体大小：{self.editor_font_size}")
        return "break"

    def _parameter_reference_from_editor(self) -> str | None:
        match = re.search(
            r"(?mi)^\s*PARAMETER_FILE\s*=\s*(.*?)\s*$",
            self.text.get("1.0", "end-1c"),
        )
        return match.group(1) if match and match.group(1) else None

    def _remove_parameter_selector_button(self) -> None:
        button = self.parameter_selector_button
        self.parameter_selector_button = None
        if button is not None:
            try:
                button.destroy()
            except tk.TclError:
                pass

    def _remove_script_selector_button(self) -> None:
        button = self.script_selector_button
        if button is not None:
            try:
                self.task_editor_toolbar.grid_remove()
            except tk.TclError:
                pass

    def _add_script_selector_button(self, content: str) -> None:
        del content
        if self.view_mode != "task" or self.current_path is None:
            return
        self.task_editor_toolbar.grid()

    def _add_workflow_step(self) -> None:
        """Show a type-specific form and append the next workflow step."""
        task_content = self.text.get("1.0", "end-1c")
        server_step_indexes = set(
            re.findall(
                r"(?mi)^\s*STEP_(\d+)_TYPE\s*=\s*SERVER_PARAMETER\s*$",
                task_content,
            )
        )
        connection_names: list[str] = []
        for index in sorted(server_step_indexes, key=int):
            match = re.search(
                rf"(?mi)^\s*STEP_{re.escape(index)}_CONNECTION_NAME\s*=\s*(.*?)\s*$",
                task_content,
            )
            if match is not None:
                connection_name = match.group(1).strip()
                if connection_name and connection_name not in connection_names:
                    connection_names.append(connection_name)
        build_step_indexes = set(
            re.findall(
                r"(?mi)^\s*STEP_(\d+)_TYPE\s*=\s*BUILD\s*$",
                task_content,
            )
        )
        artifact_names: list[str] = []
        for index in sorted(build_step_indexes, key=int):
            match = re.search(
                rf"(?mi)^\s*STEP_{re.escape(index)}_ARTIFACT_NAME\s*=\s*(.*?)\s*$",
                task_content,
            )
            if match is not None:
                artifact_name = match.group(1).strip()
                if artifact_name and artifact_name not in artifact_names:
                    artifact_names.append(artifact_name)
        parameter_files = sorted(
            [
                path.name
                for path in self.parameter_dir.glob("*.txt")
                if not self._draft_path(path, "parameter").is_file()
            ],
            key=str.lower,
        )
        local_script_files = sorted(
            [
                path.name
                for pattern in ("*.sh", "*.bash", "*.bat", "*.cmd", "*.ps1")
                for path in self.script_dir.glob(pattern)
                if not self._draft_path(path, "script").is_file()
            ],
            key=str.lower,
        )
        remote_script_files = sorted(
            [
                path.name
                for pattern in ("*.sh", "*.bash")
                for path in self.script_dir.glob(pattern)
                if not self._draft_path(path, "script").is_file()
            ],
            key=str.lower,
        )
        dialog = tk.Toplevel(self.master)
        dialog.title("增加流程步骤")
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.minsize(620, 180)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        type_by_label = {definition.label: definition for definition in WORKFLOW_TYPES}
        selected_type = tk.StringVar(value=WORKFLOW_TYPES[0].label)
        ttk.Label(frame, text="步骤类型：").grid(row=0, column=0, sticky="w", pady=3)
        type_selector = ttk.Combobox(
            frame,
            textvariable=selected_type,
            values=[definition.label for definition in WORKFLOW_TYPES],
            state="readonly",
            width=34,
        )
        type_selector.grid(row=0, column=1, sticky="ew", pady=3)
        form = ttk.Frame(frame)
        form.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        form.columnconfigure(1, weight=1)
        field_values: dict[str, tk.StringVar] = {}
        field_controls: dict[str, tk.Widget] = {}

        def resize_dialog() -> None:
            try:
                dialog.update_idletasks()
                width = max(620, dialog.winfo_reqwidth())
                height = dialog.winfo_reqheight()
                x = self.master.winfo_rootx() + max(
                    0, (self.master.winfo_width() - width) // 2
                )
                y = self.master.winfo_rooty() + max(
                    0, (self.master.winfo_height() - height) // 2
                )
                dialog.geometry(f"{width}x{height}+{x}+{y}")
            except tk.TclError:
                pass

        def rebuild_form(*_args: object) -> None:
            for child in form.winfo_children():
                child.destroy()
            field_values.clear()
            field_controls.clear()
            definition = type_by_label[selected_type.get()]
            first_field_row = 0
            if definition.key == "MERGE_BRANCH":
                ttk.Label(
                    form,
                    text=(
                        "先安全拉取目标分支，再从源目录读取当前分支并预检查冲突；"
                        "不会执行 git add，合并成功后自动提交并推送。"
                    ),
                    foreground="#4b5563",
                ).grid(
                    row=0,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    pady=(0, 8),
                )
                first_field_row = 1
            for offset, field in enumerate(definition.fields):
                row = first_field_row + offset
                label = field.label + (" *" if field.required else "")
                ttk.Label(form, text=label).grid(
                    row=row, column=0, sticky="w", padx=(0, 10), pady=4
                )
                if field.selector == "parameter":
                    values = parameter_files
                elif field.selector == "local_script":
                    values = local_script_files
                elif field.selector == "remote_script":
                    values = remote_script_files
                elif field.selector == "connection":
                    values = connection_names
                elif field.selector == "artifact":
                    values = artifact_names
                else:
                    values = []
                initial = field.default
                if values and field.selector in {
                    "parameter", "local_script", "remote_script", "connection", "artifact"
                }:
                    initial = values[0]
                variable = tk.StringVar(value=initial)
                field_values[field.key] = variable
                if field.selector == "upload_source":
                    control = ttk.Frame(form)
                    ttk.Radiobutton(
                        control,
                        text="JAR包",
                        variable=variable,
                        value="ARTIFACT",
                    ).grid(row=0, column=0, padx=(0, 16))
                    ttk.Radiobutton(
                        control,
                        text="单个文件",
                        variable=variable,
                        value="LOCAL_FILE",
                    ).grid(row=0, column=1, padx=(0, 16))
                    ttk.Radiobutton(
                        control,
                        text="上传文件夹",
                        variable=variable,
                        value="LOCAL_FOLDER",
                    ).grid(row=0, column=2)
                elif field.selector == "folder_mode":
                    control = ttk.Frame(form)
                    ttk.Radiobutton(
                        control,
                        text="保留最外层文件夹",
                        variable=variable,
                        value="INCLUDE_FOLDER",
                    ).grid(row=0, column=0, padx=(0, 16))
                    ttk.Radiobutton(
                        control,
                        text="只上传文件夹内部内容",
                        variable=variable,
                        value="CONTENTS_ONLY",
                    ).grid(row=0, column=1)
                elif field.selector == "backup_enabled":
                    control = ttk.Frame(form)
                    ttk.Radiobutton(
                        control,
                        text="不备份",
                        variable=variable,
                        value="NO",
                    ).grid(row=0, column=0, padx=(0, 16))
                    ttk.Radiobutton(
                        control,
                        text="上传前备份",
                        variable=variable,
                        value="YES",
                    ).grid(row=0, column=1)
                elif field.selector == "create_remote_path":
                    control = ttk.Frame(form)
                    ttk.Radiobutton(
                        control,
                        text="否（目录不存在时停止）",
                        variable=variable,
                        value="NO",
                    ).grid(row=0, column=0, padx=(0, 16))
                    ttk.Radiobutton(
                        control,
                        text="是（自动创建目录）",
                        variable=variable,
                        value="YES",
                    ).grid(row=0, column=1)
                elif field.selector in {
                    "parameter", "local_script", "remote_script", "connection",
                    "artifact"
                }:
                    control = ttk.Combobox(
                        form, textvariable=variable, values=values,
                        state="readonly", width=54,
                    )
                else:
                    control = ttk.Entry(form, textvariable=variable, width=56)
                control.grid(row=row, column=1, sticky="ew", pady=4)
                field_controls[field.key] = control

            def update_upload_source_fields(*_event: object) -> None:
                if definition.key != "UPLOAD":
                    return
                source_mode = field_values["SOURCE_MODE"].get()
                artifact_control = field_controls["ARTIFACT_NAME"]
                local_path_control = field_controls["LOCAL_PATH"]
                file_name_control = field_controls["FILE_NAME"]
                folder_path_control = field_controls["FOLDER_PATH"]
                folder_mode_control = field_controls["FOLDER_MODE"]
                artifact_control.configure(
                    state="readonly" if source_mode == "ARTIFACT" else tk.DISABLED
                )
                local_state = tk.NORMAL if source_mode == "LOCAL_FILE" else tk.DISABLED
                local_path_control.configure(state=local_state)
                file_name_control.configure(state=local_state)
                folder_path_control.configure(
                    state=tk.NORMAL if source_mode == "LOCAL_FOLDER" else tk.DISABLED
                )
                folder_mode_state = (
                    tk.NORMAL if source_mode == "LOCAL_FOLDER" else tk.DISABLED
                )
                for child in folder_mode_control.winfo_children():
                    child.configure(state=folder_mode_state)
                backup_enabled = field_values["BACKUP_ENABLED"].get() == "YES"
                if not backup_enabled:
                    field_values["BACKUP_PATH"].set("")
                field_controls["BACKUP_PATH"].configure(
                    state=tk.NORMAL if backup_enabled else tk.DISABLED
                )

            if definition.key == "UPLOAD":
                field_values["SOURCE_MODE"].trace_add(
                    "write", update_upload_source_fields
                )
                field_values["BACKUP_ENABLED"].trace_add(
                    "write", update_upload_source_fields
                )
                update_upload_source_fields()
            dialog.after_idle(resize_dialog)

        selected_type.trace_add("write", rebuild_form)
        rebuild_form()

        def confirm() -> None:
            definition = type_by_label[selected_type.get()]
            content = self.text.get("1.0", "end-1c")
            indexes = [
                int(value)
                for value in re.findall(r"(?mi)^\s*STEP_(\d+)_TYPE\s*=", content)
            ]
            index = max(indexes, default=0) + 1
            missing = [
                field.label
                for field in definition.fields
                if field.required and not field_values[field.key].get().strip()
            ]
            if missing:
                messagebox.showerror("参数不足", "请填写：" + "、".join(missing), parent=dialog)
                return
            if definition.key == "UPLOAD":
                source_mode = field_values["SOURCE_MODE"].get()
                if source_mode == "ARTIFACT" and not field_values[
                    "ARTIFACT_NAME"
                ].get().strip():
                    messagebox.showerror(
                        "参数不足",
                        "请先增加打包步骤，并选择自动识别的打包产物",
                        parent=dialog,
                    )
                    return
                if source_mode == "LOCAL_FILE":
                    manual_missing = [
                        label
                        for key, label in (
                            ("LOCAL_PATH", "本地文件目录"),
                            ("FILE_NAME", "文件名称"),
                        )
                        if not field_values[key].get().strip()
                    ]
                    if manual_missing:
                        messagebox.showerror(
                            "参数不足",
                            "请填写：" + "、".join(manual_missing),
                            parent=dialog,
                        )
                        return
                if source_mode == "LOCAL_FOLDER" and not field_values[
                    "FOLDER_PATH"
                ].get().strip():
                    messagebox.showerror(
                        "参数不足",
                        "请填写：本地文件夹路径",
                        parent=dialog,
                    )
                    return
                if (
                    field_values["BACKUP_ENABLED"].get() == "YES"
                    and not field_values["BACKUP_PATH"].get().strip()
                ):
                    messagebox.showerror(
                        "参数不足",
                        "启用上传前备份后，请填写服务器备份目录",
                        parent=dialog,
                    )
                    return
            lines = [
                f"# 功能：{definition.label}",
                f"STEP_{index}_TYPE={definition.key}",
            ]
            for field in definition.fields:
                if definition.key == "UPLOAD":
                    source_mode = field_values["SOURCE_MODE"].get()
                    if source_mode == "ARTIFACT" and field.key in {
                        "LOCAL_PATH", "FILE_NAME", "FOLDER_PATH", "FOLDER_MODE"
                    }:
                        continue
                    if source_mode == "LOCAL_FILE" and field.key in {
                        "ARTIFACT_NAME", "FOLDER_PATH", "FOLDER_MODE"
                    }:
                        continue
                    if source_mode == "LOCAL_FOLDER" and field.key in {
                        "ARTIFACT_NAME", "LOCAL_PATH", "FILE_NAME"
                    }:
                        continue
                value = field_values[field.key].get().strip()
                if value or field.required:
                    lines.append(f"STEP_{index}_{field.key}={value}")
            prefix = (
                f"{content.rstrip()}\n\n# ----------------------------------------\n"
                if content.strip()
                else ""
            )
            self._set_editor_content(
                prefix + "\n".join(lines) + "\n", preserve_view=True
            )
            self._dirty = True
            self._write_current_draft()
            self.status_var.set(
                f"已暂存第 {index} 步：{definition.label}，点击保存后生效"
            )
            dialog.destroy()
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="确定", command=confirm).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="取消", command=dialog.destroy).grid(row=0, column=1)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        resize_dialog()
        type_selector.focus_set()

    def _add_parameter_selector_button(self, content: str) -> None:
        if self.view_mode != "task":
            return
        for line_number, line in enumerate(content.splitlines(), start=1):
            if re.match(r"^\s*PARAMETER_FILE\s*=", line, flags=re.IGNORECASE):
                self.parameter_selector_button = ttk.Button(
                    self.text,
                    text="选择配置",
                    width=8,
                    command=self._choose_task_parameter,
                )
                self.text.window_create(
                    f"{line_number}.end",
                    window=self.parameter_selector_button,
                    padx=8,
                )
                break

    def _choose_task_parameter(self) -> None:
        parameter_files = sorted(
            (
                path
                for path in self.parameter_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() == ".txt"
                and not self._draft_path(path, "parameter").is_file()
            ),
            key=lambda path: path.name.lower(),
        )
        if not parameter_files:
            messagebox.showwarning(
                "没有可选配置",
                "请先在“配置”列表中新建并保存服务器配置",
            )
            return

        dialog = tk.Toplevel(self.master)
        dialog.title("选择服务器配置")
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        content_frame = ttk.Frame(dialog, padding=12)
        content_frame.grid(sticky="nsew")
        ttk.Label(content_frame, text="选择本任务需要连接的服务器配置：").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        parameter_list = tk.Listbox(
            content_frame,
            width=40,
            height=min(10, max(4, len(parameter_files))),
            font=("Microsoft YaHei UI", 10),
            selectmode=tk.SINGLE,
            activestyle="none",
            relief=tk.SOLID,
            borderwidth=1,
            selectbackground="#dbeafe",
            selectforeground="#111827",
        )
        parameter_list.grid(row=1, column=0, sticky="nsew")

        current_parameter = self._parameter_reference_from_editor()
        for index, parameter_path in enumerate(parameter_files):
            parameter_list.insert(tk.END, parameter_path.name)
            if current_parameter == parameter_path.name:
                parameter_list.selection_set(index)
                parameter_list.see(index)
        if not parameter_list.curselection():
            parameter_list.selection_set(0)

        button_row = ttk.Frame(content_frame)
        button_row.grid(row=2, column=0, sticky="e", pady=(12, 0))

        def confirm_selection() -> None:
            selection = parameter_list.curselection()
            if not selection:
                return
            parameter_name = parameter_files[selection[0]].name
            editor_content = self.text.get("1.0", "end-1c")
            updated_content, replacements = re.subn(
                r"(?mi)^\s*PARAMETER_FILE\s*=.*$",
                f"PARAMETER_FILE={parameter_name}",
                editor_content,
                count=1,
            )
            if replacements == 0:
                updated_content = f"PARAMETER_FILE={parameter_name}\n\n{editor_content}"
            self._set_editor_content(updated_content, preserve_view=True)
            self._dirty = True
            self._write_current_draft()
            self.status_var.set(
                f"已暂存服务器配置：{parameter_name}，点击保存后生效"
            )
            dialog.destroy()

        ttk.Button(button_row, text="确定", command=confirm_selection).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(button_row, text="取消", command=dialog.destroy).grid(
            row=0, column=1
        )
        parameter_list.bind(
            "<Double-Button-1>", lambda _event: confirm_selection()
        )
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.master.winfo_rootx() + max(0, (self.master.winfo_width() - width) // 2)
        y = self.master.winfo_rooty() + max(0, (self.master.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        parameter_list.focus_set()

    def _choose_task_script(self) -> None:
        script_files = sorted(
            (
                path
                for path in self.script_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".sh", ".bash"}
                and not self._draft_path(path, "script").is_file()
            ),
            key=lambda path: path.name.lower(),
        )
        dialog = tk.Toplevel(self.master)
        dialog.title("增加执行步骤")
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        content_frame = ttk.Frame(dialog, padding=12)
        content_frame.grid(sticky="nsew")
        content_frame.columnconfigure(1, weight=1)

        step_type = tk.StringVar(value="script" if script_files else "command")
        ttk.Label(content_frame, text="步骤类型：").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        type_frame = ttk.Frame(content_frame)
        type_frame.grid(row=0, column=1, sticky="w", pady=(0, 8))
        ttk.Radiobutton(
            type_frame, text="脚本", value="script", variable=step_type
        ).grid(row=0, column=0, padx=(0, 12))
        ttk.Radiobutton(
            type_frame, text="命令", value="command", variable=step_type
        ).grid(row=0, column=1)

        ttk.Label(content_frame, text="选择脚本：").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4
        )
        script_name = tk.StringVar(value=script_files[0].name if script_files else "")
        script_selector = ttk.Combobox(
            content_frame,
            textvariable=script_name,
            values=[path.name for path in script_files],
            state="readonly",
            width=36,
        )
        script_selector.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(content_frame, text="执行命令：").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        command_value = tk.StringVar()
        command_entry = ttk.Entry(
            content_frame,
            textvariable=command_value,
            width=38,
        )
        command_entry.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(
            content_frame,
            text="执行路径不是必填项；留空时使用服务器登录目录，不读取配置中的默认路径。",
            foreground="#6b7280",
            wraplength=420,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        ttk.Label(content_frame, text="路径方式：").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        path_mode = tk.StringVar(value="current")
        path_mode_frame = ttk.Frame(content_frame)
        path_mode_frame.grid(row=4, column=1, sticky="w", pady=4)
        ttk.Radiobutton(
            path_mode_frame,
            text="使用当前目录（默认）",
            value="current",
            variable=path_mode,
        ).grid(row=0, column=0, padx=(0, 12))
        ttk.Radiobutton(
            path_mode_frame,
            text="指定其他路径",
            value="custom",
            variable=path_mode,
        ).grid(row=0, column=1)

        ttk.Label(content_frame, text="指定路径：").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=4
        )
        working_directory = tk.StringVar()
        working_directory_entry = ttk.Entry(
            content_frame,
            textvariable=working_directory,
            width=38,
        )
        working_directory_entry.grid(row=5, column=1, sticky="ew", pady=4)

        ttk.Label(content_frame, text="完成后等待：").grid(
            row=6, column=0, sticky="w", padx=(0, 10), pady=4
        )
        delay_frame = ttk.Frame(content_frame)
        delay_frame.grid(row=6, column=1, sticky="w", pady=4)
        delay_value = tk.StringVar(value="0")
        ttk.Entry(delay_frame, textvariable=delay_value, width=10).grid(
            row=0, column=0
        )
        ttk.Label(delay_frame, text="秒后执行下一步").grid(
            row=0, column=1, padx=(6, 0)
        )

        def refresh_step_fields(*_args: object) -> None:
            is_script = step_type.get() == "script"
            script_selector.configure(
                state="readonly" if is_script and script_files else tk.DISABLED
            )
            command_entry.configure(state=tk.DISABLED if is_script else tk.NORMAL)
            working_directory_entry.configure(
                state=tk.NORMAL if path_mode.get() == "custom" else tk.DISABLED
            )

        step_type.trace_add("write", refresh_step_fields)
        path_mode.trace_add("write", refresh_step_fields)
        refresh_step_fields()

        button_row = ttk.Frame(content_frame)
        button_row.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def confirm_selection() -> None:
            try:
                delay = float(delay_value.get().strip() or "0")
            except ValueError:
                messagebox.showerror("参数错误", "等待秒数必须是数字", parent=dialog)
                return
            if not math.isfinite(delay) or delay < 0:
                messagebox.showerror(
                    "参数错误",
                    "等待秒数必须是大于或等于 0 的有限数字",
                    parent=dialog,
                )
                return
            execution_path = (
                working_directory.get().strip()
                if path_mode.get() == "custom"
                else ""
            )
            if path_mode.get() == "custom" and not execution_path:
                messagebox.showerror("未填写路径", "请输入指定的执行路径", parent=dialog)
                return

            editor_content = self.text.get("1.0", "end-1c")
            indexes = [
                int(index)
                for index in re.findall(
                    r"(?mi)^\s*RESTART_(?:COMMAND|LOCAL_SCRIPT)_(\d+)\s*=",
                    editor_content,
                )
            ]
            next_index = max(indexes, default=0) + 1

            if step_type.get() == "script":
                selected_script = script_name.get().strip()
                if not selected_script:
                    messagebox.showerror(
                        "未选择脚本",
                        "请先在“脚本”列表中新建脚本",
                        parent=dialog,
                    )
                    return
                step_line = f"RESTART_LOCAL_SCRIPT_{next_index}={selected_script}"
                step_description = selected_script
            else:
                command = command_value.get().strip()
                if not command:
                    messagebox.showerror("未填写命令", "请输入要执行的命令", parent=dialog)
                    return
                step_line = f"RESTART_COMMAND_{next_index}={command}"
                step_description = command

            step_block = (
                f"{step_line}\n"
                f"RESTART_PATH_{next_index}={execution_path}\n"
                f"RESTART_DELAY_{next_index}={delay_value.get().strip() or '0'}"
            )
            trailing_settings_section = re.search(
                r"(?m)^# (?:可选：部署失败并恢复上传目录中的旧 JAR 后执行|"
                r"重启后在服务器上循环执行的健康检查命令)",
                editor_content,
            )
            if trailing_settings_section:
                before = editor_content[: trailing_settings_section.start()].rstrip()
                after = editor_content[trailing_settings_section.start() :].lstrip()
                updated_content = f"{before}\n\n{step_block}\n\n{after}"
            else:
                updated_content = f"{editor_content.rstrip()}\n\n{step_block}\n"
            self._set_editor_content(updated_content, preserve_view=True)
            self._dirty = True
            self._write_current_draft()
            self.status_var.set(
                f"已暂存第 {next_index} 个执行步骤：{step_description}，"
                "点击保存后生效"
            )
            dialog.destroy()

        ttk.Button(button_row, text="确定", command=confirm_selection).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(button_row, text="取消", command=dialog.destroy).grid(
            row=0, column=1
        )
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.master.winfo_rootx() + max(0, (self.master.winfo_width() - width) // 2)
        y = self.master.winfo_rooty() + max(0, (self.master.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        if step_type.get() == "script":
            script_selector.focus_set()
        else:
            command_entry.focus_set()

    def on_tree_select(self, _event: object = None) -> None:
        if self._changing_selection or self._settings_page_visible:
            return
        selected_path = self._selected_path()
        if selected_path is None or selected_path == self.current_path:
            return

        previous_path = self.current_path
        if not self._confirm_pending_changes():
            if previous_path is not None:
                self._select_path(previous_path)
            else:
                self._clear_tree_selection()
            return
        if not self._conceal_current_parameter_password():
            if previous_path is not None:
                self._select_path(previous_path)
            else:
                self._clear_tree_selection()
            return

        if not self._load_file(selected_path):
            if previous_path is not None:
                self._select_path(previous_path)
            else:
                self._clear_tree_selection()

    def _choose_script_extension(self, title: str) -> str | None:
        script_types = {
            "Linux / Bash 脚本（.sh，常用）": ".sh",
            "Linux / Bash 脚本（.bash）": ".bash",
            "Windows 批处理脚本（.bat，常用）": ".bat",
            "Windows 命令脚本（.cmd）": ".cmd",
            "PowerShell 脚本（.ps1）": ".ps1",
        }
        dialog = tk.Toplevel(self.master)
        dialog.title(title)
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="请选择脚本类型：").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        selected_type = tk.StringVar(value=next(iter(script_types)))
        selector = ttk.Combobox(
            frame,
            textvariable=selected_type,
            values=list(script_types),
            state="readonly",
            width=34,
        )
        selector.grid(row=1, column=0, sticky="ew")

        result: dict[str, str | None] = {"extension": None}

        def confirm() -> None:
            result["extension"] = script_types[selected_type.get()]
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="确定", command=confirm).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(buttons, text="取消", command=dialog.destroy).grid(row=0, column=1)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.master.winfo_rootx() + max(0, (self.master.winfo_width() - width) // 2)
        y = self.master.winfo_rooty() + max(0, (self.master.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        selector.focus_set()
        self.master.wait_window(dialog)
        return result["extension"]

    def create_file(self) -> None:
        label = self._view_label()
        script_extension = None
        if self.view_mode == "script":
            script_extension = self._choose_script_extension("选择脚本类型")
            if script_extension is None:
                return
        name = simpledialog.askstring(
            f"新建{label}",
            (
                f"请输入{label}名称（自动保存为 {script_extension}）："
                if script_extension
                else f"请输入{label}名称："
            ),
        )
        file_name = self._validate_file_name(name, script_extension)
        if file_name is None:
            return

        path = self.dir_path / file_name
        if path.exists():
            messagebox.showerror("无法新建", f"文件已存在：{file_name}")
            return
        if self.view_mode == "task":
            template_content = ""
        elif self.view_mode == "script" and path.suffix.lower() in {".bat", ".cmd"}:
            template_content = "@echo off\nsetlocal\n\n"
        elif self.view_mode == "script" and path.suffix.lower() == ".ps1":
            template_content = (
                "Set-StrictMode -Version Latest\n"
                "$ErrorActionPreference = \"Stop\"\n\n"
            )
        else:
            template_path = (
                self.parameter_template_path
                if self.view_mode == "parameter"
                else self.script_template_path
            )
            try:
                template_content = template_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                messagebox.showerror(
                    "无法读取模板",
                    f"模板：{template_path}\n\n{exc}",
                )
                return
        if not self._confirm_pending_changes():
            return
        if not self._conceal_current_parameter_password():
            return

        try:
            path.write_text(template_content, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("无法新建", str(exc))
            return

        self.update_tree(path)
        self._load_file(path)

    def _task_reference_pattern(self, reference_type: str) -> re.Pattern[str]:
        if reference_type == "parameter":
            key_pattern = r"(?:PARAMETER_FILE|STEP_\d+_PARAMETER_FILE)"
        else:
            key_pattern = (
                r"(?:SCRIPT_FILE|RESTART_LOCAL_SCRIPT_\d+|"
                r"STEP_\d+_SCRIPT_FILE)"
            )
        return re.compile(
            rf"^(?P<prefix>[ \t]*{key_pattern}[ \t]*=[ \t]*)"
            r"(?P<value>[^\r\n]*?)(?P<suffix>[ \t]*)$",
            re.IGNORECASE | re.MULTILINE,
        )

    def _resolve_task_reference(
        self,
        task_path: Path,
        reference_type: str,
        value: str,
    ) -> Path:
        reference = Path(os.path.expandvars(value)).expanduser()
        if reference.is_absolute():
            return reference.resolve()
        base_directory = (
            self.parameter_dir
            if reference_type == "parameter"
            else self.script_dir
        )
        return (base_directory / reference).resolve()

    def _replacement_reference_value(
        self,
        original_value: str,
        replacement_path: Path,
    ) -> str:
        original_path = Path(original_value)
        if original_path.is_absolute():
            return str(replacement_path)
        if original_path.parent == Path("."):
            return replacement_path.name
        return str(original_path.with_name(replacement_path.name))

    def _collect_task_reference_updates(
        self,
        reference_type: str,
        referenced_path: Path,
        replacement_path: Path | None = None,
    ) -> tuple[list[Path], list[tuple[Path, str, str]]]:
        referenced_path = referenced_path.resolve()
        pattern = self._task_reference_pattern(reference_type)
        referencing_tasks: dict[str, Path] = {}
        updates: list[tuple[Path, str, str]] = []
        task_draft_directory = self.draft_root / "task"
        task_sources = list(self.task_dir.glob("*.txt"))
        if task_draft_directory.is_dir():
            task_sources.extend(task_draft_directory.glob("*.draft"))
        for task_path in sorted(task_sources, key=lambda path: str(path).lower()):
            content = task_path.read_text(encoding="utf-8-sig")
            task_references_file = False

            def replace_reference(match: re.Match[str]) -> str:
                nonlocal task_references_file
                value = match.group("value").strip()
                if not value:
                    return match.group(0)
                try:
                    resolved_reference = self._resolve_task_reference(
                        task_path,
                        reference_type,
                        value,
                    )
                except (OSError, RuntimeError, ValueError):
                    return match.group(0)
                if resolved_reference != referenced_path:
                    return match.group(0)
                task_references_file = True
                if replacement_path is None:
                    return match.group(0)
                replacement_value = self._replacement_reference_value(
                    value,
                    replacement_path,
                )
                return (
                    f"{match.group('prefix')}{replacement_value}"
                    f"{match.group('suffix')}"
                )

            updated_content = pattern.sub(replace_reference, content)
            if task_references_file:
                display_name = self._task_reference_display_name(task_path)
                referencing_tasks.setdefault(display_name, task_path)
                if updated_content != content:
                    updates.append((task_path, content, updated_content))
        return list(referencing_tasks.values()), updates

    def _task_reference_display_name(self, path: Path) -> str:
        task_draft_directory = (self.draft_root / "task").resolve()
        if path.parent.resolve() != task_draft_directory:
            return path.name
        if path.name == "__untitled__.draft":
            return "未命名任务（暂存）"
        name = path.name[:-len(".draft")] if path.name.endswith(".draft") else path.name
        return f"{name}（暂存）"

    def _referencing_tasks_message(self, referencing_tasks: list[Path]) -> str:
        visible_names = [
            self._task_reference_display_name(path)
            for path in referencing_tasks[:10]
        ]
        message = "\n".join(f"• {name}" for name in visible_names)
        remaining = len(referencing_tasks) - len(visible_names)
        if remaining > 0:
            message += f"\n• 另外 {remaining} 个任务"
        return message

    def _rename_with_task_reference_updates(
        self,
        path: Path,
        target: Path,
        updates: list[tuple[Path, str, str]],
    ) -> bool:
        attempted_updates: list[tuple[Path, str]] = []
        renamed = False
        try:
            path.rename(target)
            renamed = True
            for task_path, original_content, updated_content in updates:
                attempted_updates.append((task_path, original_content))
                task_path.write_text(updated_content, encoding="utf-8")
        except OSError as exc:
            rollback_errors: list[str] = []
            for task_path, original_content in reversed(attempted_updates):
                try:
                    task_path.write_text(original_content, encoding="utf-8")
                except OSError as rollback_exc:
                    rollback_errors.append(f"{task_path.name}: {rollback_exc}")
            if renamed and target.exists() and not path.exists():
                try:
                    target.rename(path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"恢复文件名失败：{rollback_exc}")
            rollback_message = (
                "\n\n回滚时仍有错误：\n" + "\n".join(rollback_errors)
                if rollback_errors
                else ""
            )
            messagebox.showerror(
                "无法重命名",
                f"重命名或更新任务引用失败：{exc}{rollback_message}",
            )
            return False
        return True

    def rename_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return

        name = simpledialog.askstring(
            f"重命名{self._view_label()}",
            f"请输入新的{self._view_label()}名称：",
            initialvalue=path.stem,
        )
        file_name = self._validate_file_name(
            name,
            path.suffix.lower() if self.view_mode == "script" else None,
        )
        if file_name is None:
            return

        target = path.with_name(file_name)
        if target.exists() and target != path:
            messagebox.showerror("无法重命名", f"文件已存在：{file_name}")
            return
        if not self._confirm_pending_changes():
            return

        updates: list[tuple[Path, str, str]] = []
        referencing_tasks: list[Path] = []
        if self.view_mode in {"parameter", "script"}:
            try:
                referencing_tasks, updates = self._collect_task_reference_updates(
                    self.view_mode,
                    path,
                    target,
                )
            except (OSError, UnicodeDecodeError) as exc:
                messagebox.showerror(
                    "无法检查任务引用",
                    f"读取任务文件失败，已取消重命名：{exc}",
                )
                return
        if not self._rename_with_task_reference_updates(path, target, updates):
            return

        if self.view_mode == "parameter":
            self._close_parameter_ssh_tab(path)

        self.update_tree(target)
        self._load_file(target)
        reference_hint = (
            f"，并更新 {len(referencing_tasks)} 个任务引用"
            if referencing_tasks
            else ""
        )
        self.status_var.set(f"已重命名为 {target.name}{reference_hint}")

    def copy_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return

        name = simpledialog.askstring(
            f"复制{self._view_label()}",
            "请输入副本名称：",
            initialvalue=f"{path.stem}_copy",
        )
        file_name = self._validate_file_name(
            name,
            path.suffix.lower() if self.view_mode == "script" else None,
        )
        if file_name is None:
            return

        target = path.with_name(file_name)
        if target.exists():
            messagebox.showerror("无法复制", f"文件已存在：{file_name}")
            return
        if not self._confirm_pending_changes():
            return

        try:
            shutil.copy2(path, target)
        except OSError as exc:
            messagebox.showerror("无法复制", str(exc))
            return

        self.update_tree(target)
        self._load_file(target)

    def delete_file(self) -> None:
        path = self._require_current_path()
        if path is None:
            return
        if self.view_mode in {"parameter", "script"}:
            try:
                referencing_tasks, _updates = self._collect_task_reference_updates(
                    self.view_mode,
                    path,
                )
            except (OSError, UnicodeDecodeError) as exc:
                messagebox.showerror(
                    "无法检查任务引用",
                    f"读取任务文件失败，已取消删除：{exc}",
                )
                return
            if referencing_tasks:
                messagebox.showerror(
                    "无法删除",
                    f"以下任务仍引用 {path.name}，请先修改或删除这些任务中的引用：\n\n"
                    f"{self._referencing_tasks_message(referencing_tasks)}",
                )
                return
        if not messagebox.askyesno("确认删除", f"确定删除 {path.name} 吗？"):
            return

        try:
            path.unlink()
        except OSError as exc:
            messagebox.showerror("无法删除", str(exc))
            return

        self._delete_draft(path)
        if self.view_mode == "parameter":
            self._close_parameter_ssh_tab(path)
        self.current_path = None
        self.last_selected_files.pop(self.view_mode, None)
        self._set_editor_content("")
        self.update_tree()
        self._update_view_controls()
        self.status_var.set(f"{self._view_label()}已删除")

    def save_text(self, show_message: bool = True) -> bool:
        path = self.current_path
        previous_draft_path = self._draft_path(path)
        if path is None:
            label = self._view_label()
            script_extension = None
            if self.view_mode == "script":
                script_extension = self._choose_script_extension("选择脚本类型")
                if script_extension is None:
                    return False
            name = simpledialog.askstring(
                f"保存新{label}",
                (
                    f"当前没有{label}文件，请输入名称（自动保存为 "
                    f"{script_extension}）："
                    if script_extension
                    else f"当前没有{label}文件，请输入新{label}名称："
                ),
                parent=self.master,
            )
            file_name = self._validate_file_name(name, script_extension)
            if file_name is None:
                return False
            candidate = self.dir_path / file_name
            if candidate.exists():
                messagebox.showerror("无法保存", f"文件已存在：{file_name}")
                return False
            path = candidate.resolve()

        was_dirty = self._dirty
        try:
            content = self._content_for_storage(self.text.get("1.0", "end-1c"))
            path.write_text(content, encoding="utf-8")
        except (OSError, PasswordProtectionError) as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

        self._delete_draft_path(previous_draft_path)
        self._delete_draft(path)
        if self.current_path is None:
            self.current_path = path
            self.last_selected_files[self.view_mode] = path.name
            self.update_tree(path)
            self._add_script_selector_button(self.text.get("1.0", "end-1c"))
            self._update_view_controls()
        if was_dirty and self.view_mode == "parameter":
            self._close_parameter_ssh_tab(path)
        self._dirty = False
        self._cancel_auto_save()
        self.text.edit_modified(False)
        self.status_var.set(f"已保存 {path.name}")
        if show_message:
            messagebox.showinfo("保存成功", f"已保存文件：{path.name}")
        return True

    def _save_shortcut(self, _event: object = None) -> str:
        self.save_text(show_message=True)
        return "break"

    def develop_method(self) -> None:
        if self._deploying:
            self._request_stop_execution()
            return
        if self.view_mode != "task":
            messagebox.showwarning("无法执行", "请先切换到任务列表并选择任务")
            return
        path = self._require_current_path()
        if path is None:
            return
        task_draft_path = self._draft_path(path, "task")
        if self._dirty or task_draft_path.is_file():
            messagebox.showwarning(
                "任务尚未保存",
                "当前任务只有暂存内容，请先点击保存后再执行",
            )
            return

        try:
            content = path.read_text(encoding="utf-8-sig")
            uses_legacy_task_format = bool(
                re.search(r"(?mi)^\s*(?:PARAMETER_FILE|PROJECT_PATH)\s*=", content)
            )
            workflow_task = (
                load_workflow_task(path)
                if is_workflow_task(path) or not uses_legacy_task_format
                else None
            )
        except (ConfigurationError, OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("任务配置错误", str(exc))
            return
        if workflow_task is not None:
            self._start_workflow_execution(workflow_task)
            return

        try:
            config = load_config(path)
        except ConfigurationError as exc:
            messagebox.showerror("任务配置错误", str(exc))
            return

        source_info: SourceBranchInfo | None = None
        should_merge_and_push = False
        if config.git_integration_enabled:
            try:
                source_info = GitIntegrator().inspect_source(config)
            except GitOperationError as exc:
                messagebox.showerror("Git 配置错误", str(exc))
                return

            uncommitted_note = (
                "\n\n注意：IDEA 项目存在未提交修改，这些修改不会被合并。"
                if source_info.has_uncommitted_changes
                else ""
            )
            should_merge_and_push = messagebox.askyesno(
                "是否合并并推送",
                f"IDEA 当前分支：{source_info.branch}\n"
                f"当前提交：{source_info.commit[:12]}\n"
                f"目标分支：{config.target_branch}\n"
                f"打包目录：{config.project_path}\n\n"
                "选择“是”：合并当前分支到目标分支并推送，成功后继续部署。\n"
                "选择“否”：跳过 Git 操作，直接使用打包目录中的现有代码部署。"
                f"{uncommitted_note}",
            )
        elif not messagebox.askyesno(
            "确认部署",
            f"任务：{config.name}\n"
            f"目标：{config.target}\n\n"
            "未配置 IDEA_PROJECT_PATH 和 TARGET_BRANCH，将直接打包部署。\n"
            "确定继续吗？",
        ):
            return

        self._show_interaction_panel(force=True)
        self.terminal_notebook.select(self.deployment_output_frame)
        self._clear_log()
        self.progress_var.set(0)
        self.progress_text_var.set("0%")
        self._append_log(f"部署任务：{config.name}")
        self._append_log(f"目标服务器：{config.target}")
        if source_info is not None:
            self._append_log(f"IDEA 当前分支：{source_info.branch}")
            self._append_log(f"确认的源提交：{source_info.commit[:12]}")
            self._append_log(f"目标分支：{config.target_branch}")
            self._append_log(
                "本次执行合并并推送"
                if should_merge_and_push
                else "本次跳过合并和推送"
            )
        self._deploying = True
        self._execution_cancel_event.clear()
        self._stop_requested = False
        self._update_view_controls()
        self.status_var.set("正在部署……")
        threading.Thread(
            target=self._deployment_worker,
            args=(config, source_info, should_merge_and_push),
            daemon=True,
            name="deployment-worker",
        ).start()

    def _request_stop_execution(self) -> None:
        if not self._deploying or self._stop_requested:
            return
        if not messagebox.askyesno(
            "确认停止任务",
            "确定要停止当前任务吗？\n\n"
            "正在上传的未完成临时文件会被清理；已经上传完成的内容不会回滚。",
        ):
            return
        self._stop_requested = True
        self._execution_cancel_event.set()
        self.status_var.set("正在停止任务……")
        self._append_log("用户请求停止任务，正在结束当前操作……")
        self._update_view_controls()

    def _start_workflow_execution(
        self,
        task: WorkflowTask,
    ) -> None:
        self._show_interaction_panel(force=True)
        self.terminal_notebook.select(self.deployment_output_frame)
        self._clear_log()
        self.progress_var.set(0)
        self.progress_text_var.set("0%")
        self._append_log(f"执行任务：{task.name}")
        self._append_log(f"共 {len(task.steps)} 个步骤，将按配置顺序执行")
        self._deploying = True
        self._execution_cancel_event.clear()
        self._stop_requested = False
        self._update_view_controls()
        self.status_var.set("正在执行……")
        threading.Thread(
            target=self._workflow_worker,
            args=(task,),
            daemon=True,
            name="workflow-worker",
        ).start()

    def _workflow_worker(
        self,
        task: WorkflowTask,
    ) -> None:
        try:
            WorkflowExecutor(
                self.parameter_dir,
                self.script_dir,
                cancel_event=self._execution_cancel_event,
            ).execute(
                task,
                self._queue_log,
                self._queue_status,
                self._queue_progress,
            )
        except Exception as exc:
            if self._execution_cancel_event.is_set():
                self._events.put(("workflow_cancelled", str(exc)))
            else:
                self._events.put(("workflow_error", str(exc)))
        else:
            self._events.put(("workflow_success", task.name))

    def _deployment_worker(
        self,
        config: DeploymentConfig,
        source_info: SourceBranchInfo | None,
        should_merge_and_push: bool,
    ) -> None:
        try:
            if self._execution_cancel_event.is_set():
                raise RuntimeError("任务已由用户停止")
            if should_merge_and_push:
                if source_info is None:
                    raise GitOperationError("无法获取 IDEA 当前分支")
                self._events.put(("status", "正在合并并推送代码……"))
                GitIntegrator(
                    cancel_event=self._execution_cancel_event,
                ).merge_and_push(config, source_info, self._queue_log)
                if self._execution_cancel_event.is_set():
                    raise RuntimeError("任务已由用户停止")
            else:
                self._queue_log("跳过 Git 合并和推送")

            self._events.put(("status", "正在打包……"))
            if self._execution_cancel_event.is_set():
                raise RuntimeError("任务已由用户停止")
            artifact = MavenBuilder().build(
                config,
                self._queue_log,
                cancel_event=self._execution_cancel_event,
            )
            self._events.put(("status", "正在连接服务器……"))
            health_verified = FabricDeployer(
                cancel_event=self._execution_cancel_event,
            ).deploy(
                config,
                artifact,
                self._queue_progress,
                self._queue_status,
                self._queue_log,
            )
        except Exception as exc:
            if self._execution_cancel_event.is_set():
                self._events.put(("deployment_cancelled", str(exc)))
            else:
                self._events.put(("error", str(exc)))
        else:
            self._events.put(("success", (config.target, health_verified)))

    def _queue_log(self, message: str) -> None:
        self._events.put(("log", message))

    def _queue_status(self, message: str) -> None:
        self._events.put(("status", message))

    def _queue_progress(self, transferred: int, total: int) -> None:
        self._events.put(("progress", (transferred, total)))

    def _poll_events(self) -> None:
        started_at = time.monotonic()
        processed = 0
        while processed < 512 and time.monotonic() - started_at < 0.015:
            try:
                event_type, payload = self._events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if event_type == "log":
                self._append_log(str(payload))
            elif event_type == "status":
                if not self._stop_requested:
                    self.status_var.set(str(payload))
            elif event_type == "progress":
                transferred, total = payload
                percent = 100 if total <= 0 else min(100, transferred * 100 / total)
                self.progress_var.set(percent)
                self.progress_text_var.set(f"{percent:.0f}%")
                if not self._stop_requested:
                    self.status_var.set("正在上传……")
            elif event_type == "success":
                target, health_verified = payload
                self._finish_deployment()
                self._append_log("部署完成")
                if health_verified:
                    messagebox.showinfo(
                        "启动成功",
                        f"项目已成功启动\n目标服务器：{target}",
                    )
                else:
                    messagebox.showwarning(
                        "部署完成",
                        "JAR 已上传且重启命令执行成功，但未配置健康检查，"
                        "无法确认项目是否真正启动成功。",
                    )
            elif event_type == "workflow_success":
                self._finish_deployment()
                self.progress_var.set(100)
                self.progress_text_var.set("100%")
                self._append_log("任务执行完成")
                messagebox.showinfo("执行完成", f"任务“{payload}”已执行完成")
            elif event_type == "workflow_error":
                self._finish_deployment()
                self._append_log(f"执行失败：{payload}")
                messagebox.showerror("执行失败", str(payload))
            elif event_type == "workflow_cancelled":
                self._finish_deployment()
                self._append_log("任务已停止")
                messagebox.showinfo("任务已停止", "当前任务已停止")
            elif event_type == "error":
                self._finish_deployment()
                self._append_log(f"部署失败：{payload}")
                messagebox.showerror("部署失败", str(payload))
            elif event_type == "deployment_cancelled":
                self._finish_deployment()
                self._append_log("部署已停止")
                messagebox.showinfo("部署已停止", "当前部署已停止")
        self.after(1 if not self._events.empty() else 100, self._poll_events)

    def _finish_deployment(self) -> None:
        self._deploying = False
        self._stop_requested = False
        self._execution_cancel_event.clear()
        self._sync_interaction_panel_visibility()
        self._update_view_controls()
        self.status_var.set("就绪")

    def _draft_path(
        self,
        path: Path | None,
        view_mode: str | None = None,
    ) -> Path:
        mode = view_mode or self.view_mode
        file_name = path.name if path is not None else "__untitled__"
        return self.draft_root / mode / f"{file_name}.draft"

    @staticmethod
    def _delete_draft_path(draft_path: Path) -> None:
        try:
            draft_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _delete_draft(self, path: Path | None) -> None:
        self._delete_draft_path(self._draft_path(path))

    def _write_current_draft(self) -> bool:
        draft_path = self._draft_path(self.current_path)
        temporary_path = draft_path.with_name(f".{draft_path.name}.tmp")
        try:
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            content = self._content_for_storage(self.text.get("1.0", "end-1c"))
            temporary_path.write_text(
                content,
                encoding="utf-8",
            )
            temporary_path.replace(draft_path)
        except (OSError, PasswordProtectionError) as exc:
            self.status_var.set(f"暂存失败：{exc}")
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        display_name = self.current_path.name if self.current_path else "未命名内容"
        self.status_var.set(f"已暂存：{display_name}，点击保存后生效")
        return True

    def _restore_untitled_draft(self) -> None:
        draft_path = self._draft_path(None)
        if not draft_path.is_file():
            return
        try:
            content = draft_path.read_text(encoding="utf-8-sig")
            displayed_content, stored_content = (
                self._prepare_parameter_content_for_display(content)
            )
            if stored_content != content:
                draft_path.write_text(stored_content, encoding="utf-8")
            content = displayed_content
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            messagebox.showerror("读取暂存失败", str(exc))
            return
        self.current_path = None
        self._set_editor_content(content)
        self._dirty = True
        self.status_var.set("已恢复未命名暂存内容，点击保存后生效")

    def _load_file(self, path: Path) -> bool:
        draft_path = self._draft_path(path)
        content_path = draft_path if draft_path.is_file() else path
        try:
            content = content_path.read_text(encoding="utf-8-sig")
            displayed_content, stored_content = (
                self._prepare_parameter_content_for_display(content)
            )
            if stored_content != content:
                content_path.write_text(stored_content, encoding="utf-8")
            content = displayed_content
        except (OSError, UnicodeDecodeError, PasswordProtectionError) as exc:
            messagebox.showerror("读取失败", str(exc))
            return False
        self.current_path = path.resolve()
        self.last_selected_files[self.view_mode] = path.name
        self._set_editor_content(content)
        if content_path == draft_path:
            self._dirty = True
            self.status_var.set(f"已恢复 {path.name} 的暂存内容，点击保存后生效")
        else:
            self.status_var.set(f"已加载 {path.name}")
        self._update_view_controls()
        return True

    def _set_editor_content(self, content: str, preserve_view: bool = False) -> None:
        self._cancel_auto_save()
        vertical_position = self.text.yview()[0] if preserve_view else 0.0
        horizontal_position = self.text.xview()[0] if preserve_view else 0.0
        cursor_position = self.text.index(tk.INSERT) if preserve_view else "1.0"
        self._loading_editor = True
        self._remove_parameter_selector_button()
        self._remove_script_selector_button()
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", content)
        self._add_parameter_selector_button(content)
        self._add_script_selector_button(content)
        if preserve_view:
            try:
                self.text.mark_set(tk.INSERT, cursor_position)
            except tk.TclError:
                self.text.mark_set(tk.INSERT, tk.END)
            self.text.yview_moveto(vertical_position)
            self.text.xview_moveto(horizontal_position)
        self.text.edit_modified(False)
        self._dirty = False
        self._loading_editor = False

    def _on_text_modified(self, _event: object = None) -> None:
        if self._loading_editor:
            return
        if self.text.edit_modified():
            self._dirty = True
            self.text.edit_modified(False)
            display_name = self.current_path.name if self.current_path else "未命名内容"
            self.status_var.set(f"未保存：{display_name}")
            self._schedule_auto_save()

    def _schedule_auto_save(self) -> None:
        self._cancel_auto_save()
        delay_ms = max(1, int(self.auto_save_delay_seconds * 1000))
        self._auto_save_after_id = self.after(delay_ms, self._auto_save)

    def _cancel_auto_save(self) -> None:
        if self._auto_save_after_id is None:
            return
        try:
            self.after_cancel(self._auto_save_after_id)
        except tk.TclError:
            pass
        self._auto_save_after_id = None

    def _auto_save(self) -> None:
        self._auto_save_after_id = None
        if not self._dirty:
            return
        self._write_current_draft()

    def _confirm_pending_changes(self) -> bool:
        if not self._dirty:
            return True
        choice = messagebox.askyesnocancel("存在未保存内容", "是否先保存当前文件？")
        if choice is None:
            return False
        if choice:
            return self.save_text(show_message=False)
        self._delete_draft(self.current_path)
        self._dirty = False
        self._cancel_auto_save()
        self.text.edit_modified(False)
        return True

    def _selected_path(self) -> Path | None:
        selection = self.tree.selection()
        if not selection:
            return None
        values = self.tree.item(selection[0], "values")
        if not values:
            return None
        return Path(values[0]).resolve()

    def _require_current_path(self) -> Path | None:
        path = self.current_path or self._selected_path()
        if path is None:
            messagebox.showwarning("未选择文件", "请先选择一个文件")
        return path

    def _select_path(self, path: Path) -> None:
        self._changing_selection = True
        try:
            for item in self.tree.get_children():
                values = self.tree.item(item, "values")
                if values and Path(values[0]).resolve() == path.resolve():
                    self.tree.selection_set(item)
                    self.tree.focus(item)
                    break
        finally:
            self._changing_selection = False

    def _clear_tree_selection(self) -> None:
        self._changing_selection = True
        try:
            selection = self.tree.selection()
            if selection:
                self.tree.selection_remove(*selection)
            self.tree.focus("")
        finally:
            self._changing_selection = False

    def _validate_file_name(
        self,
        name: str | None,
        script_extension: str | None = None,
    ) -> str | None:
        if name is None:
            return None
        normalized = name.strip()
        if self.view_mode == "script":
            supported_extensions = {".sh", ".bash", ".bat", ".cmd", ".ps1"}
            suffix = Path(normalized).suffix.lower()
            if script_extension not in supported_extensions:
                script_extension = None
            if suffix in supported_extensions and (
                script_extension is None or suffix == script_extension
            ):
                extension = suffix
            elif suffix in supported_extensions:
                messagebox.showerror(
                    "名称无效",
                    f"已选择 {script_extension} 类型，请使用对应的文件扩展名",
                )
                return None
            elif suffix:
                messagebox.showerror(
                    "名称无效",
                    "脚本仅支持 .sh、.bash、.bat、.cmd、.ps1",
                )
                return None
            else:
                if script_extension is None:
                    messagebox.showerror("名称无效", "请先选择脚本类型")
                    return None
                extension = script_extension
        else:
            extension = ".txt"
        if normalized.lower().endswith(extension):
            normalized = normalized[: -len(extension)]
        if not normalized:
            messagebox.showerror("名称无效", "文件名称不能为空")
            return None
        if any(character in normalized for character in '<>:"/\\|?*'):
            messagebox.showerror("名称无效", "文件名称包含 Windows 不允许的字符")
            return None
        return f"{normalized}{extension}"

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_close(self) -> None:
        if self._deploying:
            messagebox.showwarning(
                "部署仍在进行",
                "为避免在上传、替换或重启过程中中断操作，请等待本次部署完成后再关闭。",
            )
            return
        if not self._confirm_pending_changes():
            return
        if not self._conceal_current_parameter_password():
            return
        for terminal_tabs in list(self.ssh_tabs.values()):
            for terminal_tab in list(terminal_tabs):
                terminal_tab.close()
        self._save_application_state()
        self.master.destroy()


def _application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _initial_data_directory(argument_index: int, directory_name: str) -> Path:
    if len(sys.argv) > argument_index:
        directory = Path(sys.argv[argument_index]).expanduser().resolve()
    else:
        directory = _application_root() / "conf" / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _initial_task_directory() -> Path:
    return _initial_data_directory(1, "tasks")


def _initial_parameter_directory() -> Path:
    return _initial_data_directory(2, "parameters")


def _initial_script_directory() -> Path:
    return _initial_data_directory(3, "scripts")


def _initial_parameter_template_path() -> Path:
    if len(sys.argv) > 4:
        configured_path = Path(sys.argv[4]).expanduser().resolve()
        if configured_path.is_file():
            return configured_path
    return _application_root() / "templates" / "server_parameters.template.txt"


def _initial_script_template_path() -> Path:
    if len(sys.argv) > 5:
        configured_path = Path(sys.argv[5]).expanduser().resolve()
        if configured_path.is_file():
            return configured_path
    return _application_root() / "templates" / "remote_script.template.sh"


def _center_window(window: tk.Tk, width: int, height: int) -> None:
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def main() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "DeployFlow.DeploymentTool"
            )
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.title("DeployFlow 自动部署工具")
    icon_path = _application_root() / "assets" / "app_icon.ico"
    if icon_path.is_file():
        try:
            root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass
    _center_window(root, 1100, 720)
    root.minsize(800, 520)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    try:
        app = Application(
            root,
            _initial_task_directory(),
            _initial_parameter_directory(),
            _initial_script_directory(),
            _initial_parameter_template_path(),
            _initial_script_template_path(),
        )
    except Exception as exc:
        startup_log_path: Path | None = _application_root() / "startup-error.log"
        try:
            startup_log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            startup_log_path = None
        log_hint = (
            f"\n\n错误日志：{startup_log_path}"
            if startup_log_path is not None
            else "\n\n错误日志无法写入，请检查程序目录权限。"
        )
        messagebox.showerror(
            "程序启动失败",
            f"初始化程序失败：\n{exc}{log_hint}",
            parent=root,
        )
        root.destroy()
        return
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
