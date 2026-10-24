"""Provider-detectable ASGI gateway; production guards precede workspace imports."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
from runtime import install_safe_logging, prepare_environment

ROOT = Path(__file__).resolve().parent
prepare_environment(ROOT)
install_safe_logging()
sys.path.insert(0, str(ROOT / "backend"))
from app.main import app as workspace  # noqa: E402


@asynccontextmanager
async def lifespan(_gateway):
    # Mounted applications do not receive ASGI lifespan events automatically.
    async with workspace.router.lifespan_context(workspace):
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/", workspace)
