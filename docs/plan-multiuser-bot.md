# Plan — multiple Telegram users + bot commands

Status: implemented on branch `feat/multiuser-bot-commands`.

## Goals

1. Register **several Telegram chats** to the same camera (owner + family). Every
   registered chat gets the noise Alerts.
2. Add **bot commands**: `/screenshot`, `/live`, `/video`, `/help`.

## Decisions (asked to the owner)

- **Registration model: owner-managed from the Panel.** No self-service `/start`
  registration. The owner adds/removes chat_ids in the Telegram-alerts section of
  the web Panel. The registered list doubles as the **authorized set** for bot
  commands: only listed chats can drive the camera.
- **Camera: both USB and CSI must work.** So `/video` is implemented in a
  camera-agnostic way (see below) instead of the CSI-only hardware-H264 path.

## Live vs. video — feasibility

- **Live embedded in Telegram: not possible.** Telegram can't play the MJPEG
  stream inline. `/live` replies with the Panel URL (only reachable inside the
  Tailnet / home LAN); the Panel *is* the live feed.
- **Video clip: possible.** A hardware-H264 mp4 from the CSI module would mean
  a second encoder on the camera the stream process already owns (single-owner
  constraint) plus ffmpeg for the MP4 container — fragile and USB-incompatible.
  Chosen instead: **short animated GIF** built with Pillow from a burst of
  `/snapshot` grabs off the stream port. Camera-agnostic (identical for USB and
  CSI), no ffmpeg, no camera-pipeline surgery. Downscaled + low fps to fit the
  Zero W's WiFi and Telegram limits.

## Changes

### `src/guaucam.conf`
- `TELEGRAM_CHAT_ID` → `TELEGRAM_CHAT_IDS` (comma-separated list).

### `src/noise_detector.py`
- `parse_chat_ids(cfg)` — reads `TELEGRAM_CHAT_IDS`, falls back to the old
  `TELEGRAM_CHAT_ID` for back-compat. De-dupes.
- `save_config` now appends keys that are not yet in the file (so the new
  `TELEGRAM_CHAT_IDS` key gets written on old installs).
- Telegram helpers: `tg_send_message` / `tg_send_photo` / `tg_send_animation`,
  `fetch_snapshot` / `fetch_snapshot_bytes`.
- `send_alert` loops over every registered chat; one failure doesn't block others.
- **Command loop** (`bot_loop`, daemon thread, service mode only): long-polls
  `getUpdates` with a persistent offset. Sole consumer of updates. Records the
  last-seen chat_id for the Panel's "Detect" button. Drains the backlog on
  startup without executing stale commands. Only acts on commands from chats in
  the registered list; unknown chats get their own chat_id echoed back so the
  owner can add it.
- Commands: `/screenshot` (sendPhoto of a snapshot), `/live` (Panel URL),
  `/video` (GIF, Pillow; falls back to a snapshot if Pillow is missing),
  `/help`.
- Panel API: `/api/config` returns `TELEGRAM_CHAT_IDS` as an array and accepts it
  as an array/CSV; `/api/telegram/detect` appends the last-seen chat_id and
  returns the updated list; `/api/telegram/test` messages every registered chat.

### `src/panel.html`
- Telegram section: token save is its own form; below it a **chip list** of
  registered chats with per-chip remove, a manual-add input, "Detect chat_id"
  (appends) and "Send test to all".

### `install.sh`
- Installs `python3-pil` (for `/video` GIF).
- Writes/migrates `TELEGRAM_CHAT_IDS` (from an old `TELEGRAM_CHAT_ID` when
  present). Final test message goes to every registered chat.

### Docs
- `CONTEXT.md`, `README.md` updated for multiple viewers and bot commands.

## Known limits

- `/video` GIF is low-fps and silent (there's no audio in the feed anyway).
- Bot offset is in-memory: after a restart the backlog is drained (commands not
  re-run), matching "no history" domain rule.
- Registration stays owner-driven on purpose: anyone who finds the bot cannot
  watch the house without being added in the Panel.
