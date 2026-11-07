@echo off
REM K1 冒烟段 A：2005 全年全段库 + 2005 前推60日截尾库（docs/65 阶段 0 / docs/60 K1）。
REM 目的：用最短窗口验「引擎是否偷看未来」的二元问题（前视污染是引擎固有属性，与窗口长度无关）。
REM 全段库与截尾库均用 v1 参数（staledays=2，不套 D3），隔离变量只验前视。
cd /d I:\Darvas-box-trading
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
set EXE=bin\darvas-dev.exe
set SRC=var\bt\full_hist_adj.db
echo === K1 SMOKE START %date% %time% === > var\bt\k1_smoke_run.log

REM 全段库 2005 全年
%PY% scripts\base_run.py --cont --from 2005-01-01 --to 2005-12-31 --mirrors A ^
  --src %SRC% --exe %EXE% --db var\bt\arms\bt_k1_full2005.db >> var\bt\k1_full2005.log 2>&1
if errorlevel 1 goto :fail

REM 截尾库 2005 前推60日
%PY% scripts\base_run.py --cont --from 2005-01-01 --to 2005-09-30 --mirrors A ^
  --src %SRC% --exe %EXE% --db var\bt\arms\bt_k1_trunc2005.db >> var\bt\k1_trunc2005.log 2>&1
if errorlevel 1 goto :fail

echo === K1 SMOKE DONE %date% %time% === >> var\bt\k1_smoke_run.log
exit /b 0
:fail
echo === K1 SMOKE FAILED rc=%errorlevel% %date% %time% === >> var\bt\k1_smoke_run.log
exit /b %errorlevel%
