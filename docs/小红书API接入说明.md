# 小红书 API 接入说明

## 启动与数据库迁移

小红书与抖音运行在同一个 FastAPI 服务中，使用相同数据库和 `crawler_cookie` 表；抖音使用 `CRAWLER_PROJECT_ID`，小红书使用独立的 `XHS_PROJECT_ID`（dev 为 38，prod 为 42）。小红书记录使用 `type=xhs_api_account_v1`，抖音使用 `douyin_api_account_v1`，两个账号池分别调度。

启用小红书前，在配置指定的数据库手动执行 `docker/migrations/001_xhs_remark_text.sql`，将 `remark` 扩为 TEXT。程序不自动更改表结构；旧备注内容保持完整。如果已是 TEXT 或更大容量则无需再次扩容。启动时会检查字段容量。

```powershell
# 本地 Python 使用 crawler 环境，npm 安装本地签名依赖。
conda run -n crawler python -m pip install -r requirements.txt
npm ci --omit=dev --ignore-scripts
conda run --no-capture-output -n crawler python -m uvicorn main:app --host 0.0.0.0 --port 5000 --workers 1
```

Node 必须支持 `zlib.crc32`。启动会检查 Node 能力、crypto-js 和数据库字段容量。`XHS_ENABLED=false` 可以关闭小红书初始化；相应路由返回 503。登录会话和调度状态在进程内，保持单实例、单 worker。

## 凭证保存

`cookie` 仅保存标准 Cookie 字符串。`remark` 为 JSON，包含：

- `version` 与单调增加的 `revision`；
- `user_id`、`login_source`、DSL、webBuild；
- `local_storage`（包括登录得到的 webSsk）、`session_storage`；
- `host_cookie_state`（各域 Cookie 与顺序）；
- `session_state`（签名序列、指纹就绪状态、计数和时间）；
- b1 状态、MNS 环境与实际指纹覆盖配置。

同一账号重新登录更新最新记录；Cookie 与 JSON 在同一事务提交。运行时快照以旧 Cookie/remark 为条件更新，防止旧请求覆盖新登录。关键凭证变化立即保存，普通计数按配置间隔保存，正常停服时保存空闲账号。异常退出可能丢失最后一次快照之后的计数。

加载凭证时不联网，首次业务请求验证正式用户；没有 remark 的历史 Cookie 可以按默认模板恢复。webSsk 可选，有值时参与 X-S 的 x6/x7 计算。完整恢复还会保留按域 Cookie，避免将某个域的 acw_tc 发给其他域。API、状态查询和日志不返回 Cookie、remark 或 SSK。

业务请求开始前按需更新过期的安全材料和 DSL；安全材料刷新保留正式 web_session，并且不调用会创建访客会话的 activate。恢复后仍会验证正式登录用户。

## 短信登录

所有响应均包含 `success`、`request_id`，成功包含 `data`，错误包含 `error`。

本地调试打开 `/api/docs`，展开“小红书 · 登录与账号”，按“1. 发送验证码”“2. 提交验证码并登录”的顺序操作。六个业务接口位于“小红书 · 数据查询”。所有分组默认折叠，可通过筛选栏查找平台。

### 两次请求完成登录

1. `POST /api/v1/xhs/auth/sms/send`：自动初始化设备和签名环境、发送验证码，等待上游发送结果后返回 HTTP 200。

```json
{"account_id":"xhs_01","phone":"13800000000","zone":"86"}
```

成功响应中 `data.status` 为 `waiting_sms_code`，保存 `data.session_id`。

2. `POST /api/v1/xhs/auth/sms/login`：提交验证码，等待正式用户验证、数据库保存和账号入池完成后返回 HTTP 200。

```json
{"session_id":"第一步返回的session_id","code":"123456"}
```

成功响应中 `data.status` 为 `succeeded`，可以直接调用六个数据接口，无需轮询或额外查询账号。

本地 PowerShell 示例（先启动服务，将手机号换成自己的）：

