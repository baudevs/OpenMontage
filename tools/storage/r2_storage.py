"""Host local media on Cloudflare R2 so providers can fetch it by URL.

Some providers will not take a local file or Base64 at all — AnyFast's asset
library only ingests a public URL or an `asset://` ID, and its multipart upload
path produces assets that never become usable. Publishing to R2 and handing the
provider a public URL is the reliable route.

Enable it by putting the five `R2_*` values in `.env`; the tool reports
UNAVAILABLE until all of them are present.

Anything uploaded here is **world-readable by URL** — that is the point, since
the provider downloads it anonymously. Do not publish material that must stay
private, and delete what you no longer need.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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
from tools.storage import r2_client


class R2Storage(BaseTool):
    """Upload, verify, and remove public media objects on Cloudflare R2."""

    name = "r2_storage"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "media_hosting"
    provider = "cloudflare_r2"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    OPERATIONS = ("upload", "upload_many", "delete", "exists", "url", "list", "sweep")

    dependencies = [f"env:{name}" for name in r2_client.REQUIRED_ENV]
    install_instructions = (
        "Create an R2 bucket in the Cloudflare dashboard, enable its Public Development "
        "URL (bucket > Settings > Public Development URL), then mint an R2 API token "
        "with Object Read & Write.\n"
        "  Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, and "
        "R2_PUBLIC_BASE_URL (https://pub-<hash>.r2.dev) in .env.\n"
        "  Optional: R2_KEY_PREFIX (default 'openmontage'), R2_ENDPOINT."
    )
    agent_skills = ["r2-storage"]

    capabilities = ["upload_media", "public_url", "delete_media", "object_exists", "list_media", "sweep_media"]
    supports = {
        "public_urls": True,
        "images": True,
        "video": True,
        "audio": True,
        "unique_keys": True,
        "public_readback_check": True,
        "private_storage": False,
    }
    best_for = [
        "giving a provider a URL it can download (AnyFast asset library, reference video)",
        "keeping large media out of a request body that has a size ceiling",
        "reusing one hosted reference across many generations",
    ]
    not_good_for = [
        "anything that must stay private — objects are world-readable by URL",
        "long-term archival (treat it as a staging bucket and clean up)",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATIONS), "default": "upload"},
            "path": {"type": "string", "description": "Local file to upload."},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local files for operation=upload_many.",
            },
            "project": {
                "type": "string",
                "description": "Project slug — the first segment: <prefix>/<project>/<kind>/<file>.",
            },
            "kind": {
                "type": "string",
                "enum": list(r2_client.KINDS),
                "description": (
                    "What the object is for. `faces` is retained after the provider "
                    "ingests it (re-registering a person means re-uploading); the rest "
                    "are transient and swept."
                ),
            },
            "folder": {
                "type": "string",
                "description": "Optional extra segment under <prefix>/<project>/<kind>/.",
            },
            "older_than_days": {"type": "number", "default": 7, "description": "sweep: age threshold."},
            "include_retained": {
                "type": "boolean",
                "default": False,
                "description": "sweep: also delete `faces`. Off by default.",
            },
            "sweep_dry_run": {
                "type": "boolean",
                "default": True,
                "description": "sweep: list what would go without deleting. Deletion is irreversible.",
            },
            "key": {"type": "string", "description": "Exact object key (overrides folder/unique)."},
            "unique": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Append a random suffix to the key. Keep this on: overwriting a key a "
                    "provider already fetched silently changes what it ingested."
                ),
            },
            "content_type": {"type": "string", "description": "Override the guessed MIME type."},
            "verify_public": {
                "type": "boolean",
                "default": True,
                "description": "HEAD the public URL after upload to prove it is readable.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=10, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, backoff_seconds=2.0, retryable_errors=["timeout"])
    idempotency_key_fields = ["path", "key", "folder"]
    side_effects = [
        "writes a publicly readable object to the R2 bucket",
        "delete removes an object permanently",
    ]
    user_visible_verification = [
        "Open the returned URL in a private browser window to confirm it is public",
    ]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if r2_client.is_configured() else ToolStatus.UNAVAILABLE

    def is_operation_available(self, operation: str) -> bool:
        return operation in self.OPERATIONS

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """R2 has no egress fees; storage cost at these volumes is negligible."""
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        paths = inputs.get("paths") or ([inputs["path"]] if inputs.get("path") else [])
        total_mb = 0.0
        for item in paths:
            candidate = Path(str(item)).expanduser()
            if candidate.is_file():
                total_mb += candidate.stat().st_size / 1_000_000
        return round(2.0 + total_mb / 5.0, 1)

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        operation = str(inputs.get("operation", "upload"))
        report: dict[str, Any] = {
            "tool": self.name,
            "operation": operation,
            "status": self.get_status().value,
            "would_execute": False,
            "missing_env": r2_client.missing_env(),
        }
        try:
            self._validate(inputs, operation)
            if r2_client.is_configured():
                config = r2_client.get_config()
                report["bucket"] = config["bucket"]
                report["public_base_url"] = config["public_base_url"]
                if operation in ("upload", "upload_many"):
                    planned = [
                        r2_client.build_key(
                            Path(str(p)).name,
                            project=inputs.get("project"),
                            kind=inputs.get("kind"),
                            folder=inputs.get("folder"),
                            unique=bool(inputs.get("unique", True)),
                        )
                        for p in self._paths(inputs)
                    ]
                    report["planned_keys"] = planned
                    report["planned_urls"] = [r2_client.public_url(k, config) for k in planned]
            report["valid"] = True
        except (ValueError, FileNotFoundError, r2_client.R2ConfigError) as exc:
            report["valid"] = False
            report["error"] = str(exc)
        return report

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        operation = str(inputs.get("operation", "upload"))
        try:
            self._validate(inputs, operation)
            data = self._dispatch(operation, inputs)
        except (ValueError, FileNotFoundError, r2_client.R2ConfigError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"R2 {operation} failed: {exc}",
                duration_seconds=round(time.time() - started, 2),
            )
        return ToolResult(
            success=True,
            data={"provider": self.provider, "operation": operation, **data},
            duration_seconds=round(time.time() - started, 2),
        )

    def _dispatch(self, operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
        if operation == "upload":
            return r2_client.upload_file(
                inputs["path"],
                project=inputs.get("project"),
                kind=inputs.get("kind"),
                folder=inputs.get("folder"),
                key=inputs.get("key"),
                unique=bool(inputs.get("unique", True)),
                content_type=inputs.get("content_type"),
                verify_public=bool(inputs.get("verify_public", True)),
            )
        if operation == "upload_many":
            uploaded = [
                r2_client.upload_file(
                    path,
                    project=inputs.get("project"),
                    kind=inputs.get("kind"),
                    folder=inputs.get("folder"),
                    unique=bool(inputs.get("unique", True)),
                    content_type=inputs.get("content_type"),
                    verify_public=bool(inputs.get("verify_public", True)),
                )
                for path in self._paths(inputs)
            ]
            return {"uploaded": uploaded, "urls": [item["url"] for item in uploaded]}
        if operation == "delete":
            return r2_client.delete_object(str(inputs["key"]))
        if operation == "exists":
            key = str(inputs["key"])
            return {"key": key, "exists": r2_client.object_exists(key)}
        if operation == "url":
            key = str(inputs["key"])
            return {"key": key, "url": r2_client.public_url(key)}
        if operation == "list":
            objects = r2_client.list_objects(
                r2_client.key_prefix_for(inputs.get("project"), inputs.get("kind"))
            )
            return {
                "objects": objects,
                "count": len(objects),
                "total_bytes": sum(o["size_bytes"] for o in objects),
            }
        if operation == "sweep":
            return r2_client.sweep(
                project=inputs.get("project"),
                kind=inputs.get("kind"),
                older_than_days=float(inputs.get("older_than_days", 7)),
                include_retained=bool(inputs.get("include_retained", False)),
                dry_run=bool(inputs.get("sweep_dry_run", True)),
            )
        raise ValueError(f"unsupported operation: {operation}")

    @staticmethod
    def _paths(inputs: dict[str, Any]) -> list[str]:
        if inputs.get("paths"):
            return [str(p) for p in inputs["paths"]]
        return [str(inputs["path"])] if inputs.get("path") else []

    def _validate(self, inputs: dict[str, Any], operation: str) -> None:
        if operation not in self.OPERATIONS:
            raise ValueError("operation must be one of " + ", ".join(self.OPERATIONS))
        if operation == "upload" and not inputs.get("path"):
            raise ValueError("upload requires path")
        if operation == "upload_many" and not inputs.get("paths"):
            raise ValueError("upload_many requires paths")
        if operation in ("delete", "exists", "url") and not inputs.get("key"):
            raise ValueError(f"{operation} requires key")
        for path in self._paths(inputs):
            if not Path(path).expanduser().is_file():
                raise FileNotFoundError(f"file not found: {path}")
