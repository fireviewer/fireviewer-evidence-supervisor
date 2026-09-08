from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPConnection

import pytest
from pydantic import ValidationError

from fireviewer_contracts.mvp.contracts import (
    CompetingPointJsonV1,
    DetectionResultV1,
    EventEvidenceV1,
    GeospatialConsistencyCheck,
    PointAssessmentV1,
    PointEvidenceBundleV1,
    PriorFireStateReference,
    UploadLocationEvidence,
    VisualObservation,
)
from fireviewer_evidence_supervisor.mvp.supervision import (
    AzureBackendEventEvidenceAdapter,
    AzureBackendEventEvidenceConfig,
    BackendBinaryResponse,
    BackendEventEvidenceError,
    BackendJsonResponse,
    BackendPointAssessmentPublisher,
    BackendVisualEvidencePublisher,
    BedrockPixtralPointSupervisor,
    BedrockPixtralPointSupervisorConfig,
    DurablePointSupervisionService,
    EventRagIndex,
    EventRagQuery,
    PointEvidenceAssembler,
    SimulatedPointSupervisor,
    apply_point_publication_policy,
    create_point_supervisor_server,
)

NOW = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)


def _event() -> EventEvidenceV1:
    return EventEvidenceV1.model_validate(
        {
            "schema": "fireviewer.event-evidence.v1",
            "event_id": "EVENT-SUPERVISION-1",
            "time_window": {
                "from_at": "2026-08-22T14:00:00Z",
                "to_at": "2026-08-22T18:00:00Z",
            },
            "sources": [
                {
                    "source_id": "SOURCE-WITNESS-1",
                    "origin_id": "ORIGIN-WITNESS-1",
                    "publisher": "Synthetic witness",
                    "retrieved_at": "2026-08-22T17:00:00Z",
                    "source_type": "witness",
                    "independence_weight": 1,
                },
                {
                    "source_id": "SOURCE-SATELLITE-1",
                    "origin_id": "ORIGIN-SATELLITE-1",
                    "publisher": "Synthetic satellite",
                    "retrieved_at": "2026-08-22T17:10:00Z",
                    "source_type": "satellite",
                    "independence_weight": 1,
                },
            ],
            "claims": [
                {
                    "claim_id": "CLAIM-SMOKE-1",
                    "source_id": "SOURCE-WITNESS-1",
                    "claim_type": "visible_smoke",
                    "text": "Smoke is visible on the eastern ridge.",
                    "observed_at": "2026-08-22T16:40:00Z",
                    "confidence": 0.8,
                }
            ],
            "media": [
                {
                    "media_id": "MEDIA-PHOTO-1",
                    "source_id": "SOURCE-WITNESS-1",
                    "media_group_id": "GROUP-PHOTO-1",
                    "origin_id": "ORIGIN-WITNESS-1",
                    "kind": "photo",
                    "sha256": "a" * 64,
                    "captured_at": "2026-08-22T16:40:00Z",
                },
                {
                    "media_id": "MEDIA-SATELLITE-1",
                    "source_id": "SOURCE-SATELLITE-1",
                    "media_group_id": "GROUP-SATELLITE-1",
                    "origin_id": "ORIGIN-SATELLITE-1",
                    "kind": "satellite_image",
                    "sha256": "b" * 64,
                    "captured_at": "2026-08-22T16:30:00Z",
                },
            ],
            "visual_observations": [
                {
                    "observation_id": "VISUAL-SMOKE-1",
                    "media_id": "MEDIA-PHOTO-1",
                    "observation_type": "detection",
                    "result_reference": "YOLO-SMOKE-1",
                    "confidence": 0.83,
                }
            ],
            "satellite_observations": [
                {
                    "observation_id": "SATELLITE-HOTSPOT-1",
                    "source_id": "SOURCE-SATELLITE-1",
                    "media_id": "MEDIA-SATELLITE-1",
                    "observation_type": "hotspot",
                    "result_reference": "HOTSPOT-RESULT-1",
                    "acquired_at": "2026-08-22T16:30:00Z",
                    "confidence": 0.76,
                }
            ],
            "location_candidates": [
                {
                    "candidate_id": "CANDIDATE-1",
                    "longitude": 5.3705,
                    "latitude": 44.7505,
                    "radius_m": 120,
                    "score": 0.82,
                    "rank": 1,
                    "evidence_kind": "visual_retrieval",
                    "provider_id": "megaloc",
                    "provider_version": "mvp-1",
                    "source_id": "SOURCE-WITNESS-1",
                    "media_id": "MEDIA-PHOTO-1",
                    "reference_id": "REFERENCE-RIDGE-1",
                }
            ],
            "candidate_clusters": [
                {
                    "cluster_id": "CLUSTER-1",
                    "center": [5.3705, 44.7505],
                    "radius_m": 500,
                    "score": 0.81,
                    "score_breakdown": {
                        "retrieval": 0.82,
                        "source_independence": 0.5,
                        "geographic_prior": 0.8,
                    },
                    "supporting_candidate_ids": ["CANDIDATE-1"],
                    "supporting_source_ids": ["SOURCE-WITNESS-1"],
                    "supporting_media_ids": ["MEDIA-PHOTO-1"],
                    "independent_source_count": 1,
                    "independent_media_count": 1,
                }
            ],
            "needs_human_review": True,
        }
    )


