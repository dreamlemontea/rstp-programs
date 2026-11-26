# Self-update of "Улучшалка" and "Скачивалка" (the same file sits in both program folders; the source is общее/updater.py).
# The new versions are on GitHub: github.com/dreamlemontea/rstp-programs (put there by обновление/выложить.sh).
# manifest.json there says the version, its tag and the SHA-256 of every file. Only the files that differ are
# downloaded (each checked against its SHA-256) and put in place; both programs are updated together, since
# they share one Python. Missing libraries from manifest "pip" are installed. Then the program offers «Перезапустить» (relaunch) and
# shows «Что нового» once (changes).
import hashlib, json, os, subprocess, sys, threading, urllib.parse, urllib.request

REPO = "dreamlemontea/rstp-programs"
RAW = f"https://raw.githubusercontent.com/{REPO}"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # the folder that holds Улучшалка and Скачивалка (Application Support / %LOCALAPPDATA%)
WIN = sys.platform == "win32"
NOWIN = {"creationflags": 0x08000000} if WIN else {}
_lock = threading.Lock()
_cache = {}


def current():
    try:
        return open(os.path.join(HERE, "version.txt"), encoding="utf-8").read().strip()
    except OSError:
        return "0"


def _num(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _get(url, dest=None, timeout=60):
    """The Mac's own Python has no certificates of its own, so there curl does the downloading."""
    if not WIN:
        cmd = ["curl", "-fsSL", "--retry", "3", "--max-time", str(timeout * 5), url] + (["-o", dest] if dest else [])
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=timeout * 5 + 10).stdout
        return None if dest else out
    req = urllib.request.Request(url, headers={"User-Agent": "rstp-programs"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if dest:
        open(dest, "wb").write(data)
        return None
    return data


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def manifest():
    return json.loads(_get(f"{RAW}/main/manifest.json", timeout=20))


def check():
    """{"version": this one, "latest": on GitHub ('' without internet), "new": bool, "notes": text}"""
    cur = current()
    try:
        m = manifest()
        _cache["m"] = m
        lat = m["version"]
    except Exception as e:  # noqa: BLE001
        print("update check failed:", e, flush=True)
        return {"version": cur, "latest": "", "new": False, "notes": ""}
    return {"version": cur, "latest": lat, "new": _num(lat) > _num(cur), "notes": m.get("notes", "")}


def _python():
    """python.exe rather than pythonw.exe for pip on Windows."""
    p = sys.executable
    if WIN and p.lower().endswith("pythonw.exe"):
        q = p[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(q):
            return q
    return p


def run():
    """Updates both programs to the version on GitHub. Returns (ok, message)."""
    if not _lock.acquire(blocking=False):
        return False, "Уже обновляю"
    try:
        m = manifest()
        if _num(m["version"]) <= _num(current()):
            return True, f"Программа уже самая свежая ({current()})"
        tag = m["tag"]
        todo = []
        for rel, want in m["files"].items():
            top = rel.split("/")[0]
            if not os.path.isdir(os.path.join(ROOT, top)):
                continue  # that program isn't installed here
            dst = os.path.join(ROOT, *rel.split("/"))
            if not (os.path.isfile(dst) and _sha(dst) == want):
                todo.append((rel, dst, want))
        print(f"update to {m['version']}: {len(todo)} files", flush=True)
        # download everything first, put in place only when all of it is fine
        got = []
        for rel, dst, want in todo:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".new"
            _get(f"{RAW}/{tag}/{urllib.parse.quote(rel)}", tmp, timeout=300)
            if _sha(tmp) != want:
                raise ValueError(f"checksum: {rel}")
            got.append((tmp, dst))
        for tmp, dst in got:
            os.replace(tmp, dst)
        # new libraries, if the new version needs them
        # only inside the programs' own .venv (never into a system Python); a failure here doesn't undo the update
        for p in m.get("pip", []) if sys.prefix != sys.base_prefix else []:
            try:
                __import__(p["import"])
            except ImportError:
                r = subprocess.run([_python(), "-m", "pip", "install", "-q", "--prefer-binary", p["pkg"]], capture_output=True, text=True, timeout=1800, **NOWIN)
                if r.returncode:
                    print("pip failed:", p["pkg"], r.stderr[-500:], flush=True)
        for top in {rel.split("/")[0] for rel in m["files"]}:
            if os.path.isdir(os.path.join(ROOT, top)):
                open(os.path.join(ROOT, top, "version.txt"), "w", encoding="utf-8").write(m["version"] + "\n")
        _cache["updated"] = m["version"]  # the window offers «Перезапустить»
        return True, f"Обновлено до версии {m['version']}"
    except Exception as e:  # noqa: BLE001
        print("update failed:", e, flush=True)
        for rel, dst, want in locals().get("todo", []):
            try:
                os.remove(dst + ".new")
            except OSError:
                pass
        return False, "Не получилось обновить программу. Проверь интернет и попробуй ещё раз"
    finally:
        _lock.release()


def updated():
    """The version just put in place by run() (the running program is still the old one), or ''."""
    return _cache.get("updated", "")


def changes():
    """What's new in this version (changes.json, written by hand in общее/что нового.json) — shown once after an update.
    seen.txt remembers the version whose news were already shown; a fresh install has it from the installer."""
    cur = current()
    try:
        seen = open(os.path.join(HERE, "seen.txt"), encoding="utf-8").read().strip()
    except OSError:
        seen = ""
    if seen == cur:
        return {"version": cur, "items": []}
    try:
        items = json.load(open(os.path.join(HERE, "changes.json"), encoding="utf-8")).get(cur, [])
    except (OSError, ValueError):
        items = []
    return {"version": cur, "items": items}


def seen():
    open(os.path.join(HERE, "seen.txt"), "w", encoding="utf-8").write(current() + "\n")


def _app_bundle():
    """Mac: the .app of this program (the gift installer's or EATSIST's own build), found by its id."""
    ids = {"Улучшалка": ("ru.reliznaya.uluchshalka", "studio.rstp.uluchshalka"),
           "Скачивалка": ("ru.reliznaya.skachivalka", "studio.rstp.skachivalka")}.get(os.path.basename(HERE), ())
    try:
        import AppKit

        ws = AppKit.NSWorkspace.sharedWorkspace()
        for i in ids:
            u = ws.URLForApplicationWithBundleIdentifier_(i)
            if u is not None and os.path.isdir(u.path()):
                return u.path()
    except Exception as e:  # noqa: BLE001
        print("app not found:", e, flush=True)
    return ""


def relaunch():
    """Starts the program again (the new version) once this process is gone. The caller then exits."""
    app = "" if WIN else _app_bundle()
    if app:  # through its .app, so the Dock and the menu are right; waits until this process is closed
        subprocess.Popen(["/bin/sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; open "$2"', "sh", str(os.getpid()), app],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    py = os.environ.get("__PYVENV_LAUNCHER__") or sys.executable  # the venv's python (the Mac's real Python.app can't be started directly)
    args = [py, os.path.join(HERE, "server.py")] + [a for a in sys.argv[1:] if a != "--restarted"] + ["--restarted"]
    if WIN:  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen(args, cwd=HERE, creationflags=0x00000008 | 0x00000200 | 0x08000000, close_fds=True)
    else:
        subprocess.Popen(args, cwd=HERE, start_new_session=True)
