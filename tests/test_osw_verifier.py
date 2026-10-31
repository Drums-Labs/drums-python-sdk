"""drums_osw.py lab verifiers (`"func": "drums_verifier"`): loading rules, the desktop context, scores and errors.
Runs without OSWorld: a stand-in desktop_env package and a stand-in guest server."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("drums_osw_v", os.path.join(HERE, "..", "..", "osw", "drums_osw.py"))
osw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osw)

FAKE_OSWORLD = {
    "desktop_env/__init__.py": "",
    "desktop_env/controllers/__init__.py": "",
    "desktop_env/evaluators/__init__.py": "",
    "desktop_env/evaluators/getters.py": "",
    "desktop_env/evaluators/metrics.py": "def exact_match(a, b):\n    return float(a == b)\n",
    "desktop_env/controllers/python.py": "class PythonController:\n    def __init__(self, vm_ip, server_port):\n        pass\n",
    "desktop_env/controllers/setup.py": (
        "SETUPS = []\n"
        "class SetupController:\n"
        "    def __init__(self, **kw):\n        pass\n"
        "    def reset_cache_dir(self, d):\n        self.cache_dir = d\n"
        "    def setup(self, config, use_proxy=False):\n        SETUPS.append(config)\n        return True\n"),
    # OSWorld's own _set_task_info resolves the evaluator func in its metrics table: an unknown func raises there
    "desktop_env/desktop_env.py": (
        "import os\nfrom desktop_env.evaluators import metrics\n"
        "class DesktopEnv:\n"
        "    def _set_task_info(self, t):\n"
        "        self.task_id = t['id']; self.cache_dir = os.path.join(self.cache_dir_base, t['id'])\n"
        "        os.makedirs(self.cache_dir, exist_ok=True); self.instruction = t['instruction']; self.config = t.get('config', [])\n"
        "        self._set_evaluator_info(t)\n"
        "    def _set_evaluator_info(self, t):\n        self.evaluator = t['evaluator']; self.metric = getattr(metrics, t['evaluator']['func'])\n"
        "    def evaluate(self):\n        return 1.0\n"),
}

VERIFIER = '''
import os, sqlite3
def expense_ok(ctx, amount_cents):
    data = ctx.read_file("/home/user/acme/acme.db")
    if data is None:
        return {"score": 0.0, "info": {"why": "no db"}}
    p = os.path.join(ctx.workdir, "a.db"); open(p, "wb").write(data)
    n = sqlite3.connect(p).execute("SELECT count(*) FROM expenses WHERE amount_cents=?", (amount_cents,)).fetchone()[0]
    return {"score": float(n == 1), "info": {"rows": n}}
def via_run(ctx):
    return 1.0 if ctx.run(["test", "-f", "/x"])["returncode"] == 0 else 0.0
def boolean(ctx):
    return True
def nan(ctx):
    return float("nan")
def text(ctx):
    return "1.0"
def boom(ctx):
    raise ValueError("bad state")
def missing_file(ctx):
    return 0.0 if ctx.read_file("/nope") is None else 1.0
'''


class Guest(BaseHTTPRequestHandler):
    files, calls = {}, []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Guest.calls.append((self.path, body))
        if self.path == "/file":
            from urllib.parse import parse_qs
            path = parse_qs(body.decode())["file_path"][0]
            if path == "/broken":
                return self._send(500, b"server error")
            data = Guest.files.get(path)
            return self._send(200, data) if data is not None else self._send(404, b"not found")
        if self.path == "/execute":
            cmd = json.loads(body)["command"]
            return self._send(200, json.dumps({"status": "success", "returncode": 0 if cmd[-1] == "/x" else 1,
                                               "output": "", "error": ""}).encode())
        self._send(404, b"")

    def _send(self, code, b):
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def lab_task(function, **options):
    return {"id": "acme-test-01", "instruction": "x", "config": [{"type": "sleep", "parameters": {"seconds": 0}}],
            "evaluator": {"func": "drums_verifier", "verifier": "lab", "function": function, "options": options}}


class LabVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.osd = os.path.join(d, "osworld")
        for rel, src in FAKE_OSWORLD.items():
            os.makedirs(os.path.dirname(os.path.join(cls.osd, rel)), exist_ok=True)
            open(os.path.join(cls.osd, rel), "w").write(src)
        cls.vdir = os.path.join(d, "verifiers")
        os.makedirs(cls.vdir, mode=0o755)
        os.chmod(cls.vdir, 0o755)
        open(os.path.join(cls.vdir, "lab.py"), "w").write(VERIFIER)
        os.chmod(os.path.join(cls.vdir, "lab.py"), 0o644)
        db = os.path.join(d, "acme.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE expenses(amount_cents INTEGER)")
        c.execute("INSERT INTO expenses VALUES (8450)")
        c.commit()
        c.close()
        Guest.files = {"/home/user/acme/acme.db": open(db, "rb").read()}
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Guest)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cwd = os.getcwd()
        osw.TRUSTED_UID = os.getuid()      # the test's files stand in for root-owned ones

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.chdir(cls.cwd)
        cls.tmp.cleanup()

    def req(self, task, vdir=None):
        return {"task": task, "host": "127.0.0.1", "server_port": self.srv.server_address[1], "chromium_port": None,
                "vlc_port": None, "cache_dir": os.path.join(self.tmp.name, "cache"), "osworld_dir": self.osd,
                "verifiers_dir": vdir or self.vdir}

    def evaluate(self, task, vdir=None):
        return osw.run_request("evaluate", self.req(task, vdir))

    def test_setup_of_a_lab_task_runs_its_config_without_resolving_the_func_in_osworlds_metrics(self):
        r = osw.run_request("setup", self.req(lab_task("expense_ok", amount_cents=8450)))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["n_steps"], 1)

    def test_an_osworld_task_still_goes_through_osworld(self):
        t = {"id": "osw-1", "instruction": "x", "config": [], "evaluator": {"func": "exact_match"}}
        r = self.evaluate(t)
        self.assertEqual((r["score"], r["checker_error"]), (1.0, None))
        self.assertNotIn("verifier", r)
        # OSWorld's behaviour for an unknown func is unchanged: the setup fails (0.4.2 failed every lab task this way)
        with self.assertRaises(AttributeError):
            osw.run_request("setup", self.req({**t, "evaluator": {"func": "no_such_metric"}}))

    def test_score_and_info_from_the_desktop_state(self):
        r = self.evaluate(lab_task("expense_ok", amount_cents=8450))
        self.assertEqual((r["score"], r["checker_error"]), (1.0, None), r)
        self.assertEqual(r["info"], {"rows": 1})
        self.assertEqual(r["verifier"], "lab.expense_ok")
        r0 = self.evaluate(lab_task("expense_ok", amount_cents=1))
        self.assertEqual((r0["score"], r0["info"]), (0.0, {"rows": 0}))

    def test_run_returns_the_exit_status_and_bool_is_a_score(self):
        self.assertEqual(self.evaluate(lab_task("via_run"))["score"], 1.0)
        self.assertEqual(self.evaluate(lab_task("boolean"))["score"], 1.0)
        self.assertEqual(self.evaluate(lab_task("missing_file"))["score"], 0.0)   # 404 is "no file", a valid 0

    def test_a_bad_return_value_or_an_exception_is_a_checker_error_never_a_score(self):
        for fn, needle in (("nan", "finite number"), ("text", "finite number"), ("boom", "ValueError: bad state"),
                           ("absent", "has no function")):
            r = self.evaluate(lab_task(fn))
            self.assertIsNone(r["score"], fn)
            self.assertIn(needle, r["checker_error"], fn)

    def test_a_guest_error_reading_a_file_is_a_checker_error(self):
        src = os.path.join(self.vdir, "lab.py")
        t = lab_task("missing_file")
        open(os.path.join(self.vdir, "broken.py"), "w").write("def f(ctx):\n    return 0.0 if ctx.read_file('/broken') is None else 1.0\n")
        os.chmod(os.path.join(self.vdir, "broken.py"), 0o644)
        t["evaluator"].update(verifier="broken", function="f")
        r = self.evaluate(t)
        self.assertIsNone(r["score"])
        self.assertIn("500", r["checker_error"])
        self.assertTrue(os.path.exists(src))

    def test_only_trusted_files_are_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            p = os.path.join(d, "lab.py")
            open(p, "w").write(VERIFIER)
            os.chmod(p, 0o666)                                    # world-writable: anyone could change the reward
            r = self.evaluate(lab_task("boolean"), vdir=d)
            self.assertIsNone(r["score"])
            self.assertIn("not group/world-writable", r["checker_error"])
            os.chmod(p, 0o644)
            os.chmod(d, 0o777)                                    # a writable directory: the file could be swapped
            self.assertIn("not group/world-writable", self.evaluate(lab_task("boolean"), vdir=d)["checker_error"])
            os.chmod(d, 0o755)
            self.assertEqual(self.evaluate(lab_task("boolean"), vdir=d)["score"], 1.0)
            osw.TRUSTED_UID = os.getuid() + 1                     # not owned by the trusted user (root on a host)
            try:
                self.assertIn("must be owned by root", self.evaluate(lab_task("boolean"), vdir=d)["checker_error"])
            finally:
                osw.TRUSTED_UID = os.getuid()

    def test_a_task_names_a_verifier_it_cannot_point_at_a_path(self):
        for name in ("../lab", "/etc/passwd", "lab.py", "", None):
            t = lab_task("boolean")
            t["evaluator"]["verifier"] = name
            r = self.evaluate(t)
            self.assertIsNone(r["score"], name)
            self.assertIn("letters, digits and _ only", r["checker_error"], name)

    def test_a_missing_verifier_says_how_to_install_it(self):
        t = lab_task("boolean")
        t["evaluator"]["verifier"] = "nobody"
        self.assertIn("docs/custom-tasks.md", self.evaluate(t)["checker_error"])


if __name__ == "__main__":
    unittest.main()