def _upload_location() -> UploadLocationEvidence:
    return UploadLocationEvidence(
        location_id="UPLOAD-LOCATION-1",
        media_id="MEDIA-PHOTO-1",
        longitude=5.35,
        latitude=44.74,
        accuracy_m=25,
        location_origin="user_declared",
        captured_at=datetime(2026, 8, 22, 16, 40, tzinfo=UTC),
        heading_deg=42,
        heading_uncertainty_deg=15,
        source_record_sha256="c" * 64,
    )


def _prior_state() -> PriorFireStateReference:
    return PriorFireStateReference(
        state_id="PRIOR-STATE-1",
        state_kind="published_perimeter",
        observed_at=datetime(2026, 8, 22, 15, 30, tzinfo=UTC),
        artifact_reference="azure://events/EVENT-SUPERVISION-1/prior-state.json",
        artifact_sha256="d" * 64,
        distance_to_candidate_m=750,
        direction_consistency=0.86,
    )


def _checks(*, hard_contradiction: bool = False) -> tuple[GeospatialConsistencyCheck, ...]:
    bearing_status = "contradicted" if hard_contradiction else "supported"
    return (
        GeospatialConsistencyCheck(
            check_id="CHECK-DISTANCE-1",
            check_type="camera_distance",
            status="supported",
            score=0.9,
            reason_code="camera_distance_plausible",
            evidence_ids=("CANDIDATE-1", "MEDIA-PHOTO-1"),
            hard_constraint=True,
        ),
        GeospatialConsistencyCheck(
            check_id="CHECK-BEARING-1",
            check_type="camera_bearing",
            status=bearing_status,
            score=0.91 if not hard_contradiction else 0.02,
            reason_code=(
                "camera_bearing_impossible" if hard_contradiction else "camera_bearing_plausible"
            ),
            evidence_ids=("CANDIDATE-1", "MEDIA-PHOTO-1"),
            hard_constraint=True,
        ),
        GeospatialConsistencyCheck(
            check_id="CHECK-SATELLITE-1",
            check_type="satellite_overlap",
            status="supported",
            score=0.76,
            reason_code="satellite_overlap_supported",
            evidence_ids=("CANDIDATE-1", "SATELLITE-HOTSPOT-1"),
        ),
        GeospatialConsistencyCheck(
            check_id="CHECK-HISTORY-1",
            check_type="history_progression",
            status="supported",
            score=0.86,
            reason_code="history_progression_plausible",
            evidence_ids=("CANDIDATE-1", "PRIOR-STATE-1"),
        ),
    )


