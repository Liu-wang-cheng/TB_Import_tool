"""更新器：版本比较与镜像判定（gui/updater.py 此前的测试空白区）"""


def test_compare_versions_basic():
    from gui.updater import compare_versions
    assert compare_versions("2.8.8", "2.8.9") == 1     # 远端更新
    assert compare_versions("2.8.8", "2.8.8") == 0
    assert compare_versions("2.8.10", "2.8.9") == -1   # 本地更新
    assert compare_versions("2.8", "2.8.0") == 1       # 位数不同


def test_compare_versions_mixed_types_no_typeerror():
    from gui.updater import compare_versions
    # "2.8.9-rc" 第三段是字符串，与 int 混比不得抛 TypeError
    assert compare_versions("2.8.8", "2.8.9-rc") == 1
    assert compare_versions("2.8.9-rc", "2.8.9-rc") == 0


def test_is_direct_github_hostname_check():
    from gui.updater import _is_direct_github
    assert _is_direct_github(
        "https://raw.githubusercontent.com/{repo}/main") is True
    # ghfast 的 base_url 内嵌 raw.githubusercontent.com，
    # 不能被子串匹配误判为直连（其 CDN 有缓存，会取到旧 version.json）
    assert _is_direct_github(
        "https://ghfast.top/https://raw.githubusercontent.com/{repo}/main"
    ) is False
    assert _is_direct_github("https://cdn.jsdelivr.net/gh/{repo}@main") is False
