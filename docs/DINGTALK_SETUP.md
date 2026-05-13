# 钉钉机器人配置与使用指南

## 功能概述

钉钉机器人支持两种使用模式：

- **模式A：CLI 执行后自动推送** — 运行 `python main.py` 后，自动将同步结果发送到钉钉群。无需公网地址。
- **模式B：群里@机器人触发导入** — 在钉钉群里 `@机器人 同步`，机器人自动执行导入并回复结果。需要公网 HTTPS 地址（可用 ngrok 内网穿透）。

## 一、创建钉钉群机器人

### 步骤1：进入群设置
1. 打开钉钉，进入目标群聊
2. 点击右上角 **群设置**（齿轮图标）
3. 选择 **智能群助手** → **添加机器人**

### 步骤2：添加自定义机器人
1. 点击 **添加机器人** → **自定义（通过 Webhook 接入）**
2. 设置机器人名称（如"禅道导入助手"）和头像
3. 安全设置：**勾选"加签"**
4. 复制以下内容备用：
   - **Webhook 地址**：`https://oapi.dingtalk.com/robot/send?access_token=xxx`
   - **加签密钥**：`SECxxx...`

### 步骤3：配置 config.yaml

将复制的信息填入 `config.yaml`：

```yaml
dingtalk:
  enabled: true
  webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
  secret: "SECxxx..."
  at_all: false
  callback_port: 8080
```

## 二、模式A：CLI 执行后自动推送

此模式最简单，只需配置好 webhook_url 即可。

```bash
# 正常运行，完成后自动推送结果到钉钉
python main.py

# 试运行模式，也会推送结果
python main.py --dry-run

# 列出 Bug，结果也会推送到钉钉
python main.py --list-bugs

# 强制启用/禁用钉钉通知（覆盖配置）
python main.py --dingtalk      # 强制启用
python main.py --no-dingtalk   # 强制禁用
```

钉钉消息示例：

```
禅道→Teambition 同步结果
━━━━━━━━━━━━━━━━━━━━
总计处理  15 条
新建成功  12 条
去重跳过  2 条
筛选跳过  0 条
错误      1 条
耗时      45秒
```

## 三、模式B：@机器人触发导入

此模式需要在群里 `@机器人` 发送指令来触发导入。

### 前提条件

钉钉机器人回调需要公网 HTTPS 地址。如果你没有公网服务器，可以使用 **ngrok** 内网穿透：

#### 安装 ngrok
1. 访问 https://ngrok.com/download 下载 ngrok
2. 注册账号并获取 Authtoken
3. 执行 `ngrok config add-authtoken xxx`

#### 启动服务

```bash
# 1. 启动 HTTP 回调服务
python dingtalk_server.py

# 2. 另开一个终端，启动 ngrok 暴露端口
ngrok http 8080

# 3. ngrok 会显示一个 https 地址，如：
#    https://a1b2c3d4.ngrok-free.app
```

#### 配置钉钉回调地址

1. 回到钉钉群 → 群设置 → 智能群助手 → 点击已添加的机器人
2. 找到 **机器人配置** → **消息接收模式** → 选择 **HTTP 模式**
3. 在 **请求网址** 中填入：`https://你的ngrok地址/dingtalk`
   - 例如：`https://a1b2c3d4.ngrok-free.app/dingtalk`
4. 保存

### 支持的指令

在群里 `@机器人` 后发送以下指令：

| 指令 | 功能 |
|------|------|
| `@机器人 同步` / `@机器人 导入` | 执行全量同步（正式运行） |
| `@机器人 试运行` | 模拟运行（不实际创建任务） |
| `@机器人 列出bug` | 列出当前筛选条件下的禅道 Bug |
| `@机器人 状态` | 查看系统配置状态 |
| `@机器人 帮助` | 显示支持的指令列表 |

### 使用示例

```
@禅道导入助手 同步
```

机器人会回复：
```
收到指令：同步，正在执行...
```

执行完成后，机器人会发送详细结果：
```
禅道→Teambition 同步结果
总计处理  15 条
新建成功  12 条
...
```

## 四、安全说明

- **加签验证**：所有钉钉回调都会验证签名，防止伪造请求
- **密钥保护**：`config.yaml` 中的 `secret` 不要提交到 Git
- **IP 白名单**（可选）：可在 `dingtalk_server.py` 中增加钉钉 IP 段白名单

## 五、故障排查

### 钉钉收不到消息
1. 检查 `webhook_url` 和 `secret` 是否正确
2. 检查机器人是否被禁言
3. 查看日志文件 `logs/sync_*.log`

### @机器人没反应
1. 检查 ngrok 是否正常运行
2. 检查钉钉回调地址是否配置正确（必须是 HTTPS）
3. 检查防火墙是否放行了 8080 端口
4. 查看 `dingtalk_server.py` 的控制台输出

### 签名验证失败
- 确保 `config.yaml` 中的 `secret` 与钉钉机器人安全设置中的加签密钥一致
- 注意时间同步（系统时间与钉钉服务器时间偏差不能太大）
