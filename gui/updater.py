"""自动更新核心逻辑：镜像测速、版本比对、下载、校验、自替换"""

import hashlib
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# 默认镜像站（base_url 用于获取仓库文件，download_prefix 用于加速 Release 下载）
DEFAULT_MIRRORS = [
    {"name": "jsdelivr",
     "base_url": "https://cdn.jsdelivr.net/gh/{repo}@main",
     "download_prefix": ""},
    {"name": "ghfast",
     "base_url": "https://ghfast.top/https://raw.githubusercontent.com/{repo}/main",
     "download_prefix": "https://ghfast.top/"},
    {"name": "github",
     "base_url": "https://raw.githubusercontent.com/{repo}/main",
     "download_prefix": ""},
]


@dataclass
class MirrorResult:
    """镜像测速结果"""
    name: str
    base_url: str
    download_prefix: str = ""
    latency_ms: float = -1
    success: bool = False


@dataclass
class VersionInfo:
    """远程版本信息"""
    version: str = ""
    release_date: str = ""
    sha256: str = ""
    download_url: str = ""  # 完整的 GitHub Release URL
    release_notes: str = ""
    min_version: str = "0.0"


def read_current_version() -> str:
    """读取本地 VERSION 文件"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    version_path = os.path.join(base, "VERSION")
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0"


def parse_version_info(data: dict) -> VersionInfo:
    """将 version.json 的 dict 解析为 VersionInfo"""
    return VersionInfo(
        version=data.get("version", ""),
        release_date=data.get("release_date", ""),
        sha256=data.get("sha256", ""),
        download_url=data.get("download_url", ""),
        release_notes=data.get("release_notes", ""),
        min_version=data.get("min_version", "0.0"),
    )


def compare_versions(current: str, remote: str) -> int:
    """比较语义化版本号。返回 1=remote更新, 0=相同, -1=current更新"""
    def _parts(v):
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(p)
        return parts
    c, r = _parts(current), _parts(remote)
    for a, b in zip(c, r):
        if a < b:
            return 1
        if a > b:
            return -1
    if len(c) < len(r):
        return 1
    if len(c) > len(r):
        return -1
    return 0


def build_download_url(release_url: str, download_prefix: str) -> str:
    """根据镜像前缀构建加速下载 URL

    Args:
        release_url: 完整的 GitHub Release URL
            如 https://github.com/owner/repo/releases/download/v1.3/xxx.exe
        download_prefix: 镜像加速前缀，如 https://ghfast.top/
            留空表示直连
    """
    if not download_prefix:
        return release_url
    return f"{download_prefix}{release_url}"


def _test_one_mirror(mirror: dict, version_file: str,
                     timeout: float = 5.0) -> MirrorResult:
    """测试单个镜像的响应速度"""
    url = f"{mirror['base_url']}/{version_file}"
    prefix = mirror.get("download_prefix", "")
    try:
        t0 = time.monotonic()
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (200, 302):
            latency = (time.monotonic() - t0) * 1000
            return MirrorResult(mirror["name"], mirror["base_url"],
                                prefix, latency, True)
        t0 = time.monotonic()
        resp = requests.get(url, timeout=timeout, allow_redirects=True,
                            headers={"Range": "bytes=0-0"})
        latency = (time.monotonic() - t0) * 1000
        ok = resp.status_code in (200, 206)
        return MirrorResult(mirror["name"], mirror["base_url"],
                            prefix, latency if ok else -1, ok)
    except Exception:
        return MirrorResult(mirror["name"], mirror["base_url"], prefix, -1, False)


def race_mirrors(mirrors: list, version_file: str,
                 timeout: float = 5.0) -> List[MirrorResult]:
    """并发测试所有镜像，返回按速度排序的结果列表（成功优先，失败排后）"""
    results = []
    with ThreadPoolExecutor(max_workers=max(len(mirrors), 1)) as pool:
        futures = {
            pool.submit(_test_one_mirror, m, version_file, timeout): m
            for m in mirrors
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                m = futures[fut]
                results.append(MirrorResult(m["name"], m["base_url"],
                                            m.get("download_prefix", ""),
                                            -1, False))
    results.sort(key=lambda r: (not r.success, r.latency_ms if r.latency_ms > 0 else 99999))
    return results


def fetch_version_info(sorted_mirrors: List[MirrorResult],
                       version_file: str) -> Optional[Tuple[VersionInfo, MirrorResult]]:
    """按镜像速度顺序尝试获取 version.json。返回 (VersionInfo, MirrorResult) 或 None"""
    for mirror in sorted_mirrors:
        if not mirror.success:
            continue
        url = f"{mirror.base_url}/{version_file}"
        try:
            resp = requests.get(url, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                return (parse_version_info(data), mirror)
        except Exception as e:
            logger.warning("从 %s 获取版本信息失败: %s", mirror.name, e)
    return None


def download_exe(download_url: str, dest_path: str,
                 progress_callback=None) -> bool:
    """下载 exe 文件，支持进度回调。progress_callback(downloaded, total, speed_str)"""
    try:
        resp = requests.get(download_url, stream=True, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        t_start = time.monotonic()

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    elapsed = time.monotonic() - t_start
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    if total > 0:
                        speed_str = f"{speed / 1048576:.1f} MB/s"
                        progress_callback(downloaded, total, speed_str)
                    else:
                        progress_callback(downloaded, 0,
                                          f"{downloaded / 1048576:.1f} MB")

        if progress_callback:
            progress_callback(total if total > 0 else downloaded,
                              total if total > 0 else downloaded, "下载完成")
        return True
    except Exception as e:
        logger.error("下载失败: %s", e)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise


def verify_sha256(file_path: str, expected_hash: str) -> bool:
    """验证文件 SHA256"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest().lower() == expected_hash.lower()


