#!/usr/bin/env bash
#
# install.sh — GuauCam installer
#
# Downloads the code from GitHub and sets up the whole system:
#   - Camera MJPEG stream: ustreamer (USB webcam) or picamera2 (CSI module)
#   - Tailscale: private remote access
#   - Noise detector: Telegram alerts + calibration web panel
#
# Detects the connected cameras and, if there is more than one, asks which to use.
#
# Target hardware: Raspberry Pi Zero W (v1) + USB webcam (Logitech C920/C922)
#                  or CSI camera module (OV5647 / Pi Camera and compatible)
# Target OS:       Raspberry Pi OS Lite (Bookworm, 32-bit)
#
# Recommended usage (the Pi downloads everything from the repo):
#   curl -fsSL https://github.com/AlexAdiaconitei/guaucam/raw/main/install.sh | sudo bash
#
# Also works from a local clone of the repo:
#   git clone <repo> && sudo bash guaucam/install.sh
#
# Re-running it updates the code (git pull) without touching your configuration.

set -euo pipefail

REPO_URL="https://github.com/AlexAdiaconitei/guaucam.git"

APP_DIR="/opt/guaucam"
CONF="/etc/guaucam.conf"

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0"; exit 1; }

echo "==> [1/8] Installing packages (ustreamer, alsa-utils, python3, curl, git)..."
apt-get update
if ! apt-get install -y ustreamer alsa-utils python3 curl git; then
    echo "ERROR: packages could not be installed. Is this Raspberry Pi OS Bookworm?" >&2
    exit 1
fi

echo "==> [2/8] Detecting connected cameras..."
# CSI (ribbon module): the kernel creates a "unicam" device if it detects the sensor
CSI_PRESENTE=""
USB_DEV=""
USB_NOMBRE=""
for v in /sys/class/video4linux/video*; do
    [[ -e "$v" ]] || continue
    if grep -qi unicam "$v/name" 2>/dev/null; then
        CSI_PRESENTE=1
    fi
done
# USB webcam (UVC): stable by-id path, doesn't shuffle across reboots
for d in /dev/v4l/by-id/usb-*-video-index0; do
    [[ -e "$d" ]] || continue
    USB_DEV="$d"
    real=$(readlink -f "$d")
    USB_NOMBRE=$(cat "/sys/class/video4linux/$(basename "$real")/name" 2>/dev/null \
                 || echo "USB webcam")
    break
done

CAM_ACTUAL=""
[[ -f "$CONF" ]] && CAM_ACTUAL=$(grep -oP '^CAMERA_TYPE=\K.*' "$CONF" 2>/dev/null || true)

if [[ -n "$CSI_PRESENTE" && -n "$USB_DEV" ]]; then
    echo "    TWO cameras detected:"
    echo "      1) CSI module (ribbon camera)"
    echo "      2) ${USB_NOMBRE} (${USB_DEV})"
    defecto=1; [[ "$CAM_ACTUAL" == "usb" ]] && defecto=2
    eleccion="$defecto"
    if [[ -r /dev/tty ]]; then
        read -rp "    Which one for the video? [${defecto}]: " eleccion < /dev/tty || true
        eleccion=${eleccion:-$defecto}
    fi
    if [[ "$eleccion" == "2" ]]; then CAMERA_TYPE=usb; else CAMERA_TYPE=csi; fi
elif [[ -n "$CSI_PRESENTE" ]]; then
    CAMERA_TYPE=csi
    echo "    CSI module detected (ribbon camera)."
elif [[ -n "$USB_DEV" ]]; then
    CAMERA_TYPE=usb
    echo "    USB webcam detected: ${USB_NOMBRE} (${USB_DEV})"
else
    CAMERA_TYPE="${CAM_ACTUAL:-usb}"
    echo "    ⚠ No camera detected. Continuing with CAMERA_TYPE=${CAMERA_TYPE};"
    echo "      the stream will start as soon as the camera shows up."
    echo "      - USB: check the cable and power (ls /dev/video*)"
    echo "      - CSI: check the ribbon and its orientation. NOTE: the Pi Zero"
    echo "        connector has 22 pins; the standard 15-pin cable does not fit —"
    echo "        you need the Zero-specific cable/adapter. After connecting it,"
    echo "        reboot the Pi and re-run this installer."
