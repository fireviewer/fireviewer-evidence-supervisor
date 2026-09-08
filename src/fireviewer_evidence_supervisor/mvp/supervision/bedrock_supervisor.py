from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field

from fireviewer_contracts.contracts import StrictModel
from fireviewer_contracts.mvp.contracts import (
    CompetingPointJsonV1,
    PointAssessmentV1,
    PointEvidenceBundleV1,
    ProviderRun,
)
from fireviewer_evidence_ingestion.mvp.research.multimodal_evidence import (
    BedrockConverseClient,
    MultimodalEvidenceProviderError,
)
from fireviewer_evidence_supervisor.mvp.supervision.mistral_supervisor import (
    PROMPT_VERSION,
    MistralPointDecision,
    point_supervisor_prompt,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_evidence import canonical_model_sha256
from fireviewer_contracts.mvp.supervision.point_supervisor import PointSupervisorInputImage


class BedrockPointSupervisorError(RuntimeError):
    """Bedrock returned no usable evidence-bound point decision."""


class BedrockPixtralPointSupervisorConfig(StrictModel):
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
    maximum_output_tokens: int = Field(default=2_048, ge=256, le=4_096)
    maximum_images: int = Field(default=12, ge=1, le=12)
    maximum_image_bytes: int = Field(
        default=24 * 1_024 * 1_024,
        ge=1 * 1_024 * 1_024,
        le=48 * 1_024 * 1_024,
    )


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
    raise BedrockPointSupervisorError("bedrock_point_supervisor_invalid_json")


def _image_block(image: PointSupervisorInputImage) -> dict[str, Any]:
    image_format = {
        "image/jpeg": "jpeg",
        "image/png": "png",
        "image/webp": "webp",
    }[image.content_type]
    return {"image": {"format": image_format, "source": {"bytes": image.content}}}


class BedrockPixtralPointSupervisor:
    """Final managed VL arbiter over one immutable deterministic point hypothesis."""

    supervisor_mode: Literal["managed_vl"] = "managed_vl"
    provider_id = "aws-bedrock-pixtral-point-supervisor"
    provider_version = "1.0.0"
    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        config: BedrockPixtralPointSupervisorConfig,
        *,
        client: BedrockConverseClient,
    ) -> None:
        self.config = config
        self._client = client
        self.max_images = config.maximum_images

    def assess(
        self,
        bundle: PointEvidenceBundleV1,
        *,
        generated_at: datetime,
        images: tuple[PointSupervisorInputImage, ...] = (),
    ) -> PointAssessmentV1:
        if len(images) > self.config.maximum_images:
            raise ValueError("too many images for one point assessment")
        if sum(len(image.content) for image in images) > self.config.maximum_image_bytes:
            raise ValueError("point assessment images exceed the byte budget")
        evidence_by_id = {item.evidence_id: item for item in bundle.evidence_references}
        for image in images:
            reference = evidence_by_id.get(image.media_id)
            if (
                reference is None
                or reference.evidence_type != "media"
                or reference.artifact_sha256 != image.sha256
            ):
                raise ValueError("point assessment image is outside the immutable bundle")

        bundle_sha256 = canonical_model_sha256(bundle)
        content: list[dict[str, Any]] = [
            {"text": point_supervisor_prompt(bundle)},
            {
                "text": json.dumps(
                    {"supplied_image_order": [item.media_id for item in images]},
                    separators=(",", ":"),
                )
            },
        ]
        content.extend(_image_block(image) for image in images)
        started = perf_counter()
        try:
            response = self._client.converse(
                modelId=self.config.inference_profile_id,
                messages=[{"role": "user", "content": content}],
                inferenceConfig={
                    "maxTokens": self.config.maximum_output_tokens,
                    "temperature": 0,
                },
            )
        except MultimodalEvidenceProviderError as exc:
            raise BedrockPointSupervisorError(str(exc)) from exc
        except (BotoCoreError, ClientError) as exc:
            raise BedrockPointSupervisorError("bedrock_point_supervisor_request_failed") from exc
        runtime_ms = max(0, round((perf_counter() - started) * 1_000))
        try:
            blocks = response["output"]["message"]["content"]
            text = "\n".join(
                str(block["text"])
                for block in blocks
                if isinstance(block, Mapping) and isinstance(block.get("text"), str)
            )
            decision = MistralPointDecision.model_validate(_json_object(text))
            stop_reason = str(response.get("stopReason", ""))
        except BedrockPointSupervisorError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BedrockPointSupervisorError("bedrock_point_supervisor_invalid_response") from exc
        if stop_reason not in {"end_turn", "stop_sequence"}:
            raise BedrockPointSupervisorError(
                f"bedrock_point_supervisor_incomplete:{stop_reason or 'unknown'}"
            )

        returned_ids = {
            *decision.supporting_evidence_ids,
            *decision.contradicting_evidence_ids,
        }
        if decision.competing_point is not None:
            returned_ids.update(decision.competing_point.evidence_ids)
        if not returned_ids.issubset(evidence_by_id):
            raise BedrockPointSupervisorError("bedrock_point_supervisor_unknown_evidence")

        deterministic_hard_codes = {
            check.reason_code
            for check in bundle.geospatial_checks
            if check.hard_constraint and check.status == "contradicted"
        }
        verdict = "reject" if deterministic_hard_codes else decision.verdict
        reason_codes = set(decision.reason_codes)
        if deterministic_hard_codes:
            reason_codes.add("deterministic_hard_geospatial_contradiction")
        missing_codes = {
            *bundle.missing_evidence_codes,
            *decision.missing_evidence_codes,
        }
        if not images:
            missing_codes.add("missing_model_input_images")

        competing_json = None
        if decision.competing_point is not None:
            draft = decision.competing_point
            if (
                draft.point.point_id == bundle.point.point_id
                or draft.point.source_candidate_ids != bundle.point.source_candidate_ids
            ):
                raise BedrockPointSupervisorError("bedrock_competing_point_source_invalid")
            correction_digest = sha256(
                json.dumps(
                    draft.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            competing_json = CompetingPointJsonV1(
                correction_id=f"CORRECTION-{correction_digest[:24]}",
                event_id=bundle.event_id,
                source_point_id=bundle.point.point_id,
                source_bundle_sha256=bundle_sha256,
                point=draft.point,
                reason_codes=draft.reason_codes,
                evidence_ids=draft.evidence_ids,
            )

        response_sha256 = sha256(
            json.dumps(response, default=str, sort_keys=True).encode("utf-8")
        ).hexdigest()
        usage = response.get("usage")
        usage_config: dict[str, Any] = {}
        if isinstance(usage, Mapping):
            for key in ("inputTokens", "outputTokens", "totalTokens"):
                value = usage.get(key)
                if isinstance(value, int) and value >= 0:
                    usage_config[key] = value
        assessment_digest = sha256(
            (
                f"{bundle_sha256}|{verdict}|{self.config.inference_profile_id}|{response_sha256}"
            ).encode()
        ).hexdigest()
        return PointAssessmentV1(
            assessment_id=f"ASSESSMENT-{assessment_digest[:24]}",
            event_id=bundle.event_id,
            point_id=bundle.point.point_id,
            bundle_sha256=bundle_sha256,
            verdict=verdict,
            model_confidence=decision.model_confidence,
            subscores=decision.subscores,
            reason_codes=tuple(sorted(reason_codes)),
            supporting_evidence_ids=decision.supporting_evidence_ids,
            contradicting_evidence_ids=decision.contradicting_evidence_ids,
            hard_contradiction_codes=tuple(
                sorted({*deterministic_hard_codes, *decision.hard_contradiction_codes})
            ),
            missing_evidence_codes=tuple(sorted(missing_codes)),
            competing_point_json=competing_json,
            release_status="held_for_review",
            supervisor_mode="managed_vl",
            provider_run=ProviderRun(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                model_id=self.config.inference_profile_id,
                model_version=self.config.model_revision,
                config={
                    "api": "bedrock_converse",
                    "region": self.config.region_name,
                    "image_count": len(images),
                    "response_sha256": response_sha256,
                    "publication_eligible_before_calibration": False,
                    **usage_config,
                },
                input_hash=bundle_sha256,
                runtime_ms=runtime_ms,
                cost_usd=None,
                generated_at=generated_at,
            ),
            prompt_version=self.prompt_version,
            needs_human_review=True,
        )


__all__ = [
    "BedrockPixtralPointSupervisor",
    "BedrockPixtralPointSupervisorConfig",
    "BedrockPointSupervisorError",
]
