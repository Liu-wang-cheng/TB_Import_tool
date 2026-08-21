"""共享工具函数"""

import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


def get_app_data_dir(subdir: str = "data") -> str:
    """获取应用程序数据持久化目录。

    - 打包后：exe 所在目录下的 subdir/
    - 开发环境：项目根目录下的 subdir/
    """
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, subdir)
    os.makedirs(path, exist_ok=True)
    return path


def apply_module_filter(bugs, module_filter: str,
                        fetch_detail_fn: Optional[Callable] = None,
                        progress_fn: Optional[Callable[[int, int], None]] = None,
                        module_id_set: Optional[set] = None,
                        max_workers: int = 5,
                        treat_digit_as_name: bool = False):
    """根据 module_filter 在内存中筛选 Bug 列表。

    - 数字（模块ID）：默认直接比对 bug.module，零额外请求。
      设置 treat_digit_as_name=True 时跳过该快路径，进入名称子串匹配
      （用于 sync_engine 的"数字当名称"兜底场景）。
    - 名称（模块名）：
      - 若 module_id_set 已由调用方预解析（含空集合），按 ID 集合过滤，零额外请求。
      - 否则用线程池并发调用 fetch_detail_fn(bug_id) 拉详情再做子串匹配。
        默认并发 5，比串行快约 5 倍；服务端压力大可调小。
    progress_fn(current, total) 在并发路径上按完成数回调用于 UI 进度更新。
    """
    if not module_filter:
        return bugs
    mf = module_filter.strip()
    if not mf or not bugs:
        return bugs

    if mf.isdigit() and not treat_digit_as_name:
        return [b for b in bugs if str(b.module) == mf]

    # 调用方已通过模块 API 预解析了名称→ID集合（空集合也视为预解析成功）
    if module_id_set is not None:
        return [b for b in bugs if str(b.module) in module_id_set]

    if fetch_detail_fn is None:
        return bugs

    total = len(bugs)
    workers = max(1, min(max_workers, total))

    # 串行模式（workers=1 或调试方便）
    if workers == 1:
        out = []
        for i, bug in enumerate(bugs, 1):
            if progress_fn:
                try:
                    progress_fn(i, total)
                except Exception:
                    pass
            try:
                full_bug = fetch_detail_fn(bug.id)
            except Exception as e:
                logger.warning("获取 Bug#%d 详情失败: %s", bug.id, e)
                continue
            if full_bug and mf in (full_bug.moduleName or ""):
                bug.moduleName = full_bug.moduleName
                out.append(bug)
        return out

    # 并发拉取详情，结果按 bug.id 暂存以便保持原顺序
    details: dict = {}

    def _fetch(bug):
        try:
            return bug.id, fetch_detail_fn(bug.id)
        except Exception as e:
            logger.warning("获取 Bug#%d 详情失败: %s", bug.id, e)
            return bug.id, None

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch, b) for b in bugs]
        for fut in as_completed(futures):
            bid, full_bug = fut.result()
            details[bid] = full_bug
            completed += 1
            if progress_fn and (completed % 10 == 0 or completed == total):
                try:
                    progress_fn(completed, total)
                except Exception:
                    pass

    out = []
    for bug in bugs:
        full_bug = details.get(bug.id)
        if full_bug and mf in (full_bug.moduleName or ""):
            bug.moduleName = full_bug.moduleName
            out.append(bug)
    return out


def resolve_assigned_to(filters: dict, zentao_account: str = "") -> list:
    """将 assigned_to 配置解析为用户名列表，"me" 替换为当前账号

    对带部门前缀的名字（如 "IOT-陈斌"）追加去前缀形式，用于自适应匹配；
    非部门前缀的账号名（如 "乐动开发-343"）保持完整不拆分。
    """
    val = filters.get("assigned_to")
    if val is None:
        return None
    if isinstance(val, str):
        val = [val]
    # 兼容 YAML 把纯数字字符串解析成 int 的情况（如 "343" → 343）
    val = [str(x) for x in val if x not in (None, "")]

    result = []
    for name in val:
        if name.lower() == "me":
            if zentao_account:
                result.append(zentao_account)
        else:
            result.append(name)
            # 只对白名单部门前缀去前缀（"IOT-陈斌"→"陈斌"），"乐动开发-343" 不拆
            prefix = extract_department_prefix(name)
            if prefix:
                suffix = name.split("-", 1)[1].strip()
                if suffix and suffix not in result:
                    result.append(suffix)
    return result if result else None


# 部门前缀白名单：指派人筛选时的自适应去前缀只认这些前缀。
# 与 teambition.yaml 的 assignee_category_map key 对应，
# 另含禅道云版的通用前缀 "部门"（如 "部门-邓建和"）。
# 非白名单前缀（如账号名 "乐动开发-343"）保持完整，避免误拆。
DEPARTMENT_PREFIXES = frozenset({
    "IOT", "应用", "嵌入式", "算法", "项目", "测试", "硬件", "驱动", "产品",
    "部门",
})


