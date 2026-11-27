# bilimouse-cam-server

BiliMouse（ESP32-S3 + GC0308 摄像头）的**推流接收端**，一条命令起服务。

设备端把摄像头画面以 **RGB565 原始像素** POST 上来，服务端转成 JPEG，
浏览器里直接看（MJPEG 直播流）。

> 为什么不是设备端直接传 JPEG：GC0308 没有 JPEG 编码器，只能出 RGB565/YUV422。
> 在 ESP32-S3 上用 esp_jpeg 软编 320×240 会把 CPU 吃满（这台机器还要跑蓝牙、
> 刷屏和电台），所以**编码放到服务端做**，设备只管搬字节。
>
> v1.1 起**默认启用 API 密钥鉴权**：没密钥推不了帧、也看不了画面。见「安全：API 密钥」。
>
> **v1.2 起默认「按需推流」**：只有有人打开网页或正在录屏时，设备才推流；
> 平时设备只发几十字节的闸门询问，不采集、不发大帧，服务端也不解码，
> **流量 / 设备算力 / 服务器算力三头都省**。同时网页支持一键录屏（最长 10 分钟、
> 按日期归档、可回放/下载/删除），设备掉线自动停录。见「按需推流」和「网页录屏」。

---

## 快速开始

**先配密钥**（必填，漏了 `docker compose` 会直接拒绝启动）：

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(24))"   # 生成强随机密钥
# 把输出填进 .env 的 CAM_API_KEY
```

```bash
docker compose up -d          # 构建并后台启动
docker compose logs -f        # 看日志
```

打开 <http://localhost:39110>，输入刚才的密钥就能看画面。

> 默认端口 **39110**（宿主机和容器内一致）。要改端口，在 `.env` 里设
> `HOST_PORT=xxxxx`，`docker compose` 和 `update.sh` 会同时跟随。

不用 compose 也可以：

```bash
docker build -t bilimouse-cam-server .
docker run -d --name bilimouse-cam-server \
  -p 39110:39110 -v "$(pwd)/data:/data" \
  -e CAM_API_KEY='你生成的密钥' \
  bilimouse-cam-server
```

**本地开发（不用 Docker）：**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CAM_API_KEY='你生成的密钥'
uvicorn app.main:app --reload --port 39110
```

---

## 一键更新（部署机常用）

服务跑起来后，代码有更新时在仓库根目录执行：

```bash
./update.sh
```

脚本依次做：`git fetch` + `git pull --ff-only` → **按需**重新构建镜像 → 用新镜像重建容器 →
轮询 `/healthz` 确认就绪 → 打印设备端要填的地址。Windows 上在 Git Bash 里执行
`bash update.sh` 即可。

> 只有**拉到新代码**或本地还没有镜像时才会重新构建；代码没更新且镜像已存在时
> 直接重启容器，秒级完成，也不会因为 Docker Hub 连不上而失败。

首次部署到一台新机器（之后就只用 `./update.sh` 了）：

```bash
git clone https://github.com/ChenadSH/bilimouse-cam-server.git
cd bilimouse-cam-server
chmod +x update.sh
./update.sh
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--build` | 强制重新构建镜像（即使代码没更新，改了 Dockerfile 时用） |
| `--no-build` | 只拉代码并重启容器，跳过镜像构建（最快） |
| `--down` | 先 `docker compose down` 再启动，彻底重建容器/网络 |
| `--force` | 本地有改动时强制同步到远端（**会丢弃本地已跟踪的修改**） |
| `--wait 60` | 健康检查最多等 60 秒（默认 30 秒） |
| `--prune` | 完成后清理悬空镜像 |

改端口请改 `.env` 里的 `HOST_PORT`（compose 映射和脚本健康检查会一起变）；
`PORT=xxxxx` 只用于临时覆盖健康检查探测的端口。另有 `COMPOSE_FILE`、
`SERVICE`、`IMAGE` 可覆盖默认值）。要定时自动更新可以挂 cron：

```cron
0 4 * * * cd /srv/bilimouse-cam-server && ./update.sh >> update.log 2>&1
```

> 脚本只检查**已跟踪文件**的改动：`data/` 里运行时存的快照/录制和 `.env`
> 都不会阻塞更新；但本地改过 `docker-compose.yml` 这类文件时会主动中止，避免被覆盖。

### `.env` 里的密钥不会被更新替换（重点）

