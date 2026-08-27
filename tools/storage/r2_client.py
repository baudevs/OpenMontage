"""Cloudflare R2 client — public URLs for media a provider must fetch itself.

Several providers refuse local files and Base64 and will only ingest a URL they
can download (AnyFast's asset library is the driving case: video accepts a URL
or `asset://` ID and nothing else). R2 with the bucket's public development URL
enabled gives us that URL.

R2 speaks the S3 API, so this signs requests with AWS SigV4 directly rather than
pulling in boto3 — `requests` plus hmac/hashlib is enough for PUT/HEAD/DELETE
and keeps the install footprint unchanged.

Configuration (all required; presence of all five is what "R2 is enabled" means):

    R2_ACCOUNT_ID          Cloudflare account id
    R2_ACCESS_KEY_ID       R2 API token access key
    R2_SECRET_ACCESS_KEY   R2 API token secret
    R2_BUCKET              bucket name
    R2_PUBLIC_BASE_URL     the bucket's public URL, e.g. https://pub-<hash>.r2.dev

Optional:

    R2_KEY_PREFIX          key prefix for every upload (default "openmontage")
    R2_ENDPOINT            override the S3 endpoint (default <account>.r2.cloudflarestorage.com)
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

SERVICE = "s3"
REGION = "auto"
ALGORITHM = "AWS4-HMAC-SHA256"
DEFAULT_KEY_PREFIX = "openmontage"
CONNECT_TIMEOUT_SECONDS = 15.0
TRANSFER_TIMEOUT_SECONDS = 600.0

REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
)


class R2ConfigError(RuntimeError):
    """R2 is not configured, or is configured incompletely."""


def missing_env() -> list[str]:
    return [name for name in REQUIRED_ENV if not os.environ.get(name)]


def is_configured() -> bool:
    return not missing_env()


def get_config() -> dict[str, str]:
    missing = missing_env()
    if missing:
        raise R2ConfigError(
            "R2 is not configured; missing " + ", ".join(missing) + ". "
            "Create an R2 bucket, enable its public development URL, mint an R2 API "
            "token, and set these in .env."
        )
    account = os.environ["R2_ACCOUNT_ID"]
    endpoint = os.environ.get("R2_ENDPOINT") or f"https://{account}.r2.cloudflarestorage.com"
    if not endpoint.startswith("https://"):
        raise R2ConfigError("R2_ENDPOINT must be an https:// URL")
    public_base = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
    if not public_base.startswith("https://"):
        raise R2ConfigError(
            "R2_PUBLIC_BASE_URL must be an https:// URL — the bucket's public "
            "development URL (https://pub-<hash>.r2.dev) or a custom domain"
        )
    return {
        "account_id": account,
        "access_key": os.environ["R2_ACCESS_KEY_ID"],
        "secret_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "bucket": os.environ["R2_BUCKET"],
        "endpoint": endpoint.rstrip("/"),
        "public_base_url": public_base,
        "key_prefix": (os.environ.get("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX).strip("/"),
    }


# ---- key naming ----


def safe_segment(value: str) -> str:
    """Lowercase, dash-separated, URL-safe path segment."""
    cleaned = "".join(char if char.isalnum() or char in "-_." else "-" for char in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-.").lower() or "file"


# What an object is for. `faces` is kept after ingestion (re-registering a person
# means re-uploading); everything else is transient and swept once the provider
# has copied it.
KINDS = ("faces", "refs", "video", "audio")
RETAINED_KINDS = ("faces",)


def build_key(
    filename: str,
    *,
    prefix: str | None = None,
    project: str | None = None,
    kind: str | None = None,
    folder: str | None = None,
    unique: bool = True,
) -> str:
    """Build an object key: `<prefix>/<project>/<kind>/<name>-<random>.<ext>`.

    Project first so one client's media is a single prefix — finishing a project
    is then one sweep — and `kind` under it so retention can treat faces
    differently from disposable references.

    Uniqueness is on by default because overwriting a key that a provider has
    already fetched (or is about to) silently changes what it ingests — and
    AnyFast in particular rejects a second asset that reuses a name.
    """
    config_prefix = prefix if prefix is not None else (
        os.environ.get("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX
    )
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower()
    name = safe_segment(stem)
    if unique:
        name = f"{name}-{uuid.uuid4().hex[:8]}"
    segments = [config_prefix, project, kind, folder]
    parts = [safe_segment(part) for part in segments if part and str(part).strip("/")]
    parts.append(f"{name}{suffix}")
    return "/".join(parts)


def key_prefix_for(project: str | None = None, kind: str | None = None) -> str:
    """The prefix a listing/sweep should target."""
    config_prefix = os.environ.get("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX
    parts = [safe_segment(p) for p in (config_prefix, project, kind) if p and str(p).strip("/")]
    return "/".join(parts) + "/"


def is_retained(key: str) -> bool:
    """True when a key belongs to a kind we keep after the provider ingests it."""
    return any(f"/{kind}/" in f"/{key}" for kind in RETAINED_KINDS)


# ---- SigV4 ----


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str) -> bytes:
    key = _sign(f"AWS4{secret}".encode("utf-8"), datestamp)
    key = _sign(key, REGION)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def signed_headers(
    method: str,
    *,
    config: dict[str, str],
    key: str = "",
    payload: bytes = b"",
    content_type: str | None = None,
    query: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return (url, headers) for an SigV4-signed R2 request."""
    host = config["endpoint"].removeprefix("https://")
    path = f"{config['bucket']}/{key}" if key else config["bucket"]
    canonical_uri = "/" + quote(path, safe="/~")
    canonical_query = "&".join(
        f"{quote(name, safe='~')}={quote(str(value), safe='~')}"
        for name, value in sorted((query or {}).items())
    )
    url = f"https://{host}{canonical_uri}" + (f"?{canonical_query}" if canonical_query else "")

    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    if content_type:
        headers["content-type"] = content_type

    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed, payload_hash]
    )
    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(config["secret_key"], datestamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["Authorization"] = (
        f"{ALGORITHM} Credential={config['access_key']}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return url, headers


def public_url(key: str, config: dict[str, str] | None = None) -> str:
    config = config or get_config()
    return f"{config['public_base_url']}/{quote(key, safe='/~')}"


# ---- operations ----


def upload_file(
    path: str | Path,
    *,
    project: str | None = None,
    kind: str | None = None,
    folder: str | None = None,
    key: str | None = None,
    unique: bool = True,
    content_type: str | None = None,
    verify_public: bool = True,
) -> dict[str, Any]:
    """Upload one local file and return its public URL.

    `verify_public` issues a HEAD against the public URL afterwards. It is on by
    default because the failure it catches — a bucket whose public development
    URL is not enabled — otherwise surfaces much later as an opaque provider-side
    download error.
    """
    import requests

    config = get_config()
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"file not found: {source}")
    object_key = key or build_key(
        source.name, project=project, kind=kind, folder=folder, unique=unique
    )
    payload = source.read_bytes()
    guessed = content_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"

    url, headers = signed_headers(
        "PUT", config=config, key=object_key, payload=payload, content_type=guessed
    )
    response = requests.put(
        url,
        headers=headers,
        data=payload,
        timeout=(CONNECT_TIMEOUT_SECONDS, TRANSFER_TIMEOUT_SECONDS),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"R2 upload failed ({response.status_code}): {response.text[:300]}"
        )

    result = {
        "key": object_key,
        "url": public_url(object_key, config),
        "bucket": config["bucket"],
        "size_bytes": len(payload),
        "content_type": guessed,
        "public_verified": False,
        "retained": is_retained(object_key),
    }
    if verify_public:
        result["public_verified"] = wait_until_public(result["url"])
        if not result["public_verified"]:
            raise RuntimeError(
                f"uploaded to R2 but {result['url']} is not publicly readable. "
                "Enable the bucket's public development URL (R2 > bucket > Settings > "
                "Public Development URL) or point R2_PUBLIC_BASE_URL at a custom domain; "
                "a provider cannot ingest a private object."
            )
    return result


def wait_until_public(url: str, *, attempts: int = 5, delay: float = 1.0) -> bool:
    """HEAD the public URL until it answers 200 (propagation is not instant)."""
    import requests

    for attempt in range(attempts):
        try:
            response = requests.head(url, timeout=(CONNECT_TIMEOUT_SECONDS, 30), allow_redirects=True)
            if response.status_code == 200:
                return True
            if response.status_code in (401, 403):
                return False
        except requests.RequestException:
            pass
        if attempt < attempts - 1:
            time.sleep(delay * (attempt + 1))
    return False


def delete_object(key: str) -> dict[str, Any]:
    import requests

    config = get_config()
    url, headers = signed_headers("DELETE", config=config, key=key)
    response = requests.delete(url, headers=headers, timeout=(CONNECT_TIMEOUT_SECONDS, 60))
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(f"R2 delete failed ({response.status_code}): {response.text[:200]}")
    return {"key": key, "deleted": response.status_code < 400}


def object_exists(key: str) -> bool:
    import requests

    config = get_config()
    url, headers = signed_headers("HEAD", config=config, key=key)
    response = requests.head(url, headers=headers, timeout=(CONNECT_TIMEOUT_SECONDS, 30))
    return response.status_code == 200


def list_objects(prefix: str = "", *, max_keys: int = 1000) -> list[dict[str, Any]]:
    """List objects under a prefix (ListObjectsV2, paginated)."""
    import requests
    from xml.etree import ElementTree

    config = get_config()
    namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    objects: list[dict[str, Any]] = []
    token: str | None = None

    while True:
        query = {"list-type": "2", "max-keys": str(max_keys)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        url, headers = signed_headers("GET", config=config, key="", query=query)
        response = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT_SECONDS, 120))
        if response.status_code >= 400:
            raise RuntimeError(f"R2 list failed ({response.status_code}): {response.text[:300]}")
        root = ElementTree.fromstring(response.text)
        for item in root.findall(f"{namespace}Contents"):
            key = (item.findtext(f"{namespace}Key") or "").strip()
            if not key:
                continue
            objects.append(
                {
                    "key": key,
                    "size_bytes": int(item.findtext(f"{namespace}Size") or 0),
                    "last_modified": item.findtext(f"{namespace}LastModified"),
                    "url": public_url(key, config),
                    "retained": is_retained(key),
                }
            )
        if (root.findtext(f"{namespace}IsTruncated") or "").lower() != "true":
            return objects
        token = root.findtext(f"{namespace}NextContinuationToken")
        if not token:
            return objects


def sweep(
    *,
    project: str | None = None,
    kind: str | None = None,
    older_than_days: float = 7.0,
    include_retained: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Delete transient objects the provider has already ingested.

    `faces` are skipped unless `include_retained` is set: re-registering a person
    means re-uploading their source images, so those are worth keeping. Defaults
    to a dry run — deletion is not reversible.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=float(older_than_days))
    candidates = []
    for item in list_objects(key_prefix_for(project, kind)):
        if item["retained"] and not include_retained:
            continue
        stamp = item.get("last_modified")
        if stamp:
            try:
                modified = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                modified = None
            if modified and modified > cutoff:
                continue
        candidates.append(item)

    deleted = []
    if not dry_run:
        for item in candidates:
            delete_object(item["key"])
            deleted.append(item["key"])
    return {
        "prefix": key_prefix_for(project, kind),
        "older_than_days": older_than_days,
        "candidates": [item["key"] for item in candidates],
        "candidate_bytes": sum(item["size_bytes"] for item in candidates),
        "deleted": deleted,
        "dry_run": dry_run,
        "skipped_retained": not include_retained,
    }

