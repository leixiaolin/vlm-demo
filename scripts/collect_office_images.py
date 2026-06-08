#!/usr/bin/env python
"""Collect public indoor-office images with people activity hints.

The collector intentionally uses public media APIs instead of scraping arbitrary
web pages. It records source and license metadata, strips image metadata when
Pillow is available, and can optionally blur detected faces when OpenCV is
installed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
USER_AGENT = "vlm-demo-office-risk-research/1.0 (public image collection; public media APIs)"
DEFAULT_TARGET_COUNT = 200
DEFAULT_MIN_WIDTH = 640
DEFAULT_MIN_HEIGHT = 480
DEFAULT_DELAY_SECONDS = 0.7
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_THUMB_WIDTH = 1024

DEFAULT_QUERIES = [
    "office people working",
    "people working in office",
    "office workers computers",
    "employees office desks",
    "open office workers",
    "coworking space people",
    "office meeting people",
    "business meeting office",
    "conference room people meeting",
    "office team discussion",
    "startup office people",
    "call center office workers",
    "workplace people computers",
    "office reception people",
    "training room office people",
    "people at desks office",
]

NEGATIVE_HINTS = [
    "empty office",
    "office building",
    "oval office",
    "white house",
    "president",
    "official photograph",
    "exterior",
    "logo",
    "icon",
    "floor plan",
    "map",
    "illustration",
    "cartoon",
    "render",
    "home office",
    "office toy",
    "toy",
    "facade",
    "campus",
]

PERSON_HINTS = [
    "people",
    "person",
    "staff",
    "employee",
    "employees",
    "worker",
    "workers",
    "team",
    "colleague",
    "colleagues",
    "coworker",
    "coworkers",
]

ACTIVITY_HINTS = [
    "meeting",
    "discussion",
    "working",
    "workplace",
    "coworking",
    "conference",
    "training",
    "reception",
    "desk",
    "computer",
]


@dataclass
class Candidate:
    provider: str
    title: str
    source_page: str
    download_url: str
    mime: str
    width: int
    height: int
    query: str
    license_name: str
    license_url: str
    artist: str
    credit: str
    description: str


def strip_html(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ext_value(extmetadata: dict[str, Any], key: str) -> str:
    value = extmetadata.get(key, {})
    if isinstance(value, dict):
        return strip_html(value.get("value", ""))
    return strip_html(value)


def request_json(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def commons_search(query: str, limit: int, timeout: int, thumb_width: int) -> Iterable[Candidate]:
    params: dict[str, Any] = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": min(limit, 50),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|dimensions|extmetadata",
        "iiurlwidth": thumb_width,
        "format": "json",
        "formatversion": 2,
    }
    fetched = 0
    while fetched < limit:
        payload = request_json(COMMONS_API, params, timeout)
        pages = payload.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            break

        for page in pages:
            imageinfo = (page.get("imageinfo") or [{}])[0]
            mime = imageinfo.get("mime", "")
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                continue

            width = int(imageinfo.get("width") or imageinfo.get("thumbwidth") or 0)
            height = int(imageinfo.get("height") or imageinfo.get("thumbheight") or 0)
            extmetadata = imageinfo.get("extmetadata") or {}
            download_url = imageinfo.get("thumburl") or imageinfo.get("url") or ""
            source_page = imageinfo.get("descriptionurl") or ""
            if not download_url or not source_page:
                continue

            yield Candidate(
                provider="wikimedia_commons",
                title=strip_html(page.get("title", "")),
                source_page=source_page,
                download_url=download_url,
                mime=mime,
                width=width,
                height=height,
                query=query,
                license_name=ext_value(extmetadata, "LicenseShortName") or ext_value(extmetadata, "UsageTerms"),
                license_url=ext_value(extmetadata, "LicenseUrl"),
                artist=ext_value(extmetadata, "Artist"),
                credit=ext_value(extmetadata, "Credit"),
                description=ext_value(extmetadata, "ImageDescription") or ext_value(extmetadata, "ObjectName"),
            )
            fetched += 1
            if fetched >= limit:
                break

        continuation = payload.get("continue")
        if not continuation:
            break
        params.update(continuation)


def openverse_search(query: str, limit: int, timeout: int) -> Iterable[Candidate]:
    fetched = 0
    page = 1
    while fetched < limit:
        params = {
            "q": query,
            "page": page,
            "page_size": min(50, limit - fetched),
            "mature": "false",
        }
        payload = request_json(OPENVERSE_API, params, timeout)
        results = payload.get("results", [])
        if not isinstance(results, list) or not results:
            break

        for result in results:
            if result.get("mature") is True:
                continue
            download_url = result.get("url") or result.get("thumbnail") or ""
            source_page = result.get("foreign_landing_url") or result.get("url") or ""
            if not download_url or not source_page:
                continue

            tags = []
            for tag in result.get("tags") or []:
                if isinstance(tag, dict) and tag.get("name"):
                    tags.append(str(tag["name"]))
                elif isinstance(tag, str):
                    tags.append(tag)
            description = " ".join([strip_html(result.get("description", "")), " ".join(tags[:12])]).strip()
            mime = mimetypes.guess_type(urllib.parse.urlparse(download_url).path)[0] or "image/jpeg"

            yield Candidate(
                provider=f"openverse_{strip_html(result.get('source', 'unknown')) or 'unknown'}",
                title=strip_html(result.get("title", "")),
                source_page=source_page,
                download_url=download_url,
                mime=mime,
                width=int(result.get("width") or 0),
                height=int(result.get("height") or 0),
                query=query,
                license_name=strip_html(" ".join(str(part) for part in [result.get("license"), result.get("license_version")] if part)),
                license_url=strip_html(result.get("license_url", "")),
                artist=strip_html(result.get("creator", "")),
                credit=strip_html(result.get("creator_url", "")),
                description=description,
            )
            fetched += 1
            if fetched >= limit:
                break
        page += 1


def text_matches_candidate(candidate: Candidate) -> bool:
    haystack = " ".join([candidate.title, candidate.description]).lower()
    query = candidate.query.lower()
    if any(negative in haystack for negative in NEGATIVE_HINTS):
        return False
    person_in_metadata = any(hint in haystack for hint in PERSON_HINTS)
    activity_in_metadata = any(hint in haystack for hint in ACTIVITY_HINTS)
    if person_in_metadata and activity_in_metadata:
        return True
    query_has_person = any(hint in query for hint in PERSON_HINTS)
    query_has_activity = any(hint in query for hint in ACTIVITY_HINTS)
    if query_has_person and query_has_activity and ("office" in haystack or "work" in haystack):
        return True
    return False


def download_bytes(url: str, timeout: int, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError(f"file is too large: {content_length} bytes")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"file is too large: >{max_bytes} bytes")
    return data


def load_pillow():
    try:
        from PIL import Image, ImageFilter

        return Image, ImageFilter
    except Exception:
        return None, None


def blur_faces_if_possible(image, image_filter_module, enabled: bool):
    if not enabled:
        return image, 0, "metadata_stripped"

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return image, 0, "metadata_stripped_face_blur_unavailable"

    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))

    sanitized = image.copy()
    for x, y, w, h in faces:
        face = sanitized.crop((int(x), int(y), int(x + w), int(y + h)))
        face = face.filter(image_filter_module.GaussianBlur(radius=max(10, int(w) // 4)))
        sanitized.paste(face, (int(x), int(y)))
    return sanitized, len(faces), "face_blurred_metadata_stripped" if len(faces) else "metadata_stripped_no_face_detected"


def sanitize_and_save(
    raw: bytes,
    candidate: Candidate,
    output_path: Path,
    blur_faces: bool,
    require_sanitization: bool,
    min_width: int,
    min_height: int,
) -> tuple[bool, str, int, int, int]:
    Image, ImageFilter = load_pillow()
    if Image is None:
        if require_sanitization:
            return False, "pillow_unavailable_required_for_sanitization", 0, 0, 0
        output_path.write_bytes(raw)
        return True, "public_source_unmodified_pillow_unavailable", candidate.width, candidate.height, 0

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        return False, f"invalid_image:{exc}", 0, 0, 0

    image = image.convert("RGB")
    if image.width < min_width or image.height < min_height:
        return False, f"image_too_small:{image.width}x{image.height}", image.width, image.height, 0
    image, face_count, privacy_status = blur_faces_if_possible(image, ImageFilter, blur_faces)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="JPEG", quality=92, optimize=True)
    return True, privacy_status, image.width, image.height, face_count


def choose_extension(mime: str, sanitized: bool) -> str:
    if sanitized:
        return ".jpg"
    return mimetypes.guess_extension(mime) or ".jpg"


def read_queries(query_file: Path | None) -> list[str]:
    if not query_file:
        return DEFAULT_QUERIES
    queries = []
    for line in query_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            queries.append(stripped)
    return queries or DEFAULT_QUERIES


def existing_inventory(source_metadata_path: Path) -> tuple[set[str], set[str], int]:
    if not source_metadata_path.exists():
        return set(), set(), 0
    hashes: set[str] = set()
    image_ids: set[str] = set()
    max_index = 0
    with source_metadata_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sha256 = row.get("sha256", "")
            image_id = row.get("image_id", "")
            if sha256:
                hashes.add(sha256)
            if image_id:
                image_ids.add(image_id)
                match = re.fullmatch(r"office_public_(\d+)", image_id)
                if match:
                    max_index = max(max_index, int(match.group(1)))
    return hashes, image_ids, max_index


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect 150-300 public indoor-office images with people activity hints."
    )
    parser.add_argument(
        "--target-count",
        type=int,
        default=DEFAULT_TARGET_COUNT,
        help="Target total inventory count after this run. Recommended: 150-300.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/office_images/images"), help="Directory for collected images.")
    parser.add_argument("--manifest-path", type=Path, default=Path("data/office_images/evaluation_manifest_collected.csv"))
    parser.add_argument("--source-metadata-path", type=Path, default=Path("data/office_images/source_metadata.csv"))
    parser.add_argument(
        "--sources",
        default="openverse,commons",
        help="Comma-separated sources: openverse,commons. Default: openverse,commons.",
    )
    parser.add_argument("--query-file", type=Path, help="Optional UTF-8 text file with one search query per line.")
    parser.add_argument("--per-query-limit", type=int, default=80, help="Maximum results to inspect per source/query.")
    parser.add_argument("--min-width", type=int, default=DEFAULT_MIN_WIDTH)
    parser.add_argument("--min-height", type=int, default=DEFAULT_MIN_HEIGHT)
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS, help="Delay between downloads.")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--thumb-width", type=int, default=DEFAULT_THUMB_WIDTH, help="Requested Wikimedia thumbnail width.")
    parser.add_argument("--blur-faces", action="store_true", help="Blur detected frontal faces when OpenCV is installed.")
    parser.add_argument("--require-sanitization", action="store_true", help="Skip images when Pillow cannot strip metadata.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned queries and exit without network calls.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.target_count <= 0 or args.target_count > 300:
        print("--target-count must be between 1 and 300.", file=sys.stderr)
        return 2

    queries = read_queries(args.query_file)
    if args.dry_run:
        print(json.dumps({"target_count": args.target_count, "queries": queries}, ensure_ascii=False, indent=2))
        return 0

    manifest_fields = [
        "image_id",
        "image_path_or_url",
        "source_type",
        "privacy_status",
        "scene_type",
        "zone_type",
        "restricted_area",
        "split",
        "notes",
    ]
    source_fields = [
        "image_id",
        "file_path",
        "sha256",
        "source_type",
        "source_page",
        "download_url",
        "title",
        "query",
        "license_name",
        "license_url",
        "artist",
        "credit",
        "description",
        "width",
        "height",
        "mime",
        "face_count_detected",
    ]
    sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]
    invalid_sources = sorted(set(sources) - {"openverse", "commons"})
    if invalid_sources:
        print(f"Unsupported --sources values: {', '.join(invalid_sources)}", file=sys.stderr)
        return 2

    seen_hashes, existing_ids, max_existing_index = existing_inventory(args.source_metadata_path)
    existing_count = len(existing_ids)
    remaining = max(0, args.target_count - existing_count)
    if remaining == 0:
        print(
            json.dumps(
                {
                    "target_count": args.target_count,
                    "existing_count": existing_count,
                    "collected_new": 0,
                    "manifest_path": str(args.manifest_path),
                    "source_metadata_path": str(args.source_metadata_path),
                    "image_dir": str(args.output_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    collected = 0
    inspected = 0
    skipped = 0

    for query in queries:
        if collected >= remaining:
            break
        for source in sources:
            if collected >= remaining:
                break
            print(f"[query:{source}] {query}", flush=True)
            try:
                if source == "openverse":
                    candidates = openverse_search(query, args.per_query_limit, args.timeout_seconds)
                else:
                    candidates = commons_search(query, args.per_query_limit, args.timeout_seconds, args.thumb_width)
                for candidate in candidates:
                    if collected >= remaining:
                        break
                    inspected += 1
                    if (
                        candidate.width
                        and candidate.height
                        and (candidate.width < args.min_width or candidate.height < args.min_height)
                    ):
                        skipped += 1
                        continue
                    if not text_matches_candidate(candidate):
                        skipped += 1
                        continue

                    try:
                        raw = download_bytes(candidate.download_url, args.timeout_seconds, args.max_bytes)
                    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                        skipped += 1
                        print(f"[skip-download] {candidate.title}: {exc}", flush=True)
                        continue

                    sha256 = hashlib.sha256(raw).hexdigest()
                    if sha256 in seen_hashes:
                        skipped += 1
                        continue

                    next_index = max_existing_index + collected + 1
                    image_id = f"office_public_{next_index:04d}"
                    sanitized = load_pillow()[0] is not None
                    extension = choose_extension(candidate.mime, sanitized)
                    image_path = args.output_dir / f"{image_id}{extension}"
                    while image_id in existing_ids or image_path.exists():
                        next_index += 1
                        image_id = f"office_public_{next_index:04d}"
                        image_path = args.output_dir / f"{image_id}{extension}"
                    ok, privacy_status, width, height, face_count = sanitize_and_save(
                        raw,
                        candidate,
                        image_path,
                        args.blur_faces,
                        args.require_sanitization,
                        args.min_width,
                        args.min_height,
                    )
                    if not ok:
                        skipped += 1
                        print(f"[skip-sanitize] {candidate.title}: {privacy_status}", flush=True)
                        continue

                    seen_hashes.add(sha256)
                    existing_ids.add(image_id)
                    relative_path = image_path.as_posix()
                    manifest_row = {
                        "image_id": image_id,
                        "image_path_or_url": relative_path,
                        "source_type": f"public_{candidate.provider}",
                        "privacy_status": privacy_status,
                        "scene_type": "unknown",
                        "zone_type": "unknown",
                        "restricted_area": "false",
                        "split": "unassigned",
                        "notes": "Public image selected by office/people activity query; manual review still required.",
                    }
                    source_row = {
                        "image_id": image_id,
                        "file_path": relative_path,
                        "sha256": sha256,
                        "source_type": candidate.provider,
                        "source_page": candidate.source_page,
                        "download_url": candidate.download_url,
                        "title": candidate.title,
                        "query": candidate.query,
                        "license_name": candidate.license_name,
                        "license_url": candidate.license_url,
                        "artist": candidate.artist,
                        "credit": candidate.credit,
                        "description": candidate.description,
                        "width": width,
                        "height": height,
                        "mime": candidate.mime,
                        "face_count_detected": face_count,
                    }
                    append_csv(args.manifest_path, manifest_fields, [manifest_row])
                    append_csv(args.source_metadata_path, source_fields, [source_row])
                    collected += 1
                    print(f"[ok] {image_id} {width}x{height} {privacy_status} {candidate.title}", flush=True)
                    time.sleep(args.delay_seconds)
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print(f"[query-error] {source}:{query}: {exc}", file=sys.stderr, flush=True)
                continue

    summary = {
        "target_count": args.target_count,
        "existing_count_before_run": existing_count,
        "collected_new": collected,
        "total_count_after_run": existing_count + collected,
        "inspected_candidates": inspected,
        "skipped": skipped,
        "manifest_path": str(args.manifest_path),
        "source_metadata_path": str(args.source_metadata_path),
        "image_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if collected < remaining:
        print(
            "Collected fewer images than requested. Add more queries with --query-file, lower min dimensions, "
            "or rerun later. Manual review is still required before model evaluation.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
