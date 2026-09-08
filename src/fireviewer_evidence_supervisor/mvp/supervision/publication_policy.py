from __future__ import annotations

from fireviewer_contracts.contracts import SafeIdentifierV2
from fireviewer_contracts.mvp.contracts import (
    AUTO_PUBLICATION_CONFIDENCE_THRESHOLD,
    PointAssessmentV1,
)


def apply_point_publication_policy(
    assessment: PointAssessmentV1,
    *,
    calibrated_confidence: float,
    calibrator_id: SafeIdentifierV2,
) -> PointAssessmentV1:
    """Route an assessment without mutating its source point or evidence bundle."""

    eligible = (
        assessment.verdict == "accept"
        and calibrated_confidence > AUTO_PUBLICATION_CONFIDENCE_THRESHOLD
        and assessment.supervisor_mode == "managed_vl"
        and not assessment.hard_contradiction_codes
        and not assessment.missing_evidence_codes
    )
    payload = assessment.model_dump(mode="json", by_alias=True)
    payload.update(
        {
            "calibrated_confidence": calibrated_confidence,
            "calibrator_id": calibrator_id,
            "release_status": (
                "eligible_for_automatic_publication"
                if eligible
                else "held_for_review"
            ),
            "needs_human_review": not eligible,
        }
    )
    return PointAssessmentV1.model_validate(payload)


__all__ = ["apply_point_publication_policy"]
