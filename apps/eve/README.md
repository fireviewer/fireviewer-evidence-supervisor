# FireViewer Eve point supervisor

This isolated Eve app assembles and assesses evidence for one existing spatial hypothesis at a
time. The deterministic spatial-registration chain supplies the immutable coordinates. Eve does
not create maps, polygons, perimeters, or coordinates. Its assessment may rank, filter, or request
recalculation of a hypothesis. A correction is a standalone competing point JSON with its own
identity and source hash; it never replaces the source JSON and must pass through the same
assessment. A deterministic policy permits automatic publication only for an accepted
assessment whose calibrated confidence is strictly greater than `0.85`; every other assessment is
held for human review.

Eve's deterministic `mockModel` is only the tool sequencer: it never creates a point assessment.
The loopback Python service owns the assessment provider. Local tests use the simulated provider;
the deployed CPU service uses Bedrock Pixtral through a narrowly scoped Azure-to-AWS federated
role. Starting the service and querying health do not invoke the model. The two available tools
call the loopback-only Python supervision API:

- `search_event_memory`: read-only EventEvidence RAG search;
- `assess_candidate_point`: bundle construction followed by fail-closed assessment.

All default shell, filesystem, web, delegation, todo, and question tools are disabled.

The Python service is stateless. It reads each `EventEvidence` revision from the authenticated
backend endpoint, verifies its checksum and ETag, and never indexes or writes evidence. The
backend remains the durable owner of database records and private Azure objects.

## Local integration test

Start the read-only backend test double (test fixture only):

```powershell
$env:FIREVIEWER_BACKEND_TOKEN = "test-only-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
uv run python tools/run_fake_event_evidence_backend.py --fixture eve-point-supervisor/fixtures/durable-backend-event-evidence.json
```

Then start the point supervisor in another terminal:

```powershell
$env:FIREVIEWER_BACKEND_BASE_URL = "http://127.0.0.1:8090"
$env:FIREVIEWER_BACKEND_TOKEN = "test-only-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
uv run python tools/run_simulated_point_supervisor.py
```

In another terminal:

```powershell
cd eve-point-supervisor
npm install
npm run build
npm run eval
```

The managed provider is `eu.mistral.pixtral-large-2502-v1:0` on Bedrock. It is selected only by
`FIREVIEWER_POINT_SUPERVISOR_MODE=managed_vl`; the simulated provider is labelled `simulated` in
the output contract and is never eligible for publication. Publication remains disabled until the
backend gates and calibration are explicitly enabled. These local commands do not deploy, start a
GPU, or invoke Bedrock.
