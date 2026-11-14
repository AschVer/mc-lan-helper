@echo off
cd /d "%~dp0"
rem 优先用 pythonw：没有控制台窗口，程序自己会请求管理员权限
where pythonw >nul 2>nul && (start "" pythonw "mclink.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw -3 "mclink.py" & exit /b)
where python >nul 2>nul && (python "mclink.py" & exit /b)

echo Python 3 not found.
echo Install from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
pause
exit /b 1
