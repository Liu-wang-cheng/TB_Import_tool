"""外部 Teambition → 内部 Teambition 调试脚本

核心：登录态管理（Cookie 复用 + 失效自动扫码登录 + 自动抓 Cookie）

流程：
  1. 读已保存的 Cookie（tools/.tb_cookie.txt），调 /users/me 验证
  2. 有效 → 直接用
  3. 失效 → Selenium 打开登录页，尝试账号密码自动登录
     - 成功 → 提取 Cookie
     - 失败 → 浏览器停住，提示用户钉钉扫码，轮询检测登录成功
  4. 自动提取并保存 Cookie

用法：
    # 只做登录态管理（拿到有效 Cookie）
    python tools/tb_source_debug.py --account "手机号" --password "密码"

    # 登录后拉取指定项目任务
    python tools/tb_source_debug.py --account "手机号" --password "密码" --project-id 6306e205882430143f837be2
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

logger = logging.getLogger(__name__)


def _log(msg: str):
    """同时输出到 stdout（CLI 场景）和 logger（GUI 日志区）"""
    print(msg)
    logger.info(msg)


WEB_API = "https://www.teambition.com/api"
LOGIN_URL = "https://account.teambition.com/login"


def _default_cookie_file() -> str:
    """Cookie 文件默认路径：打包后 exe 同级 tools/，源码环境项目根 tools/"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tools", ".tb_cookie.txt")


