# CONTEXT — GuauCam

Domain glossary. No implementation details.

## Terms

- **Camera**: the physical camera pointing at the dogs at home. Either a USB webcam (Logitech C920/C922) or a CSI ribbon module (OV5647 / Pi Camera). CSI modules have no microphone.
- **Station**: the Raspberry Pi Zero W (v1) the Camera is connected to. The only device of the system inside the house.
- **Live feed**: the real-time video stream (video only, no audio). It is ephemeral: nothing is recorded or stored.
- **Snapshot**: a still image of the current instant, taken on demand from the Live feed.
- **Viewer**: any of the owner's browsers (phone or PC) that opens the Live feed. Can be inside or outside the house.
- **Registered chat**: a Telegram chat authorized by the owner to receive Alerts and to drive the Camera through Bot commands. There can be several (the owner plus family). The owner manages the list from the Panel; the bot never self-registers anyone.
- **Bot command**: a message sent to the Telegram bot by a Registered chat that asks the Station for something on demand — a Snapshot (`/screenshot`), a short Clip (`/video`) or the Live feed link (`/live`). Commands from chats that are not registered are refused.
- **Clip**: a few-seconds animated GIF built on demand from a burst of Snapshots. Silent and low-frame-rate, meant only as a quick "what's happening now"; nothing is stored afterwards.
- **Tailnet**: the owner's private Tailscale network. The only way to reach the Live feed from outside the house; nothing is exposed to the public internet.
- **Detector**: process on the Station that continuously measures the noise level from a microphone (the USB webcam's, or a separate USB mic if the Camera is CSI). It measures volume, it does not recognize sounds: it cannot tell a bark from a vacuum cleaner.
- **Threshold**: noise level (in dB) configured by the owner above which noise counts as excessive.
- **Alert**: Telegram notification when noise stays above the Threshold for long enough. Includes a Snapshot of the moment. It is sent to every Registered chat.
- **Cooldown**: minimum time between consecutive Alerts, so a long barking episode does not trigger an avalanche of messages.
- **Panel**: web page served by the Station that brings together the Live feed, the live noise level, the Detector settings (Threshold, sustained duration, Cooldown) and the Telegram Alert setup (bot token, chat_id detection, test message). It is the main way to calibrate the Threshold and to enable Alerts.

## Domain rules

- The Live feed is only visible to devices inside the Tailnet (or the home LAN).
- There is no history: if nobody is watching, nothing happens; there is no continuous recording and no motion detection.
- The Station must recover on its own after power cuts or Camera disconnections, with no manual intervention.
- One-off noise (a knock, a click) does not trigger an Alert: only sustained noise above the Threshold does.
- The Threshold, the sustained duration, the Cooldown and the Telegram credentials are configurable by the owner from the Panel, taking effect immediately and without reinstalling anything.
- The Telegram token never travels back to the browser: the Panel only says whether it is saved.
- The Panel is only reachable from the Tailnet or the home LAN, same as the Live feed.
- Audio is never transmitted or stored: the Detector measures it and discards it.
- Without a microphone there are no Alerts and no meter, but the Live feed and the Panel keep working; the Detector waits for a mic to show up without crashing.
- Alerts and Bot commands reach every Registered chat. The owner adds and removes Registered chats from the Panel; taking effect immediately and without a restart.
- Only Registered chats can drive the Camera with Bot commands. A stranger who finds the bot cannot watch the house: the bot replies with their chat_id so the owner can decide whether to add them, and does nothing else.
- Telegram cannot embed the Live feed; the `/live` command answers with the Panel link, which is still only reachable from the Tailnet or the home LAN.
