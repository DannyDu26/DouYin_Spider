# 抖音爬虫内部 API 服务

基于 FastAPI 的公司内部抖音数据抓取服务，提供作品详情、视频一级与二级评论、用户作品和关键词搜索接口，并通过扫码登录维护 MySQL 多账号 Cookie 池。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Internal_API-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MySQL](https://img.shields.io/badge/MySQL-Credential_Pool-4479A1?logo=mysql&logoColor=white)](https://www.mysql.com/)

> 本服务仅面向受控公司内网。请遵守适用法律、平台规则和公司数据安全制度，不得用于未经授权的数据采集或其他违法违规用途。

## 项目能力

- FastAPI HTTP 服务，Docker 默认监听 0.0.0.0:5000。
- 批量抓取作品详情，每次支持 1–20 个作品链接。
- 分页抓取视频一级评论，每页支持 1–50 条。
- 分页抓取指定一级评论的二级评论，每页支持 1–50 条。
- 按用户主页抓取 1–10 页作品。
- 关键词搜索作品，单次最多返回 100 条。
- Playwright 扫码登录，可依次保存和刷新多个账号。
- MySQL 按账号保存或更新凭证，内存中维护可用、冷却和无效状态。
- 服务启动时加载账号，并默认每 300 秒从 MySQL 合并一次最新凭证。
- 抓取请求自动轮询账号；明确认证失败时冷却当前账号并切换账号重试一次。
- 全局并发和单账号并发分别受限，避免单个账号被并发请求集中使用。
- 标准化 JSON 响应、服务端 request_id、凭证脱敏日志及健康检查。
- API 链路不创建业务目录、不写 Excel、不下载媒体，也不生成 info.json 或 detail.txt。

## API 与旧版命令行的边界

main.py 是 FastAPI 服务入口。所有 API 抓取结果只通过 JSON 返回，运行进度和错误只输出到标准输出。

scripts/legacy_cli.py 保留原项目的 Excel、媒体和详情文件落盘能力，只有显式执行该脚本时才会写文件。API 服务不会导入或调用旧版落盘函数。

底层仍保留直播、私信和其他抖音能力，但当前没有将这些能力开放为 HTTP 接口。

## API 列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/health | 服务、数据库和账号池健康状态 |
| GET | /api/v1/douyin/auth/accounts | 账号别名及运行状态，不返回任何凭证 |
| POST | /api/v1/douyin/auth/qr-sessions | 为指定账号创建扫码登录会话 |
| GET | /api/v1/douyin/auth/qr-sessions/{session_id} | 查询扫码会话状态 |
| POST | /api/v1/douyin/auth/qr-sessions/{session_id}/sms/request | 请求身份验证短信验证码 |
| POST | /api/v1/douyin/auth/qr-sessions/{session_id}/sms/verify | 提交身份验证短信验证码 |
| DELETE | /api/v1/douyin/auth/qr-sessions/{session_id} | 取消未完成的扫码会话 |
| POST | /api/v1/douyin/video_info | 批量抓取 1–20 个作品 |
| POST | /api/v1/douyin/video_comments | 分页抓取视频一级评论 |
| POST | /api/v1/douyin/video_sub_comments | 分页抓取指定一级评论的二级评论 |
| POST | /api/v1/douyin/user_videos | 按用户主页抓取 1–10 页作品 |
| POST | /api/v1/douyin/search_videos | 按关键词搜索最多 100 个作品 |

服务启动后可以直接使用：

- Swagger 调试页面：http://127.0.0.1:5000/api/docs
- ReDoc 接口文档：http://127.0.0.1:5000/api/redoc
- 健康检查：http://127.0.0.1:5000/api/health

## 快速开始

### 1. 环境要求

- Python 3.10+
- MySQL 兼容数据库
- Playwright Chromium，用于扫码登录
- Docker，可选

当前签名实现为纯 Python，启动 API 不需要安装 Node.js。

### 2. 安装依赖

~~~bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
python -m playwright install chromium
~~~

### 3. 准备开发配置

Linux 或 macOS：

~~~bash
cp .env.example .env
cp .env.dev.example .env.dev
~~~

Windows PowerShell：

~~~powershell
Copy-Item .env.example .env
Copy-Item .env.dev.example .env.dev
~~~

.env 只负责选择环境：

~~~dotenv
APP_ENV=dev
~~~

在 .env.dev 中配置本地数据库：

~~~dotenv
APP_ENV=dev

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=auto_crawler
CRAWLER_PROJECT_ID=22
MYSQL_USER=crawler_dev
MYSQL_PASSWORD=请替换为本地密码
MYSQL_SSL_DISABLED=true
~~~

环境文件加载顺序：

1. 如果设置 APP_ENV_FILE，则将它作为完整配置文件加载。
2. 否则进程环境变量 APP_ENV 优先。
3. 进程未设置 APP_ENV 时，只从根目录 .env 读取 APP_ENV。
4. 根据环境加载 .env.dev 或 .env.prod；未指定环境时默认为 dev。
5. 操作系统或容器中已经存在的环境变量不会被配置文件覆盖。

如果 PowerShell 中曾设置生产环境变量，可在本地启动前清理：

~~~powershell
Remove-Item Env:APP_ENV -ErrorAction SilentlyContinue
Remove-Item Env:APP_ENV_FILE -ErrorAction SilentlyContinue
~~~

### 4. 准备 MySQL 表

服务统一使用现有 auto_crawler.crawler_cookie 表，不再读取 crawler.crawler_cookie。账号只需要该表的 SELECT、INSERT 和 UPDATE 权限。

~~~sql
CREATE DATABASE IF NOT EXISTS auto_crawler
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE auto_crawler;

CREATE TABLE IF NOT EXISTS crawler_cookie (
  id bigint NOT NULL AUTO_INCREMENT COMMENT 'ID',
  project_id int NOT NULL COMMENT '项目ID',
  type varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Cookie类型',
  account_id varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '账号ID',
  cookie text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Cookie内容',
  remark varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '备注',
  create_time datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_project_type_account_id (project_id,type,account_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='项目Cookie管理表';
~~~

数据库名和项目 ID 由环境配置决定。示例 dev/prod 均配置 `MYSQL_DATABASE=auto_crawler`、`CRAWLER_PROJECT_ID=22`；服务只操作该项目下 `type=douyin_api_account_v1` 的记录。

| 表字段 | 本项目用途 |
| --- | --- |
| id | 自增凭证记录 ID；历史重复记录存在时使用最大 id 的记录 |
| project_id | 使用 CRAWLER_PROJECT_ID 配置值，示例为 22 |
| type | 固定为 douyin_api_account_v1 |
| account_id | 稳定账号别名，新记录使用该字段分组 |
| cookie | 新记录保存分号间隔的标准 Cookie 字符串；兼容历史版本化凭证 JSON |
| remark | 普通备注，不参与账号识别 |
| create_time | 凭证创建或刷新时间 |

扫码成功后会在当前 `CRAWLER_PROJECT_ID` 下按 `type + account_id` 查找记录：存在时更新最新一条记录的 Cookie 和刷新时间，不存在时才执行 INSERT。remark 不参与账号识别；若存在历史重复记录，读取和更新都以最大 id 的记录为准。

扫码生成的新记录使用 `sessionid=...; s_v_web_id=...` 形式的标准 Cookie 字符串。加载时仍会校验登录 Cookie 和 s_v_web_id；不符合要求的记录会标记为 invalid。历史版本化凭证 JSON 和被 JSON 引号包裹的 Cookie 字符串仍可继续读取。

### 5. 启动服务

~~~bash
python -m uvicorn main:app --host 0.0.0.0 --port 5000 --workers 1
~~~

数据库为空时服务可以启动，此时健康状态为 not_authenticated；数据库不可连接时服务启动失败。

## 扫码添加账号

account_id 是内部稳定账号别名，长度为 1–64 位，只允许小写字母、数字、下划线或连字符，并且首字符必须是小写字母或数字。单个服务进程同时只允许一个活跃扫码会话。

开发环境可设置 `QR_LOGIN_HEADLESS=false` 显示浏览器窗口。若抖音进入“验证码中间页”，请在该窗口手工完成验证，服务会继续等待登录入口和二维码；需要使用本机 Chrome 时再设置 `PLAYWRIGHT_BROWSER_CHANNEL=chrome`。修改配置后必须重启服务。

### Linux 无桌面环境使用 Xvfb

Linux 服务器没有桌面环境、且抖音在 `QR_LOGIN_HEADLESS=true` 下进入验证码中间页时，可以使用 Xvfb 提供虚拟显示，并让 Playwright 以可视模式运行。Xvfb 只负责虚拟屏幕；二维码仍由扫码会话接口以内存 Base64 PNG 返回，不会自动写入服务器磁盘。

Ubuntu 或 Debian 安装依赖：

~~~bash
sudo apt-get update
sudo apt-get install -y xvfb xauth x11-utils
python -m playwright install --with-deps chromium
~~~

先验证 Xvfb：

~~~bash
xvfb-run -a sh -c 'echo "DISPLAY=$DISPLAY"; xdpyinfo >/dev/null && echo "Xvfb OK"'
~~~

看到 `DISPLAY=:数字` 和 `Xvfb OK` 表示虚拟显示可用。在 `.env.dev`、`.env.prod` 或进程环境变量中关闭 Playwright 无头模式：

~~~dotenv
QR_LOGIN_HEADLESS=false
~~~

通过 Xvfb 启动单 worker 服务：

~~~bash
xvfb-run -a -s "-screen 0 1280x960x24" \
  python -m uvicorn main:app --host 0.0.0.0 --port 5000 --workers 1
~~~

服务启动后，按下一节创建扫码会话。创建响应中存在 `data.qrcode_data_url`，即表示 Xvfb、Chromium 和二维码提取链路均正常。若要作为后台服务运行，应将同一条 `xvfb-run` 命令写入 systemd 的 `ExecStart`，并继续保持单进程、单实例。

扫码登录异常时，可临时开启页面调试截图：

~~~dotenv
QR_DEBUG_SCREENSHOT_ENABLED=true
~~~

开启后，每个会话会在整个登录流程中按 5 秒间隔覆盖保存最新页面。生产环境写入 `${LOG_DIR}/qr-debug/qr-login-<session_id>-latest.png`，未配置 `LOG_DIR` 时写入项目的 `logs/qr-debug`。截图可能包含账号信息和短信验证码；排查结束后应关闭开关并删除截图。

常见问题：

- `xvfb-run: error: xauth command not found`：安装 `xauth`。
- `Executable doesn't exist`：执行 `python -m playwright install --with-deps chromium`。
- 返回 `QR_VERIFICATION_REQUIRED`：确认 `QR_LOGIN_HEADLESS=false` 已被当前环境文件加载，并重启服务。
- 二维码未出现：尝试增大虚拟屏幕，例如 `-screen 0 1920x1080x24`，并检查服务器是否能访问抖音。

创建扫码会话：

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/auth/qr-sessions" \
  -H "Content-Type: application/json" \
  -d '{"account_id":"marketing-01"}'
~~~

创建响应中的 data.qrcode_data_url 是 Base64 PNG Data URL，只返回这一次。可以把它展示为图片或复制到浏览器地址栏扫码。

查询扫码状态：

~~~bash
curl "http://127.0.0.1:5000/api/v1/douyin/auth/qr-sessions/会话ID"
~~~

如果扫码后状态变为 `verification_required`，先在同一会话中请求短信验证码；已经处于 `waiting_sms_code` 时再次调用同一接口会重新发送：

~~~bash
curl -X POST \
  "http://127.0.0.1:5000/api/v1/douyin/auth/qr-sessions/会话ID/sms/request"
~~~

轮询到 `waiting_sms_code` 后提交手机收到的 4～8 位数字验证码：

~~~bash
curl -X POST \
  "http://127.0.0.1:5000/api/v1/douyin/auth/qr-sessions/会话ID/sms/verify" \
  -H "Content-Type: application/json" \
  -d '{"code":"123456"}'
~~~

提交后继续轮询，成功时状态为 `succeeded`。完整状态包括 starting、waiting_scan、verification_required、requesting_sms、waiting_sms_code、verifying_sms、committing、succeeded、expired、failed 和 cancelled。短信验证码只在登录任务内存中短暂传递，不会写入应用日志或数据库。

取消扫码：

~~~bash
curl -X DELETE "http://127.0.0.1:5000/api/v1/douyin/auth/qr-sessions/会话ID"
~~~

查询账号池：

~~~bash
curl "http://127.0.0.1:5000/api/v1/douyin/auth/accounts"
~~~

账号列表仅包含 account_id、credential_id、updated_at、status 和 cooldown_until，绝不返回 Cookie、Ticket、证书或私钥。

## 抓取接口示例

### 批量作品详情

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/video_info" \
  -H "Content-Type: application/json" \
  -d '{
    "video_id": "作品ID"
  }'
~~~

video_id 与 urls 二选一；urls 可提交 1～20 个作品链接。批量接口允许部分成功。成功结果位于 data.items，失败项目位于 data.errors；全部失败时返回 HTTP 502。

### 视频一级评论

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/video_comments" \
  -H "Content-Type: application/json" \
  -d '{
    "video_id": "作品ID",
    "cursor": 0,
    "count": 20
  }'
~~~

video_id 与 url 二选一。cursor 默认为 0，count 默认为 20、范围为 1–50。使用响应中的 data.next_cursor 请求下一页；data.has_more 表示是否还有更多评论。当前接口只抓取一级评论，不自动抓取回复。

### 视频二级评论

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/video_sub_comments" \
  -H "Content-Type: application/json" \
  -d '{
    "video_id": "作品ID",
    "comment_id": "一级评论ID",
    "cursor": 0,
    "count": 20
  }'
