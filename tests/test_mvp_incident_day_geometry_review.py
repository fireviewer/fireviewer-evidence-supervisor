from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, cast

import pytest
from pydantic import SecretStr

from fireviewer_evidence_ingestion.mvp.research.multimodal_evidence import BedrockConverseClient
from fireviewer_contracts.mvp.supervision.backend_event_evidence import (
    AzureBackendEventEvidenceConfig,
    AzureBackendIncidentDayGeometryReviewAdapter,
    BackendEventEvidenceSnapshot,
    BackendIncidentDayGeometryReviewContext,
    BackendJsonResponse,
    EventEvidenceRepository,
)
from fireviewer_evidence_supervisor.mvp.supervision.durable_endpoint import (
    DurablePointSupervisionService,
)
from fireviewer_evidence_supervisor.mvp.supervision.incident_day_geometry_review import (
    BedrockIncidentDayGeometryReviewer,
    BedrockIncidentDayGeometryReviewerConfig,
    IncidentDayGeometryReviewerError,
    SimulatedIncidentDayGeometryReviewer,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_supervisor_cpu_service import (
    PointSupervisorCpuSettings,
)


def _hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _context_payload() -> dict[str, Any]:
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [5.36, 44.74],
                [5.38, 44.74],
                [5.38, 44.76],
                [5.36, 44.76],
                [5.36, 44.74],
            ]
        ],
    }
    payload: dict[str, Any] = {
        "schema": "fireviewer.incident-day-geometry-review-context.v1",
        "analysis_id": "AN-DIE-GEOMETRY-20260708",
        "fire_id": "FR-26-00001",
        "episode_id": "EP-DIE-2026",
        "local_date": "2026-07-08",
        "deterministic_perimeter": {
            "status": "fused",
            "receipt_sha256": "b" * 64,
            "published_reference_accessed": False,
        },
        "source_perimeter_sha256": "b" * 64,
        "candidate_geometries": [
            {
                "candidate_id": "CLAIM-S2-BURNED-1",
                "geometry_geojson": geometry,
                "assertion_kind": "burned_area",
                "provider_key": "copernicus-data-space",
                "processor": "sentinel2_nbr_change_v1",
                "eligible_for_competing_geometry": True,
            },
            {
                "candidate_id": "CLAIM-FIRMS-1",
                "geometry_geojson": geometry,
                "assertion_kind": "thermal_footprint",
                "provider_key": "nasa-firms",
                "processor": "firms_viirs_thermal_footprint_v1",
                "eligible_for_competing_geometry": False,
            },
        ],
        "evidence_summary": {
            "source_sha256": "a" * 64,
            "coverage": {"coverage_ready": True},
            "research_evidence": {"claims": []},
            "satellite_artifacts": [],
            "spatial_observations": [],
        },
        "prior_daily_states": [],
        "published_reference_accessed": False,
        "geometry_mutation_allowed": False,
    }
    payload["source_sha256"] = _hash(payload)
    return payload


class _GeometryTransport:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.published: list[dict[str, Any]] = []

    def get_json(
        self,
        _url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendJsonResponse:
        assert headers
        assert timeout_seconds > 0
        assert max_response_bytes > 0
        checksum = str(self.context["source_sha256"])
        return BackendJsonResponse(
            payload=self.context,
            headers={"x-checksum-sha256": checksum, "etag": f'"{checksum}"'},
        )

    def post_json(
        self,
        _url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendJsonResponse:
        assert headers
        assert timeout_seconds > 0
        assert max_response_bytes > 0
        stored_payload = dict(payload)
        self.published.append(stored_payload)
        receipt_sha256 = "c" * 64
        receipt = {
            "schema": "fireviewer.incident-day-geometry-review-receipt.v1",
            "analysis_id": stored_payload["analysis_id"],
            "review_id": stored_payload["review_id"],
            "verdict": stored_payload["verdict"],
            "proposal_state": (
                "persisted_for_human_review" if stored_payload.get("proposal") else None
            ),
            "supervisor_mode": stored_payload["supervisor_mode"],
            "source_perimeter_unchanged": True,
            "replayed": False,
            "receipt_sha256": receipt_sha256,
        }
        return BackendJsonResponse(
            payload=receipt,
            headers={
                "x-checksum-sha256": receipt_sha256,
                "etag": f'"{receipt_sha256}"',
            },
        )


class _UnusedRepository:
    def read(self, _event_id: str) -> BackendEventEvidenceSnapshot:
        raise AssertionError("point EventEvidence must not be read for incident-day review")


class _BedrockGeometryClient:
    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "verdict": "propose",
                                    "candidate_id": self.candidate_id,
                                    "model_confidence": 0.91,
                                    "reason_codes": ["better_satellite_support"],
                                    "evidence_refs": [self.candidate_id],
                                }
                            )
                        }
                    ]
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 500, "outputTokens": 80},
        }


