# "Скачивалка": paste a link -> a video (or only its sound) lands in Downloads.
# A page on localhost in its own window; the work is done by yt-dlp + ffmpeg kept next to this file (bin/).
import glob, hashlib, json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid, webbrowser, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import updater  # the program's own updates from GitHub (updater.py next to this file)

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = "https://rstpstudio.ru"  # "by RSTP Studio" in the window opens it
WIN = sys.platform == "win32"
if sys.stdout is None:  # Windows, started without a console (pythonw): the messages go to a file
    sys.stdout = sys.stderr = open(os.path.join(HERE, "log.txt"), "w", encoding="utf-8", buffering=1)
NOWIN = {"creationflags": 0x08000000} if WIN else {}  # Windows: no black console windows for yt-dlp
BIN = os.path.join(HERE, "bin")
# the unpacked official build (yt-dlp_macos.zip / yt-dlp_win.zip) starts in a fraction of a second;
# the one-file build takes ~17 s every time
APPDIR = os.path.join(BIN, "yt-dlp-app")
ZIP = "yt-dlp_win.zip" if WIN else "yt-dlp_macos.zip"


def find_ytdlp():
    if WIN:
        found = sorted(glob.glob(os.path.join(APPDIR, "yt-dlp*.exe"))) + glob.glob(os.path.join(BIN, "yt-dlp.exe"))
        return found[0] if found else os.path.join(APPDIR, "yt-dlp.exe")
    p = os.path.join(APPDIR, "yt-dlp_macos")
    if not os.path.exists(p) and os.path.exists(os.path.join(BIN, "yt-dlp")):
        p = os.path.join(BIN, "yt-dlp")
    return p


YTDLP = find_ytdlp()
REL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
TMP = os.path.join(HERE, "tmp")
OUT = os.path.expanduser("~/Downloads")
ENV = dict(os.environ, LANG="en_US.UTF-8", PYTHONIOENCODING="utf-8")
jobs, jlock = {}, threading.Lock()


# --- the folder for the results: asked on the first start, changed with ⚙ (kept in settings.json next to the program) ---
SETTINGS = os.path.join(HERE, "settings.json")
NAMES = {"Pictures": "Изображения", "Downloads": "Загрузки", "Desktop": "Рабочий стол", "Documents": "Документы",
         "Movies": "Фильмы", "Videos": "Видео", "Music": "Музыка"}


def settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def out_dir():
    """The chosen folder; while none is chosen (or it is gone: deleted, a disk unplugged) — the usual one."""
    p = settings().get("out")
    return p if p and os.path.isdir(p) else OUT


def folder_info():
    p = out_dir()
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        parts = [x for x in os.path.relpath(p, home).split(os.sep) if x not in ("", ".")]
        pretty = " › ".join([NAMES.get(parts[0], parts[0])] + parts[1:]) if parts else "Домашняя папка"
    else:
        pretty = p
    s = settings().get("out")
    return {"path": p, "pretty": pretty, "asked": bool(s and os.path.isdir(s))}


def set_folder(p):
    os.makedirs(p, exist_ok=True)
    s = settings()
    s["out"] = p
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def pick_folder():
    """The usual folder dialog of the system. Returns the folder or '' (cancelled)."""
    start = out_dir() if os.path.isdir(out_dir()) else os.path.expanduser("~")
    try:
        import webview

        if webview.windows:  # our own window: its dialog
            r = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER, directory=start)
            return (r[0] if isinstance(r, (list, tuple)) else r or "") if r else ""
    except Exception as e:  # noqa: BLE001
        print("folder dialog:", e, flush=True)
    if WIN:  # opened in the browser: the dialog through PowerShell
        ps = ("Add-Type -AssemblyName System.Windows.Forms;$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.SelectedPath='{start}';if($d.ShowDialog() -eq 'OK'){{[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.SelectedPath}}")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True, encoding="utf-8", **NOWIN)
    else:
        r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "Куда сохранять готовое?" default location (POSIX file "{start}"))'],
                           capture_output=True, text=True)
    return r.stdout.strip().rstrip("/\\") if r.returncode == 0 else ""