`update.sh` **全程只读 `.env`，从不写入、也绝不 `cp .env.example .env`**。
在此基础上还做了四重保险，专门防止「跑了次更新，密钥被换成示例值」：

1. 更新前把整份 `.env` 备份到临时文件，并算出 `CAM_API_KEY` 的短指纹；
2. 检测远端分支是否误把 `.env` 提交进仓库（若被跟踪，`git pull`/`reset --hard`
   本来会覆盖本地文件）——发现就明确告警；
3. 拉完代码后逐字节比对，只要 `.env` 有任何变化（被覆盖 / 被删），就**无条件
   用备份恢复**并告警；
4. 最后再算一次指纹，前后一致才继续，并打印 `密钥保持不变（指纹 xxxx）`；
   不一致直接中止。

所以你可以放心反复 `./update.sh`，自己配的 `CAM_API_KEY` 不会动。备份文件在
脚本退出时自动清理。升级后新加的配置项（`GATE_ENABLED` 等）都有默认值，
老 `.env` 不补这些行也能正常跑。

---

## 按需推流（默认开启，省流量 / 算力）

**目标：没人看、也没在录屏时，别浪费设备的流量和算力，也别让服务器白解码。**

```
设备 ──GET /api/push_gate（几十字节，2s 一次）──> 服务端
   ← {"push":false,...}   → 待机：不采集、不组 150KB 帧（屏上「待机省流」）
   ← {"push":true,...}    → 开始 2fps 推帧；响应里 push=false 立刻回待机

服务端的「需求」= 三者任一：
   ① 有人打开网页（前端每 3s POST /api/presence 报活，关网页约 8s 后失效）
   ② 有 MJPEG 观看者（/api/stream.mjpg 连接中）
   ③ 正在录屏
需求为假时，即使收到旧固件的帧也**直接丢弃**：不读进逻辑、不转 JPEG、
不存帧、不写盘，服务器几乎零负载。
```

- 网页**报活是免密的**（只是"我在这个页面上"的信号，不含任何画面/状态），
  打开登录页也会让设备准备起来，解锁后能立刻出画面，不用等设备唤醒。
- 设备端是 ESP32-S3 配合改造过的固件（`bilimouse-esp32s3` v2.11+）：待机时
  摄像头保持初始化但**不 capture**，DVP 不传数据，不产生 PSRAM 帧缓冲。
- 老固件不认识闸门也没关系：服务端仍会丢掉无需求时的帧，不会被拖慢。
- 想退回「设备一直推」（调试 / 老固件过渡）：`.env` 里设 `GATE_ENABLED=0`
  后 `./update.sh --no-build` 重启即可。

关闭网页后约 `PRESENCE_TTL`（默认 8 秒）设备自动回到待机；`cam status` 会显示
`STANDBY(待机省流)`。

---

## 网页录屏

网页登录后多出两块面板：

- **录制推流**：「● 开始录制 / ■ 停止录制」，实时显示录制时长、剩余时间
  （进度条）、已录帧数，以及上次录制为何结束（手动 / 到 10 分钟 / 设备掉线）。
- **录制文件**：按日期分组列出，每段可 **回放**（网页内 MJPEG 循环播放）、
  **下载**（`.mjpg`，VLC / mpv 可直接放）、**删除**；还能「删除该日」「清空全部」。

规则：

| 项 | 值 |
|---|---|
| 单次最长 | `RECORD_MAX_SECONDS` 默认 **600 秒（10 分钟）**，到点自动停 |
| 文件格式 | **MJPEG (`.mjpg`)**，服务端把收到的 JPEG 帧顺序追加写盘，零新依赖（不装 ffmpeg，构建不受影响） |
| 存放位置 | `data/recordings/YYYY-MM-DD/rec_HHMMSS.mjpg` + 同名 `.json` 元数据，按 `TZ_OFFSET_HOURS`（默认 +8 北京时间）归档 |
| 设备掉线 | 录制中连续 `RECORD_OFFLINE_TIMEOUT`（默认 8 秒）没收到新帧，服务端后台 watchdog **自动停止录制**并标记 `device_offline`，不留空/写不完的文件 |
| 权限 | 录制、列表、回放、下载、删除全部要观看鉴权（登录 cookie 或 `X-Api-Key`）；路径带正则+`resolve` 双重校验，防目录穿越 |

录制本身即构成「需求」，所以点开始录制时设备会被自动唤醒推流，不用先打开直播。

### 一键清理录制内容

命令行用 `clean-recordings.sh`（效果同网页「清空全部」）：