fi
if [[ "$CAMERA_TYPE" == "csi" ]]; then
    echo "    Installing CSI support (python3-picamera2)..."
    apt-get install -y --no-install-recommends python3-picamera2
    if [[ -z "$USB_DEV" ]]; then
        echo "    Note: CSI modules have no microphone. Without a USB mic there"
        echo "    will be no noise alerts (video and panel work anyway)."
    fi
fi

echo "==> [3/8] Downloading the code into ${APP_DIR}..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [[ -d "$APP_DIR/.git" ]]; then
    echo "    Already there: updating (git pull)..."
    # /opt/guaucam is a deploy copy (your config lives in /etc): ignore
    # permission-bit drift and discard any local edits so updates never conflict
    git -C "$APP_DIR" config core.fileMode false
    git -C "$APP_DIR" checkout -f -- .
    git -C "$APP_DIR" pull --ff-only
elif [[ -f "$SCRIPT_DIR/src/noise_detector.py" ]]; then
    echo "    Using the local copy of the repo (${SCRIPT_DIR})..."
    mkdir -p "$APP_DIR"
    cp -r "$SCRIPT_DIR/." "$APP_DIR/"
else
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi
chmod +x "$APP_DIR/src/noise_detector.py" "$APP_DIR/src/stream.sh" "$APP_DIR/src/stream_csi.py"

echo "==> [4/8] Installing Tailscale..."
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
    echo
    echo "    Open the URL below on your phone/PC to authorize the Pi:"
    tailscale up
fi

echo "==> [5/8] Telegram setup"
echo "    (Create the bot by talking to @BotFather on Telegram: /newbot gives you the token."
echo "     Then SEND ANY MESSAGE to your bot so your chat_id can be detected.)"
TELEGRAM_TOKEN=""
TELEGRAM_CHAT_ID=""
if [[ -r /dev/tty ]]; then
    read -rp "    Telegram bot token (leave empty to set it up later): " TELEGRAM_TOKEN < /dev/tty || true
    if [[ -n "$TELEGRAM_TOKEN" ]]; then
        read -rp "    chat_id (leave empty to autodetect it): " TELEGRAM_CHAT_ID < /dev/tty || true
        if [[ -z "$TELEGRAM_CHAT_ID" ]]; then
            TELEGRAM_CHAT_ID=$(curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getUpdates" \
                | python3 -c 'import json,sys
try:
    r = json.load(sys.stdin)["result"]
    print(r[-1]["message"]["chat"]["id"] if r else "")
except Exception:
    print("")' ) || TELEGRAM_CHAT_ID=""
            if [[ -n "$TELEGRAM_CHAT_ID" ]]; then
                echo "    chat_id detected: $TELEGRAM_CHAT_ID"
            else
                echo "    Could not autodetect it (no message to your bot yet?). No problem:"
                echo "    send it any message and then, on the web panel, press"
                echo "    «Detect chat_id» in the Telegram alerts section."
            fi
        fi
    fi
else
    echo "    (No interactive terminal: set up Telegram later from the web panel)"
fi

echo "==> [6/8] Ports"
# Defaults come from the existing conf on re-installs, so Enter keeps your setup
PORT_DEF="8080"
PANEL_PORT_DEF="8081"
if [[ -f "$CONF" ]]; then
    PORT_DEF=$(grep -oP '^PORT=\K.*' "$CONF" 2>/dev/null || echo 8080)
    PANEL_PORT_DEF=$(grep -oP '^PANEL_PORT=\K.*' "$CONF" 2>/dev/null || echo 8081)
fi
PORT_NUEVO="$PORT_DEF"
PANEL_PORT_NUEVO="$PANEL_PORT_DEF"
if [[ -r /dev/tty ]]; then
    read -rp "    Video stream port [${PORT_DEF}]: " PORT_NUEVO < /dev/tty || true
    PORT_NUEVO=${PORT_NUEVO:-$PORT_DEF}
    read -rp "    Web panel port [${PANEL_PORT_DEF}]: " PANEL_PORT_NUEVO < /dev/tty || true
    PANEL_PORT_NUEVO=${PANEL_PORT_NUEVO:-$PANEL_PORT_DEF}
fi
es_puerto() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )); }
if ! es_puerto "$PORT_NUEVO"; then
    echo "    Invalid stream port '${PORT_NUEVO}': keeping ${PORT_DEF}."
    PORT_NUEVO="$PORT_DEF"
fi
if ! es_puerto "$PANEL_PORT_NUEVO"; then
    echo "    Invalid panel port '${PANEL_PORT_NUEVO}': keeping ${PANEL_PORT_DEF}."
    PANEL_PORT_NUEVO="$PANEL_PORT_DEF"
