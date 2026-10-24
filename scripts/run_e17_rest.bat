@echo off
REM E17 续跑：2015-2018 逐年真实回放（2014 已于 2026-10-24 23:39 完成，+182,188）。
REM 阶梯参数与 run_e17.bat 完全一致，仅写入 scratch 库（--set），源库/生产不受影响。
REM 失败即停。批处理用显式 call，不用 for 块（延迟展开坑）。
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
set SETARGS=pos.newhigh_ladder=on,pos.newhigh.lookback=252,pos.newhigh.cutoff=40,pos.newhigh.units.t1=3,pos.newhigh.units.t2=2,pos.newhigh.units.t3=1,pos.newhigh.units.t4=0.5
set EXE=var\bt\darvas_e17.exe
set SRC=var\bt\full_hist_10pct.db
echo === E17 REST RUN START %date% %time% === > var\bt\e17_run_rest.log
if not exist var\bt\e17_metrics.csv echo year,days,eq_start,eq_end,ret_pct,maxdd_pct,sharpe,profit_factor,pos_closed,win_rate_pct,pnl_total > var\bt\e17_metrics.csv
call :run 2015
if errorlevel 1 goto :fail
call :run 2016
if errorlevel 1 goto :fail
call :run 2017
if errorlevel 1 goto :fail
call :run 2018
if errorlevel 1 goto :fail
echo === ALL DONE %date% %time% === >> var\bt\e17_run_rest.log
goto :eof

:run
echo === %1 START %date% %time% === >> var\bt\e17_run_rest.log
%EXE% backtest --src %SRC% --from %1-01-01 --to %1-12-31 --mode regime --db var\bt\bt_e17_%1.db --set "%SETARGS%" >> var\bt\e17_%1.log 2>&1
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo === %1 FAILED rc=%RC% %date% %time% === >> var\bt\e17_run_rest.log
    exit /b %RC%
)
echo === %1 DONE rc=0 %date% %time% === >> var\bt\e17_run_rest.log
"%PY%" scripts\p05_yearly_report.py var\bt\bt_e17_%1.db %1 > var\bt\bt_e17_%1_report.md
"%PY%" scripts\p05_yearly_report.py var\bt\bt_e17_%1.db %1 --csv-file var\bt\e17_metrics.csv
goto :eof

:fail
echo === CHAIN STOPPED ON FAILURE %date% %time% === >> var\bt\e17_run_rest.log
exit /b 1
