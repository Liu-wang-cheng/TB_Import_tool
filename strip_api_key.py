"""打包前清理配置中的敏感字段。由 build.bat 自动调用。

注意：代码中硬编码的兜底 LLM api_key 不需要清除，这里清除的是用户个人配置中的敏感字段。
"""
import os
import yaml

# 脚本所在目录即为项目根目录（由 build.bat cd /d "%~dp0" 保证）
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _strip_classifier(path):
    """清理分类器配置中用户配置的 classifier.llm.api_key。"""
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    changed = False
    classifier = data.get('classifier')
    if isinstance(classifier, dict):
        llm = classifier.get('llm')
        if isinstance(llm, dict) and 'api_key' in llm:
            llm['api_key'] = ''
            changed = True
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def _strip_ai_analysis(path):
    """清理 AI 分析配置中的敏感凭证。"""
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    changed = False
    if isinstance(data, dict):
        for key in ('drc_username', 'drc_password'):
            if key in data:
                data[key] = ''
                changed = True
        if 'web_cookies' in data:
            data['web_cookies'] = {}
            changed = True
        cl = data.get('collaborative_learning')
        if isinstance(cl, dict):
            cl['github_token'] = ''
            changed = True
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


if __name__ == '__main__':
    cfg_dir = os.path.join(_PROJECT_ROOT, 'configs')

    # 清理分类器配置中用户配置的 classifier.llm.api_key
    _strip_classifier(os.path.join(cfg_dir, 'classifier.yaml'))

    # 清理 AI 分析配置中的敏感凭证
    _strip_ai_analysis(os.path.join(cfg_dir, 'ai_analysis.yaml'))
