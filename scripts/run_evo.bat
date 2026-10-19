@echo off
REM M8.6 evolution backtest: probe=10%% + addon channel (v2), years 2014-2018 serially
REM Started via independent process so the sandbox does not recycle it mid-run.
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
echo === EVO RUN START %date% %time% === > var\bt\evo_run.log
if not exist var\bt\evo_metrics.csv echo year,days,eq_start,eq_end,ret_pct,maxdd_pct,breakout,pyramid_newbox,pyramid_addon,failed,stops,pos_total,pos_closed,pos_open,leg1,leg2,leg3,charged,pnl_total,pnl_addon,addon_share_pct,win_rate_pct,avg_hold_days > var\bt\evo_metrics.csv
call :run 2014
call :run 2015
call :run 2016
call :run 2017
call :run 2018
echo === ALL DONE %date% %time% === >> var\bt\evo_run.log
goto :eof

:run
echo === %1 START %date% %time% === >> var\bt\evo_run.log
darvas_v2.exe backtest --src var\bt\full_hist_10pct.db --from %1-01-01 --to %1-12-31 --mode regime --db var\bt\bt_evo_%1.db >> var\bt\evo_%1.log 2>&1
echo === %1 DONE rc=%errorlevel% %date% %time% === >> var\bt\evo_run.log
if exist var\bt\bt_evo_%1.db "%PY%" scripts\evo_metrics.py var\bt\bt_evo_%1.db %1 >> var\bt\evo_metrics.csv
goto :eof