def folder_route(path):
    """/folder — what is chosen; /folder/pick — choose another; /folder/keep — keep the one shown."""
    if path == "/folder/pick":
        p = pick_folder()
        if p:
            set_folder(p)
    elif path == "/folder/keep":
        set_folder(out_dir())
    return json.dumps(folder_info(), ensure_ascii=False).encode()

ready = threading.Event()  # cleared while yt-dlp is being replaced: downloads wait for it
ready.set()


def node():
    """YouTube needs a JavaScript engine for full quality: Node on the Mac, Deno (in bin/) on Windows."""
    if WIN:
        for kind, p in (("deno", os.path.join(BIN, "deno.exe")), ("deno", shutil.which("deno")), ("node", shutil.which("node"))):
            if p and os.path.isfile(p):
                return kind, p
        return None
    for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node", shutil.which("node") or ""):
        if p and os.access(p, os.X_OK):
            return "node", p
    if os.access(os.path.join(BIN, "deno"), os.X_OK):  # a Mac without Node: Deno put next to yt-dlp by the installer
        return "deno", os.path.join(BIN, "deno")
    return None


def base_args():
    ready.wait(600)
    a = [YTDLP, "--no-playlist", "--newline", "--no-colors", "--ffmpeg-location", BIN, "--no-mtime"]
    if WIN:
        a += ["--encoding", "utf-8"]  # Russian titles come through intact
    n = node()
    if n:
        a += ["--js-runtimes", n[0] + ":" + n[1]]
    return a


def kill(p):
    """Stops a download; on Windows together with its ffmpeg."""
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, **NOWIN)
    else:
        p.terminate()


def reveal(path, select=False):
    """A folder in Finder / Explorer; select=True: the folder with this file selected."""
    if WIN:
        subprocess.run(f'explorer /select,"{path}"' if select else f'explorer "{path}"')
    else:
        subprocess.run(["open", "-R", path] if select else ["open", path])


def clipboard():
    if not WIN:
        return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    import ctypes as c

    u, k = c.windll.user32, c.windll.kernel32
    u.GetClipboardData.restype = k.GlobalLock.restype = c.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [c.c_void_p]
    if not u.OpenClipboard(None):
        return ""
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        p = k.GlobalLock(h) if h else None
        if not p:
            return ""
        try:
            return c.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def dark_os():
    if WIN:
        try:
            import winreg

            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            return True
    return subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True).stdout.strip() == "Dark"


def win_icon(title, ico):
    """Windows: our icon on the window and in the taskbar instead of Python's."""
    import ctypes as c

    u = c.windll.user32
    u.FindWindowW.argtypes, u.FindWindowW.restype = [c.c_wchar_p, c.c_wchar_p], c.c_void_p
    u.LoadImageW.argtypes, u.LoadImageW.restype = [c.c_void_p, c.c_wchar_p, c.c_uint, c.c_int, c.c_int, c.c_uint], c.c_void_p
    u.SendMessageW.argtypes = [c.c_void_p, c.c_uint, c.c_void_p, c.c_void_p]
    hwnd = u.FindWindowW(None, title)
    for which, metric in ((1, 11), (0, 49)):  # ICON_BIG / SM_CXICON, ICON_SMALL / SM_CXSMICON
        n = u.GetSystemMetrics(metric)
        h = u.LoadImageW(None, ico, 1, n, n, 0x10)
        if hwnd and h:
            u.SendMessageW(hwnd, 0x80, which, h)


def secs(t):
    """'1:05', '65', '1:02:03', '1.05' -> seconds; '' -> None."""
    t = (t or "").strip().replace(",", ".").replace(" ", "")
    if not t:
        return None
    parts = re.split(r"[:.]", t) if ":" in t or re.fullmatch(r"\d+\.\d\d", t) else [t]
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) or len(parts) > 3:
        raise ValueError("Не понял время. Пиши так: 1:05 или 65")
    v = 0.0
    for p in parts:
        v = v * 60 + float(p)
    return v


