import { defineAgent } from "eve";
import { mockModel } from "eve/evals";

interface ReviewRequest {
  event_id: string;
  candidate_id: string;
  query: string;
}

function parseReviewRequest(message: string | null): ReviewRequest {
  if (message === null) throw new Error("A point review request is required");
  const parsed: unknown = JSON.parse(message);
  if (typeof parsed !== "object" || parsed === null) {
    throw new Error("The point review request must be a JSON object");
  }
  const request = parsed as Record<string, unknown>;
  if (typeof request.event_id !== "string" || typeof request.candidate_id !== "string") {
    throw new Error("event_id and candidate_id are required");
  }
  return {
    event_id: request.event_id,
    candidate_id: request.candidate_id,
    query:
      typeof request.query === "string"
        ? request.query
        : "preuves visuelles satellite géographiques et historique du feu",
  };
}

function simulatedModel() {
  return mockModel({
    modelId: "fireviewer-point-supervisor-simulated",
    provider: "fireviewer-local",
    respond: ({ lastUserMessage, toolResults }) => {
      const request = parseReviewRequest(lastUserMessage);
      const failed = toolResults.find((result) => result.isError);
      if (failed !== undefined) {
        return JSON.stringify({ error: "supervision_tool_failed", tool: failed.name });
      }
      if (!toolResults.some((result) => result.name === "search_event_memory")) {
        return {
          toolCalls: [
            {
              name: "search_event_memory",
              input: {
                eventId: request.event_id,
                query: request.query,
                limit: 12,
              },
            },
          ],
        };
      }
      const assessment = toolResults.find(
        (result) => result.name === "assess_candidate_point",
      );
      if (assessment === undefined) {
        return {
          toolCalls: [
            {
              name: "assess_candidate_point",
              input: {
                eventId: request.event_id,
                candidateId: request.candidate_id,
                query: request.query,
              },
            },
          ],
        };
      }
      return JSON.stringify(assessment.output);
    },
  });
}

export default defineAgent({
  model: simulatedModel(),
  modelContextWindowTokens: 65_536,
  limits: {
    maxInputTokensPerSession: 100_000,
    maxOutputTokensPerSession: 8_000,
    sessionTimeoutMs: 60 * 60 * 1_000,
  },
});
