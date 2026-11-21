"""
JanRemote — a lightweight, mobile-first local web remote for Hisense VIDAA OS TVs.

Backend: FastAPI + pyvidaa (MQTT over TLS to the TV's built-in broker on port 36669).
Frontend: templates/index.html (dark, mobile-responsive, Tailwind via CDN).

Run:
    pip install -r requirements.txt
    python app.py
Then open http://<your-computers-ip>:8000 on your phone.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

try:
    from pyvidaa import VidaaTV, discover_all, get_key
    from pyvidaa.topics import get_topic
    from pyvidaa.wol import send_wol
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pyvidaa is not installed. Run:  pip install -r requirements.txt"
    ) from exc

# --------------------------------------------------------------------------
# Logging / paths / config
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("pyvidaa").setLevel(logging.WARNING)
log = logging.getLogger("janremote")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "Hisense TV",
    "ip": "",
    "mac": "",
    "port": 36669,
    "paired": False,
}

# VIDAA text-input action (not wrapped by pyvidaa 2.3.0; publish it raw).
# Not every firmware implements it — some quietly ignore it.
TOPIC_SEND_TEXT = "/remoteapp/tv/ui_service/{client}/actions/sendtext"

# Optional mTLS client certificate for 2024+ firmware TVs. pyvidaa checks
# ~/.config/pyvidaa/certs/ itself; we also check a certs/ folder next to app.py
# and pass it explicitly, so dropping the files in the project folder works.
CERT_DIR = BASE_DIR / "certs"


def _cert_kwargs() -> dict[str, str]:
    cert = CERT_DIR / "vidaa_client.pem"
    key = CERT_DIR / "vidaa_client.key"
    if cert.is_file() and key.is_file():
        log.info("Using client certificate from %s", CERT_DIR)
        return {"certfile": str(cert), "keyfile": str(key)}
    return {}

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s (%s); using defaults", CONFIG_FILE, exc)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    log.info("Saved config: %s", CONFIG_FILE)


def normalize_mac(mac: str) -> str:
    mac = mac.strip().lower().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError(
            f"Invalid MAC address: {mac!r} — expected format aa:bb:cc:dd:ee:ff"
        )
    return mac


def normalize_ip(ip: str) -> str:
    ip = ip.strip()
    ipaddress.ip_address(ip)  # raises ValueError on bad input
    return ip


# --------------------------------------------------------------------------
# TV controller (singleton; pairing must survive across requests because the
# TV binds the PIN dialog to the MQTT connection that triggered it)
# --------------------------------------------------------------------------


class RemoteError(Exception):
    """User-facing error — message is shown in the web UI."""


class TVController:
    def __init__(self) -> None:
        self._tv: Optional[VidaaTV] = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            if self._tv is not None:
                try:
                    self._tv.disconnect()
                except Exception:
                    pass
            self._tv = None

    def _ensure_client(self) -> VidaaTV:
        """Caller must hold the lock."""
        cfg = load_config()
        if not cfg["ip"]:
            raise RemoteError("No TV configured yet — add your TV's IP and MAC first.")
        if (
            self._tv is None
            or self._tv.host != cfg["ip"]
            or self._tv.port != cfg["port"]
        ):
            if self._tv is not None:
                try:
                    self._tv.disconnect()
                except Exception:
                    pass
            log.info("Creating VidaaTV client for %s:%s", cfg["ip"], cfg["port"])
            self._tv = VidaaTV(
                host=cfg["ip"],
                port=cfg["port"],
                mac_address=cfg["mac"] or None,
                use_dynamic_auth=True,  # auto-detects firmware auth generation
                **_cert_kwargs(),
            )
        return self._tv

    def client(self, connect: bool = True) -> VidaaTV:
        """Return the shared client, connected if possible."""
        with self._lock:
            tv = self._ensure_client()
            if connect and not tv.is_connected:
                if not tv.connect(timeout=10):
                    raise RemoteError(
                        f"Could not connect to the TV at {tv.host}:{tv.port}. "
                        "Check that it is powered on (not deep-off), on the same "
                        "network, and that the IP is correct."
                    )
            return tv

    def raw(self) -> VidaaTV:
        """Return the shared client without forcing a connect."""
        with self._lock:
            return self._ensure_client()

    # -- pairing -----------------------------------------------------------

    def start_pairing(self) -> tuple[bool, str]:
        with self._lock:
            tv = self._ensure_client()
            if not tv.is_connected and not tv.connect(timeout=10):
                raise RemoteError(
                    f"Could not connect to {tv.host} to start pairing. "
                    "The TV must be powered on."
                )
            prompted = tv.start_pairing(wait_for_pin=8.0)
            if prompted:
                return True, "A 4-digit PIN should now be showing on your TV screen."
            return False, (
                "Pairing request sent, but the TV did not confirm a PIN dialog. "
                "If no PIN appeared, the TV may already be paired (try a command) "
                "or the screen is off."
            )

    def submit_pin(self, pin: str) -> str:
        with self._lock:
            tv = self._ensure_client()
            if not tv.is_connected:
                raise RemoteError(
                    "Not connected — tap 'Start pairing' first, then enter the PIN."
                )
            if not tv.authenticate(pin, timeout=12.0):
                raise RemoteError(
                    "Pairing failed — wrong PIN or it timed out "
                    "(the PIN dialog closes after ~60 seconds). Try again."
                )
            cfg = load_config()
            cfg["paired"] = True
            save_config(cfg)
            return "Paired successfully! 🎉 The remote is ready to use."

    # -- one-shot command helper -------------------------------------------

    def run(self, fn: Callable[[VidaaTV], Any], *, failure: str) -> Any:
        tv = self.client()
        result = fn(tv)
        if result is False:
            raise RemoteError(failure)
        return result


controller = TVController()

app = FastAPI(title="JanRemote")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------
# Volume helpers
# --------------------------------------------------------------------------


def _fresh_volume(tv: VidaaTV, prev: Optional[int], timeout: float = 1.5) -> Optional[int]:
    """Wait briefly for the volume-change broadcast, then read the cache.

    This firmware does not answer the getvolume request — it only broadcasts
    the new value when volume changes. So after sending a key we watch the
    broadcast cache until it moves (or time out and report whatever we have).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        vol = tv.cached_volume
        if vol is not None and vol != prev:
            return vol
        time.sleep(0.05)
    return tv.cached_volume


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------