def tc(s):
    s = int(s)
    return f"{s // 3600}.{s % 3600 // 60:02d}.{s % 60:02d}" if s >= 3600 else f"{s // 60}.{s % 60:02d}"


def nice(err):
    e = err or ""
    rules = [
        (r"vk\.(com|ru)/(audio|music)|/audio-?\d", "Музыку из ВК скачать нельзя — ВК её защищает. Видео и клипы из ВК скачиваются"),
        (r"Unsupported URL|is not a valid URL|No video formats", "Это не похоже на ссылку на видео. Скопируй ссылку из адресной строки"),
        (r"Private video", "Видео закрыто (приватное)"),
        (r"Video unavailable|video is unavailable|This video is not available|has been removed|account .* terminated", "Видео недоступно или удалено"),
        (r"confirm your age|age-restricted|inappropriate", "Видео с ограничением по возрасту — без входа в аккаунт не скачать"),
        (r"not a bot|Sign in to confirm", "YouTube просит подтвердить, что ты не робот. Подожди немного или нажми «Обновить»"),
        (r"members-only|Join this channel", "Видео только для спонсоров канала"),
        (r"live event will begin|Premieres in|is_live|live stream", "Это трансляция или премьера — скачай, когда она закончится"),
        (r"getaddrinfo|Unable to download webpage|Connection|timed out|Network is unreachable|nodename nor servname", "Нет интернета или YouTube не отвечает. Проверь сеть и попробуй ещё раз"),
        (r"HTTP Error 403|Requested format is not available|Signature|nsig|player", "YouTube что-то поменял. Нажми «Обновить» и попробуй ещё раз"),
        (r"No space left", "Закончилось место на диске"),
        (r"ffmpeg|Postprocessing|Conversion failed", "Скачалось, но не получилось собрать файл. Попробуй ещё раз или без куска по времени"),
    ]
    for pat, msg in rules:
        if re.search(pat, e, re.I):
            return msg
    return "Не получилось скачать. Попробуй ещё раз или нажми «Обновить»"


