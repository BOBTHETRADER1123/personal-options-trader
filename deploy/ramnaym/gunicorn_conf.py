"""Exactly one async application worker; provider ingress supplies HTTPS/WSS."""
import os
from pathlib import Path
from runtime import install_safe_logging, port, prepare_environment

prepare_environment(Path(__file__).resolve().parent)
install_safe_logging()
bind = f"0.0.0.0:{port()}"
workers = 1
worker_class = "safe_worker.SingleASGIWorker"
preload_app = False
reuse_port = False
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 0
worker_tmp_dir = "/tmp"
loglevel = "info"
errorlog = "-"
accesslog = None
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s'
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")


def on_starting(server):
    from uvicorn_worker import UvicornWorker
    if server.cfg.workers != 1 or not issubclass(server.cfg.worker_class, UvicornWorker) or server.cfg.preload_app:
        raise RuntimeError("The actual runtime must use exactly one asynchronous application worker without preload.")
    install_safe_logging()
