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
import json
import math
import re
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
TELEGRAM_FORMAT = {
    "TELEGRAM_TOKEN": re.compile(r"^\d+:[\w-]+$"),
    "TELEGRAM_CHAT_ID": re.compile(r"^-?\d+$"),
}

cfg = {}
cfg_lock = threading.Lock()
state = {"db": None}  # None = no microphone (the panel shows it)

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
    """Rewrites only the changed keys, keeping comments and order."""
    with open(CONF) as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in changes:
            lines[i] = f"{key}={changes[key]}\n"
    with open(CONF, "w") as f:
        f.writelines(lines)


def detect_mic():
    """First ALSA capture device, or None if there is none."""
    out = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    m = re.search(r"card (\d+):.*?device (\d+):", out)
    return f"plughw:{m.group(1)},{m.group(2)}" if m else None


def level_db(chunk):
    rms = audioop.rms(chunk, 2)
    return 20 * math.log10(rms / 32768) if rms > 0 else -90.0


def send_alert(db):
    with cfg_lock:
        token, chat = cfg.get("TELEGRAM_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
        threshold, port = cfg.get("THRESHOLD_DB", "?"), cfg.get("PORT", "8080")
    text = f"🐶 Noise at home! Level: {db:.1f} dB (threshold {threshold} dB)"
    photo = "/tmp/alert.jpg"
    try:
        subprocess.run(
            ["curl", "-sf", "-o", photo, "--max-time", "5",
             f"http://127.0.0.1:{port}/snapshot"],
            check=True)
        subprocess.run(
            ["curl", "-sf", "--max-time", "20",
             "-F", f"chat_id={chat}", "-F", f"photo=@{photo}",
             "-F", f"caption={text}",
             f"https://api.telegram.org/bot{token}/sendPhoto"],
            check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # No photo (stream down or slow network): send at least the text
        subprocess.run(
            ["curl", "-s", "--max-time", "20",
             "-d", f"chat_id={chat}", "-d", f"text={text}",
             f"https://api.telegram.org/bot{token}/sendMessage"],
            capture_output=True)


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
                data["TELEGRAM_CHAT_ID"] = cfg.get("TELEGRAM_CHAT_ID", "")
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
            for k, fmt in TELEGRAM_FORMAT.items():
                v = str(data.get(k, "")).strip()
                if not v:
                    continue  # empty field = keep what's saved
                if not fmt.match(v):
                    return self._json({"error": f"invalid {k} format"}, 400)
                changes[k] = v
            if not changes:
                raise ValueError
        except (ValueError, KeyError, AttributeError, json.JSONDecodeError):
            return self._json({"error": "invalid values"}, 400)
        with cfg_lock:
            cfg.update(changes)      # applies instantly in the audio loop
            save_config(changes)     # and persists for the next boot
        self._json({"ok": True})

    def _detect_chat(self):
        """Pulls the chat_id from the last message sent to the bot (getUpdates)."""
        with cfg_lock:
            token = cfg.get("TELEGRAM_TOKEN", "")
        if not token:
            return self._json({"error": "save the bot token first"}, 400)
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "10",
             f"https://api.telegram.org/bot{token}/getUpdates"],
            capture_output=True, text=True)
        chat_id = None
        try:
            for upd in reversed(json.loads(r.stdout)["result"]):
                m = upd.get("message") or upd.get("edited_message") or {}
                if "id" in m.get("chat", {}):
                    chat_id = str(m["chat"]["id"])
                    break
        except (ValueError, KeyError, json.JSONDecodeError):
            return self._json({"error": "Telegram not responding: wrong token?"}, 502)
        if not chat_id:
            return self._json({"error": "no messages found: send anything to "
                                        "your bot on Telegram and retry"}, 404)
        with cfg_lock:
            cfg["TELEGRAM_CHAT_ID"] = chat_id
            save_config({"TELEGRAM_CHAT_ID": chat_id})
        self._json({"ok": True, "chat_id": chat_id})

    def _test_telegram(self):
        with cfg_lock:
            token, chat = cfg.get("TELEGRAM_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return self._json({"error": "token or chat_id missing"}, 400)
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "15",
             "-d", f"chat_id={chat}", "-d", "text=🐶 GuauCam test: alerts are working.",
             f"https://api.telegram.org/bot{token}/sendMessage"],
            capture_output=True)
        if r.returncode != 0:
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
        if not (cfg.get("TELEGRAM_TOKEN") and cfg.get("TELEGRAM_CHAT_ID")):
            print("Telegram not configured: NO alerts will be sent. Set it up "
                  f"from the web panel (Telegram alerts section) or in {CONF}.",
                  file=sys.stderr)
        panel_port = int(cfg.get("PANEL_PORT", "8081"))
        server = ThreadingHTTPServer(("0.0.0.0", panel_port), Panel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Web panel on port {panel_port}")

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
            telegram_ok = bool(cfg.get("TELEGRAM_TOKEN") and cfg.get("TELEGRAM_CHAT_ID"))

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