class ConfigPayload(BaseModel):
    ip: str
    mac: str
    name: str = "Hisense TV"
    port: int = 36669


class PinPayload(BaseModel):
    pin: str


class KeyPayload(BaseModel):
    key: str


class VolumePayload(BaseModel):
    level: int


class SourcePayload(BaseModel):
    source: str


class AppPayload(BaseModel):
    app: Any  # app name (str) or full app dict from /api/apps


class TextPayload(BaseModel):
    text: str


def api_error(exc: Exception, status: int = 503) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="index.html", context={"title": "JanRemote"}
    )


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    cfg = load_config()
    return {**cfg, "configured": bool(cfg["ip"])}


@app.post("/api/config")
def set_config(payload: ConfigPayload):
    try:
        ip = normalize_ip(payload.ip)
        mac = normalize_mac(payload.mac) if payload.mac.strip() else ""
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    cfg = load_config()
    identity_changed = cfg["ip"] != ip or cfg["mac"] != mac
    cfg.update({"ip": ip, "mac": mac, "name": payload.name, "port": payload.port})
    save_config(cfg)
    if identity_changed:
        cfg["paired"] = False
        save_config(cfg)
        controller.reset()
    return {"ok": True, "message": f"Saved {payload.name} at {ip}."}


@app.post("/api/discover")
def api_discover():
    """SSDP/UDP discovery of VIDAA TVs on the local network."""
    try:
        devices = discover_all(timeout=4.0)
    except Exception as exc:
        return api_error(RemoteError(f"Discovery failed: {exc}"))
    found = []
    for ip, dev in devices.items():
        found.append(
            {
                "ip": ip,
                "mac": (getattr(dev, "mac", None) or "").upper(),
                "name": getattr(dev, "name", None) or "Unknown TV",
                "model": getattr(dev, "model", None) or "",
            }
        )
    return {"devices": found}


