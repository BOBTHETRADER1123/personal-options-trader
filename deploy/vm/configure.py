"""Validate a staged Ubuntu release and create deployment files without replacement.

This helper does not install packages, start services, migrate accounts, or open
firewall ports. install.sh is the operator entry point. No secret is printed.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shlex
import stat

APP = "personal-options-trader"
BASE = Path("/opt") / APP
STATE = Path("/var/lib") / APP
CONFIG = Path("/etc") / APP
VERCEL_ORIGIN = "https://personal-options-trader.vercel.app"
TEMPLATES = Path(__file__).resolve().parent
UNITS = (
    f"{APP}.service", f"{APP}-proxy.service",
    f"{APP}-backup.service", f"{APP}-backup.timer",
)


def hostname(value: str) -> str:
    value = value.lower()
    if len(value) > 253 or "." not in value:
        raise ValueError("Supply the actual public DNS hostname, without a URL or port.")
    labels = value.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Supply the actual public DNS hostname, without a URL or port.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("Use a public DNS hostname with HTTPS, not a bare IP address.")
    reserved = ("localhost", "local", "invalid", "test", "example", "example.com", "example.net", "example.org")
    if any(value == suffix or value.endswith("." + suffix) for suffix in reserved):
        raise ValueError("A real public hostname is required; placeholder/reserved names are refused.")
    return value


def validate_release(value: Path, base: Path = BASE) -> Path:
    """Only one real, named directory immediately inside releases/ is accepted."""
    if not value.is_absolute() or value.parent != base / "releases":
        raise ValueError("Stage the release directly under /opt/personal-options-trader/releases/NAME.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value.name):
        raise ValueError("Use a release name containing only letters, numbers, dots, underscores or hyphens.")
    if not value.is_dir() or value.resolve() != value:
        raise ValueError("The staged release must be a real directory without symlinked parents.")
    required = (
        "backend/app/main.py", "backend/requirements.txt", "scripts/backup.py",
        "frontend/dist/index.html", "deploy/vm/install.sh", "deploy/vm/backup_daily.py",
    )
    for relative in required:
        path = value / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(value):
            raise ValueError(f"Missing or unsafe release file: {relative}")
    if not (value / "frontend/dist/assets").is_dir():
        raise ValueError("The staged release needs the existing production frontend/dist build.")
    for relative in (".env", ".env.online", "companion/.env", "companion/.env.online", "OptionsTraderData", ".desktop", "backups"):
        if (value / relative).exists() or (value / relative).is_symlink():
            raise ValueError("The staged source must not contain private configuration or runtime data.")
    for folder, directories, files in os.walk(value, followlinks=False):
        directories[:] = [name for name in directories if name != ".venv"]
        if any(name in {".git", "node_modules"} for name in directories):
            raise ValueError("Stage the safe source release, excluding Git metadata and node_modules.")
        for name in directories + files:
            path = Path(folder) / name
            if path.is_symlink():
                raise ValueError("Source symlinks are not accepted outside the managed virtual environment.")
            if (name.startswith(".env") and name != ".env.example") or name.endswith((".db", ".sqlite3", ".xlsx", ".pem", ".key")):
                raise ValueError("Private configuration, credentials and runtime files must not be staged as source.")
    return value


def directory(path: Path, mode: int = 0o755) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Refusing an unsafe deployment directory: {path}")
    path.mkdir(mode=mode, parents=True, exist_ok=True)


def write_once(path: Path, content: str, mode: int = 0o600) -> None:
    data = content.encode("utf-8")
    if path.is_symlink():
        raise ValueError(f"Refusing a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError(f"Existing configuration differs; review it manually before changing: {path}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as target:
        # Explicitly grant only the named template's intended access, retaining
        # the installer's restrictive umask for everything else.
        if hasattr(os, "fchmod"):
            os.fchmod(target.fileno(), mode)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def parse_env(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in result:
            raise ValueError("The private environment file has an invalid or duplicate entry.")
        words = shlex.split(value, comments=False)
        if len(words) > 1:
            raise ValueError("Quote environment values containing spaces.")
        result[name] = words[0] if words else ""
    return result


def validate_env(text: str, public_hostname: str) -> None:
    values = parse_env(text)
    expected = {
        "ENVIRONMENT": "production", "COOKIE_SECURE": "true", "COOKIE_SAMESITE": "lax",
        "DATABASE_URL": f"sqlite:///{STATE.as_posix()}/trader.db", "DATA_DIR": STATE.as_posix(),
        "INSTRUMENT_MASTER_PATH": (STATE / "instruments.json").as_posix(),
        "MARKET_CALENDAR_PATH": (STATE / "market-calendar.json").as_posix(),
        "EXPORT_ROOT": (STATE / "reports").as_posix(),
    }
    for key, required in expected.items():
        if values.get(key) != required:
            raise ValueError(f"Review the private environment setting {key}; it is unsafe or inconsistent.")
    origins = set(values.get("ALLOWED_ORIGINS", "").split(","))
    if origins != {f"https://{public_hostname}", VERCEL_ORIGIN}:
        raise ValueError("ALLOWED_ORIGINS must contain exactly the selected HTTPS hostname and existing Vercel origin.")


def check_existing(public_hostname: str, release: Path) -> None:
    current = BASE / "current"
    if current.exists() or current.is_symlink():
        if not current.is_symlink() or current.resolve() != release:
            raise ValueError("An existing release is selected. This installer never switches or replaces it automatically.")
    if CONFIG.is_symlink() or (CONFIG.exists() and not CONFIG.is_dir()):
        raise ValueError("The configuration directory must be a real directory.")
    environment = CONFIG / "app.env"
    if environment.exists() or environment.is_symlink():
        info = environment.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("The existing environment must be a root-owned private regular file (mode 0600).")
        validate_env(environment.read_text(encoding="utf-8"), public_hostname)


def apply(public_hostname: str, release: Path) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("Deployment configuration requires root on the selected Ubuntu VM.")
    import grp
    check_existing(public_hostname, release)
    app_group = grp.getgrnam(APP).gr_gid
    caddy_group = grp.getgrnam("caddy").gr_gid
    # Only this validated, private-data-free staged source is shared with the
    # unprivileged application group. Symlink targets are never chmod/chowned.
    for path in (BASE, BASE / "releases"):
        os.chown(path, 0, app_group)
        path.chmod(0o750)
    for folder, directories, files in os.walk(release, followlinks=False):
        current_folder = Path(folder)
        os.chown(current_folder, 0, app_group)
        current_folder.chmod(0o750)
        for name in files:
            path = current_folder / name
            if path.is_symlink():
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("The staged release contains a non-regular file.")
            os.chown(path, 0, app_group)
            path.chmod(0o750 if info.st_mode & 0o111 else 0o640)
    directory(CONFIG)
    os.chown(CONFIG, 0, caddy_group)
    CONFIG.chmod(0o750)
    directory(Path("/etc/systemd/system"))
    public_config = (TEMPLATES / "Caddyfile.template").read_text(encoding="utf-8").replace("@HOSTNAME@", public_hostname)
    write_once(CONFIG / "Caddyfile", public_config, 0o640)
    os.chown(CONFIG / "Caddyfile", 0, caddy_group)
    for unit in UNITS:
        write_once(Path("/etc/systemd/system") / unit, (TEMPLATES / unit).read_text(encoding="utf-8"))
    environment = CONFIG / "app.env"
    if not environment.exists():
        content = (TEMPLATES / "app.env.example").read_text(encoding="utf-8")
        content = content.replace("REQUIRED_PUBLIC_HOSTNAME", public_hostname)
        content = content.replace("GENERATED_ONLY_IN_THE_PRIVATE_SERVER_FILE", secrets.token_urlsafe(48))
        validate_env(content, public_hostname)
        write_once(environment, content, 0o600)
    current = BASE / "current"
    if not current.is_symlink():
        current.symlink_to(release, target_is_directory=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True, type=hostname)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        release = validate_release(args.release)
        check_existing(args.hostname, release)
        if args.apply:
            apply(args.hostname, release)
        print("VM deployment configuration verified." if args.apply else "VM deployment preflight passed.")
        return 0
    except (ValueError, OSError) as error:
        # Validation errors contain names/paths only, never environment values.
        print(f"Deployment stopped: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
