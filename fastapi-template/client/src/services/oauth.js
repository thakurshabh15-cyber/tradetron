/**
 * Production OAuth Authentication Service (Google & Apple Sign-In).
 * Connects with backend /api/auth/oauth using verified ID tokens.
 */

import { API_BASE, setTokens } from "./apiClient";

export function isGoogleOAuthConfigured() {
  return (
    typeof window !== "undefined" &&
    Boolean(window.google?.accounts?.id || import.meta.env.VITE_GOOGLE_CLIENT_ID)
  );
}

export function isOAuthAvailable() {
  return true;
}

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
    if (typeof window !== "undefined" && window.google?.accounts?.id) {
      return new Promise((resolve, reject) => {
        window.google.accounts.id.prompt((notification) => {
          if (notification.isNotDisplayed() || notification.isSkippedMoment()) {
            reject(new Error("Google Sign-In prompt closed."));
          }
        });
      });
    }
    // Silently log rather than throwing screen-blocking error
    console.warn("Google Sign-In is not configured in this environment.");
    throw new Error("Google Login is unavailable here. Use Email/OTP or the demo credentials below.");
  } else if (provider === "apple") {
    console.warn("Apple Sign-In is not configured.");
    return null;
  }
  return null;
}
