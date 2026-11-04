"""配置对话框路径解析：不得依赖进程工作目录（计划任务启动时为 System32）"""
import os


def test_relative_config_dir_ignores_cwd(tmp_path, monkeypatch):
    from gui.config_dialog import resolve_config_dir
    monkeypatch.chdir(tmp_path)
    resolved = resolve_config_dir("configs")
    expected = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "configs")
    assert os.path.isabs(resolved)
    assert os.path.normcase(resolved) == os.path.normcase(expected)


def test_absolute_config_dir_passthrough(tmp_path):
    from gui.config_dialog import resolve_config_dir
    assert resolve_config_dir(str(tmp_path)) == str(tmp_path)
