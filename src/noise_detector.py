#!/usr/bin/env python3
"""Noise detector + calibration web panel.

Measures the mic level (the USB webcam's, or a separate USB mic); if it stays
above the threshold for long enough, sends a Telegram alert with a photo. Also
serves a web panel with the video, the live level and the settings (applied
instantly, no restart). Without a microphone (e.g. a CSI-only camera) the panel
still works and the meter waits until one is plugged in.

Usage:
  noise_detector.py            service mode (audio + web panel)
  noise_detector.py --monitor  live level in the terminal (SSH alternative)
"""
import io
import json
import math
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)
import audioop  # stdlib in Python 3.11 (Bookworm)

CONF = "/etc/guaucam.conf"
RATE = 16000                                # Hz, mono, S16_LE
WINDOW_SEC = 0.25
WINDOW_BYTES = int(RATE * WINDOW_SEC) * 2
NUMERIC_FIELDS = ("THRESHOLD_DB", "SUSTAINED_SECONDS", "COOLDOWN_SECONDS")
# Text fields editable from the panel; strict validation because they go to the conf
TOKEN_RE = re.compile(r"^\d+:[\w-]+$")
CHAT_ID_RE = re.compile(r"^-?\d+$")

HELP_TEXT = (
    "🐶 GuauCam bot commands:\n"
    "/screenshot — a still photo right now\n"
    "/video — a short clip (a few seconds, GIF)\n"
    "/live — link to the live feed\n"
    "/help — this message"
)

cfg = {}
cfg_lock = threading.Lock()
state = {"db": None}  # None = no microphone (the panel shows it)
# The bot command loop records here the last chat that messaged the bot, so the
# panel's "Detect chat_id" button can add it (the loop is the sole getUpdates
# consumer, so the panel can no longer poll Telegram on its own).
last_seen = {"id": None}

PAGE = (Path(__file__).with_name("panel.html")).read_text(encoding="utf-8")


def read_config():
    conf = {}
    with open(CONF) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def save_config(changes):
    """Rewrites the changed keys, keeping comments and order; appends new ones."""
    with open(CONF) as f:
        lines = f.readlines()
    written = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in changes:
            lines[i] = f"{key}={changes[key]}\n"
            written.add(key)
    for key, value in changes.items():
        if key not in written:  # e.g. TELEGRAM_CHAT_IDS on an old conf
            lines.append(f"{key}={value}\n")
    with open(CONF, "w") as f:
        f.writelines(lines)


def parse_chat_ids(conf):
    """Registered chats as a de-duped list. Falls back to the legacy single
    TELEGRAM_CHAT_ID so old confs keep working before the panel resaves them."""
    raw = conf.get("TELEGRAM_CHAT_IDS") or conf.get("TELEGRAM_CHAT_ID") or ""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in ids:
            ids.append(part)
    return ids


def detect_mic():
    """First ALSA capture device, or None if there is none."""
    out = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    m = re.search(r"card (\d+):.*?device (\d+):", out)
    return f"plughw:{m.group(1)},{m.group(2)}" if m else None


def level_db(chunk):
    rms = audioop.rms(chunk, 2)
    return 20 * math.log10(rms / 32768) if rms > 0 else -90.0


# ── Telegram helpers ──────────────────────────────────────────────────────
# All talk to the Bot API through curl (no extra dependency). The token is read
# under the lock at each call so it can be changed from the panel without a
# restart.

def _api(method):
    with cfg_lock:
        token = cfg.get("TELEGRAM_TOKEN", "")
    return token and f"https://api.telegram.org/bot{token}/{method}"


def tg_send_message(chat_id, text):
    url = _api("sendMessage")
    if not url:
        return False
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20",
         "-d", f"chat_id={chat_id}", "--data-urlencode", f"text={text}", url],
        capture_output=True)
    return r.returncode == 0


def tg_send_file(method, field, chat_id, path, caption=""):
    url = _api(method)
    if not url:
        return False
    r = subprocess.run(
        ["curl", "-s", "--max-time", "45",
         "-F", f"chat_id={chat_id}", "-F", f"{field}=@{path}",
         "-F", f"caption={caption}", url],
        capture_output=True)
    return r.returncode == 0