```bash
./clean-recordings.sh                  # 列出将删除的内容，确认后删全部
./clean-recordings.sh --yes            # 不询问直接删（cron / 一键）
./clean-recordings.sh --dry-run        # 只统计不删
./clean-recordings.sh --date 2026-11-27   # 只删某天
./clean-recordings.sh --older 7        # 只删 7 天前
./clean-recordings.sh --stop --yes     # 先通知服务端停录，再删（最干净）
```

脚本**只删 `data/recordings/` 下符合日期格式的目录里的 `.mjpg`/`.json`**，
逐个文件删、目录空了才 `rmdir`，绝不对整个目录 `rm -rf`，也绝不碰代码或 `.env`。

定时清理示例（每天凌晨 4 点删 7 天前的录制）：

```cron
0 4 * * * cd /srv/bilimouse-cam-server && ./clean-recordings.sh --older 7 --yes >> clean.log 2>&1
```

---

## 安全：API 密钥

**默认全开鉴权**，两套权限分开：

| 谁能干什么 | 要求 |
|---|---|
| 设备推帧 `POST /api/frame` | 必须带 `X-Api-Key`（或 `Authorization: Bearer`），永远强制，关不掉 |
| 浏览器看画面 | 默认也要密钥：网页输入一次换 **HttpOnly 签名 cookie**；`PROTECT_VIEW=0` 才免密 |
| Docker healthcheck `/healthz` | 免密（不泄露任何画面或状态） |
| 网页报活 `POST /api/presence` | 免密（只是"页面开着"的信号，不含画面/状态，也不能推帧） |

关键实现：

- **没配 `CAM_API_KEY` 时 `/api/frame` 一律 503**，绝不会因为「忘了配」就退化成匿名可推。
  compose 里用 `${CAM_API_KEY:?}` 在启动阶段就拦住。
- 密钥比较用 `hmac.compare_digest`（常量时间），失败响应不区分「没配/配错」细节；
  同一 IP 60 秒内失败 20 次返回 429，拖慢暴力猜密钥。
- **鉴权在读 body 之前**：未授权的 150KB 大包根本不进内存。
- 浏览器 cookie 里是 `exp.HMAC(CAM_API_KEY, exp)`，**不含密钥本身**，拿到 cookie
  也推不了流；改密钥后旧会话立即失效；默认 7 天过期。
- 网页 `/`、`/healthz`、`/api/presence` 免密（后两者不返回任何画面/状态）；
  `/docs`、`/openapi.json` 已关掉，不公开接口清单。录制/列表/回放/删除等
  管理接口全部要观看鉴权，且路径做了正则 + `resolve` 双重校验防目录穿越。
- 设备端密钥存 NVS（**明文**，能物理接触板子就能读）。要防拆机得另外开
  ESP32-S3 的 flash encryption；串口 `cam key` 不回显密钥，日志里也不打印。

`?key=` 默认禁用（`ALLOW_KEY_IN_QUERY=0`）：URL 会进浏览器历史和 access log。
只有 VLC 之类只能填 URL 的播放器才需要打开，且仅限可信网络。

> 这是**局域网 HTTP 服务**，密钥是明文传输的。别把 39110 直接映射到公网；
> 要从外网访问，请前面挂 HTTPS 反向代理（同时设 `COOKIE_SECURE=1`），
> 或放进 VPN / Tailscale 之类的内网。

---

## 设备端怎么连上

> 设备端**只配服务端前缀**，写在固件仓库 `main/app/user_config.h` 的
> `USER_CAM_SERVER_URL`（当前为 `http://124.221.91.97:39110/`），改完重新烧录即可。
> 后面具体接口地址（`/api/frame`、`/api/push_gate`）是固件与服务端约定好的，
> 由固件自动拼接，用户不用改也不会改错。下面串口 `cam url <前缀>` 是临时
> 覆盖值，优先级更高，会存进 NVS（`cam url clear` 可清掉）。
> 服务端 `.env` 的 `PUBLIC_URL` 只控制 `update.sh` 打印的提示文案。

1. 先查出服务端所在机器的局域网 IP（设备和服务端要在同一个 WiFi）：

   ```bash
   # Linux
   hostname -I
   # macOS
   ipconfig getifaddr en0
   # Windows
   ipconfig
   ```

