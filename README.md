# 智能缺陷管理平台

将禅道（Zentao）/ Jira 缺陷一键同步到 Teambition（TB），支持智能去重、字段映射、AI 日志分析、协同学习。面向扫地机器人软件测试团队。

## 功能特性

- **多平台支持**：禅道 / Jira 缺陷同步到 TB，GUI 界面切换平台
- **智能去重**：精确标签匹配 `【禅道{id}】` + 标题模糊匹配（阈值 0.8）
- **字段映射**：严重程度(S/A/B/C)、复现概率、缺陷分类、所属项目自动映射
- **AI 自动分类**：TF-IDF 相似度 → LLM 大模型 → AI 审核 → 部门兜底，四层管道
- **AI 日志分析**：同步后自动下载 DRC 日志 → LLM 根因分析 → HTML 报告 → 写入 TB 评论
- **视觉分析**：GLM-4V 多模态分析视频关键帧和图片附件（可选）
- **附件同步**：禅道附件（含视频/图片）自动上传到 TB 任务，支持断点续传和去重
- **评论同步**：禅道评论（含内联图片）同步到 TB，重新激活时增量同步新评论
- **SN 自动提取**：从 Bug 重现步骤中提取设备 SN 编码
- **双向标题标注**：TB 任务标题加 `【禅道{id}】`，禅道 Bug 标题加 `【VLNS-xxxxx】`
- **协同学习**：通过 GitHub 仓库多用户共享知识库和分类器训练数据（无需 Git）
- **钉钉通知**：同步结果通过 Markdown 表格自动推送
- **自动更新**：启动时检测新版本，镜像站自动测速，一键热更新
- **批量分析**：独立工具，支持 DRC 日志批量下载、故障检测、HTML 报告生成

## 快速开始

### 安装依赖

```bash
pip install pyyaml requests pyjwt beautifulsoup4 oss2 PyQt6 scikit-learn jieba
# 可选：视频帧提取
pip install opencv-python-headless
```

### GUI 界面（推荐）

```bash
python gui_main.py
```

双击 `智能缺陷管理平台.exe` 启动，图形界面操作。

### 命令行

```bash
# 列出禅道 Bug
python main.py --list-bugs --status active

# 列出 Jira Bug
python main.py --list-bugs --platform jira

# 试运行（不实际创建任务）
python main.py --dry-run

# 正式同步
python main.py

# 测试 TB 认证
python main.py --auth-only

# 查询 TB 组织/项目 ID
python tools/query_ids.py

# 批量日志分析
python batch_analyze.py
```

## 配置

配置文件位于 `configs/` 目录。部分配置提供了 `.example` 模板：

| 文件 | 内容 |
|------|------|
| `source.yaml` | 缺陷来源选择（zentao / jira） |
| `zentao.yaml` | 禅道服务器地址、账号、Bug 筛选条件 |
| `jira.yaml` | Jira 服务器地址、账号、JQL 查询 |
| `teambition.yaml` | TB 应用凭证、项目 ID、字段映射、用户映射 |
| `sync.yaml` | 同步规则：去重阈值、附件大小限制、API 延迟 |
| `classifier.yaml` | AI 分类器：TF-IDF 配置、LLM 模型、分类描述 |
| `ai_analysis.yaml` | AI 日志分析：DRC 服务器、知识库、协同学习 |
| `dingtalk.yaml` | 钉钉 Webhook、密钥、通知设置 |
| `prompts.yaml` | LLM Prompt 模板 |
| `fault_patterns.yaml` | 故障模式定义、预检测规则 |
| `update.yaml` | 自动更新：镜像站、版本检测 |

## 项目结构

```
gui_main.py                   → GUI 入口
gui/                          → PyQt6 图形界面（配置、筛选、同步、日志）
src/
  sync_engine.py              → 核心同步引擎（去重、映射、附件、评论）
  zentao_client.py            → 禅道 REST API v1（Token + Session 双认证）
  teambition_client.py        → TB 开放 API（JWT 认证、任务 CRUD、OSS 上传）
  classifier.py               → AI 缺陷分类器（四层管道）
  ai_log_analyzer.py          → AI 日志分析（LLM + 领域知识）
  log_analysis_integration.py → 日志分析集成（DRC 下载 → 分析 → TB 评论）
  vision_analyzer.py          → 视觉分析（GLM-4V 多模态）
  vision_integration.py       → 视觉分析集成
  knowledge_base.py           → 知识库管理（JSONL 存储、向量检索）
  knowledge_rag.py            → RAG 检索增强
  fault_pattern_library.py    → 故障模式匹配库
  prompt_builder.py           → LLM Prompt 动态构建
  html_report_generator.py    → HTML 报告生成
  collaborative_learning.py   → 协同学习（GitHub API 共享知识库）
  config_loader.py            → 多文件配置加载
  config_resolver.py          → 中文名称 → ID 解析
  source_factory.py           → 多平台工厂（禅道/Jira）
  extractor.py                → SN/时间提取
  utils.py                    → 工具函数
dingtalk/                     → 钉钉通知 + 回调服务
tools/                        → 辅助脚本（ID 查询、Bug 导出等）
configs/                      → YAML 配置文件
data/                         → 模型缓存、知识库、参考文档
tests/                        → 单元测试
docs/                         → 使用文档
```

