import { defineTool } from "eve/tools";
import { z } from "zod";

import { postSupervisionApi } from "../lib/supervision-api.js";

export default defineTool({
  description:
    "Build a bounded read-only PointEvidenceBundle and run the configured point supervisor.",
  inputSchema: z.object({
    eventId: z.string().min(1).max(128),
    candidateId: z.string().min(1).max(128),
    query: z.string().min(1).max(2_000),
  }),
  async execute({ eventId, candidateId, query }) {
    const bundle = await postSupervisionApi("/v1/points/bundle", {
      event_id: eventId,
      candidate_id: candidateId,
      query_text: query,
      max_context_documents: 12,
    });
    if (typeof bundle !== "object" || bundle === null) {
      throw new Error("The supervision API returned an invalid point bundle");
    }
    return postSupervisionApi(
      "/v1/point-assessments",
      bundle as Record<string, unknown>,
    );
  },
});
