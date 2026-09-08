const DEFAULT_API_URL = "http://127.0.0.1:8091";

function apiBaseUrl(): URL {
  const configured = process.env.FIREVIEWER_SUPERVISION_API_URL ?? DEFAULT_API_URL;
  const parsed = new URL(configured);
  if (parsed.protocol !== "http:") {
    throw new Error("The point supervision API must use HTTP on loopback");
  }
  if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "[::1]") {
    throw new Error("The point supervision API must remain on loopback");
  }
  return parsed;
}

export async function postSupervisionApi(
  path: string,
  payload: Record<string, unknown>,
): Promise<unknown> {
  const endpoint = new URL(path, apiBaseUrl());
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(120_000),
  });
  const result: unknown = await response.json();
  if (!response.ok) {
    throw new Error(`Supervision API ${response.status}: ${JSON.stringify(result)}`);
  }
  return result;
}
