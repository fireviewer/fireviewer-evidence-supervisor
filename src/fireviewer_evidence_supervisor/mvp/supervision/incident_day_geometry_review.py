from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from math import isfinite
from numbers import Real
from typing import Any, Literal, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field, model_validator

from fireviewer_contracts.contracts import StrictModel
from fireviewer_evidence_ingestion.mvp.research.multimodal_evidence import (
    BedrockConverseClient,
    MultimodalEvidenceProviderError,
)
from fireviewer_contracts.mvp.supervision.backend_event_evidence import (
    BackendGeometryCorrectionProposal,
    BackendIncidentDayGeometryReviewContext,
    BackendIncidentDayGeometryReviewRequest,
)

PROMPT_REVISION = "fireviewer-incident-day-geometry-review-1.0.1"


class IncidentDayGeometryReviewerError(RuntimeError):
    """The reviewer did not return an evidence-bound candidate selection."""


class IncidentDayGeometryReviewDecision(StrictModel):
    verdict: Literal["accept", "propose", "abstain"]
    candidate_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    model_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_selection(self) -> IncidentDayGeometryReviewDecision:
        if (self.verdict == "propose") != (self.candidate_id is not None):
            raise ValueError("only a proposal may select a competing geometry")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("geometry-review reason codes must be unique")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("geometry-review evidence references must be unique")
        return self


class IncidentDayGeometryReviewer(Protocol):
    def review(
        self,
        context: BackendIncidentDayGeometryReviewContext,
    ) -> BackendIncidentDayGeometryReviewRequest: ...


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _geometry_summary(geometry: Mapping[str, Any]) -> dict[str, Any]:
    bounds: list[float] | None = None
    coordinate_count = 0

    def visit(value: object) -> None:
        nonlocal bounds, coordinate_count
        if not isinstance(value, list):
            return
        if (
            len(value) >= 2
            and isinstance(value[0], Real)
            and not isinstance(value[0], bool)
            and isinstance(value[1], Real)
            and not isinstance(value[1], bool)
        ):
            x = float(value[0])
            y = float(value[1])
            if not isfinite(x) or not isfinite(y):
                return
            coordinate_count += 1
            if bounds is None:
                bounds = [x, y, x, y]
            else:
                bounds[0] = min(bounds[0], x)
                bounds[1] = min(bounds[1], y)
                bounds[2] = max(bounds[2], x)
                bounds[3] = max(bounds[3], y)
            return
        for item in value:
            visit(item)

    visit(geometry.get("coordinates"))
    return {
        "schema": "fireviewer.geometry-summary.v1",
        "geometry_type": geometry.get("type"),
        "geometry_sha256": _canonical_sha256(geometry),
        "coordinate_count": coordinate_count,
        "bbox": bounds,
    }


def _compact_model_value(value: object) -> object:
    if isinstance(value, Mapping):
        if value.get("type") in {
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
        } and isinstance(value.get("coordinates"), list):
            return _geometry_summary(value)
        return {key: _compact_model_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_compact_model_value(item) for item in value]
    return value


def _compact_model_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: _compact_model_value(value)
        for key, value in candidate.items()
        if key != "geometry_geojson"
    }
    geometry = candidate.get("geometry_geojson")
    if "geometry_summary" not in result and isinstance(geometry, Mapping):
        result["geometry_summary"] = _geometry_summary(geometry)
    return result


def _json_object(value: str) -> Mapping[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise IncidentDayGeometryReviewerError("bedrock_geometry_review_invalid_json")


def _review_id(context: BackendIncidentDayGeometryReviewContext, decision: object) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": context.source_sha256,
            "source_perimeter_sha256": context.source_perimeter_sha256,
            "decision": decision,
        }
    )
    return f"GEOMETRY-REVIEW-{digest[:24]}"


class SimulatedIncidentDayGeometryReviewer:
    supervisor_mode: Literal["simulated"] = "simulated"
    provider_id = "fireviewer-simulated-geometry-reviewer"
    provider_version = "1.0.1"
    model_id = "simulated-no-model"
    model_version = "simulated-no-model-v1"

    def review(
        self,
        context: BackendIncidentDayGeometryReviewContext,
    ) -> BackendIncidentDayGeometryReviewRequest:
        decision = {
            "verdict": "abstain",
            "reason_codes": ["simulated_model_not_for_publication"],
            "evidence_refs": [],
        }
        return BackendIncidentDayGeometryReviewRequest(
            review_id=_review_id(context, decision),
            analysis_id=context.analysis_id,
            source_review_sha256=context.source_sha256,
            source_perimeter_sha256=context.source_perimeter_sha256,
            verdict="abstain",
            model_confidence=0,
            reason_codes=("simulated_model_not_for_publication",),
            evidence_refs=(),
            supervisor_mode="simulated",
            provider_run={
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "model_id": self.model_id,
                "model_version": self.model_version,
                "input_hash": context.source_sha256,
                "publication_eligible": False,
            },
            prompt_revision=PROMPT_REVISION,
        )


