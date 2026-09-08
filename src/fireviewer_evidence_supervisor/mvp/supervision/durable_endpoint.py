from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, cast

from pydantic import ValidationError

from fireviewer_contracts.mvp.contracts import PointEvidenceBundleV1
from fireviewer_contracts.mvp.supervision.backend_event_evidence import (
    AzureBackendIncidentDayGeometryReviewAdapter,
    BackendEventEvidenceError,
    BackendEventEvidenceNotFoundError,
    BackendPointAssessmentPublisher,
    EventEvidenceRepository,
)
from fireviewer_evidence_supervisor.mvp.supervision.event_rag import EventRagIndex, EventRagQuery
from fireviewer_evidence_supervisor.mvp.supervision.incident_day_geometry_review import (
    IncidentDayGeometryReviewer,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_evidence import PointEvidenceAssembler
from fireviewer_contracts.mvp.supervision.point_supervisor import (
    PointSupervisor,
    PointSupervisorInputImage,
    PointSupervisorMediaRepository,
    selected_supervisor_images,
)
from fireviewer_evidence_supervisor.mvp.supervision.simulated_supervisor import SimulatedPointSupervisor

_MAX_REQUEST_BYTES = 20 * 1024 * 1024


class DurablePointSupervisionService:
    """Stateless point supervision over durable, read-only backend EventEvidence."""

    def __init__(
        self,
        repository: EventEvidenceRepository,
        *,
        supervisor: PointSupervisor | None = None,
        publisher: BackendPointAssessmentPublisher | None = None,
        geometry_review_repository: AzureBackendIncidentDayGeometryReviewAdapter | None = None,
        geometry_reviewer: IncidentDayGeometryReviewer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._assembler = PointEvidenceAssembler()
        self._supervisor = supervisor or SimulatedPointSupervisor()
        self._geometry_review_repository = geometry_review_repository
        self._geometry_reviewer = geometry_reviewer

    @property
    def assessment_sink_enabled(self) -> bool:
        return self._publisher is not None

    @property
    def supervisor_mode(self) -> str:
        return self._supervisor.supervisor_mode

    @property
    def geometry_review_enabled(self) -> bool:
        return self._geometry_review_repository is not None and self._geometry_reviewer is not None

    def search_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        query = EventRagQuery.model_validate(payload)
        durable = self._repository.read(query.event_id)
        rag = EventRagIndex.from_event(
            durable.event,
            prior_fire_states=durable.prior_fire_states,
        )
        return {
            "event_id": query.event_id,
            "documents": [item.model_dump(mode="json") for item in rag.search(query)],
            "persistent": True,
            "source": "azure_backend_event_evidence",
            "source_revision_sha256": durable.source_revision_sha256,
        }

    def bundle_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        event_id = str(payload.get("event_id", ""))
        candidate_id = str(payload.get("candidate_id", ""))
        durable = self._repository.read(event_id)
        bundle = self._assembler.assemble(
            durable.event,
            candidate_id=candidate_id,
            upload_locations=durable.upload_locations,
            prior_fire_states=durable.prior_fire_states,
            geospatial_checks=durable.checks_for(candidate_id),
            generated_at=self._clock(),
            query_text=str(
                payload.get(
                    "query_text",
                    "preuves visuelles satellite géographiques historique du feu",
                )
            ),
            max_context_documents=int(payload.get("max_context_documents", 12)),
            source_revision_sha256=durable.source_revision_sha256,
        )
        return bundle.model_dump(mode="json")

    def assess_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        bundle = PointEvidenceBundleV1.model_validate(payload)
        durable = self._repository.read(bundle.event_id)
        if durable.source_revision_sha256 != bundle.source_event_evidence_sha256:
            raise BackendEventEvidenceError(
                "point bundle references a stale EventEvidence revision"
            )
        media_repository = (
            cast(PointSupervisorMediaRepository, self._repository)
            if hasattr(self._repository, "read_media")
            else None
        )
        images: tuple[PointSupervisorInputImage, ...] = ()
        if media_repository is not None and self._supervisor.max_images > 0:
            images = selected_supervisor_images(
                bundle=bundle,
                durable_media={
                    media.media_id: (media.kind, media.sha256) for media in durable.event.media
                },
                repository=media_repository,
                maximum_images=self._supervisor.max_images,
            )
        assessment = self._supervisor.assess(
            bundle,
            generated_at=self._clock(),
            images=images,
        )
        if self._publisher is not None:
            self._publisher.publish(
                candidate_id=bundle.event_id,
                point_bundle=bundle,
                assessment=assessment,
            )
        return assessment.model_dump(mode="json")

    def supervise_event_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        """Assess every immutable geographic candidate currently attached to an event."""

        event_id = str(payload.get("event_id", ""))
        if not event_id or len(event_id) > 128:
            raise ValueError("event_id is invalid")
        durable = self._repository.read(event_id)
        candidate_ids = tuple(
            sorted(item.candidate_id for item in durable.event.location_candidates)
        )
        assessments: list[dict[str, object]] = []
        for candidate_id in candidate_ids:
            # Re-read before each bundle. Persisting the previous assessment advances
            # the durable EventEvidence revision and stale bundles must fail closed.
            bundle = self.bundle_payload(
                {
                    "event_id": event_id,
                    "candidate_id": candidate_id,
                    "query_text": (
                        "preuves visuelles satellite geographiques et historique du feu"
                    ),
                    "max_context_documents": 12,
                }
            )
            assessments.append(self.assess_payload(bundle))
        return {
            "schema": "fireviewer.event-point-supervision-receipt.v1",
            "candidate_id": event_id,
            "location_candidate_count": len(candidate_ids),
            "assessment_count": len(assessments),
            "accepted_count": sum(item.get("verdict") == "accept" for item in assessments),
            "abstained_count": sum(item.get("verdict") == "abstain" for item in assessments),
            "rejected_count": sum(item.get("verdict") == "reject" for item in assessments),
            "status": "abstained" if not candidate_ids else "completed",
            "reason": "no_geographic_candidate" if not candidate_ids else None,
            "assessment_ids": [
                str(item["assessment_id"])
                for item in assessments
                if isinstance(item.get("assessment_id"), str)
            ],
            "raw_content_stored": False,
        }

    def review_incident_day_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        analysis_id = str(payload.get("analysis_id", ""))
        if not analysis_id or len(analysis_id) > 128:
            raise ValueError("analysis_id is invalid")
        if self._geometry_review_repository is None or self._geometry_reviewer is None:
            raise BackendEventEvidenceError("incident-day geometry review is disabled")
        context = self._geometry_review_repository.read(analysis_id)
        review = self._geometry_reviewer.review(context)
        receipt = self._geometry_review_repository.publish(
            analysis_id=analysis_id,
            review=review,
        )
        return receipt.model_dump(mode="json", by_alias=True)


def _handler_for(service: DurablePointSupervisionService) -> type[BaseHTTPRequestHandler]:
    class PointSupervisorHandler(BaseHTTPRequestHandler):
        server_version = "FireViewerDurablePointSupervisor/1.0"

        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def _write_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self.send_response(status.value)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("content-length")
            if raw_length is None:
                raise ValueError("content-length is required")
            length = int(raw_length)
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self) -> None:
            if self.path != "/health":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
                return
            self._write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "model_mode": service.supervisor_mode,
                    "evidence_mode": "azure_backend_read_only",
                    "assessment_sink": (
                        "azure_backend_enabled" if service.assessment_sink_enabled else "disabled"
                    ),
                    "incident_day_geometry_review": (
                        "enabled" if service.geometry_review_enabled else "disabled"
                    ),
                    "gpu": False,
                    "cost_usd": 0,
                    "persistent": True,
                },
            )

        def do_POST(self) -> None:
            routes: dict[str, Callable[[dict[str, Any]], dict[str, object]]] = {
                "/v1/events/search": service.search_payload,
                "/v1/events/supervise": service.supervise_event_payload,
                "/v1/points/bundle": service.bundle_payload,
                "/v1/point-assessments": service.assess_payload,
                "/v1/incident-day/geometry-review": service.review_incident_day_payload,
            }
            handler = routes.get(self.path)
            if handler is None:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
                return
            try:
                response = handler(self._read_json())
            except BackendEventEvidenceNotFoundError as exc:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "event_not_found", "event_id": str(exc.args[0])},
                )
            except BackendEventEvidenceError as exc:
                self._write_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "backend_event_evidence_unavailable", "detail": str(exc)},
                )
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_request", "detail": str(exc)},
                )
            else:
                self._write_json(HTTPStatus.OK, response)

    return PointSupervisorHandler


def create_point_supervisor_server(
    repository: EventEvidenceRepository,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    supervisor: PointSupervisor | None = None,
    publisher: BackendPointAssessmentPublisher | None = None,
    geometry_review_repository: AzureBackendIncidentDayGeometryReviewAdapter | None = None,
    geometry_reviewer: IncidentDayGeometryReviewer | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ThreadingHTTPServer:
    try:
        loopback = ip_address(host).is_loopback
    except ValueError as exc:
        raise ValueError("the point supervisor host must be a loopback IP") from exc
    if not loopback:
        raise ValueError("the point supervisor must only listen on loopback")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    service = DurablePointSupervisionService(
        repository,
        supervisor=supervisor,
        publisher=publisher,
        geometry_review_repository=geometry_review_repository,
        geometry_reviewer=geometry_reviewer,
        clock=clock,
    )
    return ThreadingHTTPServer((host, port), _handler_for(service))


__all__ = ["DurablePointSupervisionService", "create_point_supervisor_server"]
