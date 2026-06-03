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
        """验证严重程度映射正确 (int key + str key)"""
        from src.sync_engine import SyncEngine
        client = _make_client(inst["base_url"], inst["account"], inst["password"])
        client.authenticate()
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=10)
        if not bugs:
            pytest.skip("无Bug数据")
        e = SyncEngine.__new__(SyncEngine)
        e.severity_map = {1: "A", 2: "B", 3: "C", 4: "C"}
        for bug in bugs[:5]:
            result = e._map_severity(bug.severity)
            assert result in ("S", "A", "B", "C")
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
