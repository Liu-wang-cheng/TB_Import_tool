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

### v2.8.2 (2026-08-31)

- **修复**：外部TB 备注图片在富文本 JSON 中（note 字段为 `[图片]` 占位符文本）时收集不到，内部TB 只有 `[图片]`
  - 根因：部分任务的 note 字段是纯文本，真实图片在 OSS 富文本 JSON 里（前端渲染时加载），`_collect_note_media` 只解析 note 文本引用
  - 修复：note 含图片/视频标记（`[图片]`/`<img>`/媒体URL等）时，抓取页面渲染的全部媒体签名 URL，未出现在 note 文本引用的媒体兜底注册
  - 真实验证：INFY-37 图片 2.5MB 下载成功

### v2.8.1 (2026-08-31)

- **新增**：周期性自动检查更新 —— 启动后 2 秒检查一次，之后按 `update.yaml` 的 `check_interval_hours` 周期复查（默认 **8 小时**，0 = 仅启动检查）；worker 忙时 60 秒后重试
- **修复**：双向标注回写标题误用任务 UUID —— 任务创建后编号/索引延迟生成导致 `create_task` 拿不到编号，外部TB标题写入 `【任务UUID】`；修复为优先取创建响应 uniqueId + 重试查询，仍失败时跳过标题回写（不污染标题）
- **优化**：外部TB 自定义字段**按名称精确匹配**（严重程度/复现概率/缺陷分类/所属版本/缺陷产生时间/SN编码/所属项目），零配置直接导入（不再依赖值特征猜测；字段名特殊时可用 `field_names` 覆盖）
- **优化**：外部TB M/D 时间格式（`8/26 15：25`）用任务创建时间补全年份归一化，日期筛选不再出错
- **新增**：外部TB 备注内联媒体（图片/视频）下载导入内部TB"日志附件" —— 解析备注媒体引用，含媒体任务用无头浏览器抓取前端签名 URL（原图优先，tcs 缩略图兜底），下载后走统一上传通道；备注占位符显示真实文件名

### v2.8.0 (2026-08-26)

- **修复**：更新成功启动后误弹"检测到上次自动更新未完成，是否继续更新"提示
  - 根因：更新 bat 替换完成后会等 3~15 秒清理 `_internal_old` 再自删，新版本启动更快时 `_update_replace.bat` 与 `_update_extracted` 仍在，自愈逻辑误判为"更新未完成"
  - 修复：自愈重试分支增加**版本对比**——解压目录 VERSION 与当前运行版本一致时判定"更新已完成"，静默清理不弹窗；仅当解压版本**高于**当前版本（真中断）才询问
  - 版本比较用语义化元组（防 "2.10" < "2.9" 陷阱），VERSION 读取兼容 exe 在子目录的 zip 结构
- **新增 3 个单元测试**：版本一致不弹窗 / 更高版本仍询问 / 版本元组比较

### v2.7.9 (2026-08-26)

- **新增**：同步支持多个禅道产品/项目（配置 `product: [11, 20]` / `project: [5, 9]`，产品×项目循环拉取 + bug.id 去重；严重程度翻译/模块过滤/关闭同步全部循环合并）
- **新增**：模块ID支持逗号分隔多值（如 `123,140`，每个值含全部子模块，多产品合并集合；公共函数 `resolve_module_filter_ids` 统一 4 处调用）
- **新增**：外部 TB 多项目支持（project_id 逗号分隔/列表）
- **新增**：URL 解析增强 —— 同服务器地址下产品ID/项目ID 与现有合并去重；识别到不同服务器地址弹窗确认切换；`project-bug-{id}` 解析出的项目ID 自动写入配置
- **新增**：更新自愈 —— 启动时 `_internal` 缺失自动回滚 `_internal_old`；更新 bat 残留且解压目录完整时弹窗询问继续更新（新模板重新生成）；损坏残留自动清理
- **优化**：GUI "禅道项目ID" 标签修正为"模块ID"（含子模块提示）；产品ID/模块ID/外部TB项目ID输入框支持逗号分隔并加提示
- **新增 18 个单元测试**：多产品合并去重、多值模块解析、URL合并语义、更新自愈回滚/重试

