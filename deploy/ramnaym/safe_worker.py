"""Published ASGI worker implementation, with access logging disabled safely."""
from uvicorn_worker import UvicornWorker
from runtime import install_safe_logging


class SingleASGIWorker(UvicornWorker):
    CONFIG_KWARGS = {"loop": "auto", "http": "auto", "access_log": False}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        install_safe_logging()
