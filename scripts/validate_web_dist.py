from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


WEB_MANIFEST_NAME = "integrity.json"
WEB_MANIFEST_SCHEMA_VERSION = 1


class _AssetReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.references.append(("script", attributes["src"] or ""))
        elif tag == "link" and attributes.get("href"):
            relationships = {item.lower() for item in (attributes.get("rel") or "").split()}
            if "stylesheet" in relationships:
                self.references.append(("stylesheet", attributes["href"] or ""))


def _local_reference_path(dist_dir: Path, reference: str) -> Path | None:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None

    decoded_path = unquote(parsed.path).replace("/", "\\")
    if decoded_path.startswith("\\"):
        raise ValueError(f"absolute local reference is not allowed: {reference}")

    resolved = (dist_dir / decoded_path).resolve()
    try:
        resolved.relative_to(dist_dir)
    except ValueError as exc:
        raise ValueError(f"reference escapes Web UI directory: {reference}") from exc
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _web_files(dist_dir: Path) -> tuple[Path, ...]:
    manifest_path = dist_dir / WEB_MANIFEST_NAME
    return tuple(
        path
        for path in sorted(dist_dir.rglob("*"))
        if path.is_file() and path != manifest_path
    )


def write_web_manifest(dist: Path) -> Path:
    dist_dir = dist.resolve()
    files = _web_files(dist_dir)
    if not files:
        raise ValueError(f"Web UI directory is empty: {dist_dir}")
    payload = {
        "schema_version": WEB_MANIFEST_SCHEMA_VERSION,
        "algorithm": "sha256",
        "files": [
            {
                "path": path.relative_to(dist_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
    }
    manifest_path = dist_dir / WEB_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validate_web_manifest(dist_dir: Path, *, required: bool) -> None:
    manifest_path = dist_dir / WEB_MANIFEST_NAME
    if not manifest_path.is_file():
        if required:
            raise ValueError(f"Web UI integrity manifest is missing: {manifest_path}")
        return
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Web UI integrity manifest is invalid: {manifest_path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != WEB_MANIFEST_SCHEMA_VERSION
        or payload.get("algorithm") != "sha256"
        or not isinstance(payload.get("files"), list)
    ):
        raise ValueError(f"Web UI integrity manifest is invalid: {manifest_path}")

    expected: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(payload["files"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Web UI integrity manifest item {index} is invalid")
        relative = str(item.get("path") or "").replace("\\", "/")
        path = Path(relative)
        size = item.get("size")
        sha256 = str(item.get("sha256") or "").lower()
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or relative in expected
            or not isinstance(size, int)
            or size <= 0
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError(f"Web UI integrity manifest item {index} is invalid")
        expected[relative] = (size, sha256)

    actual = {
        path.relative_to(dist_dir).as_posix(): path
        for path in _web_files(dist_dir)
    }
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        raise ValueError(f"Web UI integrity manifest references missing file: {missing[0]}")
    if extra:
        raise ValueError(f"Web UI integrity manifest omits file: {extra[0]}")
    for relative, path in actual.items():
        expected_size, expected_sha256 = expected[relative]
        if path.stat().st_size != expected_size:
            raise ValueError(f"Web UI integrity size mismatch: {relative}")
        if _sha256(path) != expected_sha256:
            raise ValueError(f"Web UI integrity SHA-256 mismatch: {relative}")


def validate_web_dist(dist: Path, *, require_manifest: bool = False) -> tuple[Path, ...]:
    dist_dir = dist.resolve()
    if not dist_dir.is_dir():
        raise ValueError(f"Web UI directory does not exist: {dist_dir}")

    index_path = dist_dir / "index.html"
    if not index_path.is_file():
        raise ValueError(f"Web UI entry point is missing: {index_path}")
    if index_path.stat().st_size <= 0:
        raise ValueError(f"Web UI entry point is empty: {index_path}")

    parser = _AssetReferenceParser()
    parser.feed(index_path.read_text(encoding="utf-8"))

    local_assets: list[Path] = []
    kinds: set[str] = set()
    for kind, reference in parser.references:
        asset_path = _local_reference_path(dist_dir, reference)
        if asset_path is None:
            continue
        if not asset_path.is_file():
            raise ValueError(f"Web UI {kind} asset is missing: {reference} ({asset_path})")
        if asset_path.stat().st_size <= 0:
            raise ValueError(f"Web UI {kind} asset is empty: {reference} ({asset_path})")
        local_assets.append(asset_path)
        kinds.add(kind)

    missing_kinds = {"script", "stylesheet"} - kinds
    if missing_kinds:
        raise ValueError(
            "Web UI entry point has no local " + " or ".join(sorted(missing_kinds)) + " asset reference."
        )
    _validate_web_manifest(dist_dir, required=require_manifest)
    return tuple(local_assets)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a built Voice Input Web UI directory and its referenced assets."
    )
    parser.add_argument("dist", type=Path, help="Directory containing index.html and assets")
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help=f"Write {WEB_MANIFEST_NAME} before validating the directory.",
    )
    parser.add_argument(
        "--require-manifest",
        action="store_true",
        help=f"Require and validate {WEB_MANIFEST_NAME}.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.write_manifest:
            write_web_manifest(args.dist)
        assets = validate_web_dist(
            args.dist,
            require_manifest=args.require_manifest or args.write_manifest,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Web UI validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Web UI validation passed: {args.dist.resolve()} ({len(assets)} referenced assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