2. 在设备串口里设置**前缀和密钥**（一次就够，都存进 NVS）：

   ```
   bilimouse> cam url http://192.168.1.100:39110
   prefix saved: http://192.168.1.100:39110
   bilimouse> cam key 你生成的密钥
   key saved (len=32, value hidden)
   ```

   只填到端口即可，固件会自动拼出 `/api/frame` 和 `/api/push_gate`。
   端口默认 39110，服务端改过 `HOST_PORT` 这里也要跟着改。密钥必须和服务端 `.env`
   里的 `CAM_API_KEY` **完全一致**；串口不会回显（`cam key` 只显示
   「已配置/未配置」），`cam key clear` 可以清掉。

3. 屏幕首页选「📷 摄像头推流」→ 点「开始推流」。
   （也可以直接用串口：`cam start`）
   没配地址/密钥时按钮会直接提示，不会白跑一趟；屏上还会显示「密钥 已配置」。

4. 启动后屏上通常显示「**待机省流·有人看才推**」——这是正常的按需推流状态，
   此时设备不采集不推帧。打开下面的网页、或点网页上的「开始录制」，
   设备 2 秒内自动变成「推流中」。

5. 浏览器打开 <http://192.168.1.100:39110>，输入密钥登录即可看到画面。
   网页开着期间设备持续推流；关掉网页约 8 秒后设备自动回待机省流。

> 设备端固件需 **v2.11+** 才支持闸门轮询（`GET /api/push_gate`）。
> 老固件不认识闸门也能用，只是会一直推帧，服务端在无人观看时会把帧丢弃，
> 不会拖慢服务器；想省流量请升级固件。

---

## HTTP 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/frame` | **密钥（强制）** | 收帧。body = 原始 RGB565 字节；头部 `X-Api-Key`、`X-Frame-Format/Width/Height/Seq`。无需求时返回 `{"ok":true,"push":false,"dropped":true}` 并丢弃 |
| GET | `/api/push_gate` | **密钥（强制）** | 设备问闸门：`{"push":bool,"poll_ms":2000,"recording":bool,"viewers":n,"reason":...}` |
| POST | `/api/presence` | 无 | 网页报活（免密的"我在看"信号，不含画面/状态），刷新需求有效期 |
| GET | `/api/stream.mjpg` | 会话/密钥 | MJPEG 直播流（`multipart/x-mixed-replace`）；连接中 = 有观看者 |
| GET | `/api/snapshot.jpg` | 会话/密钥 | 最新一帧的 JPEG |
| GET | `/api/status` | 会话/密钥 | JSON 状态：帧数/分辨率/观看人数/延迟，另含 `push_gate`、`gate_reason`、`device_streaming`、`recording` |
| POST | `/api/record/start` | 会话/密钥 | 开始录制（返回录制状态） |
| POST | `/api/record/stop` | 会话/密钥 | 停止录制 |
| GET | `/api/record/status` | 会话/密钥 | 录制状态：是否在录、时长、剩余、帧数、文件大小 |
| GET | `/api/recordings` | 会话/密钥 | 录制列表（按日期分组，含时长/大小/帧数/结束原因） |
| GET | `/api/recordings/<d>/<n>` | 会话/密钥 | 下载 `.mjpg` |
| GET | `/api/recordings/<d>/<n>/play` | 会话/密钥 | 网页回放（MJPEG 流，`?loop=1` 循环） |
| DELETE | `/api/recordings/<d>/<n>` | 会话/密钥 | 删除单个录制 |
| DELETE | `/api/recordings/<d>` | 会话/密钥 | 删除某天 |
| DELETE | `/api/recordings` | 会话/密钥 | 清空全部 |
| POST | `/api/login` | 密钥 | 网页登录：`{"key":"..."}`，成功后下发 HttpOnly 签名 cookie |
| POST | `/api/logout` | 无 | 清除观看会话 cookie |
| GET | `/` | 无 | 网页预览（页面内输入密钥） |
| GET | `/healthz` | 无 | 健康检查（Docker healthcheck 用） |

`<d>` = `YYYY-MM-DD`，`<n>` = `rec_HHMMSS.mjpg`（可带 `_2` 后缀）。两者都用严格正则 +
`Path.resolve()` 校验在录制目录内，杜绝 `../` 穿越。