def fetch_snapshot_bytes(port, timeout=5):
    """Current-instant JPEG from the local stream, or None if it's down."""
    r = subprocess.run(
        ["curl", "-sf", "--max-time", str(timeout),
         f"http://127.0.0.1:{port}/snapshot"],
        capture_output=True)
    return r.stdout if r.returncode == 0 else None


def send_alert(db):
    with cfg_lock:
        threshold, port = cfg.get("THRESHOLD_DB", "?"), cfg.get("PORT", "8080")
        chats = parse_chat_ids(cfg)
    text = f"🐶 Noise at home! Level: {db:.1f} dB (threshold {threshold} dB)"
    photo = "/tmp/alert.jpg"
    frame = fetch_snapshot_bytes(port)
    if frame:
        Path(photo).write_bytes(frame)
    for chat in chats:  # every registered chat gets the alert; isolate failures
        if frame:
            tg_send_file("sendPhoto", "photo", chat, photo, text)
        else:  # stream down or slow network: send at least the text
            tg_send_message(chat, text)


# ── Bot command loop ──────────────────────────────────────────────────────

def cmd_screenshot(chat_id):
    with cfg_lock:
        port = cfg.get("PORT", "8080")
    frame = fetch_snapshot_bytes(port)
    if not frame:
        return tg_send_message(chat_id, "✖ Snapshot unavailable (stream down?)")
    path = "/tmp/guaucam_shot.jpg"
    Path(path).write_bytes(frame)
    tg_send_file("sendPhoto", "photo", chat_id, path, "📸 Snapshot")


def cmd_live(chat_id):
    with cfg_lock:
        panel_port = cfg.get("PANEL_PORT", "8081")
    host = socket.gethostname()
    tg_send_message(
        chat_id,
        "📺 Live feed (only inside the Tailnet / home LAN):\n"
        f"http://{host}:{panel_port}/\n"
        "Telegram can't embed the live stream, so open the panel link.")


def cmd_video(chat_id):
    """Short animated GIF from a burst of snapshots. Camera-agnostic (works the
    same for USB and CSI) and needs no ffmpeg — just Pillow."""
    try:
        from PIL import Image
    except ImportError:
        tg_send_message(chat_id, "🎥 Video needs Pillow (python3-pil); "
                                 "sending a snapshot instead.")
        return cmd_screenshot(chat_id)
    with cfg_lock:
        port = cfg.get("PORT", "8080")
    tg_send_message(chat_id, "🎥 Recording a short clip...")
    frames = []
    for _ in range(12):  # ~3 s of wall-clock, weak WiFi permitting
        raw = fetch_snapshot_bytes(port)
        if raw:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((640, 640))  # keep it light for the Zero W + Telegram
                frames.append(im)
            except Exception:
                pass
        time.sleep(0.2)
    if not frames:
        return tg_send_message(chat_id, "✖ Clip failed (stream down?)")
    path = "/tmp/guaucam_clip.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=250, loop=0, optimize=True)
    tg_send_file("sendAnimation", "animation", chat_id, path, "🎥 Last few seconds")


COMMANDS = {
    "/screenshot": cmd_screenshot,
    "/photo": cmd_screenshot,
    "/video": cmd_video,
    "/live": cmd_live,
}


def handle_command(chat_id, text):
    if not text.startswith("/"):
        return  # not a command: ignore (last_seen was already recorded)
    cmd = text.split()[0].lower().split("@")[0]  # tolerate /cmd@BotName
    with cfg_lock:
        authorized = chat_id in parse_chat_ids(cfg)
    if cmd in ("/start", "/help"):
        tg_send_message(chat_id, HELP_TEXT if authorized else
                        f"🐶 GuauCam bot.\nYour chat_id is {chat_id}.\n"
                        "Ask the owner to add it in the panel to use the camera.")
        return
    if not authorized:
        return tg_send_message(chat_id, f"⛔ Not authorized. Your chat_id: {chat_id}")
    action = COMMANDS.get(cmd)
    if action:
        action(chat_id)
    else:
        tg_send_message(chat_id, "Unknown command. /help")


