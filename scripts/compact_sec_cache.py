"""Compact the SEC response cache without losing any information.

1. Version copies of immutable filing files are removed: a file inside an
   accession folder never changes after filing, so its active snapshot suffices.
   Versions of mutable responses (companyfacts, submissions) are kept.
2. Active snapshots of immutable filing files are rewritten gzip-compressed
   (``.json.gz``); the SEC client reads both formats.

Use --dry-run to report savings without changing anything.
"""

import argparse
import json
from pathlib import Path

from dcf_agent.config import load_config
from dcf_agent.data.sec import IMMUTABLE, read_snapshot, write_snapshot

ROOT = Path(__file__).resolve().parents[1]


def url_of(path: Path) -> str:
    text = read_snapshot(path)
    # The URL is the first field; avoid parsing a large body only to read it.
    head = text[:2000]
    marker = '"url": "'
    if marker in head:
        return head.split(marker, 1)[1].split('"', 1)[0]
    return str(json.loads(text)["url"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cache = load_config(ROOT).settings.raw_cache_dir / "sec"
    removed = compressed = before = after = 0
    versions = cache / "versions"
    if versions.exists():
        for path in versions.glob("*.json"):
            if IMMUTABLE.match(url_of(path)):
                removed += path.stat().st_size
                if not args.dry_run:
                    path.unlink()
    for path in cache.glob("*.json"):
        if not IMMUTABLE.match(url_of(path)):
            continue
        text = path.read_text(encoding="utf-8")
        json.loads(text)  # Never rewrite a corrupt snapshot.
        target = path.with_name(path.name + ".gz")
        before += path.stat().st_size
        if args.dry_run:
            continue
        temporary = target.with_name(target.name + ".tmp")
        write_snapshot(temporary, text, compressed=True)
        if read_snapshot(temporary) != text:
            raise SystemExit(f"Round-trip check failed for {path.name}")
        temporary.replace(target)
        path.unlink()
        after += target.stat().st_size
        compressed += 1
    mb = 1024 * 1024
    print(f"Removed immutable version copies: {removed / mb:,.0f} MB")
    if args.dry_run:
        print(f"Immutable snapshots to compress: {before / mb:,.0f} MB")
    else:
        print(f"Compressed {compressed} snapshots: {before / mb:,.0f} MB -> {after / mb:,.0f} MB")


if __name__ == "__main__":
    main()
