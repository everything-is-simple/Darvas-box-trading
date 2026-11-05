# Darvas adj 基线续跑看门狗
#
# ⚠️ 现状（2026-11-05）：**无法由 Agent 自动启动** —— 安全策略拦截
#    `Start-Process powershell.exe`（"interpreter/LOLBin target spawns a child process
#    that bypasses PowerShell command validation"）。需用户在自己的终端里手动跑：
#        powershell -NoProfile -ExecutionPolicy Bypass -File I:\Darvas-box-trading\scripts\bt_watchdog.ps1
#
# 作用：每 45 秒检测 darvas-dev.exe 是否存活；若已消失且库未跑完，则用 --resume-run 重新拉起。
# 安全：只在进程不存在时启动，避免并发写同一库；跑到 2018-12-20 之后即自行退出。
$ErrorActionPreference = "SilentlyContinue"

$exe   = "I:\Darvas-box-trading\bin\darvas-dev.exe"
$wd    = "I:\Darvas-box-trading"
$src   = "I:\Darvas-box-trading\var\bt\full_hist_adj.db"
$db    = "I:\Darvas-box-trading\var\bt\base\bt_baseA_cont_adj.db"
$out   = "I:\Darvas-box-trading\var\bt\base\run_wd.out"
$err   = "I:\Darvas-box-trading\var\bt\base\run_wd.err"
$watch = "I:\Darvas-box-trading\var\bt\base\watchdog.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $watch -Value "$ts  $msg" -Encoding utf8
}

function Get-Progress {
    $code = "import sqlite3;con=sqlite3.connect('file:$($db -replace '\\','/')?mode=ro',uri=True);n,d=con.execute(`"select count(*),max(date) from daily_run where task='plan' and status='ok'`").fetchone();print(n,d)"
    $r = & python -c $code 2>$null
    return $r
}

Log "watchdog start pid=$PID"

$restarts = 0
for ($i = 0; $i -lt 600; $i++) {
    Start-Sleep -Seconds 45
    $p = Get-Process darvas-dev -ErrorAction SilentlyContinue
    if ($p) { continue }

    # 进程不在：先确认是否已经跑完
    $prog = Get-Progress
    Log "process missing; progress=$prog"
    if ($prog -match "^\s*(\d+)\s+(\d{4}-\d{2}-\d{2})") {
        $last = $matches[2]
        if ($last -ge "2018-12-20") {
            Log "reached $last -- done, watchdog exit"
            break
        }
    }
    if ($restarts -ge 60) { Log "restart limit hit, exit"; break }

    $args = @("backtest","--src",$src,"--from","2005-01-01","--to","2018-12-31","--mode","regime","--db",$db,"--resume-run")
    $np = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $wd -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    $restarts++
    Log "restarted pid=$($np.Id) (restart #$restarts)"
    Start-Sleep -Seconds 10
}

Log "watchdog end, restarts=$restarts"
