"""Shared client pieces for the AnyFast gateway (https://www.anyfast.ai).

Used by `anyfast_video` (Seedance generation) and `anyfast_assets` (the
`/volc/asset` asset library, including the real-human LivenessFace flow).

Everything here is transport: auth, timeouts, error redaction, and the thin
request wrappers. Model envelopes and payload rules live with the tools.
"""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

BASE_URL = "https://www.anyfast.ai"

# Connect fast, then wait. The gateway holds the connection while it relays a
# create call upstream, which a short read timeout turns into a "timeout on
# every call" report even when the request was well formed.
CONNECT_TIMEOUT_SECONDS = 15.0
DEFAULT_CREATE_TIMEOUT_SECONDS = 300.0
QUERY_TIMEOUT_SECONDS = 60.0
TRANSFER_TIMEOUT_SECONDS = 600.0

MODELS_PATH = "/v1/models"
VIDEO_PATH = "/v1/video/generations"

ASSET_PATHS = {
    "create_group": "/volc/asset/CreateAssetGroup",
    "create_asset": "/volc/asset/CreateAsset",
    "get_asset": "/volc/asset/GetAsset",
    "list_groups": "/volc/asset/ListAssetGroups",
    "list_assets": "/volc/asset/ListAssets",
    "update_asset": "/volc/asset/UpdateAsset",
    "update_group": "/volc/asset/UpdateAssetGroup",
    "delete_asset": "/volc/asset/DeleteAsset",
    "delete_group": "/volc/asset/DeleteAssetGroup",
    "create_liveness_session": "/volc/asset/CreateVisualValidateSession",
    "get_liveness_result": "/volc/asset/GetVisualValidateResult",
}

# CreateAsset routes by billing model; images are the default.
ASSET_MODELS = {"Image": "volc-asset", "Video": "volc-asset-video", "Audio": "volc-asset-audio"}
ASSET_TERMINAL_STATUSES = frozenset({"Active", "Failed"})
GROUP_TYPES = ("AIGC", "LivenessFace")


def get_api_key() -> str | None:
    return os.environ.get("ANYFAST_API_KEY")


def get_base_url() -> str:
    base_url = os.environ.get("ANYFAST_BASE_URL", BASE_URL).rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("ANYFAST_BASE_URL must be an https:// URL")
    return base_url


def check_api_key(api_key: str | None) -> None:
    """Reject the most common misconfiguration before any request is sent."""
    if not api_key:
        raise ValueError(
            "ANYFAST_API_KEY not set. Create a key at https://www.anyfast.ai/console/token"
        )
    if api_key.lower().startswith("bearer "):
        raise ValueError(
            "ANYFAST_API_KEY must contain only the key body; remove the 'Bearer ' prefix"
        )


def json_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def auth_headers(api_key: str) -> dict[str, str]:
    """Auth without Content-Type, so requests can set a multipart boundary."""
    return {"Authorization": f"Bearer {api_key}"}


def is_remote_or_asset(value: str) -> bool:
    return value.startswith(("https://", "http://", "asset://"))


def _error_detail(payload: Any) -> str:
    """Pull a human message out of any of AnyFast's error shapes.

    Three are in circulation:
      {"error": {"type", "code", "message"}}                      gateway errors
      {"code", "message": "<json string>", "data": null}          relayed upstream errors
      {"code", "message": "<plain text>"}                         everything else
    The middle one nests a JSON document inside `message`, which is where the
    actionable text lives (e.g. an invalid `content[1].image_url.url`).
    """
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return ": ".join(
            str(error.get(key)) for key in ("type", "code", "message") if error.get(key)
        )
    message = payload.get("message")
    if isinstance(message, str) and message.strip().startswith("{"):
        import json as _json

        try:
            nested = _json.loads(message)
        except ValueError:
            return message
        nested_detail = _error_detail(nested)
        if nested_detail:
            code = payload.get("code")
            return f"{code}: {nested_detail}" if code else nested_detail
        return message
    if message:
        return f"{payload.get('code')}: {message}" if payload.get("code") else str(message)
    return ""


def raise_for_status(response: Any) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        detail = ""
        try:
            detail = _error_detail(response.json())
        except Exception:
            pass
        if not detail:
            # Never swallow the body: an unrecognised shape still carries the reason.
            body = (getattr(response, "text", "") or "").strip()
            detail = body[:400]
        raise RuntimeError(f"{exc}" + (f"; {detail}" if detail else "")) from exc