### v2.7.8 (2026-08-26)

- **修复**：自动更新脚本在部分电脑上 `_internal 复制失败，回滚`（"系统找不到指定路径"/"文件名语法不正确"）
  - 根因：更新 bat 用 UTF-8 写入但嵌入了安装目录的绝对路径与中文 exe 名，GBK 代码页的系统上 cmd 读取 bat 时中文路径乱码，rename/robocopy 路径全部失效
  - 修复：bat 不再嵌入任何绝对/中文路径，全部用 `%~dp0`（bat 所在目录）与动态遍历解析新 exe 位置
- **新增 2 个单元测试**：bat 不含绝对/中文路径、动态定位新 exe

### v2.7.7 (2026-08-26)

- **修复**：列出缺陷后严重程度中文翻译不显示（`_on_list_result` 先清空 worker 再读 `severity_labels`）
- **修复**：配置对话框保存后，平台切换逻辑把 UI 旧值写回 config，导致对话框里修改的禅道凭证/筛选被回滚
- **修复**：`learn_sn_patterns` 学到前缀模式后丢弃默认模板模式，`SN码：` 等格式提取不到
- **修复**：外部 TB 无任务编号时用 `str hash` 生成 bug_id（跨进程随机）→ 改用 `_id` 的 24 位 hex 数值，去重标签跨运行稳定
- **修复**：外部 TB 点分日期（`2026.8.21——15:50`）归一化为 ISO 格式，日期筛选不再系统性错误
- **修复**：外部 TB 严重程度猜测排除 commongroup 分类值（如"一般性建议类问题"误填严重程度）
- **修复**：窗口关闭时后台线程未结束直接销毁 QThread 崩溃 → 改为等待超时提示"任务进行中"
- **修复**：YAML 编辑时内嵌引号值转义（不再写出非法 YAML）、列表替换不再吞掉后续注释
- **修复**：中文日期"时间：8 月 24 日 16:35"优先于斜杠无时间格式
- **新增 6 个单元测试**：SN 模式兜底、外部TB稳定ID/日期归一化/严重程度排除、YAML 引号与注释

### v2.7.6 (2026-08-25)

- **调整**：列出缺陷（CLI `--list-bugs` / GUI 列出，禅道与外部 TB）不再推送钉钉通知
  - 删除 `main.py` list-bugs 钉钉段与 `ListBugsWorker` 的钉钉发送
  - 同步结果（试运行/正式同步）的钉钉通知保留不变
  - 钉钉回调（@机器人"列出Bug"）的应答回复保留不变

### v2.7.5 (2026-08-24)

- **修复**：`tools/export_bugs.py`（Excel 导出）的数字模块 ID 过滤同样改为递归包含子模块 —— 此前精确匹配 `bug.module` 漏掉子模块缺陷（修复前 0/1 条 → 修复后 28 条）
- 真实验证：模块 123 过滤后 28 条全部保留

### v2.7.4 (2026-08-24)

- **修复**：关闭同步（sync_closed_status）的数字模块 ID 过滤未随主同步改为递归 —— 仍精确匹配 `bug.module == "123"`，子模块（136/137/158/159）里的已关闭缺陷全部漏掉（真实数据：模块 123 已关闭缺陷精确匹配 3 条 vs 递归 1689 条）
  - 修复：`_run_close_sync_phase` 数字 ID 分支改用 `resolve_module_descendant_ids` 递归集合，树不可用时回退精确匹配
- **新增 2 个单元测试**：关闭同步递归集合 / 树不可用回退精确匹配

### v2.7.3 (2026-08-24)