@app.post("/api/pair/start")
def pair_start():
    try:
        prompted, message = controller.start_pairing()
    except RemoteError as exc:
        return api_error(exc)
    return {"pin_prompted": prompted, "message": message}


@app.post("/api/pair/pin")
def pair_pin(payload: PinPayload):
    pin = payload.pin.strip()
    if not re.fullmatch(r"\d{4}", pin):
        return JSONResponse(
            status_code=400, content={"detail": "PIN must be exactly 4 digits."}
        )
    try:
        message = controller.submit_pin(pin)
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "message": message}


@app.post("/api/diagnose")
def diagnose():
    """Step-by-step connection diagnosis with distinct causes."""
    cfg = load_config()
    if not cfg["ip"]:
        return JSONResponse(
            status_code=400, content={"detail": "No TV configured yet — save one first."}
        )
    ip, port = cfg["ip"], cfg["port"]
    out: dict[str, Any] = {"ip": ip, "port": port}

    # Step 1: can we even open a TCP connection to the TV's MQTT port?
    try:
        s = socket.create_connection((ip, port), timeout=5)
        s.close()
        out["tcp"] = True
    except OSError as exc:
        out["tcp"] = False
        out["error"] = f"{type(exc).__name__}"
        out["verdict"] = (
            f"Cannot reach {ip}:{port} at all. Most likely causes: (1) the TV is "
            "powered off or in deep standby — turn it ON with the physical remote "
            "and retry; (2) wrong IP — verify on the TV under Settings → Network "
            "→ Status; (3) the PC and TV are on different/isolated networks "
            "(guest Wi-Fi, AP isolation, different subnets)."
        )
        return out

    # Step 2: port is open — try the full MQTT/TLS/auth handshake.
    try:
        tv = controller.client()
        out["mqtt"] = tv.is_connected
        out["on"] = tv.is_on()
        out["verdict"] = (
            "Connected to the TV's MQTT broker successfully — the remote should work. "
            + ("TV is on." if out["on"] else "TV appears to be in standby.")
        )
    except RemoteError as exc:
        out["mqtt"] = False
        out["verdict"] = (
            f"The TV is reachable on {ip}:{port}, but the secure MQTT connection "
            f"failed: {exc} — On 2024+ firmware this almost always means the client "
            "certificate is missing. See the README section 'Obtaining the client "
            "certificate' (one-time extraction from the VIDAA app APK), then retry."
        )
    except Exception as exc:  # unexpected — surface it rather than guess
        out["mqtt"] = False
        out["verdict"] = f"The TV is reachable, but connecting failed unexpectedly: {exc}"
    return out


@app.post("/api/power/on")
def power_on():
    """Power on via Wake-on-LAN magic packet, then nudge with MQTT if possible."""
    cfg = load_config()
    if not cfg["mac"]:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "No MAC address saved — add it in Settings so the "
                "Wake-on-LAN packet can be sent."
            },
        )
    if not send_wol(cfg["mac"]):
        return api_error(RemoteError("Failed to send the Wake-on-LAN packet."))
    # Best effort: if the TV's network stack is already up (standby), poke it.
    try:
        tv = controller.client()
        tv.power_on()
    except Exception:
        pass  # WoL alone may need a few seconds; not an error.
    return {
        "ok": True,
        "message": "Wake-on-LAN packet sent — give the TV a few seconds.",
    }


@app.post("/api/power/off")
def power_off():
    try:
        controller.run(lambda tv: tv.power_off(), failure="Power-off command failed.")
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "message": "Power off sent."}


@app.get("/api/status")
def status():
    cfg = load_config()
    if not cfg["ip"]:
        return {"configured": False, "paired": False}
    out: dict[str, Any] = {
        "configured": True,
        "paired": cfg["paired"],
        "connected": False,
        "on": False,
        "volume": None,
        "muted": None,
        "state": None,
        "name": cfg["name"],
    }
    try:
        tv = controller.client()
        out["connected"] = tv.is_connected
        out["on"] = tv.is_on()
        # This firmware doesn't answer getvolume; the broadcast cache is the
        # fresh source (it updates the instant volume changes).
        out["volume"] = tv.cached_volume
        if out["volume"] is None:
            out["volume"] = tv.get_volume(timeout=1)
        out["muted"] = tv.is_muted
        state = tv.get_state(timeout=2) or tv.state or {}
        out["state"] = state.get("statetype") if isinstance(state, dict) else None
    except RemoteError:
        pass  # TV off / unreachable — report that via connected/on flags.
    except Exception as exc:  # never let polling crash
        log.debug("status error: %s", exc)
    return out


