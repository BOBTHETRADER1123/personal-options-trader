"""Supported startup command: python start.py, from the uploaded ZIP root."""
import os
from pathlib import Path
import sys

from runtime import prepare_environment

ROOT = Path(__file__).resolve().parent
prepare_environment(ROOT)
os.chdir(ROOT)
os.execv(sys.executable, [sys.executable, "-m", "gunicorn", "--config", str(ROOT / "gunicorn_conf.py"),
    "--workers", "1", "--worker-class", "safe_worker.SingleASGIWorker", "--log-level", "info", "main:app"])
