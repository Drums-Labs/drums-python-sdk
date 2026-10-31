"""drums_osw.py: setup steps that run sudo go to drumsd's provisioning endpoint (the desktop user has no sudo), and
upload_file reads only files under the assets directory. Runs without OSWorld (a stand-in setup controller)."""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("drums_osw_p", os.path.join(HERE, "..", "..", "osw", "drums_osw.py"))
osw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osw)


class Ctrl:
    """OSWorld's SetupController as far as these patches see it: _execute_setup posts to self.http_server."""
    def __init__(self):
        self.http_server = "http://127.0.0.1:25101"
        self.sent, self.uploaded = [], []

    def _execute_setup(self, command, stdout="", stderr="", shell=False, until=None):
        self.sent.append((self.http_server, command, shell))

    def _command_setup(self, command, **kw):
        self._execute_setup(command, **kw)

    def _upload_file_setup(self, files):
        self.uploaded.extend(files)


class RootStepsTests(unittest.TestCase):
    def test_which_commands_need_root(self):
        for c in ("echo {CLIENT_PASSWORD} | sudo -S mkdir ~/Desktop/todo_list_Jan_1",
                  ["bash", "-c", "echo '{CLIENT_PASSWORD}' | sudo -S timedatectl set-timezone Asia/Tokyo"],
                  ["/usr/bin/sudo", "true"], "sudo chown -R user:user /home/user/.config/google-chrome/Default/History"):
            self.assertTrue(osw.needs_root(c), c)
        for c in (["python3", "/home/user/acme/acme_app.py"], "echo pseudo-sudoku > x", "", None, []):
            self.assertFalse(osw.needs_root(c), c)

    def test_sudo_steps_go_to_provisioning_and_every_other_step_to_the_desktop(self):
        c = Ctrl()
        osw.route_root_steps(c, "http://127.0.0.1:7070/provision/tok")
        c._execute_setup("echo password | sudo -S mkdir /home/user/Desktop/x", shell=True)
        c._command_setup(["python3", "-c", "print(1)"])
        c._execute_setup(["sudo", "chown", "user", "/x"])
        self.assertEqual([s[0] for s in c.sent], ["http://127.0.0.1:7070/provision/tok", "http://127.0.0.1:25101",
                                                  "http://127.0.0.1:7070/provision/tok"])
        self.assertEqual(c.http_server, "http://127.0.0.1:25101")           # restored after each provisioning step

    def test_without_a_provisioning_url_a_sudo_step_goes_to_the_desktop_where_sudo_refuses(self):
        c = Ctrl()
        osw.route_root_steps(c, None)
        c._execute_setup("echo password | sudo -S true", shell=True)
        self.assertEqual(c.sent[0][0], "http://127.0.0.1:25101")


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.assets = os.path.join(self.root, "assets")
        os.makedirs(os.path.join(self.assets, "acme", "app"))
        self.app = os.path.join(self.assets, "acme", "app", "acme_app.py")
        open(self.app, "w").write("print(1)\n")
        self.secret = os.path.join(self.root, "drumsd.json")
        open(self.secret, "w").write("{}")

    def upload(self, local):
        c = Ctrl()
        osw.restrict_uploads(c, self.assets)
        c._upload_file_setup([{"local_path": local, "path": "/home/user/acme/acme_app.py"}])
        return c.uploaded

    def test_a_file_under_the_assets_directory_is_uploaded(self):
        self.assertEqual(self.upload(self.app), [{"local_path": os.path.realpath(self.app), "path": "/home/user/acme/acme_app.py"}])

    def test_any_other_host_file_is_refused(self):
        for p in (self.secret, "/etc/passwd", os.path.join(self.assets, "..", "drumsd.json"), self.assets + "-other/x"):
            with self.assertRaises(PermissionError, msg=p):
                self.upload(p)

    def test_a_symlink_out_of_the_assets_directory_is_refused(self):
        link = os.path.join(self.assets, "acme", "passwd")
        os.symlink(self.secret, link)
        with self.assertRaises(PermissionError):
            self.upload(link)

    def test_the_directory_itself_or_a_missing_file_is_refused(self):
        with self.assertRaises(PermissionError):
            self.upload(self.assets)
        with self.assertRaises(FileNotFoundError):
            self.upload(os.path.join(self.assets, "acme"))
        with self.assertRaises(FileNotFoundError):
            self.upload(os.path.join(self.assets, "missing.py"))
        with self.assertRaises(ValueError):
            self.upload("")


if __name__ == "__main__":
    unittest.main()
