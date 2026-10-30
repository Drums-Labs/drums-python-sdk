"""drums_osw.py: every helper run gets a private cache directory (no OSWorld needed)."""
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("drums_osw", os.path.join(HERE, "..", "..", "osw", "drums_osw.py"))
osw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osw)


class PrivateCache(unittest.TestCase):
    def test_concurrent_runs_of_one_task_never_share_fetched_files(self):
        # before: OSWorld's vm_file getter wrote every slot's fetched file to <cache>/<task id>/<dest>, one path for
        # all slots of the task, so a slot verifying while another did could be scored on the other's file
        with tempfile.TemporaryDirectory() as cache:
            tid = "035f41ba-6653-43ab-aa63-c86d449d62e5"
            tdir = os.path.join(cache, tid)
            os.makedirs(tdir)
            open(os.path.join(tdir, "gold.xlsx"), "w").write("expected")
            open(os.path.join(tdir, "IncomeStatement2.xlsx"), "w").write("another slot's desktop file")
            staged = [{"name": "gold.xlsx"}]
            a = osw.private_cache(cache, tid, staged)
            b = osw.private_cache(cache, tid, staged)
            self.assertNotEqual(a, b)
            for d in (a, b):
                self.assertEqual(sorted(os.listdir(d)), ["gold.xlsx"])      # staged files only, nothing fetched by others
                self.assertEqual(open(os.path.join(d, "gold.xlsx")).read(), "expected")
            open(os.path.join(a, "IncomeStatement2.xlsx"), "w").write("slot A")
            open(os.path.join(b, "IncomeStatement2.xlsx"), "w").write("slot B")
            self.assertEqual(open(os.path.join(a, "IncomeStatement2.xlsx")).read(), "slot A")
            self.assertEqual(open(os.path.join(b, "IncomeStatement2.xlsx")).read(), "slot B")

    def test_main_uses_a_private_cache_for_setup_and_evaluate(self):
        src = open(osw.__file__).read()
        self.assertIn("env.cache_dir = private_cache(", src)
        self.assertIn("env.setup_controller.reset_cache_dir(env.cache_dir)", src)


class ForkServer(unittest.TestCase):
    def test_requests_go_through_the_server_and_fall_back_without_it(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as d:
            osd = os.path.join(d, "osworld")
            for m in ("desktop_env", "desktop_env/controllers", "desktop_env/evaluators"):
                os.makedirs(os.path.join(osd, m), exist_ok=True)
                open(os.path.join(osd, m, "__init__.py"), "w").close()
            for m in ("controllers/python", "controllers/setup", "desktop_env", "evaluators/getters", "evaluators/metrics"):
                open(os.path.join(osd, "desktop_env", m + ".py"), "w").close()
            sock = os.path.join(d, "osw.sock")
            self.assertIsNone(osw.via_server("stage", {}, sock))           # no server: the caller does the work
            p = subprocess.Popen([sys.executable, "-I", osw.__file__, "serve", "--osworld-dir", osd, "--sock", sock],
                                 stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)
                req = {"task": {"id": "t1"}, "cache_dir": os.path.join(d, "cache")}
                outs = [osw.via_server("stage", req, sock) for _ in range(5)]
                self.assertTrue(all(o["ok"] and o["staged"] == [] for o in outs), outs)
                bad = osw.via_server("evaluate", {"task": {"id": "t1"}, "cache_dir": os.path.join(d, "cache")}, sock)
                self.assertFalse(bad["ok"])                                 # a failing worker still answers
            finally:
                p.kill()
                p.wait()


if __name__ == "__main__":
    unittest.main()
