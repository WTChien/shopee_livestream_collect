$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$config = Join-Path $project 'line.env'
if (-not (Test-Path -LiteralPath $config)) {
    throw '找不到 line.env，請先複製 line.env.example 並填入 LINE 設定。'
}
foreach ($line in Get-Content -LiteralPath $config -Encoding UTF8) {
    if ($line -match '^\s*(LINE_CHANNEL_SECRET|LINE_CHANNEL_ACCESS_TOKEN|LINE_GROUP_BIND_CODE|CLOUDFLARE_TUNNEL_TOKEN)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), 'Process')
    }
}
foreach ($key in @('LINE_CHANNEL_SECRET', 'LINE_CHANNEL_ACCESS_TOKEN')) {
    if (-not [Environment]::GetEnvironmentVariable($key, 'Process')) {
        throw "line.env 缺少 $key"
    }
}
$listener = Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Start-Process -FilePath (Join-Path $project '.venv\Scripts\python.exe') `
        -ArgumentList 'line_webhook.py' -WorkingDirectory $project -WindowStyle Hidden
}

$token = [Environment]::GetEnvironmentVariable('CLOUDFLARE_TUNNEL_TOKEN', 'Process')
if ($token -and -not (Get-Process -Name cloudflared -ErrorAction SilentlyContinue)) {
    $binary = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    if (-not (Test-Path -LiteralPath $binary)) {
        throw '找不到 cloudflared.exe，請先安裝 Cloudflare Tunnel。'
    }
    Start-Process -FilePath $binary -ArgumentList @('tunnel', '--no-autoupdate', 'run', '--token', $token) `
        -WorkingDirectory $project -WindowStyle Hidden
}

Write-Host 'LINE webhook 已啟動；公開網址為 https://shopeeline.weiting.win/webhook'
