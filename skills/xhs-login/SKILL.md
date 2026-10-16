---
name: xhs-login
description: "Use when the user asks to log in to the Spider_XHS project with a Xiaohongshu PC QR code, refresh the local login cookie, or verify the saved login session."
---

# 小红书扫码登录

在 `Spider_XHS` 项目中执行小红书 PC 扫码登录，或验证已经保存的登录态。

本技能存放在 `DouYin_Spider/skills/xhs-login/`，登录实现依赖本机的 `E:/OpenSource/crawler/Spider_XHS`。运行前切换到该目录；Cookie 写入该项目的 `.env`。本技能不操作 DouYin_Spider 服务中的账号存储。

## 工作流

1. 确认当前工作目录是包含 `save_login.py`、`.env` 和 `xhs_utils/` 的 `Spider_XHS` 项目根目录。
2. 只有用户明确要求登录或刷新登录态时，才执行会话变更；普通查询不得自动触发扫码。
3. 使用项目现有入口运行扫码登录，不要重新实现登录协议：

   ```powershell
   Set-Location E:/OpenSource/crawler/Spider_XHS
   [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
   conda run --no-capture-output -n crawler python save_login.py --qr-image "$env:TEMP/xhs-login-qr.png"
   ```

4. 在进程运行期间等待 PNG 文件生成，然后使用绝对路径 Markdown 图片直接在聊天中显示二维码，并提示用户用小红书 App 扫码确认。后台工具输出和排队中的终端面板不代表用户已经看到二维码。等待本次登录完成或二维码失效；失效后停止并报告，待用户准备好再生成新码，不自动反复重试。
5. 登录成功后，`save_login.py` 会把 Cookie 写入项目根目录 `.env` 的 `COOKIES` 字段。不要在终端、日志或回复中打印 Cookie 的值。
6. 登录完成后检查 `.env` 存在且 `COOKIES` 非空；可用项目的 Cookie 鉴权入口做一次轻量验证，例如读取用户信息或执行用户指定的查询。
7. 无论成功还是失败，都释放认证对象和 HTTP 连接。报告结果时只说明成功、失败原因和保存位置。

## 约束

- 仅用于用户本人或已获授权的账号和环境。
- 复用 `save_login.py`、`XHSPcAuth.from_qrcode_login` 和项目现有 Cookie 存储方式，不修改签名算法。
- 不把 `.env`、Cookie、二维码令牌或完整认证响应加入提交、日志或技能资源。
- 若终端编码导致中文乱码，优先使用 `conda run --no-capture-output` 和 PowerShell UTF-8 输出设置。