def _canonical_hash(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _backend_snapshot_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "event-evidence-read-1.0",
        "candidate_id": "EVENT-SUPERVISION-1",
        "candidate_revision": 3,
        "candidate_state": "NEEDS_REVIEW",
        "updated_at": "2026-08-22T18:00:00+00:00",
        "bundle": {
            "candidate_id": "EVENT-SUPERVISION-1",
            "incident_id": "FR-26-00001",
            "incident_candidate_id": None,
            "viewpoint": {
                "longitude": 5.35,
                "latitude": 44.74,
                "horizontal_accuracy_m": 25,
                "altitude_m": None,
                "label": "Synthetic viewpoint",
                "yaw_deg": 42,
                "fov_deg": 30,
                "origin": "USER_PLACED",
            },
            "observed_time": {
                "start_at": "2026-08-22T16:40:00+00:00",
                "end_at": None,
            },
            "message": "Smoke is visible on the eastern ridge.",
            "evidence_assets": [
                {
                    "evidence_asset_id": "MEDIA-PHOTO-1",
                    "kind": "image",
                    "declared_media_type": "image/png",
                    "detected_media_type": "image/png",
                    "size_bytes": 1024,
                    "sha256": "a" * 64,
                    "capture_context": {
                        "evidence_asset_id": "MEDIA-PHOTO-1",
                        "captured_at": "2026-08-22T16:42:00+00:00",
                        "viewpoint": {
                            "longitude": 5.351,
                            "latitude": 44.741,
                            "horizontal_accuracy_m": 12,
                            "altitude_m": 510,
                            "label": "Per-media viewpoint",
                            "yaw_deg": 87,
                            "pitch_deg": -6,
                            "roll_deg": 2,
                            "fov_deg": 54,
                            "vertical_fov_deg": 36,
                            "image_width_px": 1920,
                            "image_height_px": 1080,
                            "origin": "USER_PLACED",
                        },
                    },
                }
            ],
            "consent": {
                "analysis": True,
                "retention": True,
                "public_derivative": False,
            },
            "provenance": {
                "received_at": "2026-08-22T17:00:00+00:00",
                "idempotency_key": "fixture-1",
                "trace_id": "trace-fixture-1",
            },
            "external_observations": [
                {
                    "observation_id": "SATELLITE-EXTERNAL-1",
                    "artifact_revision_id": "VIIRS-REV-1",
                    "lineage_family_id": "VIIRS-FAMILY-1",
                    "semantic_role": "sensor_detection",
                    "phenomenon": "thermal_hotspot",
                    "observed_at": "2026-08-22T16:30:00+00:00",
                    "geometry_geojson": {
                        "type": "Point",
                        "coordinates": [5.3705, 44.7505],
                    },
                    "resolution_m": 375,
                    "confidence": 0.76,
                },
                {
                    "observation_id": "CLMS-BURN-SCAR-1",
                    "artifact_revision_id": "CLMS-REV-1",
                    "lineage_family_id": "CLMS-FAMILY-1",
                    "semantic_role": "interpreted_observation",
                    "phenomenon": "burned_area",
                    "observed_at": "2026-08-22T15:00:00+00:00",
                    "geometry_geojson": {
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
                    },
                    "resolution_m": 300,
                    "confidence": 0.82,
                },
            ],
        },
        "localization_attempts": [
            {
                "attempt_id": "CANDIDATE-1",
                "state": "PROPOSED",
                "method": "camera_raycast",
                "model_id": "localization-test",
                "model_revision": "mvp-1",
                "view_profile": "ground_distant_known_viewpoint",
                "anchor": {
                    "anchor_id": "ANCHOR-1",
                    "phenomenon": "active_fire_point",
                    "perception": {
                        "anchor_id": "ANCHOR-1",
                        "evidence_asset_id": "MEDIA-PHOTO-1",
                        "source_point_normalized": [0.75, 0.5],
                        "source_geometry_normalized": None,
                        "model_id": "localization-test",
                        "model_revision": "mvp-1",
                        "model_score": 0.82,
                    },
                },
                "geometry": {"type": "Point", "coordinates": [5.3705, 44.7505]},
                "uncertainty": None,
                "horizontal_uncertainty_m": 120,
                "abstention_reason": None,
                "provenance": {"reference_revision": "terrain-1"},
                "updated_at": "2026-08-22T17:45:00+00:00",
            }
        ],
        "prior_fire_activity_events": [
            {
                "event_id": "FIRE-STATE-1",
                "state": "EDITOR_PUBLISHED",
                "phenomenon_kind": "active_fire",
                "observed_start_at": "2026-08-22T15:30:00+00:00",
                "observed_end_at": None,
                "geometry": {"type": "Point", "coordinates": [5.36, 44.745]},
                "uncertainty": {"type": "Point", "coordinates": [5.36, 44.745]},
                "method": "analyst_validation",
                "version": 2,
                "updated_at": "2026-08-22T16:00:00+00:00",
            }
        ],
        "terrain_reference": {
            "terrain_id": "TERRAIN-1",
            "package_id": "PACKAGE-1",
            "file_id": 1,
            "sha256": "d" * 64,
            "size_bytes": 1_024,
            "media_type": "application/vnd.fireviewer.terrain",
            "crs": "EPSG:2154",
            "resolution_m": 25,
            "content_path": ("/api/v1/internal/event-evidence/EVENT-SUPERVISION-1/terrain/content"),
        },
        "analysis_result_sha256": "f" * 64,
    }
    payload["source_sha256"] = _canonical_hash(payload)
    return payload


class _SnapshotTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendJsonResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        checksum = str(self.payload["source_sha256"])
        return BackendJsonResponse(
            payload=dict(self.payload),
            headers={
                "etag": f'"{checksum}"',
                "x-checksum-sha256": checksum,
            },
        )


class _PublishTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendJsonResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        revision = "f" * 64
        return BackendJsonResponse(
            payload={
                "candidate_id": "EVENT-SUPERVISION-1",
                "observation_count": 1,
                "replayed": False,
                "source_revision_sha256": revision,
            },
            headers={
                "etag": f'"{revision}"',
                "x-checksum-sha256": revision,
            },
        )


class _MediaTransport:
    def __init__(self, content: bytes, content_type: str = "image/png") -> None:
        self.content = content
        self.content_type = content_type
        self.calls: list[dict[str, object]] = []

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendBinaryResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        digest = sha256(self.content).hexdigest()
        return BackendBinaryResponse(
            content=self.content,
            content_type=self.content_type,
            headers={
                "etag": f'"{digest}"',
                "x-checksum-sha256": digest,
            },
        )


