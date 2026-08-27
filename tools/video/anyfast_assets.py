"""AnyFast asset library — reusable `asset://` media for Seedance generation.

Two jobs:

1. **Asset management** (`/volc/asset`): groups and assets (image, video, audio)
   that Seedance requests reference as `asset://<ASSET_ID>` instead of public
   URLs or Base64. Video can *only* be referenced by URL or asset ID, so this is
   the sole route for a local clip.

2. **Real-human verification** (LivenessFace): AnyFast's docs are explicit —
   do not send reference images or videos containing real-person faces unless
   your workflow and account are authorized. The authorized route is:

       create_liveness_session -> the person verifies on their phone (H5Link)
       -> get_liveness_result  -> GroupId of a LivenessFace group
       -> upload               -> asset://<id>, face-matched to that person
       -> anyfast_video        -> pass the asset:// reference

   Verification needs an API token created with the **Byteplus-Direct** group;
   an AIGC-only token cannot create sessions or LivenessFace assets.

**How the upload actually has to work.** AnyFast ingests an asset from a URL it
can download. A local file is therefore published to R2 first (see `r2_storage`)
and handed over as a public URL; AnyFast's own multipart upload is behind
`allow_multipart` because assets created that way returned an Id and then never
resolved, while the identical files ingested from a public URL went `Active`
within seconds.

CreateAsset is asynchronous: `upload` polls GetAsset until `Active`. A create
that returns an Id and then answers `[NotFound.asset_id]` forever did not
survive preprocessing — face consistency failed, or the `Name` duplicates
another asset in the group (AnyFast reports that as a 404 too, which is why
names get a unique suffix by default).

Reference: https://docs.anyfast.ai/guides/model-api/bytedance/volc-asset
          https://docs.anyfast.ai/guides/model-api/bytedance/volc-real-human-assets
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
from lib import people_registry as _people
from tools.video import _anyfast


class AnyFastAssets(BaseTool):
    """Manage AnyFast asset groups, assets, and real-human verification."""

    name = "anyfast_assets"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "media_asset_management"
    provider = "anyfast"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    OPERATIONS = (
        "upload",
        "create_asset",
        "get_asset",
        "update_asset",
        "delete_asset",
        "list_assets",
        "create_group",
        "update_group",
        "delete_group",
        "list_groups",
        "create_liveness_session",
        "get_liveness_result",
    )
    DESTRUCTIVE_OPERATIONS = ("delete_asset", "delete_group")
    ASSET_TYPES = ("Image", "Video", "Audio")
    GROUP_TYPES = _anyfast.GROUP_TYPES

    dependencies = ["env:ANYFAST_API_KEY"]
    install_instructions = (
        "Set ANYFAST_API_KEY to the API key from https://www.anyfast.ai/console/token "
        "(key body only; the tool adds the 'Bearer ' scheme).\n"
        "  Real-human (LivenessFace) verification additionally requires a token created "
        "with the Byteplus-Direct group; an AIGC-only token returns "
        "'GroupType must be one of [AIGC]'.\n"
        "  Optional: ANYFAST_BASE_URL, ANYFAST_ASSET_GROUP_ID (default group for uploads)."
    )
    agent_skills = ["anyfast-assets", "r2-storage", "seedance-2-5"]

    capabilities = [
        "asset_upload",
        "asset_query",
        "asset_update",
        "asset_delete",
        "asset_group_management",
        "real_person_verification",
    ]
    supports = {
        "image_assets": True,
        "video_assets": True,
        "audio_assets": True,
        "local_file_upload": True,
        "url_ingest": True,
        "data_uri_ingest": True,
        "real_person_verification": True,
        "face_matched_assets": True,
        "asset_reuse": True,
    }
    best_for = [
        "using a LOCAL video as a Seedance reference — asset:// is the only route",
        "authorized real-person portraits via LivenessFace verification",
        "reusing one character/product reference across many generations",
        "keeping large media out of the 64 MB request body",
    ]
    not_good_for = [
        "unauthorized use of a real person's likeness",
        "mixing different people inside one real-human asset group",
        "one-off references that a public URL already covers",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATIONS), "default": "upload"},
            "source": {
                "type": "string",
                "description": (
                    "Asset content: local file path (multipart upload), public URL, or "
                    "data: URI. Required for upload/create_asset."
                ),
            },
            "asset_type": {
                "type": "string",
                "enum": list(ASSET_TYPES),
                "default": "Image",
                "description": "Selects the billing model: volc-asset / -video / -audio.",
            },
            "name": {"type": "string", "description": "Asset or group name."},
            "asset_id": {"type": "string"},
            "group_id": {
                "type": "string",
                "description": (
                    "Target group. Defaults to ANYFAST_ASSET_GROUP_ID; a new AIGC group is "
                    "created when neither is set. For real-human assets, pass the GroupId "
                    "from get_liveness_result."
                ),
            },
            "group_type": {
                "type": "string",
                "enum": list(GROUP_TYPES),
                "description": "Filter (list) or intent guard (upload). LivenessFace = real-human.",
            },
            "group_ids": {"type": "array", "items": {"type": "string"}},
            "person": {
                "type": "string",
                "description": (
                    "Person slug for a FACE reference. The upload is routed to that "
                    "person's asset group (created as AIGC if they have none) and the "
                    "result is recorded in people_registry so it can be reused. Only for "
                    "faces — ordinary reference images do not need a person."
                ),
            },
            "project_dir": {
                "type": "string",
                "description": "Scopes the people database; each project keeps its own.",
            },
            "display_name": {"type": "string", "description": "Human name for a new person."},
            "consent_confirmed": {
                "type": "boolean",
                "default": False,
                "description": "Record that the user confirmed this person authorized use of their likeness.",
            },
            "page_number": {"type": "integer", "minimum": 1, "default": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "callback_url": {
                "type": "string",
                "description": (
                    "REQUIRED for create_liveness_session. AnyFast rejects the call without "
                    "it even though the published schema marks it optional. Any reachable "
                    "https:// URL works; the result is read by polling get_liveness_result."
                ),
            },
            "byted_token": {
                "type": "string",
                "description": "Token from create_liveness_session (get_liveness_result).",
            },
            "confirm": {
                "type": "boolean",
                "default": False,
                "description": "Required for delete_asset / delete_group; deletion is irreversible.",
            },
            "unique_name": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Append a random suffix to Name. Keep this on: a duplicate Name inside "
                    "a group makes AnyFast answer 404 as if the group did not exist."
                ),
            },
            "hosting_folder": {
                "type": "string",
                "description": "R2 folder for a local source, e.g. a person or project slug.",
            },
            "keep_hosted": {
                "type": "boolean",
                "description": (
                    "Override retention of the R2 staging object. Faces are kept by "
                    "default (re-registering needs the original); other kinds are deleted "
                    "once the provider has ingested them."
                ),
            },
            "allow_multipart": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Use AnyFast's own multipart upload instead of hosting on R2. Off by "
                    "default: multipart returns an Id but the assets stayed unresolvable, "
                    "while the same files ingested from a public URL went Active."
                ),
            },
            "poll_interval_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 3},
            "timeout_seconds": {"type": "number", "minimum": 1, "default": 300},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=10, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2, backoff_seconds=2.0, retryable_errors=["rate_limit", "timeout", "server_error"]
    )
    idempotency_key_fields = ["operation", "source", "asset_type", "group_id", "name"]
    side_effects = [
        "creates, updates, or deletes media in the remote AnyFast asset library",
        "uploads local files to AnyFast object storage",
        "starts a real-person verification session that a human completes on a phone",
    ]
    user_visible_verification = [
        "Confirm the asset Status is Active before referencing it in a generation",
        "Confirm the person authorized the use of their likeness before uploading a portrait",
    ]

    # ---- status ----

    def get_status(self) -> ToolStatus:
        api_key = _anyfast.get_api_key()
        if not api_key or api_key.lower().startswith("bearer "):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def is_operation_available(self, operation: str) -> bool:
        return operation in self.OPERATIONS

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """AnyFast does not publish asset prices; reads are documented as free."""
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        operation = str(inputs.get("operation", "upload"))
        if operation in ("upload", "create_asset"):
            # 6-12s of upstream review, longer when the queue is congested.
            return {"Image": 12.0, "Video": 20.0, "Audio": 8.0}.get(
                str(inputs.get("asset_type", "Image")), 12.0
            )
        return 2.0

    # ---- preflight ----

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        operation = str(inputs.get("operation", "upload"))
        report: dict[str, Any] = {
            "tool": self.name,
            "operation": operation,
            "status": self.get_status().value,
            "would_execute": False,
            "destructive": operation in self.DESTRUCTIVE_OPERATIONS,
        }
        try:
            self._validate(inputs, operation)
            base = _anyfast.get_base_url()
            path_key = {
                "upload": "create_asset",
                "create_asset": "create_asset",
                "get_asset": "get_asset",
                "update_asset": "update_asset",
                "delete_asset": "delete_asset",
                "list_assets": "list_assets",
                "create_group": "create_group",
                "update_group": "update_group",
                "delete_group": "delete_group",
                "list_groups": "list_groups",
                "create_liveness_session": "create_liveness_session",
                "get_liveness_result": "get_liveness_result",
            }[operation]
            report["endpoint"] = f"POST {base}{_anyfast.ASSET_PATHS[path_key]}"
            report["estimated_runtime_seconds"] = self.estimate_runtime(inputs)
            if operation in ("upload", "create_asset"):
                source = str(inputs.get("source", ""))
                report["upload_mode"] = (
                    "url_or_data_uri"
                    if _anyfast.is_remote_or_asset(source) or source.startswith("data:")
                    else "multipart_file"
                )
                report["billing_model"] = _anyfast.ASSET_MODELS[
                    str(inputs.get("asset_type", "Image"))
                ]
            if str(inputs.get("group_type")) == "LivenessFace" or operation.startswith("create_liveness"):
                report["authorization_note"] = (
                    "Real-human assets require the person's authorization and a "
                    "Byteplus-Direct token; uploads are face-matched to the verified person."
                )
            report["valid"] = True
        except (TypeError, ValueError, KeyError) as exc:
            report["valid"] = False
            report["error"] = _anyfast.safe_error(exc if isinstance(exc, Exception) else ValueError(str(exc)))
        return report

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        operation = str(inputs.get("operation", "upload"))
        api_key = _anyfast.get_api_key()
        try:
            self._validate(inputs, operation)
            _anyfast.check_api_key(api_key)
        except ValueError as exc:
            return ToolResult(success=False, error=_anyfast.safe_error(exc, api_key))

        assert api_key is not None
        try:
            data = self._dispatch(operation, inputs, api_key)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"AnyFast asset {operation} failed: {_anyfast.safe_error(exc, api_key)}",
                duration_seconds=round(time.time() - started, 2),
            )
        return ToolResult(
            success=True,
            data={"provider": self.provider, "operation": operation, **data},
            duration_seconds=round(time.time() - started, 2),
        )

    def _dispatch(self, operation: str, inputs: dict[str, Any], api_key: str) -> dict[str, Any]:
        if operation == "upload":
            source = str(inputs["source"])
            person = inputs.get("person")
            group_id = inputs.get("group_id")
            group_type = inputs.get("group_type")
            registry_note = None

            if person and not group_id:
                group_id, group_type, registry_note = self._group_for_person(inputs, api_key)

            try:
                result = _anyfast.upload_asset(
                    api_key,
                    source=source,
                    asset_kind=str(inputs.get("asset_type", "Image")),
                    name=str(inputs.get("name") or Path(source).stem or "openmontage-asset"),
                    group_id=group_id,
                    group_type=group_type,
                    timeout=float(inputs.get("timeout_seconds", 300)),
                    poll_interval=float(inputs.get("poll_interval_seconds", 3)),
                    unique_name=bool(inputs.get("unique_name", True)),
                    allow_multipart=bool(inputs.get("allow_multipart", False)),
                    hosting_folder=inputs.get("hosting_folder") or (person or None),
                    hosting_project=self._project_slug(inputs),
                    hosting_kind=self._hosting_kind(inputs),
                    keep_hosted=inputs.get("keep_hosted"),
                )
            except Exception as exc:
                if _anyfast.is_face_rejection(exc) and str(group_type or "AIGC") == "AIGC":
                    raise RuntimeError(
                        f"{_anyfast.safe_error(exc, api_key)}\n"
                        "  This face was refused by an ordinary AIGC group, so it needs a "
                        "VERIFIED group. That requires the person to be present with a "
                        "phone. STOP and ask the user before starting verification, then: "
                        "create_liveness_session -> they complete it -> get_liveness_result "
                        "-> upload with that group_id. Generation is then limited to "
                        "Seedance 2.0 — 2.5 cannot read LivenessFace assets."
                    ) from exc
                raise

            if person:
                result["person"] = str(person)
                result["registry"] = self._record_upload(inputs, result)
                if registry_note:
                    result["registry_note"] = registry_note
                result["usable_models"] = _people.usable_models(
                    str(result.get("group_type") or "AIGC")
                )
            return result

        if operation == "create_asset":
            source = str(inputs["source"])
            group_id = str(inputs.get("group_id") or "")
            if not group_id:
                raise ValueError("create_asset requires group_id; use operation=upload to auto-create one")
            asset_id = _anyfast.create_asset(
                api_key,
                group_id=group_id,
                asset_kind=str(inputs.get("asset_type", "Image")),
                name=str(inputs.get("name") or Path(source).stem or "openmontage-asset"),
                source=source,
                group_type=inputs.get("group_type"),
                allow_multipart=bool(inputs.get("allow_multipart", False)),
                hosting_folder=inputs.get("hosting_folder"),
                hosting_project=self._project_slug(inputs),
                hosting_kind=self._hosting_kind(inputs),
            )
            return {
                "asset_id": asset_id,
                "asset_ref": f"asset://{asset_id}",
                "group_id": group_id,
                "status": "Processing",
                "next_step": "poll operation=get_asset until Status is Active",
            }

        if operation == "get_asset":
            asset_id = str(inputs["asset_id"])
            asset, source = _anyfast.read_asset(
                api_key,
                asset_id,
                group_id=inputs.get("group_id"),
                group_type=inputs.get("group_type"),
            )
            if asset is None:
                raise RuntimeError(
                    f"asset {asset_id} is not resolvable by this token. GetAsset only "
                    "resolves AIGC assets — pass group_id (and group_type=LivenessFace "
                    "for a real-human asset) so the read can use ListAssets, and make "
                    "sure this is the token that owns the group."
                )
            error = asset.get("Error") if isinstance(asset.get("Error"), dict) else {}
            return {
                "asset": asset,
                "asset_ref": f"asset://{asset.get('Id', asset_id)}",
                "status": asset.get("Status"),
                "usable": asset.get("Status") == "Active",
                "error_code": error.get("Code"),
                "read_via": source,
            }

        if operation == "update_asset":
            return {
                "result": _anyfast.update_asset(
                    api_key,
                    str(inputs["asset_id"]),
                    name=inputs.get("name"),
                    group_id=inputs.get("group_id"),
                )
            }

        if operation == "delete_asset":
            return {"result": _anyfast.delete_asset(api_key, str(inputs["asset_id"]))}

        if operation == "list_assets":
            return {
                "result": _anyfast.list_assets(
                    api_key,
                    filters=self._filters(inputs),
                    page_number=int(inputs.get("page_number", 1)),
                    page_size=int(inputs.get("page_size", 10)),
                )
            }

        if operation == "create_group":
            group_id = _anyfast.create_asset_group(
                api_key,
                str(inputs["name"]),
                str(inputs.get("asset_type", "Image")),
                group_type=inputs.get("group_type"),
            )
            return {"group_id": group_id}

        if operation == "update_group":
            return {
                "result": _anyfast.update_asset_group(
                    api_key, str(inputs["group_id"]), str(inputs["name"])
                )
            }

        if operation == "delete_group":
            return {"result": _anyfast.delete_asset_group(api_key, str(inputs["group_id"]))}

        if operation == "list_groups":
            return {
                "result": _anyfast.list_asset_groups(
                    api_key,
                    filters=self._filters(inputs),
                    page_number=int(inputs.get("page_number", 1)),
                    page_size=int(inputs.get("page_size", 10)),
                )
            }

        if operation == "create_liveness_session":
            session = _anyfast.create_liveness_session(api_key, inputs.get("callback_url"))
            return {
                "byted_token": session.get("BytedToken"),
                "h5_link": session.get("H5Link"),
                "callback_url": session.get("CallbackURL"),
                "next_step": (
                    "Send h5_link to the authorizing person to complete verification on a "
                    "phone, then call operation=get_liveness_result with byted_token."
                ),
            }

        if operation == "get_liveness_result":
            result = _anyfast.get_liveness_result(api_key, str(inputs["byted_token"]))
            group_id = str(result.get("GroupId") or "")
            if group_id and inputs.get("person"):
                with _people.connect(inputs.get("project_dir")) as connection:
                    _people.record_group(
                        connection,
                        person_slug=str(inputs["person"]),
                        group_id=group_id,
                        group_type="LivenessFace",
                        name=inputs.get("group_name"),
                        byted_token=str(inputs["byted_token"]),
                    )
            return {
                "group_id": group_id,
                "verified": bool(group_id),
                "group_type": "LivenessFace" if group_id else None,
                "next_step": (
                    "Upload a clear, front-facing asset of the same person with "
                    "operation=upload and this group_id"
                    if group_id
                    else "Verification is not finished; ask the person to complete the H5 page, then query again"
                ),
            }

        raise ValueError(f"unsupported operation: {operation}")

    @staticmethod
    def _project_slug(inputs: dict[str, Any]) -> str | None:
        project_dir = inputs.get("project_dir")
        return Path(str(project_dir)).name if project_dir else None

    @staticmethod
    def _hosting_kind(inputs: dict[str, Any]) -> str:
        """`faces` when the upload belongs to a person, else by media type.

        The distinction drives retention: a face source is kept because
        re-registering that person means re-uploading it, while a one-shot
        reference is swept once the provider has copied it.
        """
        if inputs.get("person"):
            return "faces"
        return {"Video": "video", "Audio": "audio"}.get(
            str(inputs.get("asset_type", "Image")), "refs"
        )

    # ---- person routing ----

    def _group_for_person(
        self, inputs: dict[str, Any], api_key: str
    ) -> tuple[str, str, str | None]:
        """Find or create the asset group a person's face references belong in.

        AIGC is preferred: its assets are readable by every Seedance model, while
        a LivenessFace group restricts generation to the 2.0 family.
        """
        person = str(inputs["person"])
        with _people.connect(inputs.get("project_dir")) as connection:
            _people.upsert_person(
                connection,
                slug=person,
                display_name=inputs.get("display_name"),
                consent_confirmed=bool(inputs.get("consent_confirmed", False)),
            )
            existing = _people.resolve_upload_group(connection, person)
            if existing:
                return existing["group_id"], existing["group_type"], (
                    f"reusing {existing['group_type']} group {existing['group_id']}"
                )
            slug = _people.slugify(person)
            group_id = _anyfast.create_asset_group(api_key, f"{slug}-assets", "Image")
            _people.record_group(
                connection,
                person_slug=person,
                group_id=group_id,
                group_type="AIGC",
                name=f"{slug}-assets",
            )
            return group_id, "AIGC", f"created AIGC group {group_id} for {slug}"

    def _record_upload(self, inputs: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        with _people.connect(inputs.get("project_dir")) as connection:
            _people.record_group(
                connection,
                person_slug=str(inputs["person"]),
                group_id=str(result["group_id"]),
                group_type=str(result.get("group_type") or "AIGC"),
            )
            asset = _people.record_asset(
                connection,
                person_slug=str(inputs["person"]),
                group_id=str(result["group_id"]),
                asset_id=str(result["asset_id"]),
                asset_type=str(result.get("asset_type", "Image")),
                name=result.get("name"),
                source=str(inputs.get("source")),
                status=str(result.get("status")),
            )
        return {"recorded": True, "asset": asset}

    # ---- helpers ----

    @staticmethod
    def _filters(inputs: dict[str, Any]) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if inputs.get("name"):
            filters["Name"] = str(inputs["name"])
        if inputs.get("group_ids"):
            filters["GroupIds"] = [str(value) for value in inputs["group_ids"]]
        if inputs.get("group_id"):
            filters.setdefault("GroupIds", [str(inputs["group_id"])])
        if inputs.get("group_type"):
            filters["GroupType"] = str(inputs["group_type"])
        return filters

    def _validate(self, inputs: dict[str, Any], operation: str) -> None:
        if operation not in self.OPERATIONS:
            raise ValueError("operation must be one of " + ", ".join(self.OPERATIONS))
        asset_type = str(inputs.get("asset_type", "Image"))
        if asset_type not in self.ASSET_TYPES:
            raise ValueError("asset_type must be Image, Video, or Audio")
        group_type = inputs.get("group_type")
        if group_type is not None and str(group_type) not in self.GROUP_TYPES:
            raise ValueError("group_type must be AIGC or LivenessFace")

        required: dict[str, tuple[str, ...]] = {
            "upload": ("source",),
            "create_asset": ("source", "group_id"),
            "get_asset": ("asset_id",),
            "update_asset": ("asset_id",),
            "delete_asset": ("asset_id",),
            "create_group": ("name",),
            "update_group": ("group_id", "name"),
            "delete_group": ("group_id",),
            "create_liveness_session": ("callback_url",),
            "get_liveness_result": ("byted_token",),
        }
        for field in required.get(operation, ()):
            if not inputs.get(field):
                raise ValueError(f"{operation} requires {field}")

        if operation in self.DESTRUCTIVE_OPERATIONS and not inputs.get("confirm"):
            raise ValueError(
                f"{operation} permanently removes remote data; pass confirm=true to proceed"
            )
