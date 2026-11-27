# bilimouse-cam-server

BiliMouse（ESP32-S3 + GC0308 摄像头）的**推流接收端**，一条命令起服务。

设备端把摄像头画面以 **RGB565 原始像素** POST 上来，服务端转成 JPEG，
浏览器里直接看（MJPEG 直播流）。

> 为什么不是设备端直接传 JPEG：GC0308 没有 JPEG 编码器，只能出 RGB565/YUV422。
> 在 ESP32-S3 上用 esp_jpeg 软编 320×240 会把 CPU 吃满（这台机器还要跑蓝牙、
> 刷屏和电台），所以**编码放到服务端做**，设备只管搬字节。
>
> v1.1 起**默认启用 API 密钥鉴权**：没密钥推不了帧、也看不了画面。见「安全：API 密钥」。

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

打开 <http://localhost:8080>，输入刚才的密钥就能看画面。

不用 compose 也可以：

```bash
docker build -t bilimouse-cam-server .
docker run -d --name bilimouse-cam-server \
  -p 8080:8080 -v "$(pwd)/data:/data" \
  -e CAM_API_KEY='你生成的密钥' \
  bilimouse-cam-server
```

**本地开发（不用 Docker）：**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CAM_API_KEY='你生成的密钥'
uvicorn app.main:app --reload --port 8080
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

改过端口就带上环境变量，例如 `PORT=8090 ./update.sh`（另有 `COMPOSE_FILE`、
`SERVICE`、`IMAGE` 可覆盖默认值）。要定时自动更新可以挂 cron：

```cron
0 4 * * * cd /srv/bilimouse-cam-server && ./update.sh >> update.log 2>&1
```

> 脚本只检查**已跟踪文件**的改动：`data/` 里运行时存的快照和 `.env`
> 都不会阻塞更新，也不会被 `git pull` / `--force` 覆盖删除；但本地改过
> `docker-compose.yml` 这类文件时会主动中止，避免被覆盖。

---

## 安全：API 密钥

**默认全开鉴权**，两套权限分开：

| 谁能干什么 | 要求 |
|---|---|
| 设备推帧 `POST /api/frame` | 必须带 `X-Api-Key`（或 `Authorization: Bearer`），永远强制，关不掉 |
| 浏览器看画面 | 默认也要密钥：网页输入一次换 **HttpOnly 签名 cookie**；`PROTECT_VIEW=0` 才免密 |
| Docker healthcheck `/healthz` | 免密（不泄露任何画面或状态） |

关键实现：

- **没配 `CAM_API_KEY` 时 `/api/frame` 一律 503**，绝不会因为「忘了配」就退化成匿名可推。
  compose 里用 `${CAM_API_KEY:?}` 在启动阶段就拦住。
- 密钥比较用 `hmac.compare_digest`（常量时间），失败响应不区分「没配/配错」细节；
  同一 IP 60 秒内失败 20 次返回 429，拖慢暴力猜密钥。
- **鉴权在读 body 之前**：未授权的 150KB 大包根本不进内存。
- 浏览器 cookie 里是 `exp.HMAC(CAM_API_KEY, exp)`，**不含密钥本身**，拿到 cookie
  也推不了流；改密钥后旧会话立即失效；默认 7 天过期。
- 网页 `/` 和 `/healthz` 免密，`/docs`、`/openapi.json` 已关掉，不公开接口清单。
- 设备端密钥存 NVS（**明文**，能物理接触板子就能读）。要防拆机得另外开
  ESP32-S3 的 flash encryption；串口 `cam key` 不回显密钥，日志里也不打印。

`?key=` 默认禁用（`ALLOW_KEY_IN_QUERY=0`）：URL 会进浏览器历史和 access log。
只有 VLC 之类只能填 URL 的播放器才需要打开，且仅限可信网络。

> 这是**局域网 HTTP 服务**，密钥是明文传输的。别把 8080 直接映射到公网；
> 要从外网访问，请前面挂 HTTPS 反向代理（同时设 `COOKIE_SECURE=1`），
> 或放进 VPN / Tailscale 之类的内网。

---

## 设备端怎么连上