---

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CAM_API_KEY` | **必填** | 推流密钥，设备端 `cam key` 要用同一个值；没配则 `/api/frame` 返回 503 |
| `HOST_PORT` | `39110` | `.env` / compose 专用：宿主机对外端口，改它 compose 与 `update.sh` 同时跟随 |
| `PUBLIC_URL` | 自动探测 | `.env` 专用：设备端实际访问的地址（公网 IP/域名），只影响 `update.sh` 打印的提示 |
| `PIP_INDEX_URL` | 清华源 | `.env` / 构建专用：pip 镜像源，仅 `docker build` 时生效 |
| `PROTECT_VIEW` | `1` | 观看端是否也要鉴权；设 `0` 仅用于可信内网调试 |
| `ALLOW_KEY_IN_QUERY` | `0` | 允许 `?key=` 传密钥（VLC 等播放器用；URL 会进日志，谨慎开） |
| `COOKIE_SECURE` | `0` | 前面有 HTTPS 反代时设 `1`，给 cookie 加 Secure |
| `SESSION_TTL` | `604800` | 网页登录会话有效期（秒，默认 7 天） |
| `GATE_ENABLED` | `1` | 按需推流总开关；设 `0` = 设备一直推（调试 / 兼容旧固件） |
| `PRESENCE_TTL` | `8` | 网页报活有效期（秒）；关网页后多久判定无需求、设备回待机 |
| `GATE_POLL_MS` | `2000` | 设备待机时轮询闸门的间隔（毫秒），由服务端下发给设备 |
| `RECORD_MAX_SECONDS` | `600` | 单次录制最长秒数（10 分钟），到点自动停 |
| `RECORD_OFFLINE_TIMEOUT` | `8` | 录制中连续多少秒无新帧判定设备掉线、自动停录 |
| `RECORD_PLAY_FPS` | `2` | 网页回放帧率 |
| `TZ_OFFSET_HOURS` | `8` | 录制按日期归档的时区偏移（小时），默认 +8 北京时间，不依赖容器 tzdata |
| `RECORD_DIR` | `/data/recordings` | 录制目录（在 `./data` 卷内，一般不用改） |
| `SAVE_EVERY` | `0` | 每 N 帧存一张 JPG 到 `SNAPSHOT_DIR`；0 = 不存 |
| `SNAPSHOT_DIR` | `/data` | 快照目录（compose 里挂到宿主机 `./data`） |
| `JPEG_QUALITY` | `80` | JPEG 质量 1~95 |
| `MAX_BODY` | `4MB` | 单帧大小上限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

这些新变量**都有默认值**，老 `.env` 不加也能直接跑；要调就在 `.env` 里加一行
再 `./update.sh --no-build` 重启（改环境变量需要重建容器，脚本会做）。

例：每 5 帧存一张图：

```yaml
environment:
  SAVE_EVERY: "5"