class _BedrockPointClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(dict(kwargs))
        return {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 512, "outputTokens": 128, "totalTokens": 640},
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "verdict": "accept",
                                    "model_confidence": 0.92,
                                    "subscores": {
                                        "visual": 0.91,
                                        "camera_geo": 0.9,
                                        "satellite": 0.86,
                                        "history": 0.88,
                                        "text_sources": 0.8,
                                    },
                                    "reason_codes": ["evidence_consistent"],
                                    "supporting_evidence_ids": [
                                        "CANDIDATE-1",
                                        "MEDIA-PHOTO-1",
                                    ],
                                    "contradicting_evidence_ids": [],
                                    "hard_contradiction_codes": [],
                                    "missing_evidence_codes": [],
                                    "competing_point": None,
                                }
                            )
                        }
                    ]
                }
            },
        }


class _PointAssessmentTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> BackendJsonResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        assessment = payload["assessment"]
        assert isinstance(assessment, dict)
        point_bundle = payload["point_bundle"]
        assert isinstance(point_bundle, dict)
        point = point_bundle["point"]
        assert isinstance(point, dict)
        receipt_sha256 = "f" * 64
        return BackendJsonResponse(
            payload={
                "schema_version": "point-assessment-publication-receipt-1.0",
                "candidate_id": "EVENT-SUPERVISION-1",
                "assessment_id": assessment["assessment_id"],
                "point_id": point["point_id"],
                "release_status": "eligible_for_automatic_publication",
                "publication_state": "EDITOR_PUBLISHED",
                "fire_activity_event_id": "FAE-AUTO-1",
                "localization_attempt_id": "LOC-AUTO-1",
                "publication_revision": 1,
                "competing_point_state": None,
                "replayed": False,
                "receipt_sha256": receipt_sha256,
            },
            headers={
                "etag": f'"{receipt_sha256}"',
                "x-checksum-sha256": receipt_sha256,
            },
        )


def _bundle(*, hard_contradiction: bool = False) -> PointEvidenceBundleV1:
    return PointEvidenceAssembler().assemble(
        _event(),
        candidate_id="CANDIDATE-1",
        upload_locations=(_upload_location(),),
        prior_fire_states=(_prior_state(),),
        geospatial_checks=_checks(hard_contradiction=hard_contradiction),
        generated_at=NOW,
    )


def test_point_bundle_is_bounded_read_only_and_keeps_upload_location() -> None:
    bundle = _bundle()

    assert bundle.point.source_candidate_ids == ("CANDIDATE-1",)
    assert bundle.upload_locations[0].location_origin == "user_declared"
    assert bundle.prior_fire_states[0].read_only is True
    assert bundle.geometry_mutation_allowed is False
    assert bundle.missing_evidence_codes == ()
    assert len(bundle.retrieved_context) <= 12
    assert {item.evidence_type for item in bundle.evidence_references} >= {
        "location_candidate",
        "visual_observation",
        "satellite_observation",
        "prior_fire_state",
    }

    payload = bundle.model_dump(mode="json")
    payload["perimeter"] = {"type": "Polygon", "coordinates": []}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PointEvidenceBundleV1.model_validate(payload)


