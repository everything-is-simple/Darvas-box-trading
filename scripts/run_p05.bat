@echo off
REM P0-5 R1 round: 2014-2018 serial backtest (战绩分级制新体系, code 7911057).
REM New DBs bt_p05_YYYY.db; old bt_m88_* / bt_20* kept untouched as R0 diff baseline.
REM Stop on first failure: a sick year must not enter the metrics table (R0 lesson: 2017/2018 rc=1).
REM Launch via Windows scheduled task -- the sandbox recycles shell long-runners.
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
echo === P05 RUN START %date% %time% === > var\bt\p05_run.log
if not exist var\bt\p05_metrics.csv echo year,days,eq_start,eq_end,ret_pct,maxdd_pct,sharpe,profit_factor,pos_closed,win_rate_pct,pnl_total > var\bt\p05_metrics.csv
call :run 2014
if errorlevel 1 goto :fail
call :run 2015
if errorlevel 1 goto :fail
call :run 2016
if errorlevel 1 goto :fail
call :run 2017
if errorlevel 1 goto :fail
call :run 2018
if errorlevel 1 goto :fail
echo === ALL DONE %date% %time% === >> var\bt\p05_run.log
goto :eof

:run
echo === %1 START %date% %time% === >> var\bt\p05_run.log
var\bt\darvas_p05.exe backtest --src var\bt\full_hist_10pct.db --from %1-01-01 --to %1-12-31 --mode regime --db var\bt\bt_p05_%1.db >> var\bt\p05_%1.log 2>&1
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo === %1 FAILED rc=%RC% %date% %time% === >> var\bt\p05_run.log
    exit /b %RC%
)
echo === %1 DONE rc=0 %date% %time% === >> var\bt\p05_run.log
if exist var\bt\bt_p05_%1.db (
    "%PY%" scripts\p05_yearly_report.py var\bt\bt_p05_%1.db %1 > var\bt\bt_p05_%1_report.md
    "%PY%" scripts\p05_yearly_report.py var\bt\bt_p05_%1.db %1 --csv-file var\bt\p05_metrics.csv
)
goto :eof

:fail
echo === CHAIN STOPPED ON FAILURE %date% %time% === >> var\bt\p05_run.log
exit /b 1
