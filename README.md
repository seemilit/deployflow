# DeployFlow

[中文文档](README_CN.md)

DeployFlow is a Windows deployment assistant for managing deployment tasks, server parameters, and scripts, then running deployment workflows through SSH.

## Features

- Manage deployment tasks, server connection parameters, and remote scripts
- Compose ordered workflows with Git branch merge/push, Maven build, local commands, file or folder upload, remote commands, wait, and health checks
- Back up existing remote files before upload and use remote commands to restore or restart services when needed
- View logs and progress, handle timeouts, cancel runs, and start from a selected workflow step
- Use multi-tab SSH terminals with command history, copy/paste, interrupt, and reconnect support
- Transfer files through a dual-pane SFTP browser with local and Windows Explorer drag-and-drop support
- Built with a PySide6 desktop interface

## Typical workflow

For a Java service release, a workflow can connect to a server, merge and push source code, build a JAR with Maven, back up and upload the new artifact, restart the service, and verify it with a health check.

## Requirements

- Windows
- Python 3

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.bat
```

Or run the PySide6 entry point directly:

```powershell
.\.venv\Scripts\pythonw.exe app\qt_main.pyw
```

## Data directory

The application creates `conf/` on first launch:

- `conf/tasks/`: deployment tasks
- `conf/parameters/`: server parameters
- `conf/scripts/`: deployment scripts
- `conf/settings.json`: UI settings

`conf/` may contain host addresses, accounts, or encrypted passwords, so it is excluded from Git by default.

## Build

```powershell
.\build.bat
```

Creates a portable build in `release/DeployFlow/`.

```powershell
.\build_installer.bat
```

Creates a Windows installer in `installer-output/`. Inno Setup 6 is required.

## License

This project is licensed under the [MIT License](LICENSE).
