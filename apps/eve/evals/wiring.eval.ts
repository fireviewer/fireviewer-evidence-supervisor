import { defineEval } from "eve/evals";
import { includes } from "eve/evals/expect";

const apiUrl = process.env.FIREVIEWER_SUPERVISION_API_URL ?? "http://127.0.0.1:8091";

export default defineEval({
  description: "Eve searches EventEvidence then obtains a fail-closed point assessment.",
  async test(t) {
    await t.send(
      JSON.stringify({
        event_id: "EVENT-SUPERVISION-1",
        candidate_id: "CANDIDATE-1",
        query: "satellite hotspot historique et cohérence géographique",
      }),
    );

    t.succeeded();
    t.calledTool("search_event_memory");
    t.calledTool("assess_candidate_point");
    t.check(t.reply, includes("fireviewer.point-assessment.v1"));
    t.check(t.reply, includes('"verdict":"abstain"'));
    t.check(t.reply, includes('"cost_usd":0'));
  },
});
