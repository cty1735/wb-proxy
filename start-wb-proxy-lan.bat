@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy proxy - LAN mode
rem
rem  Listens on every network interface so phones, laptops and
rem  other PCs on the same network can use it.
rem
rem  An API key is REQUIRED in this mode.
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem
rem  SILENT MODE: with a GUI-subsystem pythonw the service runs
rem  with NO console window and nothing in the taskbar; closing
rem  any window never stops it. A separate live log viewer opens
rem  on top - closing the viewer only closes the viewer. When no
rem  pythonw exists we fall back to the visible-window launcher.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8788"
set "KEY=%~2"
set "HERE=%~dp0"
set "SCRIPT=%HERE%wb_proxy.py"
set "PYARGS=--port %PORT% --lan"
if not "%KEY%"=="" set "PYARGS=%PYARGS% --api-key %KEY%"

if not exist "%SCRIPT%" goto no_script

rem ---- prefer pythonw (GUI subsystem: no console window) ----
if exist "%HERE%python\pythonw.exe" goto pyw_bundled
if exist "%HERE%.venv\Scripts\pythonw.exe" goto pyw_venv
pythonw --version >nul 2>nul
if not errorlevel 1 goto pyw_path
pyw --version >nul 2>nul
if not errorlevel 1 goto pyw_path_pyw
goto use_python

:pyw_bundled
set "PYW=%HERE%python\pythonw.exe"
goto run_silent

:pyw_venv
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
goto run_silent

:pyw_path
set "PYW=pythonw"
goto run_silent

:pyw_path_pyw
set "PYW=pyw"
goto run_silent

:run_silent
rem Launch the service headless: no console window, nothing in the
rem taskbar, and closing any window never stops it.
start "" "%PYW%" "%SCRIPT%" %PYARGS%

rem Open a live log viewer in its own console window. The viewer
rem waits quietly while the service starts and closes itself when
rem the service is shut down. pythonw has no window, so a console
rem python is needed for the viewer.
if exist "%HERE%python\python.exe" goto viewer_bundled
if exist "%HERE%.venv\Scripts\python.exe" goto viewer_venv
python --version >nul 2>nul
if not errorlevel 1 goto viewer_path
exit /b 0

:viewer_bundled
set "VIEWERPY=%HERE%python\python.exe"
goto viewer_go

:viewer_venv
set "VIEWERPY=%HERE%.venv\Scripts\python.exe"
goto viewer_go

:viewer_path
set "VIEWERPY=python"
goto viewer_go

:viewer_go
if not exist "%HERE%console_tail.py" exit /b 0
start "" "%VIEWERPY%" "%HERE%console_tail.py" --port %PORT%
exit /b 0

:use_python
rem ===========================================================
rem  Visible-window launcher (fallback, pythonw unavailable)
rem ===========================================================
set "PYEXE="

rem ---- 1) bundled runtime (ships with the zip) ----
if not exist "%HERE%python\python.exe" goto try_path
"%HERE%python\python.exe" --version >nul 2>nul
if errorlevel 1 goto try_path
set "PYEXE=%HERE%python\python.exe"
goto run

:try_path
rem ---- 2) python on PATH ----
python --version >nul 2>nul
if errorlevel 1 goto try_py
set "PYEXE=python"
goto run

:try_py
rem ---- 3) py launcher ----
py --version >nul 2>nul
if errorlevel 1 goto try_codex
set "PYEXE=py"
goto run

:try_codex
rem ---- 4) Codex bundled runtimes ----
call :find_codex
if defined PYEXE goto run

echo [ERROR] No usable Python found.
echo.
echo Options:
echo   1. Use the packaged zip, which already contains python\
echo   2. Install Python 3.9+ from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" during setup)
echo.
pause
exit /b 1

:no_script
echo [ERROR] wb_proxy.py not found next to this script.
echo         expected: %SCRIPT%
echo.
pause
exit /b 1

:find_codex
if not exist "%USERPROFILE%\.cache\codex-runtimes" goto :eof
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do call :probe "%%~D"
goto :eof

:probe
if defined PYEXE goto :eof
for /f "delims=" %%P in ('dir /b /s "%~1\python.exe" 2^>nul') do call :probe_one "%%~P"
goto :eof

:probe_one
if defined PYEXE goto :eof
"%~1" --version >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:run
echo ===========================================================
echo   WorkBuddy proxy - LAN MODE
echo.
echo   Port %PORT% - your API address, dashboard link and API
echo   key are printed below once the server is up.
echo.
echo   If other devices cannot connect, run allow-firewall.bat
echo   once as administrator.
echo.
echo   Keep this window open. Closing it stops the server.
echo ===========================================================
echo.

"%PYEXE%" "%SCRIPT%" %PYARGS%

echo.
echo [server exited]
pause
