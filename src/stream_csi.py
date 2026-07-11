#!/usr/bin/env python3
"""MJPEG stream for CSI (ribbon) camera modules, via picamera2/libcamera.

The ustreamer equivalent for ribbon cameras: serves /stream (multipart MJPEG)
and /snapshot (current-instant JPEG) on the conf's PORT. JPEG compression is
done by the VideoCore hardware encoder (MJPEGEncoder), not the CPU: the Zero W
v1 (ARMv6) could not compress in software.

Also accepts POST /controls (JSON with CAM_*/BITRATE keys) to adjust the image
live; the panel server validates, persists to the conf and forwards here.

Started by stream.sh when CAMERA_TYPE=csi.
"""
import io
import json
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

# conf key → (libcamera control, cast). CAM_GAIN is handled apart (0 = auto).
CONTROL_MAP = {
    "CAM_AWB": ("AwbMode", int),
    "CAM_BRIGHTNESS": ("Brightness", float),
    "CAM_CONTRAST": ("Contrast", float),
    "CAM_SATURATION": ("Saturation", float),
    "CAM_SHARPNESS": ("Sharpness", float),
    "CAM_DENOISE": ("NoiseReductionMode", int),
}

picam = None
cam_lock = threading.Lock()          # serializes set_controls/encoder swaps
cam_state = {"bitrate": 0, "controls": {}}


def camera_controls(values):
    """libcamera control dict from the CAM_* keys present in values."""
    ctrl = {}
    for key, (name, cast) in CONTROL_MAP.items():
        if key in values:
            ctrl[name] = cast(float(values[key]))
    if "CAM_GAIN" in values:
        gain = float(values["CAM_GAIN"])
        ctrl["AnalogueGain"] = gain
        if gain == 0:  # 0/0 hands gain and shutter back to the AGC
            ctrl["ExposureTime"] = 0
    return ctrl


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

    def do_POST(self):
        if self.path != "/controls":
            return self.send_error(404)
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            values = json.loads(raw)
            ctrl = camera_controls(values)
            bitrate = int(float(values["BITRATE"])) if "BITRATE" in values else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.send_error(400)
        with cam_lock:
            cam_state["controls"].update(ctrl)
            if bitrate is not None and bitrate != cam_state["bitrate"]:
                # the bitrate lives in the encoder: swap it (viewers freeze
                # for an instant and resume on the next frame)
                picam.stop_recording()
                picam.start_recording(MJPEGEncoder(bitrate=bitrate),
                                      FileOutput(output))
                cam_state["bitrate"] = bitrate
                picam.set_controls(cam_state["controls"])
            elif ctrl:
                picam.set_controls(ctrl)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global picam
    cfg = read_config()
    width, height = (int(x) for x in cfg.get("RESOLUTION", "1280x720").split("x"))
    fps = float(cfg.get("FPS", "15"))
    port = int(cfg.get("PORT", "8080"))
    bitrate = int(cfg.get("BITRATE", "4000000"))

    picam = Picamera2()
    picam.configure(picam.create_video_configuration(
        main={"size": (width, height), "format": "YUV420"},
        controls={"FrameRate": fps}))
    picam.start_recording(MJPEGEncoder(bitrate=bitrate), FileOutput(output))
    cam_state["bitrate"] = bitrate
    cam_state["controls"] = camera_controls(cfg)
    if cam_state["controls"]:
        picam.set_controls(cam_state["controls"])

    print(f"CSI stream {width}x{height}@{fps:g} ~{bitrate / 1e6:g} Mbps on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Stream).serve_forever()


if __name__ == "__main__":
    main()
