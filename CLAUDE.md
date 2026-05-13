# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供项目指导。

## 项目概述

Python GUI/CLI 工具，将禅道（Zentao，自建开源版）的 Bug 缺陷同步到 Teambition（TB）。
面向扫地机器人软件测试工程师，支持按条件筛选、去重、双向标题标注、字段映射、附件和评论同步。

## 常用命令

```bash
# 安装依赖
pip install pyyaml requests pyjwt beautifulsoup4 oss2 PyQt6 scikit-learn jieba

# 列出禅道 Bug（不需要 TB 认证）
python main.py --list-bugs --status active

# 试运行（不实际创建任务）
python main.py --dry-run

# 正式同步
python main.py

# 仅测试 TB 认证
python main.py --auth-only

# 查询 TB 组织/项目 ID
python tools/query_ids.py

# 启动 GUI 界面
python gui_main.py

# 打包为 exe
build.bat
```

## 项目结构

```
gui_main.py                  → GUI 入口：日志初始化、Qt 窗口启动
gui/
  main_window.py             → 主窗口：筛选面板、操作按钮、日志区、进度条
  config_dialog.py           → 配置对话框：分 tab 编辑禅道/TB/同步/分类器/钉钉配置
  workers.py                 → QThread 异步任务：连接测试、列出Bug、同步、附件上传
  yaml_utils.py              → YAML 原地编辑：保留注释，支持插入缺失 key
  qt_compat.py               → PyQt5/6 兼容层
  log_handler.py             → Qt 日志 Handler：将日志重定向到界面日志区
  resources/style.qss        → 界面样式表

main.py                      → CLI 入口（argparse），配置加载，客户端初始化
src/
  models.py                  → 数据类：ZentaoBug、TeambitionTask、AttachmentFile、SyncResult、SyncStats
  zentao_client.py           → 禅道 REST API v1：Token 认证 + Session 认证（用于文件下载）
  teambition_client.py       → TB 开放 API：JWT appToken 认证、任务 CRUD、文件上传到 OSS
  sync_engine.py             → 核心同步逻辑：去重、字段映射、双向标题同步、附件和评论同步
  classifier.py              → 缺陷分类器：TF-IDF 相似度（本地学习）+ LLM + 部门兜底
                               SimilarityClassifier: jieba分词 + TF-IDF + 余弦相似度
                               BugClassifier: 四层管道 + AI审核训练数据 + 部门过滤
  utils.py                   → 工具函数：URL 解析、指派人解析、部门前缀提取
  config_loader.py           → 多文件配置加载：合并 configs/ 目录下的 YAML
  config_resolver.py         → 配置解析：认证后将中文名称解析为 ID（项目、自定义字段、用户）

dingtalk/
  bot.py                     → 钉钉机器人：HMAC 加签、Markdown/文本消息、同步结果通知
  server.py                  → 钉钉回调服务：@机器人指令触发同步/列出Bug

tools/
  query_ids.py               → 独立脚本：通过 OAuth 查询 TB 组织 ID / 项目 ID

configs/
  zentao.yaml                → 禅道服务器地址、账号、Bug 筛选条件
  teambition.yaml            → TB 应用凭证、项目、字段映射、自定义字段
  dingtalk.yaml              → 钉钉 Webhook 和通知设置
  sync.yaml                  → 同步规则：标题标注格式、去重阈值、附件设置
  classifier.yaml            → 缺陷分类规则：TF-IDF 配置（阈值/训练数量/缓存目录）、LLM 配置、分类描述

data/                        → TF-IDF 模型缓存（classifier_model.pkl，自动创建）
                               含样本数据、TF-IDF 矩阵、词表、保存时间戳

zentao2teambition.spec       → PyInstaller 打包配置：自动包含 data/ 目录
build.bat                    → Windows 打包脚本：安装依赖、检查训练数据、构建 exe

docs/
  tutorial.html              → 使用教程（纯 HTML+CSS 模拟界面截图）
```

## 关键技术细节