1. 先查出服务端所在机器的局域网 IP（设备和服务端要在同一个 WiFi）：

   ```bash
   # Linux
   hostname -I
   # macOS
   ipconfig getifaddr en0
   # Windows
   ipconfig
   ```

2. 在设备串口里设置**地址和密钥**（一次就够，都存进 NVS）：

   ```
   bilimouse> cam url http://192.168.1.100:8080
   url saved: http://192.168.1.100:8080
   bilimouse> cam key 你生成的密钥
   key saved (len=32, value hidden)
   ```

   只填到端口即可，服务端会自动补 `/api/frame`。密钥必须和服务端 `.env`
   里的 `CAM_API_KEY` **完全一致**；串口不会回显（`cam key` 只显示
   「已配置/未配置」），`cam key clear` 可以清掉。

3. 屏幕首页选「📷 摄像头推流」→ 点「开始推流」。
   （也可以直接用串口：`cam start`）
   没配地址/密钥时按钮会直接提示，不会白跑一趟；屏上还会显示「密钥 已配置」。

4. 设备状态变成「推流中」后，浏览器刷新 <http://192.168.1.100:8080> 就能看到画面。

---

## HTTP 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/frame` | **密钥（强制）** | 收帧。body = 原始 RGB565 字节；头部 `X-Api-Key`、`X-Frame-Format/Width/Height/Seq` |
| GET | `/api/stream.mjpg` | 会话/密钥 | MJPEG 直播流（`multipart/x-mixed-replace`） |
| GET | `/api/snapshot.jpg` | 会话/密钥 | 最新一帧的 JPEG |
| GET | `/api/status` | 会话/密钥 | JSON 状态：总帧数、分辨率、观看人数、最新帧延迟 |
| POST | `/api/login` | 密钥 | 网页登录：`{"key":"..."}`，成功后下发 HttpOnly 签名 cookie |
| POST | `/api/logout` | 无 | 清除观看会话 cookie |
| GET | `/` | 无 | 网页预览（页面内输入密钥） |
| GET | `/healthz` | 无 | 健康检查（Docker healthcheck 用） |

---

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CAM_API_KEY` | **必填** | 推流密钥，设备端 `cam key` 要用同一个值；没配则 `/api/frame` 返回 503 |
| `PROTECT_VIEW` | `1` | 观看端是否也要鉴权；设 `0` 仅用于可信内网调试 |
| `ALLOW_KEY_IN_QUERY` | `0` | 允许 `?key=` 传密钥（VLC 等播放器用；URL 会进日志，谨慎开） |
| `COOKIE_SECURE` | `0` | 前面有 HTTPS 反代时设 `1`，给 cookie 加 Secure |
| `SESSION_TTL` | `604800` | 网页登录会话有效期（秒，默认 7 天） |
| `SAVE_EVERY` | `0` | 每 N 帧存一张 JPG 到 `SNAPSHOT_DIR`；0 = 不存 |
| `SNAPSHOT_DIR` | `/data` | 快照目录（compose 里挂到宿主机 `./data`） |
| `JPEG_QUALITY` | `80` | JPEG 质量 1~95 |
| `MAX_BODY` | `4MB` | 单帧大小上限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

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
  （瓶颈在设备端的 WiFi，不在服务端）。
- **推流鉴权前置**：密钥校验在 `await request.body()` 之前完成，
  匿名请求连 150KB 的帧都不会被读进内存。

---

## 常见问题

**画面是花的/颜色不对？**
设备端发的是 RGB565（小端，R5G6B5）。服务端用 Pillow 的 `"BGR;16"` 解码，
如果设备端改了打包方式（比如换成 RGB888），服务端需要同步改
`rgb565_to_jpeg()`。

**看不到画面？**
先看服务端日志有没有 `frame #N`。没有日志 = 设备根本没发过来，
检查：设备 WiFi 是否连上同一个网段、URL 里的 IP 对不对、8080 端口通不通。

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

覆盖：无/错密钥推流被拒且帧不入库、正确密钥和 Bearer 放行、观看端 401、
登录 cookie 的签发/篡改/过期/登出、`/healthz` 免密、文档端点关闭。

---

## 许可

MIT