class BedrockIncidentDayGeometryReviewerConfig(StrictModel):
    region_name: str = Field(default="eu-west-3", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    inference_profile_id: str = Field(
        default="eu.mistral.pixtral-large-2502-v1:0",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$",
    )
    model_revision: str = Field(
        default="mistral.pixtral-large-2502-v1:0",
        min_length=3,
        max_length=255,
    )
    maximum_output_tokens: int = Field(default=1_024, ge=256, le=2_048)


_SYSTEM_PROMPT = """You review a deterministic wildfire perimeter result.
Return one JSON object only with verdict, candidate_id, model_confidence,
reason_codes, and evidence_refs. You may accept the deterministic result,
abstain, or propose exactly one supplied candidate_id. Never output, alter, or
invent coordinates. A thermal footprint is not a fire front. History is a
prior, not a veto. Prefer abstain when evidence is contradictory or incomplete.
"""


class BedrockIncidentDayGeometryReviewer:
    supervisor_mode: Literal["managed_vl"] = "managed_vl"
    provider_id = "aws-bedrock-incident-day-geometry-reviewer"
    provider_version = "1.0.1"

    def __init__(
        self,
        config: BedrockIncidentDayGeometryReviewerConfig,
        *,
        client: BedrockConverseClient,
    ) -> None:
        self.config = config
        self._client = client

    def review(
        self,
        context: BackendIncidentDayGeometryReviewContext,
    ) -> BackendIncidentDayGeometryReviewRequest:
        eligible = {
            str(item.get("candidate_id")): item
            for item in context.candidate_geometries
            if item.get("eligible_for_competing_geometry") is True
            and isinstance(item.get("candidate_id"), str)
        }
        compact_context = {
            "analysis_id": context.analysis_id,
            "fire_id": context.fire_id,
            "local_date": context.local_date.isoformat(),
            "deterministic_perimeter": _compact_model_value(
                context.deterministic_perimeter
            ),
            "candidate_geometries": [
                _compact_model_candidate(item) for item in context.candidate_geometries
            ],
            "evidence_summary": _compact_model_value(context.evidence_summary),
            "prior_daily_states": _compact_model_value(context.prior_daily_states),
            "allowed_competing_candidate_ids": sorted(eligible),
            "published_reference_accessed": False,
            "geometry_mutation_allowed": False,
        }
        try:
            response = self._client.converse(
                modelId=self.config.inference_profile_id,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": json.dumps(
                                    compact_context,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                )
                            }
                        ],
                    }
                ],
                inferenceConfig={
                    "maxTokens": self.config.maximum_output_tokens,
                    "temperature": 0,
                },
            )
        except MultimodalEvidenceProviderError:
            raise
        except (BotoCoreError, ClientError) as exc:
            raise IncidentDayGeometryReviewerError(
                "bedrock_geometry_review_request_failed"
            ) from exc
        try:
            blocks = response["output"]["message"]["content"]
            text = "\n".join(
                str(block["text"])
                for block in blocks
                if isinstance(block, Mapping) and isinstance(block.get("text"), str)
            )
            decision = IncidentDayGeometryReviewDecision.model_validate(_json_object(text))
            stop_reason = str(response.get("stopReason", ""))
        except IncidentDayGeometryReviewerError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IncidentDayGeometryReviewerError(
                "bedrock_geometry_review_invalid_response"
            ) from exc
        if stop_reason not in {"end_turn", "stop_sequence"}:
            raise IncidentDayGeometryReviewerError(
                f"bedrock_geometry_review_incomplete:{stop_reason or 'unknown'}"
            )

        candidate = eligible.get(decision.candidate_id or "")
        if decision.verdict == "propose" and candidate is None:
            raise IncidentDayGeometryReviewerError(
                "bedrock_geometry_review_unknown_or_ineligible_candidate"
            )
        allowed_refs = {
            str(item.get("candidate_id"))
            for item in context.candidate_geometries
            if isinstance(item.get("candidate_id"), str)
        }
        if set(decision.evidence_refs) - allowed_refs:
            raise IncidentDayGeometryReviewerError(
                "bedrock_geometry_review_unknown_evidence"
            )
        evidence_refs = tuple(decision.evidence_refs)
        proposal = None
        if candidate is not None:
            candidate_id = str(candidate["candidate_id"])
            if candidate_id not in evidence_refs:
                evidence_refs = (*evidence_refs, candidate_id)
            proposal = BackendGeometryCorrectionProposal(
                correction_id=f"GEOMETRY-CORRECTION-{_canonical_sha256(candidate)[:24]}",
                incident_id=context.fire_id,
                local_date=context.local_date,
                source_perimeter_sha256=context.source_perimeter_sha256,
                competing_geometry_geojson=dict(candidate["geometry_geojson"]),
                reason_codes=decision.reason_codes,
                evidence_refs=(candidate_id,),
            )

        response_sha256 = _canonical_sha256(response)
        decision_payload = decision.model_dump(mode="json")
        return BackendIncidentDayGeometryReviewRequest(
            review_id=_review_id(context, decision_payload),
            analysis_id=context.analysis_id,
            source_review_sha256=context.source_sha256,
            source_perimeter_sha256=context.source_perimeter_sha256,
            verdict=decision.verdict,
            model_confidence=decision.model_confidence,
            reason_codes=decision.reason_codes,
            evidence_refs=evidence_refs,
            supervisor_mode="managed_vl",
            provider_run={
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "model_id": self.config.inference_profile_id,
                "model_version": self.config.model_revision,
                "input_hash": context.source_sha256,
                "response_sha256": response_sha256,
                "publication_eligible_before_calibration": False,
            },
            prompt_revision=PROMPT_REVISION,
            proposal=proposal,
        )


__all__ = [
    "PROMPT_REVISION",
    "BedrockIncidentDayGeometryReviewer",
    "BedrockIncidentDayGeometryReviewerConfig",
    "IncidentDayGeometryReviewer",
    "IncidentDayGeometryReviewerError",
    "SimulatedIncidentDayGeometryReviewer",
]