def bot_loop():
    """Long-polls Telegram for commands. Sole getUpdates consumer. Drains the
    backlog on startup (records who wrote but does not run stale commands)."""
    offset = None
    draining = True
    while True:
        with cfg_lock:
            token = cfg.get("TELEGRAM_TOKEN", "")
        if not token:
            time.sleep(5)  # no bot configured yet; the panel may set it live
            continue
        args = ["curl", "-s", "--max-time", "40", "-d", "timeout=30"]
        if offset is not None:
            args += ["-d", f"offset={offset}"]
        args.append(f"https://api.telegram.org/bot{token}/getUpdates")
        try:
            r = subprocess.run(args, capture_output=True, text=True)
            updates = json.loads(r.stdout).get("result", [])
        except (ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_id = msg.get("chat", {}).get("id")
            if chat_id is None:
                continue
            last_seen["id"] = str(chat_id)
            if not draining:
                handle_command(str(chat_id), (msg.get("text") or "").strip())
        draining = False


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # don't flood the journal with every GET

    def _reply(self, body, ctype, code=200):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser closed the connection mid-reply (tab closed,
                  # phone locked...): normal, not an error

    def _json(self, obj, code=200):
        self._reply(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        if self.path == "/":
            self._reply(PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/level":
            with cfg_lock:
                threshold = float(cfg.get("THRESHOLD_DB", "-25"))
            self._json({"db": state["db"], "threshold": threshold})
        elif self.path == "/api/level/stream":
            # Server-Sent Events: one persistent connection instead of the
            # browser polling several times per second (each poll used to open
            # a new TCP connection + thread). EventSource reconnects on its own.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    with cfg_lock:
                        threshold = float(cfg.get("THRESHOLD_DB", "-25"))
                    data = json.dumps({"db": state["db"], "threshold": threshold})
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                pass  # viewer left
        elif self.path == "/snapshot":
            # Same-origin proxy to the stream's snapshot, so the panel can
            # offer a screenshot download without CORS issues
            with cfg_lock:
                port = cfg.get("PORT", "8080")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/snapshot", timeout=5) as r:
                    self._reply(r.read(), "image/jpeg")
            except OSError:
                self._json({"error": "snapshot unavailable (stream down?)"}, 502)
        elif self.path == "/api/config":
            with cfg_lock:
                data = {k: cfg.get(k, "") for k in NUMERIC_FIELDS}
                data["PORT"] = cfg.get("PORT", "8080")
                data["TELEGRAM_CHAT_IDS"] = parse_chat_ids(cfg)
                # the token never travels to the browser: only whether it's set
                data["TELEGRAM_TOKEN_SET"] = bool(cfg.get("TELEGRAM_TOKEN"))
            self._json(data)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/telegram/detect":
            return self._detect_chat()
        if self.path == "/api/telegram/test":
            return self._test_telegram()
        if self.path != "/api/config":
            return self._json({"error": "not found"}, 404)
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(raw)
            changes = {k: str(float(data[k])) for k in NUMERIC_FIELDS if k in data}
            token = str(data.get("TELEGRAM_TOKEN", "")).strip()
            if token:  # empty = keep what's saved
                if not TOKEN_RE.match(token):
                    return self._json({"error": "invalid TELEGRAM_TOKEN format"}, 400)
                changes["TELEGRAM_TOKEN"] = token
            if "TELEGRAM_CHAT_IDS" in data:  # full replacement list (may be empty)
                items = data["TELEGRAM_CHAT_IDS"]
                if isinstance(items, str):
                    items = items.split(",")
                ids = []
                for v in items:
                    v = str(v).strip()
                    if not v:
                        continue
                    if not CHAT_ID_RE.match(v):
                        return self._json({"error": f"invalid chat_id: {v}"}, 400)
                    if v not in ids:
                        ids.append(v)
                changes["TELEGRAM_CHAT_IDS"] = ",".join(ids)
            if not changes:
                raise ValueError
        except (ValueError, KeyError, AttributeError, json.JSONDecodeError):
            return self._json({"error": "invalid values"}, 400)
        with cfg_lock:
            cfg.update(changes)      # applies instantly in the audio loop
            save_config(changes)     # and persists for the next boot
        self._json({"ok": True})

    def _detect_chat(self):
        """Adds the last chat that messaged the bot. The bot command loop is the
        sole getUpdates consumer, so we read the id it recorded (last_seen)."""
        with cfg_lock:
            token = cfg.get("TELEGRAM_TOKEN", "")
        if not token:
            return self._json({"error": "save the bot token first"}, 400)
        chat_id = last_seen["id"]
        if not chat_id:
            return self._json({"error": "no messages found: send anything to "
                                        "your bot on Telegram and retry"}, 404)
        with cfg_lock:
            ids = parse_chat_ids(cfg)
            if chat_id not in ids:
                ids.append(chat_id)
            joined = ",".join(ids)
            cfg["TELEGRAM_CHAT_IDS"] = joined
            save_config({"TELEGRAM_CHAT_IDS": joined})
        self._json({"ok": True, "chat_id": chat_id, "chat_ids": ids})

    def _test_telegram(self):
        with cfg_lock:
            token = cfg.get("TELEGRAM_TOKEN")
            chats = parse_chat_ids(cfg)
        if not (token and chats):
            return self._json({"error": "token or chat_id missing"}, 400)
        ok = False
        for chat in chats:  # test message to every registered chat
            ok = tg_send_message(chat, "🐶 GuauCam test: alerts are working.") or ok
        if not ok:
            return self._json({"error": "Telegram rejected the message: check token and chat_id"}, 502)
        self._json({"ok": True})


def main():
    global cfg
    monitor = "--monitor" in sys.argv
    cfg = read_config()

    device = cfg.get("AUDIO_DEVICE", "auto")
    if device in ("", "auto"):
        device = detect_mic()

    if monitor and not device:
        print("No audio capture device. Is a USB mic/webcam connected?",
              file=sys.stderr)
        sys.exit(1)

    if not monitor:
        if not (cfg.get("TELEGRAM_TOKEN") and parse_chat_ids(cfg)):
            print("Telegram not configured: NO alerts will be sent. Set it up "
                  f"from the web panel (Telegram alerts section) or in {CONF}.",
                  file=sys.stderr)
        panel_port = int(cfg.get("PANEL_PORT", "8081"))
        server = ThreadingHTTPServer(("0.0.0.0", panel_port), Panel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Web panel on port {panel_port}")
        # Bot command loop (/screenshot, /video, /live). Idles until a token is
        # set; it re-reads the conf each pass, so the panel enables it live.
        threading.Thread(target=bot_loop, daemon=True).start()

    if not device:
        # CSI camera with no mic, or webcam unplugged: the panel and the video
        # keep working; wait for a USB mic to show up without crashing.
        print("No microphone: no meter or alerts until a USB mic/webcam is "
              "connected. Video and panel keep working.", file=sys.stderr)
        while not device:
            time.sleep(30)
            device = detect_mic()
        print(f"Microphone detected: {device}", file=sys.stderr)

    state["db"] = -90.0
    windows_above = 0
    last_alert = 0.0

    if monitor:
        with cfg_lock:
            print(f"Mic: {device} | threshold: {cfg.get('THRESHOLD_DB')} dB | Ctrl+C to exit")

    proc = subprocess.Popen(
        ["arecord", "-q", "-D", device, "-f", "S16_LE",
         "-r", str(RATE), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)

    while True:
        chunk = proc.stdout.read(WINDOW_BYTES)
        if not chunk or len(chunk) < WINDOW_BYTES:
            print("arecord exited (webcam unplugged?)", file=sys.stderr)
            sys.exit(1)  # systemd restarts us
        db = level_db(chunk)
        state["db"] = db

        with cfg_lock:
            threshold = float(cfg.get("THRESHOLD_DB", "-25"))
            sustained = float(cfg.get("SUSTAINED_SECONDS", "3"))
            cooldown = float(cfg.get("COOLDOWN_SECONDS", "300"))
            # re-read every pass: configuring Telegram from the panel
            # enables alerts instantly, no restart
            telegram_ok = bool(cfg.get("TELEGRAM_TOKEN") and parse_chat_ids(cfg))

        if monitor:
            bar = "#" * max(0, min(50, int((db + 60) / 60 * 50)))
            mark = "  <-- ABOVE THRESHOLD" if db >= threshold else ""
            print(f"{db:6.1f} dB |{bar:<50}|{mark}")
            continue

        windows_above = windows_above + 1 if db >= threshold else 0
        windows_needed = max(1, round(sustained / WINDOW_SEC))
        if windows_above >= windows_needed and time.time() - last_alert >= cooldown:
            if telegram_ok:
                send_alert(db)
            last_alert = time.time()
            windows_above = 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
