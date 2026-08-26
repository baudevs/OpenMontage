"""Contract tests for the AnyFast asset library and real-human verification.

All HTTP calls are mocked. This suite must never touch a live asset library.
"""

from __future__ import annotations

import pytest

from tools.base_tool import BaseTool, ToolRuntime, ToolStatus
from tools.video import _anyfast
from tools.video.anyfast_assets import AnyFastAssets


class _FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self._payload}")


@pytest.fixture()
def api_key(monkeypatch):
    monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
    monkeypatch.delenv("ANYFAST_ASSET_GROUP_ID", raising=False)
    return "fake-anyfast-key"


class TestContract:
    def test_identity(self, api_key):
        assert issubclass(AnyFastAssets, BaseTool)
        tool = AnyFastAssets()
        assert tool.name == "anyfast_assets"
        assert tool.provider == "anyfast"
        assert tool.capability == "media_asset_management"
        assert tool.runtime == ToolRuntime.API
        assert "env:ANYFAST_API_KEY" in tool.dependencies
        assert tool.supports["real_person_verification"] is True

    def test_status_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("ANYFAST_API_KEY", raising=False)
        assert AnyFastAssets().get_status() == ToolStatus.UNAVAILABLE
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        assert AnyFastAssets().get_status() == ToolStatus.AVAILABLE

    def test_every_documented_endpoint_is_wired(self):
        assert set(_anyfast.ASSET_PATHS) == {
            "create_group",
            "create_asset",
            "get_asset",
            "list_groups",
            "list_assets",
            "update_asset",
            "update_group",
            "delete_asset",
            "delete_group",
            "create_liveness_session",
            "get_liveness_result",
        }
        assert _anyfast.ASSET_PATHS["create_group"] == "/volc/asset/CreateAssetGroup"
        assert _anyfast.ASSET_PATHS["create_liveness_session"] == (
            "/volc/asset/CreateVisualValidateSession"
        )

    def test_billing_models_match_the_asset_type(self):
        assert _anyfast.ASSET_MODELS == {
            "Image": "volc-asset",
            "Video": "volc-asset-video",
            "Audio": "volc-asset-audio",
        }