## TB 应用前提条件

TB 应用（`teambition.yaml` 中的 `app_id`）**必须已发布并安装在企业中**，否则所有 API 调用返回 403。

参考：https://support.teambition.com/help/docs/60c8649368465f00468fb378

## 协同学习

多用户通过 GitHub 仓库自动共享知识库和分类器训练数据：

- **方式**：GitHub REST API（无需 Git），Personal Access Token 认证
- **Token 获取**：GitHub Settings → Developer settings → Personal access tokens → 勾选 `repo` 权限
- **配置**：GUI 配置界面 → AI 分析 Tab → 协同学习区域
- **周期**：启动时自动拉取，每小时检查推送（默认每周推送一次）

## 打包

```bash
build.bat
```

输出到 `dist/智能缺陷管理平台/`。仅包含运行时必需文件，自动排除未使用的库。

## 依赖

| 类别 | 包 |
|------|-----|
| 核心 | pyyaml, requests, pyjwt, beautifulsoup4, oss2 |
| GUI | PyQt6（兼容 PyQt5） |
| ML | scikit-learn, jieba, numpy, scipy, joblib |
| AI 分析 | opencv-python-headless（可选，视频帧提取） |
| 协同学习 | requests（内置，无额外依赖） |
| 打包 | pyinstaller |

## 版本历史

### v2.5.5 (2026-06-16)

- **修复**：classifier 关闭 + TF-IDF 缓存 > 7 天时增量学习静默失效（`SimilarityClassifier` 在 `__init__` 局部导入，`_fetch_defect_samples` 看不到，`NameError` 被 broad except 吞掉）
  - 改为模块级导入，删除两处冗余局部导入
- **测试**：`TestCloudTitleUpdate` 用 `__new__` 绕过 `__init__` 导致 `_bug_raw_cache_lock` 缺失，补全属性 + 改用 `MagicMock` 让比较运算可工作，测试真正验证云版 POST 调用路径

### v2.5 (2026-06-11)

- **修复**：GUI "重新打开任务"开关关闭后不实时生效（`_save_reopen_switch` 漏更新内存配置）

### v2.4.1 (2026-06-11)

- **修复**：图片附件上传后 TB 显示格式错误（session 认证返回 HTML 登录页面被误当图片）
  - `_download_file` 加 `_is_valid()` 防 HTML 冒充图片
  - 文件下载改用 clean URL，不经过 `index.php` 动态路由
  - 魔数检测真实图片格式（JPEG/PNG/GIF/WebP/BMP），不再硬编码 `.png`
  - 上传时始终从文件扩展名推断 MIME 类型
- **修复**：评论媒体去重导致损坏文件残留（同名文件不再跳过，始终重新上传）
- **优化**：关闭同步加 VLNS/CPAX 标题预筛选，大幅减少无效遍历
- **优化**：关闭同步开启时主同步日志降为 debug，减少刷屏
- **优化**：无新建/重新激活时不发钉钉通知
- **优化**：GUI 和钉钉通知导入/关闭数据分行显示
- **新增**：14 个测试用例（图片格式检测 + 关闭同步通知格式）

### v2.4 (2026-06-11)

- **新增**：关闭同步 — 禅道已关闭 Bug 自动将 TB 待回归任务设为关闭
- **新增**：执行人模式 — 自动/指定人员切换，指定模式下统一指派执行人
- **新增**：`server_status="all"` 让禅道 API 返回已关闭 Bug
- **改进**：VLNS 回退搜索、taskflowId 空值回退
- **改进**：GUI 界面精简（去掉"项目ID"字段、"禅道项目名称"→"禅道项目ID"、模块用数字 ID）

### v2.3 (2026-06-08)

- **新增**：TB 执行人自动/指定模式切换
- **改进**：严重程度动态翻译、去重修复、图片显示优化

### v2.2

- **修复**：去重逻辑修复、严重程度映射完善

### v1.8 (2026-06-02)

- **修复**：自建版 & 云版禅道错误凭证静默通过认证（连接测试不报错）
- **修复**：指派人列表出现 `null-` 前缀异常条目（YAML 标量→列表未清除旧值）
- **修复**：日期筛选不生效（PyYAML 误解析 `2026-01-01` 为 datetime.date）
- **修复**：日期筛选 GUI 重启后无法恢复保存的日期值
- **修复**：AI 子开关执行操作后不回灰（`_set_busy` 无条件恢复）
- **修复**：云版使用纯名字指派人过滤返回 0 条（部门前缀失配）
- **修复**：CPAX 去重检测（与 VLNS 并列支持）
- **修复**：云版 browse 分页超出范围无限循环
- **新增**：token 有效性二次验证（`/user` 端点校验）
- **改进**：`_passes_filters` 部门前缀后缀匹配
- **改进**：dingtalk.yaml 加入版本控制，团队共享钉钉配置
- **改进**：打包脚本 `ai_analysis.yaml` 备份恢复

详见 [Releases](https://github.com/Liu-wang-cheng/TB_Import_tool/releases)
