@echo off
rem 新基线 14 年全量（单镜上证）—— 计划任务入口，2026-10-29
rem 已存在的年份库由 base_run.py 自动跳过（run_one 的 exists 检查）
cd /d I:\Darvas-box-trading
echo ==== run started %DATE% %TIME% ==== >> var\bt\base\base_run_full.log
C:\Python315\python.exe scripts\base_run.py --years 2005..2018 --mirrors A >> var\bt\base\base_run_full.log 2>&1
echo ==== run exited rc=%ERRORLEVEL% %DATE% %TIME% ==== >> var\bt\base\base_run_full.log
