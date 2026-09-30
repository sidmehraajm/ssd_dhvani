@echo off
REM ==========================================================================
REM  build.bat  --  build ssd_dhvani.exe with PyInstaller (Windows)
REM  (on a Mac use ./build.sh -- PyInstaller cannot cross-compile)
REM
REM  Run from the project root (where main.py lives):
REM      build.bat
REM
REM  Produces:  dist\ssd_dhvani_v<major>.exe  (single portable exe)
REM  Copy the exe + its config.json anywhere (e.g. an offline laptop).
REM  Set ONEFILE=False in the .spec for the folder build instead.
REM ==========================================================================
setlocal

REM  "build.bat debug" -> exe keeps a console window that shows import
REM  errors / tracebacks (use when the exe misbehaves but source works).
set "SSD_BUILD_CONSOLE="
if /i "%~1"=="debug" set "SSD_BUILD_CONSOLE=1"

echo(
echo === ssd_dhvani build =====================================
if defined SSD_BUILD_CONSOLE echo     (DEBUG build: console window enabled)
echo(

REM --- 1. Python venv -------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating virtual environment .venv ...
    py -3 -m venv .venv || python -m venv .venv
) else (
    echo [1/5] Reusing existing .venv
)
call ".venv\Scripts\activate.bat"

REM --- 2. Dependencies ------------------------------------------------------
echo [2/5] Installing dependencies ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller
if errorlevel 1 goto :fail

REM --- 3. Clean previous build ---------------------------------------------
echo [3/5] Cleaning old build\ and dist\ ...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

REM --- 4. Build ------------------------------------------------------------
echo [4/5] Running PyInstaller ...
pyinstaller ssd_dhvani.spec --noconfirm --clean
if errorlevel 1 goto :fail

REM --- 5. Stage a starter config next to the exe ---------------------------
echo [5/5] Staging config.json next to the exe ...
REM  Version comes from ssd_dhvani\__init__.py (single source of truth).
for /f "usebackq tokens=*" %%v in (`python -c "import ssd_dhvani,sys; sys.stdout.write(ssd_dhvani.VERSION_TAG)"`) do set "APPTAG=%%v"
if not defined APPTAG set "APPTAG=v1"
set "APPNAME=ssd_dhvani_%APPTAG%"
set "TARGET=dist\%APPNAME%"
if exist "dist\%APPNAME%.exe" set "TARGET=dist"
if not exist "%TARGET%\config.json" (
    if exist config.json (
        copy /y config.json "%TARGET%\config.json" >nul
    ) else (
        copy /y config.example.json "%TARGET%\config.json" >nul
    )
)

echo(
echo === DONE ===================================================
echo   Executable:  %TARGET%\%APPNAME%.exe
echo   Edit        %TARGET%\config.json  for AYON server / save root.
echo ============================================================
goto :eof

:fail
echo(
echo *** BUILD FAILED -- see messages above. ***
exit /b 1