COOKIE_FILE = _default_cookie_file()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def parse_cookie(cookie_str: str) -> dict:
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def check_cookie_valid(cookie_str: str) -> bool:
    """用 /users/me 验证 Cookie 是否有效"""
    if not cookie_str:
        return False
    cookies = parse_cookie(cookie_str)
    try:
        resp = requests.get(
            f"{WEB_API}/users/me", cookies=cookies,
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("name", "?")
            print(f"  [Cookie 有效] 当前用户: {name}")
            return True
        print(f"  [Cookie 失效] HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [Cookie 检查失败] {e}")
    return False


def login_via_selenium(account: str, password: str, timeout: int = 300,
                       headless: bool = False) -> str:
    """Selenium 自动登录 + 扫码兜底，返回 Cookie 字符串

    headless=True 时后台自动登录（不显示浏览器窗口）；
    自动登录失败则回退到可见浏览器供钉钉扫码。
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.edge.options import Options

    def _make_driver(hd: bool):
        options = Options()
        if hd:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
        return webdriver.Edge(options=options)

    driver = _make_driver(headless)
    try:
        login_start = time.time()
        _log(f"\n[打开浏览器] {LOGIN_URL}" + ("（后台模式）" if headless else ""))
        driver.get(LOGIN_URL)
        time.sleep(3)

        # ── 尝试账号密码自动登录 ──
        auto_login_ok = False
        if account and password:
            _log("[尝试] 账号密码自动登录")
            auto_login_ok = _try_auto_login(driver, account, password)

        # ── 自动登录失败时，headless 回退到可见浏览器供扫码 ──
        if not auto_login_ok:
            if headless:
                driver.quit()
                _log("[回退] 后台自动登录失败，切换到可见浏览器供扫码登录...")
                driver = _make_driver(False)
                driver.get(LOGIN_URL)
                time.sleep(3)
            _log("\n" + "=" * 60)
            _log(" 自动登录失败/未提供账号密码，请在浏览器中完成登录（钉钉扫码）")
            _log(" 脚本会每 2 秒检测一次登录状态，登录成功后自动继续...")
            _log("=" * 60)

        # ── 轮询检测登录成功 ──
        cookie_str = _wait_for_login(driver, timeout, login_start)
        if cookie_str:
            _save_cookie(cookie_str)
        return cookie_str
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _try_auto_login(driver, account, password) -> bool:
    """尝试账号密码自动登录，返回是否成功"""
    try:
        # 1. 点「其他登录」
        _click_text(driver, "其他登录")
        # 2. 点「账号密码登录」（可能已经是密码登录页）
        _click_text(driver, "账号密码登录")
        # 3. 勾选「同意」（radio，用 JS 点击）
        _check_agreement(driver)
        # 4. 填账号密码
        inputs = driver.find_elements("tag name", "input")
        acc_input = pwd_input = None
        for el in inputs:
            itype = (el.get_attribute("type") or "text").lower()
            ph = (el.get_attribute("placeholder") or "").lower()
            if itype == "password":
                pwd_input = el
            elif itype in ("text", "tel", "email", "number") and not acc_input:
                if not any(k in ph for k in ("验证码", "captcha", "code")):
                    acc_input = el
        if not acc_input or not pwd_input:
            _log("  [自动登录] 未找到账号/密码输入框")
            return False
        acc_input.clear(); acc_input.send_keys(account)
        pwd_input.clear(); pwd_input.send_keys(password)
        # 5. 点「立即开始」
        _click_text(driver, "立即开始")
        time.sleep(4)
        # 判断是否登录成功
        return "login" not in driver.current_url
    except Exception as e:
        _log(f"  [自动登录异常] {e}")
        return False


def _click_text(driver, text):
    """按文本点击元素（XPath 全文搜索）"""
    try:
        els = driver.find_elements(
            "xpath", f"//*[contains(normalize-space(text()), '{text}')]")
        for el in els:
            try:
                el.click()
                _log(f"  [点击] {text!r}")
                time.sleep(2)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _check_agreement(driver):
    """勾选「同意」协议 radio（勾选框在同意左边）"""
    try:
        # 找「同意」元素的父容器里的 radio
        els = driver.find_elements(
            "xpath", "//*[contains(normalize-space(text()), '同意')]")
        for el in els:
            node = el
            for _ in range(5):
                try:
                    radio = node.find_element(
                        "xpath", ".//input[@type='radio']")
                    driver.execute_script("arguments[0].click();", radio)
                    _log("  [勾选] 同意协议")
                    time.sleep(1)
                    return True
                except Exception:
                    pass
                try:
                    node = node.find_element("xpath", "..")
                except Exception:
                    break
    except Exception as e:
        _log(f"  [勾选异常] {e}")
    return False


def _wait_for_login(driver, timeout: int, start_time: float = None) -> str:
    """轮询检测登录成功（URL 离开 login 页 + 出现 TB_ACCESS_TOKEN），返回 Cookie

    start_time 传入时，耗时从登录流程开始（打开浏览器）起算，而非本轮询函数。
    """
    start = start_time if start_time is not None else time.time()
    while time.time() - start < timeout:
        try:
            cookies = driver.get_cookies()
            names = [c["name"] for c in cookies]
            has_token = "TB_ACCESS_TOKEN" in names
            moved = "login" not in driver.current_url
            if has_token and moved:
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                _log(f"\n[登录成功] 检测到 TB_ACCESS_TOKEN，已用 {int(time.time()-start)} 秒")
                return cookie_str
        except Exception:
            pass
        time.sleep(2)
    _log(f"\n[超时] {timeout} 秒内未检测到登录成功")
    return ""


def _save_cookie(cookie_str: str):
    try:
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        _log(f"[保存] Cookie → {COOKIE_FILE}")
    except Exception as e:
        _log(f"[保存失败] {e}")


def load_saved_cookie() -> str:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def fetch_tasks(cookie_str: str, project_id: str):
    """拉取项目任务列表"""
    cookies = parse_cookie(cookie_str)
    print(f"\n[拉取任务] 项目 {project_id}")
    try:
        resp = requests.get(
            f"{WEB_API}/projects/{project_id}/tasks",
            cookies=cookies,
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=30,
        )
        print(f"  HTTP {resp.status_code}")
        if resp.status_code == 200:
            tasks = resp.json()
            print(f"  共 {len(tasks)} 条任务")
            for t in tasks[:5]:
                print(f"    - [{t.get('_id','')[:12]}] {t.get('content','')[:50]}"
                      f" (isDone={t.get('isDone')})")
            return tasks
        else:
            print(f"  body: {resp.text[:300]}")
    except Exception as e:
        print(f"  [失败] {e}")
    return None


def main():
    parser = argparse.ArgumentParser(description="外部 Teambition 登录态管理 + 拉取任务调试")
    parser.add_argument("--account", help="账号（手机号/邮箱）")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--project-id", help="项目 ID（可选，登录后拉取任务）")
    parser.add_argument("--force-login", action="store_true", help="跳过 Cookie 缓存，强制重新登录")
    args = parser.parse_args()

    cookie_str = "" if args.force_login else load_saved_cookie()

    if cookie_str and check_cookie_valid(cookie_str):
        print("使用已保存的有效 Cookie")
    else:
        if cookie_str:
            print("已保存 Cookie 失效，重新登录...")
        if not args.account and not args.password:
            print("未提供账号密码，将只打开网页让你扫码登录")
        cookie_str = login_via_selenium(args.account or "", args.password or "")

    if not cookie_str:
        print("\n登录失败，无法获取 Cookie")
        sys.exit(1)

    if args.project_id:
        fetch_tasks(cookie_str, args.project_id)
    else:
        print("\n提示：加 --project-id 参数可拉取指定项目的任务列表")


if __name__ == "__main__":
    main()
