@echo off
chcp 65001 >nul
title Shopee Live 自動化控制面板

cd /d "%~dp0"
set "PORT=4001"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
	set "PYTHON_EXE=c:\python314\python.exe"
)

:: 啟動前先清掉既有監聽進程，避免 bind 衝突
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
	"$port=%PORT%; $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue; $pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 }); foreach ($p in $pids) { Write-Host ('偵測到 ' + $port + ' 被占用，PID=' + $p + '，先關閉舊進程...'); Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }"

:: 驗證連接埠是否已可用
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
	"$port=%PORT%; $deadline = (Get-Date).AddSeconds(6); while ((Get-Date) -lt $deadline) { $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if (-not $listen) { exit 0 }; Start-Sleep -Milliseconds 300 }; $stuck = @((Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 })); foreach ($pp in $stuck) { $orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match ('parent_pid=' + $pp) }; foreach ($o in $orphans) { Write-Host ('偵測到孤兒子程序 PID=' + $o.ProcessId + '，正在終止...'); Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue } }; $deadline2 = (Get-Date).AddSeconds(6); while ((Get-Date) -lt $deadline2) { $listen2 = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if (-not $listen2) { exit 0 }; Start-Sleep -Milliseconds 300 }; Write-Host ('錯誤: 連接埠 ' + $port + ' 仍被占用。'); Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object LocalAddress,State,OwningProcess | Format-Table -AutoSize; exit 1"

if errorlevel 1 (
	echo 請先以系統管理員身分執行 stop.bat，或重新開機後再試。
	pause
	exit /b 1
)

echo 啟動 Shopee Live 自動化控制面板...
echo Python: %PYTHON_EXE%
echo 伺服器網址: http://localhost:%PORT%
echo 關閉此視窗即可停止伺服器
echo.

"%PYTHON_EXE%" -m uvicorn main:app --host 0.0.0.0 --port %PORT%

pause