```powershell
$baseUrl = 'http://127.0.0.1:5000/api/v1/xhs'
# 第一次请求：初始化并发送验证码。
$sendBody = @{ account_id = 'xhs_01'; phone = '你的手机号'; zone = '86' } | ConvertTo-Json
$sent = Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/sms/send" -ContentType 'application/json' -Body $sendBody -TimeoutSec 300
# 收到短信后，第二次请求：校验并保存登录状态。
$smsCode = Read-Host '请输入短信验证码'
$loginBody = @{ session_id = $sent.data.session_id; code = $smsCode } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/sms/login" -ContentType 'application/json' -Body $loginBody -TimeoutSec 300
```

验证码错误返回 422，可使用同一 `session_id` 重试。重发时再次调用 `/auth/sms/send`，保持账号、手机号和区号一致，自动复用活跃会话；默认 60 秒内重发返回 429。上游要求额外安全验证返回 429，`error.details.status` 为 `verification_required`。

这两个接口会等待网络操作，请为客户端及反向代理设置足够的响应超时。登录会话默认有效期为 300 秒，过期后重新发送验证码。等待请求取消后，已启动的后台步骤会继续；登录请求超时可通过 `GET /api/v1/xhs/auth/accounts` 检查账号是否已入池，避免立即重复登录。发送请求超时后，请先等待当前步骤完成，再使用相同账号和手机号重发。

同一账号或手机号只允许一个活跃会话，短信与扫码共用账号冲突检查。过期与终止会话自动清理。上游要求额外安全验证时进入 `verification_required`，本版本不自动处理图形验证。

## 扫码登录

扫码同样只需要两个接口，使用已迁移的 PC 端纯 HTTP 签名链，不启动浏览器。Swagger 中展开“小红书 · 登录与账号”，使用“扫码 1”“扫码 2”。

1. `POST /api/v1/xhs/auth/qr/create`：生成二维码。

```json
{"account_id":"xhs_qr_01"}
```

成功返回 `data.session_id`、`data.status=waiting_scan` 和 `data.qrcode_data_url`。二维码为 Base64 PNG Data URL，可直接用作网页 `<img>` 的 `src`。服务只返回二维码图片，不返回上游内部 qr_id/code 或 Cookie。

2. 展示二维码后，调用 `POST /api/v1/xhs/auth/qr/login`，使用小红书 App 扫码并在手机上确认登录。也可以先完成手机操作再调用此接口。

```json
{"session_id":"二维码接口返回的session_id"}
```

服务端每约 2 秒检查扫码状态，等待正式登录确认，再校验当前用户身份、保存 Cookie/remark 并加入账号池。返回 `data.status=succeeded` 后可直接调用数据接口。登录来源在 remark 中标记为 `qrcode`，缓存与短信登录相同。

本地 PowerShell 示例：

```powershell
$baseUrl = 'http://127.0.0.1:5000/api/v1/xhs'
# 创建二维码并保存到本地供手机扫描。
$qrBody = @{ account_id = 'xhs_qr_01' } | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/qr/create" -ContentType 'application/json' -Body $qrBody -TimeoutSec 300
$qrFile = Join-Path $env:TEMP 'xhs-login-qr.png'
[System.IO.File]::WriteAllBytes($qrFile, [Convert]::FromBase64String(($created.data.qrcode_data_url -split ',', 2)[1]))
Invoke-Item $qrFile
# 发起等待请求后，在手机 App 中扫码并确认。
$loginBody = @{ session_id = $created.data.session_id } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/qr/login" -ContentType 'application/json' -Body $loginBody -TimeoutSec 300
```

二维码会话默认从创建开始计时，180 秒有效；上游可能更早判定过期，过期返回 410，需要重新生成。请将客户端及代理响应超时设置为大于二维码有效期并留出网络和持久化时间。等待请求中断后，后台继续执行；使用同一 session_id 再次调用扫码登录可继续等待或取得已完成结果，不会重复提交凭证。服务重启后未完成会话失效，已成功登录账号从数据库恢复。

二维码生成、扫码状态或正式用户校验失败不会保存账号。数据库保存失败返回 502；上游风控返回 429 且状态为 verification_required，额外验证仍需后续接入。短信登录的旧异步会话接口保持删除。

## 六个数据接口

ID 为 24 位十六进制字符串。支持完整 `www.xiaohongshu.com` HTTPS 笔记或主页链接；短链接需要调用方先解析。链接与直接 ID 二选一，冲突参数返回 422。

