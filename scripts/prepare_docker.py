"""Create local Docker runtime directories and secrets without overwriting them."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path


def _write_new_secret(path: Path, value: str) -> bool:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value + "\n")
    except FileExistsError:
        return False
    if os.name != "nt":
        path.chmod(0o600)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Docker data and secret files")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    root = args.root.resolve()
    secrets_dir = root / "secrets"
    data_dir = root / "docker-data"
    settings_path = root / ".env"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        secrets_dir.chmod(0o700)
        data_dir.chmod(0o700)

    created = []
    admin_path = secrets_dir / "admin_password.txt"
    key_path = secrets_dir / "credential_key.txt"
    mimo_path = secrets_dir / "mimo_api_key.txt"
    if _write_new_secret(admin_path, secrets.token_urlsafe(24)):
        created.append(admin_path)
    credential_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    if _write_new_secret(key_path, credential_key):
        created.append(key_path)
    # Compose needs the source file even when the optional cloud fallback is
    # disabled. An empty file keeps MiMo off until the operator adds a key.
    if _write_new_secret(mimo_path, ""):
        created.append(mimo_path)
    private_settings = "\n".join(
        (
            "# Add your public domain to this list while retaining both loopback hosts.",
            "SWU_DAKA_IMAGE=akiyunore/swu-daka:2026-09-18.2",
            "SWU_DAKA_ALLOWED_HOSTS=localhost,127.0.0.1",
            f"SWU_DAKA_ADMIN_PAGE_PATH=/panel-{secrets.token_hex(16)}",
            f"SWU_DAKA_ADMIN_API_PREFIX=/api/panel-{secrets.token_hex(16)}",
        )
    )
    settings_created = _write_new_secret(settings_path, private_settings)

    print(f"Prepared data directory: {data_dir}")
    if created:
        print("Created secrets (contents are intentionally not printed):")
        for path in created:
            print(f"  {path}")
    else:
        print("Existing secrets preserved; nothing was overwritten.")
    if settings_created:
        print(f"Created private deployment settings (contents are intentionally not printed): {settings_path}")
    else:
        print("Existing private deployment settings preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
