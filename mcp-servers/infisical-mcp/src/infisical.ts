import { getToken } from "./auth.js";

const host = process.env.INFISICAL_HOST ?? "https://app.infisical.com";

// The shape Infisical returns for a single secret
interface RawSecret {
  secretKey: string;
  secretValue: string;
}

async function authedFetch(path: string, params: Record<string, string>): Promise<Response> {
  const token = await getToken();
  const url = new URL(`${host}${path}`);

  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, value);
  }

  const response = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });

  // If the token was rejected, clear it and retry once with a fresh token.
  // This handles the case where the token expired between our check and the request.
  if (response.status === 401) {
    const { getToken: _refresh } = await import("./auth.js");
    // Force a fresh login by making getToken re-authenticate
    const freshToken = await _refresh();
    return fetch(url.toString(), {
      headers: { Authorization: `Bearer ${freshToken}` },
    });
  }

  return response;
}

export async function getSecret(
  secretName: string,
  projectSlug: string,
  environment: string
): Promise<string> {
  const response = await authedFetch(
    `/api/v3/secrets/raw/${encodeURIComponent(secretName)}`,
    { workspaceSlug: projectSlug, environment }
  );

  if (!response.ok) {
    const text = await response.text();
    throw new Error(
      `Failed to get secret "${secretName}" (${response.status}): ${text}`
    );
  }

  const data = (await response.json()) as { secret: RawSecret };
  return data.secret.secretValue;
}

export async function listSecrets(
  projectSlug: string,
  environment: string
): Promise<string[]> {
  const response = await authedFetch("/api/v3/secrets/raw", {
    workspaceSlug: projectSlug,
    environment,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(
      `Failed to list secrets (${response.status}): ${text}`
    );
  }

  const data = (await response.json()) as { secrets: RawSecret[] };
  // Return key names only — values are never exposed through list
  return data.secrets.map((s) => s.secretKey);
}