### 笔记详情

`POST /api/v1/xhs/note_info`

```json
{"note_id":"aaaaaaaaaaaaaaaaaaaaaaaa","xsec_token":"从链接或查询结果取得","xsec_source":"pc_search"}
```

或传 `url`，自动解析 token/source。`data.note` 保留原始 feed 条目，包括 `note_card`。

### 搜索笔记

`POST /api/v1/xhs/search_notes`

```json
{"keyword":"猫","page":1,"sort":"general","note_type":0}
```

响应含 `items`、`page`、`next_page`、`has_more`、`search_id`、`search_session_id`。翻页必须续传两个搜索 ID，关键词、排序及筛选保持一致。每次获取一页，沿用上游 20 条页大小。

排序：`general`、`time_descending`、`popularity_descending`、`comment_descending`、`collect_descending`。`note_type`：0 不限、1 视频、2 图文；`note_time`：0 不限、1 一天内、2 一周内、3 半年内；`note_range`：0 不限、1 已看、2 未看、3 已关注；`pos_distance`：0 不限、1 同城、2 附近；`geo` 为上游地理参数字符串。

### 用户信息和用户笔记

`POST /api/v1/xhs/user_info`

```json
{"user_id":"bbbbbbbbbbbbbbbbbbbbbbbb"}
```

或传 `user_url`。结果位于 `data.user`，保留上游用户结构。

`POST /api/v1/xhs/user_notes`

```json
{"user_id":"bbbbbbbbbbbbbbbbbbbbbbbb","cursor":"","xsec_token":"","xsec_source":"pc_search"}
```

主页链接中的 token/source 会自动提取。上游允许的用户笔记请求可不带 token，实际访问要求由上游决定。每次获取一页，沿用 30 条页大小。

### 一级评论和评论回复

`POST /api/v1/xhs/note_comments`

```json
{"note_id":"aaaaaaaaaaaaaaaaaaaaaaaa","xsec_token":"从笔记链接取得","cursor":""}
```

也支持完整笔记 `url`。一级评论按上游默认页大小，不自动遍历回复。

`POST /api/v1/xhs/note_sub_comments`

```json
{"note_id":"aaaaaaaaaaaaaaaaaaaaaaaa","root_comment_id":"cccccccccccccccccccccccc","xsec_token":"从笔记链接取得","cursor":""}
```

首次回复游标可使用一级评论条目中的 `sub_comment_cursor`。每次获取一页，沿用 10 条页大小。

用户笔记与两种评论统一返回 `items`、`next_cursor`、`has_more`。后续页原样传递 `next_cursor`。条目保留原始字段，包括 token、回复游标等。所有业务响应包含实际 `account_id` 和 `failover_count`。

## 运行参数与错误

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| XHS_ENABLED | true | 启用小红书，需先迁移 remark |
| XHS_MAX_CONCURRENT_REQUESTS | 1 | 小红书入口并发，每账号固定为 1；实际上游业务请求统一串行 |
| XHS_ACCOUNT_COOLDOWN_SECONDS | 900 | 风控后账号冷却秒数；上游 Retry-After 更长时使用上游值 |
| XHS_REQUEST_INTERVAL_SECONDS | 5 | 上一次业务请求结束后至少等待的秒数，支持非负小数 |
| XHS_REQUEST_JITTER_SECONDS | 5 | 请求间隔及退避额外随机等待的最大秒数 |
| XHS_REQUEST_QUEUE_TIMEOUT_SECONDS | 30 | 入口及账号槽位的等待预算；传输阶段另有同长度的排队与重试预算，均包含各自阶段已用时间 |
| XHS_UPSTREAM_MAX_RETRIES | 2 | 上游 HTTP 503 的最大重试次数，0～10；不含首次请求 |
| XHS_UPSTREAM_BACKOFF_SECONDS | 5 | 上游 503 指数退避基数，必须大于零 |
| XHS_UPSTREAM_BACKOFF_MAX_SECONDS | 60 | 自动退避上限，不得小于基数；上游 Retry-After 可以超过此上限 |
| XHS_SNAPSHOT_INTERVAL_SECONDS | 60 | 普通状态快照间隔 |
| XHS_SMS_SESSION_TIMEOUT_SECONDS | 300 | 短信登录会话有效时间，最低 300 秒；更小的配置按 300 秒处理 |
| XHS_SMS_SESSION_RETENTION_SECONDS | 300 | 短信与扫码终止状态保留时间 |
| XHS_SMS_RESEND_INTERVAL_SECONDS | 60 | 同一手机号发送间隔 |
| XHS_SMS_MAX_SESSIONS | 10 | 短信与扫码合计最大活跃会话数 |
| XHS_SMS_MAX_CODE_ATTEMPTS | 5 | 每会话最多提交验证码次数 |
| XHS_QR_SESSION_TIMEOUT_SECONDS | 180 | 二维码会话有效时间 |

