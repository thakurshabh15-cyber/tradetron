/**
 * Shared market-feed price resolution.
 *
 * Strictly resolves a TRADABLE price from real quote data (or an explicit
 * positive fallback). It NEVER fabricates a plausible-but-invented number:
 * while the feed has no price for a symbol the result is `null`, so order
 * panels disable MARKET submission and charts show "awaiting feed" instead of
 * a made-up fallback (previously a hardcoded `24850.0` was displayed as the
 * live price for arbitrary symbols such as SOLUSDT or custom watchlist names).
 */

/**
 * @param {object|null} liveQuote - quote record from the market store / WS feed
 * @param {number|null} [fallback] - explicit positive fallback (opt-in only)
 * @returns {number|null} resolved feed price, or `null` when unknown/zero/invalid
 */
export function getFeedPrice(liveQuote, fallback = null) {
  const p = liveQuote && typeof liveQuote === "object" ? liveQuote.price : undefined;
  if (typeof p === "number" && Number.isFinite(p) && p > 0) return p;
  if (typeof fallback === "number" && Number.isFinite(fallback) && fallback > 0) return fallback;
  return null;
}

/**
 * @param {object|null} liveQuote - quote record from the market store / WS feed
 * @returns {boolean} true only when a real feed price is currently available
 */
export function hasFeedPrice(liveQuote) {
  return getFeedPrice(liveQuote) != null;
}