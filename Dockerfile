FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
    GUNICORN_WORKERS=1 WEB_CONCURRENCY=1 DATA_DIR=/app/data PORT=8000
WORKDIR /app
COPY backend/requirements.txt backend/requirements-brokers.txt /app/backend/
ARG INSTALL_KOTAK_SDK=true
RUN python -m pip install --no-cache-dir -r backend/requirements.txt && \
    if [ "$INSTALL_KOTAK_SDK" = "true" ]; then python -m pip install --no-cache-dir -r backend/requirements-brokers.txt; fi && \
    useradd --create-home --uid 10001 appuser && mkdir -p /app/data && chown appuser:appuser /app/data
COPY --chown=root:root backend/app /app/backend/app
COPY --chown=root:root backend/migrations /app/backend/migrations
COPY --chown=root:root backend/alembic.ini /app/backend/alembic.ini
COPY --chown=root:root backend/calendars /app/backend/calendars
COPY --chown=root:root deploy/ramnaym/main.py deploy/ramnaym/runtime.py deploy/ramnaym/safe_worker.py \
    deploy/ramnaym/gunicorn_conf.py deploy/ramnaym/start.py /app/
USER appuser
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health/live',timeout=4).read()"
CMD ["python", "start.py"]
