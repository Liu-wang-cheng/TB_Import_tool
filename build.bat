@echo off
title Build ^& Release

REM 从 VERSION 文件读取版本号（唯一版本源）
set /p VERSION=<VERSION
set EXE_NAME=智能缺陷管理平台

echo ============================================
echo   Build %EXE_NAME% (onedir mode)
echo ============================================
echo.

cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found
    goto :fail
)
python --version
echo.

python -c "import PyInstaller" 2>nul
if %errorlevel% neq 0 (
    echo [INSTALL] PyInstaller ...
    pip install pyinstaller
)
echo [OK] PyInstaller
echo.

echo [INSTALL] Dependencies ...
pip install pyyaml requests pyjwt beautifulsoup4 PyQt6 scikit-learn jieba flask oss2
if %errorlevel% neq 0 (
    echo [ERROR] Dependencies install failed
    goto :fail
)
echo [OK] Dependencies
echo.

REM Check TF-IDF training data
if exist "data\classifier_model.pkl" (
    echo [OK] TF-IDF training data found
) else (
    echo [WARN] TF-IDF training data not found, will auto-fetch on first run
)
echo.

if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build

REM Strip api_key before packaging
echo [CLEAN] Stripping api_key from config ...
copy /y "configs\classifier.yaml" "configs\classifier.yaml.bak" >nul
python strip_api_key.py
echo [OK] api_key stripped
echo.

echo [BUILD] Starting ...
echo.
pyinstaller --noconfirm zentao2teambition.spec
set BUILD_RESULT=%errorlevel%

REM Restore original config
copy /y "configs\classifier.yaml.bak" "configs\classifier.yaml" >nul
del "configs\classifier.yaml.bak" >nul 2>&1

if %BUILD_RESULT% neq 0 (
    echo.
    echo [ERROR] Build failed
    goto :fail
)

echo.
echo ============================================
echo   Build success!
echo   Output: dist\%EXE_NAME%\
echo ============================================
echo.
dir "dist\%EXE_NAME%\%EXE_NAME%.exe" 2>nul
echo.

REM Zip the onedir output
echo [ZIP] Creating release package...
if exist "dist\%EXE_NAME%.zip" del "dist\%EXE_NAME%.zip"
powershell -Command "Compress-Archive -Path 'dist\%EXE_NAME%\*' -DestinationPath 'dist\%EXE_NAME%.zip' -Force"
if %errorlevel% neq 0 (
    echo [ERROR] Zip failed
    goto :fail
)
echo [OK] Created dist\%EXE_NAME%.zip
echo.

REM Auto Release to GitHub Release

echo ============================================
echo   Auto Release to GitHub
echo ============================================
echo.

python release.py %VERSION% "dist\%EXE_NAME%.zip" "SmartDefectPlatform.zip"
if %errorlevel% neq 0 (
    echo.
    echo [WARN] Release failed. Files are ready in dist\
    echo        You can manually upload to GitHub Release.
)

echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