def safe_error(exc: Exception, api_key: str | None = None) -> str:
    """Strip credentials, signed URLs, and Base64 blobs out of error text."""
    message = str(exc)
    for secret in {value for value in (api_key, get_api_key()) if value}:
        message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"data:(?:image|audio|video)/[^;\s]+;base64,[A-Za-z0-9+/=]+",
        "[redacted data URI]",
        message,
        flags=re.IGNORECASE,
    )

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;:)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return (
                urlunsplit(
                    (
                        parsed.scheme,
                        host,
                        parsed.path,
                        "[redacted]" if parsed.query else "",
                        "",
                    )
                )
                + trailing
            )
        except ValueError:
            return "[redacted URL]"

    message = re.sub(r"https?://[^\s'\"<>]+", redact_url, message, flags=re.IGNORECASE)
    return re.sub(
        r"(?i)authorization\s*[:=]\s*bearer\s+\S+",
        "Authorization: Bearer [redacted]",
        message,
    )


def read_timeout(raw: Any, env_var: str, default: float, label: str) -> float:
    """Resolve an input/env read-timeout override."""
    if raw is None:
        raw = os.environ.get(env_var)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number greater than 0") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a number greater than 0")
    return value


def post_json(
    path: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    import requests

    response = requests.post(
        f"{get_base_url()}{path}",
        headers=json_headers(api_key),
        json=payload if payload is not None else {},
        timeout=(CONNECT_TIMEOUT_SECONDS, timeout),
    )
    raise_for_status(response)
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"AnyFast {path} returned a non-object response")
    return body


def verify_access(api_key: str, known_models: list[str] | None = None) -> dict[str, Any]:
    """Free reachability + credential probe against GET /v1/models."""
    import requests

    started = time.monotonic()
    response = requests.get(
        f"{get_base_url()}{MODELS_PATH}",
        headers=json_headers(api_key),
        timeout=(CONNECT_TIMEOUT_SECONDS, QUERY_TIMEOUT_SECONDS),
    )
    latency = round(time.monotonic() - started, 3)
    try:
        body = response.json()
    except Exception:
        body = {}
    model_ids: list[str] = []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        model_ids = [
            str(entry.get("id"))
            for entry in body["data"]
            if isinstance(entry, dict) and entry.get("id")
        ]
    error = None
    if response.status_code != 200 and isinstance(body, dict):
        error = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
    return {
        "endpoint": f"GET {get_base_url()}{MODELS_PATH}",
        "http_status": response.status_code,
        "key_accepted": response.status_code == 200,
        "latency_seconds": latency,
        "model_count": len(model_ids),
        "expected_models_visible": [m for m in (known_models or []) if m in model_ids],
        "error": error,
    }


# ---- asset library ----


def create_asset_group(
    api_key: str, name: str, asset_kind: str = "Image", group_type: str | None = None
) -> str:
    payload: dict[str, Any] = {"model": ASSET_MODELS[asset_kind], "Name": name}
    if group_type:
        payload["GroupType"] = group_type
    body = post_json(ASSET_PATHS["create_group"], api_key, payload)
    group_id = body.get("Id")
    if not group_id:
        raise RuntimeError("AnyFast CreateAssetGroup returned no Id")
    return str(group_id)


