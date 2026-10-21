#!/usr/bin/env python3
"""
YT701 - YouTube search + player + downloader.
Player backends: mpv (JSON IPC) or VLC (HTTP interface).
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk, messagebox, filedialog

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX   = sys.platform.startswith("linux")
IS_MAC     = sys.platform == "darwin"

try:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    APP_DIR = os.getcwd()

CONFIG_FILE   = os.path.join(APP_DIR, "config.json")
PLAYLIST_FILE = os.path.join(APP_DIR, "playlist.json")

MAX_HEIGHT = 480
FMT_MUXED  = f"b[height<={MAX_HEIGHT}]/b"
FMT_MERGED = (f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]"
              f"/bv*+ba/b")

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
    YTDLP_VERSION = yt_dlp.version.__version__
except ImportError:
    print("ERROR: yt-dlp not installed.", file=sys.stderr)
    sys.exit(1)

YDL_SEARCH = dict(
    quiet=True, no_warnings=True, extract_flat=True,
    skip_download=True, socket_timeout=15, noplaylist=True,
)

# ---------- Config ----------
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

CONFIG = load_config()

def default_download_dir():
    if IS_WINDOWS:
        return os.path.join(os.path.expanduser("~"), "Downloads")
    if IS_MAC:
        return os.path.join(os.path.expanduser("~"), "Downloads")
    return os.path.join(os.path.expanduser("~"), "Downloads")

# ---------- Discovery ----------
def _exe(name):
    return name + ".exe" if IS_WINDOWS else name

def find_mpv():
    p = CONFIG.get("mpv_path")
    if p and os.path.isfile(p):
        return p
    found = shutil.which(_exe("mpv"))
    if found:
        return found
    candidates = [os.path.join(APP_DIR, _exe("mpv"))]
    if IS_WINDOWS:
        candidates += [
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Program Files (x86)\mpv\mpv.exe",
            os.path.expanduser(r"~\mpv\mpv.exe"),
        ]
    elif IS_MAC:
        candidates += ["/Applications/mpv.app/Contents/MacOS/mpv",
                       "/opt/homebrew/bin/mpv", "/usr/local/bin/mpv"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

def find_vlc():
    p = CONFIG.get("vlc_path")
    if p and os.path.isfile(p):
        return p
    found = shutil.which(_exe("vlc"))
    if found:
        return found
    candidates = [os.path.join(APP_DIR, _exe("vlc"))]
    if IS_WINDOWS:
        candidates += [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ]
    elif IS_MAC:
        candidates += ["/Applications/VLC.app/Contents/MacOS/VLC"]
    else:
        candidates += ["/usr/bin/vlc", "/usr/local/bin/vlc"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

def find_ytdlp_bin():
    p = CONFIG.get("ytdlp_path")
    if p and os.path.isfile(p):
        return p
    found = shutil.which(_exe("yt-dlp"))
    if found:
        return found
    candidates = [os.path.join(APP_DIR, _exe("yt-dlp"))]
    try:
        py_dir = os.path.dirname(sys.executable)
        candidates += [os.path.join(py_dir, _exe("yt-dlp")),
                       os.path.join(py_dir, "Scripts", _exe("yt-dlp"))]
    except Exception:
        pass
    if IS_WINDOWS:
        for ver in ("312", "311", "310"):
            candidates.append(os.path.expanduser(
                rf"~\AppData\Local\Programs\Python\Python{ver}\Scripts\yt-dlp.exe"
            ))
        candidates.append(os.path.expanduser(
            r"~\AppData\Roaming\Python\Scripts\yt-dlp.exe"
        ))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None

def find_ffmpeg():
    p = CONFIG.get("ffmpeg_path")
    if p and os.path.isfile(p):
        return p
    found = shutil.which(_exe("ffmpeg"))
    if found:
        return found
    if IS_WINDOWS:
        for c in [os.path.join(APP_DIR, "ffmpeg.exe"),
                  r"C:\ffmpeg\bin\ffmpeg.exe"]:
            if os.path.isfile(c):
                return c
    return None

# ---------- Helpers ----------
def fmt_duration(sec):
    if sec is None:
        return "?:??"
    try:
        sec = int(float(sec))
    except (ValueError, TypeError):
        return "?:??"
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"

def safe_filename(name, maxlen=150):
    bad = '<>:"/\\|?*\x00-\x1f'
    cleaned = "".join(c if c not in bad else "_" for c in name)
    return cleaned.strip().rstrip(".")[:maxlen] or "video"

def load_playlist():
    if not os.path.exists(PLAYLIST_FILE):
        return []
    try:
        with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_playlist(items):
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------- Download dialog ----------
class DownloadDialog:
    def __init__(self, parent, title):
        self.top = tk.Toplevel(parent)
        self.top.title("Downloading")
        self.top.geometry("440x140")
        self.top.transient(parent)
        self.top.resizable(False, False)
        self.cancelled = False

        ttk.Label(self.top, text=title, wraplength=420,
                  anchor="w").pack(fill="x", padx=12, pady=(12, 4))
        self.progress = ttk.Progressbar(self.top, mode="determinate",
                                         maximum=100)
        self.progress.pack(fill="x", padx=12, pady=4)
        self.pct_var = tk.StringVar(value="Starting…")
        ttk.Label(self.top, textvariable=self.pct_var, anchor="w").pack(
            fill="x", padx=12)
        ttk.Button(self.top, text="Cancel",
                   command=self.cancel).pack(pady=8)
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)

    def cancel(self):
        self.cancelled = True
        try:
            self.top.destroy()
        except Exception:
            pass

    def update(self, text, pct):
        try:
            self.pct_var.set(text)
            self.progress["value"] = max(0, min(100, pct))
        except Exception:
            pass

    def close(self):
        try:
            self.top.destroy()
        except Exception:
            pass

# ---------- mpv controller ----------
class MpvController:
    def __init__(self, mpv_path, ytdlp_path):
        self.mpv_path = mpv_path
        self.ytdlp_path = ytdlp_path
        self.proc = None
        self.sock = None
        self.socket_path = None
        self._running = False
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._responses = {}
        self._pending = {}
        self._properties = {}
        self._prop_lock = threading.Lock()

    def _pipe_name(self):
        return f"yt701-mpv-{os.getpid()}"

    def _socket_path(self):
        if IS_WINDOWS:
            return rf"\\.\pipe\{self._pipe_name()}"
        return f"/tmp/{self._pipe_name()}.sock"

    def play(self, url):
        self.stop()
        self.socket_path = self._socket_path()
        if not IS_WINDOWS and os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        cmd = [
            self.mpv_path, "--no-config", "--quiet", "--hwdec=no",
            "--cache=yes", "--cache-secs=10",
            "--demuxer-max-bytes=16M", "--demuxer-max-back-bytes=8M",
            "--osc=no", "--force-window=yes",
            f"--input-ipc-server={self.socket_path}",
            f"--ytdl-format={FMT_MERGED}",
            f"--script-opts=ytdl_hook-ytdl_path={self.ytdlp_path}",
        ]
        if IS_WINDOWS:
            cmd += ["--vo=gpu", "--ao=wasapi"]
        elif IS_MAC:
            cmd += ["--vo=gpu", "--ao=coreaudio"]
        else:
            cmd += ["--vo=xv,x11", "--ao=alsa", "--input-vo-keyboard"]
        cmd.append(url)

        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except Exception as ex:
            return False, str(ex)

        if not self._connect_ipc(timeout=15):
            code = self.proc.poll() if self.proc else None
            self.stop()
            return False, f"mpv IPC failed (exit code {code})"

        for i, prop in enumerate(("time-pos", "duration", "pause", "volume"), 1):
            self._send(["observe_property", i, prop])
        return True, None

    def _connect_ipc(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                return False
            try:
                if IS_WINDOWS:
                    self.sock = open(self.socket_path, "r+b", buffering=0)
                else:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.socket_path)
                    self.sock = s
                self._running = True
                threading.Thread(target=self._read_loop, daemon=True).start()
                return True
            except (FileNotFoundError, OSError, ConnectionRefusedError):
                time.sleep(0.1)
        return False

    def _read_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = (self.sock.read(4096) if IS_WINDOWS
                         else self.sock.recv(4096))
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    self._handle_message(msg)
            except (OSError, ValueError):
                break

    def _handle_message(self, msg):
        if "request_id" in msg:
            rid = msg["request_id"]
            self._responses[rid] = msg
            ev = self._pending.get(rid)
            if ev:
                ev.set()
        elif msg.get("event") == "property-change":
            name = msg.get("name")
            if name:
                with self._prop_lock:
                    self._properties[name] = msg.get("data")

    def _send(self, command, wait=False, timeout=2.0):
        if not self.sock:
            return None
        with self._id_lock:
            self._next_id += 1
            rid = self._next_id
        try:
            data = (json.dumps({"command": command, "request_id": rid})
                    + "\n").encode()
            if IS_WINDOWS:
                self.sock.write(data)
            else:
                self.sock.sendall(data)
        except (OSError, ValueError):
            return None
        if not wait:
            return rid
        ev = threading.Event()
        self._pending[rid] = ev
        if ev.wait(timeout):
            resp = self._responses.pop(rid, None)
            self._pending.pop(rid, None)
            return resp
        self._pending.pop(rid, None)
        return None

    def get_property(self, name):
        with self._prop_lock:
            if name in self._properties:
                return self._properties[name]
        return None

    def toggle_pause(self):
        self._send(["cycle", "pause"])

    def seek(self, percent):
        self._send(["seek", float(percent), "absolute-percent"])

    def set_volume(self, pct):
        self._send(["set_property", "volume", float(pct)])

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self._properties = {}

# ---------- VLC controller ----------
class VlcController:
    def __init__(self, vlc_path):
        self.vlc_path = vlc_path
        self.proc = None
        self.port = None
        self.password = "yt701"

    def play(self, target, headers=None):
        self.stop()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]

        cmd = [
            self.vlc_path,
            "--no-video-title-show",
            "--extraintf", "http",
            "--http-password", self.password,
            "--http-port", str(self.port),
            "--http-host", "127.0.0.1",
        ]
        if headers:
            ua = headers.get("User-Agent")
            ref = headers.get("Referer")
            if ua:
                cmd.append(f"--http-user-agent={ua}")
            if ref:
                cmd.append(f"--http-referrer={ref}")
        cmd.append(target)

        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except Exception as ex:
            return False, str(ex)

        for _ in range(60):
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self.stop()
                return False, f"VLC exited (code {code})"
            if self._http_get("") is not None:
                return True, None
            time.sleep(0.1)
        self.stop()
        return False, "VLC HTTP interface did not start"

    def _http_get(self, query):
        url = f"http://127.0.0.1:{self.port}/requests/status.json"
        if query:
            url += "?" + query
        req = urllib.request.Request(url)
        cred = base64.b64encode(f":{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        try:
            with urllib.request.urlopen(req, timeout=1) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def toggle_pause(self):
        return self._http_get("command=pl_pause")

    def seek(self, percent):
        return self._http_get(f"command=seek&val={int(percent)}%25")

    def set_volume(self, pct):
        val = int(pct * 512 / 100)
        return self._http_get(f"command=volume&val={val}")

    def get_status(self):
        return self._http_get("")

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

# ---------- App ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title("YT701")
        root.geometry("840x500")
        self.playlist = load_playlist()
        self.search_results = []
        self.player_var = tk.StringVar(value=CONFIG.get("player", "mpv"))
        self.active_player = None
        self.mpv_ctrl = None
        self.vlc_ctrl = None
        self.user_seeking = False
        self._tmp_dir = None
        self._build_ui()
        self._refresh_playlist()
        self._report_dependencies()
        self._poll_playback()

    def _report_dependencies(self):
        parts = [
            f"mpv:{'ok' if find_mpv() else 'MISSING'}",
            f"vlc:{'ok' if find_vlc() else 'MISSING'}",
            f"yt-dlp:{'ok' if find_ytdlp_bin() else 'MISSING'}",
            f"ffmpeg:{'ok' if find_ffmpeg() else 'MISSING'}",
        ]
        self.status.set("  |  ".join(parts))

    def _build_ui(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom", padx=4, pady=4)

        self.play_btn = ttk.Button(bar, text="▶", width=3,
                                    command=self._toggle_pause)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■", width=3,
                                    command=self._stop_playback)
        self.stop_btn.pack(side="left", padx=(2, 8))

        self.progress_scale = ttk.Scale(bar, from_=0, to=100,
                                         orient="horizontal")
        self.progress_scale.pack(side="left", fill="x", expand=True, padx=4)
        self.progress_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.progress_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(bar, textvariable=self.time_var).pack(side="left", padx=8)

        ttk.Label(bar, text="Vol:").pack(side="left", padx=(8, 2))
        self.volume_scale = ttk.Scale(bar, from_=0, to=100, length=100,
                                       orient="horizontal",
                                       command=self._on_volume_change)
        self.volume_scale.set(100)
        self.volume_scale.pack(side="left", padx=4)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self._build_search_tab(nb)
        self._build_playlist_tab(nb)

    def _build_search_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Search")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=4, pady=4)
        self.query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.do_search())
        self.search_btn = ttk.Button(top, text="Search", command=self.do_search)
        self.search_btn.pack(side="left", padx=(4, 0))

        ttk.Label(top, text="Player:").pack(side="left", padx=(8, 2))
        combo = ttk.Combobox(top, textvariable=self.player_var,
                              values=["mpv", "vlc"], state="readonly", width=6)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._on_player_changed)

        lf = ttk.Frame(tab)
        lf.pack(fill="both", expand=True, padx=4)
        self.results_list = tk.Listbox(lf, activestyle="none")
        self.results_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, orient="vertical",
                            command=self.results_list.yview)
        sb.pack(side="right", fill="y")
        self.results_list.config(yscrollcommand=sb.set)
        self.results_list.bind("<Double-Button-1>",
                                lambda e: self.play_selected_search())

        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=4, pady=4)
        ttk.Button(btns, text="Play",
                    command=self.play_selected_search).pack(side="left")
        ttk.Button(btns, text="Add to Playlist",
                    command=self.add_search_to_playlist).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="Download",
                    command=self.download_selected_search).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="Show Formats",
                    command=self.show_formats).pack(side="left", padx=(4, 0))

    def _build_playlist_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Playlist")
        lf = ttk.Frame(tab)
        lf.pack(fill="both", expand=True, padx=4, pady=4)
        self.pl_list = tk.Listbox(lf, activestyle="none")
        self.pl_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, orient="vertical",
                            command=self.pl_list.yview)
        sb.pack(side="right", fill="y")
        self.pl_list.config(yscrollcommand=sb.set)
        self.pl_list.bind("<Double-Button-1>",
                           lambda e: self.play_selected_playlist())
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=4, pady=4)
        ttk.Button(btns, text="Play",
                    command=self.play_selected_playlist).pack(side="left")
        ttk.Button(btns, text="Download",
                    command=self.download_selected_playlist).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="Remove",
                    command=self.remove_selected_playlist).pack(side="left", padx=(4, 0))

    def _on_player_changed(self, event=None):
        CONFIG["player"] = self.player_var.get()
        save_config(CONFIG)
        self._stop_playback()
        self.status.set(f"Player: {self.player_var.get()}")

    # ---------- Search ----------
    def do_search(self):
        q = self.query_var.get().strip()
        if not q:
            return
        self.search_btn.config(state="disabled")
        self.status.set(f"Searching: {q}")
        self.results_list.delete(0, "end")
        threading.Thread(target=self._search_worker, args=(q,),
                          daemon=True).start()

    def _search_worker(self, q):
        try:
            with yt_dlp.YoutubeDL(YDL_SEARCH) as ydl:
                info = ydl.extract_info(f"ytsearch15:{q}", download=False)
            results = []
            for e in (info.get("entries") or []):
                if not e or not e.get("id"):
                    continue
                results.append({
                    "id": e["id"],
                    "title": e.get("title") or "(no title)",
                    "duration": e.get("duration"),
                    "uploader": e.get("uploader") or e.get("channel") or "",
                })
            self.root.after(0, self._populate_results, results)
        except Exception as ex:
            msg = str(ex)
            self.root.after(0, self._search_error, msg)

    def _populate_results(self, results):
        self.search_results = results
        self.results_list.delete(0, "end")
        for r in results:
            self.results_list.insert(
                "end",
                f"{r['title']}  [{fmt_duration(r['duration'])}]  — {r['uploader']}")
        self.search_btn.config(state="normal")
        self.status.set(f"{len(results)} results")

    def _search_error(self, msg):
        self.search_btn.config(state="normal")
        self.status.set("Search failed")
        messagebox.showerror("Search failed", msg)

    def _selected_result(self):
        sel = self.results_list.curselection()
        return self.search_results[sel[0]] if sel else None

    def play_selected_search(self):
        r = self._selected_result()
        if r:
            self._play(r)

    def add_search_to_playlist(self):
        r = self._selected_result()
        if not r:
            return
        if any(x["id"] == r["id"] for x in self.playlist):
            self.status.set("Already in playlist")
            return
        self.playlist.append(r)
        save_playlist(self.playlist)
        self._refresh_playlist()
        self.status.set("Added to playlist")

    def download_selected_search(self):
        r = self._selected_result()
        if r:
            self._start_download(r)

    def show_formats(self):
        r = self._selected_result()
        if not r:
            return
        threading.Thread(target=self._formats_worker, args=(r,),
                          daemon=True).start()

    def _formats_worker(self, rec):
        url = f"https://www.youtube.com/watch?v={rec['id']}"
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                    "skip_download": True,
                                    "noplaylist": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            rows = ["  id  height  vcodec      acodec    note"]
            for f in info.get("formats", []):
                rows.append(
                    f"  {str(f.get('format_id','?')):>3}  "
                    f"{str(f.get('height','-')):>4}  "
                    f"{str(f.get('vcodec','-')):>10}  "
                    f"{str(f.get('acodec','-')):>7}  "
                    f"{f.get('format_note','')}")
            text = f"{rec['title']}\n\n" + "\n".join(rows)
            self.root.after(0, lambda: self._show_text_window(
                "Available formats", text))
        except Exception as ex:
            msg = str(ex)
            self.root.after(0, lambda: messagebox.showerror("Formats failed", msg))

    def _show_text_window(self, title, text):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("720x500")
        txt = tk.Text(win, wrap="none", font=("Courier", 9))
        txt.insert("1.0", text)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=4)

    # ---------- Playlist ----------
    def _refresh_playlist(self):
        self.pl_list.delete(0, "end")
        for r in self.playlist:
            self.pl_list.insert(
                "end", f"{r['title']}  [{fmt_duration(r['duration'])}]")

    def _selected_playlist(self):
        sel = self.pl_list.curselection()
        return self.playlist[sel[0]] if sel else None

    def play_selected_playlist(self):
        r = self._selected_playlist()
        if r:
            self._play(r)

    def download_selected_playlist(self):
        r = self._selected_playlist()
        if r:
            self._start_download(r)

    def remove_selected_playlist(self):
        r = self._selected_playlist()
        if not r:
            return
        self.playlist = [x for x in self.playlist if x["id"] != r["id"]]
        save_playlist(self.playlist)
        self._refresh_playlist()

    # ---------- Download ----------
    def _start_download(self, rec):
        if not find_ffmpeg():
            messagebox.showerror(
                "ffmpeg required",
                "Downloading needs ffmpeg to merge separate video and\n"
                "audio streams.\n\n"
                "Linux:   sudo apt install ffmpeg\n"
                "Windows: Place ffmpeg.exe next to this script.")
            return

        dl_dir = CONFIG.get("download_dir") or default_download_dir()
        if not os.path.isdir(dl_dir):
            try:
                os.makedirs(dl_dir, exist_ok=True)
            except Exception:
                dl_dir = APP_DIR

        target = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=dl_dir,
            mustexist=False,
        )
        if not target:
            return
        if not os.path.isdir(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                messagebox.showerror("Folder error",
                                     f"Cannot create folder:\n{target}")
                return

        CONFIG["download_dir"] = target
        save_config(CONFIG)

        self.status.set(f"Downloading: {rec['title']}")
        threading.Thread(target=self._download_worker,
                          args=(rec, target), daemon=True).start()

    def _download_worker(self, rec, target_dir):
        url = f"https://www.youtube.com/watch?v={rec['id']}"
        title = rec["title"]

        dialog = DownloadDialog(self.root, title)
        tmp_dir = tempfile.mkdtemp(prefix="yt701_dl_")
        outtmpl = os.path.join(tmp_dir, "video.%(ext)s")

        def hook(d):
            if dialog.cancelled:
                raise DownloadCancelled("cancelled by user")
            status = d.get("status")
            if status == "downloading":
                pct_str = d.get("_percent_str", "0%").strip()
                try:
                    num = float(pct_str.replace("%", "").strip())
                except Exception:
                    num = 0.0
                speed = d.get("_speed_str", "").strip()
                eta = d.get("_eta_str", "").strip()
                extra = ""
                if speed:
                    extra += f"  {speed}"
                if eta:
                    extra += f"  ETA {eta}"
                self.root.after(0, dialog.update,
                                f"{pct_str}{extra}", num)
            elif status == "finished":
                self.root.after(0, dialog.update, "Merging…", 100)

        try:
            with yt_dlp.YoutubeDL({
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": 30, "format": FMT_MERGED,
                "outtmpl": outtmpl, "progress_hooks": [hook],
                "merge_output_format": "mp4",
                "continuedl": False,
            }) as ydl:
                ydl.download([url])

            files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                     if os.path.isfile(os.path.join(tmp_dir, f))]
            if not files:
                raise RuntimeError("No file was produced by the download")

            src = files[0]
            ext = os.path.splitext(src)[1] or ".mp4"
            base = safe_filename(title)
            dst = os.path.join(target_dir, base + ext)
            counter = 1
            while os.path.exists(dst):
                dst = os.path.join(target_dir, f"{base} ({counter}){ext}")
                counter += 1
            shutil.move(src, dst)

            self.root.after(0, dialog.close)
            self.root.after(0, self.status.set, f"Saved: {dst}")
            self.root.after(0, lambda: self._ask_open_folder(dst))

        except DownloadCancelled:
            self.root.after(0, dialog.close)
            self.root.after(0, self.status.set, "Download cancelled")
        except Exception as ex:
            err = str(ex)
            self.root.after(0, dialog.close)
            self.root.after(0, self.status.set, "Download failed")
            self.root.after(0, lambda: messagebox.showerror(
                "Download failed", err))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _ask_open_folder(self, path):
        folder = os.path.dirname(path)
        if messagebox.askyesno(
                "Download complete",
                f"Saved to:\n{path}\n\nOpen folder?"):
            try:
                if IS_WINDOWS:
                    os.startfile(folder)
                elif IS_MAC:
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception:
                pass

    # ---------- Playback controls ----------
    def _toggle_pause(self):
        if self.active_player == "mpv" and self.mpv_ctrl and self.mpv_ctrl.is_running():
            self.mpv_ctrl.toggle_pause()
        elif self.active_player == "vlc" and self.vlc_ctrl and self.vlc_ctrl.is_running():
            self.vlc_ctrl.toggle_pause()

    def _stop_playback(self):
        if self.mpv_ctrl:
            self.mpv_ctrl.stop()
            self.mpv_ctrl = None
        if self.vlc_ctrl:
            self.vlc_ctrl.stop()
            self.vlc_ctrl = None
        if self._tmp_dir:
            try:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
            self._tmp_dir = None
        self._reset_playback_ui()

    def _on_seek_press(self, event):
        self.user_seeking = True

    def _on_seek_release(self, event):
        if not self.user_seeking:
            return
        self.user_seeking = False
        pct = self.progress_scale.get()
        if self.active_player == "mpv" and self.mpv_ctrl and self.mpv_ctrl.is_running():
            self.mpv_ctrl.seek(pct)
        elif self.active_player == "vlc" and self.vlc_ctrl and self.vlc_ctrl.is_running():
            self.vlc_ctrl.seek(pct)

    def _on_volume_change(self, val):
        try:
            pct = float(val)
        except (ValueError, TypeError):
            return
        if self.active_player == "mpv" and self.mpv_ctrl and self.mpv_ctrl.is_running():
            self.mpv_ctrl.set_volume(pct)
        elif self.active_player == "vlc" and self.vlc_ctrl and self.vlc_ctrl.is_running():
            self.vlc_ctrl.set_volume(pct)

    def _poll_playback(self):
        try:
            if self.active_player == "mpv" and self.mpv_ctrl:
                if not self.mpv_ctrl.is_running():
                    self._stop_playback()
                else:
                    pos = self.mpv_ctrl.get_property("time-pos")
                    dur = self.mpv_ctrl.get_property("duration")
                    paused = self.mpv_ctrl.get_property("pause")
                    if pos is not None and dur and dur > 0 and not self.user_seeking:
                        self.progress_scale.set(min(100, (pos / dur) * 100))
                        self.time_var.set(
                            f"{fmt_duration(pos)} / {fmt_duration(dur)}")
                    self.play_btn.config(text="▶" if paused else "⏸")
            elif self.active_player == "vlc" and self.vlc_ctrl:
                if not self.vlc_ctrl.is_running():
                    self._stop_playback()
                else:
                    status = self.vlc_ctrl.get_status()
                    if status:
                        pos = status.get("time", 0)
                        dur = status.get("length", 0)
                        state = status.get("state", "")
                        if dur and dur > 0 and not self.user_seeking:
                            self.progress_scale.set(min(100, (pos / dur) * 100))
                            self.time_var.set(
                                f"{fmt_duration(pos)} / {fmt_duration(dur)}")
                        self.play_btn.config(text="▶" if state == "paused" else "⏸")
        except Exception:
            pass
        self.root.after(500, self._poll_playback)

    def _reset_playback_ui(self):
        self.active_player = None
        self.progress_scale.set(0)
        self.time_var.set("00:00 / 00:00")
        self.play_btn.config(text="▶")

    # ---------- Play dispatch ----------
    def _play(self, rec):
        self._stop_playback()
        player = self.player_var.get()
        self.status.set(f"Starting ({player}): {rec['title']}")
        if player == "vlc":
            threading.Thread(target=self._vlc_worker, args=(rec,),
                              daemon=True).start()
        else:
            threading.Thread(target=self._mpv_worker, args=(rec,),
                              daemon=True).start()

    def _mpv_worker(self, rec):
        mpv = find_mpv()
        if not mpv:
            self.root.after(0, lambda: messagebox.showerror(
                "mpv not found",
                ("Place mpv.exe next to this script:\n" + APP_DIR)
                if IS_WINDOWS else "Install mpv: sudo apt install mpv"))
            return
        ytdlp = find_ytdlp_bin()
        if not ytdlp:
            self.root.after(0, lambda: messagebox.showerror(
                "yt-dlp binary not found",
                "Install: pip install --user -U yt-dlp"))
            return
        url = f"https://www.youtube.com/watch?v={rec['id']}"
        self.mpv_ctrl = MpvController(mpv, ytdlp)
        ok, err = self.mpv_ctrl.play(url)
        if not ok:
            self.mpv_ctrl = None
            self.root.after(0, lambda: messagebox.showerror(
                "mpv failed", err or "unknown"))
            return
        self.active_player = "mpv"
        self.root.after(0, lambda: self.status.set(
            f"Playing (mpv): {rec['title']}"))

    def _vlc_worker(self, rec):
        vlc = find_vlc()
        if not vlc:
            self.root.after(0, lambda: messagebox.showerror(
                "VLC not found", "Install VLC or set vlc_path in config.json."))
            return
        url = f"https://www.youtube.com/watch?v={rec['id']}"

        direct_url, headers = None, {}
        try:
            with yt_dlp.YoutubeDL({
                "quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 20,
                "format": FMT_MUXED,
            }) as ydl:
                info = ydl.extract_info(url, download=False)
            direct_url = info.get("url")
            headers = info.get("http_headers") or {}
        except Exception:
            direct_url = None

        self.vlc_ctrl = VlcController(vlc)
        if direct_url:
            self.root.after(0, lambda: self.status.set(
                f"Starting (VLC direct): {rec['title']}"))
            ok, err = self.vlc_ctrl.play(direct_url, headers)
            if not ok:
                self.vlc_ctrl = None
                self.root.after(0, lambda: messagebox.showerror(
                    "VLC failed", err or "unknown"))
                return
            self.active_player = "vlc"
            self.root.after(0, lambda: self.status.set(
                f"Playing (VLC): {rec['title']}"))
            return

        ytdlp = find_ytdlp_bin()
        ffmpeg = find_ffmpeg()
        if not ytdlp or not ffmpeg:
            self.root.after(0, lambda: messagebox.showerror(
                "Missing dependency",
                "This video needs downloading. Install ffmpeg and yt-dlp."))
            return
        tmp_dir = tempfile.mkdtemp(prefix="yt701_")
        outtmpl = os.path.join(tmp_dir, "video.%(ext)s")
        try:
            self.root.after(0, lambda: self.status.set(
                "Downloading (no pre-muxed 480p)…"))

            def progress(d):
                if d.get("status") == "downloading":
                    pct = d.get("_percent_str", "").strip()
                    if pct:
                        self.root.after(0, lambda p=pct: self.status.set(
                            f"Downloading… {p}"))
            with yt_dlp.YoutubeDL({
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": 30, "format": FMT_MERGED,
                "outtmpl": outtmpl, "progress_hooks": [progress],
                "merge_output_format": "mp4",
            }) as ydl:
                ydl.download([url])
            files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                     if os.path.isfile(os.path.join(tmp_dir, f))]
            if not files:
                raise RuntimeError("Download produced no file")
            self._tmp_dir = tmp_dir
            ok, err = self.vlc_ctrl.play(files[0])
            if not ok:
                self.vlc_ctrl = None
                self.root.after(0, lambda: messagebox.showerror(
                    "VLC failed", err or "unknown"))
                return
            self.active_player = "vlc"
            self.root.after(0, lambda: self.status.set(
                f"Playing (VLC file): {rec['title']}"))
        except Exception as ex:
            err = str(ex)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.root.after(0, lambda: messagebox.showerror(
                "Download failed", err))

# ---------- Main ----------
def main():
    print(f"[yt701] platform: {sys.platform}", file=sys.stderr)
    print(f"[yt701] mpv:      {find_mpv() or 'NOT FOUND'}", file=sys.stderr)
    print(f"[yt701] vlc:      {find_vlc() or 'NOT FOUND'}", file=sys.stderr)
    print(f"[yt701] yt-dlp:   {find_ytdlp_bin() or 'NOT FOUND'} "
          f"(module {YTDLP_VERSION})", file=sys.stderr)
    print(f"[yt701] ffmpeg:   {find_ffmpeg() or 'NOT FOUND'}", file=sys.stderr)
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()