- **修复**：v2.7.2 引入的回归 —— 数字模块 ID 过滤时，`apply_module_filter` 的数字快路径（精确匹配 `bug.module == "123"`）在 `module_id_set` 判断之前，导致递归解析出的"模块+全部后代"集合被忽略，列出缺陷仍为 0 条
  - 根因：`apply_module_filter` 中数字快路径未优先使用调用方预解析的 ID 集合（sync_engine 内联过滤不受影响，CLI list-bugs / GUI 列出缺陷走 `apply_module_filter` 受影响）
  - 修复：数字快路径在 `module_id_set` 已传入时优先用集合过滤，未传入才回退精确匹配
- **新增 4 个单元测试**：数字ID+后代集合过滤 / 无集合精确匹配 / 空集合 / 名称集合过滤

### v2.7.2 (2026-08-24)

- **修复**：模块过滤改为递归包含子模块（与禅道网页 byModule 一致）
  - 背景：网页 `bug-browse-11--byModule-123.html` 显示 2207 条缺陷，工具按模块 ID 精确匹配只找到 1 条 —— 禅道网页是"模块+全部后代"递归筛选，而工具只匹配 `bug.module == "123"`
  - 根因：模块 API `/api.php/v1/modules` 只返回根模块（无父子层级），子模块（如 123 下的 136/137/158/159）无法枚举，数字 ID 过滤漏掉全部子模块缺陷
  - 修复：新增 `fetch_module_tree()`（API 根模块 + 回退浏览页 JSON `modules` 字段构建完整树）、`resolve_module_descendant_ids()`（模块+全部后代集合）；sync_engine / CLI list-bugs / GUI 列出全部走递归集合过滤
- **修复**：禅道 clean URL 探测逻辑（`_probe_clean_url`）
  - 根因：动态路径 `/index.php?m=api&f=getsessionid` 未登录时返回 200 但内容是登录重定向 HTML，被误判为"动态 URL 模式"，导致 `_ensure_session` 拿到 HTML 报"非JSON"，Session 登录（文件下载回退链）失败
  - 修复：动态路径返回 200 时校验响应体是否为有效 JSON，非 JSON 判定为 clean 模式
- **优化**：列出缺陷 / 试运行时获取到的缺陷数量为 0 时不再推送钉钉消息
- **修复**：备注内联图片占位符显示真实文件名（不再 fallback 成 `image_{id}.png`）
  - 根因：部分禅道版本（如 18.3）REST 详情与网页 JSON 的 `files` 字段返回空，`file_id→真实文件名` 映射缺失，备注图片显示 `[图片: image_17629.png]` 这类假名
  - 修复：新增 `fetch_file_name()` 探测 `file-download-{id}.html` 响应头的 Content-Disposition 真实文件名（带缓存），`_build_note` 对未映射的 file_id 自动补全
- **新增 22 个单元测试**：模块树构建/后代解析、clean URL 探测、钉钉 0 条判断、文件真实名探测与备注占位符

### v2.7.1 (2026-08-24)

- **优化**：外部 TB 自定义字段优先同步（缺陷产生时间/版本/复现概率/SN，对应不上从备注提取）
- **修复**：严重程度为空时默认 B
- **修复**：定时同步每周天数

### v2.7.0 (2026-08-21)

- **新增**：外部 TB 源（teambition_source）—— 从外部 Teambition 项目导入缺陷到内部 TB
- **新增**：定时同步每周天数选择
- **优化**：分类器去除部门过滤

### v2.6.8 (2026-06-24)

- **新增**：定时同步功能 — 主界面设置每天固定时间自动执行导入同步
  - 开关：`QCheckBox "启用定时同步"` + `QTimeEdit` 时间选择器（HH:mm 格式）
  - 机制：`QTimer` 每分钟检查一次（精确到分钟），到点触发同步，同一天不重复执行
  - 触发：静默触发（不弹确认框），完成后可选弹窗提示
  - 同步范围：使用主界面当前筛选条件
  - 持久化：配置存到 `sync.yaml/ scheduled_sync`，启动时自动加载
  - 安全性：如果上次同步仍在进行中则跳过本轮
  - 新增 `QTime`/`QTimeEdit` 到 `qt_compat` 兼容层（PyQt5/6 双支持）