```

---

## 设计要点

- **只保留最新一帧**：观看者慢了就丢帧，不会把内存撑爆 —— 监控场景就该这样。
  每个观看者独立消费「最新帧」，帧率由设备决定。
- **服务端做编码**：设备端 CPU 太紧张（蓝牙 + LCD + 电台 + 推流）。
  RGB565 → JPEG 在这里用 Pillow 的 `"BGR;16"` 原始解码器完成，
  和设备端 `gen_bg.py` / UI 的 RGB565 打包方式完全一致。
- **单进程单事件循环**：FastAPI + asyncio 足够；一台上百路设备也扛得住
  （瓶颈在设备端的 WiFi，不在服务端）。录屏的后台 watchdog 也是这个循环里的
  一个轻量 task，每 1s 检查一次录制超时/掉线，不引入线程或额外进程。
- **推流鉴权前置**：密钥校验在 `await request.body()` 之前完成，
  匿名请求连 150KB 的帧都不会被读进内存。
- **按需推流靠设备端节流**：服务端单独"拒收"是省不了流量的（帧已经发出来了），
  所以真正省在设备端——没人看就**不采集、不发包**。服务端再做一层兜底：
  无需求时连解码都不做。两者配合才同时省下流量、设备算力和服务器算力。
- **录屏用 MJPEG 顺序写盘**：不引 ffmpeg / 不装系统包，镜像依赖不变、构建
  不受国内网络影响；每帧 `flush()`，录制中文件即完整可读，掉线自动停也不会
  丢整段缓冲。一个录制 = 一个 `.mjpg` + 一个 `.json`，按日期目录，方便删。

---

## 常见问题

**画面是花的/颜色不对？**
设备端发的是 RGB565（小端，R5G6B5）。服务端用 Pillow 的 `"BGR;16"` 解码，
如果设备端改了打包方式（比如换成 RGB888），服务端需要同步改
`rgb565_to_jpeg()`。

**构建一开始就报 `registry-1.docker.io ... EOF`？**
国内拉 Docker Hub 基础镜像元数据也常断。在 `.env` 里换基础镜像源即可，
默认仍是官方地址、不影响海外机器：

```bash
BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
./update.sh --build
```

也可以先手动 `docker pull python:3.12-slim` 多试几次；已有可用镜像时
`./update.sh --no-build` 能直接跳过构建重启服务。

**构建时 `pip install` 报 `Read timed out` / `exit code 2`？**
国内直连 `files.pythonhosted.org` 拉依赖经常超时。镜像默认已改走清华 PyPI 源，
并放宽到 `--timeout 60 --retries 10`；若还失败，在 `.env` 里换源后重建：

```bash
# 阿里云 / 官方源 / 公司内网镜像，任选一个写进 .env 的 PIP_INDEX_URL
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
./update.sh --build
```

单独构建也可以直接传参：
`docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple -t bilimouse-cam-server .`
（海外机器建议切回官方源。已有可用镜像时加 `--no-build` 可跳过构建直接重启。）

**改端口后访问不到 / `update.sh` 健康检查失败？**
改 `.env` 的 `HOST_PORT` 即可，compose 映射和脚本探测会同时变；
只在命令行写 `PORT=xxxx` 而不改 `HOST_PORT` 会造成两边不一致。

**看不到画面 / 一直「等待设备」？**
现在默认是**按需推流**：只有网页开着（报活）或在录屏时，设备才会推流。
设备串口 `cam status` 显示 `STANDBY(待机省流)` 是正常状态，不是故障——
打开服务端网页、或点「开始录制」，设备 2 秒内就会被唤醒。若 `RUNNING`
却没画面，再查：设备 WiFi 是否连上同一网段、前缀里的 IP/端口对不对、
39110 端口通不通、`cam url` 的端口有没有跟着 `HOST_PORT` 改。

**想让设备一直推、不想按需？**
`.env` 里设 `GATE_ENABLED=0`，`./update.sh --no-build` 重启即可（也可用于
还没升级到 v2.11+ 的旧固件）。

**录制到一半自己停了？**
两种情况都正常：① 到了 10 分钟上限（`stop_reason=max_duration`）；
② 录制中设备掉线，连续 `RECORD_OFFLINE_TIMEOUT` 秒没新帧
（`stop_reason=device_offline`）。网页录制状态区会提示上次结束原因。

**画面卡住不动？**
设备到服务端的连接断了。设备串口 `cam status` 能看到 frames/fails 计数。

**设备显示「密钥错误(401)」/ 推流一直失败？**
三处要对齐：服务端 `.env` 的 `CAM_API_KEY`、设备 `cam key` 的值、
以及改完 `.env` 后容器有没有重启（`docker compose up -d --force-recreate`，
改环境变量不重启不生效）。`cam status` 里 `last_http=401` = 密钥不对，
`503` = 服务端根本没配密钥，`429` = 失败太多次被临时限速（等 60 秒）。

**浏览器打不开画面 / 接口返回 401？**
`PROTECT_VIEW=1` 时观看接口也要鉴权：先在网页输入密钥登录
（或请求带 `X-Api-Key` 头），拿到会话 cookie 后 `<img>` 才能拉流。

**换了密钥之后？**
重启服务端容器 → 每台设备重新 `cam key <新密钥>` → 浏览器旧 cookie
会立即失效，重新输一次即可。

---

## 测试

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

34 个测试，两个文件：

- `tests/test_auth.py`（15 个，鉴权回归）：无/错密钥推流被拒且帧不入库、
  正确密钥和 Bearer 放行、观看端 401、登录 cookie 的签发/篡改/过期/登出、
  `/healthz` 免密、文档端点关闭。测试里把 `GATE_ENABLED=0` 关掉闸门，
  专注鉴权语义。
- `tests/test_features.py`（19 个，v1.2 新功能）：闸门空闲/报活/观看者/录制
  四种需求判定、无需求时帧被丢弃且鉴权优先、闸门需密钥、`GATE_ENABLED=0`
  恒接受、录制完整生命周期与按日期落盘、列表/下载/回放/删除单个/删除某天/
  清空、路径穿越防护、10 分钟超时自动停、设备掉线自动停、帧流动时不误停、
  状态接口新字段、录制接口的观看鉴权。

功能测试用临时目录做 `RECORD_DIR`，不碰真实录制数据。

---

## 许可

MIT
