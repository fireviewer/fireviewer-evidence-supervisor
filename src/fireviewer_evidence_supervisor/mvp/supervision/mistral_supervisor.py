from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field, SecretStr, model_validator

from fireviewer_contracts.contracts import SafeIdentifierV2, StrictModel
from fireviewer_contracts.mvp.contracts import (
    AssessmentSubscores,
    CandidatePoint,
    CompetingPointJsonV1,
    PointAssessmentV1,
    PointEvidenceBundleV1,
    ProviderRun,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_evidence import canonical_model_sha256

MISTRAL_SMALL_4_MODEL_ID: Literal["mistral-small-2603"] = "mistral-small-2603"
MISTRAL_API_URL: Literal[
    "https://api.mistral.ai/v1/conversations"
] = "https://api.mistral.ai/v1/conversations"
PROMPT_VERSION = "fireviewer-point-evidence-assessor-v2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MistralSupervisorError(RuntimeError):
    """Mistral could not return a validated, evidence-bound decision."""


class MistralCompetingPointDraft(StrictModel):
    point: CandidatePoint
    reason_codes: tuple[SafeIdentifierV2, ...] = Field(min_length=1, max_length=128)
    evidence_ids: tuple[SafeIdentifierV2, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def validate_unique_values(self) -> MistralCompetingPointDraft:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("competing point reason codes must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("competing point evidence references must be unique")
        return self


class MistralPointDecision(StrictModel):
    verdict: Literal["accept", "reject", "abstain"]
    model_confidence: float = Field(ge=0, le=1)
    subscores: AssessmentSubscores = Field(default_factory=AssessmentSubscores)
    reason_codes: tuple[SafeIdentifierV2, ...] = Field(min_length=1, max_length=128)
    supporting_evidence_ids: tuple[SafeIdentifierV2, ...] = Field(
        default=(),
        max_length=512,
    )
    contradicting_evidence_ids: tuple[SafeIdentifierV2, ...] = Field(
        default=(),
        max_length=512,
    )
    hard_contradiction_codes: tuple[SafeIdentifierV2, ...] = Field(
        default=(),
        max_length=128,
    )
    missing_evidence_codes: tuple[SafeIdentifierV2, ...] = Field(
        default=(),
        max_length=128,
    )
    competing_point: MistralCompetingPointDraft | None = None

    @model_validator(mode="after")
    def validate_unique_values(self) -> MistralPointDecision:
        for label, values in (
            ("reason code", self.reason_codes),
            ("supporting evidence", self.supporting_evidence_ids),
            ("contradicting evidence", self.contradicting_evidence_ids),
            ("hard contradiction", self.hard_contradiction_codes),
            ("missing evidence", self.missing_evidence_codes),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")
        if set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("evidence cannot be both supporting and contradicting")
        return self
class MistralPointSupervisorConfig(StrictModel):
    api_key: SecretStr = Field(min_length=16, max_length=4_096)
    agent_id: SafeIdentifierV2
    model_id: Literal["mistral-small-2603"] = MISTRAL_SMALL_4_MODEL_ID
    api_url: Literal["https://api.mistral.ai/v1/conversations"] = MISTRAL_API_URL
    timeout_seconds: float = Field(default=90, ge=10, le=180)
    max_output_tokens: int = Field(default=2_048, ge=256, le=8_192)
    max_images: int = Field(default=12, ge=1, le=12)
    max_image_payload_bytes: int = Field(
        default=24 * 1024 * 1024,
        ge=1 * 1024 * 1024,
        le=48 * 1024 * 1024,
    )

    @classmethod
    def from_environment(cls) -> MistralPointSupervisorConfig:
        api_key = os.getenv("MISTRAL_API_KEY", "")
        agent_id = os.getenv("MISTRAL_FIREVIEWER_AGENT_ID", "")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY is required for the Mistral supervisor")
        if not agent_id:
            raise ValueError(
                "MISTRAL_FIREVIEWER_AGENT_ID is required for the customized agent"
            )
        return cls(api_key=SecretStr(api_key), agent_id=agent_id)


@dataclass(frozen=True, slots=True)
class MistralJsonResponse:
    payload: dict[str, Any]
    headers: Mapping[str, str]


class MistralSupervisorTransport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> MistralJsonResponse: ...


class HttpxMistralSupervisorTransport:
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> MistralJsonResponse:
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_seconds, connect=10),
                trust_env=False,
            ) as client, client.stream(
                "POST",
                url,
                headers=dict(headers),
                json=dict(payload),
            ) as response:
                if response.is_redirect:
                    raise MistralSupervisorError("Mistral API redirects are not allowed")
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise MistralSupervisorError(
                            "Mistral API response exceeds the size limit"
                        )
                response_headers = dict(response.headers)
        except MistralSupervisorError:
            raise
        except httpx.HTTPError as exc:
            raise MistralSupervisorError("Mistral API request failed") from exc
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MistralSupervisorError("Mistral API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise MistralSupervisorError("Mistral API response must be a JSON object")
        return MistralJsonResponse(payload=decoded, headers=response_headers)


def _message_text(response: Mapping[str, Any]) -> str:
    outputs = response.get("outputs")
    if not isinstance(outputs, list):
        raise MistralSupervisorError("Mistral response has no outputs")
    for output in reversed(outputs):
        if not isinstance(output, dict):
            continue
        output_type = output.get("type")
        if output_type not in {None, "message.output"}:
            continue
        content = output.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
            if texts:
                return "".join(texts)
    raise MistralSupervisorError("Mistral response has no final message output")


def _validate_image_data_urls(
    values: tuple[str, ...],
    config: MistralPointSupervisorConfig,
) -> None:
    if len(values) > config.max_images:
        raise ValueError("too many images for one point assessment")
    total_bytes = sum(len(item.encode("utf-8")) for item in values)
    if total_bytes > config.max_image_payload_bytes:
        raise ValueError("point assessment images exceed the payload limit")
    allowed_prefixes = (
        "data:image/jpeg;base64,",
        "data:image/png;base64,",
        "data:image/webp;base64,",
    )
    if any(not item.startswith(allowed_prefixes) for item in values):
        raise ValueError("point assessment images must be bounded image data URLs")


def point_supervisor_prompt(bundle: PointEvidenceBundleV1) -> str:
    compact_bundle = json.dumps(
        bundle.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "Évalue uniquement la cohérence des preuves autour du point GPS déjà calculé "
        "par la chaîne de spatialisation déterministe et fourni dans ce "
        "PointEvidenceBundle. "
        "Ne génère, ne déplace et ne complète aucune coordonnée, carte ou géométrie. "
        "Recoupe les preuves visuelles, la prise de vue, le satellite, le relief, "
        "les textes et l'historique. L'historique est un prior et non un veto absolu "
        "car une saute de feu reste possible. Utilise exclusivement les evidence_id "
        "présents. En cas de contradiction ou de preuve insuffisante, réponds abstain. "
        "Tu peux accepter, rejeter ou t'abstenir, classer la solidité des preuves et "
        "demander un nouveau calcul géographique au moyen des reason_codes. Le verdict peut "
        "piloter le tri interne du pipeline, mais il ne modifie pas directement la géométrie "
        "et n'autorise aucune publication. Si une correction est nécessaire, retourne un seul "
        "competing_point : un JSON candidat complet, avec un nouvel identifiant, placé en "
        "concurrence avec le point source sans jamais l'écraser. "
        "La politique déterministe décidera ensuite entre publication automatique au-dessus "
        "du seuil calibré et revue humaine. "
        "La confiance demandée est celle du modèle, jamais une confiance calibrée. "
        "Réponds uniquement selon le schéma JSON imposé.\nPOINT_EVIDENCE_BUNDLE="
        + compact_bundle
    )


class MistralPointSupervisor:
    provider_id = "mistral-agents-api"
    provider_version = "1.0.0"
    prompt_version = PROMPT_VERSION
    mode = "mistral_api_free_tier"

    def __init__(
        self,
        config: MistralPointSupervisorConfig,
        *,
        transport: MistralSupervisorTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpxMistralSupervisorTransport()

    def assess(
        self,
        bundle: PointEvidenceBundleV1,
        *,
        generated_at: datetime,
        image_data_urls: tuple[str, ...] = (),
    ) -> PointAssessmentV1:
        _validate_image_data_urls(image_data_urls, self._config)
        bundle_sha256 = canonical_model_sha256(bundle)
        content: list[dict[str, str]] = [
            {"type": "text", "text": point_supervisor_prompt(bundle)}
        ]
        content.extend(
            {"type": "image_url", "image_url": image_data_url}
            for image_data_url in image_data_urls
        )
        schema = MistralPointDecision.model_json_schema()
        request_payload = {
            "agent_id": self._config.agent_id,
            "inputs": [{"role": "user", "content": content}],
            "store": False,
            "stream": False,
            "handoff_execution": "server",
            "completion_args": {
                "temperature": 0,
                "max_tokens": self._config.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "fireviewer_point_decision",
                        "schema": schema,
                        "strict": True,
                    },
                },
            },
        }
        started = perf_counter()
        response = self._transport.post_json(
            self._config.api_url,
            headers={
                "Accept": "application/json",
                "Authorization": (
                    "Bearer " + self._config.api_key.get_secret_value()
                ),
                "Content-Type": "application/json",
                "User-Agent": "FireViewer-PointSupervisor/1.0",
            },
            payload=request_payload,
            timeout_seconds=self._config.timeout_seconds,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        runtime_ms = max(0, round((perf_counter() - started) * 1_000))
        try:
            decision = MistralPointDecision.model_validate_json(
                _message_text(response.payload)
            )
        except (ValueError, TypeError) as exc:
            raise MistralSupervisorError(
                "Mistral decision does not match the frozen schema"
            ) from exc
        evidence_ids = {item.evidence_id for item in bundle.evidence_references}
        returned_ids = {
            *decision.supporting_evidence_ids,
            *decision.contradicting_evidence_ids,
        }
        if decision.competing_point is not None:
            returned_ids.update(decision.competing_point.evidence_ids)
        if not returned_ids.issubset(evidence_ids):
            raise MistralSupervisorError(
                "Mistral decision references evidence outside the point bundle"
            )
        deterministic_hard_codes = {
            item.reason_code
            for item in bundle.geospatial_checks
            if item.hard_constraint and item.status == "contradicted"
        }
        hard_codes = tuple(
            sorted({*deterministic_hard_codes, *decision.hard_contradiction_codes})
        )
        missing_codes = {
            *bundle.missing_evidence_codes,
            *decision.missing_evidence_codes,
        }
        if not image_data_urls:
            missing_codes.add("missing_model_input_images")
        verdict = decision.verdict
        if deterministic_hard_codes:
            verdict = "reject"
        reason_codes = set(decision.reason_codes)
        if deterministic_hard_codes:
            reason_codes.add("deterministic_hard_geospatial_contradiction")
        competing_json = None
        if decision.competing_point is not None:
            draft = decision.competing_point
            if draft.point.point_id == bundle.point.point_id:
                raise MistralSupervisorError(
                    "Mistral competing point must use a distinct point identifier"
                )
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
        assessment_digest = sha256(
            (
                f"{bundle_sha256}|{verdict}|{self._config.model_id}|"
                f"{self._config.agent_id}|{json.dumps(response.payload, sort_keys=True)}"
            ).encode()
        ).hexdigest()
        usage = response.payload.get("usage")
        usage_config: dict[str, Any] = {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and value >= 0:
                    usage_config[key] = value
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
            hard_contradiction_codes=hard_codes,
            missing_evidence_codes=tuple(sorted(missing_codes)),
            competing_point_json=competing_json,
            release_status="held_for_review",
            supervisor_mode="managed_vl",
            provider_run=ProviderRun(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                model_id=self._config.model_id,
                model_version=self._config.model_id,
                config={
                    "agent_id": self._config.agent_id,
                    "api": "mistral_conversations",
                    "store": False,
                    "requested_quota_mode": "studio_free",
                    "billing_status_verified_by_response": False,
                    "image_count": len(image_data_urls),
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
    "MISTRAL_API_URL",
    "MISTRAL_SMALL_4_MODEL_ID",
    "PROMPT_VERSION",
    "HttpxMistralSupervisorTransport",
    "MistralCompetingPointDraft",
    "MistralJsonResponse",
    "MistralPointDecision",
    "MistralPointSupervisor",
    "MistralPointSupervisorConfig",
    "MistralSupervisorError",
    "MistralSupervisorTransport",
    "point_supervisor_prompt",
]
