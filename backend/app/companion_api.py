"""Owner-only report downloads and a separately authenticated outbound companion."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .database import SessionLocal
from .reporting import ExportArtifact, ExportJob, archive_after_verified_save, now_iso, public_job, reconciliation, job_metadata

router_browser = APIRouter(prefix="/api/exports", tags=["Excel exports"])
router_companion = APIRouter(prefix="/api/companion", tags=["Local companion"])


def db_session():
    with SessionLocal() as db:
        yield db


def _job(db: Session, job_id: str) -> ExportJob:
    job = db.get(ExportJob, job_id)
    if not job:
        raise HTTPException(404, "Export job not found")
    return job


def require_companion(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    expected_hash = os.getenv("COMPANION_TOKEN_SHA256", "").strip().lower()
    if not expected_hash or len(expected_hash) != 64:
        raise HTTPException(503, "Companion is not paired. Configure COMPANION_TOKEN_SHA256 on the backend.")
    # Only a direct TLS request or a trusted loopback deployment is accepted.
    # Render/uvicorn must be configured to trust its own reverse proxy only.
    if request.url.scheme != "https":
        loopback_host = request.url.hostname in {"127.0.0.1", "localhost", "::1"}
        loopback_peer = request.client and request.client.host in {"127.0.0.1", "::1"}
        if not loopback_host or not loopback_peer:
            raise HTTPException(400, "Companion requires HTTPS")
    token = (authorization or "").removeprefix("Bearer ")
    actual = hashlib.sha256(token.encode()).hexdigest()
    if not token or not hmac.compare_digest(actual, expected_hash):
        raise HTTPException(401, "Invalid companion token")
    return actual


def _claim_valid(job: ExportJob, claim: str | None) -> bool:
    return bool(claim and job.claim_hash and hmac.compare_digest(hashlib.sha256(claim.encode()).hexdigest(), job.claim_hash))


def _download(job: ExportJob) -> Response:
    if not job.file_bytes or not job.checksum:
        raise HTTPException(409, "Report is not generated yet; source records are retained")
    return Response(
        content=job.file_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{job.filename}"',
            "X-Content-SHA256": job.checksum,
            "X-Export-Job-ID": job.id,
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        },
    )


@router_browser.get("")
def list_exports(db: Session = Depends(db_session)):
    return {"jobs": job_metadata(db), "recent_limit": 200}


@router_browser.get("/reconciliation")
def reconciliation_report(db: Session = Depends(db_session)):
    return reconciliation(db)


@router_browser.get("/{job_id}/artifacts")
def browser_artifacts(job_id: str, db: Session = Depends(db_session)):
    """Metadata only: listing never fetches XLSX or compressed history bytes."""
    job = _job(db, job_id)
    items = []
    if job.checksum and job.size and job.status in {"ready", "claimed", "saved"}:
        items.append({"kind": "xlsx", "filename": job.filename, "size": job.size,
                      "sha256": job.checksum,
                      "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
        rows = db.execute(select(ExportArtifact.kind, ExportArtifact.filename, ExportArtifact.size,
                                 ExportArtifact.sha256, ExportArtifact.mime_type).where(ExportArtifact.job_id == job_id))
        items.extend(dict(row._mapping) for row in rows)
    return {"job_id": job_id, "artifacts": items}


@router_browser.get("/{job_id}/download")
def browser_download(job_id: str, kind: Literal["xlsx", "csv_gz"] = "xlsx", db: Session = Depends(db_session)):
    job = _job(db, job_id)
    if kind == "xlsx":
        return _download(job)
    artifact = db.scalar(select(ExportArtifact).where(ExportArtifact.job_id == job_id, ExportArtifact.kind == kind))
    if not artifact or job.status not in {"ready", "claimed", "saved"}:
        raise HTTPException(404, "Detailed history archive is unavailable for this report; existing source records are retained")
    content = bytes(artifact.file_bytes)
    if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise HTTPException(409, "Stored archive verification failed; source records are retained")
    return Response(content=content, media_type=artifact.mime_type,
                    headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"',
                             "X-Content-SHA256": artifact.sha256, "X-Export-Job-ID": job.id,
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router_browser.post("/{job_id}/retry")
def retry_export(job_id: str, db: Session = Depends(db_session)):
    job = _job(db, job_id)
    if job.status == "saved":
        return public_job(job)
    if job.status == "generating" and (job.lease_until or "") > now_iso():
        raise HTTPException(409, "Workbook is currently being generated")
    job.status = "ready" if job.file_bytes and job.checksum else "pending"
    job.error, job.lease_until, job.claim_hash, job.companion_id = None, None, None, None
    job.automatic_retry = True
    job.updated_at = now_iso()
    db.commit()
    from .reporting import notify_work
    notify_work()
    return public_job(job)


@router_companion.get("/jobs", dependencies=[Depends(require_companion)])
def companion_jobs(db: Session = Depends(db_session)):
    query = select(ExportJob).where(or_(
        ExportJob.status == "ready",
        (ExportJob.status == "claimed") & (ExportJob.lease_until < now_iso()),
        (ExportJob.status == "failed") & (ExportJob.file_bytes.is_not(None)) & (ExportJob.lease_until < now_iso()),
    )).order_by(ExportJob.created_at).limit(20)
    return {"jobs": job_metadata(db, query)}


class Claim(BaseModel):
    companion_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


@router_companion.post("/jobs/{job_id}/claim", dependencies=[Depends(require_companion)])
def claim_job(job_id: str, body: Claim, db: Session = Depends(db_session)):
    job = _job(db, job_id)
    if job.status == "saved":
        return {**public_job(job), "already_saved": True}
    token = secrets.token_urlsafe(32)
    lease = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="microseconds")
    result = db.execute(update(ExportJob).where(
        ExportJob.id == job_id,
        or_(ExportJob.status == "ready", (ExportJob.status == "claimed") & (ExportJob.lease_until < now_iso()),
            (ExportJob.status == "failed") & (ExportJob.file_bytes.is_not(None)) & (ExportJob.lease_until < now_iso())),
    ).values(status="claimed", claim_hash=hashlib.sha256(token.encode()).hexdigest(),
             companion_id=body.companion_id, lease_until=lease, updated_at=now_iso()))
    db.commit()
    if result.rowcount != 1:
        raise HTTPException(409, "Job has an active claim or is not ready")
    db.refresh(job)
    return {**public_job(job), "claim_token": token, "lease_until": lease}


@router_companion.get("/jobs/{job_id}/download", dependencies=[Depends(require_companion)])
def companion_download(
    job_id: str, x_export_claim: Annotated[str | None, Header()] = None, db: Session = Depends(db_session),
):
    job = _job(db, job_id)
    if not _claim_valid(job, x_export_claim):
        raise HTTPException(403, "A valid export claim is required")
    return _download(job)


class Acknowledgement(BaseModel):
    companion_id: str = Field(min_length=8, max_length=80)
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0)
    filename: str = Field(min_length=1, max_length=255)
    local_path: str = Field(min_length=1, max_length=2048)
    workbook_valid: bool


@router_companion.post("/jobs/{job_id}/ack", dependencies=[Depends(require_companion)])
def acknowledge_save(
    job_id: str, body: Acknowledgement,
    x_export_claim: Annotated[str | None, Header()] = None, db: Session = Depends(db_session),
):
    job = db.scalar(select(ExportJob).where(ExportJob.id == job_id).with_for_update())
    if not job:
        raise HTTPException(404, "Export job not found")
    if not _claim_valid(job, x_export_claim) or job.companion_id != body.companion_id:
        raise HTTPException(403, "Companion claim does not match")
    if not body.workbook_valid or not job.checksum or not hmac.compare_digest(body.checksum, job.checksum) or body.size != job.size or body.filename != job.filename:
        raise HTTPException(422, "Local workbook verification does not match; cloud data retained")
    if job.status == "saved":
        return {**public_job(job), "already_saved": True}
    if job.status != "claimed":
        raise HTTPException(409, "Only a claimed export can be acknowledged")
    job.status = "saved"
    job.verification_status = "verified"
    job.saved_at, job.updated_at = now_iso(), now_iso()
    job.local_path = body.local_path
    job.lease_until = None
    archive_after_verified_save(db, job)
    db.commit()
    return public_job(job)


class Failure(BaseModel):
    message: str = Field(max_length=250)


@router_companion.post("/jobs/{job_id}/failed", dependencies=[Depends(require_companion)])
def failed_save(
    job_id: str, body: Failure,
    x_export_claim: Annotated[str | None, Header()] = None, db: Session = Depends(db_session),
):
    job = _job(db, job_id)
    if not _claim_valid(job, x_export_claim):
        raise HTTPException(403, "Invalid claim")
    if job.status != "saved":
        job.error = "Local save failed: " + body.message
        job.status = "failed"
        job.automatic_retry = False  # Retry the local save, not the valid cloud workbook.
        # Retain the lease briefly to avoid hammering a locked file.
        job.lease_until = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="microseconds")
        job.updated_at = now_iso()
        db.commit()
    return public_job(job)
