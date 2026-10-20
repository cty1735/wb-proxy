@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy proxy - restart (LAN mode)
rem
rem  Stops every wb_proxy.py instance on this machine (from
rem  any folder - that is what would otherwise refuse to start
rem  the new copy with "already running"), then launches
rem  start-wb-proxy-lan.bat so phones / other PCs can connect.
rem
rem  Usage: restart-wb-proxy.bat [port] [api-key]
rem         (both optional, forwarded to the LAN launcher)
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem ===========================================================

echo Stopping other wb_proxy instances ...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*wb_proxy.py*' }; if ($p) { $p | ForEach-Object { Write-Host ('  stopping: PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1; if (Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*wb_proxy.py*' }) { Write-Host '  [WARN] some wb_proxy processes survived; they may still hold the port.' } else { Write-Host '  all wb_proxy instances stopped.' } } else { Write-Host '  no running wb_proxy instance.' }"

echo.
echo Starting LAN mode ...
call "%~dp0start-wb-proxy-lan.bat" %*
