"""测试更新 bat 模板：不得嵌入绝对/中文路径（GBK 系统读 UTF-8 bat 会乱码）"""
import os
import tempfile
import shutil

from gui.workers import _generate_updater_bat


def make_fake_dir():
    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "智能缺陷管理平台.exe"), "w").close()
    return tmp


class TestUpdaterBatNoAbsoluteChinesePath:
    def test_bat_uses_dynamic_paths_only(self):
        tmp = make_fake_dir()
        try:
            extract = os.path.join(tmp, "_update_extracted")
            bat = _generate_updater_bat(
                tmp, os.path.join(extract, "智能缺陷管理平台"),
                12345, extract)
            # 不嵌入安装绝对路径
            assert tmp not in bat
            # 不嵌入中文 exe 名作为路径引用
            assert '"智能缺陷管理平台.exe"' not in bat
            # 全部动态解析
            assert "cd /d \"%~dp0\"" in bat
            assert "%NEW_DIR%" in bat
            assert "%EXE_NAME%" in bat
        finally:
            shutil.rmtree(tmp)

    def test_bat_locates_new_exe_in_extract_dir(self):
        tmp = make_fake_dir()
        try:
            extract = os.path.join(tmp, "_update_extracted")
            os.makedirs(os.path.join(extract, "智能缺陷管理平台"), exist_ok=True)
            bat = _generate_updater_bat(
                tmp, os.path.join(extract, "智能缺陷管理平台"),
                12345, extract)
            # 递归遍历 _update_extracted 找新 exe，而非写死路径
            assert 'for /r "_update_extracted" %%f in (*.exe)' in bat
            assert 'set "NEW_DIR=%%~dpf"' in bat
        finally:
            shutil.rmtree(tmp)
