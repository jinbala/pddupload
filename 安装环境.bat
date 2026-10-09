@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   安装环境（首次使用 / 换新电脑时运行一次）
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。
    echo 请先安装 Python 3.11 或更高版本，安装时勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    python -m venv .venv
) else (
    echo [1/3] 虚拟环境已存在，跳过。
)

echo [2/3] 安装依赖（playwright、pydantic）...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -e . -q

echo [3/3] 安装 Playwright 浏览器（chromium，约 150MB，需要网络）...
.venv\Scripts\python.exe -m playwright install chromium

echo.
echo ============================================
echo   完成！现在可以双击「一键启动.bat」使用。
echo   首次运行需要在浏览器里登录 17zwd 并授权超级店长店铺。
echo ============================================
pause
