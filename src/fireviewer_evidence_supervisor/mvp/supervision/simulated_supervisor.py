from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from statistics import fmean
from typing import Literal

from fireviewer_contracts.mvp.contracts import (
    AssessmentSubscores,
    CandidatePoint,
    CompetingPointJsonV1,
    PointAssessmentV1,
    PointEvidenceBundleV1,
    ProviderRun,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_evidence import canonical_model_sha256
from fireviewer_contracts.mvp.supervision.point_supervisor import PointSupervisorInputImage


def _mean_check_score(
    bundle: PointEvidenceBundleV1,
    check_types: frozenset[str],
) -> float | None:
    scores = [
        check.score
        for check in bundle.geospatial_checks
        if check.check_type in check_types and check.score is not None
    ]
    return fmean(scores) if scores else None


class SimulatedPointSupervisor:
    """Fail-closed wiring double. It never promotes a point to production."""

    provider_id = "eve-point-supervisor"
    provider_version = "simulated-1.0.0"
    model_id = "fireviewer/mock-multimodal-supervisor"
    model_version = "simulated-no-gpu-v1"
    prompt_version = "point-evidence-assessor-v2"
    supervisor_mode: Literal["simulated"] = "simulated"
    max_images = 0

    def assess(
        self,
        bundle: PointEvidenceBundleV1,
        *,
        generated_at: datetime,
        images: tuple[PointSupervisorInputImage, ...] = (),
    ) -> PointAssessmentV1:
        del images
        bundle_sha256 = canonical_model_sha256(bundle)
        supported = {
            evidence_id
            for check in bundle.geospatial_checks
            if check.status == "supported"
            for evidence_id in check.evidence_ids
        }
        contradicted = {
            evidence_id
            for check in bundle.geospatial_checks
            if check.status == "contradicted"
            for evidence_id in check.evidence_ids
        }
        supported -= contradicted
        hard_codes = tuple(
            sorted(
                {
                    check.reason_code
                    for check in bundle.geospatial_checks
                    if check.hard_constraint and check.status == "contradicted"
                }
            )
        )
        verdict: Literal["reject", "abstain"]
        competing_point_json: CompetingPointJsonV1 | None
        if hard_codes:
            verdict = "reject"
            reason_codes = ("hard_geospatial_contradiction", *hard_codes)
            missing_codes = bundle.missing_evidence_codes
            competing_point_json = CompetingPointJsonV1(
                correction_id=f"CORRECTION-{bundle.point.point_id}",
                event_id=bundle.event_id,
                source_point_id=bundle.point.point_id,
                source_bundle_sha256=bundle_sha256,
                point=CandidatePoint(
                    point_id=f"COMPETING-{bundle.point.point_id}",
                    phenomenon=bundle.point.phenomenon,
                    longitude=bundle.point.longitude,
                    latitude=bundle.point.latitude,
                    radius_m=min(bundle.point.radius_m * 2, 1_000_000),
                    source_candidate_ids=bundle.point.source_candidate_ids,
                ),
                reason_codes=("hard_geospatial_contradiction", *hard_codes),
                evidence_ids=tuple(sorted(contradicted)),
            )
        else:
            verdict = "abstain"
            reason_codes = ("simulated_model_not_for_promotion",)
            missing_codes = tuple(
                sorted({*bundle.missing_evidence_codes, "real_multimodal_model_required"})
            )
            competing_point_json = None
        assessment_digest = sha256(
            f"{bundle_sha256}|{verdict}|{self.model_version}".encode()
        ).hexdigest()
        return PointAssessmentV1(
            assessment_id=f"ASSESSMENT-{assessment_digest[:24]}",
            event_id=bundle.event_id,
            point_id=bundle.point.point_id,
            bundle_sha256=bundle_sha256,
            verdict=verdict,
            model_confidence=0,
            subscores=AssessmentSubscores(
                visual=_mean_check_score(bundle, frozenset({"camera_bearing"})),
                camera_geo=_mean_check_score(
                    bundle,
                    frozenset(
                        {
                            "camera_distance",
                            "camera_bearing",
                            "line_of_sight",
                            "terrain_visibility",
                        }
                    ),
                ),
                satellite=_mean_check_score(bundle, frozenset({"satellite_overlap"})),
                history=_mean_check_score(bundle, frozenset({"history_progression"})),
                text_sources=None,
            ),
            reason_codes=reason_codes,
            supporting_evidence_ids=tuple(sorted(supported)),
            contradicting_evidence_ids=tuple(sorted(contradicted)),
            hard_contradiction_codes=hard_codes,
            missing_evidence_codes=missing_codes,
            competing_point_json=competing_point_json,
            release_status="held_for_review",
            supervisor_mode="simulated",
            provider_run=ProviderRun(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                model_id=self.model_id,
                model_version=self.model_version,
                config={
                    "mode": "deterministic_fail_closed",
                    "gpu": False,
                    "publication_eligible": False,
                },
                input_hash=bundle_sha256,
                runtime_ms=0,
                cost_usd=0,
                generated_at=generated_at,
            ),
            prompt_version=self.prompt_version,
            needs_human_review=True,
        )


__all__ = ["SimulatedPointSupervisor"]