账号池刷新沿用 `ACCOUNT_REFRESH_INTERVAL_SECONDS`。`/api/health` 的 `data.xhs` 返回小红书启用状态及账号统计。认证过期返回 401，移出过期账号并最多切换一次；风控只冷却实际账号，不立即重试或切换账号，业务错误不冷却账号。

六项业务接口及其 `/user/me` 登录校验共享进程内节流器，包括分页和重试；等待结束后再签名，重试重新生成签名。默认每次请求完成后等待 5～10 秒，默认单个账号冷却 15 分钟。短信发送、扫码轮询和安全材料刷新保留各自原有流程，不自动重放登录操作。抖音的 `SEARCH_VIDEOS_INTERVAL_SECONDS` 不控制小红书。

上游 503 使用指数退避并最多重试两次；支持秒数及 HTTP 日期格式的 `Retry-After`，不会提前重试。如果等待超过本次预算，直接返回剩余等待时间；等待期内新请求不会继续访问上游，等待结束后串行探测，成功后清除故障计数。网络超时、401、429、其他 HTTP 错误和业务错误不自动重试。传输预算限制排队和再次发起请求，已经开始的签名、网络请求仍受各自超时控制，不是整个 API 的硬超时。

| HTTP / error.code | details.reason | 调用方处理 |
| --- | --- | --- |
| 429 / XHS_RISK_CONTROL | account_risk_control | 停止当前任务，等待冷却；出现验证要求时先完成正常验证 |
| 503 / XHS_NO_AVAILABLE_ACCOUNT | accounts_cooling | 按等待时间暂停，再从原页或原 cursor 继续 |
| 503 / XHS_NO_AVAILABLE_ACCOUNT | no_valid_accounts | 检查账号池及登录状态，不要无限重试 |
| 503 / XHS_NO_AVAILABLE_ACCOUNT | account_busy | 降低并发，稍后重试 |
| 503 / XHS_REQUEST_QUEUE_FULL | queue_timeout | 降低提交频率，等待后重试 |
| 503 / XHS_UPSTREAM_UNAVAILABLE | upstream_backoff | 上游临时故障，按等待时间重试 |
| 503 / XHS_SERVICE_UNAVAILABLE | 无 | 检查服务是否启用及初始化是否成功 |

存在等待提示的 429/503 同时返回 `Retry-After` 响应头和 `error.details.retry_after_seconds`。调用方必须遵守等待时间，不应收到 503 后立即循环重试。建议保存每页结果和分页断点、按笔记 ID 去重，按需抓取评论回复；调低频率不能保证上游不再限制访问。

以上调度与冷却状态均在进程内，服务重启会清空；维持单实例、单 worker。多实例部署需要先改为共享的限速与冷却存储。浏览器扩展直接访问小红书，不经过此服务端节流器。

## 验证

```powershell
# 离线测试不会发送真实短信或查询真实账号。
conda run --no-capture-output -n crawler python -m pytest tests/test_xhs_request_policy.py tests/test_xhs_api.py tests/test_xhs_credentials.py tests/test_xhs_login.py tests/test_xhs_qr_login.py tests/test_xhs_signing.py -q
```

真实短信登录、扫码登录与六个接口仍需要有效账号及目标链接进行联调。迁移 SQL、Docker 构建和真实上游验证应分别完成，离线测试不代表上游始终接受签名。
