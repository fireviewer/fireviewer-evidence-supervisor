"""Loopback supervision API used by the CPU-hosted Eve harness."""

from __future__ import annotations

import os
from typing import Literal, cast

from pydantic import Field, SecretStr, model_validator

from fireviewer_contracts.contracts import StrictModel
from fireviewer_evidence_ingestion.mvp.research.multimodal_evidence import (
    AzureFederatedBedrockClient,
    AzureManagedIdentityWebTokenProvider,
    BedrockConverseClient,
    InvocationLimitedBedrockClient,
)
from fireviewer_contracts.mvp.supervision.backend_event_evidence import (
    AzureBackendEventEvidenceAdapter,
    AzureBackendEventEvidenceConfig,
    AzureBackendIncidentDayGeometryReviewAdapter,
    BackendPointAssessmentPublisher,
)
from fireviewer_evidence_supervisor.mvp.supervision.bedrock_supervisor import (
    BedrockPixtralPointSupervisor,
    BedrockPixtralPointSupervisorConfig,
)
from fireviewer_evidence_supervisor.mvp.supervision.durable_endpoint import (
    create_point_supervisor_server,
)
from fireviewer_evidence_supervisor.mvp.supervision.incident_day_geometry_review import (
    BedrockIncidentDayGeometryReviewer,
    BedrockIncidentDayGeometryReviewerConfig,
    IncidentDayGeometryReviewer,
    SimulatedIncidentDayGeometryReviewer,
)
from fireviewer_contracts.mvp.supervision.point_supervisor import PointSupervisor
from fireviewer_evidence_supervisor.mvp.supervision.simulated_supervisor import SimulatedPointSupervisor


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


