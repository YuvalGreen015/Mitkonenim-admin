"""End-to-end privacy test: starts the real server and checks who can see what."""
import json, os, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); PORT = 8093; BASE = f"http://127.0.0.1:{PORT}"
PW = "correct horse battery"

def call(path, body=None, headers=None, method=None):
    req = urllib.request.Request(BASE + path, None if body is None else json.dumps(body).encode(),
                                 {"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=5); return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        env = {**os.environ, "PORT": str(PORT), "DB_PATH": cls.d + "/t.db", "DASHBOARD_PASSWORD": PW, "ALLOWED_ORIGIN": "https://me.github.io"}
        cls.p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try: call("/"); break
            except Exception: time.sleep(.1)
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait()

    def test_1_strangers_see_only_the_login_page(self):
        s, b, _ = call("/"); self.assertEqual(s, 200); self.assertIn(b"Private dashboard", b); self.assertNotIn(b"Live installs", b)
        self.assertEqual(call("/api/metrics")[0], 401); self.assertEqual(call("/api/stream")[0], 401)
        self.assertEqual(call("/api/metrics", headers={"Cookie": "an_sess=1999999999.deadbeef"})[0], 401)   # forged cookie

    def test_2_app_endpoints_stay_open_and_dashboard_has_no_cors(self):
        tok = "t" * 40
        s, b, h = call("/api/session", {"event_id": "e1", "token": tok}); self.assertEqual(s, 200); self.assertEqual(h.get("Access-Control-Allow-Origin"), "https://me.github.io")
        s, _, h = call("/api/session", method="OPTIONS"); self.assertEqual(s, 204); self.assertIn("content-type", h["Access-Control-Allow-Headers"])
        self.assertIsNone(call("/api/metrics")[2].get("Access-Control-Allow-Origin"))
        self.assertEqual(call("/api/metrics", method="OPTIONS")[0], 404)

    def test_3_password_login_then_access(self):
        s, _, h = call("/api/login", {"password": PW}); self.assertEqual(s, 200)
        ck = h["Set-Cookie"]; self.assertIn("HttpOnly", ck); self.assertIn("SameSite=Strict", ck)
        cookie = ck.split(";")[0]
        s, b, _ = call("/api/metrics", headers={"Cookie": cookie}); self.assertEqual(s, 200); self.assertIn("total_installs", json.loads(b))
        s, b, _ = call("/", headers={"Cookie": cookie}); self.assertIn(b"Live installs", b)

    def test_9_brute_force_is_throttled(self):                         # runs last (name order): locks this address out
        codes = [call("/api/login", {"password": f"guess{i}"})[0] for i in range(10)]
        self.assertEqual(codes[0], 401); self.assertEqual(codes[-1], 429)
        self.assertEqual(call("/api/login", {"password": PW})[0], 429)  # even the right password is refused while locked

if __name__ == "__main__": unittest.main()
