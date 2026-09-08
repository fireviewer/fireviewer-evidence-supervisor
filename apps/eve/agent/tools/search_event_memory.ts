import { defineTool } from "eve/tools";
import { z } from "zod";

import { postSupervisionApi } from "../lib/supervision-api.js";

export default defineTool({
  description:
    "Search the read-only temporal and spatial EventEvidence memory for one fire event.",
  inputSchema: z.object({
    eventId: z.string().min(1).max(128),
    query: z.string().min(1).max(2_000),
    limit: z.number().int().min(1).max(64).default(12),
  }),
  async execute({ eventId, query, limit }) {
    return postSupervisionApi("/v1/events/search", {
      event_id: eventId,
      text: query,
      limit,
    });
  },
});
