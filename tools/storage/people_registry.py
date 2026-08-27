"""Registry of people usable as video face references, and their asset IDs.

A face reference is expensive to recreate: it must be uploaded to a provider
asset group, and a verified group additionally requires the person to be
physically present with a phone. This tool remembers who has been registered,
which group holds their references, and therefore **which models can use them**.

Storage is local, per project, and outside the repository
(`~/.openmontage/projects/<project-slug>/people.db`). It holds names and
provider identifiers — never images.

The routing rule this exists to enforce:

    AIGC group          -> every Seedance model (2.5 and 2.0)
    LivenessFace group  -> Seedance 2.0 family ONLY

Before generating with someone's face, call `list_people` and offer the user
their registered people rather than re-uploading. `references` returns only the
assets the chosen model can actually read.
"""

from __future__ import annotations

import time
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from lib import people_registry


class PeopleRegistry(BaseTool):
    """Local, per-project record of face references and their asset groups."""

    name = "people_registry"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "person_reference_registry"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    OPERATIONS = (
        "list_people",
        "get_person",
        "register_person",
        "record_group",
        "record_asset",
        "resolve_upload_group",
        "references",
        "forget_person",
    )

    dependencies = []
    install_instructions = (
        "No setup: a SQLite file is created on first write at "
        "~/.openmontage/projects/<project-slug>/people.db. Override with "
        "OPENMONTAGE_PEOPLE_DB (exact file) or OPENMONTAGE_STATE_DIR (root directory)."
    )
    agent_skills = ["anyfast-assets"]

    capabilities = ["person_lookup", "asset_reference_lookup", "consent_record"]
    supports = {
        "per_project_isolation": True,
        "model_compatibility_routing": True,
        "consent_tracking": True,
        "stores_images": False,
    }
    best_for = [
        "offering the user a person they already registered instead of re-uploading",
        "knowing whether a face reference works on Seedance 2.5 or only on 2.0",
        "keeping a LivenessFace GroupId that the provider cannot list back",
    ]
    not_good_for = [
        "storing images or any biometric data — it holds identifiers only",
        "sharing between clients: each project gets its own database",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATIONS), "default": "list_people"},
            "project_dir": {
                "type": "string",
                "description": "Project directory; its name scopes the database. Omit for the shared default store.",
            },
            "person": {"type": "string", "description": "Person slug or display name."},
            "display_name": {"type": "string"},
            "consent_confirmed": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set once, when the user confirms this person authorized use of their "
                    "likeness. Recorded with a timestamp and never cleared."
                ),
            },
            "consent_note": {"type": "string"},
            "group_id": {"type": "string"},
            "group_type": {"type": "string", "enum": list(people_registry.GROUP_TYPES)},
            "group_name": {"type": "string"},
            "byted_token": {
                "type": "string",
                "description": "Verification token that produced a LivenessFace group (it expires; the GroupId does not).",
            },
            "asset_id": {"type": "string"},
            "asset_type": {"type": "string", "enum": ["Image", "Video", "Audio"], "default": "Image"},
            "name": {"type": "string"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "model": {
                "type": "string",
                "description": "Filter references to those this model can resolve, e.g. seedance-2.5.",
            },
            "prefer": {
                "type": "string",
                "enum": list(people_registry.GROUP_TYPES),
                "default": "AIGC",
                "description": "Group type preferred by resolve_upload_group.",
            },
            "confirm": {"type": "boolean", "default": False, "description": "Required by forget_person."},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, disk_mb=5, network_required=False)
    idempotency_key_fields = ["operation", "person", "asset_id", "group_id"]
    side_effects = ["writes a local SQLite file outside the repository"]
    user_visible_verification = [
        "Confirm the person offered is the one the user meant before generating with their face",
    ]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def is_operation_available(self, operation: str) -> bool:
        return operation in self.OPERATIONS

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info["model_compatibility"] = {
            group_type: list(models)
            for group_type, models in people_registry.MODELS_BY_GROUP_TYPE.items()
        }
        return info

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        operation = str(inputs.get("operation", "list_people"))
        report: dict[str, Any] = {
            "tool": self.name,
            "operation": operation,
            "status": "available",
            "would_execute": False,
            "database": str(people_registry.db_path(inputs.get("project_dir"))),
        }
        try:
            self._validate(inputs, operation)
            report["valid"] = True
        except (ValueError, people_registry.RegistryError) as exc:
            report["valid"] = False
            report["error"] = str(exc)
        return report

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        operation = str(inputs.get("operation", "list_people"))
        try:
            self._validate(inputs, operation)
            with people_registry.connect(inputs.get("project_dir")) as connection:
                data = self._dispatch(operation, inputs, connection)
        except (ValueError, people_registry.RegistryError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            return ToolResult(success=False, error=f"people_registry {operation} failed: {exc}")
        return ToolResult(
            success=True,
            data={
                "operation": operation,
                "database": str(people_registry.db_path(inputs.get("project_dir"))),
                **data,
            },
            duration_seconds=round(time.time() - started, 3),
        )

    def _dispatch(self, operation: str, inputs: dict[str, Any], connection: Any) -> dict[str, Any]:
        person = inputs.get("person")

        if operation == "list_people":
            people = people_registry.list_people(connection)
            return {
                "people": people,
                "count": len(people),
                "hint": (
                    "Offer these to the user before uploading a new face. A person whose "
                    "only group is LivenessFace can be generated with Seedance 2.0 only."
                ),
            }

        if operation == "get_person":
            return {"person": people_registry.get_person(connection, str(person))}

        if operation == "register_person":
            return {
                "person": people_registry.upsert_person(
                    connection,
                    slug=str(person),
                    display_name=inputs.get("display_name"),
                    consent_confirmed=bool(inputs.get("consent_confirmed", False)),
                    consent_note=inputs.get("consent_note"),
                )
            }

        if operation == "record_group":
            return {
                "group": people_registry.record_group(
                    connection,
                    person_slug=str(person),
                    group_id=str(inputs["group_id"]),
                    group_type=str(inputs["group_type"]),
                    name=inputs.get("group_name"),
                    byted_token=inputs.get("byted_token"),
                )
            }

        if operation == "record_asset":
            return {
                "asset": people_registry.record_asset(
                    connection,
                    person_slug=str(person),
                    group_id=str(inputs["group_id"]),
                    asset_id=str(inputs["asset_id"]),
                    asset_type=str(inputs.get("asset_type", "Image")),
                    name=inputs.get("name"),
                    source=inputs.get("source"),
                    status=inputs.get("status"),
                )
            }

        if operation == "resolve_upload_group":
            group = people_registry.resolve_upload_group(
                connection, str(person), prefer=str(inputs.get("prefer", "AIGC"))
            )
            return {
                "group": group,
                "needs_new_group": group is None,
                "hint": (
                    "No group yet — create an AIGC group for this person and upload there; "
                    "its assets work on every Seedance model."
                    if group is None
                    else f"Upload into {group['group_id']} ({group['group_type']})."
                ),
            }

        if operation == "references":
            model = inputs.get("model")
            references = people_registry.references_for(
                connection, str(person), model=model, asset_type=str(inputs.get("asset_type", "Image"))
            )
            return {
                "references": references,
                "count": len(references),
                "model": model,
                "hint": (
                    "Pass asset_ref values straight into the generation's reference inputs."
                    if references
                    else "No reference this model can read; check the person's group type."
                ),
            }

        if operation == "forget_person":
            return people_registry.forget_person(connection, str(person))

        raise ValueError(f"unsupported operation: {operation}")

    def _validate(self, inputs: dict[str, Any], operation: str) -> None:
        if operation not in self.OPERATIONS:
            raise ValueError("operation must be one of " + ", ".join(self.OPERATIONS))
        needs_person = {
            "get_person",
            "register_person",
            "record_group",
            "record_asset",
            "resolve_upload_group",
            "references",
            "forget_person",
        }
        if operation in needs_person and not inputs.get("person"):
            raise ValueError(f"{operation} requires person")
        if operation == "record_group":
            for field in ("group_id", "group_type"):
                if not inputs.get(field):
                    raise ValueError(f"record_group requires {field}")
        if operation == "record_asset":
            for field in ("group_id", "asset_id"):
                if not inputs.get(field):
                    raise ValueError(f"record_asset requires {field}")
        if operation == "forget_person" and not inputs.get("confirm"):
            raise ValueError(
                "forget_person drops the local record of this person's groups and assets; "
                "pass confirm=true (remote assets are not deleted)"
            )
