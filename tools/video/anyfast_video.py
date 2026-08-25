"""AnyFast gateway adapter for the Seedance video generation family.

AnyFast (https://www.anyfast.ai) fronts several vendors behind one key and one
REST contract.  Video generation is asynchronous:

    POST /v1/video/generations          -> {"id": ..., "task_id": ..., "status": "queued"}
    GET  /v1/video/generations/{id}     -> {"code": ..., "data": {"status": "SUCCESS", "result_url": ...}}

The result URL is valid for 24 hours (100 downloads), so this tool downloads
the finished clip immediately.  Task records stay queryable for 7 days.

One creation endpoint serves five capabilities, selected by the `content[]`
items and their `role`, plus `omni_reference_task_type`:

    text_to_video      text only
    image_to_video     first_frame image (+ optional last_frame)
    reference_to_video reference_image / reference_video / reference_audio
    video_edit         instruction + source video   (omni_reference_task_type=edit)
    video_extend       instruction + source video   (omni_reference_task_type=extend)

Reference: https://docs.anyfast.ai/api-reference/model-api/bytedance/seedance-2-5
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

# Single source of truth for the default model, referenced by the input schema
# and every code path that reads `model`.
_DEFAULT_MODEL = "seedance-2.5"


class AnyFastVideo(BaseTool):
    """Generate video through the AnyFast gateway's Seedance model family."""

    name = "anyfast_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "anyfast"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    BASE_URL = "https://www.anyfast.ai"
    CREATE_PATH = "/v1/video/generations"

    # AnyFast model IDs exactly as the API reference spells them.  Note that
    # Seedance 2.0 Fast is published as `seedance-fast`, not `seedance-2.0-fast`.
    MODELS: dict[str, dict[str, Any]] = {
        "seedance-2.5": {
            "resolutions": ("480p", "720p", "1080p"),
            "max_duration": 30,
            "auto_duration": True,
            "max_images": 30,
            "max_videos": 10,
            "max_audios": 10,
            "max_reference_seconds": 30,
            "max_video_bytes": 200 * 1024 * 1024,
            "adaptive_only_operations": ("image_to_video", "video_edit", "video_extend"),
        },
        "seedance-2.0": {
            "resolutions": ("480p", "720p", "1080p", "4k"),
            "max_duration": 15,
            "auto_duration": False,
            "max_images": 9,
            "max_videos": 3,
            "max_audios": 3,
            "max_reference_seconds": 15,
            "max_video_bytes": 50 * 1024 * 1024,
            "adaptive_only_operations": (),
        },
        "seedance-fast": {
            "resolutions": ("480p", "720p"),
            "max_duration": 15,
            "auto_duration": False,
            "max_images": 9,
            "max_videos": 3,
            "max_audios": 3,
            "max_reference_seconds": 15,
            "max_video_bytes": 50 * 1024 * 1024,
            "adaptive_only_operations": (),
        },
        "seedance-2.0-mini": {
            "resolutions": ("480p", "720p"),
            "max_duration": 15,
            "auto_duration": False,
            "max_images": 9,
            "max_videos": 3,
            "max_audios": 3,
            "max_reference_seconds": 15,
            "max_video_bytes": 50 * 1024 * 1024,
            "adaptive_only_operations": (),
        },
        "seedance-2.0-ultra": {
            "resolutions": ("720p", "1080p", "2k"),
            "resolution_required": True,
            "max_duration": 15,
            "auto_duration": False,
            "max_images": 9,
            "max_videos": 3,
            "max_audios": 3,
            "max_reference_seconds": 15,
            "max_video_bytes": 50 * 1024 * 1024,
            "adaptive_only_operations": (),
        },
    }
    # `-nsfw` twins share the parent's envelope and require the Direct resource
    # group on the AnyFast console.
    NSFW_MODELS = {
        "seedance-2.5-nsfw": "seedance-2.5",
        "seedance-2.0-nsfw": "seedance-2.0",
        "seedance-2.0-fast-nsfw": "seedance-fast",
        "seedance-2.0-mini-nsfw": "seedance-2.0-mini",
    }
    # Short names an agent or director skill is likely to pass through.
    MODEL_ALIASES = {
        "2.5": "seedance-2.5",
        "2.0": "seedance-2.0",
        "standard": "seedance-2.0",
        "fast": "seedance-fast",
        "seedance-2.0-fast": "seedance-fast",
        "mini": "seedance-2.0-mini",
        "ultra": "seedance-2.0-ultra",
    }

    OPERATIONS = (
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "video_edit",
        "video_extend",
    )
    OMNI_TASK_TYPES = ("auto", "reference", "edit", "extend")
    RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
    OUTPUT_FORMATS = ("mp4", "mov")

    PENDING_STATUSES = frozenset({"NOT_START", "QUEUED", "IN_PROGRESS"})
    SUCCESS_STATUS = "SUCCESS"
    FAILURE_STATUS = "FAILURE"
    TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

    IMAGE_SUFFIX_TO_MIME = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }
    AUDIO_SUFFIX_TO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
    MAX_IMAGE_BYTES = 30 * 1024 * 1024
    MAX_AUDIO_BYTES = 15 * 1024 * 1024
    MAX_REQUEST_BYTES = 64 * 1024 * 1024

    dependencies = ["env:ANYFAST_API_KEY"]
    install_instructions = (
        "Set ANYFAST_API_KEY to the API key from https://www.anyfast.ai/console/token "
        "(key body only; the tool adds the 'Bearer ' scheme).\n"
        "  Optional: ANYFAST_BASE_URL, ANYFAST_VIDEO_MODEL.\n"
        "  AnyFast bills video per generation and does not publish a rate table in its docs, "
        "so set ANYFAST_VIDEO_PRICE_USD (flat per clip) or ANYFAST_VIDEO_PRICE_USD_PER_SECOND "
        "from the console pricing page to get real budget estimates."
    )
    agent_skills = ["seedance-2-5", "seedance-2-0", "ai-video-gen"]

    capabilities = [
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "video_edit",
        "video_extend",
        "task_create",
        "task_query",
    ]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "video_edit": True,
        "video_extend": True,
        "multiple_reference_images": True,
        "reference_image": True,
        "reference_video": True,
        "reference_audio": True,
        "first_last_frame": True,
        "native_audio": True,
        "local_image_data_uri": True,
        "local_audio_data_uri": True,
        "local_video": False,
        "return_last_frame": True,
        "web_search": True,
        "aspect_ratio": True,
        "seed": True,
    }
    best_for = [
        "one-key access to the whole Seedance family (2.5, 2.0, Fast, Mini, Ultra)",
        "long single takes — Seedance 2.5 runs 4-30 seconds with synchronized audio",
        "multimodal reference video: up to 30 images, 10 videos, and 10 audio clips",
        "video editing and video extension from an existing source clip",
        "an alternative Seedance route when Volcengine Ark or fal.ai is unavailable",
    ]
    not_good_for = [
        "offline generation",
        "unapproved paid generation",
        "direct local reference-video upload (URL or asset:// only)",
        "precise pre-flight budgeting without a configured price override",
    ]
    fallback_tools = ["seedance_ark", "seedance_video", "seedance_replicate", "kling_video"]

    input_schema = {
        "type": "object",
        "properties": {
            "task_action": {
                "type": "string",
                "enum": ["generate", "create", "query"],
                "default": "generate",
                "description": (
                    "generate = create then poll and download; create = submit only; "
                    "query = read an existing task."
                ),
            },
            "task_id": {"type": "string", "description": "Required for task_action=query."},
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": list(OPERATIONS),
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": sorted(set(MODELS) | set(NSFW_MODELS) | set(MODEL_ALIASES)),
                "default": _DEFAULT_MODEL,
                "description": (
                    "AnyFast model ID or short alias. Defaults to seedance-2.5; "
                    "overridden by nothing except an explicit value (ANYFAST_VIDEO_MODEL "
                    "sets the default when this is omitted)."
                ),
            },
            "omni_reference_task_type": {
                "type": "string",
                "enum": list(OMNI_TASK_TYPES),
                "description": (
                    "Reference workflow hint. Derived from `operation` when omitted."
                ),
            },
            "duration": {
                "description": (
                    "Integer seconds (4-30 on Seedance 2.5, 4-15 on the 2.0 family), "
                    "or -1/'auto' for automatic duration (2.5 only)."
                ),
                "default": 5,
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(RATIOS),
                "default": "16:9",
                "description": "Sent as `ratio`. Frame-guided, edit, and extend tasks are coerced to adaptive.",
            },
            "ratio": {"type": "string", "enum": list(RATIOS), "description": "Alias for aspect_ratio."},
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p", "2k", "4k"],
                "default": "720p",
                "description": "Allowed values vary by model; 4k is 2.0 only and 2k is Ultra only.",
            },
            "generate_audio": {"type": "boolean", "default": True},
            "output_format": {"type": "string", "enum": list(OUTPUT_FORMATS), "default": "mp4"},
            "seed": {"type": "integer", "description": "-1 (default) picks a random seed."},
            "watermark": {"type": "boolean", "default": False},
            "return_last_frame": {"type": "boolean", "default": False},
            "web_search": {
                "type": "boolean",
                "description": "Text-to-video only; sends tools=[{type: web_search}].",
            },
            "priority": {"type": "integer"},
            "service_tier": {"type": "string"},
            "safety_identifier": {"type": "string", "maxLength": 64},
            "execution_expires_after": {"type": "integer", "minimum": 60, "maximum": 172800},
            "reference_image_path": {"type": "string"},
            "reference_image_url": {"type": "string"},
            "reference_image_paths": {"type": "array", "items": {"type": "string"}},
            "reference_image_urls": {"type": "array", "items": {"type": "string"}},
            "end_image_path": {"type": "string", "description": "Last frame; alias last_image_path."},
            "end_image_url": {"type": "string", "description": "Last frame; alias last_image_url."},
            "reference_video_url": {"type": "string"},
            "reference_video_urls": {"type": "array", "items": {"type": "string"}},
            "reference_video_path": {
                "type": "string",
                "description": "Rejected: AnyFast accepts video only as a URL or asset:// ID.",
            },
            "reference_video_durations": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Optional durations for remote-video preflight validation.",
            },
            "reference_audio_url": {"type": "string"},
            "reference_audio_urls": {"type": "array", "items": {"type": "string"}},
            "reference_audio_path": {"type": "string"},
            "reference_audio_paths": {"type": "array", "items": {"type": "string"}},
            "price_usd": {
                "type": "number",
                "minimum": 0,
                "description": "Flat per-generation price from the AnyFast console, for budget estimates.",
            },
            "price_usd_per_second": {
                "type": "number",
                "minimum": 0,
                "description": "Per-second price from the AnyFast console, for budget estimates.",
            },
            "poll_interval_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 5},
            "timeout_seconds": {"type": "number", "minimum": 1, "default": 1200},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=2048, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=2.0,
        retryable_errors=["rate_limit", "timeout", "server_error"],
    )
    idempotency_key_fields = [
        "prompt",
        "operation",
        "model",
        "duration",
        "aspect_ratio",
        "resolution",
        "generate_audio",
        "seed",
        "reference_image_url",
        "reference_image_path",
        "reference_image_urls",
        "reference_image_paths",
        "reference_video_urls",
        "reference_audio_urls",
    ]
    side_effects = [
        "submits a paid task to the AnyFast API",
        "writes the completed video to output_path",
    ]
    user_visible_verification = [
        "Watch the downloaded clip for motion coherence and synchronized audio",
        "Confirm the local artifact before the 24-hour result URL expires",
    ]

    # ---- configuration ----

    def _get_api_key(self) -> str | None:
        return os.environ.get("ANYFAST_API_KEY")

    def _get_base_url(self) -> str:
        base_url = os.environ.get("ANYFAST_BASE_URL", self.BASE_URL).rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("ANYFAST_BASE_URL must be an https:// URL")
        return base_url

    def get_status(self) -> ToolStatus:
        api_key = self._get_api_key()
        if not api_key or api_key.lower().startswith("bearer "):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info["model_catalog"] = {
            model: {
                "resolutions": list(spec["resolutions"]),
                "max_duration_seconds": spec["max_duration"],
                "automatic_duration": spec["auto_duration"],
                "max_reference_images": spec["max_images"],
                "max_reference_videos": spec["max_videos"],
                "max_reference_audio_clips": spec["max_audios"],
            }
            for model, spec in self.MODELS.items()
        }
        info["model_aliases"] = dict(self.MODEL_ALIASES)
        return info

    def is_operation_available(self, operation: str) -> bool:
        return operation in self.OPERATIONS

    # ---- cost & runtime ----

    def _price_overrides(self, inputs: dict[str, Any]) -> tuple[float | None, float | None]:
        flat = self._read_price(inputs.get("price_usd"), "price_usd")
        if flat is None:
            flat = self._read_price(os.environ.get("ANYFAST_VIDEO_PRICE_USD"), "ANYFAST_VIDEO_PRICE_USD")
        per_second = self._read_price(inputs.get("price_usd_per_second"), "price_usd_per_second")
        if per_second is None:
            per_second = self._read_price(
                os.environ.get("ANYFAST_VIDEO_PRICE_USD_PER_SECOND"),
                "ANYFAST_VIDEO_PRICE_USD_PER_SECOND",
            )
        return flat, per_second

    @staticmethod
    def _read_price(raw: Any, label: str) -> float | None:
        if raw is None or raw == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a finite number >= 0") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label} must be a finite number >= 0")
        return value

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Estimate USD from a configured price only.

        AnyFast bills video per generation and publishes no rate table in its
        API docs, so a fabricated default would be worse than none. Without an
        override this reports 0.0 and every caller-facing payload carries
        ``cost_estimate_status = "unknown_gateway_pricing"``.
        """
        flat, per_second = self._price_overrides(inputs)
        if flat is not None:
            return round(flat, 4)
        if per_second is None:
            return 0.0
        return round(per_second * self._billable_seconds(inputs), 4)

    def _billable_seconds(self, inputs: dict[str, Any]) -> int:
        """Seconds to price/time against, tolerant of an unvalidated duration.

        Estimation runs during ranking on arbitrary context dicts, so a bad
        value falls back to the model's maximum rather than raising — the
        conservative direction for a budget check.
        """
        try:
            _, spec = self._resolve_model(inputs)
        except ValueError:
            spec = self.MODELS[_DEFAULT_MODEL]
        try:
            duration = self._normalize_duration(self._duration_input(inputs), spec)
        except (TypeError, ValueError):
            return int(spec["max_duration"])
        return int(spec["max_duration"] if duration == -1 else duration)

    @staticmethod
    def _duration_input(inputs: dict[str, Any]) -> Any:
        """`duration` with an explicit None normalized back to the default."""
        value = inputs.get("duration", 5)
        return 5 if value is None else value

    def cost_estimate_status(self, inputs: dict[str, Any]) -> str:
        flat, per_second = self._price_overrides(inputs)
        if flat is not None:
            return "configured_flat_price"
        if per_second is not None:
            return "configured_per_second_price"
        return "unknown_gateway_pricing"

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        try:
            model, _ = self._resolve_model(inputs)
        except ValueError:
            return 180.0
        base = 60.0 if model in ("seedance-fast", "seedance-2.0-mini") else 120.0
        return round(base + 6.0 * self._billable_seconds(inputs), 1)

    # ---- preflight ----

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validate and estimate locally; never send the paid POST."""
        action = str(inputs.get("task_action", "generate"))
        result: dict[str, Any] = {
            "tool": self.name,
            "task_action": action,
            "status": self.get_status().value,
            "would_execute": False,
            "paid_submission": False,
        }
        try:
            base_url = self._get_base_url()
            api_key = self._get_api_key()
            if api_key and api_key.lower().startswith("bearer "):
                raise ValueError(
                    "ANYFAST_API_KEY must contain only the key body; remove the 'Bearer ' prefix"
                )
            result["api_contract"] = {
                "create": f"POST {base_url}{self.CREATE_PATH}",
                "query": f"GET {base_url}{self.CREATE_PATH}/{{task_id}}",
            }
            if action == "query":
                self._validate_task_id(inputs.get("task_id"))
            else:
                payload, warnings = self._build_payload(inputs)
                result.update(
                    {
                        "model": payload["model"],
                        "operation": inputs.get("operation", "text_to_video"),
                        "resolution": payload.get("resolution"),
                        "ratio": payload.get("ratio"),
                        "duration": payload.get("duration"),
                        "generate_audio": payload.get("generate_audio"),
                        "output_format": payload.get("output_format"),
                        "omni_reference_task_type": payload.get("omni_reference_task_type"),
                        "media_counts": self._media_counts(payload["content"]),
                        "warnings": warnings,
                        "estimated_cost_usd": self.estimate_cost(inputs),
                        "estimated_runtime_seconds": self.estimate_runtime(inputs),
                        "cost_estimate_status": self.cost_estimate_status(inputs),
                        "cost_estimate_note": (
                            "AnyFast bills video per generation and does not publish rates in "
                            "its API docs. Set ANYFAST_VIDEO_PRICE_USD or "
                            "ANYFAST_VIDEO_PRICE_USD_PER_SECOND (or pass price_usd / "
                            "price_usd_per_second) from the console pricing page; unknown "
                            "pricing is reported as unknown, never as free."
                        ),
                    }
                )
            result["valid"] = True
        except (TypeError, ValueError, OSError) as exc:
            result["valid"] = False
            result["error"] = self._safe_error(exc)
        return result

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        action = str(inputs.get("task_action", "generate"))
        task_id: str | None = None
        payload: dict[str, Any] = {}
        warnings: list[str] = []
        estimated_cost_usd = 0.0

        try:
            if action not in {"generate", "create", "query"}:
                raise ValueError("task_action must be generate, create, or query")
            if action == "query":
                self._validate_task_id(inputs.get("task_id"))
            else:
                payload, warnings = self._build_payload(inputs)
                # Finish every local parse before the paid POST so a malformed
                # price override can never create an untracked task.
                estimated_cost_usd = self.estimate_cost(inputs)
        except (TypeError, ValueError, OSError) as exc:
            return ToolResult(success=False, error=self._safe_error(exc))

        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="ANYFAST_API_KEY not set. " + self.install_instructions,
            )
        if api_key.lower().startswith("bearer "):
            return ToolResult(
                success=False,
                error=(
                    "ANYFAST_API_KEY must contain only the key body; remove the "
                    "'Bearer ' prefix"
                ),
            )

        try:
            if action == "query":
                task = self._query_task(str(inputs["task_id"]), api_key)
                return ToolResult(
                    success=True,
                    data={
                        "provider": self.provider,
                        "task": task,
                        "task_id": task.get("task_id", inputs["task_id"]),
                        "status": task.get("status"),
                        "progress": task.get("progress"),
                        "video_url": self._result_url(task),
                        "cost_estimate_status": self.cost_estimate_status(inputs),
                    },
                )

            task_id = self._create_task(payload, api_key)
            model = str(payload["model"])
            if action == "create":
                return ToolResult(
                    success=True,
                    data={
                        "provider": self.provider,
                        "task_id": task_id,
                        "status": "submitted",
                        "model": model,
                        "warnings": warnings,
                        "cost_estimate_status": self.cost_estimate_status(inputs),
                    },
                    cost_usd=estimated_cost_usd,
                    model=model,
                )

            task = self._poll_task(task_id, api_key, inputs)
            status = str(task.get("status", "")).upper()
            if status != self.SUCCESS_STATUS:
                detail = str(task.get("fail_reason") or "")
                safe_detail = self._safe_error(RuntimeError(detail), api_key) if detail else ""
                return ToolResult(
                    success=False,
                    data={"task_id": task_id, "status": status or "unknown"},
                    error=(
                        f"AnyFast video task {status.lower() or 'failed'}"
                        + (f": {safe_detail}" if safe_detail else "")
                    ),
                    duration_seconds=round(time.time() - started, 2),
                    model=model,
                )

            video_url = self._result_url(task)
            if not video_url:
                raise RuntimeError("AnyFast task succeeded without a result_url")

            upstream = task.get("data") if isinstance(task.get("data"), dict) else {}
            output_format = str(payload.get("output_format", "mp4"))
            output_path = Path(
                inputs.get("output_path") or f"anyfast_video_output.{output_format}"
            )
            self._download_video(str(video_url), output_path)

            from tools.video._shared import probe_output

            probed = probe_output(output_path)
            return ToolResult(
                success=True,
                data={
                    "provider": self.provider,
                    "task_id": task_id,
                    "status": status,
                    "model": model,
                    "upstream_model": upstream.get("model"),
                    "prompt": inputs.get("prompt"),
                    "operation": inputs.get("operation", "text_to_video"),
                    "video_url": video_url,
                    "original_result_url": task.get("original_result_url"),
                    "last_frame_url": (upstream.get("content") or {}).get("last_frame_url"),
                    "output": str(output_path),
                    "output_path": str(output_path),
                    "format": upstream.get("output_format", output_format),
                    "resolution": upstream.get("super_resolution")
                    or upstream.get("resolution")
                    or payload.get("resolution"),
                    "aspect_ratio": upstream.get("ratio", payload.get("ratio")),
                    "duration": upstream.get("duration", payload.get("duration")),
                    "generate_audio": upstream.get("generate_audio", payload.get("generate_audio")),
                    "seed": upstream.get("seed"),
                    "usage": upstream.get("usage") or {},
                    "request_id": task.get("request_id"),
                    "warnings": warnings,
                    "cost_estimate_status": self.cost_estimate_status(inputs),
                    **probed,
                },
                artifacts=[str(output_path)],
                cost_usd=estimated_cost_usd,
                duration_seconds=round(time.time() - started, 2),
                seed=upstream.get("seed") if isinstance(upstream.get("seed"), int) else None,
                model=model,
            )
        except Exception as exc:
            error_data: dict[str, Any] = {}
            if task_id:
                # The task may still be running and billable — hand the caller
                # a recovery path instead of losing the id.
                error_data = {
                    "task_id": task_id,
                    "status": "submitted_result_unknown",
                    "recovery_action": "query",
                }
            elif action == "query" and inputs.get("task_id"):
                error_data = {"task_id": str(inputs["task_id"])}
            return ToolResult(
                success=False,
                data=error_data,
                error=f"AnyFast video request failed: {self._safe_error(exc, api_key)}",
                duration_seconds=round(time.time() - started, 2),
            )

    # ---- payload construction ----

    def _build_payload(self, inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        operation = str(inputs.get("operation", "text_to_video"))
        if operation not in self.OPERATIONS:
            raise ValueError("operation must be one of " + ", ".join(self.OPERATIONS))

        model, spec = self._resolve_model(inputs)
        warnings: list[str] = []
        prompt = str(inputs.get("prompt") or "").strip()

        resolution = inputs.get("resolution")
        if resolution is None:
            if spec.get("resolution_required"):
                raise ValueError(f"resolution is required for {model}")
            resolution = "720p"
        resolution = str(resolution).lower()
        if resolution not in spec["resolutions"]:
            raise ValueError(
                f"{model} supports resolution "
                + ", ".join(spec["resolutions"])
                + f" (got {resolution})"
            )

        ratio = str(inputs.get("ratio") or inputs.get("aspect_ratio") or "16:9")
        if ratio not in self.RATIOS:
            raise ValueError("aspect_ratio must be one of " + ", ".join(self.RATIOS))
        if operation in spec["adaptive_only_operations"] and ratio != "adaptive":
            warnings.append(
                f"{model} supports only ratio=adaptive for {operation}; "
                f"coerced {ratio} to adaptive"
            )
            ratio = "adaptive"

        duration = self._normalize_duration(self._duration_input(inputs), spec)
        if operation == "video_edit" and spec["auto_duration"] and duration != -1:
            warnings.append(
                f"{model} supports only duration=-1 for video_edit (the output follows the "
                f"source); coerced {duration} to -1"
            )
            duration = -1

        output_format = str(inputs.get("output_format", "mp4")).lower()
        if output_format not in self.OUTPUT_FORMATS:
            raise ValueError("output_format must be mp4 or mov")

        content = self._build_content(inputs, operation, spec, prompt)

        payload: dict[str, Any] = {
            "model": model,
            "content": content,
            "generate_audio": bool(inputs.get("generate_audio", True)),
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "output_format": output_format,
            "watermark": bool(inputs.get("watermark", False)),
            "return_last_frame": bool(inputs.get("return_last_frame", False)),
        }

        omni = inputs.get("omni_reference_task_type") or self._default_omni_type(operation)
        if omni is not None:
            if omni not in self.OMNI_TASK_TYPES:
                raise ValueError(
                    "omni_reference_task_type must be one of " + ", ".join(self.OMNI_TASK_TYPES)
                )
            payload["omni_reference_task_type"] = omni

        if inputs.get("web_search"):
            if operation != "text_to_video" or len(content) != 1:
                raise ValueError("web_search is supported only for pure text-to-video requests")
            payload["tools"] = [{"type": "web_search"}]

        for key in ("seed", "priority", "execution_expires_after", "safety_identifier", "service_tier"):
            if inputs.get(key) is not None:
                payload[key] = inputs[key]
        self._validate_optional_parameters(payload)
        self._validate_request_size(payload)
        return payload, warnings

    def _build_content(
        self,
        inputs: dict[str, Any],
        operation: str,
        spec: dict[str, Any],
        prompt: str,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"type": "text", "text": prompt})

        if inputs.get("reference_video_path") or inputs.get("video_path"):
            raise ValueError(
                "AnyFast accepts video only as a public URL or asset:// ID; upload the "
                "local file first (Base64 video is not supported)"
            )

        if operation == "text_to_video":
            if not prompt:
                raise ValueError("prompt is required for text_to_video")
            if self._has_any_media(inputs):
                raise ValueError(
                    "text_to_video does not accept reference media; use image_to_video, "
                    "reference_to_video, video_edit, or video_extend"
                )
            return content

        if operation == "image_to_video":
            first = self._first_frame_refs(inputs)
            if len(first) != 1:
                raise ValueError("image_to_video requires exactly one first-frame image")
            content.append(self._image_content(first[0], role="first_frame"))
            last = self._last_frame_refs(inputs)
            if len(last) > 1:
                raise ValueError("provide only one last-frame image")
            if last:
                content.append(self._image_content(last[0], role="last_frame"))
            return content

        videos = self._video_refs(inputs)
        if operation in ("video_edit", "video_extend"):
            if not prompt:
                raise ValueError(f"{operation} requires a prompt describing the instruction")
            if not videos:
                raise ValueError(f"{operation} requires a source video URL or asset:// ID")
            if operation == "video_edit" and len(videos) != 1:
                raise ValueError("video_edit accepts exactly one source video")
        images = self._image_refs(inputs)
        audios = self._audio_refs(inputs)

        if len(images) > spec["max_images"]:
            raise ValueError(f"at most {spec['max_images']} reference images are supported")
        if len(videos) > spec["max_videos"]:
            raise ValueError(f"at most {spec['max_videos']} reference videos are supported")
        if len(audios) > spec["max_audios"]:
            raise ValueError(f"at most {spec['max_audios']} reference audio clips are supported")
        if operation == "reference_to_video" and not (images or videos):
            raise ValueError("reference_to_video requires at least one reference image or video")
        if audios and not (images or videos):
            raise ValueError("reference audio requires at least one reference image or video")

        self._validate_remote_refs(videos, "reference video")
        self._validate_reference_durations(
            inputs.get("reference_video_durations"), videos, spec, "reference video"
        )

        content.extend(self._image_content(ref, role="reference_image") for ref in images)
        content.extend(
            {"type": "video_url", "video_url": {"url": str(ref)}, "role": "reference_video"}
            for ref in videos
        )
        content.extend(self._audio_content(ref, role="reference_audio", spec=spec) for ref in audios)
        return content

    @staticmethod
    def _default_omni_type(operation: str) -> str | None:
        return {
            "reference_to_video": "reference",
            "video_edit": "edit",
            "video_extend": "extend",
        }.get(operation)

    def _resolve_model(self, inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw = str(
            inputs.get("model")
            or inputs.get("model_variant")
            or os.environ.get("ANYFAST_VIDEO_MODEL")
            or _DEFAULT_MODEL
        ).strip()
        model = self.MODEL_ALIASES.get(raw.lower(), raw)
        if model in self.NSFW_MODELS:
            return model, self.MODELS[self.NSFW_MODELS[model]]
        if model not in self.MODELS:
            raise ValueError(
                f"unsupported AnyFast video model {raw!r}; use one of "
                + ", ".join(sorted(set(self.MODELS) | set(self.NSFW_MODELS)))
            )
        return model, self.MODELS[model]

    @staticmethod
    def _normalize_duration(value: Any, spec: dict[str, Any]) -> int:
        max_seconds = spec["max_duration"]
        allow_auto = spec["auto_duration"]
        hint = (
            f"duration must be an integer from 4 to {max_seconds}"
            + (" or -1" if allow_auto else "")
        )
        if value == "auto":
            if not allow_auto:
                raise ValueError(hint)
            return -1
        if isinstance(value, bool):
            raise ValueError(hint)
        try:
            duration = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(hint) from exc
        if str(value).strip() not in {str(duration), "auto"}:
            raise ValueError(hint)
        if duration == -1:
            if not allow_auto:
                raise ValueError(hint)
            return -1
        if not 4 <= duration <= max_seconds:
            raise ValueError(hint)
        return duration

    # ---- reference collection ----

    @staticmethod
    def _collect(inputs: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
        refs: list[Any] = []
        for key in keys:
            value = inputs.get(key)
            if not value:
                continue
            if isinstance(value, (list, tuple)):
                refs.extend(item for item in value if item)
            else:
                refs.append(value)
        return refs

    def _first_frame_refs(self, inputs: dict[str, Any]) -> list[Any]:
        return self._collect(
            inputs,
            ("reference_image_url", "reference_image_path", "image_url", "image_path"),
        )

    def _last_frame_refs(self, inputs: dict[str, Any]) -> list[Any]:
        return self._collect(
            inputs,
            ("end_image_url", "end_image_path", "last_image_url", "last_image_path"),
        )

    def _image_refs(self, inputs: dict[str, Any]) -> list[Any]:
        return self._collect(
            inputs,
            (
                "reference_image_urls",
                "reference_image_paths",
                "reference_image_url",
                "reference_image_path",
                "image_url",
                "image_path",
            ),
        )

    def _video_refs(self, inputs: dict[str, Any]) -> list[Any]:
        return self._collect(
            inputs,
            ("reference_video_urls", "reference_video_url", "video_url"),
        )

    def _audio_refs(self, inputs: dict[str, Any]) -> list[Any]:
        return self._collect(
            inputs,
            (
                "reference_audio_urls",
                "reference_audio_paths",
                "reference_audio_url",
                "reference_audio_path",
            ),
        )

    @staticmethod
    def _has_any_media(inputs: dict[str, Any]) -> bool:
        keys = (
            "reference_image_url",
            "reference_image_path",
            "reference_image_urls",
            "reference_image_paths",
            "reference_video_url",
            "reference_video_urls",
            "reference_video_path",
            "reference_audio_url",
            "reference_audio_urls",
            "reference_audio_path",
            "reference_audio_paths",
            "image_url",
            "image_path",
            "video_url",
            "video_path",
            "end_image_url",
            "end_image_path",
            "last_image_url",
            "last_image_path",
        )
        return any(inputs.get(key) for key in keys)

    def _validate_reference_durations(
        self,
        durations: Any,
        refs: list[Any],
        spec: dict[str, Any],
        label: str,
    ) -> None:
        if not durations:
            return
        values = [float(value) for value in durations]
        if len(values) != len(refs):
            raise ValueError(f"{label} durations must match the number of {label}s")
        limit = spec["max_reference_seconds"]
        if any(value < 2 or value > limit for value in values):
            raise ValueError(f"each {label} must be 2 to {limit} seconds")
        if sum(values) > limit:
            raise ValueError(f"all {label}s together must be at most {limit} seconds")

    # ---- media encoding ----

    def _image_content(self, ref: Any, *, role: str) -> dict[str, Any]:
        url = self._media_url(
            ref,
            suffix_to_mime=self.IMAGE_SUFFIX_TO_MIME,
            max_bytes=self.MAX_IMAGE_BYTES,
            label="image",
        )
        return {"type": "image_url", "image_url": {"url": url}, "role": role}

    def _audio_content(self, ref: Any, *, role: str, spec: dict[str, Any]) -> dict[str, Any]:
        url = self._media_url(
            ref,
            suffix_to_mime=self.AUDIO_SUFFIX_TO_MIME,
            max_bytes=self.MAX_AUDIO_BYTES,
            label="audio",
            max_seconds=spec["max_reference_seconds"],
        )
        return {"type": "audio_url", "audio_url": {"url": url}, "role": role}

    def _media_url(
        self,
        ref: Any,
        *,
        suffix_to_mime: dict[str, str],
        max_bytes: int,
        label: str,
        max_seconds: int = 30,
    ) -> str:
        value = str(ref)
        if value.startswith("data:"):
            return self._validate_data_uri(
                value,
                suffix_to_mime=suffix_to_mime,
                max_bytes=max_bytes,
                label=label,
                max_seconds=max_seconds,
            )
        if self._is_remote_or_asset(value):
            return value
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError(
                f"{label} reference must be a public URL, asset:// ID, or existing "
                f"local file: {value}"
            )
        if path.stat().st_size >= max_bytes:
            raise ValueError(
                f"local {label} must be smaller than {max_bytes // (1024 * 1024)} MB"
            )
        suffix = path.suffix.lower()
        mime = suffix_to_mime.get(suffix)
        if not mime:
            guessed, _ = mimetypes.guess_type(path.name)
            mime = guessed if guessed in suffix_to_mime.values() else None
        if not mime:
            raise ValueError(
                f"unsupported local {label} format; expected one of "
                + ", ".join(sorted(suffix_to_mime))
            )
        if label == "image":
            self._validate_image_bytes(path.read_bytes(), str(path))
        else:
            self._probe_audio_duration(path, max_seconds=max_seconds)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _validate_data_uri(
        self,
        value: str,
        *,
        suffix_to_mime: dict[str, str],
        max_bytes: int,
        label: str,
        max_seconds: int,
    ) -> str:
        match = re.fullmatch(
            r"data:([a-z]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", value, flags=re.IGNORECASE
        )
        if not match:
            raise ValueError(
                f"{label} Data URI must use a supported MIME type and strict base64 encoding"
            )
        mime = match.group(1).lower()
        if mime not in set(suffix_to_mime.values()):
            raise ValueError(f"unsupported {label} Data URI MIME type: {mime}")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"invalid base64 in {label} Data URI") from exc
        if len(decoded) >= max_bytes:
            raise ValueError(
                f"decoded {label} Data URI must be smaller than {max_bytes // (1024 * 1024)} MB"
            )
        if label == "image":
            self._validate_image_bytes(decoded, "Data URI")
        else:
            suffix = ".wav" if mime == "audio/wav" else ".mp3"
            # ffprobe cannot reopen a NamedTemporaryFile on Windows while Python
            # holds the handle, so close it first and unlink on every path.
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                temp.write(decoded)
                temp.flush()
                temp_path = Path(temp.name)
            try:
                self._probe_audio_duration(temp_path, max_seconds=max_seconds)
            finally:
                temp_path.unlink(missing_ok=True)
        return value

    @staticmethod
    def _validate_image_bytes(data: bytes, source: str) -> None:
        """Enforce AnyFast's documented image envelope when Pillow is available."""
        try:
            from PIL import Image
        except ImportError:
            return
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                image.verify()
        except Exception as exc:
            raise ValueError(f"image is unreadable or corrupt: {source}") from exc
        if not (300 <= width <= 6000 and 300 <= height <= 6000):
            raise ValueError("image width and height must each be 300 to 6000 pixels")
        if not 0.4 <= width / height <= 2.5:
            raise ValueError("image width/height ratio must be between 0.4 and 2.5")

    @staticmethod
    def _probe_audio_duration(path: Path, *, max_seconds: int) -> float | None:
        """Check the 2-N second audio window. Skipped when ffprobe is absent."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            duration = float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError(f"failed to probe reference audio: {path}") from exc
        if not 2 <= duration <= max_seconds:
            raise ValueError(
                f"each reference audio clip must be 2 to {max_seconds} seconds "
                f"(got {duration:.2f})"
            )
        return duration

    @staticmethod
    def _is_remote_or_asset(value: str) -> bool:
        return value.startswith(("https://", "http://", "asset://"))

    def _validate_remote_refs(self, refs: list[Any], label: str) -> None:
        for ref in refs:
            if not self._is_remote_or_asset(str(ref)):
                raise ValueError(
                    f"{label} must be a public URL or asset:// ID; AnyFast does not "
                    "accept Base64 video or local paths"
                )

    def _validate_optional_parameters(self, payload: dict[str, Any]) -> None:
        seed = payload.get("seed")
        if seed is not None and (isinstance(seed, bool) or int(seed) < -1):
            raise ValueError("seed must be -1 or a non-negative integer")
        expires = payload.get("execution_expires_after")
        if expires is not None and not 60 <= int(expires) <= 172800:
            raise ValueError("execution_expires_after must be between 60 and 172800 seconds")
        priority = payload.get("priority")
        if priority is not None and isinstance(priority, bool):
            raise ValueError("priority must be an integer")
        safety = payload.get("safety_identifier")
        if safety is not None and len(str(safety)) > 64:
            raise ValueError("safety_identifier must be at most 64 characters")

    def _validate_request_size(self, payload: dict[str, Any]) -> None:
        # Base64 dominates request size; summing the encoded media is a cheap
        # conservative check that avoids serializing the whole body twice.
        encoded_bytes = 0
        for item in payload["content"]:
            media = item.get("image_url") or item.get("audio_url") or item.get("video_url") or {}
            url = str(media.get("url", ""))
            if url.startswith("data:"):
                encoded_bytes += len(url.encode("ascii"))
        if encoded_bytes >= self.MAX_REQUEST_BYTES:
            raise ValueError("AnyFast request body must be smaller than 64 MB")

    @staticmethod
    def _media_counts(content: list[dict[str, Any]]) -> dict[str, int]:
        return {
            kind: sum(1 for item in content if item.get("type") == kind)
            for kind in ("text", "image_url", "video_url", "audio_url")
        }

    # ---- HTTP ----

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _create_task(self, payload: dict[str, Any], api_key: str) -> str:
        import requests

        response = requests.post(
            f"{self._get_base_url()}{self.CREATE_PATH}",
            headers=self._headers(api_key),
            json=payload,
            timeout=60,
        )
        self._raise_for_status(response)
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("AnyFast create returned a non-object response")
        task_id = data.get("id") or data.get("task_id")
        self._validate_task_id(task_id)
        return str(task_id)

    def _query_task(self, task_id: str, api_key: str) -> dict[str, Any]:
        import requests

        url = f"{self._get_base_url()}{self.CREATE_PATH}/{task_id}"
        response = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = requests.get(url, headers=self._headers(api_key), timeout=30)
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < self.retry_policy.max_retries:
                    time.sleep(self.retry_policy.backoff_seconds * (2**attempt))
                    continue
                self._raise_for_status(response)
                break
            except requests.RequestException:
                if attempt >= self.retry_policy.max_retries:
                    raise
                time.sleep(self.retry_policy.backoff_seconds * (2**attempt))
        if response is None:
            raise RuntimeError("AnyFast query returned no response")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("AnyFast query returned a non-object response")
        # The query endpoint wraps the task in {code, message, data}; tolerate a
        # bare task object in case the gateway ever returns one.
        task = body.get("data") if isinstance(body.get("data"), dict) else body
        if body.get("code") not in (None, "success", "Success", 0, "0") and not task.get("status"):
            raise RuntimeError(
                f"AnyFast query failed: {body.get('code')}: {body.get('message', '')}".strip()
            )
        return task

    def _poll_task(
        self, task_id: str, api_key: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        interval = float(inputs.get("poll_interval_seconds", 5))
        timeout = float(inputs.get("timeout_seconds", 1200))
        if not 0 <= interval <= 60:
            raise ValueError("poll_interval_seconds must be between 0 and 60")
        if timeout <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        deadline = time.monotonic() + timeout
        while True:
            task = self._query_task(task_id, api_key)
            status = str(task.get("status", "")).upper()
            if status in (self.SUCCESS_STATUS, self.FAILURE_STATUS):
                return task
            if status not in self.PENDING_STATUSES:
                raise RuntimeError(
                    f"AnyFast returned unknown task status: {status or '<empty>'}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"AnyFast task {task_id} did not finish within {timeout}s"
                )
            time.sleep(interval)

    @staticmethod
    def _result_url(task: dict[str, Any]) -> str | None:
        """Read the finished clip URL, preferring the top-level pre-signed one."""
        url = task.get("result_url")
        if url:
            return str(url)
        upstream = task.get("data") if isinstance(task.get("data"), dict) else {}
        content = upstream.get("content") if isinstance(upstream.get("content"), dict) else {}
        video_url = content.get("video_url")
        return str(video_url) if video_url else None

    @staticmethod
    def _download_video(video_url: str, output_path: Path) -> None:
        import requests

        response = requests.get(video_url, timeout=300)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = output_path.with_name(output_path.name + ".part")
        partial.write_bytes(response.content)
        partial.replace(output_path)

    @staticmethod
    def _validate_task_id(task_id: Any) -> None:
        value = str(task_id or "")
        if not AnyFastVideo.TASK_ID_PATTERN.fullmatch(value):
            raise ValueError("task_id is missing or invalid")

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        try:
            response.raise_for_status()
        except Exception as exc:
            detail = ""
            try:
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    detail = ": ".join(
                        str(error.get(key))
                        for key in ("type", "code", "message")
                        if error.get(key)
                    )
            except Exception:
                pass
            raise RuntimeError(f"{exc}" + (f"; {detail}" if detail else "")) from exc

    def _safe_error(self, exc: Exception, api_key: str | None = None) -> str:
        """Strip credentials, signed URLs, and Base64 blobs out of error text."""
        message = str(exc)
        for secret in {value for value in (api_key, self._get_api_key()) if value}:
            message = message.replace(secret, "[redacted]")
        message = re.sub(
            r"data:(?:image|audio)/[^;\s]+;base64,[A-Za-z0-9+/=]+",
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
