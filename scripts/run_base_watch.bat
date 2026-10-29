@echo off
rem 新基线逐年监视器 —— 跑完一年立即出该年统计（2026-10-29）
rem 与 base_run.py 并行、只读年库；14 年全齐后自行退出
rem 注意：stdout 重定向到 .out.log，不能与 python 自己写的 base_watch.log 同名（Windows 文件锁）
cd /d I:\Darvas-box-trading
C:\Python315\python.exe scripts\base_watch.py --interval 60 >> var\bt\base\base_watch.out.log 2>&1