- **禅道双认证**：REST API 用 Token 认证（`/api.php/v1/tokens`）；文件下载用 Session 认证（`/api-getsessionid.json` → `/user-login.json`）
- **禅道 v1 奇怪之处**：Token 接口返回 HTTP 201；`files` 字段是 `{id: obj}` 字典而非列表；`assignedTo`/`status` 可能是嵌套字典
- **TB 认证**：JWT 签名的 appAccessToken。企业应用安装后可直接使用应用级 token，无需 OAuth 用户 token
- **去重策略**：第一层 = 精确搜索 `【禅道{id}】` 标签；第二层 = `difflib.SequenceMatcher` 标题模糊匹配（阈值 0.8）
- **双向标题标注**：TB 任务标题加 `【禅道{id}】` 前缀；禅道 Bug 标题加 `【VLNS-xxxxx】` 前缀
- **自定义字段**：严重程度(S/A/B/C)、复现概率、缺陷分类等 — 通过 `scenariofieldconfig` API 自动检测，或从配置中的中文名称解析
- **配置解析**：`config_resolver.py` 认证后将中文名称解析为 ID。支持：`scenariofieldconfig_name` → ID、`customfields` 名称 → ID、`creator_name` → 用户 ID。`project_id` 必须直接填写 UUID（appToken 模式下无项目搜索 API）
- **多文件配置**：`config_loader.py` 加载并合并 `configs/` 目录下的 YAML（兼容旧版单文件 `config.yaml`）
- **严重程度映射**：禅道 1-4 → TB S/A/B/C
- **分类管道（四层）**：① TF-IDF 相似度（本地学习）→ ② LLM 大模型分类 → ③ LLM 审核（兜底前 AI 复核）→ ④ 部门兜底（按指派人部门归入"其他问题"）
- **TF-IDF 相似度分类**：使用 jieba 中文分词 + scikit-learn TfidfVectorizer + 余弦相似度。首次运行扫描 TB 最新 N 条缺陷任务（`max_fetch` 可配置，默认 5000），缓存到 `data/classifier_model.pkl`。超过 7 天自动增量学习最新 500 条。无需 GPU
- **AI 审核训练数据**：训练/增量学习后，LLM 抽检每个分类的样本，剔除不合理分类并重新训练，确保模型质量
- **部门过滤**：有明确部门的执行者（IOT/算法/应用/嵌入式/硬件/驱动）只匹配本部门分类；项目/测试/产品部门不限制
- **TB 任务详情与列表差异**：列表 API `/v3/project/{pid}/task/query` 不返回 `customfields` 值，需逐条调 `/v3/task/query` 获取详情。自定义字段值格式为 `[{"id":"xxx","title":"显示名"}]`，取 `title` 字段
- **附件上传超时**：按文件大小动态计算（最低 100KB/s），范围 120-900 秒，避免大文件卡死
- **模块过滤**：先探测禅道模块 API 是否支持层级查询，支持则 BFS 展开子模块，不支持则回退逐条详情匹配
- **钉钉通知**：同步结果通过 Markdown 表格发送，包含 TB 所属项目、处理数量、耗时等
- **打包**：`build.bat` 一键打包，自动包含 `data/` 目录下的训练模型。若模型不存在，打包后首次运行自动从 TB 拉取训练

## 依赖

- **核心**：pyyaml, requests, pyjwt, beautifulsoup4, oss2
- **GUI**：PyQt6（兼容 PyQt5）
- **ML**：scikit-learn, jieba, numpy, scipy, joblib
- **打包**：pyinstaller

## TB 应用前提条件

TB 应用（配置中的 app_id）**必须已发布并安装在企业中**，否则所有 API 调用返回 403 "应用在该企业未安装"。
参考：https://support.teambition.com/help/docs/60c8649368465f00468fb378

## 用户信息

- 角色：扫地机器人软件测试工程师
- 语言：Python（pytest、Appium、Selenium、PyAutoGUI）
- 用途：日常工作需要将禅道 Bug 导入到 TB 进行跟踪管理
- 界面语言：所有 GUI 文字、文档、exe 文件名均使用中文
