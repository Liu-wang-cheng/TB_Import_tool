"""
探测 Teambition 文件(work)下载 URL 格式

用法:
    python tools/probe_work_url.py                  # 列出最近任务的附件 work 记录
    python tools/probe_work_url.py <work_id>        # 查询指定 work_id 的文件信息
    python tools/probe_work_url.py --upload-test    # 上传测试图片并探测 URL

目的: 找到 work_id → 可用于 <img src="..."> 的下载 URL 映射关系。
"""

import json
import logging
import os
import sys

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config_loader import load_configs
from src.teambition_client import TeambitionClient

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def make_client() -> TeambitionClient:
    """从配置文件创建已认证的 TeambitionClient"""
    config = load_configs("configs")
    tb_cfg = config["teambition"]
    sync_cfg = config.get("sync", {})

    project_id = tb_cfg.get("project_id", "")
    if not project_id:
        project_cfg = tb_cfg.get("project", {})
        project_id = project_cfg.get("id", "") or project_cfg.get("project_id", "")

    fallback_id = tb_cfg.get("creator_id") or tb_cfg.get("operator_id")

    client = TeambitionClient(
        app_id=tb_cfg["app_id"],
        app_secret=tb_cfg["app_secret"],
        org_id=tb_cfg["org_id"],
        project_id=project_id,
        api_delay=sync_cfg.get("api_delay", 0.5),
        # token_cache 不是 TeambitionClient 构造参数，probe 工具每次重新认证
        scenariofieldconfig_id=tb_cfg.get("scenariofieldconfig_id"),
        operator_id=fallback_id,
    )
    client.authenticate()
    return client


def probe_work_by_id(client: TeambitionClient, work_id: str):
    """查询指定 work_id 的文件信息，尝试多个 API 路径"""
    paths_to_try = [
        f"/v3/work/{work_id}",
        f"/v3/work/info?workId={work_id}",
        f"/v3/work/query?workId={work_id}",
        f"/work/info?workId={work_id}",
    ]

    for path in paths_to_try:
        print(f"\n--- 尝试 GET {path} ---")
        try:
            data = client._request("GET", path)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"  失败: {e}")

    # 尝试 POST 路径
    post_paths = [
        ("/v3/work/list", {"workIds": [work_id]}),
        ("/v3/work/batchQuery", {"workIds": [work_id]}),
    ]
    for path, body in post_paths:
        print(f"\n--- 尝试 POST {path} ---")
        try:
            data = client._request("POST", path, json=body)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"  失败: {e}")


def list_task_works(client: TeambitionClient):
    """列出项目中最近任务的附件（work）列表"""
    print("\n=== 查询项目最近任务 ===")
    tasks_data = client._request(
        "GET", f"/v3/project/{client.project_id}/task/query",
        params={"pageSize": 5},
    )
    result = tasks_data.get("result", [])
    if isinstance(result, dict):
        result = result.get("list", [result])

    if not result:
        print("未找到任务")
        return

    for t in result[:3]:
        task_id = t.get("taskId") or t.get("id", "")
        content = t.get("content", "")
        note = t.get("note", "")
        print(f"\n--- 任务: {content[:60]} (ID: {task_id}) ---")

        # 查询任务的附件
        work_paths = [
            f"/v3/work/list?taskId={task_id}",
            f"/v3/work/query?taskId={task_id}&scope=task&scopeId={task_id}",
        ]
        for path in work_paths:
            try:
                data = client._request("GET", path)
                works = data.get("result", [])
                if isinstance(works, dict):
                    works = works.get("list", [works])
                if works:
                    print(f"  GET {path} => {len(works)} 个附件")
                    for w in works:
                        print(json.dumps(w, ensure_ascii=False, indent=4))
                else:
                    print(f"  GET {path} => 无附件")
            except Exception as e:
                print(f"  GET {path} => 失败: {e}")

        # 如果 note 中有 img 标签，打印出来
        if "<img" in (note or ""):
            print(f"  Note 包含 img 标签:")
            import re
            imgs = re.findall(r'<img[^>]+>', note)
            for img_tag in imgs:
                print(f"    {img_tag}")


