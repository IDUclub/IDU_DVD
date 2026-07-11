"""Unit tests for src/common/db/minio_client — DocumentStorage.

The ``minio.Minio`` client is a MagicMock throughout — these verify DocumentStorage's own logic
(bucket bootstrap, upload/download/delete plumbing, best-effort delete), not the real MinIO wire
protocol.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from minio.error import S3Error

from src.common.db.minio_client import DocumentStorage


def _s3_error() -> S3Error:
    return S3Error(None, "NoSuchKey", "not found", "key", "req", "host")


@pytest.fixture
def storage_and_client():
    client = MagicMock()
    return DocumentStorage(client, "dvd-documents"), client


class TestEnsureBucket:
    def test_creates_bucket_when_absent(self, storage_and_client):
        storage, client = storage_and_client
        client.bucket_exists.return_value = False
        storage.ensure_bucket()
        client.make_bucket.assert_called_once_with("dvd-documents")

    def test_skips_creation_when_bucket_exists(self, storage_and_client):
        storage, client = storage_and_client
        client.bucket_exists.return_value = True
        storage.ensure_bucket()
        client.make_bucket.assert_not_called()


class TestUpload:
    def test_uploads_with_length_and_content_type(self, storage_and_client):
        storage, client = storage_and_client
        storage.upload("abc.docx", b"hello", content_type="application/vnd.docx")
        args, kwargs = client.put_object.call_args
        assert args[0] == "dvd-documents"
        assert args[1] == "abc.docx"
        assert kwargs["length"] == 5
        assert kwargs["content_type"] == "application/vnd.docx"

    def test_propagates_failures(self, storage_and_client):
        storage, client = storage_and_client
        client.put_object.side_effect = _s3_error()
        with pytest.raises(S3Error):
            storage.upload("abc.docx", b"hello")


class TestDownload:
    def test_reads_and_releases_connection(self, storage_and_client):
        storage, client = storage_and_client
        response = MagicMock()
        response.read.return_value = b"hello"
        response.headers = {"Content-Type": "application/vnd.docx"}
        client.get_object.return_value = response

        data, content_type = storage.download("abc.docx")

        assert data == b"hello" and content_type == "application/vnd.docx"
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_releases_connection_even_on_read_failure(self, storage_and_client):
        storage, client = storage_and_client
        response = MagicMock()
        response.read.side_effect = RuntimeError("boom")
        client.get_object.return_value = response

        with pytest.raises(RuntimeError):
            storage.download("abc.docx")
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_propagates_missing_object(self, storage_and_client):
        storage, client = storage_and_client
        client.get_object.side_effect = _s3_error()
        with pytest.raises(S3Error):
            storage.download("missing.docx")


class TestExists:
    def test_true_when_stat_succeeds(self, storage_and_client):
        storage, client = storage_and_client
        assert storage.exists("abc.docx") is True

    def test_false_on_s3_error(self, storage_and_client):
        storage, client = storage_and_client
        client.stat_object.side_effect = _s3_error()
        assert storage.exists("missing.docx") is False


class TestDelete:
    def test_removes_object(self, storage_and_client):
        storage, client = storage_and_client
        storage.delete("abc.docx")
        client.remove_object.assert_called_once_with("dvd-documents", "abc.docx")

    def test_swallows_failures(self, storage_and_client):
        storage, client = storage_and_client
        client.remove_object.side_effect = _s3_error()
        storage.delete("abc.docx")  # must not raise — best-effort


class TestRepr:
    def test_repr_mentions_bucket(self, storage_and_client):
        storage, _ = storage_and_client
        assert repr(storage) == "DocumentStorage(bucket=dvd-documents)"
