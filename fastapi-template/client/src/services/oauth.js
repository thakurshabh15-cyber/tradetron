/**
 * Production OAuth Authentication Service (Google & Apple Sign-In).
 * Connects with backend /api/auth/oauth using verified ID tokens.
 */

import { API_BASE, setTokens } from "./apiClient";

export async function loginWithOAuth(provider, oauthToken, email = null, fullName = null) {
  try {
    const res = await fetch(`${API_BASE}/api/auth/oauth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: provider.toLowerCase(),
        token: oauthToken,
        email,
        full_name: fullName,
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `OAuth failed with ${provider}`);
    }

    const data = await res.json();
    if (data.access_token) {
      setTokens(data);
    }
    return data;
  } catch (error) {
    console.error(`[OAuth] Login failed for ${provider}:`, error);
    throw error;
  }
}

/**
 * Triggers OAuth workflow for Google / Apple
 */
export async function triggerOAuthFlow(provider) {
  if (provider === "google") {
    // If Google Identity Services script is loaded on window
    if (window.google?.accounts?.id) {
      return new Promise((resolve, reject) => {
        window.google.accounts.id.prompt((notification) => {
          if (notification.isNotDisplayed() || notification.isSkippedMoment()) {
            reject(new Error("Google Sign-In prompt closed or not displayed. Please check configuration."));
          }
        });
      });
    }
    throw new Error(
      "Google Sign-In client is not configured. Set GOOGLE_OAUTH_CLIENT_ID in your environment."
    );
  } else if (provider === "apple") {
    throw new Error("Apple Sign-In is coming soon.");
  }
  throw new Error(`Unsupported provider: ${provider}`);
}
