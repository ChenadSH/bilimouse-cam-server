"""按需推流 / 录制 / 掉线自动停止 / 删除 的功能测试。

运行：
    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v

注意：这些测试直接操作 app.main 的模块级可变状态（GATE / PRESENCE /
VIEWERS / RECORD_DIR / recorder），不依赖 unittest 的模块导入顺序。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KEY = "test-secret-0123456789abcdef"
os.environ["CAM_API_KEY"] = KEY
os.environ["PROTECT_VIEW"] = "1"
os.environ["ALLOW_KEY_IN_QUERY"] = "0"
os.environ["COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402
from app.main import app, store  # noqa: E402

# 2x2 的一帧 RGB565 = 8 字节
FRAME = bytes([0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00, 0xFF, 0xFF])
HDR = {
    "X-Frame-Format": "rgb565",
    "X-Frame-Width": "2",
    "X-Frame-Height": "2",
    "X-Frame-Seq": "1",
}
AUTH = {"X-Api-Key": KEY}


class FeatureTest(unittest.TestCase):
    def setUp(self):
        # 每次都把共享状态复位，测试之间互不影响
        self._gate = main.GATE["enabled"]
        self._presence = main.PRESENCE["last"]
        self._viewers = main.VIEWERS["n"]
        self._record_dir = main.RECORD_DIR
        self._play_fps = main.RECORD_PLAY_FPS
        self._offline = main.RECORD_OFFLINE_TIMEOUT
        self._maxsec = main.RECORD_MAX_SECONDS

        main.PRESENCE["last"] = 0.0
        main.VIEWERS["n"] = 0
        if main.recorder.active:
            main.recorder.stop("test_reset")
        self._tmp = tempfile.TemporaryDirectory(prefix="bm-rec-")
        main.RECORD_DIR = Path(self._tmp.name)
        main.RECORD_PLAY_FPS = 200.0   # 回放测试别真的 sleep
        self.c = TestClient(app)

    def tearDown(self):
        if main.recorder.active:
            main.recorder.stop("test_reset")
        main.GATE["enabled"] = self._gate
        main.PRESENCE["last"] = self._presence
        main.VIEWERS["n"] = self._viewers
        main.RECORD_DIR = self._record_dir
        main.RECORD_PLAY_FPS = self._play_fps
        main.RECORD_OFFLINE_TIMEOUT = self._offline
        main.RECORD_MAX_SECONDS = self._maxsec
        self._tmp.cleanup()

    # ---- 小工具 ----
    def gate(self, auth=True):
        return self.c.get("/api/push_gate", headers=AUTH if auth else None)

    def post(self, headers=None, body=FRAME):
        h = dict(HDR)
        if headers:
            h.update(headers)
        return self.c.post("/api/frame", content=body, headers=h)

    def demand_off(self):
        main.GATE["enabled"] = True
        main.PRESENCE["last"] = 0.0
        main.VIEWERS["n"] = 0
        if main.recorder.active:
            main.recorder.stop("test_reset")

    # ---- 按需推流闸门 ----
    def test_01_idle_gate_closed(self):
        self.demand_off()
        r = self.gate()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["push"])
        self.assertEqual(r.json()["reason"], "idle")

    def test_02_gate_requires_key(self):
        self.assertEqual(self.gate(auth=False).status_code, 401)

    def test_03_frame_dropped_when_no_demand(self):
        self.demand_off()
        before = store.total
        r = self.post({"X-Api-Key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertFalse(r.json()["push"])
        self.assertTrue(r.json()["dropped"])
        self.assertEqual(store.total, before)   # 帧没进内存/没解码

    def test_04_auth_checked_before_gate(self):
        self.demand_off()
        # 没密钥时即便闸门关着，也应该是 401（鉴权优先），不能泄露 dropped
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post({"X-Api-Key": "wrong"}).status_code, 401)

    def test_05_presence_enables_push(self):
        self.demand_off()
        p = self.c.post("/api/presence")      # 免密的网页报活
        self.assertEqual(p.status_code, 200)
        self.assertTrue(p.json()["push"])
        self.assertTrue(self.gate().json()["push"])
        before = store.total
        r = self.post({"X-Api-Key": KEY})
        self.assertTrue(r.json()["ok"])
        self.assertFalse(r.json().get("dropped", False))
        self.assertEqual(store.total, before + 1)

    def test_06_viewer_enables_push(self):
        self.demand_off()
        main.VIEWERS["n"] = 1
        self.assertTrue(self.gate().json()["push"])
        self.assertEqual(self.gate().json()["reason"], "viewer")

    def test_07_presence_expires(self):
        self.demand_off()
        main.PRESENCE["last"] = time.time() - (main.PRESENCE_TTL + 1)
        self.assertFalse(self.gate().json()["push"])

    def test_08_gate_disabled_always_accepts(self):
        self.demand_off()
        main.GATE["enabled"] = False
        self.assertTrue(self.gate().json()["push"])
        self.assertEqual(self.gate().json()["reason"], "forced")
        before = store.total
        self.assertFalse(self.post({"X-Api-Key": KEY}).json().get("dropped", False))
        self.assertEqual(store.total, before + 1)

    # ---- 录制 ----
    def test_10_record_start_needs_auth(self):
        self.assertEqual(self.c.post("/api/record/start").status_code, 401)

    def test_11_record_lifecycle_and_file(self):
        self.demand_off()
        r = self.c.post("/api/record/start", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["active"])
        # 录制本身就是需求，设备被允许推流
        self.assertTrue(self.gate().json()["push"])
        self.assertEqual(self.gate().json()["reason"], "recording")

        for i in range(3):
            h = dict(HDR); h["X-Frame-Seq"] = str(i + 1); h.update(AUTH)
            rr = self.c.post("/api/frame", content=FRAME, headers=h)
            self.assertEqual(rr.status_code, 200)
            self.assertFalse(rr.json().get("dropped", False))

        path = main.recorder.path
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}-\d{2}$")  # 按日期目录
        raw = path.read_bytes()
        self.assertGreaterEqual(raw.count(b"\xff\xd8"), 3)   # 3 帧 JPEG SOI
        self.assertIn(b"--frame", raw)

        s = self.c.get("/api/record/status", headers=AUTH)
        self.assertTrue(s.json()["active"])
        self.assertEqual(s.json()["frames"], 3)

        st = self.c.post("/api/record/stop", headers=AUTH)
        self.assertEqual(st.status_code, 200)
        self.assertFalse(self.c.get("/api/record/status", headers=AUTH).json()["active"])
        self.assertEqual(st.json()["last"]["stop_reason"], "manual")
        self.assertTrue(path.with_suffix(".json").exists())   # 元数据

    def test_12_record_list_download_play_delete(self):
        self.demand_off()
        self.c.post("/api/record/start", headers=AUTH)
        h = dict(HDR); h.update(AUTH)
        self.c.post("/api/frame", content=FRAME, headers=h)
        self.c.post("/api/record/stop", headers=AUTH)
        date = main.recorder.date
        name = main.recorder.name

        lst = self.c.get("/api/recordings", headers=AUTH)
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(lst.json()["total"], 1)
        self.assertIn(date, lst.json()["groups"])

        dl = self.c.get(f"/api/recordings/{date}/{name}", headers=AUTH)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.content[:2], b"--")
        self.assertIn(b"\xff\xd8", dl.content)

        play = self.c.get(f"/api/recordings/{date}/{name}/play", headers=AUTH)
        self.assertEqual(play.status_code, 200)
        self.assertIn("multipart", play.headers["content-type"])
        self.assertIn(b"\xff\xd8", play.content)

        de = self.c.delete(f"/api/recordings/{date}/{name}", headers=AUTH)
        self.assertEqual(de.status_code, 200)
        self.assertFalse((main.RECORD_DIR / date / name).exists())
        self.assertEqual(self.c.get("/api/recordings", headers=AUTH).json()["total"], 0)

    def test_13_delete_day_and_all(self):
        self.demand_off()
        for _ in range(2):
            self.c.post("/api/record/start", headers=AUTH)
            h = dict(HDR); h.update(AUTH)
            self.c.post("/api/frame", content=FRAME, headers=h)
            self.c.post("/api/record/stop", headers=AUTH)
        date = main.recorder.date
        self.assertGreaterEqual(self.c.get("/api/recordings", headers=AUTH).json()["total"], 1)
        self.assertEqual(self.c.delete(f"/api/recordings/{date}", headers=AUTH).status_code, 200)
        self.assertFalse((main.RECORD_DIR / date).exists())
        self.assertEqual(self.c.delete("/api/recordings", headers=AUTH).status_code, 200)

    def test_14_path_traversal_blocked(self):
        from fastapi import HTTPException
        # API 层：非法日期 / 非法文件名一律 400，不会落到文件系统
        self.assertEqual(
            self.c.get("/api/recordings/not-a-date/rec_120000.mjpg", headers=AUTH).status_code, 400)
        self.assertEqual(
            self.c.get("/api/recordings/2026-11-27/evil.mjpg", headers=AUTH).status_code, 400)
        self.assertEqual(
            self.c.delete("/api/recordings/not-a-date", headers=AUTH).status_code, 400)
        # 函数层：即使拼出 ../ 也被 resolve + parents 校验挡住
        for date, name in [("2026-11-27", "../../etc/passwd"),
                           ("..", "rec_120000.mjpg"),
                           ("2026-11-27", "..%2f..%2fsecret")]:
            with self.assertRaises(HTTPException) as ctx:
                main._resolve_recording(date, name)
            self.assertEqual(ctx.exception.status_code, 400)

    # ---- 自动停止 ----
    def test_15_max_duration_auto_stop(self):
        self.demand_off()
        self.c.post("/api/record/start", headers=AUTH)
        main.recorder.start_ts = time.time() - (main.RECORD_MAX_SECONDS + 1)
        main.enforce_record_limits()
        self.assertFalse(main.recorder.active)
        self.assertEqual(main.recorder.last_meta["stop_reason"], "max_duration")

    def test_16_device_offline_auto_stop(self):
        self.demand_off()
        self.c.post("/api/record/start", headers=AUTH)
        h = dict(HDR); h.update(AUTH)
        self.c.post("/api/frame", content=FRAME, headers=h)
        # 模拟设备掉线：录制中很久没收到新帧
        main.recorder.last_frame_at = time.time() - (main.RECORD_OFFLINE_TIMEOUT + 1)
        main.enforce_record_limits()
        self.assertFalse(main.recorder.active)
        self.assertEqual(main.recorder.last_meta["stop_reason"], "device_offline")

    def test_17_no_auto_stop_while_frames_flow(self):
        self.demand_off()
        self.c.post("/api/record/start", headers=AUTH)
        h = dict(HDR); h.update(AUTH)
        self.c.post("/api/frame", content=FRAME, headers=h)
        main.enforce_record_limits()
        self.assertTrue(main.recorder.active)   # 帧在流，不能误判掉线

    # ---- 状态接口新增字段 ----
    def test_18_status_exposes_gate_and_recording(self):
        self.demand_off()
        s = self.c.get("/api/status", headers=AUTH)
        self.assertEqual(s.status_code, 200)
        j = s.json()
        self.assertIn("push_gate", j)
        self.assertIn("gate_reason", j)
        self.assertIn("recording", j)
        self.assertIn("device_streaming", j)
        self.assertFalse(j["push_gate"])

    # ---- 观看端鉴权仍生效 ----
    def test_19_record_apis_require_view_auth(self):
        for method, path in [
            ("get", "/api/record/status"),
            ("get", "/api/recordings"),
            ("post", "/api/record/stop"),
        ]:
            r = getattr(self.c, method)(path)
            self.assertEqual(r.status_code, 401, path)
        self.assertEqual(self.c.delete("/api/recordings").status_code, 401)

    def test_20_healthz_and_index_public(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
