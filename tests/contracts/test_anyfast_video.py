"""Contract tests for AnyFast gateway video generation.

All HTTP calls are mocked. This suite must never create a paid task.
"""

from __future__ import annotations

import pytest

from tools.base_tool import BaseTool, ExecutionMode, ToolRuntime, ToolStatus
from tools.video.anyfast_video import AnyFastVideo


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._payload = payload if payload is not None else {}
        self.content = content
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self._payload}")


def _succeeded_task(**overrides: object) -> dict:
    task = {
        "task_id": "asyntask_abc123",
        "action": "omniGenerate",
        "status": "SUCCESS",
        "result_url": "https://cdn.example.com/generated-video.mp4",
        "progress": "100%",
        "request_id": "req-1",
        "data": {
            "content": {"video_url": "https://cdn.example.com/generated-video.mp4"},
            "duration": 8,
            "generate_audio": True,
            "ratio": "16:9",
            "resolution": "720p",
            "output_format": "mp4",
            "status": "succeeded",
            "seed": 42,
            "model": "dreamina-seedance-2-5-260628",
        },
    }
    task.update(overrides)
    return {"code": "success", "message": "", "data": task}


class TestContract:
    def test_identity_and_capabilities(self):
        assert issubclass(AnyFastVideo, BaseTool)
        tool = AnyFastVideo()
        assert tool.name == "anyfast_video"
        assert tool.provider == "anyfast"
        assert tool.capability == "video_generation"
        assert tool.runtime == ToolRuntime.API
        assert tool.execution_mode == ExecutionMode.ASYNC
        assert "env:ANYFAST_API_KEY" in tool.dependencies
        for operation in ("text_to_video", "image_to_video", "reference_to_video"):
            assert tool.supports[operation] is True
        assert tool.supports["local_video"] is False

    def test_status_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("ANYFAST_API_KEY", raising=False)
        assert AnyFastVideo().get_status() == ToolStatus.UNAVAILABLE
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        assert AnyFastVideo().get_status() == ToolStatus.AVAILABLE

    def test_status_rejects_a_bearer_prefixed_key(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "Bearer fake-anyfast-key")
        assert AnyFastVideo().get_status() == ToolStatus.UNAVAILABLE

    def test_documented_model_ids(self):
        assert set(AnyFastVideo.MODELS) == {
            "seedance-2.5",
            "seedance-2.0",
            "seedance-fast",
            "seedance-2.0-mini",
            "seedance-2.0-ultra",
        }
        # Seedance 2.0 Fast is published as `seedance-fast`, not `seedance-2.0-fast`.
        assert AnyFastVideo.MODEL_ALIASES["seedance-2.0-fast"] == "seedance-fast"
        assert AnyFastVideo.MODEL_ALIASES["2.5"] == "seedance-2.5"

    def test_registry_catalog_exposes_model_envelopes(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        catalog = AnyFastVideo().get_info()["model_catalog"]
        assert catalog["seedance-2.5"]["max_duration_seconds"] == 30
        assert catalog["seedance-2.5"]["automatic_duration"] is True
        assert catalog["seedance-2.0"]["resolutions"] == ["480p", "720p", "1080p", "4k"]
        assert catalog["seedance-2.0-ultra"]["resolutions"] == ["720p", "1080p", "2k"]


class TestPayload:
    def test_text_to_video_matches_the_documented_body(self):
        payload, warnings = AnyFastVideo()._build_payload(
            {
                "prompt": (
                    "A 20-second cinematic tracking shot through a miniature "
                    "steampunk city at golden hour"
                ),
                "generate_audio": True,
                "resolution": "1080p",
                "aspect_ratio": "16:9",
                "duration": 20,
                "output_format": "mp4",
            }
        )
        assert warnings == []
        assert payload == {
            "model": "seedance-2.5",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "A 20-second cinematic tracking shot through a miniature "
                        "steampunk city at golden hour"
                    ),
                }
            ],
            "generate_audio": True,
            "resolution": "1080p",
            "ratio": "16:9",
            "duration": 20,
            "output_format": "mp4",
            "watermark": False,
            "return_last_frame": False,
        }

    def test_first_and_last_frame_roles(self):
        payload, warnings = AnyFastVideo()._build_payload(
            {
                "prompt": "The subject smiles while the camera orbits",
                "operation": "image_to_video",
                "reference_image_url": "https://example.com/first.jpg",
                "last_image_url": "https://example.com/last.jpg",
                "duration": 5,
            }
        )
        roles = [item.get("role") for item in payload["content"]]
        assert roles == [None, "first_frame", "last_frame"]
        # Seedance 2.5 frame-guided generation only supports adaptive.
        assert payload["ratio"] == "adaptive"
        assert any("adaptive" in warning for warning in warnings)

    def test_multimodal_reference_sets_roles_and_task_type(self):
        payload, _ = AnyFastVideo()._build_payload(
            {
                "prompt": "@image1 performs the motion from @video1, paced to @audio1",
                "operation": "reference_to_video",
                "reference_image_urls": ["https://example.com/character.jpg"],
                "reference_video_urls": ["https://example.com/motion.mp4"],
                "reference_audio_urls": ["https://example.com/music.mp3"],
                "duration": 15,
            }
        )
        assert payload["omni_reference_task_type"] == "reference"
        assert payload["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/character.jpg"},
            "role": "reference_image",
        }
        assert payload["content"][2] == {
            "type": "video_url",
            "video_url": {"url": "https://example.com/motion.mp4"},
            "role": "reference_video",
        }
        assert payload["content"][3] == {
            "type": "audio_url",
            "audio_url": {"url": "https://example.com/music.mp3"},
            "role": "reference_audio",
        }

    def test_video_edit_forces_adaptive_ratio_and_auto_duration(self):
        payload, warnings = AnyFastVideo()._build_payload(
            {
                "prompt": "Edit @video1: remove every background pedestrian",
                "operation": "video_edit",
                "video_url": "https://example.com/source.mov",
                "aspect_ratio": "16:9",
                "duration": 10,
                "output_format": "mov",
            }
        )
        assert payload["omni_reference_task_type"] == "edit"
        assert payload["ratio"] == "adaptive"
        assert payload["duration"] == -1
        assert len(warnings) == 2

    def test_web_search_is_text_only(self):
        payload, _ = AnyFastVideo()._build_payload(
            {"prompt": "Today's biggest sports moment, recreated", "web_search": True}
        )
        assert payload["tools"] == [{"type": "web_search"}]
        with pytest.raises(ValueError, match="web_search"):
            AnyFastVideo()._build_payload(
                {
                    "prompt": "animate this",
                    "operation": "image_to_video",
                    "reference_image_url": "https://example.com/a.jpg",
                    "web_search": True,
                }
            )

    def test_local_video_reference_is_rejected(self):
        with pytest.raises(ValueError, match="public URL or asset"):
            AnyFastVideo()._build_payload(
                {
                    "prompt": "extend this",
                    "operation": "video_extend",
                    "reference_video_path": "/tmp/source.mp4",
                }
            )

    def test_text_to_video_rejects_reference_media(self):
        with pytest.raises(ValueError, match="does not accept reference media"):
            AnyFastVideo()._build_payload(
                {"prompt": "a cat", "reference_image_url": "https://example.com/cat.jpg"}
            )

    @pytest.mark.parametrize(
        ("inputs", "message"),
        [
            ({"prompt": "x", "model": "seedance-fast", "resolution": "1080p"}, "supports resolution"),
            ({"prompt": "x", "model": "seedance-2.0-ultra"}, "resolution is required"),
            ({"prompt": "x", "model": "seedance-2.0", "duration": "auto"}, "duration must be"),
            ({"prompt": "x", "duration": 40}, "duration must be"),
            ({"prompt": "x", "model": "nope-1.0"}, "unsupported AnyFast video model"),
            ({"prompt": "x", "output_format": "webm"}, "output_format"),
        ],
    )
    def test_envelope_violations_fail_before_submission(self, inputs, message):
        with pytest.raises(ValueError, match=message):
            AnyFastVideo()._build_payload(inputs)

    def test_seedance_25_accepts_thirty_second_auto_duration(self):
        payload, _ = AnyFastVideo()._build_payload({"prompt": "x", "duration": "auto"})
        assert payload["duration"] == -1
        payload, _ = AnyFastVideo()._build_payload({"prompt": "x", "duration": 30})
        assert payload["duration"] == 30

    def test_env_default_model(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_VIDEO_MODEL", "seedance-2.0-mini")
        payload, _ = AnyFastVideo()._build_payload({"prompt": "x"})
        assert payload["model"] == "seedance-2.0-mini"


class TestCostReporting:
    def test_unknown_pricing_is_reported_as_unknown_not_free(self, monkeypatch):
        monkeypatch.delenv("ANYFAST_VIDEO_PRICE_USD", raising=False)
        monkeypatch.delenv("ANYFAST_VIDEO_PRICE_USD_PER_SECOND", raising=False)
        tool = AnyFastVideo()
        inputs = {"prompt": "x", "duration": 10}
        assert tool.estimate_cost(inputs) == 0.0
        assert tool.cost_estimate_status(inputs) == "unknown_gateway_pricing"
        assert tool.dry_run(inputs)["cost_estimate_status"] == "unknown_gateway_pricing"

    def test_per_second_override(self, monkeypatch):
        monkeypatch.delenv("ANYFAST_VIDEO_PRICE_USD", raising=False)
        monkeypatch.setenv("ANYFAST_VIDEO_PRICE_USD_PER_SECOND", "0.05")
        tool = AnyFastVideo()
        assert tool.estimate_cost({"prompt": "x", "duration": 10}) == pytest.approx(0.5)
        # An automatic duration prices against the model maximum.
        assert tool.estimate_cost({"prompt": "x", "duration": "auto"}) == pytest.approx(1.5)
        assert tool.cost_estimate_status({"prompt": "x"}) == "configured_per_second_price"

    def test_flat_price_input_wins(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_VIDEO_PRICE_USD_PER_SECOND", "0.05")
        tool = AnyFastVideo()
        inputs = {"prompt": "x", "duration": 10, "price_usd": 0.9}
        assert tool.estimate_cost(inputs) == pytest.approx(0.9)
        assert tool.cost_estimate_status(inputs) == "configured_flat_price"

    def test_malformed_price_fails_before_the_paid_post(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        monkeypatch.setenv("ANYFAST_VIDEO_PRICE_USD", "free")

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a paid POST was attempted")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastVideo().execute({"prompt": "x", "duration": 5})
        assert result.success is False
        assert "ANYFAST_VIDEO_PRICE_USD" in result.error


class TestTaskActions:
    def test_create_uses_the_documented_endpoint_and_bearer_auth(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        captured = {}

        def fake_post(url, *, headers, json, timeout):
            captured.update(url=url, headers=headers, json=json)
            return _FakeResponse({"id": "asyntask_abc123", "status": "queued"})

        monkeypatch.setattr("requests.post", fake_post)
        result = AnyFastVideo().execute(
            {"task_action": "create", "prompt": "A paper bird takes flight", "duration": 5}
        )

        assert result.success is True
        assert result.data["task_id"] == "asyntask_abc123"
        assert result.data["status"] == "submitted"
        assert captured["url"] == "https://www.anyfast.ai/v1/video/generations"
        assert captured["headers"]["Authorization"] == "Bearer fake-anyfast-key"
        assert captured["headers"]["Content-Type"] == "application/json"
        assert captured["json"]["model"] == "seedance-2.5"

    def test_query_unwraps_the_code_message_data_envelope(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        captured = {}

        def fake_get(url, *, headers, timeout):
            captured.update(url=url, headers=headers)
            return _FakeResponse(_succeeded_task())

        monkeypatch.setattr("requests.get", fake_get)
        result = AnyFastVideo().execute(
            {"task_action": "query", "task_id": "asyntask_abc123"}
        )

        assert result.success is True
        assert captured["url"] == (
            "https://www.anyfast.ai/v1/video/generations/asyntask_abc123"
        )
        assert result.data["status"] == "SUCCESS"
        assert result.data["video_url"] == "https://cdn.example.com/generated-video.mp4"

    def test_query_rejects_a_missing_task_id(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        result = AnyFastVideo().execute({"task_action": "query"})
        assert result.success is False
        assert "task_id" in result.error

    def test_generate_polls_until_success_then_downloads(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        monkeypatch.setenv("ANYFAST_VIDEO_PRICE_USD_PER_SECOND", "0.05")
        polls = {"count": 0}

        monkeypatch.setattr(
            "requests.post",
            lambda url, **kwargs: _FakeResponse({"id": "asyntask_abc123"}),
        )

        def fake_get(url, **kwargs):
            if url.endswith(".mp4"):
                return _FakeResponse(content=b"fake-mp4-bytes")
            polls["count"] += 1
            if polls["count"] == 1:
                return _FakeResponse(
                    {"code": "success", "data": {"status": "IN_PROGRESS", "progress": "40%"}}
                )
            return _FakeResponse(_succeeded_task())

        monkeypatch.setattr("requests.get", fake_get)
        output = tmp_path / "clip.mp4"
        result = AnyFastVideo().execute(
            {
                "prompt": "A neon skyline timelapse",
                "duration": 8,
                "resolution": "720p",
                "poll_interval_seconds": 0,
                "output_path": str(output),
            }
        )

        assert result.success is True
        assert polls["count"] == 2
        assert output.read_bytes() == b"fake-mp4-bytes"
        assert result.artifacts == [str(output)]
        assert result.cost_usd == pytest.approx(0.4)
        assert result.data["upstream_model"] == "dreamina-seedance-2-5-260628"
        assert result.data["video_url"] == "https://cdn.example.com/generated-video.mp4"
        assert result.seed == 42
        assert not (tmp_path / "clip.mp4.part").exists()

    def test_failure_surfaces_fail_reason_without_downloading(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        monkeypatch.setattr(
            "requests.post", lambda url, **kwargs: _FakeResponse({"id": "asyntask_abc123"})
        )
        monkeypatch.setattr(
            "requests.get",
            lambda url, **kwargs: _FakeResponse(
                {
                    "code": "success",
                    "data": {"status": "FAILURE", "fail_reason": "content policy"},
                }
            ),
        )
        result = AnyFastVideo().execute(
            {
                "prompt": "x",
                "poll_interval_seconds": 0,
                "output_path": str(tmp_path / "clip.mp4"),
            }
        )
        assert result.success is False
        assert "content policy" in result.error
        assert result.cost_usd == 0.0
        assert not (tmp_path / "clip.mp4").exists()

    def test_a_submitted_task_id_survives_a_download_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        monkeypatch.setattr(
            "requests.post", lambda url, **kwargs: _FakeResponse({"id": "asyntask_abc123"})
        )

        def fake_get(url, **kwargs):
            if url.endswith(".mp4"):
                raise RuntimeError("connection reset")
            return _FakeResponse(_succeeded_task())

        monkeypatch.setattr("requests.get", fake_get)
        result = AnyFastVideo().execute(
            {
                "prompt": "x",
                "poll_interval_seconds": 0,
                "output_path": str(tmp_path / "clip.mp4"),
            }
        )
        assert result.success is False
        assert result.data["task_id"] == "asyntask_abc123"
        assert result.data["recovery_action"] == "query"

    def test_missing_key_never_calls_the_api(self, monkeypatch):
        monkeypatch.delenv("ANYFAST_API_KEY", raising=False)

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a paid POST was attempted")

        monkeypatch.setattr("requests.post", explode)
        result = AnyFastVideo().execute({"prompt": "x"})
        assert result.success is False
        assert "ANYFAST_API_KEY" in result.error


class TestErrorRedaction:
    def test_api_key_and_signed_urls_are_redacted(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "sk-super-secret")
        tool = AnyFastVideo()
        message = tool._safe_error(
            RuntimeError(
                "Authorization: Bearer sk-super-secret failed for "
                "https://cdn.example.com/v.mp4?X-Signature=abc"
            )
        )
        assert "sk-super-secret" not in message
        assert "X-Signature" not in message

    def test_data_uri_payloads_are_redacted(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "sk-super-secret")
        message = AnyFastVideo()._safe_error(
            RuntimeError("bad request for data:image/png;base64,AAAABBBBCCCC")
        )
        assert "AAAABBBBCCCC" not in message
        assert "[redacted data URI]" in message


class TestDryRun:
    def test_dry_run_never_submits_and_reports_the_contract(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("dry_run attempted a network call")

        monkeypatch.setattr("requests.post", explode)
        monkeypatch.setattr("requests.get", explode)

        report = AnyFastVideo().dry_run({"prompt": "x", "duration": 6})
        assert report["valid"] is True
        assert report["paid_submission"] is False
        assert report["would_execute"] is False
        assert report["api_contract"]["create"] == (
            "POST https://www.anyfast.ai/v1/video/generations"
        )
        assert report["api_contract"]["query"] == (
            "GET https://www.anyfast.ai/v1/video/generations/{task_id}"
        )
        assert report["media_counts"] == {
            "text": 1,
            "image_url": 0,
            "video_url": 0,
            "audio_url": 0,
        }

    def test_dry_run_reports_invalid_inputs(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        report = AnyFastVideo().dry_run({"prompt": "x", "duration": 99})
        assert report["valid"] is False
        assert "duration" in report["error"]


class TestSelectorIntegration:
    def test_registry_discovers_the_provider(self, monkeypatch):
        monkeypatch.setenv("ANYFAST_API_KEY", "fake-anyfast-key")
        from tools.tool_registry import registry

        registry.ensure_discovered()
        names = [tool.name for tool in registry.get_by_capability("video_generation")]
        assert "anyfast_video" in names

    def test_selector_passthrough_keys_are_accepted(self):
        """video_selector forwards duration as a string and uses last_image_url."""
        payload, _ = AnyFastVideo()._build_payload(
            {
                "prompt": "orbit the subject",
                "operation": "image_to_video",
                "duration": "5",
                "aspect_ratio": "9:16",
                "reference_image_url": "https://example.com/first.jpg",
                "last_image_url": "https://example.com/last.jpg",
            }
        )
        assert payload["duration"] == 5
        assert [item.get("role") for item in payload["content"]] == [
            None,
            "first_frame",
            "last_frame",
        ]

    def test_operation_availability(self):
        tool = AnyFastVideo()
        assert tool.is_operation_available("text_to_video") is True
        assert tool.is_operation_available("video_edit") is True
        assert tool.is_operation_available("stock_video") is False
