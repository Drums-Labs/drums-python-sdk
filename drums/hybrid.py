"""Hybrid actions: GUI, shell, files and app APIs in one desktop.

Every tool runs in the same env (the same desktop, the same user, the same filesystem), so what one kind of action
does is visible to the others: a file Chrome downloads to ~/Downloads is there for `shell` and `read_file` at once, and
a file `write_file` puts on the Desktop opens in LibreOffice. All tools act as the desktop user (no root), with the
env's loopback-only network.

    import drums
    from drums.hybrid import HybridTools
    env = drums.connect().create(task=...)
    t = HybridTools(env)
    t.gui("pyautogui.click(960, 540)")                 # GUI (pyautogui), same as env.step
    t.shell("ls -l ~/Downloads")                       # {"returncode", "stdout", "stderr"}
    t.write_file("~/Desktop/notes.txt", "hello\\n")
    t.read_file("~/Downloads/report.csv")              # {"exists", "size", "text" or "base64"}
    t.list_dir("~/Documents")
    t.office_convert("~/Documents/a.docx", "pdf")      # LibreOffice (headless, its own profile)
    t.spreadsheet_read("~/Documents/sales.xlsx")       # cells of the first sheet, read by LibreOffice
    t.browser_tabs(); t.browser_open("http://127.0.0.1:8000/")   # Chrome's DevTools endpoint (when Chrome runs with it)

`TOOLS` is the JSON schema of every tool (OpenAI / MCP style); `call(name, args)` dispatches one call, so a trainer
adapter or the MCP server (`python -m drums.mcp`) exposes the same set. Standard library only.

For RL: a policy with `shell` can solve many GUI tasks without the GUI. That is the point of hybrid actions (UI-TARS-2
and ComputerRL train this way), but a GUI-only study must not expose these tools.
"""
from __future__ import annotations

import base64
import json
import posixpath

HOME = "/home/user"
MAX_READ = 2 * 1024 * 1024
CHUNK = 8_000  # one argv chunk: the gateway caps each step command argument at 8192 chars (contracts drumsActionSchema)
SOFFICE = ["soffice", "-env:UserInstallation=file:///tmp/drums-hybrid-lo", "--headless", "--norestore"]


def _path(p: str) -> str:
    p = str(p)
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    if not p.startswith("/"):
        p = posixpath.join(HOME, p)
    return posixpath.normpath(p)


_READ = ("import base64,json,os,sys\np=sys.argv[1];m=int(sys.argv[2])\n"
         "if not os.path.exists(p):\n print(json.dumps({'exists':False}));sys.exit(0)\n"
         "if os.path.isdir(p):\n print(json.dumps({'exists':True,'dir':True}));sys.exit(0)\n"
         "n=os.path.getsize(p);d=open(p,'rb').read(m)\n"
         "try:\n t=d.decode('utf-8');print(json.dumps({'exists':True,'size':n,'truncated':n>m,'text':t}))\n"
         "except UnicodeDecodeError:\n print(json.dumps({'exists':True,'size':n,'truncated':n>m,'base64':base64.b64encode(d).decode()}))\n")
_WRITE = ("import base64,os,sys\np=sys.argv[1];os.makedirs(os.path.dirname(p) or '.',exist_ok=True)\n"
          "open(p,sys.argv[2]).write(base64.b64decode(sys.argv[3]))\n")
_LIST = ("import json,os,sys\np=sys.argv[1]\ntry:\n e=sorted(os.scandir(p),key=lambda x:x.name)\n"
         "except OSError as x:\n print(json.dumps({'error':type(x).__name__}));sys.exit(0)\n"
         "print(json.dumps([{'name':x.name,'dir':x.is_dir(),'size':(x.stat().st_size if x.is_file() else None)} for x in e]))\n")
_CDP = ("import json,sys,urllib.request\nu='http://127.0.0.1:%s'%sys.argv[1]\nop=sys.argv[2]\n"
        "try:\n if op=='list':\n  d=json.load(urllib.request.urlopen(u+'/json/list',timeout=10))\n"
        "  print(json.dumps([{'url':x.get('url'),'title':x.get('title')} for x in d if x.get('type')=='page']))\n"
        " else:\n  r=urllib.request.Request(u+'/json/new?'+sys.argv[3],method='PUT')\n"
        "  d=json.load(urllib.request.urlopen(r,timeout=10));print(json.dumps({'url':d.get('url')}))\n"
        "except Exception as e:\n print(json.dumps({'error':type(e).__name__+': '+str(e)[:200]}))\n")
_SHEET = ("import csv,glob,os,subprocess,sys,tempfile,json\np=sys.argv[1];soff=json.loads(sys.argv[2])\n"
          "d=tempfile.mkdtemp(prefix='drums-sheet-')\n"
          "r=subprocess.run(soff+['--convert-to','csv','--outdir',d,p],capture_output=True,text=True,timeout=120)\n"
          "f=glob.glob(d+'/*.csv')\n"
          "if not f:\n print(json.dumps({'error':'conversion failed: '+(r.stderr or r.stdout)[-300:]}));sys.exit(0)\n"
          "rows=list(csv.reader(open(f[0],encoding='utf-8',errors='replace')))\nprint(json.dumps({'rows':rows[:500],'truncated':len(rows)>500}))\n")

