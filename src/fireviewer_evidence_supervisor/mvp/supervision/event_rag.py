from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from math import asin, cos, radians, sin, sqrt

from pydantic import Field, model_validator

from fireviewer_contracts.contracts import SafeIdentifierV2, Sha256HexV2, StrictModel
from fireviewer_contracts.mvp.contracts import (
    EventEvidenceV1,
    PriorFireStateReference,
    RagContextExcerpt,
)
from fireviewer_contracts.mvp.contracts.common import TimeWindow, is_timezone_aware, validate_lon_lat

_TOKEN_PATTERN = re.compile(r"[\w-]+", flags=re.UNICODE)


def _content_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _tokens(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(text))


def _distance_m(left: tuple[float, float], right: tuple[float, float]) -> float:
    left_lon, left_lat = map(radians, left)
    right_lon, right_lat = map(radians, right)
    delta_lon = right_lon - left_lon
    delta_lat = right_lat - left_lat
    haversine = sin(delta_lat / 2) ** 2 + cos(left_lat) * cos(right_lat) * sin(
        delta_lon / 2
    ) ** 2
    return 2 * 6_371_008.8 * asin(min(1.0, sqrt(haversine)))


class EventRagDocument(StrictModel):
    document_id: SafeIdentifierV2
    event_id: SafeIdentifierV2
    evidence_type: SafeIdentifierV2
    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: tuple[SafeIdentifierV2, ...] = Field(min_length=1, max_length=128)
    observed_at: datetime | None = None
    center: tuple[float, float] | None = None
    content_sha256: Sha256HexV2

    @model_validator(mode="after")
    def validate_document(self) -> EventRagDocument:
        if self.observed_at is not None and not is_timezone_aware(self.observed_at):
            raise ValueError("RAG document observed_at must include a timezone")
        if self.center is not None:
            validate_lon_lat(self.center, label="RAG document center")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("RAG document evidence references must be unique")
        return self


class EventRagQuery(StrictModel):
    event_id: SafeIdentifierV2
    text: str = Field(min_length=1, max_length=2_000)
    time_window: TimeWindow = Field(default_factory=TimeWindow)
    center: tuple[float, float] | None = None
    radius_m: float | None = Field(default=None, gt=0, le=2_000_000, allow_inf_nan=False)
    evidence_types: tuple[SafeIdentifierV2, ...] = Field(default=(), max_length=64)
    limit: int = Field(default=12, ge=1, le=64)

    @model_validator(mode="after")
    def validate_query(self) -> EventRagQuery:
        if self.center is not None:
            validate_lon_lat(self.center, label="RAG query center")
        if (self.center is None) != (self.radius_m is None):
            raise ValueError("RAG spatial query requires both center and radius")
        if len(self.evidence_types) != len(set(self.evidence_types)):
            raise ValueError("RAG evidence type filters must be unique")
        return self


def _document(
    *,
    event_id: str,
    evidence_type: str,
    evidence_ids: tuple[str, ...],
    text: str,
    observed_at: datetime | None = None,
    center: tuple[float, float] | None = None,
) -> EventRagDocument:
    payload = {
        "event_id": event_id,
        "evidence_type": evidence_type,
        "evidence_ids": evidence_ids,
        "text": text,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "center": center,
    }
    digest = _content_sha256(payload)
    return EventRagDocument(
        document_id=f"RAG-{digest[:24]}",
        event_id=event_id,
        evidence_type=evidence_type,
        text=text,
        evidence_ids=evidence_ids,
        observed_at=observed_at,
        center=center,
        content_sha256=digest,
    )