def test_bedrock_pixtral_supervisor_reads_selected_image_and_stays_unreleased() -> None:
    image_content = b"bounded-private-fire-image"
    image_sha256 = sha256(image_content).hexdigest()
    snapshot = _backend_snapshot_payload()
    snapshot.pop("source_sha256")
    bundle_payload = snapshot["bundle"]
    assert isinstance(bundle_payload, dict)
    evidence_assets = bundle_payload["evidence_assets"]
    assert isinstance(evidence_assets, list)
    evidence_assets[0]["sha256"] = image_sha256
    snapshot["source_sha256"] = _canonical_hash(snapshot)
    media_transport = _MediaTransport(image_content)
    repository = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=_SnapshotTransport(snapshot),
        media_transport=media_transport,
    )
    bedrock = _BedrockPointClient()
    service = DurablePointSupervisionService(
        repository,
        supervisor=BedrockPixtralPointSupervisor(
            BedrockPixtralPointSupervisorConfig(),
            client=bedrock,
        ),
        clock=lambda: NOW,
    )

    point_bundle = service.bundle_payload(
        {"event_id": "EVENT-SUPERVISION-1", "candidate_id": "CANDIDATE-1"}
    )
    assessment = service.assess_payload(point_bundle)

    assert assessment["supervisor_mode"] == "managed_vl"
    assert assessment["verdict"] == "accept"
    assert assessment["release_status"] == "held_for_review"
    assert assessment["calibrated_confidence"] is None
    assert assessment["point_id"] == point_bundle["point"]["point_id"]
    assert media_transport.calls[0]["url"].endswith(
        "/EVENT-SUPERVISION-1/assets/MEDIA-PHOTO-1/content"
    )
    request = bedrock.requests[0]
    assert request["modelId"] == "eu.mistral.pixtral-large-2502-v1:0"
    assert request["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0}
    messages = request["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert any(
        isinstance(block, dict)
        and block.get("image", {}).get("source", {}).get("bytes") == image_content
        for block in content
    )


def test_rag_is_event_scoped_and_returns_evidence_backed_context() -> None:
    rag = EventRagIndex.from_event(_event(), prior_fire_states=(_prior_state(),))
    results = rag.search(
        EventRagQuery(
            event_id="EVENT-SUPERVISION-1",
            text="satellite hotspot historique",
            center=(5.3705, 44.7505),
            radius_m=10_000,
            limit=6,
        )
    )

    assert results
    assert all(result.evidence_ids for result in results)
    assert any(result.evidence_type == "satellite_observation" for result in results)
    with pytest.raises(ValueError, match="different event"):
        rag.search(EventRagQuery(event_id="OTHER-EVENT", text="smoke"))


def test_simulated_supervisor_never_promotes_and_rejects_hard_contradictions() -> None:
    supervisor = SimulatedPointSupervisor()

    abstained = supervisor.assess(_bundle(), generated_at=NOW)
    assert abstained.verdict == "abstain"
    assert abstained.release_status == "held_for_review"
    assert abstained.calibrated_confidence is None
    assert abstained.provider_run.cost_usd == 0

    rejected = supervisor.assess(_bundle(hard_contradiction=True), generated_at=NOW)
    assert rejected.verdict == "reject"
    assert rejected.release_status == "held_for_review"
    assert rejected.hard_contradiction_codes == ("camera_bearing_impossible",)
    assert rejected.competing_point_json is not None
    assert rejected.competing_point_json.relationship == "competes_with_source"
    assert rejected.competing_point_json.source_bundle_sha256 == rejected.bundle_sha256
    assert rejected.competing_point_json.point.point_id != rejected.point_id
    assert rejected.competing_point_json.point.radius_m == 240
    assert rejected.competing_point_json.source_mutation_allowed is False


def test_publication_policy_requires_calibrated_confidence_strictly_above_85() -> None:
    payload = SimulatedPointSupervisor().assess(_bundle(), generated_at=NOW).model_dump(mode="json")
    payload.update(
        {
            "verdict": "accept",
            "release_status": "held_for_review",
            "reason_codes": ["all_evidence_consistent"],
            "missing_evidence_codes": [],
            "supervisor_mode": "managed_vl",
        }
    )
    assessment = PointAssessmentV1.model_validate(payload)

    threshold = apply_point_publication_policy(
        assessment,
        calibrated_confidence=0.85,
        calibrator_id="CALIBRATOR-FIREVIEWER-V1",
    )
    eligible = apply_point_publication_policy(
        assessment,
        calibrated_confidence=0.850001,
        calibrator_id="CALIBRATOR-FIREVIEWER-V1",
    )

    assert threshold.release_status == "held_for_review"
    assert threshold.needs_human_review is True
    assert eligible.release_status == "eligible_for_automatic_publication"
    assert eligible.needs_human_review is False
    assert eligible.bundle_sha256 == assessment.bundle_sha256
    assert eligible.point_id == assessment.point_id


def test_publication_policy_never_promotes_a_simulated_supervisor() -> None:
    payload = SimulatedPointSupervisor().assess(_bundle(), generated_at=NOW).model_dump(mode="json")
    payload.update(
        {
            "verdict": "accept",
            "release_status": "held_for_review",
            "reason_codes": ["simulated_acceptance_probe"],
            "missing_evidence_codes": [],
        }
    )

    held = apply_point_publication_policy(
        PointAssessmentV1.model_validate(payload),
        calibrated_confidence=0.99,
        calibrator_id="CALIBRATOR-FIREVIEWER-V1",
    )

    assert held.supervisor_mode == "simulated"
    assert held.release_status == "held_for_review"
    assert held.needs_human_review is True


def test_point_assessment_publisher_routes_eligible_source_bundle_to_event_v2() -> None:
    bundle = _bundle()
    payload = SimulatedPointSupervisor().assess(bundle, generated_at=NOW).model_dump(mode="json")
    payload.update(
        {
            "verdict": "accept",
            "release_status": "held_for_review",
            "reason_codes": ["all_evidence_consistent"],
            "missing_evidence_codes": [],
            "supervisor_mode": "managed_vl",
        }
    )
    eligible = apply_point_publication_policy(
        PointAssessmentV1.model_validate(payload),
        calibrated_confidence=0.9,
        calibrator_id="CALIBRATOR-FIREVIEWER-V1",
    )
    transport = _PointAssessmentTransport()
    publisher = BackendPointAssessmentPublisher(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=transport,
    )

    receipt = publisher.publish(
        candidate_id=bundle.event_id,
        point_bundle=bundle,
        assessment=eligible,
    )

    assert receipt.publication_state == "EDITOR_PUBLISHED"
    sent = transport.calls[0]
    assert sent["url"] == (
        "https://backend.fireviewer.test/api/v1/internal/event-evidence/"
        "EVENT-SUPERVISION-1/point-assessments"
    )
    sent_payload = sent["payload"]
    assert isinstance(sent_payload, dict)
    assert sent_payload["schema_version"] == "point-assessment-publication-1.0"
    point_bundle = sent_payload["point_bundle"]
    assert isinstance(point_bundle, dict)
    assert point_bundle["point"]["phenomenon"] == "active_fire_point"
    assert sent_payload["assessment"]["release_status"] == ("eligible_for_automatic_publication")


def test_correction_is_a_concurrent_json_and_cannot_replace_its_source() -> None:
    payload = {
        "schema": "fireviewer.competing-point-correction.v1",
        "correction_id": "CORRECTION-1",
        "event_id": "EVENT-SUPERVISION-1",
        "source_point_id": "CANDIDATE-1",
        "source_bundle_sha256": "a" * 64,
        "point": {
            "point_id": "COMPETING-CANDIDATE-1",
            "phenomenon": "active_fire_point",
            "longitude": 5.4,
            "latitude": 44.8,
            "radius_m": 2_000,
            "source_candidate_ids": ["CANDIDATE-1"],
        },
        "reason_codes": ["camera_pose_inconsistent"],
        "evidence_ids": ["CANDIDATE-1"],
        "relationship": "competes_with_source",
        "state": "proposed",
        "source_mutation_allowed": False,
    }

    competing = CompetingPointJsonV1.model_validate(payload)

    assert competing.point.longitude == 5.4
    assert competing.point.latitude == 44.8
    assert competing.source_point_id == "CANDIDATE-1"
    assert competing.point.point_id == "COMPETING-CANDIDATE-1"

    payload["source_mutation_allowed"] = True
    with pytest.raises(ValidationError, match="Input should be False"):
        CompetingPointJsonV1.model_validate(payload)


def test_azure_backend_adapter_validates_revision_and_maps_durable_evidence() -> None:
    transport = _SnapshotTransport(_backend_snapshot_payload())
    adapter = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=transport,
    )

    durable = adapter.read("EVENT-SUPERVISION-1")

    assert durable.event.event_id == "EVENT-SUPERVISION-1"
    assert durable.event.location_candidates[0].candidate_id == "CANDIDATE-1"
    assert durable.media_locations[0].working_file_url == (
        "https://backend.fireviewer.test/api/v1/internal/event-evidence/"
        "EVENT-SUPERVISION-1/assets/MEDIA-PHOTO-1/content"
    )
    assert durable.vision_artifacts == ()
    assert durable.upload_locations[0].horizontal_fov_deg == 54
    assert durable.upload_locations[0].vertical_fov_deg == 36
    assert durable.upload_locations[0].heading_deg == 87
    assert durable.upload_locations[0].pitch_deg == -6
    assert durable.upload_locations[0].roll_deg == 2
    assert durable.upload_locations[0].image_width_px == 1920
    assert durable.upload_locations[0].captured_at == datetime(2026, 8, 22, 16, 42, tzinfo=UTC)
    assert durable.terrain_reference is not None
    assert durable.terrain_reference.content_url == (
        "https://backend.fireviewer.test/api/v1/internal/event-evidence/"
        "EVENT-SUPERVISION-1/terrain/content"
    )
    assert durable.terrain_reference.sha256 == "d" * 64
    assert {item.reference_kind for item in durable.geographic_references} == {
        "prior_active_point",
        "satellite_hotspot",
        "satellite_active_area",
    }
    assert {item.observation_id for item in durable.event.satellite_observations} == {
        "SATELLITE-EXTERNAL-1",
        "CLMS-BURN-SCAR-1",
    }
    assert durable.upload_locations[0].location_origin == "user_declared"
    assert durable.prior_fire_states[0].read_only is True
    assert {check.check_type for check in durable.checks_for("CANDIDATE-1")} == {
        "camera_distance",
        "camera_bearing",
        "history_progression",
    }
    assert transport.calls[0]["url"] == (
        "https://backend.fireviewer.test/api/v1/internal/event-evidence/EVENT-SUPERVISION-1"
    )
    assert transport.calls[0]["headers"] == {
        "Accept": "application/json",
        "Authorization": f"Bearer {'s' * 40}",
    }


