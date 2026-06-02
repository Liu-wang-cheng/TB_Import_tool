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
        bugs = client.fetch_all_bugs(product_id=inst["product_id"], page_size=10)
        file_id = None
        for bug in bugs:
            files = bug.files
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
            pytest.skip("未找到有附件的 Bug")

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
            # 非云版应能检测 URL 模式
            assert client._clean_url is not None