def _documents_from_event(event: EventEvidenceV1) -> Iterable[EventRagDocument]:
    for source in event.sources:
        yield _document(
            event_id=event.event_id,
            evidence_type="source",
            evidence_ids=(source.source_id,),
            text=(
                f"Source {source.source_type} publiée par {source.publisher}; "
                f"origine indépendante {source.origin_id}."
            ),
            observed_at=source.published_at or source.retrieved_at,
        )
    for claim in event.claims:
        yield _document(
            event_id=event.event_id,
            evidence_type="claim",
            evidence_ids=(claim.claim_id, claim.source_id),
            text=f"Déclaration {claim.claim_type}: {claim.text}",
            observed_at=claim.observed_at,
        )
    for media in event.media:
        yield _document(
            event_id=event.event_id,
            evidence_type="media",
            evidence_ids=(media.media_id, media.source_id),
            text=f"Média {media.kind} {media.media_id}, groupe {media.media_group_id}.",
            observed_at=media.captured_at,
        )
    for visual_observation in event.visual_observations:
        yield _document(
            event_id=event.event_id,
            evidence_type="visual_observation",
            evidence_ids=(visual_observation.observation_id, visual_observation.media_id),
            text=(
                f"Observation visuelle {visual_observation.observation_type} sur "
                f"{visual_observation.media_id}, résultat "
                f"{visual_observation.result_reference}."
            ),
        )
    for satellite_observation in event.satellite_observations:
        satellite_evidence_ids: tuple[str, ...] = (
            satellite_observation.observation_id,
            satellite_observation.source_id,
        )
        if satellite_observation.media_id is not None:
            satellite_evidence_ids += (satellite_observation.media_id,)
        yield _document(
            event_id=event.event_id,
            evidence_type="satellite_observation",
            evidence_ids=satellite_evidence_ids,
            text=(
                f"Observation satellite {satellite_observation.observation_type}, "
                f"résultat {satellite_observation.result_reference}."
            ),
            observed_at=satellite_observation.acquired_at,
        )
    for candidate in event.location_candidates:
        candidate_evidence_ids: tuple[str, ...] = (candidate.candidate_id,)
        if candidate.source_id is not None:
            candidate_evidence_ids += (candidate.source_id,)
        if candidate.media_id is not None:
            candidate_evidence_ids += (candidate.media_id,)
        yield _document(
            event_id=event.event_id,
            evidence_type="location_candidate",
            evidence_ids=candidate_evidence_ids,
            text=(
                f"Candidat GPS {candidate.candidate_id} à {candidate.longitude:.6f}, "
                f"{candidate.latitude:.6f}, rayon {candidate.radius_m:.1f} m, "
                f"score amont {candidate.score:.3f}."
            ),
            center=(candidate.longitude, candidate.latitude),
        )
    for cluster in event.candidate_clusters:
        yield _document(
            event_id=event.event_id,
            evidence_type="candidate_cluster",
            evidence_ids=(cluster.cluster_id, *cluster.supporting_candidate_ids),
            text=(
                f"Cluster {cluster.cluster_id} avec {cluster.independent_source_count} "
                f"sources indépendantes, score {cluster.score:.3f}."
            ),
            center=cluster.center,
        )
    for contradiction in event.contradictions:
        yield _document(
            event_id=event.event_id,
            evidence_type="contradiction",
            evidence_ids=(
                contradiction.contradiction_id,
                contradiction.left_evidence_id,
                contradiction.right_evidence_id,
            ),
            text=f"Contradiction: {contradiction.description}",
        )
    for uncertainty in event.uncertainties:
        yield _document(
            event_id=event.event_id,
            evidence_type="uncertainty",
            evidence_ids=(uncertainty.uncertainty_id, uncertainty.scope_id),
            text=f"Incertitude {uncertainty.code}: {uncertainty.description}",
        )


class EventRagIndex:
    """Immutable, no-cost lexical/spatial adapter used until durable RAG is connected."""

    def __init__(self, event_id: str, documents: tuple[EventRagDocument, ...]) -> None:
        if any(document.event_id != event_id for document in documents):
            raise ValueError("RAG documents belong to different events")
        identifiers = [document.document_id for document in documents]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("RAG document identifiers must be unique")
        self.event_id = event_id
        self._documents = tuple(sorted(documents, key=lambda item: item.document_id))

    @classmethod
    def from_event(
        cls,
        event: EventEvidenceV1,
        *,
        prior_fire_states: tuple[PriorFireStateReference, ...] = (),
    ) -> EventRagIndex:
        documents = list(_documents_from_event(event))
        for state in prior_fire_states:
            documents.append(
                _document(
                    event_id=event.event_id,
                    evidence_type="prior_fire_state",
                    evidence_ids=(state.state_id,),
                    text=(
                        f"État antérieur en lecture seule {state.state_kind} "
                        f"référencé par {state.artifact_reference}."
                    ),
                    observed_at=state.observed_at,
                )
            )
        return cls(event.event_id, tuple(documents))

    @property
    def documents(self) -> tuple[EventRagDocument, ...]:
        return self._documents

    def search(self, query: EventRagQuery) -> tuple[RagContextExcerpt, ...]:
        if query.event_id != self.event_id:
            raise ValueError("RAG query targets a different event")
        query_tokens = _tokens(query.text)
        allowed_types = set(query.evidence_types)
        ranked: list[tuple[float, EventRagDocument]] = []
        for document in self._documents:
            if allowed_types and document.evidence_type not in allowed_types:
                continue
            if (
                query.time_window.from_at is not None
                and document.observed_at is not None
                and document.observed_at < query.time_window.from_at
            ):
                continue
            if (
                query.time_window.to_at is not None
                and document.observed_at is not None
                and document.observed_at > query.time_window.to_at
            ):
                continue
            spatial_score = 0.0
            if (
                query.center is not None
                and query.radius_m is not None
                and document.center is not None
            ):
                distance = _distance_m(query.center, document.center)
                if distance > query.radius_m:
                    continue
                spatial_score = 1.0 - distance / query.radius_m
            document_tokens = _tokens(document.text)
            lexical_score = (
                len(query_tokens & document_tokens) / len(query_tokens)
                if query_tokens
                else 0.0
            )
            score = min(1.0, lexical_score * 0.8 + spatial_score * 0.2)
            ranked.append((score, document))
        ranked.sort(key=lambda item: (-item[0], item[1].document_id))
        return tuple(
            RagContextExcerpt(
                document_id=document.document_id,
                evidence_type=document.evidence_type,
                text=document.text,
                score=score,
                evidence_ids=document.evidence_ids,
                observed_at=document.observed_at,
                center=document.center,
                content_sha256=document.content_sha256,
            )
            for score, document in ranked[: query.limit]
        )


__all__ = ["EventRagDocument", "EventRagIndex", "EventRagQuery"]