def extract_department_prefix(assigned_to: str) -> str:
    """从 assignedTo 名称中提取部门前缀（仅白名单前缀）

    "IOT-陈斌" → "IOT"，"应用-罗林旺" → "应用"
    非白名单前缀或无前缀时返回空字符串
    """
    if assigned_to and "-" in assigned_to:
        prefix = assigned_to.split("-", 1)[0].strip()
        if prefix in DEPARTMENT_PREFIXES:
            return prefix
    return ""


def normalize_zentao_filters(filters: dict) -> dict:
    """将新配置格式中的 product/project 映射为 product_id/project_id（数字ID直接转换）

    新 configs/zentao.yaml 中使用 `product: 11` 而非 `product_id: 11`，
    此函数在调用 fetch_all_bugs 前统一转换为旧格式。
    """
    # product → product_id
    product = filters.get("product")
    if product and "product_id" not in filters:
        if isinstance(product, int):
            filters["product_id"] = product
        elif isinstance(product, str) and product.isdigit():
            filters["product_id"] = int(product)

    # project → project_id
    project = filters.get("project")
    if project and "project_id" not in filters:
        if isinstance(project, int):
            filters["project_id"] = project
        elif isinstance(project, str) and project.isdigit():
            filters["project_id"] = int(project)

    return filters


def parse_zentao_url(url: str) -> dict:
    """从禅道 Bug 页面 URL 中解析产品ID、项目ID、模块ID、分支ID

    支持的 URL 格式（大小写不敏感）：
      新版（PATH_INFO）：
        bug-browse-{产品ID}-{分支ID}-{浏览方式}-{参数}.html
        bug-browse-{产品ID}-{分支ID}.html
        bug-browse-{产品ID}.html
        bug-browse-{产品ID}--byModule-{模块ID}.html
        project-bug-{项目ID}.html
      旧版（查询参数）：
        ?m=bug&f=browse&productID=324
        ?m=bug&f=browse&productid=381
        ?m=bug&f=browse&product=381
        ?m=bug&f=browse&root=381&type=byModule&param=2091
        ?m=project&f=bug&projectID=5

    返回: {"product_id": int|None, "project_id": int|None, "module_id": int|None,
            "branch_id": int|None, "base_url": str|None}
    """
    result = {"product_id": None, "project_id": None, "module_id": None,
              "branch_id": None, "base_url": None}
    if not url:
        return result

    # ── 新版 PATH_INFO 格式 ──
    m = re.search(r'bug-browse-(\d+)', url, re.IGNORECASE)
    if m:
        result["product_id"] = int(m.group(1))

    m = re.search(r'project-bug-(\d+)', url, re.IGNORECASE)
    if m:
        result["project_id"] = int(m.group(1))

    # 模块ID：仅当有 byModule 前缀时解析
    m = re.search(r'byModule-(\d+)', url, re.IGNORECASE)
    if m:
        result["module_id"] = int(m.group(1))

    # 分支ID：bug-browse-{产品ID}-{分支ID}(-...).html（第二个数字，非 byModule 前缀）
    if not result["module_id"]:
        m = re.search(r'bug-browse-\d+-(\d+)', url, re.IGNORECASE)
        if m and m.group(1) != "0":
            result["branch_id"] = int(m.group(1))

    # ── 旧版查询参数格式（大小写不敏感）──
    # 产品ID：productID / productid / product / root
    m = re.search(r'[?&](?:productID|product|root)=(\d+)', url, re.IGNORECASE)
    if m:
        result["product_id"] = int(m.group(1))

    # 项目ID：projectID / projectid / project
    m = re.search(r'[?&](?:projectID|project)=(\d+)', url, re.IGNORECASE)
    if m:
        result["project_id"] = int(m.group(1))

    # 模块ID：browseType=byModule 或 type=byModule &param={模块ID}
    if re.search(r'[?&](?:browseType|type)=byModule', url, re.IGNORECASE):
        m = re.search(r'[?&]param=(\d+)', url, re.IGNORECASE)
        if m:
            result["module_id"] = int(m.group(1))

    # ── 提取 base_url ──
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path
        m = re.match(r'(.+?)/(?:bug-browse|project-bug|index\.php)', path, re.IGNORECASE)
        if m:
            base = f"{parsed.scheme}://{parsed.netloc}{m.group(1)}"
        elif 'index.php' in path.lower():
            idx = path.lower().rfind('/index.php')
            base = f"{parsed.scheme}://{parsed.netloc}{path[:idx]}"
        else:
            base = f"{parsed.scheme}://{parsed.netloc}"
        result["base_url"] = base.rstrip('/')

    return result
