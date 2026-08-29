"""Optional STACKIT / S3-compatible object storage for invoice PDFs."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@lru_cache
def _client():
    if not settings.s3_bucket or not settings.s3_access_key_id or not settings.s3_secret_access_key:
        return None
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        logger.warning("boto3 not installed; object store disabled")
        return None

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def object_store_enabled() -> bool:
    return _client() is not None


def object_key_for_pdf(relative_path: str) -> str:
    prefix = (settings.s3_prefix or "finance/invoices").strip().strip("/")
    rel = relative_path.replace("\\", "/").lstrip("/")
    return f"{prefix}/{rel}"


def upload_pdf(local_path: Path, relative_path: str) -> str | None:
    """Upload local PDF; returns object key on success, else None."""
    client = _client()
    if client is None:
        return None
    key = object_key_for_pdf(relative_path)
    try:
        client.upload_file(
            str(local_path),
            settings.s3_bucket,
            key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        logger.info("uploaded pdf s3://%s/%s", settings.s3_bucket, key)
        return key
    except Exception:
        logger.exception("failed uploading pdf to object store key=%s", key)
        return None


def download_pdf_bytes(object_key: str) -> bytes | None:
    client = _client()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=settings.s3_bucket, Key=object_key)
        return obj["Body"].read()
    except Exception:
        logger.exception("failed downloading pdf from object store key=%s", object_key)
        return None


def presigned_pdf_url(relative_path: str, *, expires_seconds: int = 3600) -> str | None:
    """Temporary HTTPS URL for an archived PDF in object storage."""
    client = _client()
    if client is None or not settings.s3_bucket:
        return None
    key = object_key_for_pdf(relative_path)
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )
    except Exception:
        logger.exception("failed presigning pdf key=%s", key)
        return None