~~~

video_id 和 comment_id 必填。cursor 默认为 0，count 默认为 20、范围为 1–50。后续分页使用响应中的 data.next_cursor，并根据 data.has_more 判断是否还有更多回复。

### 用户作品

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/user_videos" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "用户主页路径中的用户ID",
    "page_num": 2
  }'
~~~

user_id 与 user_url 二选一；user_id 是用户主页 `/user/{id}` 路径中的 ID（即 sec_user_id）。page_num 默认 1，范围为 1–10。

### 搜索作品

~~~bash
curl -X POST "http://127.0.0.1:5000/api/v1/douyin/search_videos" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "榴莲",
    "limit": 20,
    "sort_type": "0",
    "publish_time": "0",
    "filter_duration": "",
    "search_range": "0",
    "content_type": "0"
  }'
~~~

| 字段 | 允许值 |
| --- | --- |
| limit | 1–100，默认 20 |
| sort_type | 0 综合排序；1 最多点赞；2 最新发布 |
| publish_time | 0 不限；1 一天内；7 一周内；180 半年内 |
| filter_duration | 空字符串不限；0-1 一分钟内；1-5 一至五分钟；5-10000 五分钟以上 |
| search_range | 0 不限；1 最近看过；2 还未看过；3 关注的人 |
| content_type | 0 不限；1 视频；2 图文 |