def test_azure_backend_adapter_maps_source_tickets_without_raw_scraped_content() -> None:
    payload = _backend_snapshot_payload()
    payload.pop("source_sha256")
    payload["research_evidence"] = {
        "schema_version": "research-evidence-1.0",
        "candidate_id": "EVENT-SUPERVISION-1",
        "plan_id": "PLAN-SOURCES-1",
        "plan_revision": "1" * 64,
        "pages": [
            {
                "page_id": "PAGE-SOURCES-1",
                "page_number": 1,
                "cursor": None,
                "next_cursor": None,
                "completed": True,
                "request_sha256": "2" * 64,
                "duplicate_counts": [0, 0, 0],
                "persisted_at": "2026-08-22T18:00:00+00:00",
            }
        ],
        "sources": [
            {
                "source_id": "SRC-OFFICIAL-1",
                "origin_id": "ORIGIN-OFFICIAL-1",
                "source_url": "https://sources.example/incident-update",
                "publisher": "Official source",
                "published_at": "2026-08-22T16:35:00+00:00",
                "retrieved_at": "2026-08-22T18:00:00+00:00",
                "source_type": "official",
                "independence_weight": 0.95,
                "content_sha256": "3" * 64,
            }
        ],
        "claims": [],
        "media": [
            {
                "media_id": "MEDIA-OFFICIAL-1",
                "source_id": "SRC-OFFICIAL-1",
                "media_group_id": "GROUP-OFFICIAL-1",
                "origin_id": "ORIGIN-MEDIA-OFFICIAL-1",
                "kind": "photo",
                "sha256": "4" * 64,
                "captured_at": "2026-08-22T16:35:00+00:00",
                "source_url": "https://sources.example/fire.jpg",
                "content_type": "image/jpeg",
                "size_bytes": 4096,
            }
        ],
        "journal_entries": [
            {
                "entry_id": "JOURNAL-SOURCES-1",
                "stage": "media_fetch",
                "outcome": "partial",
                "error_code": "one_media_missing",
                "detail": "One referenced media URL was unavailable.",
                "source_url": "https://sources.example/missing.jpg",
                "occurred_at": "2026-08-22T18:00:00+00:00",
                "retryable": True,
                "provider_id": None,
                "model_revision": None,
                "prompt_revision": "prompt-1",
            }
        ],
        "retention_policy": {
            "raw_scraped_content_stored": False,
            "articles_stored": False,
            "transcripts_stored": False,
            "public_media_binaries_stored": False,
            "satellite_binaries_allowed": True,
            "perimeter_tiles_allowed": True,
            "user_media_requires_republication_consent": True,
        },
        "completed": True,
        "next_cursor": None,
    }
    payload["source_sha256"] = _canonical_hash(payload)
    adapter = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=_SnapshotTransport(payload),
    )

    durable = adapter.read("EVENT-SUPERVISION-1")

    assert len(durable.event.sources) == 2
    assert "SRC-OFFICIAL-1" in {item.source_id for item in durable.event.sources}
    assert durable.event.claims[0].claim_type == "contributor_observation"
    assert durable.event.media[-1].media_id == "MEDIA-OFFICIAL-1"
    assert durable.research_progress is not None
    assert durable.research_progress.completed is True
    assert durable.research_progress.page_count == 1
    assert durable.research_journal[0].outcome == "partial"
    assert "RAW ARTICLE" not in json.dumps(
        [item.model_dump(mode="json") for item in durable.research_journal]
    )


