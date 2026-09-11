/**
 * Shared REST-error normalization.
 *
 * Backend error bodies vary by origin:
 *   {detail: "msg"}                    — FastAPI HTTPException
 *   {detail: [{loc, msg, type}, ...]}  — FastAPI/Pydantic validation array
 *   {message: "msg"} / {error: "msg"}  — generic REST services
 *   "msg"                              — plain-text error body
 *
 * This helper guarantees a plain, human-readable string and NEVER lets a
 * non-serializable object (e.g. a Pydantic validation `detail` array) leak
 * into the UI as "[object Object]".
 */
export function apiErrorMessage(body, fallback = "Request failed") {
  if (body == null) return fallback;
  if (typeof body === "string") return body.trim() || fallback;
  if (typeof body !== "object" || Array.isArray(body)) return fallback;

  const detail = body.detail ?? body.message ?? body.error;
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail.trim() || fallback;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => (typeof entry === "string" ? entry : entry?.msg || entry?.message))
      .filter((part) => typeof part === "string" && part.trim().length > 0)
      .map((part) => part.trim());
    if (parts.length > 0) return parts.join("; ");
    return fallback;
  }
  // An object we cannot stringify into anything meaningful — never surface
  // "[object Object]" to a trader.
  return fallback;
}