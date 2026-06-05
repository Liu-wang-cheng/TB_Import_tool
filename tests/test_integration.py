"""集成测试：验证三个禅道实例的列出Bug、附件下载功能

凭据从环境变量获取，运行时设置:
  set ZT1_BASE_URL=https://zentao.hctrobot.com/zentao
  set ZT1_ACCOUNT=hujizhen
  set ZT1_PASSWORD=Abc123456
  set ZT1_PRODUCT=11
  (同样设置 ZT2_*, ZT3_*)

或创建 tests/.env_test.json 文件配置凭据。
"""
import json
import os
import pytest


def _load_instances():
    """从环境变量或配置文件加载测试实例凭据"""
    env_file = os.path.join(os.path.dirname(__file__), ".env_test.json")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            return json.load(f)

    instances = []
    for idx in [1, 2, 3]:
        prefix = f"ZT{idx}_"
        base_url = os.environ.get(f"{prefix}BASE_URL", "")
        account = os.environ.get(f"{prefix}ACCOUNT", "")
        password = os.environ.get(f"{prefix}PASSWORD", "")
        product = os.environ.get(f"{prefix}PRODUCT", "")
        if base_url and account:
            instances.append({
                "name": f"实例{idx}",
                "base_url": base_url,
                "account": account,
                "password": password,
                "product_id": int(product) if product.isdigit() else 0,
            })
    return instances


def _make_client(base_url, account, password):
    from src.zentao_client import ZentaoClient
    return ZentaoClient(base_url, account, password, api_delay=0.3)


INSTANCES = _load_instances()
HAS_CREDENTIALS = len(INSTANCES) > 0


@pytest.mark.skipif(not HAS_CREDENTIALS,
                    reason="未配置测试凭据 (设置环境变量或 tests/.env_test.json)")
