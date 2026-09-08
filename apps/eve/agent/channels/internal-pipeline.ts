import { createHash, timingSafeEqual } from "node:crypto";

import { POST, defineChannel } from "eve/channels";

import { postSupervisionApi } from "../lib/supervision-api.js";

const MAX_REQUEST_BYTES = 4 * 1024;
const CANDIDATE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

function authorized(request: Request): boolean {
  const configured = process.env.FIREVIEWER_EVE_WORKER_TOKEN;
  const header = request.headers.get("authorization");
  if (configured === undefined || configured.length < 32 || header === null) return false;
  const prefix = "Bearer ";
  if (!header.startsWith(prefix)) return false;
  return timingSafeEqual(digest(header.slice(prefix.length)), digest(configured));
}

async function supervise(request: Request): Promise<Response> {
  if (!authorized(request)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const rawLength = request.headers.get("content-length");
  const length = rawLength === null ? Number.NaN : Number(rawLength);
  if (!Number.isInteger(length) || length <= 0 || length > MAX_REQUEST_BYTES) {
    return Response.json({ error: "invalid_content_length" }, { status: 400 });
  }
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "invalid_json" }, { status: 400 });
  }
  if (typeof payload !== "object" || payload === null) {
    return Response.json({ error: "invalid_request" }, { status: 400 });
  }
  const candidateId = (payload as Record<string, unknown>).candidate_id;
  if (typeof candidateId !== "string" || !CANDIDATE_ID.test(candidateId)) {
    return Response.json({ error: "invalid_candidate_id" }, { status: 400 });
  }
  try {
    const result = await postSupervisionApi("/v1/events/supervise", {
      event_id: candidateId,
    });
    return Response.json(result, {
      status: 200,
      headers: { "cache-control": "no-store" },
    });
  } catch (error) {
    return Response.json(
      {
        error: "point_supervision_failed",
        detail: error instanceof Error ? error.message : "unknown error",
      },
      { status: 502 },
    );
  }
}

async function reviewIncidentDay(request: Request): Promise<Response> {
  if (!authorized(request)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const rawLength = request.headers.get("content-length");
  const length = rawLength === null ? Number.NaN : Number(rawLength);
  if (!Number.isInteger(length) || length <= 0 || length > MAX_REQUEST_BYTES) {
    return Response.json({ error: "invalid_content_length" }, { status: 400 });
  }
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "invalid_json" }, { status: 400 });
  }
  if (typeof payload !== "object" || payload === null) {
    return Response.json({ error: "invalid_request" }, { status: 400 });
  }
  const analysisId = (payload as Record<string, unknown>).analysis_id;
  if (typeof analysisId !== "string" || !CANDIDATE_ID.test(analysisId)) {
    return Response.json({ error: "invalid_analysis_id" }, { status: 400 });
  }
  try {
    const result = await postSupervisionApi("/v1/incident-day/geometry-review", {
      analysis_id: analysisId,
    });
    return Response.json(result, {
      status: 200,
      headers: { "cache-control": "no-store" },
    });
  } catch (error) {
    return Response.json(
      {
        error: "incident_day_geometry_review_failed",
        detail: error instanceof Error ? error.message : "unknown error",
      },
      { status: 502 },
    );
  }
}

export default defineChannel({
  routes: [
    POST("/v1/event-evidence/supervise", supervise),
    POST("/v1/incident-day/geometry-review", reviewIncidentDay),
  ],
});