- **新增 4 个单元测试**：覆盖启用判断、时间匹配、同日去重、YAML 持久化往返

### v2.6.7 (2026-06-24)

- **修复**：自动更新后 `_internal_old` 目录残留（~150MB），未自动删除
  - 根因：v2.6.5 的 rename 策略，新 exe 启动后杀毒软件扫描 `_internal_old` 里的 dll 锁定文件，rmdir 失败被 `2>nul` 静默吞掉
  - 修复1：`.bat` 清理 `_internal_old` 加 5 次重试（共 15 秒），失败时打印 `[WARN]` 不再静默
  - 修复2：`gui_main.py` 启动时也清理 `_internal_old`（兜底，杀毒扫完后必定能删）
  - 修复3：.bat 标签一致性（`goto :cleanup` 错误处理路径不受影响）

### v2.6.6 (2026-06-24)

- **修复**：自定义状态（如 `delay` 延期）的 bug 被默认 status 过滤丢弃，搜索不到
  - 实例：禅道 product 413 上 57/85 条 bug 是 `delay` 状态，工具的"激活"分组只含 `active/confirmed`，导致这些 bug 全部搜不到
  - 根因1：`_fetch_status_groups_self_hosted` 归类规则没 else 兜底，未识别的 status（既不是"关闭/解决"也不是"激活/确认"）被丢弃
  - 根因2：部分禅道实例的 `/api.php/v1/bugStatuses` API 返回空，工具直接走 fallback `["active", "confirmed"]`，连自定义状态存在都不知道
  - 修复1：未识别的 status 默认归入 open（保守策略，让用户能搜到）
  - 修复2：bugStatuses API 返回空时，扫描实际 `fetch_bugs` 看到的 status 作为补全（缓存到 `_last_bug_status_cache`）
  - 修复3：云版 `_fetch_status_groups_cloud` 同步修复，未识别 status 也归 open
- **新增 3 个单元测试**：覆盖 delay 归 open / 未识别状态归 open / API 空时扫描补全三种场景

### v2.6.5 (2026-06-24)

- **修复**：v2.6.4 的 robocopy 重试策略仍可能被 dll 锁定卡住
  - 改用 **rename 策略**：先 `rename _internal → _internal_old`（Windows 原子操作，即使文件被锁也能成功），再 robocopy 新 `_internal` 到空目录（无锁，必定成功），启动新 exe 后等 5 秒再删除 `_internal_old`
  - 不再依赖 robocopy 重试 / dll 释放时机，**第一次就能成功**
- **修复**：`_apply_update_and_restart` 用 `os._exit(0)` 强制退出，立即释放 dll 句柄（不等 QThread / GC）
- **重要**：v2.6.5 之前的版本自动更新到 v2.6.5 时仍用旧 .bat 模板（rename 策略用不上），可能失败。需要手动安装 v2.6.5 一次，之后所有自动更新都会顺利

### v2.6.4 (2026-06-24)

- **修复**：自动更新时卡在"正在更新程序文件..."最终报 `_internal 更新失败`
  - 根因：旧 GUI 进程退出后，PyQt6 的 .dll 文件句柄释放有延迟（3-5 秒）；原 .bat 在进程退出后立即 robocopy，dll 还被锁定，重试 3 次 × 1 秒不够
  - 修复：原进程退出后**多等 3 秒**让 dll 句柄释放
  - robocopy 重试参数从 `/r:3 /w:1` 加长到 `/r:5 /w:3`（5 次重试 × 3 秒间隔）
  - exe 复制加重试 5 次 × 2 秒（可能被杀毒短暂锁定）
  - 失败时打印详细 robocopy 输出（便于定位是哪个文件失败）
