# JanRemote 📺

A lightweight, mobile-first **local web remote for Hisense VIDAA OS TVs**.

Runs a small FastAPI server on your computer that talks to the TV's built-in
MQTT broker (port 36669) via the [pyvidaa](https://pypi.org/project/pyvidaa/)
library. You open one page on your phone and get a full remote: power,
Wake-on-LAN, D-pad, volume, playback, HDMI/app switching and text input.

No cloud, no accounts — everything stays on your LAN.

## Project layout

```
JanRemote/
├── app.py               # FastAPI backend (all TV control + pairing)
├── requirements.txt     # Python dependencies
├── config.json          # TV settings (created/updated by the web UI)
└── templates/
    └── index.html       # Mobile-first dark UI (Tailwind via CDN)
```

## 1. Prerequisites

- Python 3.9+
- Your TV and this computer on the **same network**
- The TV's **IP address** and **MAC address**
  (TV → Settings → Network → Network Status; a static IP or DHCP reservation
  is strongly recommended)
- On the TV, enable the remote-control app pairing if present:
  Settings → System → Application Settings → Remote Control (name varies by model)

> **Modern-firmware note (mTLS):** VIDAA TVs from ~2024 onward (protocol
> version 3290+) require a client certificate for the MQTT TLS handshake.
> `pyvidaa` cannot ship it; without it you'll see "unreachable" even though
> the TV answers on the network. If diagnostics say *"TV is reachable, but the
> secure MQTT connection failed"*, extract the certificate once, as follows.

### Obtaining the client certificate (Windows walkthrough)

The certificate ships inside the official Hisense remote app (RemoteNOW / VIDAA)
as a PKCS#12 keystore. One-time extraction:

1. **Get the APK** — download the RemoteNOW APK from
   [APKMirror](https://www.apkmirror.com/apk/hisense/remotenow/) (any recent
   version) on your PC. You don't need to install it.

2. **Run the extractor** (from the JanRemote folder, venv active):
   ```powershell
   python tools\extract_cert.py C:\Users\you\Downloads\RemoteNow-2.x.x.apk
   ```
   It finds the keystore inside the APK (canonical or obfuscated name),
   decrypts it, and writes `certs\vidaa_client.pem` + `certs\vidaa_client.key`.

3. **Restart `python app.py`.** The startup log should say
   `Using client certificate from ...certs` instead of warning about a missing
   certificate. Then run diagnostics again — the MQTT handshake should now
   succeed, and you can pair.

<details><summary>Manual extraction (if you prefer Git Bash + openssl)</summary>

```bash
mkdir apk && cd apk && unzip ~/Downloads/RemoteNow-*.apk
PK12=$(find . -iname "*.p12" | head -1)
openssl pkcs12 -in "$PK12" -clcerts -nokeys \
  -passin pass:186e990688070325a1c4b0ce275d2388 -out vidaa_client.pem
openssl pkcs12 -in "$PK12" -nocerts -nodes \
  -passin pass:186e990688070325a1c4b0ce275d2388 -out vidaa_client.key
mkdir -p ../certs && cp vidaa_client.pem vidaa_client.key ../certs/
```
</details>

> **Why the app can't just ship this file:** the keystore is Hisense's
> proprietary material — the same reason pyvidaa refuses to redistribute it.
> Extracting your own copy for your own TV is personal interoperability;
> redistributing it (including committing it to a repo) is not.

## 2. Install & run

```bash
cd JanRemote
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py
```

The terminal prints your computer's LAN address, e.g.:

```
  JanRemote — Hisense VIDAA TV remote
  -------------------------------------
  On your phone (same Wi-Fi), open:
  http://192.168.1.20:8000
```

Open that URL on your phone. Tip: "Add to Home Screen" in the phone's browser
menu makes it feel like a native app.

## 3. First-time setup (pairing)

1. On first load the **Setup** sheet opens. Either tap **Scan network for TVs**
   (SSDP/UDP discovery) or type the IP + MAC manually, then **Save & Connect**.
2. The app connects and starts pairing — the TV shows a **4-digit PIN**.
3. Type the PIN into the PIN dialog and tap **Pair**. You're done — pairing and
   settings are persisted (`config.json` + pyvidaa token store), so subsequent
   runs connect automatically.

## Features

| Area | What you get |
|---|---|
| **Power** | Power On (Wake-on-LAN magic packet + MQTT standby wake), Power Off (safe state-checked, won't accidentally turn a sleeping TV back on) |
| **Navigation** | Touch D-pad (Up/Down/Left/Right/OK) with hold-to-repeat, Back / Home / Exit / Menu |
| **Volume** | Up/Down buttons, Mute toggle, and a direct 0–100 slider with live level readout |
| **Playback** | Rewind, Play/Pause, Fast-Forward |
| **Quick switch** | Input/HDMI dropdown and Apps dropdown, both fetched live from the TV (`sourcelist` / `applist`), with refresh button |
| **Keyboard** | Text box that sends typed strings to the TV's focused search/input field (`ui_service/actions/sendtext`) |
| **Status** | Header dot shows connection + power + current state (standby / launcher / live TV / input…), volume polls every 4 s |

## Troubleshooting

- **"unreachable" / connect timeout** — TV is deep-off or the IP changed.
  Wake-on-LAN needs the MAC saved and the TV connected by Ethernet
  (Wi-Fi WoL is unreliable). Enable "Network Standby" in TV settings.
- **Pairing PIN never appears** — the TV may already consider this client
  authorized (try any command), or the pairing dialog timed out (~60 s);
  restart pairing. A wrong TV clock can also break dynamic auth — check the
  TV's date/time settings.
- **Power Off does nothing when TV is off** — by design: `KEY_POWER` is a
  toggle, so the app checks state first and never sends it blind.
- **Text input does nothing** — focus a search field on the TV first; some
  firmware doesn't implement `sendtext` (the D-pad still works).
- **"pairing failed (code 5)"** — the TV rejected credentials: usually the
  missing mTLS client certificate (see Prerequisites) or a stale pairing.
  `Re-pair with TV (new PIN)` in Settings clears it up.
- **Logs** — run with `LOG_LEVEL=DEBUG python app.py` for verbose pyvidaa logs.

## Security note

The server binds `0.0.0.0:8000` with no authentication — that's fine on your
home LAN, but don't port-forward it. Anyone on the network can control the TV.
