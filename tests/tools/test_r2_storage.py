"""Contract tests for Cloudflare R2 media hosting.

All HTTP is mocked; nothing here touches a real bucket.
"""

from __future__ import annotations

import hashlib

import pytest

from tools.base_tool import BaseTool, ToolRuntime, ToolStatus
from tools.storage import r2_client
from tools.storage.r2_storage import R2Storage


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture()
def r2_env(monkeypatch):
    values = {
        "R2_ACCOUNT_ID": "acct123",
        "R2_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "R2_SECRET_ACCESS_KEY": "secret-key-value",
        "R2_BUCKET": "openmontage-media",
        "R2_PUBLIC_BASE_URL": "https://pub-abc123.r2.dev",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("R2_KEY_PREFIX", raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    return values


class TestContract:
    def test_identity(self, r2_env):
        assert issubclass(R2Storage, BaseTool)
        tool = R2Storage()
        assert tool.name == "r2_storage"
        assert tool.capability == "media_hosting"
        assert tool.provider == "cloudflare_r2"
        assert tool.runtime == ToolRuntime.API
        assert tool.supports["private_storage"] is False

    def test_every_env_var_is_declared_as_a_dependency(self):
        declared = {dep.removeprefix("env:") for dep in R2Storage.dependencies}
        assert declared == set(r2_client.REQUIRED_ENV)

    def test_status_needs_the_complete_configuration(self, r2_env, monkeypatch):
        assert R2Storage().get_status() == ToolStatus.AVAILABLE
        monkeypatch.delenv("R2_PUBLIC_BASE_URL")
        assert R2Storage().get_status() == ToolStatus.UNAVAILABLE
        assert r2_client.missing_env() == ["R2_PUBLIC_BASE_URL"]

    def test_public_base_url_must_be_https(self, r2_env, monkeypatch):
        monkeypatch.setenv("R2_PUBLIC_BASE_URL", "http://pub-abc123.r2.dev")
        with pytest.raises(r2_client.R2ConfigError, match="https"):
            r2_client.get_config()


class TestKeys:
    def test_keys_are_prefixed_foldered_and_unique(self, r2_env):
        key = r2_client.build_key("PXL_20260825 Photo.RAW-01.jpg", folder="juanda")
        assert key.startswith("openmontage/juanda/")
        assert key.endswith(".jpg")
        assert " " not in key and key.islower()

    def test_uniqueness_can_be_disabled(self, r2_env):
        key = r2_client.build_key("portrait.jpg", folder="juanda", unique=False)
        assert key == "openmontage/juanda/portrait.jpg"

    def test_two_uploads_of_one_filename_do_not_collide(self, r2_env):
        first = r2_client.build_key("portrait.jpg")
        second = r2_client.build_key("portrait.jpg")
        assert first != second

    def test_prefix_override(self, r2_env, monkeypatch):
        monkeypatch.setenv("R2_KEY_PREFIX", "staging")
        assert r2_client.build_key("a.png", unique=False) == "staging/a.png"


class TestSigning:
    def test_authorization_header_shape(self, r2_env):
        url, headers = r2_client.signed_headers(
            "PUT",
            config=r2_client.get_config(),
            key="openmontage/a.png",
            payload=b"bytes",
            content_type="image/png",
        )
        assert url == (
            "https://acct123.r2.cloudflarestorage.com/openmontage-media/openmontage/a.png"
        )
        auth = headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
        assert "/auto/s3/aws4_request" in auth
        assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date" in auth
        assert headers["x-amz-content-sha256"] == hashlib.sha256(b"bytes").hexdigest()

    def test_signature_covers_the_payload(self, r2_env):
        config = r2_client.get_config()
        _, one = r2_client.signed_headers("PUT", config=config, key="k", payload=b"a")
        _, two = r2_client.signed_headers("PUT", config=config, key="k", payload=b"b")
        assert one["Authorization"] != two["Authorization"]

    def test_secret_never_appears_in_the_headers(self, r2_env):
        _, headers = r2_client.signed_headers(
            "PUT", config=r2_client.get_config(), key="k", payload=b"x"
        )
        assert "secret-key-value" not in str(headers)


class TestUpload:
    def test_upload_puts_the_bytes_and_returns_a_public_url(self, r2_env, monkeypatch, tmp_path):
        source = tmp_path / "portrait.jpg"
        source.write_bytes(b"jpeg-bytes")
        seen: dict = {}

        def fake_put(url, **kwargs):
            seen.update(url=url, **kwargs)
            return _FakeResponse(200)

        monkeypatch.setattr("requests.put", fake_put)
        monkeypatch.setattr("requests.head", lambda url, **kwargs: _FakeResponse(200))

        result = r2_client.upload_file(source, folder="juanda")
        assert result["url"].startswith("https://pub-abc123.r2.dev/openmontage/juanda/portrait-")
        assert result["public_verified"] is True
        assert result["content_type"] == "image/jpeg"
        assert seen["data"] == b"jpeg-bytes"
        assert seen["url"].startswith("https://acct123.r2.cloudflarestorage.com/")

    def test_a_private_bucket_fails_loudly(self, r2_env, monkeypatch, tmp_path):
        source = tmp_path / "portrait.jpg"
        source.write_bytes(b"jpeg-bytes")
        monkeypatch.setattr("requests.put", lambda url, **kwargs: _FakeResponse(200))
        monkeypatch.setattr("requests.head", lambda url, **kwargs: _FakeResponse(403))

        with pytest.raises(RuntimeError, match="Public Development URL"):
            r2_client.upload_file(source)

    def test_upload_error_is_surfaced(self, r2_env, monkeypatch, tmp_path):
        source = tmp_path / "portrait.jpg"
        source.write_bytes(b"x")
        monkeypatch.setattr(
            "requests.put", lambda url, **kwargs: _FakeResponse(403, "AccessDenied")
        )
        with pytest.raises(RuntimeError, match="AccessDenied"):
            r2_client.upload_file(source)

    def test_missing_file_is_rejected(self, r2_env):
        with pytest.raises(FileNotFoundError):
            r2_client.upload_file("/tmp/definitely-not-here-9df2.png")


class TestToolSurface:
    def test_upload_many(self, r2_env, monkeypatch, tmp_path):
        files = []
        for name in ("a.png", "b.png"):
            path = tmp_path / name
            path.write_bytes(b"x")
            files.append(str(path))
        monkeypatch.setattr("requests.put", lambda url, **kwargs: _FakeResponse(200))
        monkeypatch.setattr("requests.head", lambda url, **kwargs: _FakeResponse(200))

        result = R2Storage().execute({"operation": "upload_many", "paths": files})
        assert result.success is True
        assert len(result.data["urls"]) == 2

    def test_dry_run_plans_keys_without_uploading(self, r2_env, monkeypatch, tmp_path):
        source = tmp_path / "portrait.jpg"
        source.write_bytes(b"x")

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("dry_run must not touch the network")

        monkeypatch.setattr("requests.put", explode)
        report = R2Storage().dry_run({"operation": "upload", "path": str(source)})
        assert report["valid"] is True
        assert report["would_execute"] is False
        assert report["planned_urls"][0].startswith("https://pub-abc123.r2.dev/")

    def test_unconfigured_dry_run_names_the_missing_variables(self, monkeypatch):
        for name in r2_client.REQUIRED_ENV:
            monkeypatch.delenv(name, raising=False)
        report = R2Storage().dry_run({"operation": "upload", "path": "/tmp/x.png"})
        assert report["status"] == "unavailable"
        assert set(report["missing_env"]) == set(r2_client.REQUIRED_ENV)

    def test_delete_and_exists(self, r2_env, monkeypatch):
        monkeypatch.setattr("requests.delete", lambda url, **kwargs: _FakeResponse(204))
        monkeypatch.setattr("requests.head", lambda url, **kwargs: _FakeResponse(200))
        tool = R2Storage()
        assert tool.execute({"operation": "delete", "key": "openmontage/a.png"}).success
        assert tool.execute({"operation": "exists", "key": "openmontage/a.png"}).data["exists"]

    def test_operations_require_their_inputs(self, r2_env):
        tool = R2Storage()
        assert "requires path" in tool.execute({"operation": "upload"}).error
        assert "requires key" in tool.execute({"operation": "delete"}).error

    def test_registry_discovers_the_tool(self, r2_env):
        from tools.tool_registry import registry

        registry.ensure_discovered()
        names = [t.name for t in registry.get_by_capability("media_hosting")]
        assert "r2_storage" in names
