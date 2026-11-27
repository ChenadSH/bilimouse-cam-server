"""
BiliMouse 摄像头推流服务端

设备端（ESP32-S3 + GC0308）把 RGB565 原始帧 POST 到这里：

    POST /api/frame
      X-Api-Key: <密钥>            ← 必填，与服务端 CAM_API_KEY 一致，否则 401
      Content-Type: application/octet-stream
      X-Frame-Format: rgb565
      X-Frame-Width: 320
      X-Frame-Height: 240
      X-Frame-Seq: 42
      <body = width*height*2 字节的 RGB565 像素>

鉴权（CAM_API_KEY，必填）：
    POST /api/frame            必须带 X-Api-Key（或 Authorization: Bearer）
    GET  /api/*                默认也要密钥：网页登录拿 HttpOnly 签名 cookie，
                               或直接带请求头（PROTECT_VIEW=0 可关闭观看鉴权）
    POST /api/login            网页用密钥换 cookie
    POST /api/logout           清 cookie
    GET  /healthz              免密，Docker healthcheck 用

服务端负责把原始像素转成 JPEG（设备端没有 JPEG 编码器，
GC0308 只出 RGB565/YUV422），并提供：

    GET /                 网页预览（内嵌 MJPEG）
    GET /api/stream.mjpg  MJPEG 视频流（可直接嵌到任何播放器）
    GET /api/snapshot.jpg 最新一帧的 JPEG
    GET /api/status       JSON 状态（帧数、客户端数、最近一帧时间…）

也可以把画面存成图片文件（定时抓帧），用环境变量控制：
    SAVE_EVERY=10      每 10 帧存一张（0=不存）
    SNAPSHOT_DIR=/data 存放目录
"""

import asyncio
import hashlib
import hmac
import io
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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
# 推流密钥（必填）：设备端用 X-Api-Key 请求头带上。
# 没配置时服务能启动，但 /api/frame 一律 503，绝不放行匿名推流。
CAM_API_KEY = os.environ.get("CAM_API_KEY", "").strip()
# 观看端保护：默认开。设 0 则 /api/* 的 GET 免密（仅限可信内网调试）。
PROTECT_VIEW = os.environ.get("PROTECT_VIEW", "1").lower() not in ("0", "false", "no")
# 允许用 ?key= 传密钥（给 VLC 之类只能填 URL 的播放器用）。
# 默认关：URL 会进浏览器历史和 uvicorn access log。
ALLOW_KEY_IN_QUERY = os.environ.get("ALLOW_KEY_IN_QUERY", "0").lower() in ("1", "true", "yes")
# 浏览器登录会话有效期（秒），默认 7 天
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(7 * 24 * 3600)))
COOKIE_NAME = "cam_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")

if not CAM_API_KEY:
    LOG.error("CAM_API_KEY 未配置：/api/frame 将拒绝一切推流（503）。"
              "请在 .env 中设置后重启容器")
elif len(CAM_API_KEY) < 16:
    LOG.warning("CAM_API_KEY 只有 %d 个字符，建议至少 16 位随机字符串", len(CAM_API_KEY))
LOG.info("auth: push key %s, view protection %s",
         "enabled" if CAM_API_KEY else "MISSING", "on" if PROTECT_VIEW else "off")


@dataclass
class Frame:
    """最新一帧。jpeg 在收到时就转好，避免每个观看者都重复转一次。"""

    jpeg: bytes
    width: int
    height: int
    seq: int
    received_at: float
    clients: int = 0
    total: int = 0


class FrameStore:
    """最新帧 + 订阅者通知。

    设备是按固定帧率推的，服务端只保留「最新一帧」——
    慢的观看者会丢帧而不是把内存撑爆，这对监控场景是对的。
    """

    def __init__(self) -> None:
        self._frame: Optional[Frame] = None
        self._cond = asyncio.Condition()
        self._total_received = 0
        self._last_seq = -1
        self._duplicates = 0

    async def put(self, frame: Frame) -> None:
        async with self._cond:
            self._frame = frame
            self._total_received += 1
            self._cond.notify_all()

    async def wait_for_new(self, last: int, timeout: float = 5.0):
        """等到比 last更新的帧；超时返回 None（让生成器有机会退出）。"""
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

    @property
    def duplicates(self) -> int:
        return self._duplicates

    def note_duplicate(self) -> None:
        self._duplicates += 1