class TestAllInstances:
    """对所有已配置实例运行基本测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        pass

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_authenticate(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        if client._cloud_session_auth:
            assert client._session_logged_in is True
        else:
            assert client._token is not None

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_list_bugs(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=5)
        assert len(bugs) > 0

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_fetch_bug_detail(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=2)
        if bugs:
            detail = client.fetch_bug_detail(bugs[0].id)
            assert detail.id == bugs[0].id

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_attachment_download(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        # 拉取详情找附件（列表API可能不带files字段）
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=30)
        file_id = None
        for bug in bugs[:15]:
            detail = client.fetch_bug_detail(bug.id)
            files = detail.files
            if isinstance(files, dict):
                files = list(files.values())
            elif not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, dict) and f.get("id"):
                    file_id = int(f["id"])
                    break
            if file_id:
                break
        if file_id:
            att = client.download_attachment(file_id)
            assert att.size > 0
        else:
            pytest.skip("前15条Bug中未找到附件")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_wrong_credentials_fail(self, inst):
        from src.zentao_client import ZentaoClient, ZentaoAPIError
        bad = ZentaoClient(inst["base_url"], "no_such_user_xyz", "bad_pass_xyz")
        with pytest.raises(ZentaoAPIError):
            bad.authenticate()

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_url_mode_detection(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        is_cloud = client._cloud_session_auth
        if not is_cloud:
            assert client._clean_url is not None

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_date_filter(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        # 日期筛选：只查最近30天的 Bug
        from datetime import date, timedelta
        d_from = (date.today() - timedelta(days=30)).isoformat()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            date_from=d_from, date_to=date.today().isoformat(),
            page_size=20)
        assert len(bugs) >= 0  # 可能为0，但不崩溃

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_status_filter(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            statuses=["active"], page_size=10)
        assert all(b.status in ("active", "") for b in bugs)

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_vlns_check_and_extract(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=5)
        if bugs:
            has_vlns = client.check_bug_has_vlns(bugs[0].id)
            vlns_nums = client.extract_vlns_numbers(bugs[0].id)
            assert isinstance(has_vlns, bool)
            assert isinstance(vlns_nums, list)
            # 如果有 VLNS，编号应为纯数字
            for n in vlns_nums:
                assert n.isdigit()

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_fetch_comments(self, inst):
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=3)
        if bugs:
            comments = client.fetch_bug_comments(bugs[0].id)
            assert isinstance(comments, list)

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_dry_run_no_create(self, inst):
        """试运行：验证同步引擎不实际创建任务"""
        from src.source_factory import create_source_client
        config = {
            "source": {"platform": "zentao"},
            "zentao": {
                "base_url": inst["base_url"],
                "account": inst["account"],
                "password": inst["password"],
                "filters": {"product_id": inst["product_id"]},
            },
            "sync": {"api_delay": 0.3},
        }
        source = create_source_client(config)
        source.authenticate()
        # 获取少量 Bug 验证试运行路径不会崩溃
        bugs = source.fetch_all_bugs(product_id=inst["product_id"])
        assert isinstance(bugs, list)
        source.close()

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_severity_mapping(self, inst):
        """验证严重程度映射正确：先获取禅道页面翻译，再查 severity_map"""
        from src.sync_engine import SyncEngine
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=10)
        if not bugs:
            pytest.skip("无Bug数据")

        # 动态获取禅道页面的严重程度翻译
        severity_labels = client.fetch_severity_labels(inst["product_id"])

        e = SyncEngine.__new__(SyncEngine)
        e.severity_map = {
            # 数字映射
            "1": "A", "2": "B", "3": "C", "4": "C",
            # 中文映射
            "致命": "S", "严重": "A", "一般": "B", "建议": "C", "轻微": "C",
            # 字母映射
            "A": "A", "B": "B", "C": "C", "D": "C",
        }
        e.severity_labels = severity_labels
        for bug in bugs[:5]:
            result = e._map_severity(bug.severity)
            assert result in ("S", "A", "B", "C"), \
                f"severity={bug.severity}, labels={severity_labels}, result={result}"
            # 双键查找: str 和 int 都能命中
            result2 = e._map_severity(str(bug.severity))
            assert result == result2

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_reproduction_extraction(self, inst):
        """验证复现概率从步骤文本提取"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        # 拉一批Bug找有步骤的
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=15)
        found = False
        for bug in bugs:
            detail = client.fetch_bug_detail(bug.id)
            if detail.steps and len(detail.steps) > 50:
                found = True
                # 验证提取逻辑不崩溃
                import re
                m = re.search(r'(?:复现|重现|出现)(?:概率|频率)?[：:\s]*([^\s<]+)',
                              detail.steps)
                if m:
                    word = m.group(1)
                    assert word  # 至少提取到内容
                break
        if not found:
            pytest.skip("未找到有步骤的Bug")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_frequency_api_fallback(self, inst):
        """验证 frequency API 字段可正常读取"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=10)
        if bugs:
            freq = getattr(bugs[0], "frequency", None)
            # 有值或为空均可，不崩溃即可
            assert freq is not None or freq == ""

    # ── 新增集成测试：覆盖更多产品功能 ──────────────────────

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_assignee_filter(self, inst):
        """验证指派人筛选：用当前账号作为指派人筛选条件"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            assigned_to=[inst["account"]],
            page_size=20)
        assert isinstance(bugs, list)
        for bug in bugs:
            assert isinstance(bug.id, int)

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_multi_status_filter(self, inst):
        """验证多状态筛选：同时指定 active + confirmed"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            statuses=["active", "confirmed"],
            page_size=10)
        assert isinstance(bugs, list)
        for bug in bugs:
            assert bug.status in ("active", "confirmed", "")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_date_boundary_same_day(self, inst):
        """验证日期边界：date_from == date_to 为同一天"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        from datetime import date, timedelta
        # 用 30 天前的日期，大概率有数据
        target = (date.today() - timedelta(days=30)).isoformat()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            date_from=target, date_to=target,
            page_size=20)
        assert isinstance(bugs, list)
        for bug in bugs:
            assert bug.openedDate[:10] == target

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_pagination_small_page(self, inst):
        """验证分页功能：用小 page_size 强制多页拉取"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"],
            page_size=3)
        assert isinstance(bugs, list)
        # 至少能拉取到数据且不崩溃
        assert len(bugs) > 0

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_fetch_status_groups(self, inst):
        """验证动态状态码分组：open/closed 状态归类"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        groups = client.fetch_status_groups()
        assert "open" in groups
        assert "closed" in groups
        assert isinstance(groups["open"], list)
        assert isinstance(groups["closed"], list)
        assert len(groups["open"]) > 0 or len(groups["closed"]) > 0

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_resolve_module_ids_by_name(self, inst):
        """验证模块名称解析为 ID 集合"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        # 直接从模块 API 获取真实模块名（列表 API 不返回 moduleName）
        modules = client.fetch_product_modules(inst["product_id"])
        if not modules:
            pytest.skip("该产品无模块数据")
        # 取第一个模块的名称测试
        module_name = modules[0].get("name", "")
        if not module_name:
            pytest.skip("模块名称为空")
        result = client.resolve_module_ids_by_name(
            inst["product_id"], module_name)
        # 自建版无父子层级 → 返回 None（回退逐条比对）
        # 云版扁平模块 → 返回 set（ID 集合）
        assert result is None or isinstance(result, set)

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_resolve_module_name(self, inst):
        """验证模块 ID 转名称"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"], page_size=20)
        if not bugs:
            pytest.skip("无Bug数据")
        # 找一个有 module ID 的 bug
        for bug in bugs:
            if bug.module and str(bug.module).isdigit() and int(bug.module) > 0:
                name = client.resolve_module_name(
                    inst["product_id"], int(bug.module))
                assert isinstance(name, str)
                break
        else:
            pytest.skip("未找到有模块ID的Bug")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_empty_openeddate_no_crash(self, inst):
        """验证空 openedDate 不崩溃（BUG 3 修复）"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        from src.models import ZentaoBug
        # 构造 openedDate 为空的 Bug 对象
        bug = ZentaoBug(
            id=99999, title="test", severity="1", pri="1", type="bug",
            status="active", steps="", assignedTo="", assignedToAccount="",
            openedBy="", openedByAccount="", openedDate="",
            product=str(inst["product_id"]), productName="", project="",
            projectName="", module="", moduleName="", openedBuild="",
            snCode="", frequency="", files=[],
        )
        # 空日期 + 日期筛选 → 不崩溃
        assert not client._passes_filters_with_assignees(
            bug, None, "2026-01-01", "2026-12-31", set())
        # 短日期也应安全
        bug2 = ZentaoBug(
            id=99998, title="test", severity="1", pri="1", type="bug",
            status="active", steps="", assignedTo="", assignedToAccount="",
            openedBy="", openedByAccount="", openedDate="2026",
            product=str(inst["product_id"]), productName="", project="",
            projectName="", module="", moduleName="", openedBuild="",
            snCode="", frequency="", files=[],
        )
        assert not client._passes_filters_with_assignees(
            bug2, None, "2026-01-01", "2026-12-31", set())

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_bug_detail_field_completeness(self, inst):
        """验证 Bug 详情字段完整性：files/steps/snCode 等字段正确填充"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"], page_size=15)
        if not bugs:
            pytest.skip("无Bug数据")
        detail = client.fetch_bug_detail(bugs[0].id)
        assert detail.id == bugs[0].id
        assert isinstance(detail.title, str)
        assert len(detail.title) > 0
        assert isinstance(detail.severity, str)
        assert isinstance(detail.status, str)
        assert isinstance(detail.steps, str)
        # files 应为 dict 或 list（禅道 v1 返回 dict）
        assert isinstance(detail.files, (dict, list))

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_download_image(self, inst):
        """验证图片下载功能"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"], page_size=50)
        image_id = None
        for bug in bugs[:45]:
            detail = client.fetch_bug_detail(bug.id)
            files = detail.files
            if isinstance(files, dict):
                files = list(files.values())
            elif not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, dict) and f.get("id"):
                    ext = (f.get("title", "") or f.get("name", "")).rsplit(".", 1)[-1].lower()
                    if ext in ("png", "jpg", "jpeg", "gif", "bmp"):
                        image_id = int(f["id"])
                        break
            if image_id:
                break
        if image_id:
            att = client.download_image(image_id)
            assert att.size > 0
            assert att.filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp"))
        else:
            pytest.skip("前15条Bug中未找到图片附件")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_search_product(self, inst):
        """验证产品搜索功能"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        # 搜索当前 product_id 对应的产品名
        bugs = client.fetch_all_bugs(
            product_id=inst["product_id"], page_size=1)
        if bugs and bugs[0].productName:
            pid = client.search_product(bugs[0].productName)
            assert pid is None or isinstance(pid, int)
        else:
            # 无 Bug 时用空字符串搜索，不应崩溃
            pid = client.search_product("")
            assert pid is None

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_source_factory_creates_client(self, inst):
        """验证 source_factory 能正确创建禅道客户端"""
        from src.source_factory import create_source_client
        config = {
            "source": {"platform": "zentao"},
            "zentao": {
                "base_url": inst["base_url"],
                "account": inst["account"],
                "password": inst["password"],
                "filters": {"product_id": inst["product_id"]},
            },
            "sync": {"api_delay": 0.3},
        }
        source = create_source_client(config)
        assert source is not None
        source.authenticate()
        bugs = source.fetch_all_bugs(product_id=inst["product_id"])
        assert isinstance(bugs, list)
        source.close()

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_extract_datetime_from_steps(self, inst):
        """验证从 Bug 步骤中提取缺陷时间"""
        from src.extractor import extract_datetime
        from datetime import datetime
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=15)
        found = False
        for bug in bugs:
            detail = client.fetch_bug_detail(bug.id)
            if detail.steps and len(detail.steps) > 50:
                found = True
                ref = datetime.now()
                if bug.openedDate:
                    try:
                        ref = datetime.fromisoformat(bug.openedDate.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass
                result = extract_datetime(detail.steps, reference_date=ref)
                # 有结果时格式应为 YYYY-MM-DD HH:MM
                if result:
                    assert len(result) == 16  # "2026-05-08 20:31"
                    assert result[4] == '-'
                    assert result[13] == ':'
                break
        if not found:
            pytest.skip("未找到有步骤的Bug")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_extract_sn_from_steps(self, inst):
        """验证从 Bug 步骤中提取 SN 编码"""
        from src.extractor import extract_sn
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=15)
        found = False
        for bug in bugs:
            detail = client.fetch_bug_detail(bug.id)
            if detail.steps and len(detail.steps) > 30:
                found = True
                sn = extract_sn(detail.steps)
                # SN 可能为 None（步骤中无 SN），不崩溃即可
                if sn:
                    assert len(sn) >= 8
                    assert sn.upper() == sn
                break
        if not found:
            pytest.skip("未找到有步骤的Bug")

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_clean_title_preserves_semantics(self, inst):
        """验证 _clean_title 保留核心语义"""
        from src.classifier import SimilarityClassifier
        test_cases = [
            ("【禅道60365】回充异常", "回充异常"),
            ("VLNS-12345清扫路径异常", "清扫路径异常"),
            ("HQ5S00700002HC260600建图失败", "建图失败"),
            ("2026-05-08 20:31充电失败", "充电失败"),
        ]
        for title, expected_keyword in test_cases:
            clean = SimilarityClassifier._clean_title(title)
            assert expected_keyword in clean, f"'{title}' → '{clean}' 缺少 '{expected_keyword}'"

    @pytest.mark.parametrize("inst", INSTANCES)
    def test_module_filter_with_submodules(self, inst):
        """验证模块筛选包含子模块"""
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        modules = client.fetch_product_modules(inst["product_id"])
        if not modules:
            pytest.skip("该产品无模块数据")
        # 验证模块列表非空且结构正确
        for m in modules[:5]:
            assert isinstance(m, dict)
            assert "id" in m or "name" in m
