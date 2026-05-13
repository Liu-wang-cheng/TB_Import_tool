# 智能缺陷管理平台 (TB Import Tool)

将禅道（Zentao）Bug 缺陷自动同步到 Teambition（TB），支持按条件筛选、去重、双向标题标注、字段映射、附件和评论同步。

## 功能特性

- **自动同步**：禅道 Bug → Teambition 缺陷任务，一键执行
- **智能去重**：精确标签匹配 + 标题模糊匹配，避免重复创建
- **字段映射**：严重程度(S/A/B/C)、复现概率、缺陷分类自动映射
- **附件同步**：禅道附件自动上传到 Teambition 任务
- **AI 分类**：TF-IDF 相似度 + LLM 大模型 + 部门兜底，四层分类管道
- **SN 提取**：自动从重现步骤中提取设备 SN 编码
- **双向标注**：TB 标题加 `【禅道{id}】`，禅道标题加 `【VLNS-xxxxx】`
- **钉钉通知**：同步结果自动推送到钉钉群
- **自动更新**：启动时检测新版本，镜像站自动测速选最快，一键更新

## 使用方式

### GUI 界面（推荐）

双击 exe 启动，图形界面操作。

### 命令行

```bash
# 列出禅道 Bug
python main.py --list-bugs --status active

# 试运行（不实际创建任务）
python main.py --dry-run

# 正式同步
python main.py

# 查询 TB 组织/项目 ID
python tools/query_ids.py
```

## 配置

配置文件位于 `configs/` 目录：

| 文件 | 内容 |
|------|------|
| `zentao.yaml` | 禅道服务器地址、账号、Bug 筛选条件 |
| `teambition.yaml` | TB 应用凭证、项目、字段映射 |
| `sync.yaml` | 同步规则、去重阈值、附件设置 |
| `classifier.yaml` | AI 分类器配置（TF-IDF / LLM） |
| `dingtalk.yaml` | 钉钉通知设置 |
| `update.yaml` | 自动更新镜像站配置 |

## 版本历史

详见 [Releases](https://github.com/Liu-wang-cheng/TB_Import_tool/releases)
