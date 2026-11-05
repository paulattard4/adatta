"""Raw payload storage behind a small interface (R2 in prod, local disk for offline dev)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from adatta.config import Settings


class RawStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalStore:
    def __init__(self, root: Path | str = "data/raw") -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class R2Store:
    def __init__(self, settings: Settings) -> None:
        import boto3

        self._bucket = settings.r2_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return body


def make_store(settings: Settings) -> RawStore:
    return R2Store(settings) if settings.storage_backend == "r2" else LocalStore()


def encode_payload(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Canonical gzip JSON plus content hash of the uncompressed canonical form."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()


def storage_key(source_id: str, endpoint: str, date: str, content_hash: str) -> str:
    return f"{source_id}/{endpoint.strip('/').replace('/', '_')}/{date}/{content_hash[:16]}.json.gz"
