"""One-time Windows installation. Credentials are never generated into source."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]

def run(args, cwd=ROOT):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)

def main():
    if sys.version_info < (3, 12):
        raise RuntimeError('Install Python 3.12 or newer, then run Setup-Windows.cmd again.')
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('1/4 Creating the project environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    print('2/4 Installing backend and report dependencies...', flush=True)
    run([python, '-m', 'pip', 'install', '-r', 'backend/requirements.txt'])
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd'
    manager = npm or (str(bundled) if bundled.exists() else None)
    if not manager:
        raise RuntimeError('Install Node.js LTS from https://nodejs.org/ . Close this window and run setup again.')
    print('3/4 Installing and building the website...', flush=True)
    run([manager, 'install'], ROOT / 'frontend')
    run([manager, 'run', 'build'], ROOT / 'frontend')
    print('4/4 Preparing private local settings...', flush=True)
    if not (ROOT / '.env').exists():
        shutil.copyfile(ROOT / '.env.example', ROOT / '.env')
    print('Ready. Run Start-Windows.cmd and open http://127.0.0.1:8000 .', flush=True)
    print('Create your own password on first visit. The initial workspace uses clearly marked sample data.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'\nSetup stopped: {error}', file=sys.stderr)
        raise SystemExit(1)
