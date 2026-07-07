#!/usr/bin/env bash
# Starts the right streamer for CAMERA_TYPE from the conf:
#   usb → ustreamer (the webcam already compresses MJPEG; the Pi just relays it)
#   csi → stream_csi.py (picamera2 + the Pi's hardware JPEG encoder)
# Both serve the same thing: /stream (MJPEG) and /snapshot (still) on PORT.
set -eu
source /etc/guaucam.conf

case "${CAMERA_TYPE:-usb}" in
  csi)
    exec /usr/bin/python3 /opt/guaucam/src/stream_csi.py
    ;;
  *)
    exec /usr/bin/ustreamer \
      --device="${VIDEO_DEVICE:-/dev/video0}" --format=MJPEG \
      --resolution="${RESOLUTION:-1280x720}" --desired-fps="${FPS:-15}" \
      --host=0.0.0.0 --port="${PORT:-8080}"
    ;;
esac
