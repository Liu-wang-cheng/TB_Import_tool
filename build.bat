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
pip install pyyaml requests pyjwt beautifulsoup4 PyQt6 scikit-learn jieba flask oss2 opencv-python-headless
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

REM 清理 __pycache__，避免 PyInstaller 用旧 .pyc 缓存（mtime 异常时尤其重要）
echo [CLEAN] Removing __pycache__ directories ...
for /d /r %%i in (__pycache__) do (
    if exist "%%i" rmdir /s /q "%%i"
)
echo [OK] __pycache__ cleaned
echo.

REM Strip api_key before packaging
REM ⚠ 备份必须放在 configs 之外：spec 的 datas 会递归打包整个 configs 目录，
REM   若 .bak 留在 configs\ 内，其中的真实密钥会被打进 zip 发布到公开 Release
echo [CLEAN] Stripping api_key from config ...
set "STRIP_BAK=%TEMP%\tb_import_strip_bak"
if not exist "%STRIP_BAK%" mkdir "%STRIP_BAK%"
copy /y "configs\classifier.yaml" "%STRIP_BAK%\classifier.yaml.bak" >nul
copy /y "configs\ai_analysis.yaml" "%STRIP_BAK%\ai_analysis.yaml.bak" >nul
REM 兜底：清掉历史构建可能残留在 configs 的 .bak
del /q "configs\classifier.yaml.bak" "configs\ai_analysis.yaml.bak" 2>nul
python strip_api_key.py
echo [OK] api_key stripped
echo.

echo [BUILD] Starting ...
echo.
REM --clean: 清 PyInstaller 自己的 build/ 缓存（pyc、Analysis-00.toc、PYZ 等）
pyinstaller --clean --noconfirm zentao2teambition.spec
set BUILD_RESULT=%errorlevel%

REM Restore original configs
copy /y "%STRIP_BAK%\classifier.yaml.bak" "configs\classifier.yaml" >nul
copy /y "%STRIP_BAK%\ai_analysis.yaml.bak" "configs\ai_analysis.yaml" >nul
del /q "%STRIP_BAK%\classifier.yaml.bak" "%STRIP_BAK%\ai_analysis.yaml.bak" >nul 2>&1

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

REM Zip the onedir output（用 Python zipfile，避免 PowerShell Compress-Archive 模块缺失）
echo [ZIP] Creating release package...
if exist "dist\%EXE_NAME%.zip" del "dist\%EXE_NAME%.zip"
python -c "import zipfile,os;src=r'dist\%EXE_NAME%';out=r'dist\%EXE_NAME%.zip';zf=zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED);[zf.write(os.path.join(r,f),os.path.relpath(os.path.join(r,f),src)) for r,_,fs in os.walk(src) for f in fs];zf.close()"
if %errorlevel% neq 0 (
    echo [ERROR] Zip failed
    goto :fail
)
echo [OK] Created dist\%EXE_NAME%.zip
echo.

REM 发布前安全校验：包内不得含 .bak（历史事故：备份文件把真实密钥带进了公开 Release）
python -c "import zipfile,sys;z=zipfile.ZipFile(r'dist\%EXE_NAME%.zip');bad=[n for n in z.namelist() if n.lower().endswith('.bak')];print('[FAIL] 包内发现 .bak: '+','.join(bad)) if bad else print('[OK] 包内无 .bak 残留');sys.exit(1 if bad else 0)"
if %errorlevel% neq 0 (
    echo [ERROR] 安全校验未通过，已中止发布
    goto :fail
)
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
