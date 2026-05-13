"""打包前清除 classifier.yaml 中的 api_key（保留原始格式和注释）"""
import re

path = "configs/classifier.yaml"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

with open(path, "w", encoding="utf-8") as f:
    for line in lines:
        if line.strip().startswith("api_key:"):
            # 只替换值部分，保留缩进和 key
            f.write(re.sub(r'(api_key:\s*)\S+', r'\1', line))
        else:
            f.write(line)
