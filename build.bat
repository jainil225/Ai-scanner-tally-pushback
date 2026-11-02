@echo off
REM Builds dist\AIBillScanner.exe (one file). Needs: pip install pyinstaller
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name AIBillScanner --add-data "index.html;." --add-data "samples;samples" scanner.py
echo.
echo Built dist\AIBillScanner.exe
pause
