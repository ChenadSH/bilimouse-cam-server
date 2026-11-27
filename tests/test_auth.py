"""鉴权回归测试。

运行：
    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v
"""
import os
import sys
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
from app.main import app, store  # noqa: E402

# 2x2 的一帧 RGB565 = 8 字节
FRAME = bytes([0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00, 0xFF, 0xFF])
HDR = {
    "X-Frame-Format": "rgb565",
    "X-Frame-Width": "2",
    "X-Frame-Height": "2",
    "X-Frame-Seq": "1",
}


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)

    def post(self, headers=None, body=FRAME):
        h = dict(HDR)
        if headers:
            h.update(headers)
        return self.c.post("/api/frame", content=body, headers=h)

    # ---- 推流：必须带密钥 ----
    def test_01_no_key_rejected(self):
        r = self.post()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], "unauthorized")

    def test_02_wrong_key_rejected(self):
        self.assertEqual(self.post({"X-Api-Key": "wrong-key"}).status_code, 401)

    def test_03_unauthorized_frame_not_stored(self):
        before = store.total
        self.post()
        self.post({"X-Api-Key": "wrong"})
        self.assertEqual(store.total, before)

    def test_04_correct_key_accepted(self):
        r = self.post({"X-Api-Key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_05_bearer_accepted(self):
        self.assertEqual(self.post({"Authorization": "Bearer " + KEY}).status_code, 200)

    def test_06_query_key_disabled_by_default(self):
        r = self.c.post("/api/frame?key=" + KEY, content=FRAME, headers=HDR)
        self.assertEqual(r.status_code, 401)

    # ---- 观看：默认也要鉴权 ----
    def test_07_view_requires_auth(self):
        for path in ("/api/status", "/api/snapshot.jpg", "/api/stream.mjpg"):
            self.assertEqual(self.c.get(path).status_code, 401, path)

    def test_08_view_with_header(self):
        self.assertEqual(
            self.c.get("/api/status", headers={"X-Api-Key": KEY}).status_code, 200)
        self.post({"X-Api-Key": KEY})
        self.assertEqual(
            self.c.get("/api/snapshot.jpg", headers={"X-Api-Key": KEY}).status_code, 200)

    # ---- 网页登录：签名 cookie ----
    def test_09_login_and_cookie(self):
        self.assertEqual(self.c.post("/api/login", json={"key": "wrong"}).status_code, 401)
        r = self.c.post("/api/login", json={"key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertIn("HttpOnly", r.headers.get("set-cookie", ""))
        self.assertNotIn(KEY, r.headers.get("set-cookie", ""))  # cookie 不含密钥
        self.post({"X-Api-Key": KEY})
        self.assertEqual(self.c.get("/api/status").status_code, 200)
        self.assertEqual(self.c.get("/api/snapshot.jpg").status_code, 200)

    def test_10_tampered_cookie_rejected(self):
        c = TestClient(app)
        c.cookies.set("cam_session", "9999999999.deadbeef")
        self.assertEqual(c.get("/api/status").status_code, 401)

    def test_11_expired_cookie_rejected(self):
        c = TestClient(app)
        c.cookies.set("cam_session", "1.deadbeef")
        self.assertEqual(c.get("/api/status").status_code, 401)

    def test_12_logout_clears_session(self):
        c = TestClient(app)
        self.assertEqual(c.post("/api/login", json={"key": KEY}).status_code, 200)
        self.assertEqual(c.get("/api/status").status_code, 200)
        c.post("/api/logout")
        self.assertEqual(c.get("/api/status").status_code, 401)

    def test_13_login_by_header(self):
        self.assertEqual(
            self.c.post("/api/login", headers={"X-Api-Key": KEY}).status_code, 200)

    # ---- 免密端点 ----
    def test_14_healthz_and_index_public(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_15_docs_disabled(self):
        for path in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(self.c.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
