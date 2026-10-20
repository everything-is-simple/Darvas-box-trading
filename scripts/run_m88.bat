@echo off
REM M8.8 breadth+unit backtest: years 2014-2018 serially, independent process.
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
echo === M88 RUN START %date% %time% === > var\bt\m88_run.log
if not exist var\bt\m88_metrics.csv echo year,days,eq_start,eq_end,ret_pct,maxdd_pct,breakout,pyramid,failed,stops,probe_fills,main_fills,leg1,leg2,leg3,charged,pos_closed,win_rate_pct,pnl_total,unit_peak,cash_min_pct,probe_units_end,main_units_end > var\bt\m88_metrics.csv
call :run 2014
call :run 2015
call :run 2016
call :run 2017
call :run 2018
echo === ALL DONE %date% %time% === >> var\bt\m88_run.log
goto :eof

:run
echo === %1 START %date% %time% === >> var\bt\m88_run.log
darvas_v2.exe backtest --src var\bt\full_hist_10pct.db --from %1-01-01 --to %1-12-31 --mode regime --db var\bt\bt_m88_%1.db >> var\bt\m88_%1.log 2>&1
echo === %1 DONE rc=%errorlevel% %date% %time% === >> var\bt\m88_run.log
if exist var\bt\bt_m88_%1.db "%PY%" scripts\m88_metrics.py var\bt\bt_m88_%1.db %1 >> var\bt\m88_metrics.csv
goto :eof