def test_simulated_incident_day_review_is_persisted_but_never_proposes_geometry() -> None:
    payload = _context_payload()
    transport = _GeometryTransport(payload)
    adapter = AzureBackendIncidentDayGeometryReviewAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token=SecretStr("s" * 40),
        ),
        transport=transport,
    )
    service = DurablePointSupervisionService(
        cast(EventEvidenceRepository, _UnusedRepository()),
        geometry_review_repository=adapter,
        geometry_reviewer=SimulatedIncidentDayGeometryReviewer(),
    )

    receipt = service.review_incident_day_payload(
        {"analysis_id": payload["analysis_id"]}
    )

    assert receipt["verdict"] == "abstain"
    assert receipt["source_perimeter_unchanged"] is True
    assert transport.published[0]["supervisor_mode"] == "simulated"
    assert transport.published[0]["proposal"] is None
    assert transport.published[0]["source_mutation_allowed"] is False


def test_bedrock_reviewer_can_only_copy_an_eligible_deterministic_candidate() -> None:
    context = BackendIncidentDayGeometryReviewContext.model_validate(_context_payload())
    client = _BedrockGeometryClient("CLAIM-S2-BURNED-1")
    reviewer = BedrockIncidentDayGeometryReviewer(
        BedrockIncidentDayGeometryReviewerConfig(maximum_output_tokens=512),
        client=cast(BedrockConverseClient, client),
    )

    review = reviewer.review(context)

    assert review.verdict == "propose"
    assert review.proposal is not None
    assert review.proposal.competing_geometry_geojson == context.candidate_geometries[0][
        "geometry_geojson"
    ]
    assert review.proposal.source_mutation_allowed is False
    assert review.provider_run["input_hash"] == context.source_sha256
    request = client.requests[0]
    assert request["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}
    response_schema_prompt = request["system"][0]["text"]
    assert "Never output, alter, or" in response_schema_prompt
    model_context = json.loads(request["messages"][0]["content"][0]["text"])
    model_candidate = model_context["candidate_geometries"][0]
    assert "geometry_geojson" not in model_candidate
    assert model_candidate["geometry_summary"] == {
        "schema": "fireviewer.geometry-summary.v1",
        "geometry_type": "Polygon",
        "geometry_sha256": _hash(
            context.candidate_geometries[0]["geometry_geojson"]
        ),
        "coordinate_count": 5,
        "bbox": [5.36, 44.74, 5.38, 44.76],
    }


def test_bedrock_reviewer_rejects_thermal_or_unknown_candidate_selection() -> None:
    context = BackendIncidentDayGeometryReviewContext.model_validate(_context_payload())
    reviewer = BedrockIncidentDayGeometryReviewer(
        BedrockIncidentDayGeometryReviewerConfig(maximum_output_tokens=512),
        client=cast(BedrockConverseClient, _BedrockGeometryClient("CLAIM-FIRMS-1")),
    )

    with pytest.raises(
        IncidentDayGeometryReviewerError,
        match="unknown_or_ineligible_candidate",
    ):
        reviewer.review(context)


def test_geometry_backend_can_use_a_dedicated_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIREVIEWER_BACKEND_BASE_URL", "https://backend.fireviewer.test")
    monkeypatch.setenv("FIREVIEWER_BACKEND_TOKEN", "e" * 40)
    monkeypatch.setenv("FIREVIEWER_GEOMETRY_BACKEND_TOKEN", "g" * 40)

    settings = PointSupervisorCpuSettings.from_environment()

    assert settings.backend.bearer_token.get_secret_value() == "e" * 40
    assert settings.geometry_backend.bearer_token.get_secret_value() == "g" * 40


def test_geometry_backend_token_defaults_to_existing_backend_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIREVIEWER_BACKEND_BASE_URL", "https://backend.fireviewer.test")
    monkeypatch.setenv("FIREVIEWER_BACKEND_TOKEN", "e" * 40)
    monkeypatch.delenv("FIREVIEWER_GEOMETRY_BACKEND_TOKEN", raising=False)

    settings = PointSupervisorCpuSettings.from_environment()

    assert settings.geometry_backend.bearer_token.get_secret_value() == "e" * 40
