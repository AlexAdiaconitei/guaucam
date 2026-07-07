# ADR 0001 — MJPEG passthrough with ustreamer, not H.264/RTSP

## Status

Accepted (2026-07-05)

## Context

The Station is a Pi Zero W v1: 1 ARMv6 core at 1GHz, 512MB RAM, 2.4GHz-only WiFi (~20-30 Mbps real). It cannot transcode video in software, and its hardware H.264 encoder is not reliably usable from a USB source in this pipeline. Recent C920s no longer expose H.264 over USB (Logitech removed it in newer revisions), so it cannot be counted on.

The C920/C922 does deliver compressed MJPEG over USB natively (UVC).

## Decision

The camera compresses (MJPEG) and the Pi only relays: **ustreamer** reads MJPEG from `/dev/video0` and serves it over HTTP without touching the frames. Default resolution 1280x720 @ 15fps to fit the 2.4GHz WiFi. No audio.

## Alternatives considered

- **ffmpeg → RTSP/HLS (H.264)**: requires transcoding; impossible on a single ARMv6 core. Plus 5-15s latency with HLS.
- **motion / motionEye**: recompresses every frame on the CPU; the Zero W chokes even at 640x480.
- **mjpg-streamer**: same idea as ustreamer but no apt package (manual build) and no active maintenance.

## Consequences

- Latency <1s, opens in any browser, CPU nearly idle.
- No audio (MJPEG does not carry it). Explicitly accepted.
- MJPEG uses more bandwidth than H.264: each simultaneous Viewer costs ~5-15 Mbps. With the Zero W's WiFi, 1-2 Viewers at a time max.
- If audio or recording is ever wanted, different hardware is needed (Pi Zero 2 W or better).
