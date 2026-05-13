# 智能缺陷管理平台

多平台缺陷自动同步到 Teambition（TB），支持禅道（Zentao）、Jira 等平台接入，一键完成筛选、去重、字段映射、附件和评论同步。

## 功能特性

- **多平台支持**：禅道 / Jira 缺陷一键同步到 Teambition，GUI 界面自由切换平台
- **智能去重**：精确标签匹配 + 标题模糊匹配，避免重复创建
- **字段映射**：严重程度(S/A/B/C)、复现概率、缺陷分类自动映射
- **附件同步**：源平台附件自动上传到 Teambition 任务
- **AI 分类**：TF-IDF 相似度 + LLM 大模型 + 部门兜底，四层分类管道
- **SN 提取**：自动从重现步骤中提取设备 SN 编码
- **双向标注**：TB 标题加 `【源平台{id}】`，源平台标题加 `【VLNS-xxxxx】`
- **钉钉通知**：同步结果自动推送到钉钉群
- **自动更新**：启动时检测新版本，镜像站自动测速选最快，一键更新

## 使用方式

### GUI 界面（推荐）

双击 exe 启动，图形界面操作。支持在界面上切换禅道/Jira 平台。

### 命令行

```bash
# 列出 Bug（禅道）
python main.py --list-bugs --status active

# 列出 Bug（Jira）
python main.py --list-bugs --platform jira

# 试运行（不实际创建任务）
python main.py --dry-run

# 正式同步
python main.py

# 查询 TB 组织/项目 ID
python tools/query_ids.py
```

## 配置

配置文件位于 `configs/` 目录，首次使用请复制 `.example` 模板并填写实际值：

```bash
cp configs/zentao.yaml.example configs/zentao.yaml
cp configs/teambition.yaml.example configs/teambition.yaml
```

| 文件 | 内容 |
|------|------|
| `source.yaml` | 选择缺陷来源平台（zentao / jira） |
| `zentao.yaml` | 禅道服务器地址、账号、Bug 筛选条件 |
| `jira.yaml` | Jira 服务器地址、账号、JQL 查询条件 |
| `teambition.yaml` | TB 应用凭证、项目、字段映射 |
| `classifier.yaml` | AI 分类器配置（TF-IDF / LLM） |
| `dingtalk.yaml` | 钉钉通知设置 |
| `update.yaml` | 自动更新镜像站配置 |

## 项目结构

```
gui_main.py              → GUI 入口
gui/                     → PyQt5/6 图形界面（平台切换、筛选、日志）
src/
  sync_engine.py         → 核心同步逻辑（去重、映射、附件）
  source_factory.py      → 平台工厂（禅道/Jira 自动选择）
  zentao_client.py       → 禅道 API 客户端
  zentao_adapter.py      → 禅道适配器
  source_client.py       → Jira 客户端
  classifier.py          → AI 缺陷分类器
  teambition_client.py   → Teambition API 客户端
dingtalk/                → 钉钉通知集成
tools/                   → 辅助工具（ID 查询、Bug 导出等）
docs/                    → 使用文档
```

## 版本历史

详见 [Releases](https://github.com/Liu-wang-cheng/TB_Import_tool/releases)
