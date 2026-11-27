"""
BiliMouse 摄像头推流服务端

设备端（ESP32-S3 + GC0308）把 RGB565 原始帧 POST 到这里：

    POST /api/frame
      X-Api-Key: <密钥>            <- 必填，与服务端 CAM_API_KEY 一致，否则 401
      Content-Type: application/octet-stream
      X-Frame-Format: rgb565
      X-Frame-Width: 320
      X-Frame-Height: 240
      X-Frame-Seq: 42
      <body = width*height*2 字节的 RGB565 像素>

按需推流（v1.2，原则：不浪费流量 / 设备算力 / 服务器算力）：
    设备每次开始推帧前先问 GET /api/push_gate（几十字节，带 X-Api-Key）：
      push=true  才采集并 POST 大帧；响应里也带 push，false 就转待机
      push=false 设备不碰摄像头、不发 150KB 帧，只每 poll_ms 轻量轮询
    服务端的「需求」= 有人看 MJPEG / 网页心跳新鲜 / 正在录制，三者都没有时：
      /api/frame 直接丢弃（不解码、不存储、不录制），服务器几乎零负载
    网页每 3s POST /api/presence 报活；关掉网页约 PRESENCE_TTL 秒后自动停推。
    老固件不认识闸门也没关系：服务端仍会丢掉无需求时的帧，不会拖慢服务器。
    设 GATE_ENABLED=0 可退回「永远接受推流」（调试用）。

鉴权（CAM_API_KEY，必填）：
    POST /api/frame            必须带 X-Api-Key（或 Authorization: Bearer）
    GET  /api/push_gate        必须带 X-Api-Key（设备问闸门）
    GET  /api/*                默认也要密钥：网页登录拿 HttpOnly 签名 cookie，
                               或直接带请求头（PROTECT_VIEW=0 可关闭观看鉴权）
    POST /api/presence         免密：网页报活（不含任何画面/状态）
    POST /api/login            网页用密钥换 cookie
    POST /api/logout           清 cookie
    GET  /healthz              免密，Docker healthcheck 用

录制（v1.2）：
    POST /api/record/start|stop   开始/停止录制
    GET  /api/record/status       录制状态（时长/剩余/帧数）
    GET  /api/recordings          按日期分组的录制列表
    GET  /api/recordings/<d>/<n>          下载 .mjpg（MJPEG，VLC 可直接放）
    GET  /api/recordings/<d>/<n>/play     网页回放（MJPEG 流，可 ?loop=1）
    DELETE /api/recordings/<d>/<n>        删除单个
    DELETE /api/recordings/<d>            删除某天
    DELETE /api/recordings                清空全部
    单次最长 RECORD_MAX_SECONDS（默认 600s=10 分钟），超时自动停；
    录制中设备掉线（连续 RECORD_OFFLINE_TIMEOUT 秒没帧）自动停。
    文件按 data/recordings/YYYY-MM-DD/ 归档，可用 clean-recordings.sh 一键清理。
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from PIL import Image

LOG = logging.getLogger("bilimouse")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

SAVE_EVERY = int(os.environ.get("SAVE_EVERY", "0"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
MAX_BODY = int(os.environ.get("MAX_BODY", str(4 * 1024 * 1024)))

# ---------------- 鉴权配置 ----------------
CAM_API_KEY = os.environ.get("CAM_API_KEY", "").strip()
PROTECT_VIEW = os.environ.get("PROTECT_VIEW", "1").lower() not in ("0", "false", "no")
ALLOW_KEY_IN_QUERY = os.environ.get("ALLOW_KEY_IN_QUERY", "0").lower() in ("1", "true", "yes")
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(7 * 24 * 3600)))
COOKIE_NAME = "cam_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")

# ---------------- 按需推流配置 ----------------
# 关掉后恒接受推流（老行为）。测试与调试用。
GATE = {"enabled": os.environ.get("GATE_ENABLED", "1").lower() in ("1", "true", "yes")}
# 网页心跳有效期：前端每 3s 报活，8s 内没报活就认为网页关了
PRESENCE_TTL = float(os.environ.get("PRESENCE_TTL", "8"))
# 设备待机时轮询闸门的间隔（下发到设备）
GATE_POLL_MS = int(os.environ.get("GATE_POLL_MS", "2000"))

# ---------------- 录制配置 ----------------
RECORD_DIR = Path(os.environ.get("RECORD_DIR", str(SNAPSHOT_DIR / "recordings")))
RECORD_MAX_SECONDS = int(os.environ.get("RECORD_MAX_SECONDS", "600"))
RECORD_OFFLINE_TIMEOUT = float(os.environ.get("RECORD_OFFLINE_TIMEOUT", "8"))
RECORD_PLAY_FPS = float(os.environ.get("RECORD_PLAY_FPS", "2"))
# 日期文件夹 / 文件名按哪个时区。容器通常是 UTC，这里默认 +8（北京时间），
# 不依赖镜像里有没有 tzdata。
TZ_OFFSET_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "8"))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^rec_\d{6}(?:_\d+)?\.mjpg$")

if not CAM_API_KEY:
    LOG.error("CAM_API_KEY 未配置：/api/frame 将拒绝一切推流（503）。"
              "请在 .env 中设置后重启容器")
elif len(CAM_API_KEY) < 16:
    LOG.warning("CAM_API_KEY 只有 %d 个字符，建议至少 16 位随机字符串", len(CAM_API_KEY))
LOG.info("auth: push key %s, view protection %s, demand gate %s, record dir %s",
         "enabled" if CAM_API_KEY else "MISSING", "on" if PROTECT_VIEW else "off",
         "on" if GATE["enabled"] else "off", RECORD_DIR)


# ---------------- 时间/日期（不依赖容器时区数据库） ----------------
def _local_dt(ts: Optional[float] = None) -> datetime:
    t = time.time() if ts is None else ts
    return datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)


def _date_str(ts: Optional[float] = None) -> str:
    return _local_dt(ts).strftime("%Y-%m-%d")


def _clock_str(ts: Optional[float] = None) -> str:
    return _local_dt(ts).strftime("%H%M%S")


# ---------------- 帧仓库 ----------------
class Frame:
    """最新一帧。jpeg 在收到时就转好，避免每个观看者都重复转一次。"""

    def __init__(self, jpeg: bytes, width: int, height: int, seq: int, received_at: float):
        self.jpeg = jpeg
        self.width = width
        self.height = height
        self.seq = seq
        self.received_at = received_at
        self.clients = 0
        self.total = 0


class FrameStore:
    """最新帧 + 订阅者通知。慢的观看者会丢帧而不是把内存撑爆。"""

    def __init__(self) -> None:
        self._frame: Optional[Frame] = None
        self._cond = asyncio.Condition()
        self._total_received = 0

    async def put(self, frame: Frame) -> None:
        async with self._cond:
            self._frame = frame
            self._total_received += 1
            self._cond.notify_all()

    async def wait_for_new(self, last: int, timeout: float = 5.0):
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(
                    lambda: self._frame is not None and self._frame.seq > last),
                    timeout=timeout)
            except asyncio.TimeoutError:
                return None
            return self._frame

    @property
    def frame(self) -> Optional[Frame]:
        return self._frame

    @property
    def total(self) -> int:
        return self._total_received


store = FrameStore()

# 当前正在看 MJPEG 直播的人数 / 最近一次网页报活时间
VIEWERS = {"n": 0}
PRESENCE = {"last": 0.0}


# ---------------- 录制器 ----------------
class Recorder:
    """把收到的 JPEG 帧按 MJPEG 边界顺序写成一个 .mjpg 文件。

    选 MJPEG 而不是 MP4/AVI：服务端镜像里没有 ffmpeg，也不想为录屏引入
    新的 pip/apt 依赖（国内构建容易超时）。MJPEG 文件 VLC 能直接放，
    网页也能通过 /play 端点按帧回放，零依赖、写盘只是顺序 append，
    2fps 下对服务器几乎没影响。
    """

    def __init__(self) -> None:
        self.active = False
        self.path: Optional[Path] = None
        self.fh = None
        self.start_ts = 0.0
        self.last_frame_at = 0.0
        self.frames = 0
        self.nbytes = 0
        self.width = 0
        self.height = 0
        self.date = ""
        self.name = ""
        self.stop_reason = ""
        self.last_meta: Optional[dict] = None

    def start(self) -> dict:
        if self.active:
            return self.status()
        ts = time.time()
        date = _date_str(ts)
        clock = _clock_str(ts)
        folder = RECORD_DIR / date
        folder.mkdir(parents=True, exist_ok=True)
        name = f"rec_{clock}.mjpg"
        path = folder / name
        i = 2
        while path.exists():
            path = folder / f"rec_{clock}_{i}.mjpg"
            i += 1
        try:
            self.fh = open(path, "wb")
        except OSError as e:
            LOG.error("open record file failed: %s", e)
            raise HTTPException(status_code=500, detail=f"cannot create record file: {e}")
        self.active = True
        self.path = path
        self.start_ts = ts
        self.last_frame_at = ts
        self.frames = 0
        self.nbytes = 0
        self.width = self.height = 0
        self.date = date
        self.name = path.name
        self.stop_reason = ""
        LOG.info("record started: %s/%s", date, self.name)
        return self.status()

    def append(self, jpeg: bytes, width: int, height: int) -> None:
        if not self.active or self.fh is None:
            return
        self.fh.write(
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
        )
        # 每帧刷到 OS：录制中文件就是完整可读的（能边录边下），
        # 服务意外退出/掉线自动停止时也不会丢掉整段缓冲。
        # 2fps 下只是普通 flush（非 fsync），开销可忽略。
        self.fh.flush()
        self.frames += 1
        self.nbytes += len(jpeg)
        self.width, self.height = width, height
        self.last_frame_at = time.time()
        if self.last_frame_at - self.start_ts >= RECORD_MAX_SECONDS:
            self.stop("max_duration")

    def stop(self, reason: str = "manual") -> Optional[dict]:
        if not self.active:
            return self.last_meta
        self.stop_reason = reason
        try:
            if self.fh is not None:
                self.fh.flush()
                self.fh.close()
        except OSError as e:
            LOG.warning("close record file failed: %s", e)
        self.fh = None
        self.active = False
        end = time.time()
        meta = {
            "date": self.date,
            "name": self.name,
            "start": int(self.start_ts),
            "end": int(end),
            "duration": round(end - self.start_ts, 2),
            "frames": self.frames,
            "width": self.width,
            "height": self.height,
            "bytes": self.nbytes,
            "stop_reason": reason,
        }
        if self.path is not None:
            try:
                self.path.with_suffix(".json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                LOG.warning("write record meta failed: %s", e)
        self.last_meta = meta
        LOG.info("record stopped (%s): %d frames, %.1fs, %s/%s",
                 reason, self.frames, meta["duration"], self.date, self.name)
        return meta

    def status(self) -> dict:
        out = {"active": self.active, "last": self.last_meta}
        if self.active:
            elapsed = time.time() - self.start_ts
            out.update({
                "elapsed": round(elapsed, 1),
                "remaining": round(max(0, RECORD_MAX_SECONDS - elapsed), 1),
                "max_seconds": RECORD_MAX_SECONDS,
                "frames": self.frames,
                "bytes": self.nbytes,
                "path": f"{self.date}/{self.name}",
            })
        return out


recorder = Recorder()


def enforce_record_limits() -> None:
    """由后台 watchdog 每 1s 调用；也可被测试直接调用。"""
    if not recorder.active:
        return
    now = time.time()
    if now - recorder.start_ts >= RECORD_MAX_SECONDS:
        recorder.stop("max_duration")
    elif now - recorder.last_frame_at >= RECORD_OFFLINE_TIMEOUT:
        # 录制中设备掉线（或压根没推）：连续超时没有新帧，自动结束，
        # 避免留下一个永远写不完 / 空内容的录制文件。
        recorder.stop("device_offline")


async def _watchdog() -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            enforce_record_limits()
        except Exception as e:  # pragma: no cover - 保底，别让后台任务死掉
            LOG.warning("watchdog error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_watchdog())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if recorder.active:
            recorder.stop("server_shutdown")


app = FastAPI(title="BiliMouse Camera Server", version="1.2.0",
              docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ---------------- 需求（demand）----------------
def demand_active() -> bool:
    if not GATE["enabled"]:
        return True
    return (recorder.active
            or VIEWERS["n"] > 0
            or (time.time() - PRESENCE["last"]) < PRESENCE_TTL)


def demand_reason() -> str:
    if not GATE["enabled"]:
        return "forced"
    if recorder.active:
        return "recording"
    if VIEWERS["n"] > 0:
        return "viewer"
    if time.time() - PRESENCE["last"] < PRESENCE_TTL:
        return "presence"
    return "idle"


# ---------------- 鉴权 ----------------
_FAILS: dict = {}
MAX_FAILS = 20
FAIL_WINDOW = 60.0


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    cnt, first = _FAILS.get(ip, (0, now))
    if now - first > FAIL_WINDOW:
        cnt, first = 0, now
        _FAILS[ip] = (cnt, first)
    return cnt >= MAX_FAILS


def _note_fail(ip: str) -> None:
    now = time.time()
    cnt, first = _FAILS.get(ip, (0, now))
    if now - first > FAIL_WINDOW:
        cnt, first = 0, now
    _FAILS[ip] = (cnt + 1, first)
    if len(_FAILS) > 1024:
        for k, (_, t) in list(_FAILS.items()):
            if now - t > FAIL_WINDOW:
                _FAILS.pop(k, None)


def _clear_fails(ip: str) -> None:
    _FAILS.pop(ip, None)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized",
                         headers={"WWW-Authenticate": "Bearer"})


def _key_matches(candidate: Optional[str]) -> bool:
    if not CAM_API_KEY or not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), CAM_API_KEY)


def _key_from_request(request: Request) -> Optional[str]:
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if not key and ALLOW_KEY_IN_QUERY:
        key = request.query_params.get("key")
    return key


def _session_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(CAM_API_KEY.encode("utf-8"),
                   ("bilimouse-view|" + exp).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return exp + "." + sig


def _session_valid(token: Optional[str]) -> bool:
    if not CAM_API_KEY or not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp <= int(time.time()):
        return False
    want = hmac.new(CAM_API_KEY.encode("utf-8"),
                    ("bilimouse-view|" + exp_s).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


async def require_push_key(request: Request) -> None:
    """推流/闸门鉴权：必须在读 body 之前执行，未授权的大包不进内存。"""
    if not CAM_API_KEY:
        raise HTTPException(status_code=503,
                            detail="server has no CAM_API_KEY configured")
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(status_code=429, detail="too many auth failures")
    if not _key_matches(_key_from_request(request)):
        _note_fail(ip)
        LOG.warning("rejected push from %s: bad or missing API key", ip)
        raise _unauthorized()
    _clear_fails(ip)


async def require_view_key(request: Request) -> None:
    """观看鉴权：密钥请求头，或网页登录拿到的签名 cookie。"""
    if not PROTECT_VIEW:
        return
    if not CAM_API_KEY:
        raise HTTPException(status_code=503,
                            detail="server has no CAM_API_KEY configured")
    if _key_matches(_key_from_request(request)):
        return
    if _session_valid(request.cookies.get(COOKIE_NAME)):
        return
    raise _unauthorized()


def rgb565_to_jpeg(raw: bytes, width: int, height: int) -> bytes:
    need = width * height * 2
    if len(raw) < need:
        raise ValueError(f"frame too short: {len(raw)} < {need}")
    img = Image.frombytes("RGB", (width, height), raw[:need], "raw", "BGR;16")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ---------------- 设备：闸门 / 收帧 ----------------
@app.get("/api/push_gate")
async def push_gate(request: Request, _auth=Depends(require_push_key)) -> JSONResponse:
    """设备推帧前问一句：现在有人要看 / 在录制吗？几十字节，很轻。"""
    on = demand_active()
    return JSONResponse({
        "push": on,
        "poll_ms": GATE_POLL_MS,
        "recording": recorder.active,
        "viewers": VIEWERS["n"],
        "reason": demand_reason(),
    }, headers={"Cache-Control": "no-store"})


@app.post("/api/presence")
async def presence() -> JSONResponse:
    """网页报活（免密）。只要网页开着就定时调用，关掉后 TTL 到期自动停推。"""
    PRESENCE["last"] = time.time()
    return JSONResponse({"ok": True, "push": demand_active()},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/frame")
async def receive_frame(
    request: Request,
    _auth=Depends(require_push_key),
    x_frame_format: str = Header(default="rgb565"),
    x_frame_width: int = Header(default=320),
    x_frame_height: int = Header(default=240),
    x_frame_seq: int = Header(default=0),
) -> JSONResponse:
    # 无需求：不解码、不存储、不录制。读掉 body 只是为了保持 HTTP 连接干净，
    # 最贵的 Pillow 解码/写盘全部跳过，服务器运行不受无人观看时的推流影响。
    # 新固件在 push=false 时根本不会发大帧，这里主要兜底老固件 / 竞态。
    if not demand_active():
        await request.body()
        return JSONResponse({"ok": True, "push": False, "dropped": True,
                             "poll_ms": GATE_POLL_MS})

    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=413, detail="frame too large")

    if x_frame_format.lower() not in ("rgb565", "jpeg"):
        raise HTTPException(status_code=415,
                            detail=f"unsupported format: {x_frame_format}")

    if x_frame_format.lower() == "jpeg":
        jpeg = body
    else:
        try:
            jpeg = rgb565_to_jpeg(body, x_frame_width, x_frame_height)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    seq = x_frame_seq or (store.frame.seq + 1 if store.frame else 1)
    frame = Frame(jpeg=jpeg, width=x_frame_width, height=x_frame_height,
                  seq=seq, received_at=time.time())
    await store.put(frame)

    if recorder.active:
        recorder.append(jpeg, x_frame_width, x_frame_height)

    # 定时存图（默认关）
    if SAVE_EVERY > 0 and store.total % SAVE_EVERY == 0:
        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            name = f"frame_{int(frame.received_at)}_{seq:06d}.jpg"
            (SNAPSHOT_DIR / name).write_bytes(jpeg)
        except OSError as e:
            LOG.warning("save snapshot failed: %s", e)

    LOG.debug("frame #%d %dx%d %d->%d bytes (total %d)",
              seq, x_frame_width, x_frame_height, len(body), len(jpeg), store.total)
    return JSONResponse({"ok": True, "seq": seq, "jpeg_bytes": len(jpeg),
                         "push": demand_active()})


# ---------------- 观看：快照 / 状态 / 直播 ----------------
@app.get("/api/snapshot.jpg")
async def snapshot(request: Request, _auth=Depends(require_view_key)) -> Response:
    f = store.frame
    if f is None:
        raise HTTPException(status_code=404, detail="no frame yet")
    return Response(content=f.jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def status(request: Request, _auth=Depends(require_view_key)) -> JSONResponse:
    f = store.frame
    return JSONResponse({
        "online": f is not None,
        "frames_total": store.total,
        "last_seq": f.seq if f else 0,
        "width": f.width if f else 0,
        "height": f.height if f else 0,
        "age_seconds": round(time.time() - f.received_at, 2) if f else None,
        "viewers": VIEWERS["n"],
        # v1.2：按需推流与录制状态
        "push_gate": demand_active(),
        "gate_reason": demand_reason(),
        "gate_enabled": GATE["enabled"],
        "device_streaming": f is not None and (time.time() - f.received_at) < 3,
        "recording": recorder.status(),
    })


@app.get("/api/stream.mjpg")
async def stream(request: Request, _auth=Depends(require_view_key)) -> StreamingResponse:
    """MJPEG 视频流。观看者存在本身就是一种「需求」，会让设备开始推流。"""

    async def gen():
        last_seq = -1
        VIEWERS["n"] += 1
        try:
            while True:
                if await request.is_disconnected():
                    break
                f = await store.wait_for_new(last_seq, timeout=5.0)
                if f is None:
                    continue
                last_seq = f.seq
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(f.jpeg)).encode() + b"\r\n\r\n"
                       + f.jpeg + b"\r\n")
        finally:
            VIEWERS["n"] = max(0, VIEWERS["n"] - 1)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------- 录制：控制 / 列表 / 下载 / 回放 / 删除 ----------------
@app.post("/api/record/start")
async def record_start(request: Request, _auth=Depends(require_view_key)) -> JSONResponse:
    """开始录制。录制本身即构成「需求」，设备会因此被允许推流。"""
    PRESENCE["last"] = time.time()
    return JSONResponse(recorder.start())


@app.post("/api/record/stop")
async def record_stop(request: Request, _auth=Depends(require_view_key)) -> JSONResponse:
    meta = recorder.stop("manual")
    return JSONResponse({"ok": True, "last": meta})


@app.get("/api/record/status")
async def record_status(request: Request, _auth=Depends(require_view_key)) -> JSONResponse:
    return JSONResponse(recorder.status())


def list_recordings() -> dict:
    groups: dict = {}
    if not RECORD_DIR.is_dir():
        return groups
    for d in sorted(RECORD_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not DATE_RE.match(d.name):
            continue
        items = []
        for p in sorted(d.glob("*.mjpg"), reverse=True):
            if not NAME_RE.match(p.name):
                continue
            meta = {}
            mj = p.with_suffix(".json")
            if mj.exists():
                try:
                    meta = json.loads(mj.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            st = p.stat()
            items.append({
                "name": p.name,
                "date": d.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "duration": meta.get("duration"),
                "frames": meta.get("frames"),
                "width": meta.get("width"),
                "height": meta.get("height"),
                "stop_reason": meta.get("stop_reason"),
                "recording": recorder.active and recorder.path == p,
            })
        if items:
            groups[d.name] = items
    return groups


@app.get("/api/recordings")
async def recordings(request: Request, _auth=Depends(require_view_key)) -> JSONResponse:
    groups = list_recordings()
    total = sum(len(v) for v in groups.values())
    return JSONResponse({"groups": groups, "total": total,
                         "max_seconds": RECORD_MAX_SECONDS})


def _resolve_recording(date: str, name: str) -> Path:
    """严格校验 date/name，杜绝 ../ 之类的路径穿越。"""
    if not DATE_RE.match(date) or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="bad recording path")
    root = RECORD_DIR.resolve()
    p = (root / date / name).resolve()
    if root not in p.parents:
        raise HTTPException(status_code=400, detail="bad recording path")
    return p


@app.get("/api/recordings/{date}/{name}/play")
async def recording_play(date: str, name: str, request: Request,
                         _auth=Depends(require_view_key)) -> StreamingResponse:
    """把存好的 .mjpg 按帧重放成 MJPEG 流，网页 <img> 直接能看。"""
    p = _resolve_recording(date, name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="recording not found")
    data = p.read_bytes()
    frames = list(_mjpeg_frames(data))
    interval = 1.0 / max(0.1, RECORD_PLAY_FPS)
    loop = request.query_params.get("loop") == "1"

    async def gen():
        while True:
            for jf in frames:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jf)).encode() + b"\r\n\r\n"
                       + jf + b"\r\n")
                await asyncio.sleep(interval)
            if not loop:
                break

    return StreamingResponse(gen(),
                             media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


def _mjpeg_frames(data: bytes):
    for part in data.split(b"--frame\r\n"):
        sep = part.find(b"\r\n\r\n")
        if sep < 0:
            continue
        head = part[:sep]
        m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
        n = int(m.group(1)) if m else 0
        jpeg = part[sep + 4: sep + 4 + n] if n else part[sep + 4:].rstrip(b"\r\n")
        if jpeg[:2] == b"\xff\xd8":
            yield jpeg


@app.get("/api/recordings/{date}/{name}")
async def recording_download(date: str, name: str, request: Request,
                             _auth=Depends(require_view_key)) -> Response:
    p = _resolve_recording(date, name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(p, media_type="video/x-motion-jpeg", filename=name)


@app.delete("/api/recordings/{date}/{name}")
async def recording_delete(date: str, name: str, request: Request,
                           _auth=Depends(require_view_key)) -> JSONResponse:
    p = _resolve_recording(date, name)
    if recorder.active and recorder.path == p:
        recorder.stop("deleted")
    p.unlink(missing_ok=True)
    p.with_suffix(".json").unlink(missing_ok=True)
    try:
        p.parent.rmdir()   # 目录空了顺手删掉，别留一堆空日期目录
    except OSError:
        pass
    return JSONResponse({"ok": True})


@app.delete("/api/recordings/{date}")
async def recording_delete_day(date: str, request: Request,
                               _auth=Depends(require_view_key)) -> JSONResponse:
    if not DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="bad date")
    root = RECORD_DIR.resolve()
    d = (root / date).resolve()
    if root not in d.parents or not d.is_dir():
        raise HTTPException(status_code=404, detail="date not found")
    if recorder.active and recorder.path is not None and recorder.path.parent == d:
        recorder.stop("deleted")
    shutil.rmtree(d, ignore_errors=True)
    return JSONResponse({"ok": True})


@app.delete("/api/recordings")
async def recording_delete_all(request: Request,
                               _auth=Depends(require_view_key)) -> JSONResponse:
    if recorder.active:
        recorder.stop("deleted")
    n = 0
    if RECORD_DIR.is_dir():
        for d in RECORD_DIR.iterdir():
            if d.is_dir() and DATE_RE.match(d.name):
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return JSONResponse({"ok": True, "dates_removed": n})


# ---------------- 网页 / 登录 / 健康 ----------------
@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(status_code=429, detail="too many auth failures")
    key = _key_from_request(request)
    if not key:
        try:
            payload = await request.json()
            key = str(payload.get("key", ""))
        except Exception:
            key = None
    if not _key_matches(key):
        _note_fail(ip)
        LOG.warning("login failed from %s", ip)
        raise _unauthorized()
    _clear_fails(ip)
    PRESENCE["last"] = time.time()
    resp = JSONResponse({"ok": True, "ttl": SESSION_TTL})
    resp.set_cookie(
        COOKIE_NAME, _session_token(), max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/",
    )
    return resp


@app.post("/api/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # 打开网页本身也算一次报活：即使还停在登录页，设备也可以先准备起来
    PRESENCE["last"] = time.time()
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