## 响应格式

成功响应：

~~~json
{
  "success": true,
  "request_id": "服务端生成的请求ID",
  "data": {
    "items": [],
    "account_id": "marketing-01",
    "failover_count": 0
  }
}
~~~

每个响应还会通过 X-Request-ID 响应头返回相同的服务端请求 ID，便于关联标准输出日志。

失败响应：

~~~json
{
  "success": false,
  "request_id": "服务端生成的请求ID",
  "error": {
    "code": "UPSTREAM_ERROR",
    "message": "安全的错误说明"
  }
}
~~~

常见状态码：

| HTTP 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| 422 | INVALID_REQUEST | 请求字段或抖音链接校验失败 |
| 429 | UPSTREAM_RISK_CONTROL | 上游返回明确的访问频率或安全验证信号 |
| 502 | UPSTREAM_ERROR | 抖音上游网络或响应异常 |
| 503 | NO_AVAILABLE_ACCOUNT | 没有有效账号或账号全部处于冷却状态 |
| 500 | INTERNAL_ERROR | 未处理的服务内部异常 |

扫码接口还会按具体场景返回 QR_SESSION_ACTIVE、QR_SESSION_NOT_FOUND、QR_VERIFICATION_REQUIRED、QR_LOGIN_FAILED 等安全错误码。

