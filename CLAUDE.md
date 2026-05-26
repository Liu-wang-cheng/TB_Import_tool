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
  config_dialog.py           → 配置对话框：分 tab 编辑各模块配置 + 协同学习管理
  workers.py                 → QThread 异步任务：连接测试、列出Bug、同步、附件上传、协同学习
  yaml_utils.py              → YAML 原地编辑：保留注释，支持插入缺失 key
  qt_compat.py               → PyQt5/6 兼容层
  log_handler.py             → Qt 日志 Handler：将日志重定向到界面日志区
  updater.py                 → 自动更新：版本检测、镜像站测速、热更新
  resources/style.qss        → 界面样式表

main.py                      → CLI 入口（argparse），配置加载，客户端初始化
src/
  models.py                   → 数据类：ZentaoBug、TeambitionTask、AttachmentFile、SyncResult、SyncStats
  zentao_client.py            → 禅道 REST API v1：Token 认证 + Session 认证（用于文件下载）
  teambition_client.py        → TB 开放 API：JWT appToken 认证、任务 CRUD、文件上传到 OSS
  sync_engine.py              → 核心同步逻辑：去重、字段映射、双向标题同步、附件和评论同步
  classifier.py               → 缺陷分类器：TF-IDF 相似度（本地学习）+ LLM + 部门兜底
                                SimilarityClassifier: jieba分词 + TF-IDF + 余弦相似度
                                BugClassifier: 四层管道 + AI审核训练数据 + 部门过滤
  ai_log_analyzer.py          → AI 日志分析：LLM 调用、LogSummarizer 日志摘要、领域知识 SYSTEM_PROMPT
  log_analysis_integration.py → 日志分析集成：从 SN/时间下载 DRC 日志 → AI 分析 → 写入 TB 评论 + HTML 报告
  vision_analyzer.py          → 视觉分析器：GLM-4V 多模态分析视频关键帧和图片（cv2 提取帧）
  vision_integration.py       → 视觉分析集成：从 TB/禅道下载附件 → 按需调用视觉分析
  tb_web_downloader.py        → TB Web 附件下载：通过浏览器 Cookie 模拟下载（API 不可用时的回退）
  zentao_video_downloader.py  → 禅道附件下载：从禅道 Bug 附件中下载视频/图片
  knowledge_base.py           → 知识库管理：JSONL 知识存储、向量化、相似度检索、增量学习
  knowledge_rag.py            → RAG 检索增强：结合知识库的上下文增强 LLM 分析
  fault_pattern_library.py    → 故障模式库：预定义故障模式匹配、因果链模板
  prompt_builder.py           → Prompt 构建器：动态组装 LLM 分析 prompt（知识+故障模式+上下文）
  html_report_generator.py    → HTML 报告生成：AI 分析结果可视化 HTML 报告
  extractor.py                → 字段提取：从禅道 Bug 中提取 SN 编码、缺陷产生时间
  source_client.py            → 源平台抽象接口（Protocol），统一禅道/Jira 调用方式
  source_factory.py           → 源平台工厂：根据 source.yaml 自动创建禅道或 Jira 适配器
  zentao_adapter.py           → 禅道适配器：将 ZentaoClient 包装为 SourceClient 接口
  utils.py                    → 工具函数：URL 解析、指派人解析、部门前缀提取
  config_loader.py            → 多文件配置加载：合并 configs/ 目录下的 YAML
  config_resolver.py          → 配置解析：认证后将中文名称解析为 ID（项目、自定义字段、用户）
  collaborative_learning.py   → 协同学习：GitHub REST API 多用户共享知识库和分类器训练数据，无需 Git

batch_analyze.py              → 批量日志分析：DRC 下载、内存/CPU 趋势、故障检测、HTML 报告生成
drc_parser.py                 → DRC 二进制日志解析：LZ4 解压、AABBCC00 帧格式解析
report_generator.py           → HTML 报告生成器：Chart.js 图表、故障卡片、系统健康监控
release.py                    → 发布脚本：构建、打包、上传到 GitHub Release

dingtalk/
  bot.py                      → 钉钉机器人：HMAC 加签、Markdown/文本消息、同步结果通知
  server.py                   → 钉钉回调服务：@机器人指令触发同步/列出Bug

tools/
  query_ids.py                → 独立脚本：通过 OAuth 查询 TB 组织 ID / 项目 ID
  export_bugs.py              → Bug 导出工具：批量导出禅道 Bug 数据
  extract_pdf_flowcharts.py   → PDF 流程图提取：从 PDF 文档中提取知识库流程图
  feedback_automation.py      → 反馈自动化：批量处理知识库反馈审核
  generate_proposal.py        → 提案生成：基于模板生成改进提案
  probe_work_url.py           → URL 探测：验证 TB work URL 可访问性

