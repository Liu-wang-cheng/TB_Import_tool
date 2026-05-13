"""查询 Teambition org_id 和 project_id 的辅助脚本"""

import http.server
import json
import jwt
import requests
import sys
import threading
import time
import urllib.parse
import webbrowser

import os

API_BASE = "https://open.teambition.com/api"

# 从环境变量读取凭证；如未设置则从 config 文件读取
APP_ID = os.getenv("TB_APP_ID", "")
APP_SECRET = os.getenv("TB_APP_SECRET", "")

if not APP_ID or not APP_SECRET:
    # 尝试从项目 config 文件读取
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    try:
        from src.config_loader import load_configs
        cfg = load_configs()
        tb_cfg = cfg.get("teambition", {})
        APP_ID = tb_cfg.get("app_id", "")
        APP_SECRET = tb_cfg.get("app_secret", "")
    except Exception:
        pass

auth_code_holder = {"code": None}
event = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            auth_code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>授权成功！</h2>"
                "<p>请回到命令行查看结果。</p></body></html>".encode("utf-8")
            )
            event.set()
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *args):
        pass


def get_user_token():
    app_token = jwt.encode(
        {"_appId": APP_ID, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        APP_SECRET, algorithm="HS256",
    )

    port = 8765
    server = None
    for p in range(port, port + 10):
        try:
            server = http.server.HTTPServer(("localhost", p), Handler)
            port = p
            break
        except OSError:
            continue

    redirect_uri = f"http://localhost:{port}/callback"
    auth_url = (
        f"https://open.teambition.com/oauth2/authorize"
        f"?app_id={APP_ID}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
    )

    if server:
        print(f"\n正在打开浏览器进行 Teambition 授权...")
        print(f"如果未自动打开，请访问:\n{auth_url}\n")
        webbrowser.open(auth_url)
        event.wait(timeout=300)
        server.server_close()
        code = auth_code_holder["code"]
    else:
        print(f"\n请在浏览器中打开:\n{auth_url}")
        code = input("授权后粘贴回调URL中的code参数: ").strip()

    if not code:
        print("未获取到授权码")
        sys.exit(1)

    resp = requests.post(
        f"{API_BASE}/oauth2/token",
        json={"grantType": "authorizationCode", "code": code, "expires": 86400},
        headers={"Authorization": f"Bearer {app_token}"},
        timeout=30,
    )
    data = resp.json()
    result = data.get("result", data)
    token = result.get("userAccessToken") or result.get("access_token")
    if not token:
        print(f"获取token失败: {json.dumps(data, ensure_ascii=False)[:300]}")
        sys.exit(1)
    return token


def query_info(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 1. 查询用户信息（获取 org_id）
    print("\n=== 用户信息 ===")
    resp = requests.get(f"{API_BASE}/v3/user/info", headers=headers, timeout=15)
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2)[:800])

    # 从用户信息提取 org_id
    org_id = None
    result = data.get("result", data)
    if isinstance(result, dict):
        orgs = result.get("orgs", result.get("organizations", []))
        if orgs:
            org_id = orgs[0].get("id") or orgs[0].get("orgId")
            print(f"\n>>> 检测到 org_id: {org_id}")
            print(f"    组织名: {orgs[0].get('name', '')}")

    # 2. 尝试获取用户所属组织
    if not org_id:
        print("\n=== 尝试其他方式获取组织 ===")
        for path in ["/v3/user/org", "/v3/auth/org"]:
            resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=15)
            d = resp.json()
            print(f"{path}: {json.dumps(d, ensure_ascii=False)[:300]}")
            if d.get("result"):
                r = d["result"]
                if isinstance(r, list) and r:
                    org_id = r[0].get("id") or r[0].get("orgId")
                    break
                elif isinstance(r, dict):
                    org_id = r.get("id") or r.get("orgId")
                    break

    if not org_id:
        print("\n未能自动获取 org_id，可能需要在 Teambition 开放平台查看")
        print("请在 Teambition 项目页面 URL 中查看，格式如:")
        print("  https://www.teambition.com/project/{project_id}")
        org_id = input("\n请手动输入 org_id (或按回车跳过): ").strip() or None

    # 3. 查询项目列表
    if org_id:
        headers["X-Tenant-Id"] = str(org_id)
        headers["X-Tenant-Type"] = "organization"

    print("\n=== 项目列表 ===")
    for path in [
        "/v3/project/search",
        "/v3/project/list",
        "/v3/org/project/list",
    ]:
        try:
            resp = requests.post(
                f"{API_BASE}{path}",
                headers=headers,
                json={"pageSize": 50},
                timeout=15,
            )
            d = resp.json()
            if d.get("result"):
                print(f"\n{path} 成功:")
                result = d["result"]
                if isinstance(result, list):
                    for proj in result[:20]:
                        pid = proj.get("id") or proj.get("projectId", "")
                        name = proj.get("name", "")
                        print(f"  项目: {name}  ID: {pid}")
                elif isinstance(result, dict):
                    projects = result.get("projects", result.get("list", [result]))
                    if isinstance(projects, list):
                        for proj in projects[:20]:
                            pid = proj.get("id") or proj.get("projectId", "")
                            name = proj.get("name", "")
                            print(f"  项目: {name}  ID: {pid}")
                break
        except Exception as e:
            print(f"{path}: {e}")

    # 4. 汇总
    print("\n" + "=" * 50)
    print("查询结果汇总（请填入 config.yaml）:")
    print(f"  org_id: {org_id}")
    print("=" * 50)


if __name__ == "__main__":
    if not APP_ID or not APP_SECRET:
        print("错误: 未配置 APP_ID 和 APP_SECRET")
        print("请设置环境变量 TB_APP_ID 和 TB_APP_SECRET，")
        print("或确保 configs/teambition.yaml 中已配置 app_id 和 app_secret")
        sys.exit(1)
    print("Teambition ID 查询工具")
    print("=" * 40)
    token = get_user_token()
    print("Token 获取成功!")
    query_info(token)
