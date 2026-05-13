"""一键发布脚本：打包后自动上传 exe 到 GitHub Release 并更新 version.json

用法：
    python release.py <version> <exe_filename> <release_exe_name>
    python release.py 1.4 "智能缺陷管理平台.exe" "智能缺陷管理平台.exe"

或由 build.bat 自动调用。

前置条件：
    1. SSH key 已配置（git push 用）
    2. ~/.github_token 文件存在（GitHub API 用），或设置环境变量 GITHUB_TOKEN
       获取 token: https://github.com/settings/tokens/new?scopes=repo
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

import requests
import yaml


def get_github_token() -> str:
    """读取 GitHub token（优先环境变量，其次 ~/.github_token 文件）"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    token_path = os.path.expanduser("~/.github_token")
    if os.path.exists(token_path):
        with open(token_path, "r") as f:
            return f.read().strip()
    return ""


def get_repo_from_config() -> str:
    """从 configs/update.yaml 读取 repository"""
    config_path = os.path.join(os.path.dirname(__file__), "configs", "update.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("repository", "")
    return ""


def compute_sha256(filepath: str) -> str:
    """计算文件 SHA256"""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def create_release(repo: str, token: str, version: str, notes: str) -> int:
    """创建 GitHub Release，返回 release_id"""
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json"}
    r = requests.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        json={
            "tag_name": f"v{version}",
            "target_commitish": "main",
            "name": f"v{version}",
            "body": notes,
            "draft": False,
            "prerelease": False,
        },
    )
    if r.status_code == 422:
        # tag 已存在，尝试获取已有 release
        r2 = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/v{version}",
            headers=headers,
        )
        if r2.status_code == 200:
            return r2.json()["id"]
    r.raise_for_status()
    return r.json()["id"]


def upload_asset(repo: str, token: str, release_id: int,
                 filepath: str, asset_name: str) -> str:
    """上传文件到 Release，返回 browser_download_url"""
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json"}
    # 先删除同名旧 asset（如果存在）
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases/{release_id}/assets",
        headers=headers,
    )
    for asset in r.json():
        if asset["name"] == asset_name:
            requests.delete(
                f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}",
                headers=headers,
            )
            print(f"  [DEL] 旧 asset {asset_name} 已删除")

    # 上传
    file_size = os.path.getsize(filepath)
    print(f"  [UPLOAD] {asset_name} ({file_size / 1048576:.0f}MB)...")
    upload_headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(file_size),
    }
    with open(filepath, "rb") as f:
        r = requests.post(
            f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets"
            f"?name={asset_name}",
            headers=upload_headers,
            data=f,
        )
    r.raise_for_status()
    return r.json()["browser_download_url"]


def update_version_json(repo: str, token: str, version: str,
                        sha256: str, download_url: str, notes: str):
    """克隆仓库 → 更新 version.json + README.md → 推送"""
    version_file_path = "version.json"
    readme_src = os.path.join(os.path.dirname(__file__), "README.md")
    version_data = {
        "version": version,
        "sha256": sha256,
        "download_url": download_url,
        "release_date": time.strftime("%Y-%m-%d"),
        "release_notes": notes,
        "min_version": "1.0",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_url = f"git@github.com:{repo}.git"
        try:
            subprocess.run(["git", "clone", "--depth", "1", repo_url, tmpdir],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] git clone 失败:\n{e.stderr.decode('utf-8', errors='replace')}")
            sys.exit(1)

        # 写入 version.json
        vpath = os.path.join(tmpdir, version_file_path)
        os.makedirs(os.path.dirname(vpath), exist_ok=True)
        with open(vpath, "w", encoding="utf-8") as f:
            json.dump(version_data, f, indent=2, ensure_ascii=False)

        # 同步 README.md
        files_to_add = [version_file_path]
        if os.path.exists(readme_src):
            readme_dst = os.path.join(tmpdir, "README.md")
            with open(readme_src, "r", encoding="utf-8") as f:
                readme_content = f.read()
            with open(readme_dst, "w", encoding="utf-8") as f:
                f.write(readme_content)
            files_to_add.append("README.md")
            print(f"  [SYNC] README.md updated")

        # commit & push
        subprocess.run(["git", "config", "user.name", "release-bot"],
                       cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email",
                        "release-bot@users.noreply.github.com"],
                       cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "add"] + files_to_add,
                       cwd=tmpdir, check=True, capture_output=True)
        # 检查是否有变更
        diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                     cwd=tmpdir, capture_output=True)
        if diff_result.returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            f"release v{version}: update version.json & README"],
                           cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "main"],
                           cwd=tmpdir, check=True, capture_output=True)
        else:
            print(f"  [GIT] version.json & README 无变化，跳过 commit")

        # 推 tag（已存在则强制更新）
        subprocess.run(["git", "tag", "-f", f"v{version}"],
                       cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "push", "-f", "origin", f"v{version}"],
                       cwd=tmpdir, capture_output=True)

    print(f"  [GIT] version.json + README pushed, tag v{version} pushed")


def main():
    if len(sys.argv) < 4:
        print("用法: python release.py <version> <exe_filename> <release_exe_name>")
        print('示例: python release.py 1.4 "智能缺陷管理平台.exe" "智能缺陷管理平台.exe"')
        sys.exit(1)

    version = sys.argv[1]
    exe_filename = sys.argv[2]
    release_exe_name = sys.argv[3]

    token = get_github_token()
    if not token:
        print("[ERROR] GitHub token 未配置")
        print("  方式1: set GITHUB_TOKEN=ghp_xxx")
        print("  方式2: 创建 ~/.github_token 文件写入 token")
        print("  获取: https://github.com/settings/tokens/new?scopes=repo")
        sys.exit(1)

    repo = get_repo_from_config()
    if not repo:
        print("[ERROR] configs/update.yaml 中未配置 repository")
        sys.exit(1)

    exe_path = os.path.join("dist", exe_filename)
    if not os.path.exists(exe_path):
        print(f"[ERROR] {exe_path} 不存在")
        sys.exit(1)

    # 读取更新说明（从用户输入或使用默认）
    notes = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else f"v{version} release"

    print(f"[1/4] 计算 SHA256...")
    sha256 = compute_sha256(exe_path)
    print(f"  SHA256: {sha256}")

    print(f"[2/4] 创建 GitHub Release v{version}...")
    release_id = create_release(repo, token, version, notes)
    print(f"  Release ID: {release_id}")

    print(f"[3/4] 上传 {release_exe_name} ({os.path.getsize(exe_path)/1048576:.0f}MB)...")
    download_url = upload_asset(repo, token, release_id, exe_path, release_exe_name)
    print(f"  URL: {download_url}")

    print(f"[4/4] 更新 version.json 并推送...")
    update_version_json(repo, token, version, sha256, download_url, notes)

    print()
    print("=" * 50)
    print(f"  Release v{version} published!")
    print(f"  Download: {download_url}")
    print("=" * 50)


if __name__ == "__main__":
    main()
