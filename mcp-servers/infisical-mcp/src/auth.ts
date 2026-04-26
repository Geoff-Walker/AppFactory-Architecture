const host = process.env.INFISICAL_HOST ?? "https://app.infisical.com";
const clientId = process.env.INFISICAL_CLIENT_ID;
const clientSecret = process.env.INFISICAL_CLIENT_SECRET;

// Cached token state — lives for the lifetime of the server process
let cachedToken: string | null = null;
let tokenExpiresAt: number = 0;

interface LoginResponse {
  accessToken: string;
  expiresIn: number; // seconds
}

async function login(): Promise<void> {
  if (!clientId || !clientSecret) {
    throw new Error(
      "INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET must be set"
    );
  }

  const response = await fetch(
    `${host}/api/v1/auth/universal-auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clientId, clientSecret }),
    }
  );

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Infisical login failed (${response.status}): ${text}`);
  }

  const data = (await response.json()) as LoginResponse;

  cachedToken = data.accessToken;
  // Subtract 30 seconds so we refresh slightly before actual expiry
  tokenExpiresAt = Date.now() + (data.expiresIn - 30) * 1000;
}

export async function getToken(): Promise<string> {
  if (!cachedToken || Date.now() >= tokenExpiresAt) {
    await login();
  }
  return cachedToken!;
}
