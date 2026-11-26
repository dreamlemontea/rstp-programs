# Local image improver for one computer: a page on localhost, the neural nets run on this computer
# (Mac: processor, 4 threads; Windows with an NVIDIA card: the card), nothing is sent anywhere. Models: Real-ESRGAN anime 6B (BSD-3), x4plus (BSD-3), 4xNomos8kSC (CC BY 4.0, Philip Hofmann).
import io, json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import spandrel
import torch
from PIL import Image, ImageOps

import updater  # the program's own updates from GitHub (updater.py next to this file)

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = "https://rstpstudio.ru"  # the RSTP Studio logo in the window opens it
WIN = sys.platform == "win32"
if sys.stdout is None:  # Windows, started without a console (pythonw): the messages go to a file
    sys.stdout = sys.stderr = open(os.path.join(HERE, "log.txt"), "w", encoding="utf-8", buffering=1)
OUT = os.path.expanduser("~/Downloads")
# the variants being tried live here until «Сохранить»; whatever is left is wiped on start and on close
TMP = os.path.join(HERE, "варианты")
MODELS = {"anime": "RealESRGAN_x4plus_anime_6B.pth", "nomos": "Nomos8kSC.safetensors", "x4plus": "RealESRGAN_x4plus.pth"}
torch.set_num_threads(4)  # half of the cores: the machine stays usable
DEV = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # NVIDIA card (Windows) if there is one
TILE = 320 if DEV.type == "cuda" else 192
_loaded, lock, bglock = {}, threading.Lock(), threading.Lock()  # bglock: the background net works beside the upscaling
progress = {}  # job id -> [tiles done, tiles total], for the progress bar on the page
MAXSIDE = 8000


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
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True, encoding="utf-8", **({"creationflags": 0x08000000} if WIN else {}))
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



def net(kind):
    if kind not in _loaded:
        _loaded[kind] = spandrel.ModelLoader().load_from_file(os.path.join(HERE, "модели", MODELS[kind])).model.eval().to(DEV)
    return _loaded[kind]


