@echo off
REM D3+D4「原著实现资格线」臂（docs/65 阶段 0 / docs/60 §7.0）。
REM 一次性修正 O2（time_stop 禁用）+ O3（无冷却）两处反向，其余参数 = v1 基线 COMMON。
REM 走 base_run.py 注入完整 COMMON（阶梯/units/lookback/cutoff/max_hold/equity_gate/ma60），
REM 仅覆写 staledays=0 + blacklist_months=0，保证单变量对照（只动 O2/O3）。
REM 引擎 bin\darvas-dev.exe（sha fe3a9f22，HARD① 身份校验）；全新库，不带 --resume。
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
echo === D3+D4 START %date% %time% === > var\bt\d3d4_run.log
%PY% scripts\base_run.py --cont --from 2005-01-01 --to 2018-12-31 --mirrors A ^
  --src var\bt\full_hist_adj.db --exe bin\darvas-dev.exe ^
  --db var\bt\arms\bt_d3d4_off.db ^
  --set "position.staledays=0,pool.blacklist_months=0" >> var\bt\d3d4_off.log 2>&1
set RC=%errorlevel%
echo === D3+D4 DONE rc=%RC% %date% %time% === >> var\bt\d3d4_run.log
exit /b %RC%