@app.post("/api/key")
def send_key(payload: KeyPayload):
    try:
        key = get_key(payload.key.strip())
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Unknown key."})
    try:
        controller.run(
            lambda tv: tv.send_key(key),
            failure=f"Could not send key {key} — is the TV on?",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True}


@app.post("/api/volume")
def set_volume(payload: VolumePayload):
    level = max(0, min(100, payload.level))
    try:
        controller.run(
            lambda tv: tv.set_volume(level),
            failure="Could not set volume — is the TV on?",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "level": level}


@app.post("/api/volume/up")
def volume_up():
    try:
        tv = controller.client()
        prev = tv.cached_volume
        if not tv.volume_up():
            raise RemoteError("Volume up failed — is the TV on?")
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "volume": _fresh_volume(tv, prev), "muted": tv.is_muted}


@app.post("/api/volume/down")
def volume_down():
    try:
        tv = controller.client()
        prev = tv.cached_volume
        if not tv.volume_down():
            raise RemoteError("Volume down failed — is the TV on?")
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "volume": _fresh_volume(tv, prev), "muted": tv.is_muted}


@app.post("/api/volume/mute")
def volume_mute():
    try:
        tv = controller.client()
        prev = tv.cached_volume
        if not tv.mute():
            raise RemoteError("Mute toggle failed — is the TV on?")
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "volume": _fresh_volume(tv, prev), "muted": tv.is_muted}


@app.get("/api/sources")
def get_sources():
    try:
        sources = controller.run(
            lambda tv: tv.get_sources(timeout=5),
            failure="Could not fetch the input list — is the TV on?",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"sources": sources or []}


@app.post("/api/source")
def set_source(payload: SourcePayload):
    try:
        controller.run(
            lambda tv: tv.set_source(payload.source.strip().lower()),
            failure=f"Could not switch to {payload.source}.",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "message": f"Switched to {payload.source}."}


@app.get("/api/apps")
def get_apps():
    try:
        apps = controller.run(
            lambda tv: tv.get_apps(timeout=5),
            failure="Could not fetch the app list — is the TV on?",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"apps": apps or []}


@app.post("/api/app")
def launch_app(payload: AppPayload):
    try:
        controller.run(
            lambda tv: tv.launch_app(payload.app),
            failure="Could not launch that app.",
        )
    except RemoteError as exc:
        return api_error(exc)
    return {"ok": True, "message": "App launch sent."}


@app.post("/api/text")
def send_text(payload: TextPayload):
    text = payload.text
    if not text.strip():
        return JSONResponse(status_code=400, content={"detail": "Nothing to send."})

    def _send(t: VidaaTV) -> bool:
        # pyvidaa 2.3.0 has no send_text(); use the documented VIDAA action.
        # Not every firmware implements it — some quietly ignore it.
        try:
            topic = get_topic(TOPIC_SEND_TEXT, t.client_id)
            return t._publish(topic, {"text": text})
        except Exception:
            return False

    try:
        controller.run(_send, failure="NOT_SENT")
    except RemoteError as exc:
        if str(exc) == "NOT_SENT":
            return api_error(
                RemoteError(
                    "Text could not be sent. Focus a text/search field on the "
                    "TV first — and note some firmware does not implement "
                    "text input at all."
                )
            )
        return api_error(exc)
    return {"ok": True, "message": "Text sent to the TV."}


# --------------------------------------------------------------------------
# Startup / entrypoint
# --------------------------------------------------------------------------


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))  # never actually sends packets
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import uvicorn

    print("\n  JanRemote — Hisense VIDAA TV remote\n" + "  " + "-" * 37)
    print("  On your phone (same Wi-Fi), open:")
    print(f"  http://{_lan_ip()}:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
