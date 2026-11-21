#!/usr/bin/env python3
"""Extract the VIDAA mTLS client certificate from the official RemoteNOW APK.

Modern (2024+, protocol 3290+) Hisense VIDAA TVs require the client certificate
that ships inside the official RemoteNOW/VIDAA mobile app. The app cannot
legally redistribute that certificate, so this script extracts YOUR copy from
the APK you downloaded, for interoperability with your own TV.

Usage:
    python tools/extract_cert.py <path-to-RemoteNow.apk> [more.apk ...]

What it does:
    1. Opens each APK (it's a zip) and locates the PKCS#12 keystore
       (assets/client_mobile_android.p12, or an obfuscated res/*.p12 on newer
       builds).
    2. Decrypts it with the publicly documented keystore password.
    3. Writes  certs/vidaa_client.pem  and  certs/vidaa_client.key  next to
       app.py, where the app picks them up automatically.

Requires:  pip install cryptography   (already in requirements.txt)
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
except ImportError:
    sys.exit("cryptography is not installed. Run:  pip install -r requirements.txt")

# Publicly documented in pyvidaa's VIDAA_PROTOCOL_ANALYSIS.md
KEYSTORE_PASSWORD = b"186e990688070325a1c4b0ce275d2388"

CERT_OUT = Path(__file__).resolve().parent.parent / "certs" / "vidaa_client.pem"
KEY_OUT = Path(__file__).resolve().parent.parent / "certs" / "vidaa_client.key"


def find_keystore(apk: Path) -> bytes | None:
    """Return the raw bytes of the first .p12 inside the APK."""
    with zipfile.ZipFile(apk) as zf:
        p12_names = sorted(
            n for n in zf.namelist()
            if n.lower().endswith(".p12") and not n.endswith("/")
        )
        if not p12_names:
            return None
        # Prefer the canonical asset name when present, else take the first.
        preferred = [n for n in p12_names if "client_mobile_android" in n]
        name = preferred[0] if preferred else p12_names[0]
        print(f"  found keystore: {name}")
        return zf.read(name)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    for apk_arg in argv[1:]:
        apk = Path(apk_arg).expanduser().resolve()
        if not apk.is_file():
            print(f"error: {apk} does not exist")
            return 1
        print(f"Reading {apk.name} ...")
        try:
            blob = find_keystore(apk)
        except zipfile.BadZipFile:
            print(f"error: {apk} is not a valid zip/APK")
            return 1
        if blob is None:
            print("  no .p12 keystore inside — is this the RemoteNow APK?")
            continue

        try:
            key, cert, _ = pkcs12.load_key_and_certificates(blob, KEYSTORE_PASSWORD)
        except Exception as exc:
            print(f"error: could not decrypt keystore: {exc}")
            return 1
        if key is None or cert is None:
            print("error: keystore contains no private key / certificate")
            return 1

        CERT_OUT.parent.mkdir(parents=True, exist_ok=True)
        CERT_OUT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        KEY_OUT.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        print(f"  cert -> {CERT_OUT}")
        print(f"  key  -> {KEY_OUT}")
        print("Done! Restart 'python app.py' and the app will use it.")
        return 0

    print("no usable keystore found in any input APK")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