def generate_updater_bat(current_exe: str, new_exe: str, pid: int) -> str:
    """生成自替换 bat 脚本内容"""
    exe_dir = os.path.dirname(current_exe)
    exe_name = os.path.basename(current_exe)
    return f"""@echo off
chcp 65001 >nul 2>&1
title 正在更新 智能缺陷管理平台

echo ============================================
echo   正在更新，请勿关闭此窗口
echo ============================================
echo.

REM 等待原进程退出（最多 30 秒）
set WAITED=0
:wait_loop
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if %errorlevel% equ 0 (
    set /a WAITED+=1
    if %WAITED% geq 30 (
        echo [ERROR] 原进程未能退出，更新取消
        goto :cleanup
    )
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

echo [OK] 原进程已退出
echo.

REM 替换 exe
echo 正在替换程序文件...
if exist "{current_exe}.bak" del /f /q "{current_exe}.bak"
ren "{current_exe}" "{exe_name}.bak"
if %errorlevel% neq 0 (
    echo [WARN] 无法重命名旧文件，尝试直接覆盖...
    copy /y "{new_exe}" "{current_exe}"
    if %errorlevel% neq 0 (
        echo [ERROR] 更新失败，请手动替换
        echo 新文件位置: {new_exe}
        pause
        goto :cleanup
    )
    del /f /q "{new_exe}"
    goto :start_new
)

copy /y "{new_exe}" "{current_exe}"
if %errorlevel% neq 0 (
    echo [ERROR] 复制新文件失败，正在回滚...
    ren "{current_exe}.bak" "{exe_name}"
    echo [OK] 已回滚到旧版本
    pause
    goto :cleanup
)

del /f /q "{new_exe}"
del /f /q "{current_exe}.bak"

echo [OK] 文件替换成功
echo.

:start_new
echo 正在启动新版本...
start "" "{current_exe}"

:cleanup
(goto) 2>nul & del /f /q "%~f0"
"""