## 多账号轮询行为

- 服务启动时只加载 type=douyin_api_account_v1 的每账号最新记录。
- 服务运行期间默认每 300 秒重新查询 MySQL；外部新增的更高版本会自动进入内存账号池，本进程扫码更新会立即刷新账号池。
- 定时刷新失败时保留现有账号继续服务，下一周期自动重试。
- 刷新只接受更大的凭证 ID，避免并发扫码刚写入的新凭证被旧查询结果覆盖。
- 可用账号按照内存游标轮询，单个抓取请求固定使用同一认证快照。
- HTTP 401/403、明确登录失效响应、缺少必要登录 Cookie 或明确风控信号会使账号进入冷却。
- 内容不存在、参数问题和普通网络错误不会摘除账号。
- 认证失败后最多选择另一个账号完整重试一次。
- 成功抓取响应包含实际使用的 account_id 和 failover_count。
- 冷却次数在当前进程内累计；达到配置阈值后移出账号池，并精确删除对应数据库凭证记录。

## 配置项

| 配置项 | 示例或默认值 | 说明 |
| --- | --- | --- |
| APP_ENV | dev | 仅支持 dev、prod |
| APP_ENV_FILE | 未设置 | 自定义完整配置文件 |
| MYSQL_HOST | 按环境文件 | MySQL 地址 |
| MYSQL_PORT | 按环境文件 | MySQL 端口 |
| MYSQL_DATABASE | auto_crawler | 凭证表所在数据库 |
| CRAWLER_PROJECT_ID | 22 | 项目 Cookie 隔离 ID，必须是正整数 |
| MYSQL_USER / MYSQL_PASSWORD | 必填 | MySQL 账号和密码 |
| MYSQL_SSL_DISABLED | false | 仅本地 dev 可设为 true |
| MYSQL_SSL_CA | 未设置 | 远程 MySQL 必须配置的 CA |
| MYSQL_SSL_CERT / MYSQL_SSL_KEY | 未设置 | 可选客户端证书，必须成对配置 |
| MYSQL_CONNECT_TIMEOUT_SECONDS | 10 | MySQL 连接超时 |
| MYSQL_READ_TIMEOUT_SECONDS | 30 | MySQL 读取超时 |
| MYSQL_WRITE_TIMEOUT_SECONDS | 30 | MySQL 写入超时 |
| MYSQL_POOL_RECYCLE_SECONDS | 1800 | MySQL 连接回收时间 |
| MAX_CONCURRENT_REQUESTS | 2 | 服务全局抓取并发 |
| MAX_CONCURRENT_REQUESTS_PER_ACCOUNT | 1 | 单账号抓取并发 |
| ENABLE_TEST_ACCOUNT_PINNING | false | 仅 dev 独立风控测试实例可定向搜索账号；生产强制关闭 |
| ACCOUNT_COOLDOWN_SECONDS | 300 | 认证失败或明确风控后的冷却秒数 |
| ACCOUNT_COOLDOWN_FAILURE_LIMIT | 3 | 同一凭证累计冷却达到该次数后移出账号池并删除对应数据库记录；0 表示关闭 |
| ACCOUNT_ACQUIRE_TIMEOUT_SECONDS | 30 | 等待并发和账号槽位的秒数 |
| ACCOUNT_REFRESH_INTERVAL_SECONDS | 300 | 从 MySQL 刷新账号池的间隔秒数 |
| DOUYIN_CONNECT_TIMEOUT_SECONDS | 10 | 抖音连接超时 |
| DOUYIN_READ_TIMEOUT_SECONDS | 30 | 抖音读取超时 |
| DOUYIN_CA_BUNDLE | 系统 CA | 企业代理使用的受信 CA |
| QR_SESSION_TIMEOUT_SECONDS | 180 | 扫码会话有效期 |
| QR_SMS_VERIFICATION_TIMEOUT_SECONDS | 180 | 进入短信身份验证后重新保留的操作时间 |
| QR_SESSION_RETENTION_SECONDS | 300 | 终态会话内存保留时间 |
| QR_PERSIST_TIMEOUT_SECONDS | 30 | 凭证持久化慢请求告警阈值 |
| QR_DEBUG_SCREENSHOT_ENABLED | false | 是否覆盖保存扫码登录页面调试截图 |
| PLAYWRIGHT_BROWSER_CHANNEL | 未设置 | 可选浏览器通道，如 chrome |
| QR_LOGIN_HEADLESS | false | 是否无界面启动扫码浏览器；Xvfb 环境应设置为 false |

