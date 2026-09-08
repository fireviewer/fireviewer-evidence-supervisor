from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel

from fireviewer_contracts.mvp.contracts import (
    CandidatePoint,
    EventEvidenceV1,
    GeographicReference,
    GeospatialConsistencyCheck,
    PointEvidenceBundleV1,
    PointEvidenceReference,
    PriorFireStateReference,
    ProviderRun,
    UploadLocationEvidence,
)
from fireviewer_evidence_supervisor.mvp.supervision.event_rag import EventRagIndex, EventRagQuery


def canonical_model_sha256(model: BaseModel) -> str:
    payload = model.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _reference_catalog(
    event: EventEvidenceV1,
    prior_fire_states: tuple[PriorFireStateReference, ...],
    geographic_references: tuple[GeographicReference, ...],
) -> dict[str, PointEvidenceReference]:
    references: dict[str, PointEvidenceReference] = {}

    def add(reference: PointEvidenceReference) -> None:
        existing = references.get(reference.evidence_id)
        if existing is not None and existing != reference:
            raise ValueError(f"conflicting evidence reference {reference.evidence_id}")
        references[reference.evidence_id] = reference

    for source in event.sources:
        add(
            PointEvidenceReference(
                evidence_id=source.source_id,
                evidence_type="source",
                source_id=source.source_id,
            )
        )
    for claim in event.claims:
        add(
            PointEvidenceReference(
                evidence_id=claim.claim_id,
                evidence_type="claim",
                source_id=claim.source_id,
            )
        )
    for media in event.media:
        add(
            PointEvidenceReference(
                evidence_id=media.media_id,
                evidence_type="media",
                source_id=media.source_id,
                media_id=media.media_id,
                artifact_sha256=media.sha256,
            )
        )
    for visual_observation in event.visual_observations:
        add(
            PointEvidenceReference(
                evidence_id=visual_observation.observation_id,
                evidence_type="visual_observation",
                media_id=visual_observation.media_id,
                result_reference=visual_observation.result_reference,
            )
        )
    for satellite_observation in event.satellite_observations:
        add(
            PointEvidenceReference(
                evidence_id=satellite_observation.observation_id,
                evidence_type="satellite_observation",
                source_id=satellite_observation.source_id,
                media_id=satellite_observation.media_id,
                result_reference=satellite_observation.result_reference,
            )
        )
    for candidate in event.location_candidates:
        add(
            PointEvidenceReference(
                evidence_id=candidate.candidate_id,
                evidence_type="location_candidate",
                source_id=candidate.source_id,
                media_id=candidate.media_id,
                result_reference=candidate.reference_id,
            )
        )
    for cluster in event.candidate_clusters:
        add(
            PointEvidenceReference(
                evidence_id=cluster.cluster_id,
                evidence_type="candidate_cluster",
            )
        )
    for contradiction in event.contradictions:
        add(
            PointEvidenceReference(
                evidence_id=contradiction.contradiction_id,
                evidence_type="contradiction",
            )
        )
    for uncertainty in event.uncertainties:
        add(
            PointEvidenceReference(
                evidence_id=uncertainty.uncertainty_id,
                evidence_type="uncertainty",
            )
        )
    for state in prior_fire_states:
        add(
            PointEvidenceReference(
                evidence_id=state.state_id,
                evidence_type="prior_fire_state",
                artifact_sha256=state.artifact_sha256,
            )
        )
    for reference in geographic_references:
        if reference.reference_id not in references:
            add(
                PointEvidenceReference(
                    evidence_id=reference.reference_id,
                    evidence_type="geographic_reference",
                )
            )
    return references


