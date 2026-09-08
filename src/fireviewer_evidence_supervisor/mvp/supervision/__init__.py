"""Read-only point supervision over EventEvidence without perimeter generation."""

from importlib import import_module
from typing import Any

from fireviewer_contracts.mvp.supervision.backend_event_evidence import (
    AzureBackendEventEvidenceAdapter,
    AzureBackendEventEvidenceConfig,
    BackendBinaryResponse,
    BackendDerivedKeyframeReceipt,
    BackendEventEvidenceError,
    BackendEventEvidenceNotFoundError,
    BackendEventEvidenceSnapshot,
    BackendEvidenceMediaLocation,
    BackendGeographicEvidencePublisher,
    BackendGeographicEvidenceReceipt,
    BackendIncidentDayMediaAnalysisPublisher,
    BackendJsonResponse,
    BackendKeyframeEvidencePublisher,
    BackendPointAssessmentPublisher,
    BackendPointAssessmentReceipt,
    BackendResearchEvidencePublisher,
    BackendResearchEvidenceReceipt,
    BackendResearchMediaAnalysisReceipt,
    BackendVisualEvidencePublisher,
    BackendVisualEvidenceReceipt,
    DurableEventEvidence,
    DurableResearchProgress,
    DurableTerrainReference,
    EventEvidenceRepository,
)
from fireviewer_evidence_supervisor.mvp.supervision.durable_endpoint import (
    DurablePointSupervisionService,
    create_point_supervisor_server,
)
from fireviewer_evidence_supervisor.mvp.supervision.event_rag import (
    EventRagDocument,
    EventRagIndex,
    EventRagQuery,
)
from fireviewer_evidence_supervisor.mvp.supervision.mistral_supervisor import (
    MISTRAL_SMALL_4_MODEL_ID,
    MistralCompetingPointDraft,
    MistralJsonResponse,
    MistralPointDecision,
    MistralPointSupervisor,
    MistralPointSupervisorConfig,
    MistralSupervisorError,
)
from fireviewer_evidence_supervisor.mvp.supervision.point_evidence import (
    PointEvidenceAssembler,
    canonical_model_sha256,
)
from fireviewer_contracts.mvp.supervision.point_supervisor import (
    PointSupervisor,
    PointSupervisorInputImage,
    PointSupervisorMediaRepository,
    selected_supervisor_images,
)
from fireviewer_evidence_supervisor.mvp.supervision.publication_policy import (
    apply_point_publication_policy,
)
from fireviewer_evidence_supervisor.mvp.supervision.simulated_supervisor import SimulatedPointSupervisor

_BEDROCK_EXPORTS = frozenset(
    {
        "BedrockPixtralPointSupervisor",
        "BedrockPixtralPointSupervisorConfig",
        "BedrockPointSupervisorError",
    }
)


def __getattr__(name: str) -> Any:
    if name not in _BEDROCK_EXPORTS:
        raise AttributeError(name)
    module = import_module("fireviewer_evidence_supervisor.mvp.supervision.bedrock_supervisor")
    value = getattr(module, name)
    globals()[name] = value
    return value

__all__ = [
    "MISTRAL_SMALL_4_MODEL_ID",
    "AzureBackendEventEvidenceAdapter",
    "AzureBackendEventEvidenceConfig",
    "BackendBinaryResponse",
    "BackendDerivedKeyframeReceipt",
    "BackendEventEvidenceError",
    "BackendEventEvidenceNotFoundError",
    "BackendEventEvidenceSnapshot",
    "BackendEvidenceMediaLocation",
    "BackendGeographicEvidencePublisher",
    "BackendGeographicEvidenceReceipt",
    "BackendIncidentDayMediaAnalysisPublisher",
    "BackendJsonResponse",
    "BackendKeyframeEvidencePublisher",
    "BackendPointAssessmentPublisher",
    "BackendPointAssessmentReceipt",
    "BackendResearchEvidencePublisher",
    "BackendResearchEvidenceReceipt",
    "BackendResearchMediaAnalysisReceipt",
    "BackendVisualEvidencePublisher",
    "BackendVisualEvidenceReceipt",
    "BedrockPixtralPointSupervisor",
    "BedrockPixtralPointSupervisorConfig",
    "BedrockPointSupervisorError",
    "DurableEventEvidence",
    "DurablePointSupervisionService",
    "DurableResearchProgress",
    "DurableTerrainReference",
    "EventEvidenceRepository",
    "EventRagDocument",
    "EventRagIndex",
    "EventRagQuery",
    "MistralCompetingPointDraft",
    "MistralJsonResponse",
    "MistralPointDecision",
    "MistralPointSupervisor",
    "MistralPointSupervisorConfig",
    "MistralSupervisorError",
    "PointEvidenceAssembler",
    "PointSupervisor",
    "PointSupervisorInputImage",
    "PointSupervisorMediaRepository",
    "SimulatedPointSupervisor",
    "apply_point_publication_policy",
    "canonical_model_sha256",
    "create_point_supervisor_server",
    "selected_supervisor_images",
]
