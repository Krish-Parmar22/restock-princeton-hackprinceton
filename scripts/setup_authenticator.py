"""One-time: link the owner's authenticator app (Google Authenticator, 1Password, Authy...).

  uv run python scripts/setup_authenticator.py          # creates the secret (once), shows the QR
  uv run python scripts/setup_authenticator.py --new    # replace it (old phone lost)

Writes OWNER_TOTP_SECRET to .env (gitignored) and saves the QR to exports/authenticator-qr.png.
Scan it with the authenticator app; then restart the API. The secret is never printed.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pyotp
import qrcode

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", action="store_true", help="replace the existing secret")
    a = ap.parse_args()
    text = ENV.read_text() if ENV.exists() else ""
    m = re.search(r"^OWNER_TOTP_SECRET=(\S+)$", text, re.M)
    if m and not a.new:
        secret = m.group(1)
        print("Using the existing authenticator secret from .env (pass --new to replace it).")
    else:
        secret = pyotp.random_base32()
        line = f"OWNER_TOTP_SECRET={secret}"
        text = re.sub(r"^OWNER_TOTP_SECRET=.*$", line, text, flags=re.M) if m else text.rstrip("\n") + "\n" + line + "\n"
        ENV.write_text(text)
        print("Created a new authenticator secret in .env.")
    uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="Shop Inventory")
    out = ROOT / "exports" / "authenticator-qr.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(uri).save(out)
    print(f"QR code saved to {out}")
    if sys.stdout.isatty():
        q = qrcode.QRCode(border=1)
        q.add_data(uri)
        q.print_ascii(invert=True)
    print("Scan it in your authenticator app (+ > Scan a QR code), then restart the API.")


if __name__ == "__main__":
    main()
