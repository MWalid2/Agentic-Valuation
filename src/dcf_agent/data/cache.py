"""Versioned, credential-free response snapshots shared by external adapters."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


class DataUnavailable(RuntimeError):
    """An input is unavailable; callers must preserve the diagnostic."""


class SnapshotCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def versions(self, key: str) -> list[dict[str, Any]]:
        directory = self.root / hashlib.sha256(key.encode()).hexdigest()
        result = []
        for path in directory.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                datetime.fromisoformat(item["retrieved_at"])
                if (
                    item["key"] == key
                    and item["sha256"]
                    == hashlib.sha256(
                        json.dumps(item["body"], sort_keys=True, allow_nan=False).encode()
                    ).hexdigest()
                ):
                    result.append(item)
            except (ValueError, KeyError, TypeError, OSError):
                continue
        return sorted(result, key=lambda r: r["retrieved_at"], reverse=True)

    def put(self, key: str, body: Any) -> dict[str, Any]:
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        directory = self.root / hashlib.sha256(key.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        item = {
            "key": key,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "sha256": digest,
            "body": body,
        }
        # Preserve the earliest acquisition time when content is unchanged.
        target = directory / (digest + ".json")
        if target.exists():
            existing = [v for v in self.versions(key) if v["sha256"] == digest]
            if existing:
                return existing[0]
        fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(item, stream, allow_nan=False)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return item


def get_response(
    client: httpx.Client,
    url: str,
    params: dict[str, str] | None = None,
    *,
    timeout: float | None = None,
) -> httpx.Response:
    """Bounded retries; never expose request URLs containing API credentials."""
    for attempt in range(3):
        try:
            response = (
                client.get(url, params=params, timeout=timeout)
                if timeout
                else client.get(url, params=params)
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    import time

                    time.sleep(0.5 * (2**attempt))
                    continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError:
            raise DataUnavailable(f"Source returned HTTP {response.status_code}") from None
        except httpx.RequestError:
            if attempt == 2:
                raise DataUnavailable("Source request failed or timed out") from None
    raise DataUnavailable("Source unavailable after retries")