def upload_test_image(client: TeambitionClient):
    """上传一张测试图片，打印 work/create 响应的完整结构"""
    from src.models import AttachmentFile

    # 创建一个 1x1 红色 PNG 图片
    import struct
    import zlib

    def make_1x1_png():
        """生成最小的 1x1 红色 PNG"""
        # PNG 签名
        signature = b'\x89PNG\r\n\x1a\n'
        # IHDR
        ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
        ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
        # IDAT - 红色像素 RGB(255,0,0)
        raw_data = b'\x00\xff\x00\x00'  # filter byte + RGB
        compressed = zlib.compress(raw_data)
        idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
        idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)
        # IEND
        iend_crc = zlib.crc32(b'IEND') & 0xffffffff
        iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
        return signature + ihdr + idat + iend

    png_data = make_1x1_png()

    # 先找一个任务 ID
    tasks_data = client._request(
        "GET", f"/v3/project/{client.project_id}/task/query",
        params={"pageSize": 1},
    )
    result = tasks_data.get("result", [])
    if isinstance(result, dict):
        result = result.get("list", [result])
    if not result:
        print("没有可用的测试任务")
        return

    task_id = result[0].get("taskId") or result[0].get("id", "")
    print(f"\n使用测试任务: {task_id}")

    att = AttachmentFile(
        filename="test_inline_image.png",
        data=png_data,
        content_type="image/png",
        size=len(png_data),
    )

    # Step 1: 获取上传凭证
    print("\n=== Step 1: upload-token ===")
    token_data = client._request("POST", "/v3/awos/upload-token",
                                  json={"category": "attachment",
                                        "fileName": att.filename,
                                        "fileType": att.content_type,
                                        "fileSize": att.size,
                                        "scope": "task",
                                        "scopeId": task_id})
    print(json.dumps(token_data, ensure_ascii=False, indent=2))

    result_data = token_data.get("result", {})
    sdk = result_data.get("sdk", {})
    credentials = sdk.get("credentials", {})
    upload_info = result_data.get("upload", {})
    file_token = result_data.get("token", "")
    bucket = upload_info.get("Bucket", "")
    object_key = upload_info.get("Key", "")

    print(f"\nbucket: {bucket}")
    print(f"object_key: {object_key}")
    print(f"file_token: {file_token}")

    # Step 2: 上传到 OSS
    print("\n=== Step 2: OSS 上传 ===")
    import oss2
    auth = oss2.StsAuth(
        credentials["accessKeyId"],
        credentials["secretAccessKey"],
        credentials["sessionToken"],
    )
    endpoint = "oss-cn-zhangjiakou.aliyuncs.com"
    bucket_obj = oss2.Bucket(auth, f"https://{endpoint}", bucket)
    bucket_obj.put_object(object_key, png_data)
    print("OSS 上传成功")

    # Step 3: 创建 work 记录
    print("\n=== Step 3: work/create ===")
    work_data = client._request("POST", "/v3/work/create", json={
        "projectId": client.project_id,
        "taskId": task_id,
        "fileTokens": [file_token],
    })
    print(json.dumps(work_data, ensure_ascii=False, indent=2))

    # 提取 work_id
    work_result = work_data.get("result", [])
    work_id = ""
    if isinstance(work_result, list) and work_result:
        work_id = work_result[0].get("id", "")
        print(f"\nwork_id: {work_id}")
        # 打印完整的第一条记录
        print("完整 work 记录:")
        print(json.dumps(work_result[0], ensure_ascii=False, indent=2))

    # Step 4: 查询 work 信息
    if work_id:
        print(f"\n=== Step 4: 查询 work 信息 ===")
        probe_work_by_id(client, work_id)

    # Step 5: 尝试构造 URL 并测试
    print("\n=== Step 5: 尝试常见 URL 模式 ===")
    url_patterns = [
        f"https://striker.teambition.net/download/{object_key}",
        f"https://striker.teambition.net/{object_key}",
        f"https://{bucket}.oss-cn-zhangjiakou.aliyuncs.com/{object_key}",
        f"https://open.teambition.com/api/v3/work/{work_id}/download",
        f"https://open.teambition.com/api/v3/work/download?workId={work_id}",
    ]
    for url in url_patterns:
        print(f"\n  测试: {url}")
        try:
            import requests as req
            headers = client._get_headers()
            resp = req.get(url, headers=headers, timeout=10, allow_redirects=False)
            print(f"  状态码: {resp.status_code}")
            print(f"  Headers: {dict(resp.headers)}")
            if resp.status_code in (301, 302):
                print(f"  重定向到: {resp.headers.get('Location', 'N/A')}")
            elif resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                print(f"  Content-Type: {ct}")
                print(f"  Body 大小: {len(resp.content)} bytes")
        except Exception as e:
            print(f"  失败: {e}")

    # 清理: 删除测试附件（如果可能）
    print(f"\n测试完成。work_id={work_id}, object_key={object_key}")
    print("请手动在 Teambition 中删除测试附件，或保留用于后续调试。")


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--upload-test":
            client = make_client()
            upload_test_image(client)
        else:
            # 假设是 work_id
            client = make_client()
            probe_work_by_id(client, arg)
    else:
        client = make_client()
        list_task_works(client)


if __name__ == "__main__":
    main()
