"""Prepare a local-only, source-and-public-assets trial ZIP. Never uploads data."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile
from packaging.requirements import InvalidRequirement, Requirement


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_allowlist", ROOT / "scripts/package_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
ASSET_TYPES = {".html", ".js", ".css", ".json", ".svg", ".png", ".ico", ".webmanifest", ".txt", ".woff", ".woff2"}
ENTRYPOINTS = {"main.py", "runtime.py", "start.py", "safe_worker.py", "gunicorn_conf.py", "import_workspace.py"}
ROOT_FILES = ENTRYPOINTS | {"requirements.txt", ".python-version", "TRIAL-SETUP.md", "RELEASE-MANIFEST.json"}


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        return False
    if name in ROOT_FILES:
        return True
    if path.parts[:2] == ("backend", "app"):
        return path.suffix == ".py" and "__pycache__" not in path.parts
    if name in {"backend/requirements.txt", "backend/requirements-brokers.txt", "backend/alembic.ini"}:
        return True
    if path.parts[:2] == ("backend", "calendars"):
        return len(path.parts) == 3 and path.suffix == ".json"
    if path.parts[:2] == ("backend", "migrations"):
        return path.suffix in {".py", ".mako"} and "__pycache__" not in path.parts
    return (path.parts[:2] == ("frontend", "dist") and path.suffix.lower() in ASSET_TYPES
            and not any(part.startswith(".") or part in {"node_modules", "OptionsTraderData"} for part in path.parts))


def read_public(path, boundary):
    if path.is_symlink() or not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("A package source escaped its approved source directory.")
    return path.read_bytes()


def flattened_requirements(source):
    """No -r includes, remote URLs, editable installs or pip option execution."""
    lines, seen = [], {}
    for name in ("backend/requirements.txt", "backend/requirements-brokers.txt"):
        path = Path(source) / name
        for raw in read_public(path, Path(source)).decode("utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-") or "://" in line or "@" in line or "\\" in line:
                raise ValueError("Only named, versioned public dependencies may enter the upload manifest.")
            try:
                requirement = Requirement(line)
            except InvalidRequirement:
                raise ValueError("Invalid public dependency in upload manifest.") from None
            if requirement.url or not requirement.specifier:
                raise ValueError("Every upload dependency requires a bounded version specification.")
            key = requirement.name.lower().replace("_", "-")
            if key in seen and seen[key] != line:
                raise ValueError("Conflicting upload dependency declarations.")
            if key not in seen:
                lines.append(line)
                seen[key] = line
    return ("# Flattened public runtime dependencies; no private indexes or credentials.\n" + "\n".join(lines) + "\n").encode()


def payload(source, built):
    source, built = Path(source), Path(built)
    files = {}
    for path in sorted((source / "backend/app").rglob("*.py")):
        name = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts:
            continue
        if not safe_name(name) or not release.allowed(path, root=source):
            raise ValueError("Runtime source is outside the source-release allowlist.")
        files[name] = read_public(path, source)
    for directory, suffixes in (("backend/migrations", {".py", ".mako"}), ("backend/calendars", {".json"})):
        for path in sorted((source / directory).rglob("*")):
            if not path.is_file() or path.suffix not in suffixes or "__pycache__" in path.parts:
                continue
            name = path.relative_to(source).as_posix()
            if not safe_name(name) or not release.allowed(path, root=source):
                raise ValueError("Unapproved migration or calendar source in runtime package.")
            files[name] = read_public(path, source)
    for name in ("backend/requirements.txt", "backend/requirements-brokers.txt", "backend/alembic.ini", ".python-version"):
        path = source / name
        if not release.allowed(path, root=source):
            raise ValueError("Required source is outside the source-release allowlist.")
        files[name] = read_public(path, source)
    for path in sorted(built.rglob("*")):
        if not path.is_file():
            continue
        name = "frontend/dist/" + path.relative_to(built).as_posix()
        if not safe_name(name) or not release.allowed(source / name, root=source):
            raise ValueError("Unexpected or private file in the isolated frontend build.")
        files[name] = read_public(path, built)
    for name in ENTRYPOINTS:
        files[name] = read_public(HERE / name, HERE)
    files["requirements.txt"] = flattened_requirements(source)
    files["TRIAL-SETUP.md"] = read_public(source / "docs/RAMNAYM-TRIAL.md", source)
    if not {"backend/app/main.py", "frontend/dist/index.html", "backend/calendars/exchange_sessions.json", "backend/migrations/env.py"}.issubset(files):
        raise ValueError("The complete runtime source and prebuilt website are required.")
    return files


def validate_archive(path):
    with ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("The trial archive failed its integrity check.")
        names = archive.namelist()
        if len(names) != len(set(names)) or not all(safe_name(name) for name in names):
            raise ValueError("Unexpected, duplicate or private path in the trial archive.")
        manifest = json.loads(archive.read("RELEASE-MANIFEST.json"))
        if set(names) != set(manifest) | {"RELEASE-MANIFEST.json"}:
            raise ValueError("The archive contents differ from its manifest.")
        for name, item in manifest.items():
            data = archive.read(name)
            if item != {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}:
                raise ValueError("The archive checksum manifest does not match its contents.")
        for required in ROOT_FILES - {"RELEASE-MANIFEST.json"} | {"backend/app/main.py", "frontend/dist/index.html", "backend/alembic.ini", "backend/migrations/env.py", "backend/calendars/exchange_sessions.json"}:
            if required not in names:
                raise ValueError("The trial archive is missing a required runtime file.")
        for raw in archive.read("requirements.txt").decode("utf-8").splitlines():
            if raw.strip().startswith("-"):
                raise ValueError("The upload dependency manifest must be flattened.")
    return len(manifest)


def write_archive(source, built, output):
    output = Path(output)
    if output.suffix.lower() != ".zip":
        raise ValueError("The trial output must be a ZIP file.")
    files = payload(source, built)
    manifest = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()}
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".ramnaym-trial-", suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
            archive.writestr("RELEASE-MANIFEST.json", json.dumps(manifest, indent=2))
        count = validate_archive(temporary)
        os.replace(temporary, output)
        return count
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "PersonalOptionsTrader-RamNaym-Trial.zip")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        raise RuntimeError("The existing local Node runtime is required to build public frontend assets.")
    # Only OS process-launch settings are forwarded. No application credentials,
    # VITE_ values, dotenv files or actual workspace data are consumed.
    launch_env = {name: os.environ[name] for name in ("PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
    launch_env["NODE_ENV"] = "production"
    scratch = ROOT / ".desktop"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="ramnaym-public-build-") as folder:
        built = Path(folder) / "dist"
        subprocess.run([node, str(HERE / "build_frontend.mjs"), str(built)], cwd=ROOT, env=launch_env, check=True, timeout=120)
        count = write_archive(ROOT, built, args.output.resolve())
    print(f"Verified local-only trial ZIP: {args.output.resolve()}")
    print(f"{count} public runtime files plus checksum manifest; {args.output.stat().st_size:,} bytes. No upload was performed.")


if __name__ == "__main__":
    main()