@torch.inference_mode()
def x4(m, img, tile=TILE, pad=16, job=None):
    x = torch.from_numpy(np.array(img)).permute(2, 0, 1).float().div(255)[None]
    _, _, h, w = x.shape
    out = torch.zeros(1, 3, h * 4, w * 4)
    total, done = -(-h // tile) * -(-w // tile), 0
    if job:
        progress[job] = [0, total]
    for y in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1, x1 = min(y + tile, h), min(x0 + tile, w)
            ya, xa, yb, xb = max(y - pad, 0), max(x0 - pad, 0), min(y1 + pad, h), min(x1 + pad, w)
            r = m(x[:, :, ya:yb, xa:xb].to(DEV)).float().cpu()
            out[:, :, y * 4 : y1 * 4, x0 * 4 : x1 * 4] = r[:, :, (y - ya) * 4 : (y - ya + y1 - y) * 4, (x0 - xa) * 4 : (x0 - xa + x1 - x0) * 4]
            done += 1
            if job:
                progress[job] = [done, total]
    return Image.fromarray(out[0].clamp(0, 1).permute(1, 2, 0).mul(255).round().byte().numpy())


BG_MODEL = os.path.join(HERE, "модели", "BiRefNet-lite.onnx")  # removes the background (BiRefNet, MIT)
SAM_ENC, SAM_DEC = (os.path.join(HERE, "модели", f) for f in ("SAM-encoder.onnx", "SAM-decoder.onnx"))  # «умное выделение» (MobileSAM)
_HF = "https://huggingface.co/"
NETS = [  # file, where from (pinned versions), SHA-256
    (BG_MODEL, _HF + "onnx-community/BiRefNet_lite-ONNX/resolve/de15b22ba131738a16dff04aab8bdf8dc32e3ac1/onnx/model.onnx",
     "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333"),
    (SAM_ENC, _HF + "Acly/MobileSAM/resolve/0d3b403339b4674a82493d5e97964dd78089ddc8/mobile_sam_image_encoder.onnx",
     "580f5fb648ea1062c0aabc26217aed56921985f03f0cbbd852bba81d760cc749"),
    (SAM_DEC, _HF + "Acly/MobileSAM/resolve/0d3b403339b4674a82493d5e97964dd78089ddc8/sam_mask_decoder_multi.onnx",
     "8976b90a87ba50a6a72217a5ff994f7d25ce16f2229fcc1ed259e1294c622ffe"),
]


def bg_ready():
    """The installers put the nets and onnxruntime in; a program updated by «Обновить» gets them here, once."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--prefer-binary", "onnxruntime"], check=True,
                       creationflags=0x08000000 if WIN else 0)  # no console window on Windows
    import hashlib
    for path, url, sha in NETS:
        if os.path.isfile(path):
            continue
        tmp, h = path + ".part", hashlib.sha256()
        updater._get(url, tmp, timeout=600)  # Mac: curl (its Python has no certificates)
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha:
            os.remove(tmp)
            raise RuntimeError("bgnet: a net came broken")
        os.replace(tmp, path)


def onnx(key, path):
    if key not in _loaded:
        bg_ready()
        import onnxruntime as ort
        _loaded[key] = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return _loaded[key]


_emb = {}  # «умное выделение»: picture id -> (what the net saw of it, its size at 1024); the last few pictures


def select(pid, x, y, w, h, data=None):
    """A click at (x, y) (0..1) -> the mask of the thing there, w×h. The picture is sent once, then only clicks."""
    with bglock:
        if data:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            k = 1024 / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BILINEAR)
            emb = onnx("sam-enc", SAM_ENC).run(None, {"input_image": np.asarray(im, dtype=np.float32)})[0]
            _emb[pid] = (emb, im.size)
            while len(_emb) > 6:
                _emb.pop(next(iter(_emb)))
        if pid not in _emb:
            return None
        emb, (sw, sh) = _emb[pid]
        m, iou, _ = onnx("sam-dec", SAM_DEC).run(None, {
            "image_embeddings": emb, "point_coords": np.array([[[x * sw, y * sh], [0, 0]]], np.float32),
            "point_labels": np.array([[1, -1]], np.float32), "mask_input": np.zeros((1, 1, 256, 256), np.float32),
            "has_mask_input": np.zeros(1, np.float32), "orig_im_size": np.array([sh, sw], np.float32)})
    a = m[0, int(iou[0].argmax())] > 0
    out = Image.fromarray((a * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def bg_mask(im):
    """A mask of the main subject (white = keep), the size of im. BiRefNet sees the picture at 1024×1024."""
    s = onnx("bg", BG_MODEL)
    x = np.asarray(im.resize((1024, 1024), Image.BILINEAR), dtype=np.float32) / 255
    x = ((x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]).astype(np.float32).transpose(2, 0, 1)[None]
    y = s.run(None, {s.get_inputs()[0].name: x})[0][0, 0]
    y = 1 / (1 + np.exp(-y))
    return Image.fromarray((y * 255).round().astype(np.uint8)).resize(im.size, Image.LANCZOS)


def mask_png(data):
    """«Убрать фон» ticked: the mask at once, for the preview and the brush (at most 2048 px on the long side)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    with bglock:
        m = bg_mask(im)
    m.thumbnail((2048, 2048), Image.LANCZOS)
    buf = io.BytesIO()
    m.save(buf, "PNG")
    return buf.getvalue()


def improve(data, name, kind, scale, job=None, nobg=False, mask=None, soft=0):
    """mask: the mask as the person fixed it (a file in TMP); none: the net makes it here. soft: edges blurred (px of a 2048 picture)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    w, h = im.size
    if mask:
        mask = Image.open(mask).convert("L").resize(im.size, Image.LANCZOS)
    elif nobg:  # the mask is made from the whole picture, then made as big as the result
        with bglock:
            mask = bg_mask(im)
    if mask is not None and soft > 0:
        from PIL import ImageFilter
        mask = mask.filter(ImageFilter.GaussianBlur(soft * max(w, h) / min(2048, max(w, h))))
    # ×2: the net works on a half-size copy (4 × 0.5 = 2); too big a result is made smaller first
    k = 0.5 if scale == 2 else 1.0
    k = min(k, MAXSIDE / (4 * max(w, h)))
    if k < 1:
        im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    with lock:
        big = x4(net(kind), im, job=job)
    if mask is not None:
        big.putalpha(mask.resize(big.size, Image.LANCZOS))
    os.makedirs(TMP, exist_ok=True)
    buf = io.BytesIO()
    big.save(buf, "PNG")
    path = os.path.join(TMP, uuid.uuid4().hex + ".png")
    open(path, "wb").write(buf.getvalue())
    return buf.getvalue(), path, big.size


def in_tmp(p):
    p = os.path.realpath(p or "")
    return p if p.startswith(os.path.realpath(TMP) + os.sep) and os.path.isfile(p) else None


def save(tmp, name, scale, nobg=False):
    """«Сохранить»: the chosen variant goes to the chosen folder (default Downloads) under the picture's own name."""
    os.makedirs(out_dir(), exist_ok=True)
    base = re.sub(r"[^\w .()-]", "_", os.path.splitext(name)[0])[:80] or "image"
    base += f" ×{scale}" + (" без фона" if nobg else "")
    path, n = os.path.join(out_dir(), f"{base}.png"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(out_dir(), f"{base} ({n}).png")
    shutil.move(tmp, path)
    return path


def reveal(path, select=False):
    """A folder in Finder / Explorer; select=True: the folder with this file selected."""
    if WIN:
        subprocess.run(f'explorer /select,"{path}"' if select else f'explorer "{path}"')
    else:
        subprocess.run(["open", "-R", path] if select else ["open", path])


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


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/open":  # the results folder in Finder
            os.makedirs(out_dir(), exist_ok=True)
            reveal(out_dir())
            self.send_response(204)
            return self.end_headers()
        if u.path.startswith("/t/"):  # a variant not saved yet (thumbnails can be dragged out of the window)
            p = in_tmp(os.path.join(TMP, urllib.parse.unquote(u.path[3:])))
            if p:
                return self.send(open(p, "rb").read(), "image/png")
            self.send_response(404)
            return self.end_headers()
        if u.path == "/show":  # one result, selected in Finder (only files from the results folder)
            p = os.path.realpath(q.get("path", [""])[0])
            if p.startswith(os.path.realpath(out_dir()) + os.sep) and os.path.exists(p):
                reveal(p, select=True)
            self.send_response(204)
            return self.end_headers()
        if u.path.startswith("/r/"):  # a saved result by its file name: thumbnails can be dragged out of the window
            p = os.path.realpath(os.path.join(out_dir(), urllib.parse.unquote(u.path[3:])))
            if p.startswith(os.path.realpath(out_dir()) + os.sep) and os.path.isfile(p):
                return self.send(open(p, "rb").read(), "image/png")
            self.send_response(404)
            return self.end_headers()
        if u.path == "/progress":
            d, t = progress.get(q.get("id", [""])[0], [0, 0])
            return self.send(json.dumps({"done": d, "total": t}).encode(), "application/json")
        if u.path.startswith("/folder"):
            return self.send(folder_route(u.path), "application/json")
        if u.path == "/appver":
            return self.send(json.dumps({"v": updater.current()}).encode(), "application/json")
        if u.path == "/appcheck":  # on start: is a newer version of the program on GitHub?
            return self.send(json.dumps(updater.check(), ensure_ascii=False).encode(), "application/json")
        if u.path == "/appupdate":
            if progress and any(d < t for d, t in progress.values()):
                return self.send(json.dumps({"ok": False, "msg": "Сначала дождись, пока закончится улучшение"}, ensure_ascii=False).encode(), "application/json")
            ok, msg = updater.run()
            return self.send(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False).encode(), "application/json")
        if u.path == "/site":  # the logo was clicked: the site in the usual browser
            webbrowser.open(SITE)
            self.send_response(204)
            return self.end_headers()
        if u.path in ("/rstp-logo.png", "/rstp-logo-dark.png"):
            return self.send(open(os.path.join(HERE, "значок", u.path[1:]), "rb").read(), "image/png")
        if u.path == "/icon.png":
            return self.send(open(os.path.join(HERE, "значок", "icon-256.png"), "rb").read(), "image/png")
        body = open(os.path.join(HERE, "index.html"), "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/select":  # «умное выделение»: a click on the picture
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                f = lambda k: float(q.get(k, ["0"])[0])  # noqa: E731
                png = select(q.get("id", [""])[0], f("x"), f("y"), max(1, int(f("w"))), max(1, int(f("h"))), data or None)
                if png is None:  # forgotten: the page sends the picture again
                    self.send_response(409)
                    return self.end_headers()
                return self.send(png, "image/png")
            except Exception as e:  # noqa: BLE001
                self.send_response(500)
                self.end_headers()
                return self.wfile.write(str(e).encode())
        if u.path in ("/mask", "/maskup"):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                if u.path == "/mask":  # the picture -> its mask
                    return self.send(mask_png(data), "image/png")
                os.makedirs(TMP, exist_ok=True)  # the mask fixed with the brush, for the next improving
                name = "mask-" + uuid.uuid4().hex + ".png"
                open(os.path.join(TMP, name), "wb").write(data)
                return self.send(json.dumps({"id": name}).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                self.send_response(500)
                self.end_headers()
                return self.wfile.write(str(e).encode())
        if u.path in ("/save", "/forget"):
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if u.path == "/forget":  # variants that are not wanted any more
                for p in d.get("paths", []):
                    if in_tmp(p):
                        os.remove(in_tmp(p))
                return self.send(b"{}", "application/json")
            tmp = in_tmp(d.get("path"))
            if not tmp:
                return self.send(json.dumps({"error": "Этот вариант уже стёрт — улучши ещё раз"}, ensure_ascii=False).encode(), "application/json")
            path = save(tmp, str(d.get("name") or "image"), int(d.get("scale") or 4), bool(d.get("bg")))
            return self.send(json.dumps({"path": path}, ensure_ascii=False).encode(), "application/json")
        kind, scale = q.get("kind", ["anime"])[0], int(q.get("scale", ["4"])[0])
        name = urllib.parse.unquote(q.get("name", ["image"])[0])
        job = q.get("id", [None])[0]
        nobg = q.get("bg", ["0"])[0] != "0"
        mask = in_tmp(os.path.join(TMP, q.get("mask", [""])[0])) if q.get("mask") else None
        soft = float(q.get("soft", ["0"])[0] or 0)
        try:
            if kind not in MODELS or scale not in (2, 4):
                raise ValueError("bad options")
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            t = time.time()
            png, path, size = improve(data, name, kind, scale, job, nobg, mask, soft)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("X-Info", urllib.parse.quote(json.dumps({"path": path, "w": size[0], "h": size[1], "secs": round(time.time() - t, 1)})))
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
        finally:
            progress.pop(job, None)


def window(url):
    """Its own window, like a normal program (pywebview: Safari engine on Mac, Edge on Windows). False if it can't be shown."""
    shown, closed = threading.Event(), threading.Event()
    try:
        if WIN:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RSTP.Uluchshalka")  # own taskbar button
        else:
            from Foundation import NSBundle

            info = NSBundle.mainBundle().infoDictionary()  # the name in the menu bar instead of "Python"
            info["CFBundleName"] = info["CFBundleDisplayName"] = "Улучшалка"
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True  # "Скачать" asks where to save
        ru = {"global.quit": "Выйти", "global.ok": "OK", "global.cancel": "Отмена", "global.saveFile": "Сохранить",
              "cocoa.menu.about": "О программе", "cocoa.menu.services": "Службы", "cocoa.menu.view": "Вид",
              "cocoa.menu.edit": "Правка", "cocoa.menu.hide": "Скрыть", "cocoa.menu.hideOthers": "Скрыть остальные",
              "cocoa.menu.showAll": "Показать все", "cocoa.menu.quit": "Выйти", "cocoa.menu.fullscreen": "Во весь экран",
              "cocoa.menu.cut": "Вырезать", "cocoa.menu.copy": "Копировать", "cocoa.menu.paste": "Вставить",
              "cocoa.menu.selectAll": "Выбрать всё"}
        dark = dark_os()
        w = webview.create_window("Улучшалка", url, width=1180, height=780, min_size=(880, 600), background_color="#0b0b0f" if dark else "#f4f1ec")
        w.events.shown += lambda: shown.set()
        w.events.closed += lambda: closed.set()
        if WIN:
            w.events.shown += lambda: win_icon("Улучшалка", os.path.join(HERE, "значок", "icon.ico"))
        else:
            import AppKit

            img = AppKit.NSImage.alloc().initWithContentsOfFile_(os.path.join(HERE, "значок", "icon.icns"))
            if img:  # its own icon in the Dock
                AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        webview.start(localization=ru, private_mode=False, storage_path=os.path.join(HERE, "webview") if WIN else None)  # not private: the chosen theme is remembered
        return True
    except Exception as e:  # noqa: BLE001
        if shown.is_set():  # the window is up after all (seen on Windows, first start): no browser on top of it
            print("Ошибка окна, но оно открыто:", e, flush=True)
            closed.wait()
            return True
        print("Окно не открылось, открываю в браузере:", e, flush=True)
        return False


if __name__ == "__main__":
    if "--bgnet" in sys.argv:  # the installers fetch the background net this way
        bg_ready()
        sys.exit(0)
    if WIN:  # gentle: the rest of the computer stays smooth (like "nice" on the Mac)
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    shutil.rmtree(TMP, ignore_errors=True)  # unsaved variants of the last time
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 8777), H)
    except OSError:  # already taken (the program is open twice): any free port
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"Улучшалка: {url}", flush=True)
    if "--no-open" in sys.argv:
        srv.serve_forever()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if "--browser" in sys.argv or not window(url):
        webbrowser.open(url)
        print("Закрой это окно — программа остановится.", flush=True)
        while True:
            time.sleep(3600)
    srv.shutdown()  # the window is closed: everything stops, unsaved variants are wiped
    shutil.rmtree(TMP, ignore_errors=True)
    os._exit(0)
