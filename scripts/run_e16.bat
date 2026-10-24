@echo off
REM E16: 黑名单冷却期对照（0=无 / 6 / 12 / 24 月；永久臂 = 既有 p05 基线，行为等价）。
REM 注意：本二进制含 2026-10-24 修复——冷却截止日以回放清仓日为准，不再用 time.Now。
REM 每个 scratch 跑完先出报告 + 精简库，再删除 4.4GB scratch（精简库保留全部交易证据）。
REM 批处理用显式 call，不用 for 块（延迟展开坑）。
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
set EXE=var\bt\darvas_e16.exe
set SRC=var\bt\full_hist_10pct.db
echo === E16 RUN START %date% %time% === > var\bt\e16_run.log
call :arm 0
if errorlevel 1 goto :fail
call :arm 6
if errorlevel 1 goto :fail
call :arm 12
if errorlevel 1 goto :fail
call :arm 24
if errorlevel 1 goto :fail
echo === ALL DONE %date% %time% === >> var\bt\e16_run.log
goto :eof

:arm
echo === arm=%1 START %date% %time% === >> var\bt\e16_run.log
call :run %1 2014
if errorlevel 1 exit /b 1
call :run %1 2015
if errorlevel 1 exit /b 1
call :run %1 2016
if errorlevel 1 exit /b 1
call :run %1 2017
if errorlevel 1 exit /b 1
call :run %1 2018
if errorlevel 1 exit /b 1
echo === arm=%1 DONE %date% %time% === >> var\bt\e16_run.log
goto :eof

:run
echo === arm=%1 year=%2 START %date% %time% === >> var\bt\e16_run.log
%EXE% backtest --src %SRC% --from %2-01-01 --to %2-12-31 --mode regime --db var\bt\bt_e16_%1_%2.db --set "pool.blacklist_months=%1" >> var\bt\e16_%1_%2.log 2>&1
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo === arm=%1 year=%2 FAILED rc=%RC% %date% %time% === >> var\bt\e16_run.log
    exit /b %RC%
)
"%PY%" scripts\p05_yearly_report.py var\bt\bt_e16_%1_%2.db %2 > var\bt\bt_e16_%1_%2_report.md
if not exist var\bt\e16_arm_%1.csv echo year,days,eq_start,eq_end,ret_pct,maxdd_pct,sharpe,profit_factor,pos_closed,win_rate_pct,pnl_total > var\bt\e16_arm_%1.csv
"%PY%" scripts\p05_yearly_report.py var\bt\bt_e16_%1_%2.db %2 --csv-file var\bt\e16_arm_%1.csv
"%PY%" scripts\slim_bt_db.py var\bt\bt_e16_%1_%2.db var\bt\bt_e16_%1_%2_slim.db >> var\bt\e16_run.log 2>&1
if errorlevel 1 (
    echo === arm=%1 year=%2 SLIM FAILED, scratch kept === >> var\bt\e16_run.log
    exit /b 1
)
del var\bt\bt_e16_%1_%2.db var\bt\bt_e16_%1_%2.db-wal var\bt\bt_e16_%1_%2.db-shm 2>nul
echo === arm=%1 year=%2 DONE rc=0 %date% %time% === >> var\bt\e16_run.log
goto :eof

:fail
echo === CHAIN STOPPED ON FAILURE %date% %time% === >> var\bt\e16_run.log
exit /b 1