远程 MySQL 可配置 CA 启用 TLS，或在可信内网显式设置 `MYSQL_SSL_DISABLED=true`。实际 .env.dev、.env.prod、CA、证书和私钥不应提交到仓库。

## Docker 部署

Docker 镜像会安装 Playwright Chromium、Xvfb 和 X11 诊断工具，并通过虚拟显示以可视浏览器模式启动单 worker Uvicorn：

~~~bash
docker build -f docker/Dockerfile -t douyin-spider-api .

docker run -d \
  --name douyin-spider \
  -p 5000:5000 \
  --restart=always \
  --shm-size=1g \
  --env-file .env.prod \
  douyin-spider-api
~~~

检查容器中的 Xvfb 进程：

~~~bash
docker exec douyin-spider pgrep -a Xvfb
~~~

镜像默认设置 `QR_LOGIN_HEADLESS=false`，运行命令再次显式指定该值，避免 `.env.prod` 中的旧配置覆盖可视模式。`--shm-size=1g` 用于降低 Chromium 因共享内存不足而崩溃的概率。

.env.prod 中的 MYSQL_SSL_CA 应填写容器内路径 /run/secrets/mysql-ca.pem。若抖音流量经过企业自签代理，也需要只读挂载 CA，并通过 DOUYIN_CA_BUNDLE 配置容器内路径。

