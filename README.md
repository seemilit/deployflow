# DeployFlow

**Visual deployment workflows, SSH terminals, and SFTP file management in one Windows desktop application.**

[English](README.md) · [简体中文](README_CN.md) · [繁體中文](README_TW.md) · [日本語](README_JA.md) · [한국어](README_KO.md) · [Español](README_ES.md)

## Why DeployFlow?

For a few projects or a small team, setting up and maintaining a full automation platform can add more work than the deployment itself. Doing everything in a terminal has its own friction: repeated commands, copied paths, separate transfer windows, and release steps that are easy to miss.

DeployFlow connects the tools and scripts you already use into a local desktop workflow. Build, connect, upload, run a script, and check the service in sequence—then inspect the result through the integrated terminal and file browser.

It is designed for **individual developers, small teams, and developers who regularly deploy to or troubleshoot Linux servers**. It suits desktop-driven releases, frontend uploads, Java service deployments, and routine server work. Central scheduling, multi-user approvals, and distributed execution are outside its current scope.

![Deployment workflow and step parameters](docs/screenshots/workflow.png)

*Example: Maven build → connect → upload → deployment script → health check. Screenshots use the English interface; user content and command output retain their original language.*

## From a release checklist to a reusable workflow

Create a task by adding ordered steps. Select a node to inspect its parameters, double-click to edit it, or drag it to change the order. A new step is inserted after the selected node, or appended when nothing is selected. Show the workflow canvas and parameter panel independently or together.

| Step | What it does |
| --- | --- |
| Connect to server | Load a saved configuration and create a named SSH connection. |
| Merge branches / push branch | Run the configured Git pull, merge, commit, and push operations. |
| Maven build | Build a project and identify its JAR artifact for later upload. |
| Local command / local script | Run Windows commands or scripts with a working directory, timeout, and script arguments. |
| Upload | Send an artifact, file, or folder; optionally create the destination and back up existing files. |
| Remote command / remote script | Run commands or saved shell scripts through a named connection. |
| Wait | Pause between steps. |
| Health check | Repeat a command until consecutive successes or the timeout. |

Click **Run** with no selection to execute the entire task, or select nodes to run them in their original order. Node context menus also offer **run this step** and **start from here**. Output, progress, timeouts, cancellation, and failure details accompany execution.

Partial execution does not recreate skipped dependencies. Include the connection or build steps needed by the selected operations in the same run.

## Turn terminal paths into actions

You do not have to copy a path out of the terminal and paste it into a file browser every time.

- **Ctrl + hover:** a recognized path under the pointer is underlined.
- **Ctrl + left-click:** jump to the directory in the terminal and file panel. For a file, open its containing directory.
- **Right-click a path:** use an existing text selection, or right-click the path directly without selecting it first. After identification, the menu offers actions for that item.
- **Act on the file:** open a text editor, run a script with optional arguments, change permissions, or view/search log files using follow, last-N-lines, and keyword actions.

These actions depend on the path being resolvable on the current server. Relative names need a known directory context; each SSH tab keeps its own context.

![Terminal with its linked remote directory panel](docs/screenshots/remote-files.png)

## Save server details and common locations

Manage addresses, ports, accounts, and authentication through a form. Password and private-key values can both be retained; the selected authentication mode determines which is used.

A server can have multiple saved locations, each with an optional command. Set a default location and choose whether its command should run, or use **Connect here**, double-click, or the row context menu to connect to a particular location directly.

![Server settings, authentication, and saved locations](docs/screenshots/server-config.png)

Settings include password protection and **Hide IP**. Masking changes an address such as `192.168.10.25` to `192.**.**.25` in supported address displays while connections retain the real value. Turn it off to edit a masked address. It does not redact saved logs or arbitrary remote output.

## A full SSH workspace

Open multiple server tabs with the **+** button. Use the terminal below the editor, or switch to **Terminal mode** for a larger workspace.

The terminal uses xterm.js through Qt WebEngine and supports shell completion, command history, copy/paste, interrupt, and reconnect. Its side panel displays Linux OS/kernel information, uptime, load, CPU, memory, swap, disk usage, and running programs with ports and resource usage. Process details include the command; context actions can stop or attempt to restart a process.

![SSH terminal and live Linux server information](docs/screenshots/ssh-monitor.png)

Process restart is marked **not recommended** because the original startup script, pipes, and log redirection may not be recoverable. Use the original script or service manager when those details are required.

## Transfer files in the layout that fits your work

The **dual-pane SFTP window** shows local drives and folders on the left and the server on the right. Upload selected items or drag local files/folders, including from Windows Explorer, into the remote list.

![Local and remote SFTP transfer window](docs/screenshots/sftp-transfer.png)

The **embedded file panel** stays below the terminal, with a directory tree, breadcrumbs, directory history, and file details. The adjacent **Logs** tab shows the current connection's log.

