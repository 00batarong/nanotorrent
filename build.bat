@echo off
cd /d "%~dp0"
echo Installing build dependencies...
py -3.12 -m pip install libtorrent PySide6 pyinstaller
if errorlevel 1 goto fail

echo Building nanotorrent.exe...
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed --name nanoTorrent --icon nanotorrent.ico --collect-all libtorrent nanotorrent.py
if errorlevel 1 goto fail

echo.
echo Done! Your exe is at: %~dp0dist\nanotorrent.exe
pause
exit /b 0

:fail
echo.
echo Build failed. Read for any errors.
pause
exit /b 1
