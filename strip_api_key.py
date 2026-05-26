"""打包前清理配置中的敏感字段。由 build.bat 自动调用。"""
import os
import yaml
import shutil

def _strip_yaml(path, keys):
    """将指定 YAML 文件中的敏感 key 替换为空字符串。"""
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    
    changed = False
    for key in keys:
        if isinstance(data, dict) and key in data:
            data[key] = ''
            changed = True
    
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

# 清理分类器配置中的 api_key
_strip_yaml('configs/classifier.yaml', ['api_key'])
# 清理 AI 分析配置中的 github_token 和敏感凭证
ai_path = 'configs/ai_analysis.yaml'
if os.path.exists(ai_path):
    with open(ai_path, 'r', encoding='utf-8') as f:
        ai_data = yaml.safe_load(f) or {}
    changed = False
    if isinstance(ai_data, dict):
        # Clean DRC credentials
        for key in ('drc_username', 'drc_password'):
            if key in ai_data:
                ai_data[key] = ''
                changed = True
        # Clean web_cookies
        if 'web_cookies' in ai_data:
            ai_data['web_cookies'] = {}
            changed = True
        # Clean collaborative_learning token
        if 'collaborative_learning' in ai_data and isinstance(ai_data['collaborative_learning'], dict):
            ai_data['collaborative_learning']['github_token'] = ''
            changed = True
    if changed:
        with open(ai_path, 'w', encoding='utf-8') as f:
            yaml.dump(ai_data, f, allow_unicode=True, default_flow_style=False)
