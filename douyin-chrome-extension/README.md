# 短视频数据助手 Chrome 插件

这是一个独立运行的 Manifest V3 插件，不需要启动本仓库的 FastAPI 服务，也不使用数据库。
插件使用当前 Chrome 配置文件中的平台登录状态。搜索和评论请求在当前平台标签页中发起，
用户作品请求由扩展 Service Worker 直接发送。

## 功能

- 通过当前 Cookie Store 和页面账号状态检测登录，兼容缺少 `s_v_web_id` 的会话。
- 根据当前活动标签页自动识别搜索页、用户主页和作品页。
- 搜索综合作品，支持数量、排序、发布时间、时长、范围和内容类型参数。
- 按总数量自动分页读取用户作品，接口持续拒绝时回退到主页滚动读取。
- 按总数量自动分页获取视频评论，可选择是否包含二级回复。
- 预览结果并导出 JSON 或 CSV。
- 用户输入参数保存在扩展本地存储中，Cookie 不会写入扩展存储。

## 安装

1. 在 Chrome 地址栏打开 `chrome://extensions/`。
2. 开启右上角的“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择本目录 `douyin-chrome-extension`。
5. 打开任意 `https://www.douyin.com/` 页面并完成登录。
6. 点击工具栏中的“短视频数据助手”，侧边栏会自动识别当前页面。

## 使用

搜索页面会自动填写路径中的关键词，用户主页会自动填写 `sec_user_id`，作品页会自动填写作品 ID。
也可以切换功能后手动粘贴链接或输入 ID；用户作品的同一输入框会自动识别完整主页链接和 `sec_user_id`，保留链接查询参数。用户作品只需要设置总数量，插件按每页 18 条自动管理
游标和分页；只要当前是已登录的平台标签页，不要求停留在目标用户主页。评论功能只需要设置
总数量和“包含回复”开关，
请求间隔、游标与每页数量由插件自动管理。遇到安全验证时应停止任务，
在平台页面完成验证后再继续。

## 隐私政策与商店提交

隐私政策正文见 [privacy-policy.html](privacy-policy.html)，依据 `manifest.json` 及当前扩展实现编写。
政策涵盖登录 Cookie、浏览器环境、查询条件、作品/评论结果、抖音和字节跳动接口传输、
本地保存与删除方式，以及 Chrome 应用商店的有限使用声明。

在 Chrome 应用商店开发者信息中心选择该扩展，在“隐私权”标签页的“隐私权政策”指定字段
填写公开网页 URL，然后保存并重新提交审核。不要把链接只放在产品说明里；本地 HTML 文件
和 `chrome-extension://` 地址不能代替公开 URL。商店的数据使用披露应与政策和实现保持一致。

## 权限用途

| 权限 | 用途 |
| --- | --- |
| `cookies` | 检测平台登录 Cookie |
| `declarativeNetRequestWithHostAccess` | 为 mssdk 换取 token 的请求临时设置会话请求头 |
| `activeTab` | 识别用户当前操作的标签页 |
| `scripting` | 在当前平台页面中使用登录状态发起同源请求 |
| `sidePanel` | 提供不会因弹窗关闭而中断的操作界面 |
| `storage` | 保存用户填写的非敏感参数 |
| `webNavigation` | 识别目标平台单页应用内部的 URL 切换 |
| `https://*.douyin.com/*` | 仅允许操作目标平台域名 |
| `https://mssdk.bytedance.com/*` | 换取用户作品查询使用的动态 `msToken` |

## 实现说明

- 评论列表使用从 `utils/ab_pure.py` 移植的纯 JavaScript `a_bogus` 签名。
- 综合搜索使用项目内 `js/bdms_1.0.1.19_fix.js` 的完整 BDMS 签名。
- 评论回复沿用项目规则，使用 BDMS 签名末尾 87 个字符。
- 开启“包含回复”后，一级评论与回复合计不会超过设置的数量。
- 搜索和用户作品结果会展示并导出点赞、评论、转发和收藏数量。
- 搜索、一级评论和评论回复遇到临时拒绝或无效响应时会重新签名并有限重试。
- 用户作品优先调用作品列表接口，随机 403 时重新签名并有限重试。
- 用户作品查询 token 通过 mssdk 动态生成，并按当前浏览器 `ttwid` 和指纹上报数据换取。
- 动态 `msToken` 缓存 10 分钟；连续两次 403 后强制刷新一次，最多尝试 5 次。
- 用户作品在 Service Worker 中直接请求，避免页面安全 SDK 追加或重排查询参数。
- 用户作品参数顺序与后端 API 一致，请求 Cookie 会移除旧的 `msToken`。
- 用户作品使用与 `utils/user_read` 对齐的主站 `a_bogus` 和 webSign 双签名，携带 `uifid`、`timestamp`、`x-secsdk-web-signature`，指纹参数在生成 `a_bogus` 后追加。
- 用户作品直接读取列表，不额外请求用户资料。Service Worker 原样发送签名 URL，并串行管理临时请求头规则；规则只作用于扩展自身请求。
- 接口持续被拒绝且当前正好是目标用户主页时，自动滚动读取主页作品，不请求用户信息接口。
- 浏览器 UA、屏幕尺寸、硬件并发数等参数从当前平台标签页读取，避免使用后端固定指纹。
- Cookie 查询覆盖当前页面 URL、主域名和子域名；诊断信息只展示 Cookie 名称，不展示值。
- API 原始结果仅保存在侧边栏内存中；关闭侧边栏或刷新扩展后结果会消失。

## 本地测试

在仓库根目录执行：

```powershell
node douyin-chrome-extension/tests/extension-tests.mjs
node douyin-chrome-extension/tests/service-worker-tests.mjs
node --check douyin-chrome-extension/lib/mstoken.js
node --check douyin-chrome-extension/sidepanel.js
node --check douyin-chrome-extension/service-worker.js
```

测试包含 URL 路由、Cookie 判定、SM3 标准向量、Python/JavaScript 固定签名向量以及四类请求构造。

## 注意事项

目标平台 Web 接口和安全规则可能调整。插件不会绕过验证码、账号权限或平台限制，也不提供代理轮换、
批量账号或自动规避风控能力。请仅处理自己有权访问和使用的数据，并控制请求频率。

隐私政策公开网址：https://dannydu26-video-helper-privacy.dannyduo26.chatgpt.site