class TestUpload:
    def test_local_file_is_hosted_on_r2_then_ingested_by_url(
        self, api_key, monkeypatch, tmp_path
    ):
        """The proven path: R2 public URL -> CreateAsset(URL), never multipart."""
        image = tmp_path / "portrait.jpg"
        image.write_bytes(b"fake-jpeg")
        calls: list[tuple[str, dict]] = []
        statuses = iter(["Processing", "Active"])

        monkeypatch.setattr(
            "tools.storage.r2_client.is_configured", lambda: True
        )
        monkeypatch.setattr(
            "tools.storage.r2_client.upload_file",
            lambda path, **kwargs: {
                "key": "openmontage/portrait-abc.jpg",
                "url": "https://pub-test.r2.dev/openmontage/portrait-abc.jpg",
            },
        )

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                return _FakeResponse({"Id": "asset-1"})
            return _FakeResponse({"Id": "asset-1", "Status": next(statuses)})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {"operation": "upload", "source": str(image), "poll_interval_seconds": 0}
        )

        assert result.success is True
        assert result.data["asset_ref"] == "asset://asset-1"
        create = next(kwargs for url, kwargs in calls if url.endswith("/CreateAsset"))
        assert "files" not in create, "multipart is not the default path"
        assert create["json"]["URL"] == "https://pub-test.r2.dev/openmontage/portrait-abc.jpg"
        assert create["json"]["model"] == "volc-asset"
        assert sum(1 for url, _ in calls if url.endswith("/GetAsset")) == 2

    def test_a_local_file_without_r2_explains_the_requirement(
        self, api_key, monkeypatch, tmp_path
    ):
        image = tmp_path / "portrait.jpg"
        image.write_bytes(b"fake-jpeg")
        monkeypatch.setattr("tools.storage.r2_client.is_configured", lambda: False)
        monkeypatch.setattr(
            "tools.storage.r2_client.missing_env", lambda: ["R2_BUCKET"]
        )
        monkeypatch.setattr(
            "requests.post", lambda url, **kwargs: _FakeResponse({"Id": "group-1"})
        )
        result = AnyFastAssets().execute({"operation": "upload", "source": str(image)})
        assert result.success is False
        assert "R2_BUCKET" in result.error

    def test_multipart_stays_available_behind_a_flag(self, api_key, monkeypatch, tmp_path):
        image = tmp_path / "portrait.jpg"
        image.write_bytes(b"fake-jpeg")
        calls: list[tuple[str, dict]] = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                return _FakeResponse({"Id": "asset-1"})
            return _FakeResponse({"Id": "asset-1", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": str(image),
                "allow_multipart": True,
                "poll_interval_seconds": 0,
            }
        )
        assert result.success is True
        create = next(kwargs for url, kwargs in calls if url.endswith("/CreateAsset"))
        assert "files" in create
        assert create["headers"].get("Content-Type") is None

    def test_names_get_a_unique_suffix(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                seen.update(kwargs)
                return _FakeResponse({"Id": "asset-1"})
            return _FakeResponse({"Id": "asset-1", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://example.com/a.png",
                "name": "juan-portrait-front",
            }
        )
        sent = seen["json"]["Name"]
        assert sent.startswith("juan-portrait-front-")
        assert sent != "juan-portrait-front", "a duplicate Name is reported as a 404"

    def test_video_upload_uses_the_video_billing_model(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                seen.update(kwargs)
                return _FakeResponse({"Id": "asset-2"})
            return _FakeResponse({"Id": "asset-2", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://pub-test.r2.dev/clip.mp4",
                "asset_type": "Video",
            }
        )
        assert result.success is True
        assert seen["json"]["model"] == "volc-asset-video"
        assert seen["json"]["AssetType"] == "Video"

    def test_url_source_is_sent_as_json(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                seen.update(kwargs)
                return _FakeResponse({"Id": "asset-3"})
            return _FakeResponse({"Id": "asset-3", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        AnyFastAssets().execute(
            {"operation": "upload", "source": "https://example.com/a.png", "name": "ref"}
        )
        assert seen["json"]["URL"] == "https://example.com/a.png"
        assert "files" not in seen

    def test_existing_group_is_reused(self, api_key, monkeypatch):
        urls: list[str] = []

        def fake_post(url, **kwargs):
            urls.append(url)
            if url.endswith("/ListAssetGroups"):
                return _FakeResponse(
                    {"Items": [{"Id": "group-existing", "GroupType": "AIGC"}], "TotalCount": 1}
                )
            if url.endswith("/CreateAsset"):
                return _FakeResponse({"Id": "asset-4"})
            return _FakeResponse({"Id": "asset-4", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://example.com/a.png",
                "group_id": "group-existing",
            }
        )
        assert result.success is True
        assert not any(u.endswith("/CreateAssetGroup") for u in urls)
        assert result.data["group_created"] is False
        assert result.data["group_type"] == "AIGC"

    def test_an_unlistable_group_still_uploads(self, api_key, monkeypatch):
        """Regression: a LivenessFace group never appears in ListAssetGroups.

        Blocking on that listing made every real-human upload impossible, so the
        preflight is advisory — CreateAsset is the authority on ownership.
        """
        urls: list[str] = []

        def fake_post(url, **kwargs):
            urls.append(url)
            if url.endswith("/ListAssetGroups"):
                return _FakeResponse({"Items": [], "TotalCount": 0})
            if url.endswith("/CreateAsset"):
                return _FakeResponse({"Id": "asset-live"})
            return _FakeResponse({"Id": "asset-live", "Status": "Active"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://example.com/a.png",
                "group_id": "group-from-liveness-result",
            }
        )
        assert result.success is True
        assert any(u.endswith("/CreateAsset") for u in urls)
        # An unlistable group is treated as real-human so the reads scope correctly.
        assert result.data["group_type"] == "LivenessFace"

    def test_a_foreign_group_is_rejected_by_create_asset(self, api_key, monkeypatch):
        """The upstream 404 is the ownership check, and it costs nothing."""

        def fake_post(url, **kwargs):
            if url.endswith("/ListAssetGroups"):
                return _FakeResponse({"Items": [], "TotalCount": 0})
            return _FakeResponse(
                {
                    "error": {
                        "message": (
                            "upstream_error: [NotFound.group_id] The specified asset_group "
                            "group-foreign is not found."
                        )
                    }
                },
                status_code=404,
            )

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://example.com/a.png",
                "group_id": "group-foreign",
            }
        )
        assert result.success is False
        assert "NotFound.group_id" in result.error

    def test_failed_asset_explains_face_mismatch(self, api_key, monkeypatch):
        def fake_post(url, **kwargs):
            if url.endswith("/CreateAssetGroup"):
                return _FakeResponse({"Id": "group-1"})
            if url.endswith("/CreateAsset"):
                return _FakeResponse({"Id": "asset-5"})
            return _FakeResponse(
                {
                    "Id": "asset-5",
                    "Status": "Failed",
                    "Error": {
                        "Code": "FaceMismatch",
                        "Message": "Face consistency verification failed.",
                    },
                }
            )

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {"operation": "upload", "source": "https://example.com/a.png"}
        )
        assert result.success is False
        assert "FaceMismatch" in result.error
        assert "same person" in result.error

    def test_a_liveness_group_is_never_auto_created(self, api_key, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a LivenessFace group cannot be created directly")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastAssets().execute(
            {
                "operation": "upload",
                "source": "https://example.com/a.png",
                "group_type": "LivenessFace",
            }
        )
        assert result.success is False
        assert "verification session" in result.error


class TestRealHumanVerification:
    def test_session_returns_the_h5_link_and_token(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse(
                {
                    "BytedToken": "2026070222152680501D985EA34A3BE3D8",
                    "H5Link": "https://www.byteplus.com/en/liveness-face-manage/authorization?pl=x",
                    "CallbackURL": "https://example.com/callback",
                }
            )

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {"operation": "create_liveness_session", "callback_url": "https://example.com/callback"}
        )

        assert result.success is True
        assert seen["url"].endswith("/volc/asset/CreateVisualValidateSession")
        assert seen["json"] == {"CallbackURL": "https://example.com/callback"}
        assert result.data["byted_token"] == "2026070222152680501D985EA34A3BE3D8"
        assert result.data["h5_link"].startswith("https://")
        assert "phone" in result.data["next_step"]

    def test_result_returns_the_liveness_group(self, api_key, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kwargs: _FakeResponse({"GroupId": "group-20260702221642-5czvq"}),
        )
        result = AnyFastAssets().execute(
            {"operation": "get_liveness_result", "byted_token": "token-1"}
        )
        assert result.data["verified"] is True
        assert result.data["group_id"] == "group-20260702221642-5czvq"
        assert result.data["group_type"] == "LivenessFace"

    def test_unfinished_verification_is_reported_not_faked(self, api_key, monkeypatch):
        monkeypatch.setattr(
            "requests.post", lambda url, **kwargs: _FakeResponse({"GroupId": ""})
        )
        result = AnyFastAssets().execute(
            {"operation": "get_liveness_result", "byted_token": "token-1"}
        )
        assert result.success is True
        assert result.data["verified"] is False
        assert "not finished" in result.data["next_step"]

    def test_result_requires_a_token(self, api_key):
        result = AnyFastAssets().execute({"operation": "get_liveness_result"})
        assert result.success is False
        assert "byted_token" in result.error

    def test_aigc_only_token_error_is_surfaced(self, api_key, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kwargs: _FakeResponse(
                {"error": {"message": "GroupType must be one of [AIGC]"}}, status_code=400
            ),
        )
        result = AnyFastAssets().execute(
            {"operation": "create_liveness_session", "callback_url": "https://example.com/cb"}
        )
        assert result.success is False
        assert "GroupType must be one of [AIGC]" in result.error


class TestManagement:
    def test_get_asset_reports_usability(self, api_key, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kwargs: _FakeResponse(
                {"Id": "asset-1", "Status": "Processing", "AssetType": "Video"}
            ),
        )
        result = AnyFastAssets().execute({"operation": "get_asset", "asset_id": "asset-1"})
        assert result.data["usable"] is False
        assert result.data["asset_ref"] == "asset://asset-1"

    def test_list_assets_builds_the_documented_filter(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse({"Assets": [], "Total": 0})

        monkeypatch.setattr("requests.post", fake_post)
        AnyFastAssets().execute(
            {
                "operation": "list_assets",
                "group_ids": ["group-1"],
                "group_type": "LivenessFace",
                "page_size": 50,
            }
        )
        assert seen["url"].endswith("/volc/asset/ListAssets")
        assert seen["json"]["Filter"] == {
            "GroupIds": ["group-1"],
            "GroupType": "LivenessFace",
        }
        assert seen["json"]["PageSize"] == 50

    def test_update_asset_requires_something_to_change(self, api_key, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("an empty update must not be sent")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastAssets().execute({"operation": "update_asset", "asset_id": "asset-1"})
        assert result.success is False
        assert "new name" in result.error

    def test_update_asset_moves_it_between_groups(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse({"Id": "asset-1"})

        monkeypatch.setattr("requests.post", fake_post)
        AnyFastAssets().execute(
            {
                "operation": "update_asset",
                "asset_id": "asset-1",
                "name": "renamed",
                "group_id": "group-2",
            }
        )
        assert seen["url"].endswith("/volc/asset/UpdateAsset")
        assert seen["json"] == {
            "model": "volc-asset",
            "Id": "asset-1",
            "Name": "renamed",
            "GroupId": "group-2",
        }

    @pytest.mark.parametrize(
        ("operation", "field"),
        [("delete_asset", "asset_id"), ("delete_group", "group_id")],
    )
    def test_deletes_require_explicit_confirmation(
        self, api_key, monkeypatch, operation, field
    ):
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("an unconfirmed delete must not be sent")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastAssets().execute({"operation": operation, field: "id-1"})
        assert result.success is False
        assert "confirm=true" in result.error

    def test_confirmed_delete_calls_the_endpoint(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse({"Id": "asset-1", "Deleted": True})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {"operation": "delete_asset", "asset_id": "asset-1", "confirm": True}
        )
        assert result.success is True
        assert seen["url"].endswith("/volc/asset/DeleteAsset")

    def test_create_group_returns_the_group_id(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse({"Id": "group-9"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute({"operation": "create_group", "name": "brand-refs"})
        assert result.data["group_id"] == "group-9"
        assert seen["json"] == {"model": "volc-asset", "Name": "brand-refs"}

    def test_create_asset_without_group_is_rejected(self, api_key):
        result = AnyFastAssets().execute(
            {"operation": "create_asset", "source": "https://example.com/a.png"}
        )
        assert result.success is False
        assert "group_id" in result.error


class TestDryRun:
    def test_dry_run_never_calls_the_api(self, api_key, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("dry_run attempted a network call")

        monkeypatch.setattr("requests.post", explode)
        report = AnyFastAssets().dry_run(
            {"operation": "upload", "source": "/tmp/clip.mp4", "asset_type": "Video"}
        )
        assert report["valid"] is True
        assert report["would_execute"] is False
        assert report["upload_mode"] == "multipart_file"
        assert report["billing_model"] == "volc-asset-video"
        assert report["endpoint"].endswith("/volc/asset/CreateAsset")

    def test_dry_run_marks_destructive_operations(self, api_key):
        report = AnyFastAssets().dry_run(
            {"operation": "delete_group", "group_id": "group-1", "confirm": True}
        )
        assert report["destructive"] is True
        assert report["valid"] is True

    def test_dry_run_carries_the_authorization_note(self, api_key):
        report = AnyFastAssets().dry_run(
            {"operation": "create_liveness_session", "callback_url": "https://example.com/cb"}
        )
        assert "authorization" in report["authorization_note"].lower()


class TestRegistry:
    def test_registry_discovers_the_tool(self, api_key):
        from tools.tool_registry import registry

        registry.ensure_discovered()
        names = [tool.name for tool in registry.get_by_capability("media_asset_management")]
        assert "anyfast_assets" in names

    def test_error_text_redacts_the_key(self, api_key):
        message = _anyfast.safe_error(RuntimeError("failed with fake-anyfast-key"), api_key)
        assert "fake-anyfast-key" not in message


class TestLivenessAssetReads:
    """Regression: a LivenessFace asset 404s on GetAsset and needs ListAssets."""

    def test_get_asset_falls_back_to_list_for_a_liveness_asset(self, api_key, monkeypatch):
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/GetAsset"):
                return _FakeResponse(
                    {
                        "error": {
                            "message": (
                                "upstream_error: [NotFound.asset_id] The specified asset "
                                "asset-9 is not found."
                            )
                        }
                    },
                    status_code=404,
                )
            return _FakeResponse(
                {
                    "Items": [
                        {"Id": "asset-9", "Status": "Active", "GroupId": "group-live"}
                    ],
                    "TotalCount": 1,
                }
            )

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastAssets().execute(
            {
                "operation": "get_asset",
                "asset_id": "asset-9",
                "group_id": "group-live",
                "group_type": "LivenessFace",
            }
        )
        assert result.success is True
        assert result.data["usable"] is True
        assert result.data["read_via"] == "ListAssets"
        assert any(url.endswith("/ListAssets") for url in calls)

    def test_list_fallback_scopes_by_group_and_type(self, api_key, monkeypatch):
        seen: dict = {}

        def fake_post(url, **kwargs):
            if url.endswith("/GetAsset"):
                return _FakeResponse(
                    {"error": {"message": "[NotFound.asset_id] not found"}}, status_code=404
                )
            seen.update(kwargs)
            return _FakeResponse({"Items": [{"Id": "asset-9", "Status": "Active"}], "TotalCount": 1})

        monkeypatch.setattr("requests.post", fake_post)
        _anyfast.read_asset(
            api_key, "asset-9", group_id="group-live", group_type="LivenessFace"
        )
        assert seen["json"]["Filter"] == {
            "GroupIds": ["group-live"],
            "GroupType": "LivenessFace",
        }

    def test_get_asset_without_a_group_explains_the_fallback(self, api_key, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kwargs: _FakeResponse(
                {"error": {"message": "[NotFound.asset_id] not found"}}, status_code=404
            ),
        )
        result = AnyFastAssets().execute({"operation": "get_asset", "asset_id": "asset-9"})
        assert result.success is False
        assert "NotFound" in result.error or "not found" in result.error

    def test_persistent_not_found_stops_instead_of_hammering(self, api_key, monkeypatch):
        """The old loop retried a 404 for the whole timeout and spammed the account log."""
        calls = {"count": 0}

        def fake_post(url, **kwargs):
            calls["count"] += 1
            if url.endswith("/GetAsset"):
                return _FakeResponse(
                    {"error": {"message": "[NotFound.asset_id] not found"}}, status_code=404
                )
            return _FakeResponse({"Items": [], "TotalCount": 0})

        monkeypatch.setattr("requests.post", fake_post)
        with pytest.raises(RuntimeError, match="Face consistency verification failed"):
            _anyfast.poll_asset(
                api_key,
                "asset-9",
                interval=0,
                timeout=300,
                group_id="group-live",
                not_found_grace_seconds=0,
            )
        assert calls["count"] <= 4, "a persistent 404 must not be retried for the whole timeout"


class TestLivenessSessionContract:
    def test_callback_url_is_required_by_the_api(self, api_key, monkeypatch):
        """The published schema marks the body optional; the API rejects an empty one."""

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a session must not be attempted without CallbackURL")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastAssets().execute({"operation": "create_liveness_session"})
        assert result.success is False
        assert "callback_url" in result.error

    def test_helper_explains_the_schema_discrepancy(self, api_key):
        with pytest.raises(ValueError, match="marks it optional"):
            _anyfast.create_liveness_session(api_key)

    def test_callback_url_must_be_http(self, api_key):
        with pytest.raises(ValueError, match="http"):
            _anyfast.create_liveness_session(api_key, "not-a-url")


class TestErrorSurfacing:
    """Regression: a 400 whose reason is nested inside `message` was swallowed."""

    def test_nested_upstream_error_is_extracted(self):
        payload = {
            "code": "fail_to_fetch_task",
            "message": (
                '{"error":{"code":"InvalidParameter","message":"The specified asset '
                'asset-1 is not found","type":"BadRequest"}}'
            ),
            "data": None,
        }
        detail = _anyfast._error_detail(payload)
        assert "The specified asset asset-1 is not found" in detail
        assert "fail_to_fetch_task" in detail

    def test_plain_gateway_error_still_works(self):
        detail = _anyfast._error_detail(
            {"error": {"type": "new_api_error", "message": "Invalid token"}}
        )
        assert "Invalid token" in detail

    def test_unrecognised_shape_falls_back_to_the_body(self):
        class _Resp:
            status_code = 400
            text = "<html>gateway exploded</html>"

            def json(self):
                raise ValueError("not json")

            def raise_for_status(self):
                raise RuntimeError("400 Client Error")

        with pytest.raises(RuntimeError, match="gateway exploded"):
            _anyfast.raise_for_status(_Resp())