store = FrameStore()
# 关掉自动生成的 /docs、/openapi.json：这是个内网摄像头服务，
# 没必要把接口清单公开出去（也不影响任何业务接口）。
app = FastAPI(title="BiliMouse Camera Server", version="1.1.0",
              docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- 鉴权 ----------------
# 简单的失败限速：同一 IP 60 秒内失败 20 次就先拒 429，拖慢暴力猜密钥。
# 只记计数不记密钥；局域网规模下这张表很小。
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
    """常量时间比较，避免用响应耗时逐字节猜密钥。"""
    if not CAM_API_KEY or not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), CAM_API_KEY)


def _key_from_request(request: Request) -> Optional[str]:
    """优先级：X-Api-Key > Authorization: Bearer > ?key=（默认禁用）。"""
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if not key and ALLOW_KEY_IN_QUERY:
        key = request.query_params.get("key")
    return key


def _session_token() -> str:
    """签发观看会话：exp.HMAC(key, "view|exp")。

    cookie 里不含密钥本身，拿到 cookie 也推不了流；改了 CAM_API_KEY 后
    旧会话立即失效。
    """
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
    """推流鉴权：必须在读 body 之前执行，未授权的大包不进内存。"""
    if not CAM_API_KEY:
        raise HTTPException(status_code=503,
                            detail="server has no CAM_API_KEY configured")
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(status_code=429, detail="too many auth failures")
    if not _key_matches(_key_from_request(request)):
        _note_fail(ip)
        LOG.warning("rejected frame from %s: bad or missing API key", ip)
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
    """RGB565（小端）-> JPEG。

    Pillow 的 "BGR;16" 原始解码器正好是 R5G6B5 小端，
    和设备端 gen_bg.py / ui 的 RGB565 打包方式完全一致。
    """
    need = width * height * 2
    if len(raw) < need:
        raise ValueError(f"frame too short: {len(raw)} < {need}")
    img = Image.frombytes("RGB", (width, height), raw[:need], "raw", "BGR;16")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


@app.post("/api/frame")
async def receive_frame(
    request: Request,
    _auth=Depends(require_push_key),
    x_frame_format: str = Header(default="rgb565"),
    x_frame_width: int = Header(default=320),
    x_frame_height: int = Header(default=240),
    x_frame_seq: int = Header(default=0),
) -> JSONResponse:
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

    # 定时存图（默认关）
    if SAVE_EVERY > 0 and store.total % SAVE_EVERY == 0:
        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            name = f"frame_{int(frame.received_at)}_{seq:06d}.jpg"
            (SNAPSHOT_DIR / name).write_bytes(jpeg)
        except OSError as e:
            LOG.warning("save snapshot failed: %s", e)

    LOG.info("frame #%d %dx%d %d bytes -> %d bytes jpeg (total %d)",
             seq, x_frame_width, x_frame_height, len(body), len(jpeg), store.total)
    return JSONResponse({"ok": True, "seq": seq, "jpeg_bytes": len(jpeg)})


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
    })


@app.get("/api/stream.mjpg")
async def stream(request: Request, _auth=Depends(require_view_key)) -> StreamingResponse:
    """MJPEG 视频流。多个观看者各自独立，服务端只保留最新一帧。"""

    async def gen():
        last_seq = -1
        VIEWERS["n"] += 1
        try:
            while True:
                if await request.is_disconnected():
                    break
                f = await store.wait_for_new(last_seq, timeout=5.0)
                if f is None:
                    # 5 秒没有新帧：继续等（客户端只是画面停住，不会断流）
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


# 当前正在看直播的人数
VIEWERS = {"n": 0}


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    """网页登录：校验密钥后下发 HttpOnly 签名 cookie。

    <img src="/api/stream.mjpg"> 没法带自定义请求头，所以浏览器走 cookie；
    cookie 里是 HMAC 签名而不是密钥本身，拿到它也推不了流。
    """
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
    resp = JSONResponse({"ok": True, "ttl": SESSION_TTL})
    resp.set_cookie(
        COOKIE_NAME, _session_token(), max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/",
    )
    return resp


@app.post("/api/logout")
async def logout() -> JSONResponse:
    """清掉观看会话（只是删 cookie，不影响推流密钥）。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