fi
if [[ "$PANEL_PORT_NUEVO" == "$PORT_NUEVO" ]]; then
    PANEL_PORT_NUEVO=$((PORT_NUEVO + 1))
    echo "    The panel port must differ from the stream port: using ${PANEL_PORT_NUEVO}."
fi

if [[ -f "$CONF" ]]; then
    echo "    $CONF already exists: keeping it (only camera, ports and Telegram get updated)."
    grep -q '^PORT=' "$CONF" || echo "PORT=8080" >> "$CONF"
    grep -q '^PANEL_PORT=' "$CONF" || echo "PANEL_PORT=8081" >> "$CONF"
    grep -q '^CAMERA_TYPE=' "$CONF" || echo "CAMERA_TYPE=usb" >> "$CONF"
    grep -q '^VIDEO_DEVICE=' "$CONF" || echo "VIDEO_DEVICE=/dev/video0" >> "$CONF"
else
    cp "$APP_DIR/src/guaucam.conf" "$CONF"
    chmod 600 "$CONF"   # it will hold the Telegram token
fi
sed -i "s|^PORT=.*|PORT=${PORT_NUEVO}|" "$CONF"
sed -i "s|^PANEL_PORT=.*|PANEL_PORT=${PANEL_PORT_NUEVO}|" "$CONF"
sed -i "s|^CAMERA_TYPE=.*|CAMERA_TYPE=${CAMERA_TYPE}|" "$CONF"
if [[ -n "$USB_DEV" ]]; then
    sed -i "s|^VIDEO_DEVICE=.*|VIDEO_DEVICE=${USB_DEV}|" "$CONF"
fi
if [[ -n "$TELEGRAM_TOKEN" ]]; then
    sed -i "s|^TELEGRAM_TOKEN=.*|TELEGRAM_TOKEN=${TELEGRAM_TOKEN}|" "$CONF"
    sed -i "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}|" "$CONF"
fi

echo "==> [7/8] Installing systemd services..."
cp "$APP_DIR/src/guaucam-stream.service" "$APP_DIR/src/guaucam-detector.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable guaucam-stream.service guaucam-detector.service
systemctl restart guaucam-stream.service guaucam-detector.service

echo "==> [8/8] Done."
source "$CONF"
if [[ -n "${TELEGRAM_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -s --max-time 15 \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=✅ GuauCam installed. I'll alert you when noise stays above ${THRESHOLD_DB} dB." \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" >/dev/null || true
    AVISOS_MSG="active (test message sent to your Telegram)"
else
    AVISOS_MSG="DISABLED: open the web panel, «Telegram alerts» section:
                 save the token, send any message to your bot and press
                 «Detect chat_id». It activates instantly, no restart."
fi

TS_IP=$(tailscale ip -4 2>/dev/null | head -n1 || echo "?")
HOST=$(hostname)
if [[ "$CAMERA_TYPE" == "csi" ]]; then
    CAMARA_MSG="CSI module (picamera2 + hardware encoder)"
else
    CAMARA_MSG="USB webcam ${USB_NOMBRE:+(${USB_NOMBRE}) }(ustreamer)"
fi
cat <<EOF

════════════════════════════════════════════════════════════════
  INSTALLATION COMPLETE
════════════════════════════════════════════════════════════════

  Camera in use: ${CAMARA_MSG}
  (Swapping cameras? Re-run this installer: it detects them again.)

  Panel (video + live noise meter + threshold settings):
      http://${HOST}:${PANEL_PORT}/     (MagicDNS)
      http://${TS_IP}:${PANEL_PORT}/    (Tailscale IP)

  Video only:
      http://${HOST}:${PORT}/
      Still photo:  http://${HOST}:${PORT}/snapshot

  Telegram alerts: ${AVISOS_MSG}

  Calibrating the threshold: open the panel, watch where the bar sits
  in silence and where it goes with barking, and set the threshold in
  between. It saves and applies instantly, no restart.
  (SSH alternative: python3 ${APP_DIR}/src/noise_detector.py --monitor
   with the service stopped.)

  Updating to the latest version of the repo: re-run
      sudo bash ${APP_DIR}/install.sh

  Changing resolution/fps/ports: edit ${CONF} and
      sudo systemctl restart guaucam-stream guaucam-detector

  Logs:
      journalctl -u guaucam-stream -u guaucam-detector -f
════════════════════════════════════════════════════════════════
EOF