TOOLS = [
    {"name": "gui", "description": "Run a GUI action in the desktop: Python using pyautogui (e.g. pyautogui.click(100, 200), "
     "pyautogui.write('text'), pyautogui.hotkey('ctrl', 's')). Screen is 1920x1080 pixels.",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "screenshot", "description": "Take a screenshot of the desktop (PNG).", "parameters": {"type": "object", "properties": {}}},
    {"name": "shell", "description": "Run a bash command in the desktop as the desktop user (no root, no internet). Returns returncode, stdout, stderr.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_s": {"type": "number"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a file in the desktop (~ is /home/user). Text comes back as text, binary as base64.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a text file in the desktop (creates folders; overwrites).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "list_dir", "description": "List a folder in the desktop: name, dir, size.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "office_convert", "description": "Convert a document with LibreOffice (e.g. docx->pdf, xlsx->csv, csv->xlsx). Writes next to the source unless outdir is given.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "format": {"type": "string"}, "outdir": {"type": "string"}},
                    "required": ["path", "format"]}},
    {"name": "spreadsheet_read", "description": "Read the first sheet of a spreadsheet (xlsx, ods, csv) as rows of text, through LibreOffice.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "browser_tabs", "description": "List the open Chrome tabs (url, title). Needs Chrome started with --remote-debugging-port.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "browser_open", "description": "Open a URL in a new Chrome tab.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


class HybridTools:
    def __init__(self, env, cdp_port: int = 1337):
        self.env, self.cdp_port = env, cdp_port

    def _exec(self, cmd: list, timeout_s=None) -> dict:
        r = self.env.step({"type": "execute", "command": cmd, "shell": False}, check=False)
        res = r.result or {}
        return {"returncode": res.get("returncode"), "stdout": res.get("output") or "", "stderr": res.get("error") or ""}

    def _py(self, code: str, *args) -> dict:
        r = self._exec(["python3", "-I", "-c", code, *[str(a) for a in args]])
        try:
            return json.loads(r["stdout"].strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"returncode {r['returncode']}: {r['stderr'][-300:]}"}

    def gui(self, code: str) -> dict:
        r = self.env.step({"type": "pyautogui", "code": code}, check=False)
        res = r.result or {}
        return {"returncode": res.get("returncode"), "error": res.get("error") or ""}

    def screenshot(self) -> bytes:
        return self.env.screenshot()

    def shell(self, command: str, timeout_s: float = 60) -> dict:
        t = max(1, min(int(timeout_s or 60), 120))
        return self._exec(["timeout", str(t), "bash", "-c", command])

    def read_file(self, path: str) -> dict:
        return self._py(_READ, _path(path), MAX_READ)

    def write_file(self, path: str, content, binary: bool = False) -> dict:
        data = content if isinstance(content, (bytes, bytearray)) else str(content).encode()
        b64 = base64.b64encode(bytes(data)).decode()
        p = _path(path)
        for i in range(0, max(1, len(b64)), CHUNK):
            r = self._exec(["python3", "-I", "-c", _WRITE, p, "wb" if i == 0 else "ab", b64[i:i + CHUNK]])
            if r["returncode"] != 0:
                return {"ok": False, "error": r["stderr"][-300:]}
        return {"ok": True, "path": p, "bytes": len(data)}

    def list_dir(self, path: str) -> dict:
        out = self._py(_LIST, _path(path))
        return {"entries": out} if isinstance(out, list) else out

    def office_convert(self, path: str, format: str, outdir: str | None = None) -> dict:  # noqa: A002
        p = _path(path)
        od = _path(outdir) if outdir else posixpath.dirname(p)
        r = self._exec(["timeout", "120", *SOFFICE, "--convert-to", format, "--outdir", od, p])
        stem = posixpath.splitext(posixpath.basename(p))[0]
        out = posixpath.join(od, f"{stem}.{format.split(':')[0]}")
        ok = self._py(_READ, out, 0).get("exists", False)
        return {"ok": bool(ok), "output": out, "returncode": r["returncode"], "log": (r["stdout"] + r["stderr"])[-400:]}

    def spreadsheet_read(self, path: str) -> dict:
        return self._py(_SHEET, _path(path), json.dumps(SOFFICE))

    def browser_tabs(self) -> dict:
        out = self._py(_CDP, self.cdp_port, "list", "")
        return {"tabs": out} if isinstance(out, list) else out

    def browser_open(self, url: str) -> dict:
        from urllib.parse import quote
        return self._py(_CDP, self.cdp_port, "new", quote(url, safe=":/?&=#%"))

    def call(self, name: str, args: dict | None = None):
        """Dispatch one tool call by name (the TOOLS schema). Returns a JSON-able dict, or PNG bytes for screenshot."""
        args = dict(args or {})
        if name not in {t["name"] for t in TOOLS}:
            raise ValueError(f"unknown tool {name!r}")
        return getattr(self, name)(**args)