# Upload envelopes. A real-human (LivenessFace) group is stricter than the
# generic AIGC library, and an out-of-envelope file is accepted by CreateAsset
# and only fails later as Status=Failed — so check before uploading.
ASSET_LIMITS = {
    "AIGC": {
        "Image": {"suffixes": (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"), "max_mb": 30},
        "Video": {"suffixes": (".mp4", ".mov"), "max_mb": 200, "duration": (2, 30), "fps": (24, 60), "pixels": (407696, 8295044)},
        "Audio": {"suffixes": (".wav", ".mp3"), "max_mb": 15, "duration": (2, 30)},
    },
    "LivenessFace": {
        "Image": {"suffixes": (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"), "max_mb": 30},
        "Video": {"suffixes": (".mp4", ".mov"), "max_mb": 50, "duration": (2, 15), "fps": (24, 60), "pixels": (407696, 8295044)},
        "Audio": {"suffixes": (".wav", ".mp3"), "max_mb": 15, "duration": (2, 15)},
    },
}


def probe_media(path: Path) -> dict[str, Any]:
    """Duration / dimensions / fps via ffprobe. Empty dict when ffprobe is absent."""
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate:format=duration",
                "-of", "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    info: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("width", "height"):
            try:
                info[key] = int(value)
            except ValueError:
                pass
        elif key == "duration":
            try:
                info["duration"] = float(value)
            except ValueError:
                pass
        elif key == "r_frame_rate" and "/" in value:
            num, _, den = value.partition("/")
            try:
                info["fps"] = float(num) / float(den) if float(den) else None
            except (ValueError, ZeroDivisionError):
                pass
    return info


def validate_asset_file(path: Path, asset_kind: str, group_type: str | None = None) -> None:
    """Reject a local file the asset library would fail asynchronously."""
    limits = ASSET_LIMITS.get(group_type or "AIGC", ASSET_LIMITS["AIGC"]).get(asset_kind)
    if not limits:
        return
    label = "real-human" if group_type == "LivenessFace" else "asset"
    suffix = path.suffix.lower()
    if suffix not in limits["suffixes"]:
        raise ValueError(
            f"{label} {asset_kind.lower()} must be one of "
            + ", ".join(limits["suffixes"])
            + f" (got {suffix or 'no extension'})"
        )
    size_mb = path.stat().st_size / 1_000_000
    if size_mb > limits["max_mb"]:
        raise ValueError(
            f"{label} {asset_kind.lower()} must be at most {limits['max_mb']} MB "
            f"(got {size_mb:.1f} MB)"
        )
    if asset_kind == "Image":
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except Exception:
            return
        if not (300 <= width <= 6000 and 300 <= height <= 6000):
            raise ValueError(f"image sides must be 300-6000 px (got {width}x{height})")
        if not 0.4 <= width / height <= 2.5:
            raise ValueError(
                f"image width/height ratio must be 0.4-2.5 (got {width / height:.2f})"
            )
        return

    info = probe_media(path)
    if not info:
        return
    low, high = limits["duration"]
    duration = info.get("duration")
    if duration is not None and not low <= duration <= high:
        raise ValueError(
            f"{label} {asset_kind.lower()} must be {low}-{high} seconds (got {duration:.1f}s)"
        )
    if asset_kind == "Video":
        fps = info.get("fps")
        if fps is not None and not limits["fps"][0] <= fps <= limits["fps"][1]:
            raise ValueError(
                f"reference video must be {limits['fps'][0]}-{limits['fps'][1]} FPS "
                f"(got {fps:.0f})"
            )
        height = info.get("height")
        width = info.get("width")
        if height and width:
            # The upstream check is on pixels per frame, not on a named resolution:
            # a 406x720 portrait crop is "720p" but only 292k pixels, and is
            # rejected with [***.PixelCountTooSmall].
            low_px, high_px = limits["pixels"]
            pixels = width * height
            if not low_px <= pixels <= high_px:
                raise ValueError(
                    f"reference video must be {low_px:,}-{high_px:,} pixels per frame "
                    f"(got {width}x{height} = {pixels:,}); rescale it"
                )
            if not (300 <= width <= 6000 and 300 <= height <= 6000):
                raise ValueError(
                    f"reference video sides must be 300-6000 px (got {width}x{height})"
                )
            if not 0.4 <= width / height <= 2.5:
                raise ValueError(
                    f"reference video ratio must be 0.4-2.5 (got {width / height:.2f})"
                )


def create_asset(
    api_key: str,
    *,
    group_id: str,
    asset_kind: str,
    name: str,
    source: str,
    group_type: str | None = None,
    allow_multipart: bool = False,
    hosting_folder: str | None = None,
) -> str:
    """Register one asset from a URL, a data URI, or a local file.

    A local file is published to R2 first and ingested by URL: that is the path
    proven to produce `Active` assets. AnyFast's own multipart upload is behind
    `allow_multipart` because assets created that way stayed unresolvable.
    """
    import requests

    if asset_kind not in ASSET_MODELS:
        raise ValueError("asset_type must be Image, Video, or Audio")
    url = f"{get_base_url()}{ASSET_PATHS['create_asset']}"
    model = ASSET_MODELS[asset_kind]
    is_inline = is_remote_or_asset(source) or source.startswith("data:")

    if not is_inline:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"asset source file does not exist: {source}")
        validate_asset_file(path, asset_kind, group_type)
        if allow_multipart:
            # Kept for completeness, NOT the default: multipart uploads are accepted
            # and return an Id, but the resulting assets were never resolvable in
            # testing while the identical files ingested from a public URL went
            # Active within seconds.
            with path.open("rb") as handle:
                response = requests.post(
                    url,
                    headers=auth_headers(api_key),
                    data={
                        "model": model,
                        "GroupId": group_id,
                        "Name": name,
                        "AssetType": asset_kind,
                    },
                    files={"file": (path.name, handle)},
                    timeout=(CONNECT_TIMEOUT_SECONDS, TRANSFER_TIMEOUT_SECONDS),
                )
            raise_for_status(response)
            asset_id = (response.json() or {}).get("Id")
            if not asset_id:
                raise RuntimeError("AnyFast CreateAsset returned no Id")
            return str(asset_id)
        source = host_locally_for_ingestion(path, folder=hosting_folder)

    response = requests.post(
        url,
        headers=json_headers(api_key),
        json={
            "model": model,
            "GroupId": group_id,
            "Name": name,
            "AssetType": asset_kind,
            "URL": source,
        },
        timeout=(CONNECT_TIMEOUT_SECONDS, TRANSFER_TIMEOUT_SECONDS),
    )
    raise_for_status(response)
    asset_id = (response.json() or {}).get("Id")
    if not asset_id:
        raise RuntimeError("AnyFast CreateAsset returned no Id")
    return str(asset_id)


def host_locally_for_ingestion(path: Path, *, folder: str | None = None) -> str:
    """Publish a local file to R2 and return the public URL AnyFast will fetch."""
    from tools.storage import r2_client

    if not r2_client.is_configured():
        raise ValueError(
            f"{path.name} is a local file, and AnyFast ingests assets only from a URL "
            "it can download. Configure R2 (" + ", ".join(r2_client.missing_env()) + ") "
            "so OpenMontage can host it, host it yourself and pass the https:// URL, or "
            "pass allow_multipart=true to use AnyFast's multipart upload — which returned "
            "unusable assets in testing."
        )
    uploaded = r2_client.upload_file(path, folder=folder, unique=True, verify_public=True)
    return str(uploaded["url"])


def get_asset(api_key: str, asset_id: str) -> dict[str, Any]:
    return post_json(ASSET_PATHS["get_asset"], api_key, {"Id": asset_id})


def is_unresolvable(exc: Exception) -> bool:
    """True for the poll error raised when no read path resolves the asset."""
    return "cannot resolve asset" in str(exc)


def is_not_found(exc: Exception) -> bool:
    """True for the upstream NotFound shape, e.g. `[NotFound.asset_id]`."""
    return "NotFound." in str(exc) or "is not found" in str(exc)


def find_asset_in_group(
    api_key: str,
    asset_id: str,
    *,
    group_id: str,
    group_type: str | None = None,
) -> dict[str, Any] | None:
    """Locate one asset through ListAssets.

    GetAsset resolves AIGC assets only: an asset in a LivenessFace group answers
    `[NotFound.asset_id]` there, while ListAssets scoped to the group finds it.
    ListAssets is also the only read that returns the `Error` object carrying
    `FaceMismatch`, so it is the better status read for real-human assets.
    """
    filters: dict[str, Any] = {"GroupIds": [group_id]}
    if group_type:
        filters["GroupType"] = group_type
    page = 1
    while True:
        body = list_assets(api_key, filters=filters, page_number=page, page_size=50)
        items = body.get("Items") or []
        for item in items:
            if str(item.get("Id")) == asset_id:
                return item
        total = int(body.get("TotalCount") or 0)
        if page * 50 >= total or not items:
            return None
        page += 1


def read_asset(
    api_key: str,
    asset_id: str,
    *,
    group_id: str | None = None,
    group_type: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Read one asset, returning (asset, source). `asset` is None when unresolved."""
    try:
        return get_asset(api_key, asset_id), "GetAsset"
    except Exception as exc:
        if not is_not_found(exc) or not group_id:
            raise
    found = find_asset_in_group(api_key, asset_id, group_id=group_id, group_type=group_type)
    return found, "ListAssets"


def poll_asset(
    api_key: str,
    asset_id: str,
    *,
    interval: float = 3.0,
    timeout: float = 300.0,
    group_id: str | None = None,
    group_type: str | None = None,
    not_found_grace_seconds: float = 20.0,
) -> dict[str, Any]:
    """CreateAsset is asynchronous — wait for Active before referencing it.

    A persistent not-found is terminal, not something to retry for the whole
    timeout: hammering GetAsset for five minutes only fills the account log with
    404s. Brief 404s right after creation are tolerated (`not_found_grace_seconds`)
    because indexing can lag the create response.
    """
    started = time.monotonic()
    deadline = started + timeout
    while True:
        asset, source = read_asset(api_key, asset_id, group_id=group_id, group_type=group_type)
        if asset is None:
            if time.monotonic() - started >= not_found_grace_seconds:
                raise RuntimeError(
                    f"AnyFast returns [NotFound.asset_id] for {asset_id}, "
                    f"{not_found_grace_seconds:.0f}s after CreateAsset accepted it. "
                    "A create that returns an Id and then never resolves means the asset "
                    "did not survive preprocessing. In order of likelihood: (1) face "
                    "consistency verification failed — the account log shows "
                    "'502 upstream asset error: Face consistency verification failed' "
                    "just before the 404s; upload a clear, front-facing shot of the "
                    "verified person; (2) the Name duplicates another asset in the group "
                    "— AnyFast reports that as a 404 too, so use a unique Name; (3) the "
                    "reads are being made with a different token than the one that owns "
                    "the group."
                )
        else:
            if str(asset.get("Status", "")) in ASSET_TERMINAL_STATUSES:
                asset.setdefault("_read_via", source)
                return asset
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"AnyFast asset {asset_id} was still processing after {timeout:.0f}s"
            )
        time.sleep(interval)


def upload_asset(
    api_key: str,
    *,
    source: str,
    asset_kind: str,
    name: str,
    group_id: str | None = None,
    group_type: str | None = None,
    timeout: float = 300.0,
    poll_interval: float = 3.0,
    unique_name: bool = True,
    allow_multipart: bool = False,
    hosting_folder: str | None = None,
) -> dict[str, Any]:
    """Group -> CreateAsset -> poll to Active -> a usable `asset://` reference.

    A LivenessFace group is never created here: it only exists as the result of
    a completed real-person verification session.
    """
    if not source:
        raise ValueError("asset source is required")
    group_id = group_id or os.environ.get("ANYFAST_ASSET_GROUP_ID") or None
    created_group = False
    if not group_id:
        if group_type == "LivenessFace":
            raise ValueError(
                "a LivenessFace group must come from a completed verification session; "
                "run create_liveness_session -> get_liveness_result and pass its GroupId"
            )
        group_id = create_asset_group(api_key, f"openmontage-{asset_kind.lower()}", asset_kind)
        created_group = True
    else:
        # ListAssetGroups cannot see a LivenessFace group — not even filtered by its
        # exact Id — so a miss here proves nothing and must not block the upload.
        # CreateAsset is the real authority: it rejects a group the token does not
        # own with [NotFound.group_id] before anything is created or billed.
        group = describe_group(api_key, group_id)
        if group is not None:
            group_type = group_type or str(group.get("GroupType") or "") or None
        elif group_type is None:
            # Unlistable almost always means a real-human group; scope the reads
            # accordingly so polling does not fall back to an AIGC-only lookup.
            group_type = "LivenessFace"

    if unique_name:
        # A duplicate Name inside a group makes CreateAsset/GetAsset answer 404 as if
        # the group did not exist — a confusing failure that costs a paid create.
        name = f"{name}-{uuid.uuid4().hex[:6]}"
    asset_id = create_asset(
        api_key,
        group_id=group_id,
        asset_kind=asset_kind,
        name=name,
        source=source,
        group_type=group_type,
        allow_multipart=allow_multipart,
        hosting_folder=hosting_folder,
    )
    asset = poll_asset(
        api_key,
        asset_id,
        interval=poll_interval,
        timeout=timeout,
        group_id=group_id,
        group_type=group_type,
    )
    status = str(asset.get("Status", ""))
    if status != "Active":
        error = asset.get("Error") if isinstance(asset.get("Error"), dict) else {}
        code = str(error.get("Code") or "")
        detail = str(error.get("Message") or "")
        hint = ""
        if code == "FaceMismatch":
            hint = (
                " FaceMismatch means the upload is not the person verified for this "
                "group — upload a clear, front-facing asset of the same person."
            )
        raise RuntimeError(
            f"AnyFast asset {asset_id} finished as {status or 'unknown'}"
            + (f" ({code}: {detail})" if code or detail else "")
            + " and cannot be used."
            + hint
        )
    return {
        "asset_id": asset_id,
        "asset_ref": f"asset://{asset_id}",
        "group_id": group_id,
        "group_type": group_type,
        "group_created": created_group,
        "asset_type": asset_kind,
        "status": status,
        "status_verified": True,
        "name": asset.get("Name", name),
        "read_via": asset.get("_read_via", "GetAsset"),
    }


def describe_group(api_key: str, group_id: str) -> dict[str, Any] | None:
    """Return the group as ListAssetGroups sees it, or None.

    None is NOT proof the group is missing: a LivenessFace group never appears in
    ListAssetGroups, even filtered by its exact Id. Only GetVisualValidateResult
    reports it, so keep the BytedToken/GroupId from the verification run.
    """
    for group_type in (None, "LivenessFace"):
        filters: dict[str, Any] = {"Id": group_id}
        if group_type:
            filters["GroupType"] = group_type
        body = list_asset_groups(api_key, filters=filters, page_size=50)
        for item in body.get("Items") or []:
            if str(item.get("Id")) == group_id:
                return item
    return None


def list_asset_groups(
    api_key: str,
    *,
    filters: dict[str, Any] | None = None,
    page_number: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    return post_json(
        ASSET_PATHS["list_groups"],
        api_key,
        {
            "model": ASSET_MODELS["Image"],
            "Filter": filters or {},
            "PageNumber": page_number,
            "PageSize": page_size,
        },
    )


def list_assets(
    api_key: str,
    *,
    filters: dict[str, Any] | None = None,
    page_number: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    return post_json(
        ASSET_PATHS["list_assets"],
        api_key,
        {
            "model": ASSET_MODELS["Image"],
            "Filter": filters or {},
            "PageNumber": page_number,
            "PageSize": page_size,
        },
    )


def update_asset_group(api_key: str, group_id: str, name: str) -> dict[str, Any]:
    return post_json(
        ASSET_PATHS["update_group"],
        api_key,
        {"model": ASSET_MODELS["Image"], "Id": group_id, "Name": name},
    )


def update_asset(
    api_key: str,
    asset_id: str,
    *,
    name: str | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    if name is None and group_id is None:
        raise ValueError("update_asset requires a new name, a target group_id, or both")
    payload: dict[str, Any] = {"model": ASSET_MODELS["Image"], "Id": asset_id}
    if name is not None:
        payload["Name"] = name
    if group_id is not None:
        payload["GroupId"] = group_id
    return post_json(ASSET_PATHS["update_asset"], api_key, payload)


def delete_asset(api_key: str, asset_id: str) -> dict[str, Any]:
    return post_json(ASSET_PATHS["delete_asset"], api_key, {"Id": asset_id})


def delete_asset_group(api_key: str, group_id: str) -> dict[str, Any]:
    return post_json(ASSET_PATHS["delete_group"], api_key, {"Id": group_id})


# ---- real-human (LivenessFace) verification ----


def create_liveness_session(api_key: str, callback_url: str | None = None) -> dict[str, Any]:
    """Start real-person verification; returns H5Link + BytedToken.

    Requires a token created with the Byteplus-Direct group; an AIGC-only token
    cannot create verification sessions.

    CallbackURL is mandatory in practice. The published schema marks the whole
    body optional, but an empty body is rejected with
    `[***.CallbackURL] The required parameter CallbackURL is missing.`
    """
    if not callback_url:
        raise ValueError(
            "callback_url is required: AnyFast rejects CreateVisualValidateSession "
            "without CallbackURL even though the published schema marks it optional. "
            "Any reachable https:// URL works — the verification result is read by "
            "polling get_liveness_result with the BytedToken, not from the callback."
        )
    if not str(callback_url).startswith(("https://", "http://")):
        raise ValueError("callback_url must be an http(s) URL")
    return post_json(ASSET_PATHS["create_liveness_session"], api_key, {"CallbackURL": callback_url})


def get_liveness_result(api_key: str, byted_token: str) -> dict[str, Any]:
    if not byted_token:
        raise ValueError("byted_token is required")
    return post_json(ASSET_PATHS["get_liveness_result"], api_key, {"BytedToken": byted_token})