def est_sizes(i):
    """Rough file size for each choice (same order of picking as the download: size, fps, H.264 first, https first)."""
    d = i.get("duration") or 0
    fs = i.get("formats") or []
    def size(f):
        b = f.get("filesize") or f.get("filesize_approx")
        if not b and f.get("tbr") and d:
            b = f["tbr"] * 1000 / 8 * d
        return b or 0
    def crank(c):
        c = (c or "").lower()
        return 0 if c.startswith(("avc", "h264")) else 1 if c.startswith(("hev", "hvc", "h265")) else 2 if c.startswith("vp") else 3
    vids = [f for f in fs if f.get("vcodec") not in (None, "none") and f.get("width") and f.get("height")]
    auds = [f for f in fs if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    a = max(auds, key=lambda f: (str(f.get("acodec", "")).startswith("mp4a"), f.get("protocol") == "https", f.get("abr") or 0), default=None)
    ab = size(a) if a else 0
    out = {}
    for q in (0, 4320, 2160, 1440, 1080, 720, 480, 360, 240):
        c = [f for f in vids if not q or min(f["width"], f["height"]) <= q * 1.1] or vids
        if not c:
            continue
        v = max(c, key=lambda f: (min(f["width"], f["height"]), f.get("fps") or 0, -crank(f.get("vcodec")),
                                  f.get("protocol") == "https", size(f)))
        b = size(v) + (ab if v.get("acodec") in (None, "none") else 0)
        if b:
            out[str(q)] = int(b)
    if d:
        out["wav"] = int(d * 44100 * 4)
        out["mp3"] = int(d * 40000)
    return out


def run_job(j):
    a = base_args() + ["-P", out_dir(), "-P", "temp:" + TMP, "--progress",
                       "--progress-template", "download:PROG %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
                       "--print", "before_dl:TITLE %(title)s", "--print", "after_move:FILE %(filepath)s",
                       # YouTube sometimes chokes one connection to a few KB/s — then yt-dlp reconnects by itself
                       "--throttled-rate", "100K"]
    name = "%(title).150B"
    if j["start"] is not None or j["end"] is not None:
        s, e = j["start"] or 0, j["end"]
        # "-f mp4": our ffmpeg can't write .m4a ("ipod"), mp4 is the same thing inside
        a += ["--download-sections", f"*{s}-{e if e is not None else 'inf'}", "--force-keyframes-at-cuts",
              "--downloader-args", "ffmpeg_o:-f mp4"]
        name += f" ({tc(s)}–{tc(e) if e is not None else 'конец'})"
    if j["mode"] == "audio":
        # a piece: the plain https stream — from HLS (VK) ffmpeg can't cut out the middle, the piece comes out empty
        cut = j["start"] is not None or j["end"] is not None
        a += ["-f", "ba[acodec^=mp4a][protocol=https]/ba[ext=webm]/ba" if cut else "ba[acodec^=mp4a]/ba/b",
              "-x", "--audio-format", j["fmt"]]
        if j["fmt"] == "mp3":
            a += ["--audio-quality", "320K"]
        j["parts"] = 1
    else:
        # highest resolution first; at the same size H.264 (opens everywhere, CapCut too); stereo sound
        # with a chosen size: that size or the nearest smaller one
        # "<=?": VK doesn't say how many channels; https sound first (a piece from VK's HLS sound is silent)
        a += ["-f", "bv*+ba[acodec^=mp4a][audio_channels<=?2][protocol=https]/bv*+ba[acodec^=mp4a][audio_channels<=?2]/bv*+ba/b", "-S", f"res:{j['q']},fps,vcodec:h264" if j["q"] else "res,fps,vcodec:h264",
              "--merge-output-format", "mp4", "--remux-video", "mp4"]
        j["parts"] = 2
        if j["q"]:
            name += " " + {2160: "4K", 1440: "2K"}.get(j["q"], f"{j['q']}p")
    a += ["-o", name + ".%(ext)s", j["url"]]
    os.makedirs(TMP, exist_ok=True)
    try:
        p = subprocess.Popen(a, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV, bufsize=1,
                             encoding="utf-8", errors="replace", **NOWIN)
    except OSError as e:
        j.update(state="err", log=str(e), error="Не найден yt-dlp. Нажми «Обновить»" if WIN
                 else "Не найден yt-dlp. Пересобери программу (значок/build_app.sh)")
        return
    j["proc"], log, part = p, [], 0
    for line in p.stdout:
        line = line.rstrip()
        log.append(line)
        del log[:-60]
        if line.startswith("TITLE "):
            j["title"] = line[6:]
        elif line.startswith("FILE "):
            j["file"] = line[5:]
        elif line.startswith("[download] Destination") or (line.startswith("[download]") and "already been downloaded" in line):
            part += 1
        elif line.startswith("PROG "):
            pct, sp, eta = (line[5:].split("|") + ["", ""])[:3]
            try:
                v = float(pct.strip().rstrip("%"))
            except ValueError:
                continue
            k = max(part, 1)
            lo, hi = ((0, 80), (80, 95))[min(k, 2) - 1] if j["parts"] == 2 else (0, 92)
            j.update(pct=round(lo + (hi - lo) * v / 100, 1), speed=sp.strip(), eta=eta.strip(), stage="download")
        elif re.match(r"\[(Merger|ExtractAudio|VideoRemuxer|FixupM3u8|FixupM4a|ModifyChapters|VideoConvertor)\]", line):
            j.update(stage="build", pct=max(j.get("pct") or 0, 96))
        elif line.startswith("[download]") and "section" in line.lower():
            j["stage"] = "section"
    p.wait()
    if j.get("cancel"):
        j["state"] = "cancel"
    elif p.returncode == 0 and j.get("file") and os.path.exists(j["file"]):
        j.update(state="done", pct=100, size=os.path.getsize(j["file"]))
    else:
        errs = "\n".join(l for l in log if "ERROR" in l) or "\n".join(log[-8:])
        j.update(state="err", error=nice(errs), log="\n".join(log[-20:]))
        print("\n".join(log[-20:]), flush=True)
    j.pop("proc", None)


def public(j):
    return {k: v for k, v in j.items() if k not in ("proc", "url")}


def version():
    try:
        return subprocess.run([YTDLP, "--version"], capture_output=True, text=True, env=ENV, timeout=180, **NOWIN).stdout.strip()
    except Exception:  # noqa: BLE001
        return "" if os.access(YTDLP, os.X_OK) else "не найден"


def fetch(url, dest=None, timeout=600):
    """Downloads url (to the file dest, or returns the text). Mac: curl; Windows: Python itself."""
    if not WIN:
        cmd = ["curl", "-fsSL", "--retry", "2", "--max-time", str(timeout), url] + (["-o", dest] if dest else [])
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout + 10).stdout
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 skachivalka"})
    with urllib.request.urlopen(req, timeout=min(timeout, 60)) as r:
        if not dest:
            return r.read().decode("utf-8")
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)


