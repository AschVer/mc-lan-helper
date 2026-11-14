@echo off
cd /d "%~dp0"
if exist "%LOCALAPPDATA%\Programs\Python\Python3*\pythonw.exe" goto :pyw
where pythonw >nul 2>nul && (start "" pythonw "mclink.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw -3 "mclink.py" & exit /b)
where python >nul 2>nul && (python "mclink.py" & exit /b)
echo Python 3 not found. Install from https://www.python.org/downloads/
pause
exit /b 1
:pyw
start "" pythonw "mclink.py"
exit /b