class PointSupervisorCpuSettings(StrictModel):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65_535)
    supervisor_mode: Literal["managed_vl", "simulated"] = "simulated"
    assessment_sink_enabled: bool = False
    bedrock_paid_invocation_enabled: bool = False
    authorized_bedrock_invocations: int = Field(default=0, ge=0, le=10_000)
    bedrock_maximum_output_tokens: int = Field(default=2_048, ge=256, le=4_096)
    managed_identity_client_id: str | None = Field(default=None, min_length=36, max_length=36)
    aws_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$",
    )
    aws_oidc_audience: str | None = Field(default=None, min_length=8, max_length=512)
    aws_region: str = Field(default="eu-west-3", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    bedrock_model_id: str = Field(
        default="eu.mistral.pixtral-large-2502-v1:0",
        min_length=3,
        max_length=256,
    )
    backend: AzureBackendEventEvidenceConfig
    geometry_backend: AzureBackendEventEvidenceConfig

    @model_validator(mode="after")
    def validate_paid_supervisor_gate(self) -> PointSupervisorCpuSettings:
        if self.supervisor_mode == "managed_vl" and not self.bedrock_paid_invocation_enabled:
            raise ValueError("managed VL requires the explicit paid Bedrock invocation gate")
        if self.supervisor_mode == "managed_vl" and self.authorized_bedrock_invocations < 1:
            raise ValueError("managed VL requires a positive invocation budget")
        return self

    @classmethod
    def from_environment(cls) -> PointSupervisorCpuSettings:
        raw_mode = os.getenv("FIREVIEWER_POINT_SUPERVISOR_MODE", "simulated")
        if raw_mode not in {"managed_vl", "simulated"}:
            raise ValueError("FIREVIEWER_POINT_SUPERVISOR_MODE is invalid")
        return cls(
            port=int(os.getenv("FIREVIEWER_SUPERVISION_PORT", "8091")),
            supervisor_mode=cast(Literal["managed_vl", "simulated"], raw_mode),
            assessment_sink_enabled=_env_bool(
                "FIREVIEWER_POINT_ASSESSMENT_SINK_ENABLED",
                _env_bool("FIREVIEWER_POINT_PUBLICATION_ENABLED", False),
            ),
            bedrock_paid_invocation_enabled=_env_bool(
                "FIREVIEWER_BEDROCK_PAID_INVOCATION_ENABLED", False
            ),
            authorized_bedrock_invocations=int(
                os.getenv("FIREVIEWER_BEDROCK_AUTHORIZED_INVOCATIONS", "0")
            ),
            bedrock_maximum_output_tokens=int(
                os.getenv("FIREVIEWER_BEDROCK_MAX_OUTPUT_TOKENS", "2048")
            ),
            managed_identity_client_id=os.getenv("AZURE_CLIENT_ID"),
            aws_role_arn=os.getenv("FIREVIEWER_BEDROCK_ROLE_ARN"),
            aws_oidc_audience=os.getenv("FIREVIEWER_AWS_OIDC_AUDIENCE"),
            aws_region=os.getenv("AWS_REGION", "eu-west-3"),
            bedrock_model_id=os.getenv(
                "FIREVIEWER_BEDROCK_MODEL_ID",
                "eu.mistral.pixtral-large-2502-v1:0",
            ),
            backend=AzureBackendEventEvidenceConfig(
                base_url=os.environ["FIREVIEWER_BACKEND_BASE_URL"],
                bearer_token=SecretStr(os.environ["FIREVIEWER_BACKEND_TOKEN"]),
                timeout_seconds=float(os.getenv("FIREVIEWER_BACKEND_TIMEOUT_SECONDS", "20")),
            ),
            geometry_backend=AzureBackendEventEvidenceConfig(
                base_url=os.environ["FIREVIEWER_BACKEND_BASE_URL"],
                bearer_token=SecretStr(
                    os.getenv(
                        "FIREVIEWER_GEOMETRY_BACKEND_TOKEN",
                        os.environ["FIREVIEWER_BACKEND_TOKEN"],
                    )
                ),
                timeout_seconds=float(os.getenv("FIREVIEWER_BACKEND_TIMEOUT_SECONDS", "20")),
            ),
        )


def _managed_client(settings: PointSupervisorCpuSettings) -> BedrockConverseClient:
    if not all(
        (
            settings.managed_identity_client_id,
            settings.aws_role_arn,
            settings.aws_oidc_audience,
        )
    ):
        raise ValueError("managed VL mode requires Azure-to-AWS federation settings")
    return InvocationLimitedBedrockClient(
            AzureFederatedBedrockClient(
                role_arn=str(settings.aws_role_arn),
                region_name=settings.aws_region,
                role_session_name="fireviewer-point-supervisor",
                web_token_provider=AzureManagedIdentityWebTokenProvider(
                    audience=str(settings.aws_oidc_audience),
                    managed_identity_client_id=str(settings.managed_identity_client_id),
                ),
            ),
            maximum_invocations=settings.authorized_bedrock_invocations,
    )


def _managed_supervisor(
    settings: PointSupervisorCpuSettings,
    *,
    client: BedrockConverseClient,
) -> BedrockPixtralPointSupervisor:
    return BedrockPixtralPointSupervisor(
        BedrockPixtralPointSupervisorConfig(
            region_name=settings.aws_region,
            inference_profile_id=settings.bedrock_model_id,
            maximum_output_tokens=settings.bedrock_maximum_output_tokens,
        ),
        client=client,
    )


def main() -> int:
    settings = PointSupervisorCpuSettings.from_environment()
    repository = AzureBackendEventEvidenceAdapter(settings.backend)
    geometry_repository = AzureBackendIncidentDayGeometryReviewAdapter(
        settings.geometry_backend
    )
    supervisor: PointSupervisor
    geometry_reviewer: IncidentDayGeometryReviewer
    if settings.supervisor_mode == "managed_vl":
        managed_client = _managed_client(settings)
        supervisor = _managed_supervisor(settings, client=managed_client)
        geometry_reviewer = BedrockIncidentDayGeometryReviewer(
            BedrockIncidentDayGeometryReviewerConfig(
                region_name=settings.aws_region,
                inference_profile_id=settings.bedrock_model_id,
                maximum_output_tokens=min(
                    settings.bedrock_maximum_output_tokens,
                    2_048,
                ),
            ),
            client=managed_client,
        )
    else:
        supervisor = SimulatedPointSupervisor()
        geometry_reviewer = SimulatedIncidentDayGeometryReviewer()
    publisher = (
        BackendPointAssessmentPublisher(settings.backend)
        if settings.assessment_sink_enabled
        else None
    )
    server = create_point_supervisor_server(
        repository,
        host=settings.host,
        port=settings.port,
        supervisor=supervisor,
        publisher=publisher,
        geometry_review_repository=geometry_repository,
        geometry_reviewer=geometry_reviewer,
    )
    print(
        "point-supervision-api ready "
        f"http://{settings.host}:{settings.port} "
        f"supervisor={settings.supervisor_mode} "
        f"assessment_sink={settings.assessment_sink_enabled}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
