#!/usr/bin/env python3
"""MJPEG stream for CSI (ribbon) camera modules, via picamera2/libcamera.

The ustreamer equivalent for ribbon cameras: serves /stream (multipart MJPEG)
and /snapshot (current-instant JPEG) on the conf's PORT. JPEG compression is
done by the VideoCore hardware encoder (MJPEGEncoder), not the CPU: the Zero W
v1 (ARMv6) could not compress in software.

Started by stream.sh when CAMERA_TYPE=csi.
"""
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

CONF = "/etc/guaucam.conf"


def read_config():
    conf = {}
    with open(CONF) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


class LatestFrame(io.BufferedIOBase):
    """The encoder writes each JPEG here; clients wait for the next one."""

    def __init__(self):
        self.frame = None
        self.ready = threading.Condition()

    def write(self, buf):
        with self.ready:
            self.frame = buf
            self.ready.notify_all()


output = LatestFrame()


class Stream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # don't flood the journal with every GET

    def _next_frame(self):
        with output.ready:
            output.ready.wait()
            return output.frame

    def do_GET(self):
        if self.path == "/snapshot":
            frame = self._next_frame()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
        elif self.path in ("/", "/stream"):
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    frame = self._next_frame()
                    self.wfile.write(b"--FRAME\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # the viewer closed the tab
        else:
            self.send_error(404)


def main():
    cfg = read_config()
    width, height = (int(x) for x in cfg.get("RESOLUTION", "1280x720").split("x"))
    fps = float(cfg.get("FPS", "15"))
    port = int(cfg.get("PORT", "8080"))

    picam = Picamera2()
    picam.configure(picam.create_video_configuration(
        main={"size": (width, height), "format": "YUV420"},
        controls={"FrameRate": fps}))
    picam.start_recording(MJPEGEncoder(), FileOutput(output))

    print(f"CSI stream {width}x{height}@{fps:g} on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Stream).serve_forever()


if __name__ == "__main__":
    main()
