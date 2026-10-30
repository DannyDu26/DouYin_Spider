---
name: fetch-xhs-data
description: 通过 DouYin_Spider 项目的 HTTP API 检查服务、查看小红书账号池，以及执行小红书扫码或短信登录。当用户要求小红书登录、刷新登录态或检查已认证账号时使用。当前仅实现登录，不提供笔记、用户或评论抓取命令。
---

# 小红书登录

复用本仓库的 FastAPI 服务，凭据由服务端保存并加入小红书账号池。不要直接调用上游登录接口，不要读取或输出 Cookie、票据、设备状态和凭据数据库记录。

以下命令从本 skill 所在目录执行；若 skill 已安装，使用安装目录，不能依赖当前工作目录下存在 `skills/`。Python 一律通过 `conda run --no-capture-output -n crawler python` 执行。客户端只依赖 Python 3.10+ 标准库。

默认 API 根地址为 `https://crawler.xoyo.com/api/`；可通过 `XHS_API_BASE_URL` 或放在子命令前的 `--base-url` 覆盖。本地开发示例：`--base-url http://127.0.0.1:5000/api/`。不要修改环境文件或虚构数据库配置。

## 检查服务与账号

```powershell
conda run --no-capture-output -n crawler python scripts/xhs_client.py health
conda run --no-capture-output -n crawler python scripts/xhs_client.py accounts
```

账号列表只返回安全元数据。只有用户要求登录、刷新登录态时才创建新会话；普通账号检查不会发送短信或创建二维码。

## 扫码登录（默认方式）

1. 沿用用户指定的账号别名；未指定时使用稳定别名 `xhs_01`，刷新登录态沿用原别名。

```powershell
conda run --no-capture-output -n crawler python scripts/xhs_client.py login-start --account-id xhs_01
```

2. 读取 `data.session_id` 与 `data.qrcode_path`。立即用绝对路径 Markdown 图片把 PNG 展示在聊天里，提示用户用小红书 App 扫码并在手机上确认。仅终端输出文件路径不算展示。二维码默认位于系统临时目录；可用 `--output` 指定路径，只有明确需要覆盖已有文件时才加 `--force`。
3. 展示后立即执行等待，不必等用户再次回复：

```powershell
conda run --no-capture-output -n crawler python scripts/xhs_client.py login-wait --session-id SESSION_ID --wait-timeout 240
```

这是一次阻塞 HTTP 请求，服务端负责等待扫码、手机确认、正式用户校验、凭据保存和账号入池，客户端无需轮询。使用后台执行和每次不超过 60 秒的工具等待保持聊天可响应。HTTP 请求默认等待 240 秒，覆盖默认 180 秒服务会话；自定义服务会话更长时，相应增加 `--wait-timeout`。二维码本身可能早于 `expires_at` 失效。

4. 只有 `data.status=succeeded` 才说明登录成功。随后运行 `accounts` 检查相同 `account_id` 的入池状态；报告别名、登录结果及可用状态，不声称已抓取数据。
5. 本地超时或连接中断不能判定登录失败。继续使用原 `session_id` 执行 `login-wait`，不要立刻新建二维码；服务端可能仍在等待或提交凭据。二维码过期、会话不存在、服务重启时报告错误，用户准备好重试后再创建新会话。当前 API 不提供登录状态查询和取消路由，不要套用抖音的 `login-status`、`login-cancel` 或短信验证分支。

## 短信登录（用户选择时）

用户选择短信登录并提供手机号时，在交互终端中执行发送命令；等隐藏输入提示出现后输入手机号，避免手机号进入命令行和普通输出。

```powershell
conda run --no-capture-output -n crawler python scripts/xhs_client.py login-sms-send --account-id xhs_01 --zone 86
```

保存 `data.session_id`；`waiting_sms_code` 表示短信步骤已完成，随后向用户索取手机收到的验证码。不能自行生成、猜测或回显验证码。使用相同会话提交：

```powershell
conda run --no-capture-output -n crawler python scripts/xhs_client.py login-sms-verify --session-id SESSION_ID
```

只在出现 `请输入短信验证码：` 时输入用户提供的验证码。命令不接受 `--code` 或 `--phone`，不支持隐藏输入的终端会安全退出；此时改用交互终端，或向用户说明并选择扫码登录。短信接口在校验、保存和入池后返回 `succeeded`，再运行 `accounts` 检查结果。

用户要求重发时，重复 `login-sms-send` 并输入相同手机号、别名和区号，服务端复用活动会话，默认重发间隔 60 秒。验证码错误可在原会话重试；不要自动反复发送短信。

## 故障与边界

接口路径、状态、错误码和配置详见 [references/api.md](references/api.md)，排障时按需阅读。

- 连接或服务未启用：检查根地址和服务状态，保留安全错误码与请求 ID。
- 已有活动会话：复用已知会话；没有会话 ID 时说明需等待原会话过期，不切换别名制造重复会话。
- `XHS_RISK_CONTROL` / `verification_required`：报告平台要求额外安全验证，停止自动尝试；此 API 没有完成验证挑战的接口。
- 凭据保存失败：不得报告登录成功，不读取数据库绕过错误。
- 二维码保存失败：输出保留会话 ID；图片仅创建时返回，不重复调用创建接口抢占活动会话。说明本地文件问题并等待会话过期后重试。
- 不将二维码、手机号、验证码、凭据或真实认证响应打包进 skill 或提交到仓库。