Both layouts support upload/download, remote copy/cut/paste, rename, delete, new files/folders, permission editing, and a separate remote text editor. Remote copy/paste works within the same server connection identity. The transfer panel shows multiple upload/download jobs; removing an active job cancels it, and completed downloads can open their location. You can also drag remote files to a Windows Explorer folder to download there.

## Reuse scripts and revisit changes

Keep local build scripts and remote deployment scripts in **Scripts**, then reference them from tasks. Supported local formats are `.bat`, `.cmd`, `.ps1`, `.sh`, and `.bash`; remote workflow scripts use `.sh` or `.bash`.

![Built-in script editor](docs/screenshots/script-editor.png)

Tasks, configurations, and scripts have automatic drafts and version history. **Save / Ctrl+S** writes changes to the main file. History supports preview, restore, multi-selection, and deletion. Renaming configurations or scripts inside the application updates references in current JSON workflows, drafts, and workflow history.

Execution and SSH logs are stored in date folders with creation time in their filenames and timestamps on entries. The **Logs** page lets you revisit or delete records after the output panel has been cleared.

![Persistent task and connection logs](docs/screenshots/logs.png)

## Get started

### Requirements and launch

- Windows 64-bit; the current development environment uses Python 3.12.
- SSH/SFTP access for remote operations. Monitoring and remote-script features target Linux servers.
- Tools for the steps you use: Git; JDK and Maven or the project's Maven wrapper; Node.js/npm for frontend scripts; Bash for local shell scripts.

Run from the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.bat
```

For an IDE or direct launch, select `.venv\Scripts\python.exe` and the entry point `app\qt_main.pyw`:

```powershell
.\.venv\Scripts\python.exe app\qt_main.pyw
```

If you already have a portable build, keep its complete folder and run `DeployFlow.exe`.

### Your first task

1. Create and save a server under **Configs**.
2. Add any required build/deployment scripts under **Scripts**.
3. Create a task and add steps. Name the server connection and build artifact, then reference those names in later steps.
4. Check working directories, save, and run. Inspect the result using output, SSH, and the file panel.

A frontend release can use **local script → connect → upload folder → remote command → health check**. In batch files, `%CD%` means the current working directory; `%~dp0` means the script's own directory.

## Everyday controls

| Area | Control |
| --- | --- |
| Workflow | Double-click to edit; drag nodes to reorder. |
| Workflow selection | Ctrl+click for multiple nodes; drag on empty canvas to box-select. |
| Canvas | Wheel to zoom; right-button drag to pan. |
| Editing | Ctrl+S to save; Ctrl+wheel in the parameter/text panel adjusts and remembers font size. |
| Remote file list | Ctrl+C / Ctrl+X / Ctrl+V, Delete, F2, Ctrl+A. |
| History | Ctrl+click / Shift+click / Ctrl+A for multiple selection. |
| Terminal paths | Ctrl+hover, Ctrl+click, or right-click for path actions. |

## Languages and appearance

The UI supports **English, Simplified Chinese, Traditional Chinese, Japanese, Korean, and Spanish**. English is the initial default; the saved choice is used afterward. Language changes take effect without restarting. User content and external command output are not translated.

Choose light, dark, paper-yellow, eye-care green, or custom colors. Panel sizes, per-task workflow zoom, and parameter/text font size are remembered.

## Local data and behavior

Data stays under `conf/` beside the source project or packaged executable. Required folders are created automatically, so the application directory must be writable.

```text
conf/
├── tasks/          # Version 2 JSON workflows stored as .txt
├── host/           # Server configurations
├── scripts/        # Local and remote scripts
├── .drafts/        # Automatic editing drafts
├── .history/       # Version history
├── .logs/          # Task and SSH logs grouped by date
├── .cache/         # Temporary application caches
├── downloads/      # Default download location
├── known_hosts     # Remembered SSH host keys
└── settings.json   # Language, theme, layout, and preferences
```

- The current loader accepts **version 2 JSON**, not legacy `STEP_xxx=...` workflow files.
- Renames outside DeployFlow do not automatically update task references.
- A server's default location/command applies to interactive connections. Set remote workflow steps' working directories separately.
- Upload backups are optional. A later script or health-check failure does not automatically roll back the whole release.
- Password protection uses Windows user-bound encryption; moving data to another computer/account may require entering credentials again.
- `conf/` is excluded from Git. Back it up privately; logs can contain real addresses and command output even with IP masking enabled.

## Build and license

```powershell
.\build.bat
```

Portable output: `release/DeployFlow/`. For an installer, make Inno Setup 6 available and run:

```powershell
.\build_installer.bat
```

Installer output: `installer-output/`. Ensure the installed application's `conf/` directory is writable.

Built with PySide6, Qt WebEngine, xterm.js, and Fabric/Paramiko. DeployFlow's own code uses the [MIT License](LICENSE); dependencies retain their respective licenses. Bundled terminal licenses are in [app/resources/xterm](app/resources/xterm/).

A standalone Chinese product introduction is available in [项目介绍](docs/PROJECT_OVERVIEW_CN.md).