- **重要**：本次更新到 v2.6.4 必须手动安装（下载 zip 解压覆盖安装目录），因为旧版本 .bat 模板还有问题。装上 v2.6.4 后，下次更新就会用新的 .bat 流程

### v2.6.3 (2026-06-24)

- **修复**：v2.6.2 代码改对了但 exe 没生效（PyInstaller 用了缓存 .pyc，源码改了没重新编译）
  - 彻底清理所有缓存：`build/`、`dist/`、所有 `__pycache__/`、PyInstaller 自身的 cache
  - 重新打包确保 v2.6.2 的正则修复（`【(?:VLNS|CPAX)-\d+】`）真正进入 exe
  - 实测验证：源码层面 Bug#7988 的 `【P260724-00083】` 已能保留，本版本确保 exe 行为一致

### v2.6.2 (2026-06-24)

- **修复**：同步禅道 Bug 时，标题里的禅道产品编号 `【P260626-00013】` 被误清
  - 根因：`_build_zentao_title` 和 `get_base_title` 用了过度宽松的正则 `【[\w]+-\d+】`，本意是清除旧的 VLNS/CPAX 标注（避免重复堆叠），但匹配范围过广，把禅道自己的产品编号（`P+日期-序号` 格式）也匹配上了
  - 修复：正则限定为只匹配 `【(?:VLNS|CPAX)-\d+】`，CLAUDE.md 明确这是唯一两种双向标注格式
  - 旧的 VLNS 标注（如 `【VLNS-61849】`）仍然被正常清除，避免标题堆叠
- **新增 3 个单元测试**：覆盖禅道产品编号保留 + 旧 VLNS 标注清除两种场景

### v2.6.1 (2026-06-24)

- **修复**：TB 评论中的图片名和实际附件名对应不上（如 `[图片: comment_01.png]` 在附件列表里却是 `image_15411.png`）
  - 根因：评论附件走独立通道 `_upload_comment_media`，用虚假序号命名 `comment_NN.{ext}`，与 `_sync_attachments` 的命名（`image_{file_id}.{ext}`）不一致，同一张图被上传两次
  - 改造为单一上传通道：评论解析只收集 `file_id`（占位符 `[图片: __ATTACH_{id}__]`），`_sync_attachments` 接收 `comment_file_ids` 参数统一上传，按 `file_id` 跨来源去重，上传完后回填占位符为真实文件名
  - 评论同步拆为两阶段：`_parse_bug_comments`（解析）→ `_sync_attachments`（上传）→ `_submit_bug_comments`（回填+发送）
- **优化**：图片附件改用真实文件名（不再用 `image_{id}.png` 假名）
  - `download_image` 切换到 `file-download-{id}.html` 接口，从 `Content-Disposition` 提取真实文件名
  - 兼容 RFC 5987 编码（`filename*=UTF-8''xxx`）
  - 无 `Content-Disposition` 时 fallback 到 `image_{file_id}.{ext}`（魔数检测扩展名）
  - 任务描述（`_clean_html_for_tb`）的图片占位符也优先用 `bug.files` 中的真实文件名
- **修复**：标题含 `VLNS-xxx` 的 bug 仍被新建任务（去重失效）
  - 根因：`extract_vlns_numbers` 只扫 `actions`（备注/历史），不扫 `bug.title`
  - 双向标题同步把 `VLNS-xxx` 写在 title 字段，actions 里没有对应记录
  - `_find_existing_task` Tier 1.5 补充从 `bug.title` 提取 VLNS/CPAX 编号
- **修复**：`_extract_file_id_from_src` 不匹配禅道 clean URL `file-download-{id}.html`
  - 正则统一为 `file-(?:read|download)[_-](\d+)`，覆盖所有 URL 格式
  - `_clean_html_for_tb` 中的 img 标签解析同步修复
- **测试**：新增 27 个单元测试（评论附件占位符 + 去重 + 真实文件名提取）

