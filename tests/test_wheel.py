"""The release's SDK wheel: built from these sources with the standard library, byte-reproducible, importable, and
installable by pip without root (no build step in the source tree)."""
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.dont_write_bytecode = True     # no __pycache__ in install/
import build_wheel  # noqa: E402


class WheelTests(unittest.TestCase):
    def test_wheel_is_reproducible_complete_and_importable(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            w1, w2 = build_wheel.build(a), build_wheel.build(b)
            self.assertEqual(open(w1, "rb").read(), open(w2, "rb").read())
            import drums
            self.assertTrue(os.path.basename(w1) == f"drums-{drums.__version__}-py3-none-any.whl", w1)
            with zipfile.ZipFile(w1) as z:
                names = z.namelist()
                rec = z.read(f"drums-{drums.__version__}.dist-info/RECORD").decode().splitlines()
                for line in rec:
                    n, h, size = line.rsplit(",", 2)
                    if not h:
                        continue
                    data = z.read(n)
                    self.assertEqual(h, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), n)
                    self.assertEqual(int(size), len(data))
            for mod in ("drums/__init__.py", "drums/aio.py", "drums/errors.py"):
                self.assertIn(mod, names)
            # a pure-Python wheel is importable as a zip: the package works from the wheel alone
            r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {w1!r}); import drums, drums.aio; "
                                "print(drums.__version__, drums.__file__)"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(w1, r.stdout)

    @unittest.skipUnless(os.environ.get("DRUMS_TEST_PIP") == "1", "set DRUMS_TEST_PIP=1 to pip install the wheel into a venv")
    def test_pip_installs_the_wheel_into_a_venv(self):
        with tempfile.TemporaryDirectory() as d:
            whl = build_wheel.build(os.path.join(d, "dist"))
            subprocess.run([sys.executable, "-m", "venv", os.path.join(d, "v")], check=True)
            py = os.path.join(d, "v", "bin", "python")
            subprocess.run([py, "-m", "pip", "install", "-q", "--no-index", whl], check=True)
            out = subprocess.run([py, "-I", "-c", "import drums; print(drums.__file__)"], capture_output=True, text=True, check=True, cwd=d).stdout
            self.assertIn(os.path.realpath(os.path.join(d, "v")), os.path.realpath(out.strip()))


if __name__ == "__main__":
    unittest.main()