def test_azure_backend_adapter_maps_persisted_yolo_as_visual_only() -> None:
    payload = _backend_snapshot_payload()
    payload.pop("source_sha256")
    payload["visual_evidence"] = {
        "schema_version": "visual-evidence-1.0",
        "candidate_id": "EVENT-SUPERVISION-1",
        "source_revision_sha256": "e" * 64,
        "request_sha256": "d" * 64,
        "persisted_at": "2026-08-22T18:01:00+00:00",
        "observations": [
            {
                "observation_id": "OBS-YOLO-1",
                "media_id": "MEDIA-PHOTO-1",
                "observation_type": "detection",
                "result_reference": "GDN-YOLO-1",
                "confidence": 0.88,
                "result": {
                    "schema": "fireviewer.detection.v1",
                    "media_id": "MEDIA-PHOTO-1",
                    "provider_run": {
                        "provider_id": "yolo-fire-smoke-cpu",
                        "provider_version": "1.0.0",
                        "model_id": "mfranzon/fire-smoke-yolov8",
                        "model_version": "revision-1",
                        "config": {"device": "cpu"},
                        "input_hash": "a" * 64,
                        "runtime_ms": 42,
                        "cost_usd": 0,
                        "generated_at": "2026-08-22T18:00:30+00:00",
                    },
                    "detections": [
                        {
                            "detection_id": "DET-YOLO-1",
                            "detection_class": "smoke",
                            "bbox": [0.2, 0.1, 0.8, 0.7],
                            "score": 0.88,
                            "prompt": "smoke",
                        }
                    ],
                    "status": "smoke",
                    "review_status": "candidate",
                    "needs_human_review": True,
                },
            }
        ],
    }
    payload["source_sha256"] = _canonical_hash(payload)
    adapter = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=_SnapshotTransport(payload),
    )

    durable = adapter.read("EVENT-SUPERVISION-1")

    assert any(item.observation_id == "OBS-YOLO-1" for item in durable.event.visual_observations)
    assert durable.vision_artifacts[0].status == "smoke"
    assert len(durable.event.location_candidates) == 1