def latest_tag():
    """The newest yt-dlp version on its official page (like '2026.10.26'), or '' without internet."""
    try:
        return json.loads(fetch("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest", timeout=20))["tag_name"]
    except Exception:  # noqa: BLE001
        return ""


def update():
    """Fetches the newest official yt-dlp_macos.zip / yt-dlp_win.zip, checks its checksum and puts it in place of the old one."""
    with ulock:
        ready.clear()
        try:
            return _update()
        finally:
            ready.set()


def auto_update():
    """On start: a newer yt-dlp is put in by itself (YouTube changes often), quietly. Without internet — nothing."""
    try:
        latest = latest_tag()
        if latest and (latest != version() or not YTDLP.startswith(APPDIR)):
            auto["on"] = True  # the window shows «Обновляю загрузчик…» on the button
            print("yt-dlp auto update:", update(), flush=True)
    except Exception as e:  # noqa: BLE001
        print("yt-dlp auto update failed:", e, flush=True)
    finally:
        auto["on"] = False
        checked.set()


ulock, checked, auto = threading.Lock(), threading.Event(), {"on": False}


def _update():
    global YTDLP
    if any(j["state"] == "work" for j in jobs.values()):
        return "Сначала дождись, пока закончатся скачивания"
    before = version()
    latest = latest_tag()
    if not latest:
        return "Не получилось проверить обновления. Проверь интернет"
    if latest == before and YTDLP.startswith(APPDIR):
        return f"Уже самая свежая версия ({before})"
    work = os.path.join(HERE, "update")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    z = os.path.join(work, ZIP)
    try:
        fetch(REL + ZIP, z)
        sums = fetch(REL + "SHA2-256SUMS", timeout=120)
        want = next(l.split()[0] for l in sums.splitlines() if l.endswith(" " + ZIP))
        h = hashlib.sha256()
        with open(z, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if want != h.hexdigest():
            raise ValueError("checksum")
        new = os.path.join(work, "yt-dlp-app")
        if WIN:
            zipfile.ZipFile(z).extractall(new)
            exe = sorted(glob.glob(os.path.join(new, "yt-dlp*.exe")))[0]
        else:
            subprocess.run(["unzip", "-q", z, "-d", new], check=True)
            exe = os.path.join(new, "yt-dlp_macos")
            os.chmod(exe, 0o755)
        if not subprocess.run([exe, "--version"], capture_output=True, timeout=180, **NOWIN).returncode == 0:
            raise ValueError("does not start")
        old = APPDIR + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(APPDIR):
            os.rename(APPDIR, old)
        os.rename(new, APPDIR)
        shutil.rmtree(old, ignore_errors=True)
        YTDLP = find_ytdlp()
    except Exception as e:  # noqa: BLE001
        print("update failed:", e, flush=True)
        return "Не получилось обновить. Проверь интернет и попробуй ещё раз"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return f"Обновлено: {version()}"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/jobs":
            return self.send([public(j) for j in jobs.values()])
        if u.path == "/paste":  # what's in the clipboard, so a copied link appears by itself
            try:
                t = clipboard().strip()
            except Exception:  # noqa: BLE001
                t = ""
            return self.send({"text": t if re.match(r"https?://\S+$", t) else ""})
        if u.path == "/info":  # title, length and picture for the pasted link
            try:
                r = subprocess.run(base_args() + ["-J", q.get("url", "")],
                                   capture_output=True, text=True, env=ENV, timeout=90, encoding="utf-8", errors="replace", **NOWIN)
                if r.returncode:
                    return self.send({"error": nice(r.stdout + r.stderr)})
                i = json.loads(r.stdout)
                # the short side of each picture size (a vertical 1080×1920 counts as 1080)
                hs = sorted({min(f["width"], f["height"]) for f in i.get("formats") or []
                             if f.get("vcodec") not in (None, "none") and f.get("width") and f.get("height")}, reverse=True)
                return self.send({"title": i.get("title") or "", "duration": i.get("duration"), "thumb": i.get("thumbnail") or "",
                                  "channel": i.get("channel") or i.get("uploader") or "", "heights": hs,
                                  "sizes": est_sizes(i)})
            except subprocess.TimeoutExpired:
                return self.send({"error": "YouTube долго не отвечает. Попробуй ещё раз"})
        if u.path.startswith("/folder"):
            return self.send(json.loads(folder_route(u.path)))
        if u.path == "/busy":  # the automatic yt-dlp update is going on
            return self.send({"on": auto["on"]})
        if u.path == "/appver":
            return self.send({"v": updater.current()})
        if u.path == "/version":
            return self.send({"v": version()})
        if u.path == "/check":  # on start: is there a newer yt-dlp? a newer version of the program itself?
            checked.wait(300)  # the automatic yt-dlp update goes first
            return self.send({"v": version(), "latest": latest_tag(), "app": updater.check()})
        if u.path == "/update":  # first the program (if a new version is out), then yt-dlp
            app = updater.check()
            if app["new"]:
                if any(j["state"] == "work" for j in jobs.values()):
                    return self.send({"v": version(), "msg": "Сначала дождись, пока закончатся скачивания", "app": app})
                ok, m = updater.run()
                if not ok:
                    return self.send({"v": version(), "msg": m, "app": app})
                return self.send({"v": version(), "msg": m + ". yt-dlp: " + update(), "app": updater.check()})
            return self.send({"v": version(), "msg": update(), "app": app})
        if u.path == "/cancel":
            j = jobs.get(q.get("id", ""))
            if j and j.get("proc"):
                j["cancel"] = True
                kill(j["proc"])
            return self.send({})
        if u.path == "/forget":
            jobs.pop(q.get("id", ""), None)
            return self.send({})
        if u.path == "/open":
            reveal(out_dir())
            return self.send({})
        if u.path == "/show":  # one file, selected in Finder / Explorer (only from Downloads)
            p = os.path.realpath(q.get("path", ""))
            if p.startswith(os.path.realpath(out_dir()) + os.sep) and os.path.exists(p):
                reveal(p, select=True)
            return self.send({})
        if u.path == "/site":  # "by RSTP Studio" was clicked: the site in the usual browser
            webbrowser.open(SITE)
            return self.send({})
        if u.path in ("/rstp-logo.png", "/rstp-logo-dark.png"):
            return self.send(open(os.path.join(HERE, "значок", u.path[1:]), "rb").read(), "image/png")
        if u.path == "/icon.png":
            return self.send(open(os.path.join(HERE, "значок", "icon-256.png"), "rb").read(), "image/png")
        return self.send(open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/start":
            return self.send({}, code=404)
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        url = (d.get("url") or "").strip()
        try:
            if not re.match(r"https?://\S+$", url):
                raise ValueError("Вставь ссылку на видео — она начинается с https://")
            s, e = secs(d.get("start")), secs(d.get("end"))
            if s is not None and e is not None and e <= s:
                raise ValueError("Конец куска должен быть позже начала")
        except ValueError as ex:
            return self.send({"error": str(ex)})
        mode = "audio" if d.get("mode") == "audio" else "video"
        fmt = "mp3" if d.get("fmt") == "mp3" else "wav"
        qv = int(d.get("q") or 0) if str(d.get("q") or 0).isdigit() else 0
        jid = uuid.uuid4().hex[:10]
        j = {"id": jid, "url": url, "mode": mode, "fmt": fmt, "q": qv, "start": s, "end": e, "state": "work", "pct": 0,
             "title": d.get("title") or url, "t": time.time()}
        with jlock:
            if not any(x["state"] == "work" for x in jobs.values()):
                shutil.rmtree(TMP, ignore_errors=True)  # leftovers of cancelled downloads
            jobs[jid] = j
        threading.Thread(target=run_job, args=(j,), daemon=True).start()
        return self.send({"id": jid})


def window(url):
    """Its own window, like a normal program (pywebview). False if it can't be shown."""
    shown, closed = threading.Event(), threading.Event()
    try:
        if WIN:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RSTP.Skachivalka")  # own taskbar button
        else:
            from Foundation import NSBundle

            info = NSBundle.mainBundle().infoDictionary()
            info["CFBundleName"] = info["CFBundleDisplayName"] = "Скачивалка"
        import webview

        ru = {"global.quit": "Выйти", "global.ok": "OK", "global.cancel": "Отмена", "global.saveFile": "Сохранить",
              "cocoa.menu.about": "О программе", "cocoa.menu.services": "Службы", "cocoa.menu.view": "Вид",
              "cocoa.menu.edit": "Правка", "cocoa.menu.hide": "Скрыть", "cocoa.menu.hideOthers": "Скрыть остальные",
              "cocoa.menu.showAll": "Показать все", "cocoa.menu.quit": "Выйти", "cocoa.menu.fullscreen": "Во весь экран",
              "cocoa.menu.cut": "Вырезать", "cocoa.menu.copy": "Копировать", "cocoa.menu.paste": "Вставить",
              "cocoa.menu.selectAll": "Выбрать всё"}
        dark = dark_os()
        w = webview.create_window("Скачивалка", url, width=1100, height=780, min_size=(480, 560), background_color="#0b0b0f" if dark else "#f4f1ec")
        w.events.shown += lambda: shown.set()
        w.events.closed += lambda: closed.set()
        if WIN:
            w.events.shown += lambda: win_icon("Скачивалка", os.path.join(HERE, "значок", "icon.ico"))
        else:
            import AppKit

            img = AppKit.NSImage.alloc().initWithContentsOfFile_(os.path.join(HERE, "значок", "icon.icns"))
            if img:
                AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        webview.start(localization=ru, private_mode=False, storage_path=os.path.join(HERE, "webview") if WIN else None)
        return True
    except Exception as e:  # noqa: BLE001
        if shown.is_set():  # the window is up after all (seen on Windows, first start): no browser on top of it
            print("Ошибка окна, но оно открыто:", e, flush=True)
            closed.wait()
            return True
        print("Окно не открылось, открываю в браузере:", e, flush=True)
        return False


def stop_all():
    for j in jobs.values():
        if j.get("proc"):
            j["cancel"] = True
            kill(j["proc"])


if __name__ == "__main__":
    if "--update" in sys.argv:  # the Windows installer fetches yt-dlp this way
        print(update(), flush=True)
        sys.exit(0 if os.path.isfile(YTDLP) else 1)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 8778), H)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"Скачивалка: {url}", flush=True)
    threading.Thread(target=auto_update, daemon=True).start()
    if "--no-open" in sys.argv:
        srv.serve_forever()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if "--browser" in sys.argv or not window(url):
        webbrowser.open(url)
        while True:
            time.sleep(3600)
    stop_all()  # the window is closed: unfinished downloads stop too
    srv.shutdown()
    os._exit(0)