### v2.6.0 (2026-06-17)

- **重构**：指派人筛选简化——剥离部门前缀 + 单向账号查找，删除跨部门反向查找
  - 修复同名跨部门用户串号（`IOT-陈斌` 配置不再误匹配 `应用-陈斌` 的 bug）
  - 保留单向 account 兜底（`bug.assignedTo` 是英文账号时不漏筛）
  - 兼容配置 `"李珍"` / `"项目-李珍"` 两种格式
- **修复**：云版禅道 invalidate 漏清缓存
  - `invalidate_cloud_browse_cache` 现在清空所有缓存（`_bug_raw_cache` / `_product_modules_cache` / `_severity_labels_cache` / `_normalized_users_cache`）
  - `_severity_labels_cache` 改为按实例 key 清除（`pop((base_url, account))`），不再影响其他 client 实例
- **优化**：GUI 更新弹窗用 `v{version}` 前缀定位当前版本说明，不再依赖 `\n\n` 分割
- **优化**：`_get_bug_raw` 用 `_normalize_users_cached`（按对象 id 缓存），避免每 bug 重建
- **优化**：`SimilarityClassifier` 改为 `_fetch_defect_samples` 内惰性导入，避免顶层拉 sklearn/jieba
- **重构**：统一 `_normalize_id_value_map(data, id_keys, value_keys)`，`_normalize_users` / `_normalize_modules` 复用同一份逻辑
- **修复**：`_normalize_users` 不再过滤 `account == realname`（保留 `admin/admin` 默认账号）
- **优化**：`build.bat` 加 `pyinstaller --clean`，清 PyInstaller 自己的 build 缓存

### v2.5.9 (2026-06-16)

- **修复**：云版禅道列出/同步 Bug 不是实时刷新（需重启工具才生效）
  - 根因：`_cloud_get_browse` 永久缓存浏览页数据，单例 client 跨用户操作命中旧缓存
  - 新增 `invalidate_cloud_browse_cache()` 方法，`ListBugsWorker` / `SyncWorker`
    在 authenticate 后主动清缓存
  - 单次操作内多次分页/筛选仍复用缓存，性能不退化
  - 仅云版禅道受益（自建版走 REST API 无此问题）

### v2.5.8 (2026-06-16)

- **修复**：v2.5.6/v2.5.7 GUI 弹窗仍显示全部历史版本说明
  - 根因：`gui/__pycache__/main_window.cpython-313.pyc` mtime 异常（早于源文件），PyInstaller 直接复用旧 .pyc 缓存
  - build.bat 增加清理 `__pycache__` 步骤，确保每次构建从源码重新编译

### v2.5.7 (2026-06-16)

- **修复**：云版禅道 `modules` 字段 list 格式导致列出 Bug 时崩溃（`'list' object has no attribute 'items'` at `_register_cloud_modules`）
  - v2.5.6 只修了 `users` 字段，遗漏了 `modules` 字段同样的格式漂移
  - 新增 `_normalize_modules()` 统一 list / dict / 嵌套 dict 格式为扁平 `{id: name}`
  - 2 个使用点（`_cloud_get_browse` / `_fetch_modules_endpoint`）全部走规范化

### v2.5.6 (2026-06-16)

- **修复**：云版禅道 `users` 字段格式漂移导致同步崩溃（`'list' object has no attribute 'items'`）
  - 新增 `_normalize_users()` 统一 list / dict / 嵌套 dict 格式为扁平 `{account: realname}`
  - 3 个使用点（`_update_user_mapping` / `fetch_bugs` / `_get_bug_raw`）全部走规范化
- **优化**：指派人筛选统一剥离部门前缀，兼容 `"李珍"` / `"项目-李珍"` 两种配置格式
  - 新增 `_strip_dept_prefix()` 工具方法
  - 分类器不再依赖部门前缀后，配置侧不再需要写 `"部门-姓名"` 形式

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
