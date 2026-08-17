@echo off
setlocal
pushd "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto :find_inno

where python.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python or create a .venv in the project directory.
    goto :failed
)
set "PYTHON_EXE=python.exe"

:find_inno
set "ISCC_EXE="
where ISCC.exe >nul 2>nul
if not errorlevel 1 set "ISCC_EXE=ISCC.exe"
if defined ISCC_EXE goto :check_files
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files

echo Inno Setup 6 was not found. Installing it with WinGet...
where winget.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] WinGet was not found. Inno Setup cannot be installed automatically.
    goto :failed
)
winget.exe install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [ERROR] Inno Setup installation failed.
    goto :failed
)

if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto :check_files

echo [ERROR] Inno Setup was installed, but ISCC.exe could not be located.
goto :failed

:check_files
if not exist "%~dp0setup.py" (
    echo [ERROR] setup.py was not found.
    goto :failed
)
if not exist "%~dp0app\qt_main.pyw" (
    echo [ERROR] app\qt_main.pyw was not found.
    goto :failed
)
if not exist "%~dp0installer\DeployFlow.iss" (
    echo [ERROR] installer\DeployFlow.iss was not found.
    goto :failed
)
if not exist "%~dp0assets\app_icon.ico" (
    echo [ERROR] assets\app_icon.ico was not found.
    goto :failed
)

echo Checking the Python build environment...
"%PYTHON_EXE%" -c "import cx_Freeze, fabric, paramiko, pyte, PySide6" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] cx_Freeze or a project dependency is missing.
    echo Run: "%PYTHON_EXE%" -m pip install -r requirements.txt
    goto :failed
)

set "STAGING_DIR=%~dp0.installer-build\DeployFlow"
set "INSTALLER_FILE=%~dp0installer-output\DeployFlow-0.3.0-Setup.exe"

if exist "%~dp0.installer-build" rmdir /S /Q "%~dp0.installer-build"
if errorlevel 1 (
    echo [ERROR] The temporary build directory could not be removed.
    echo Close any program that is using the directory and try again.
    goto :failed
)

echo Building the standalone application...
"%PYTHON_EXE%" setup.py build_exe --build-exe "%STAGING_DIR%"
if errorlevel 1 (
    echo [ERROR] Application build failed. Review the output above.
    goto :failed
)
if not exist "%STAGING_DIR%\DeployFlow.exe" (
    echo [ERROR] DeployFlow.exe was not generated.
    goto :failed
)

echo Building the Windows installer...
"%ISCC_EXE%" "%~dp0installer\DeployFlow.iss"
if errorlevel 1 (
    echo [ERROR] Installer build failed. Review the output above.
    goto :failed
)
if not exist "%INSTALLER_FILE%" (
    echo [ERROR] Installer output was not found: %INSTALLER_FILE%
    goto :failed
)

rmdir /S /Q "%~dp0.installer-build" 2>nul
echo.
echo [SUCCESS] Installer created:
echo %INSTALLER_FILE%
goto :finish

:failed
echo.
echo Installer build stopped.

:finish
popd
pause
endlocal