configs/
  source.yaml                 → 缺陷来源选择（zentao / jira）
  zentao.yaml                 → 禅道服务器地址、账号、Bug 筛选条件
  jira.yaml                   → Jira 服务器地址、账号、JQL 查询条件
  teambition.yaml             → TB 应用凭证、项目、字段映射、自定义字段、用户映射
  dingtalk.yaml               → 钉钉 Webhook 和通知设置
  sync.yaml                   → 同步规则：标题标注格式、去重阈值、附件设置
  classifier.yaml             → 缺陷分类规则：TF-IDF 配置（阈值/训练数量/缓存目录）、LLM 配置、分类描述
  ai_analysis.yaml            → AI 日志分析：enabled 开关、DRC 服务器凭证、知识库配置、协同学习配置
  prompts.yaml                → LLM Prompt 模板：系统提示词、分析模板
  fault_patterns.yaml         → 故障模式定义：预检测规则、因果链模板
  update.yaml                 → 自动更新配置：镜像站地址、版本检测

data/                         → TF-IDF 模型缓存（classifier_model.pkl）、知识库（knowledge_base.jsonl）、
                                反馈文件（knowledge_feedback.yaml）、参考文档等。自动创建和同步。

zentao2teambition.spec        → PyInstaller 打包配置：自动包含 data/ 运行时文件
build.bat                     → Windows 一键打包脚本
strip_api_key.py              → 打包前密钥剥离：移除敏感配置信息
VERSION                       → 版本号文件（唯一版本源）
tests/                        → 单元测试

docs/
  tutorial.html               → 使用教程（纯 HTML+CSS 模拟界面截图）
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
- **打包**：`build.bat` 一键打包，只包含 5 个运行时必需文件（排除 42MB 的 PDF 和 DRC 配置 JSON）。PyInstaller 自动排除 pandas/matplotlib/PIL/pytest 等未使用库。打包前 `strip_api_key.py` 自动剥离密钥。
- **所属项目**：AI 分析中的"所属项目"字段自动同步 TB 项目名（`teambition.yaml` 中的 `project.name`），保存时不会覆盖到配置文件。DRC 日志查询的 `drc_model` 参数自动 fallback 到该项目名。
- **多平台支持**：通过 `source.yaml` 选择平台（zentao / jira），`source_factory.py` 创建对应适配器，`source_client.py` 定义统一接口。

## 依赖

- **核心**：pyyaml, requests, pyjwt, beautifulsoup4, oss2
- **GUI**：PyQt6（兼容 PyQt5）
- **ML**：scikit-learn, jieba, numpy, scipy, joblib
- **AI分析**：opencv-python-headless（视频帧提取，可选）
- **打包**：pyinstaller

## 协同学习（Collaborative Learning）

- **用途**：多用户通过 GitHub 仓库自动共享知识库（`knowledge_base.jsonl`）和分类器训练数据（`classifier_model.pkl`）
- **方式**：GitHub REST API（无需安装 Git），使用 Personal Access Token 认证
- **配置**：`ai_analysis.yaml` 中的 `collaborative_learning` 节，GUI 配置界面中管理
- **合并**：JSONL 按 `id` 字段去重（append-only union），YAML 深度合并，二进制模型文件直接替换
- **Token 解析优先级**：环境变量 `GITHUB_TOKEN` → `~/.github_token` 文件 → 配置文件 `ai_analysis.yaml`
- **周期**：启动时自动 pull（延迟 5 秒），每小时检查是否需要 push（间隔可配置，默认 168h = 每周）
- **GUI 管理**：配置对话框"协同学习"区域，支持开关、Token 输入、同步间隔选择、手动"立即同步"按钮

## TB 应用前提条件

TB 应用（配置中的 app_id）**必须已发布并安装在企业中**，否则所有 API 调用返回 403 "应用在该企业未安装"。
参考：https://support.teambition.com/help/docs/60c8649368465f00468fb378

## 自动启用的 Skills

以下 Superpowers skills 在对应场景下自动调用：

| Skill | 触发场景 |
|-------|---------|
| `systematic-debugging` | 遇到任何 bug、测试失败、构建错误、异常行为时 |
| `verification-before-completion` | 声称完成、修复成功、任务结束前，必须先运行验证 |
| `brainstorming` | 新功能、新组件、行为变更等任何创造性工作前 |

## 用户信息

- 角色：扫地机器人软件测试工程师
- 语言：Python（pytest、Appium、Selenium、PyAutoGUI）
- 用途：日常工作需要将禅道 Bug 导入到 TB 进行跟踪管理
- 界面语言：所有 GUI 文字、文档、exe 文件名均使用中文