当前账号轮询游标、冷却状态和扫码会话均保存在进程内，因此部署时必须保持单 Uvicorn worker、单实例。多实例共享状态不在当前版本范围内。

## 本地测试

单账号关键词搜索并发测试请参阅 [单账号关键词搜索并发风控测试方案](docs/单账号关键词搜索并发风控测试方案.md)。该测试必须使用只包含一个已授权测试账号的独立实例。

Mock 自动化测试不需要连接真实 MySQL 或抖音：

~~~bash
python -m pytest tests/test_api.py -q
~~~

运行完整测试：

~~~bash
python -m pytest -q
~~~

真实联调建议按以下顺序执行：

1. 启动本地 MySQL 和 API。
2. 访问 /api/health，确认 database=ok。
3. 创建扫码会话并扫码，等待状态变为 succeeded。
4. 访问 /api/v1/douyin/auth/accounts，确认账号为 available。
5. 在 /api/docs 中测试作品、评论、用户作品和搜索接口。

## Legacy CLI

如需使用原有 Excel、媒体和详情文件落盘能力：

1. 在根目录 .env 中配置 DY_COOKIES 等旧版凭证。
2. 显式执行：

~~~bash
python -m scripts.legacy_cli
~~~

DY_COOKIES、DY_TICKET、DY_TS_SIGN、DY_CLIENT_CERT 和 DY_PRIVATE_KEY 仅供 scripts/legacy_cli.py 使用，FastAPI 不读取或更新这些变量。

## 安全与部署说明

- API 本身不提供 API Key 或 Bearer Token，只应部署在受控内网，并由网关、网络策略或服务网格限制调用方。
- 日志不会记录 Cookie、Ticket、二维码内容、证书、私钥、数据库密码、搜索关键词或评论正文。
- 数据库中的 Cookie 凭证当前未做应用层加密，应通过 MySQL 最小权限、TLS、网络隔离、审计和备份保护。
- API 请求链路不会写入 Excel、媒体和业务详情文件，但 MySQL 会持久化扫码获得的账号凭证。
- 不要使用多个 Uvicorn worker 或多个服务副本，否则扫码会话和账号运行状态无法共享。
- 抖音接口和页面结构可能变化，真实联调失败时应先检查账号状态、网络和上游响应。

## 目录说明

| 路径 | 用途 |
| --- | --- |
| main.py | FastAPI 应用、路由和统一异常处理 |
| app/api_schemas.py | API 请求模型和参数校验 |
| app/spider_service.py | 无落盘抓取服务层及账号故障切换 |
| app/account_pool.py | 多账号轮询、冷却和单账号并发控制 |
| app/account_store.py | MySQL 凭证读取、校验和保存更新 |
| app/qr_login_service.py | 扫码会话生命周期与凭证提交 |
| app/env_config.py | dev、prod 和自定义环境文件加载 |
| dy_apis/ | 原有抖音底层 API 能力 |
| scripts/legacy_cli.py | 显式旧版落盘入口 |
| docs/ | 项目业务与补充文档 |
| tests/ | Mock API、账号池、扫码和配置测试 |

## 🙏 致谢

本项目基于 [cv-cat/Douyin_Spider](https://github.com/cv-cat/DouYin_Spider) 改造。感谢原作者 [cv-cat](https://github.com/cv-cat) 及原项目所有贡献者提供的公开代码与基础能力。本项目在其基础上增加了 FastAPI 内部服务、多账号扫码登录与 Cookie 轮询、MySQL 凭证存储、开发/生产环境配置隔离，以及 API 链路不落地业务文件等能力。

原项目代码权利归其作者及贡献者所有；使用、修改或分发时请遵守原项目的授权要求。
