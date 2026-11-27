# bilimouse-cam-server

BiliMouse（ESP32-S3 + GC0308 摄像头）的**推流接收端**，一条命令起服务。

设备端把摄像头画面以 **RGB565 原始像素** POST 上来，服务端转成 JPEG，
浏览器里直接看（MJPEG 直播流）。

> 为什么不是设备端直接传 JPEG：GC0308 没有 JPEG 编码器，只能出 RGB565/YUV422。
> 在 ESP32-S3 上用 esp_jpeg 软编 320×240 会把 CPU 吃满（这台机器还要跑蓝牙、
> 刷屏和电台），所以**编码放到服务端做**，设备只管搬字节。

---

## 快速开始

```bash
docker compose up -d          # 构建并后台启动
docker compose logs -f        # 看日志
```

打开 <http://localhost:8080> 就是实时画面。

不用 compose 也可以：

```bash
docker build -t bilimouse-cam-server .
docker run -d --name bilimouse-cam-server \
  -p 8080:8080 -v "$(pwd)/data:/data" \
  bilimouse-cam-server
```

**本地开发（不用 Docker）：**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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

> 脚本只检查**已跟踪文件**的改动：`data/` 里运行时存的快照不会阻塞更新；
> 但本地改过 `docker-compose.yml` 这类文件时会主动中止，避免被覆盖。

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

2. 在设备串口里设置地址（**一次就够，会存进 NVS**）：

   ```
   bilimouse> cam url http://192.168.1.100:8080
   url saved: http://192.168.1.100:8080
   ```

   只填到端口即可，服务端会自动补 `/api/frame`。

3. 屏幕首页选「📷 摄像头推流」→ 点「开始推流」。
   （也可以直接用串口：`cam start`）

4. 设备状态变成「推流中」后，浏览器刷新 <http://192.168.1.100:8080> 就能看到画面。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/frame` | 收帧。body = 原始 RGB565 字节；头部 `X-Frame-Format/Width/Height/Seq` |
| GET | `/api/stream.mjpg` | MJPEG 直播流（`multipart/x-mixed-replace`） |
| GET | `/api/snapshot.jpg` | 最新一帧的 JPEG |
| GET | `/api/status` | JSON 状态：总帧数、分辨率、观看人数、最新帧延迟 |
| GET | `/` | 网页预览 |
| GET | `/healthz` | 健康检查（Docker healthcheck 用） |

---

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
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

---

## 许可

MIT
