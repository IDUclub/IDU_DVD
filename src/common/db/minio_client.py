"""MinIO-backed storage for original source files (closed contour, proxied via this service)."""

from __future__ import annotations

import io

import structlog
from minio import Minio
from minio.error import S3Error

log = structlog.get_logger(__name__)


class DocumentStorage:
    """Thin wrapper around one MinIO bucket — upload/download/delete original source files."""

    def __init__(self, client: Minio, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bucket={self.bucket})"

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
            log.info("minio_bucket_created", bucket=self.bucket)

    def upload(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """Store an object. Exceptions propagate — callers treat this as fail-closed."""
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def download(self, key: str) -> tuple[bytes, str | None]:
        response = self.client.get_object(self.bucket, key)
        try:
            data = response.read()
            content_type = response.headers.get("Content-Type")
        finally:
            response.close()
            response.release_conn()
        return data, content_type

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error:
            return False

    def delete(self, key: str) -> None:
        """Best-effort removal — logs and swallows failures instead of raising."""
        try:
            self.client.remove_object(self.bucket, key)
        except S3Error as exc:
            log.warning("minio_delete_failed", bucket=self.bucket, key=key, error=str(exc))
