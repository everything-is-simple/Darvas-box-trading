@echo off
REM Darvas-box-trading 构建脚本（单文件可执行）
REM 依赖：Go 1.22+（纯Go SQLite驱动，无需CGO）
setlocal
cd /d %~dp0\..
go build -o darvas.exe .\cmd\darvas
if errorlevel 1 (
    echo 构建失败
    exit /b 1
)
echo 构建成功: darvas.exe
echo 用法:
echo   darvas.exe doctor              体检数据库
echo   darvas.exe ingest --date YYYY-MM-DD   增量导入
echo   darvas.exe ingest --since full       全量导入
endlocal