class PointEvidenceAssembler:
    provider_id = "point-evidence-assembler"
    provider_version = "1.0.0"

    def assemble(
        self,
        event: EventEvidenceV1,
        *,
        candidate_id: str,
        upload_locations: tuple[UploadLocationEvidence, ...] = (),
        prior_fire_states: tuple[PriorFireStateReference, ...] = (),
        geographic_references: tuple[GeographicReference, ...] = (),
        geospatial_checks: tuple[GeospatialConsistencyCheck, ...] = (),
        generated_at: datetime,
        query_text: str = "preuves visuelles satellite géographiques historique du feu",
        max_context_documents: int = 12,
        source_revision_sha256: str | None = None,
        phenomenon: Literal["active_fire_point", "smoke_origin"] | None = None,
    ) -> PointEvidenceBundleV1:
        candidates = {
            candidate.candidate_id: candidate for candidate in event.location_candidates
        }
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(f"unknown location candidate {candidate_id}")
        media_ids = {media.media_id for media in event.media}
        if any(location.media_id not in media_ids for location in upload_locations):
            raise ValueError("upload location references media outside EventEvidence")

        rag = EventRagIndex.from_event(event, prior_fire_states=prior_fire_states)
        context = rag.search(
            EventRagQuery(
                event_id=event.event_id,
                text=f"{query_text} {candidate.candidate_id}",
                center=(candidate.longitude, candidate.latitude),
                radius_m=max(candidate.radius_m * 20, 10_000),
                limit=max_context_documents,
            )
        )
        catalog = _reference_catalog(event, prior_fire_states, geographic_references)
        selected_ids = {candidate.candidate_id}
        if candidate.source_id is not None:
            selected_ids.add(candidate.source_id)
        if candidate.media_id is not None:
            selected_ids.add(candidate.media_id)
        selected_ids.update(location.media_id for location in upload_locations)
        selected_ids.update(state.state_id for state in prior_fire_states)
        selected_ids.update(
            reference.reference_id for reference in geographic_references
        )
        selected_ids.update(
            evidence_id for check in geospatial_checks for evidence_id in check.evidence_ids
        )
        selected_ids.update(evidence_id for item in context for evidence_id in item.evidence_ids)

        for visual_observation in event.visual_observations:
            if visual_observation.media_id in selected_ids:
                selected_ids.add(visual_observation.observation_id)
        for claim in event.claims:
            if claim.source_id in selected_ids:
                selected_ids.add(claim.claim_id)
        for satellite_observation in event.satellite_observations:
            if (
                satellite_observation.media_id in selected_ids
                or satellite_observation.source_id in selected_ids
            ):
                selected_ids.add(satellite_observation.observation_id)
        for cluster in event.candidate_clusters:
            if candidate.candidate_id in cluster.supporting_candidate_ids:
                selected_ids.add(cluster.cluster_id)
        selected_evidence = tuple(
            catalog[evidence_id]
            for evidence_id in sorted(selected_ids)
            if evidence_id in catalog
        )
        missing_reference_ids = selected_ids - set(catalog)
        if missing_reference_ids:
            raise ValueError(
                "point evidence inputs reference unknown evidence: "
                + ", ".join(sorted(missing_reference_ids))
            )

        missing_codes: set[str] = set()
        if not upload_locations:
            missing_codes.add("missing_upload_location")
        check_types = {check.check_type for check in geospatial_checks}
        if not {"camera_distance", "camera_bearing"}.issubset(check_types):
            missing_codes.add("missing_camera_geo_checks")
        if not prior_fire_states:
            missing_codes.add("missing_prior_fire_state")
        evidence_types = {reference.evidence_type for reference in selected_evidence}
        has_satellite_reference = any(
            reference.reference_kind
            in {"satellite_hotspot", "satellite_active_area"}
            for reference in geographic_references
        )
        if "satellite_observation" not in evidence_types and not has_satellite_reference:
            missing_codes.add("missing_satellite_evidence")
        if "visual_observation" not in evidence_types:
            missing_codes.add("missing_visual_evidence")

        event_sha256 = canonical_model_sha256(event)
        input_payload = {
            "event_sha256": event_sha256,
            "source_revision_sha256": source_revision_sha256,
            "candidate_id": candidate.candidate_id,
            "upload_locations": [
                item.model_dump(mode="json") for item in upload_locations
            ],
            "prior_fire_states": [
                item.model_dump(mode="json") for item in prior_fire_states
            ],
            "geographic_references": [
                item.model_dump(mode="json") for item in geographic_references
            ],
            "geospatial_checks": [
                item.model_dump(mode="json") for item in geospatial_checks
            ],
            "retrieved_context": [item.model_dump(mode="json") for item in context],
        }
        input_hash = sha256(
            json.dumps(
                input_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        point_id_digest = sha256(candidate.candidate_id.encode("utf-8")).hexdigest()
        point = CandidatePoint(
            point_id=f"POINT-{point_id_digest[:24]}",
            phenomenon=phenomenon or "active_fire_point",
            longitude=candidate.longitude,
            latitude=candidate.latitude,
            radius_m=candidate.radius_m,
            source_candidate_ids=(candidate.candidate_id,),
        )
        return PointEvidenceBundleV1(
            bundle_id=f"BUNDLE-{input_hash[:24]}",
            event_id=event.event_id,
            point=point,
            upload_locations=upload_locations,
            evidence_references=selected_evidence,
            prior_fire_states=prior_fire_states,
            geographic_references=geographic_references,
            geospatial_checks=geospatial_checks,
            retrieved_context=context,
            missing_evidence_codes=tuple(sorted(missing_codes)),
            source_event_evidence_sha256=source_revision_sha256 or event_sha256,
            assembler_run=ProviderRun(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                config={
                    "max_context_documents": max_context_documents,
                    "rag_adapter": "immutable-lexical-spatial-event-evidence-v1",
                    "source_revision_sha256": source_revision_sha256,
                },
                input_hash=input_hash,
                runtime_ms=0,
                cost_usd=0,
                generated_at=generated_at,
            ),
            needs_human_review=True,
        )


__all__ = ["PointEvidenceAssembler", "canonical_model_sha256"]