def test_visual_publisher_sends_detection_without_geographic_payload() -> None:
    observation = VisualObservation(
        observation_id="OBS-YOLO-1",
        media_id="MEDIA-PHOTO-1",
        observation_type="detection",
        result_reference="GDN-YOLO-1",
        confidence=0.88,
    )
    result = DetectionResultV1.model_validate(
        {
            "schema": "fireviewer.detection.v1",
            "media_id": "MEDIA-PHOTO-1",
            "provider_run": {
                "provider_id": "yolo-fire-smoke-cpu",
                "provider_version": "1.0.0",
                "model_id": "mfranzon/fire-smoke-yolov8",
                "model_version": "revision-1",
                "config": {"device": "cpu"},
                "input_hash": "a" * 64,
                "runtime_ms": 42,
                "cost_usd": 0,
                "generated_at": NOW,
            },
            "detections": [
                {
                    "detection_id": "DET-YOLO-1",
                    "detection_class": "smoke",
                    "bbox": [0.2, 0.1, 0.8, 0.7],
                    "score": 0.88,
                    "prompt": "smoke",
                }
            ],
            "status": "smoke",
            "needs_human_review": True,
        }
    )
    transport = _PublishTransport()
    publisher = BackendVisualEvidencePublisher(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=transport,
    )

    receipt = publisher.publish(
        candidate_id="EVENT-SUPERVISION-1",
        source_revision_sha256="e" * 64,
        observations=(observation,),
        artifacts={"GDN-YOLO-1": result},
    )

    assert receipt.observation_count == 1
    sent = transport.calls[0]["payload"]
    assert isinstance(sent, dict)
    assert "location_candidates" not in sent
    assert "localization_attempts" not in sent
    assert sent["observations"][0]["result"]["detections"][0]["bbox"] == [
        0.2,
        0.1,
        0.8,
        0.7,
    ]


def test_azure_backend_adapter_rejects_tampered_snapshot_and_plain_remote_http() -> None:
    payload = _backend_snapshot_payload()
    payload["candidate_revision"] = 4
    adapter = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=_SnapshotTransport(payload),
    )

    with pytest.raises(BackendEventEvidenceError, match="checksum mismatch"):
        adapter.read("EVENT-SUPERVISION-1")
    with pytest.raises(ValidationError, match="requires HTTPS"):
        AzureBackendEventEvidenceConfig(
            base_url="http://backend.fireviewer.test",
            bearer_token="s" * 40,
        )


def test_loopback_endpoint_reads_backend_searches_bundles_and_assesses() -> None:
    transport = _SnapshotTransport(_backend_snapshot_payload())
    repository = AzureBackendEventEvidenceAdapter(
        AzureBackendEventEvidenceConfig(
            base_url="https://backend.fireviewer.test",
            bearer_token="s" * 40,
        ),
        transport=transport,
    )
    server = create_point_supervisor_server(repository, clock=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    def post(path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        connection = HTTPConnection(str(host), int(port), timeout=5)
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST",
            path,
            body=body,
            headers={"content-type": "application/json", "content-length": str(len(body))},
        )
        response = connection.getresponse()
        parsed = json.loads(response.read())
        connection.close()
        return response.status, parsed

    try:
        status, removed_index = post("/v1/events/index", {"event_id": "ignored"})
        assert status == 404
        assert removed_index["error"] == "route_not_found"

        status, search = post(
            "/v1/events/search",
            {
                "event_id": "EVENT-SUPERVISION-1",
                "text": "satellite hotspot historique",
                "limit": 6,
            },
        )
        assert status == 200
        assert search["documents"]
        assert search["persistent"] is True
        assert search["source"] == "azure_backend_event_evidence"

        status, bundle = post(
            "/v1/points/bundle",
            {"event_id": "EVENT-SUPERVISION-1", "candidate_id": "CANDIDATE-1"},
        )
        assert status == 200
        assert bundle["schema"] == "fireviewer.point-evidence-bundle.v1"
        assert bundle["geometry_mutation_allowed"] is False
        assert bundle["assembler_run"]["config"]["source_revision_sha256"] == str(
            _backend_snapshot_payload()["source_sha256"]
        )
        assert bundle["source_event_evidence_sha256"] == str(
            _backend_snapshot_payload()["source_sha256"]
        )

        status, assessment = post("/v1/point-assessments", bundle)
        assert status == 200
        assert assessment["schema"] == "fireviewer.point-assessment.v1"
        assert assessment["verdict"] == "abstain"

        status, batch = post(
            "/v1/events/supervise",
            {"event_id": "EVENT-SUPERVISION-1"},
        )
        assert status == 200
        assert batch["schema"] == "fireviewer.event-point-supervision-receipt.v1"
        assert batch["candidate_id"] == "EVENT-SUPERVISION-1"
        assert batch["location_candidate_count"] == 1
        assert batch["assessment_count"] == 1
        assert batch["abstained_count"] == 1
        assert batch["raw_content_stored"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_simulated_endpoint_refuses_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="only listen on loopback"):
        create_point_supervisor_server(
            AzureBackendEventEvidenceAdapter(
                AzureBackendEventEvidenceConfig(
                    base_url="https://backend.fireviewer.test",
                    bearer_token="s" * 40,
                ),
                transport=_SnapshotTransport(_backend_snapshot_payload()),
            ),
            host="0.0.0.0",  # noqa: S104
        )
