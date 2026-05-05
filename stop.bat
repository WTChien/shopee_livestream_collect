@echo off
chcp 65001 >nul
title 停止 Shopee Live 自動化控制面板

set "PORT=4001"

echo 正在停止 Shopee Live 自動化控制面板...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$port=%PORT%; $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue; $pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 }); if ($pids.Count -eq 0) { Write-Host ('沒有找到正在監聽 ' + $port + ' 的服務。'); exit 0 }; foreach ($p in $pids) { Write-Host ('找到 ' + $port + ' 的 PID: ' + $p + '，正在終止...'); Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }; $deadline = (Get-Date).AddSeconds(6); while ((Get-Date) -lt $deadline) { $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if (-not $listen) { Write-Host '連接埠已釋放。'; exit 0 }; Start-Sleep -Milliseconds 300 }; Write-Host '偵測到疑似孤兒子程序，嘗試清理 spawn_main...'; foreach ($pp in $pids) { $orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match ('parent_pid=' + $pp) }; foreach ($o in $orphans) { Write-Host ('終止孤兒 PID: ' + $o.ProcessId); Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue } }; $deadline2 = (Get-Date).AddSeconds(6); while ((Get-Date) -lt $deadline2) { $listen2 = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if (-not $listen2) { Write-Host '連接埠已釋放。'; exit 0 }; Start-Sleep -Milliseconds 300 }; Write-Host '警告: 連接埠仍被占用，請用管理員身分執行或重新開機。'; Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object LocalAddress,State,OwningProcess | Format-Table -AutoSize; exit 1"

if errorlevel 1 (
    echo 停止流程完成，但 %PORT% 仍可能被占用。
    exit /b 1
)

echo 完成。
