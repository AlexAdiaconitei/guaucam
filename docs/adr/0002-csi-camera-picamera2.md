# ADR 0002 — CSI module support with picamera2 + hardware JPEG encoder

## Status

Accepted (2026-07-07)

## Context

Besides the USB webcam (ADR 0001) we want to support a CSI (ribbon) camera
module, specifically a 5MP OV5647 clone (Pi Camera v1.3 type). Two problems:

- On Raspberry Pi OS Bookworm the legacy camera stack (bcm2835-v4l2) was
  removed: CSI modules are only accessible through libcamera. ustreamer reads
  from V4L2 devices and cannot use them.
- The sensor delivers raw Bayer: someone has to compress to JPEG, and the
  Zero W v1's ARMv6 CPU cannot do it in software (ADR 0001).

## Decision

New `stream_csi.py`: **picamera2** with `MJPEGEncoder`, which uses the
VideoCore's **hardware** JPEG encoder (via V4L2 M2M) — the CPU only copies
buffers. It serves the same endpoints as ustreamer (`/stream` multipart MJPEG
and `/snapshot`), so the Panel and the Detector don't change.

`guaucam-stream.service` runs `stream.sh`, which launches ustreamer or
`stream_csi.py` depending on `CAMERA_TYPE` in the conf. The installer detects
the connected cameras (unicam in sysfs = CSI; `/dev/v4l/by-id/usb-*` = UVC)
and asks which one to use if there are several.

## Alternatives considered

- **camera-streamer (ayufan)**: would do exactly this (same endpoints,
  hardware acceleration), but does not support the Pi Zero W v1 — armv7/arm64
  only (Zero 2W onwards).
- **rpicam-vid --codec mjpeg**: rpicam-apps' MJPEG encoder is software;
  unusable on ARMv6.
- **rpicam-vid H.264 → RTSP/HLS**: same reasons as ADR 0001 (latency, needs a
  player, complexity).
- **Re-enabling the legacy stack**: it no longer exists in Bookworm.

## Consequences

- CSI modules have **no microphone**: without an extra USB mic there is no
  Detector and no Alerts. The detector no longer crashes over this: it starts
  the panel, shows "no microphone" and waits for one to appear.
- picamera2 (`python3-picamera2`, tens of MB with numpy/libcamera) is only
  installed when CSI is chosen.
- The Pi Zero's CSI connector has 22 pins: modules shipping the standard
  15-pin cable need the Zero-specific cable/adapter.
- The OV5647's quality/FoV (72°, 5MP) is below the C920's; the stream is still
  MJPEG at ~5-15 Mbps per Viewer, same as before.
