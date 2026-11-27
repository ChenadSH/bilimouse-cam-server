"""
BiliMouse 摄像头推流服务端

设备端（ESP32-S3 + GC0308）把 RGB565 原始帧 POST 到这里：

    POST /api/frame
      Content-Type: application/octet-stream
      X-Frame-Format: rgb565
      X-Frame-Width: 320
      X-Frame-Height: 240
      X-Frame-Seq: 42
      <body = width*height*2 字节的 RGB565 像素>

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
import io
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
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
app = FastAPI(title="BiliMouse Camera Server", version="1.0.0")


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
async def snapshot() -> Response:
    f = store.frame
    if f is None:
        raise HTTPException(status_code=404, detail="no frame yet")
    return Response(content=f.jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def status() -> JSONResponse:
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
async def stream(request: Request) -> StreamingResponse:
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


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
