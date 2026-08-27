"""Contract tests for the local people/face-reference registry."""

from __future__ import annotations

import pytest

from lib import people_registry
from tools.base_tool import BaseTool, ToolRuntime, ToolStatus
from tools.storage.people_registry import PeopleRegistry


@pytest.fixture()
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENMONTAGE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENMONTAGE_PEOPLE_DB", raising=False)
    return tmp_path


class TestStorageLocation:
    def test_database_is_per_project_and_outside_the_repo(self, state_dir):
        one = people_registry.db_path("/somewhere/projects/roman-pinsa")
        two = people_registry.db_path("/somewhere/projects/other-client")
        assert one != two, "projects must not share a people database"
        assert one.parent.name == "roman-pinsa"
        assert str(one).startswith(str(state_dir))

    def test_explicit_override(self, state_dir, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENMONTAGE_PEOPLE_DB", str(tmp_path / "custom.db"))
        assert people_registry.db_path("anything").name == "custom.db"

    def test_no_project_falls_back_to_a_default_store(self, state_dir):
        assert people_registry.db_path(None).parent.name == "default"


class TestModelCompatibility:
    def test_aigc_group_serves_every_seedance_model(self):
        models = people_registry.usable_models("AIGC")
        assert "seedance-2.5" in models
        assert "seedance-2.0" in models

    def test_liveness_group_excludes_25(self):
        models = people_registry.usable_models("LivenessFace")
        assert "seedance-2.5" not in models, "2.5 cannot resolve LivenessFace assets"
        assert "seedance-2.0" in models


class TestRegistry:
    def test_person_group_and_asset_round_trip(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(
                conn, slug="Juan Arroyave", display_name="Juan Arroyave", consent_confirmed=True
            )
            people_registry.record_group(
                conn, person_slug="juan-arroyave", group_id="group-1", group_type="AIGC"
            )
            people_registry.record_asset(
                conn,
                person_slug="juan-arroyave",
                group_id="group-1",
                asset_id="asset-1",
                status="Active",
            )
            person = people_registry.get_person(conn, "juan-arroyave")

        assert person["slug"] == "juan-arroyave"
        assert person["consent_confirmed"] is True
        assert person["asset_count"] == 1
        assert "seedance-2.5" in person["models_available"]

    def test_consent_timestamp_is_never_cleared(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="ana", consent_confirmed=True)
            first = people_registry.get_person(conn, "ana")["consent_confirmed_at"]
            people_registry.upsert_person(conn, slug="ana", display_name="Ana")
            assert people_registry.get_person(conn, "ana")["consent_confirmed_at"] == first

    def test_references_are_filtered_by_model(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn, person_slug="juan", group_id="group-live", group_type="LivenessFace"
            )
            people_registry.record_asset(
                conn,
                person_slug="juan",
                group_id="group-live",
                asset_id="asset-live",
                status="Active",
            )
            assert people_registry.references_for(conn, "juan", model="seedance-2.0")
            assert people_registry.references_for(conn, "juan", model="seedance-2.5") == []

    def test_inactive_assets_are_not_offered(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn, person_slug="juan", group_id="g", group_type="AIGC"
            )
            people_registry.record_asset(
                conn, person_slug="juan", group_id="g", asset_id="a", status="Failed"
            )
            assert people_registry.references_for(conn, "juan") == []

    def test_upload_group_prefers_aigc(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn, person_slug="juan", group_id="group-live", group_type="LivenessFace"
            )
            people_registry.record_group(
                conn, person_slug="juan", group_id="group-aigc", group_type="AIGC"
            )
            chosen = people_registry.resolve_upload_group(conn, "juan")
        assert chosen["group_id"] == "group-aigc"

    def test_liveness_group_is_used_when_it_is_the_only_one(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn, person_slug="juan", group_id="group-live", group_type="LivenessFace"
            )
            assert people_registry.resolve_upload_group(conn, "juan")["group_type"] == "LivenessFace"

    def test_an_asset_needs_a_registered_group(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            with pytest.raises(people_registry.RegistryError, match="not registered"):
                people_registry.record_asset(
                    conn, person_slug="juan", group_id="ghost", asset_id="a"
                )

    def test_byted_token_is_kept_because_the_group_is_unlistable(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn,
                person_slug="juan",
                group_id="group-live",
                group_type="LivenessFace",
                byted_token="token-1",
            )
            group = people_registry.get_person(conn, "juan")["groups"][0]
        assert group["byted_token"] == "token-1"

    def test_forget_person_is_local_only(self, state_dir):
        with people_registry.connect("proj") as conn:
            people_registry.upsert_person(conn, slug="juan")
            people_registry.record_group(
                conn, person_slug="juan", group_id="g", group_type="AIGC"
            )
            result = people_registry.forget_person(conn, "juan")
            assert result["forgotten_groups"] == ["g"]
            assert "provider still holds" in result["note"]
            with pytest.raises(people_registry.RegistryError):
                people_registry.get_person(conn, "juan")


class TestToolSurface:
    def test_identity(self, state_dir):
        tool = PeopleRegistry()
        assert issubclass(PeopleRegistry, BaseTool)
        assert tool.capability == "person_reference_registry"
        assert tool.runtime == ToolRuntime.LOCAL
        assert tool.get_status() == ToolStatus.AVAILABLE
        assert tool.supports["stores_images"] is False

    def test_list_is_empty_before_anything_is_registered(self, state_dir):
        result = PeopleRegistry().execute({"operation": "list_people", "project_dir": "proj"})
        assert result.success is True
        assert result.data["count"] == 0

    def test_register_then_offer_references(self, state_dir):
        tool = PeopleRegistry()
        tool.execute(
            {
                "operation": "register_person",
                "person": "juan",
                "display_name": "Juan",
                "consent_confirmed": True,
                "project_dir": "proj",
            }
        )
        tool.execute(
            {
                "operation": "record_group",
                "person": "juan",
                "group_id": "g1",
                "group_type": "AIGC",
                "project_dir": "proj",
            }
        )
        tool.execute(
            {
                "operation": "record_asset",
                "person": "juan",
                "group_id": "g1",
                "asset_id": "a1",
                "status": "Active",
                "project_dir": "proj",
            }
        )
        refs = tool.execute(
            {
                "operation": "references",
                "person": "juan",
                "model": "seedance-2.5",
                "project_dir": "proj",
            }
        )
        assert refs.data["references"][0]["asset_ref"] == "asset://a1"

    def test_resolve_upload_group_reports_a_missing_group(self, state_dir):
        tool = PeopleRegistry()
        tool.execute({"operation": "register_person", "person": "new", "project_dir": "proj"})
        result = tool.execute(
            {"operation": "resolve_upload_group", "person": "new", "project_dir": "proj"}
        )
        assert result.data["needs_new_group"] is True
        assert "AIGC" in result.data["hint"]

    def test_forget_requires_confirmation(self, state_dir):
        tool = PeopleRegistry()
        tool.execute({"operation": "register_person", "person": "juan", "project_dir": "proj"})
        result = tool.execute({"operation": "forget_person", "person": "juan", "project_dir": "proj"})
        assert result.success is False
        assert "confirm=true" in result.error

    def test_operations_require_a_person(self, state_dir):
        assert "requires person" in PeopleRegistry().execute({"operation": "get_person"}).error

    def test_registry_discovers_the_tool(self, state_dir):
        from tools.tool_registry import registry

        registry.ensure_discovered()
        names = [t.name for t in registry.get_by_capability("person_reference_registry")]
        assert "people_registry" in names
