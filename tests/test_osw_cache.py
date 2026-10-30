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


if __name__ == "__main__":
    unittest.main()
