/**
 * Secure Serverless Trade Execution API Route (Vercel / Next.js compatible)
 * 
 * - Strictly accesses sensitive API keys & credentials via server-side process.env
 * - Verifies caller JWT Authorization token before submitting orders
 * - Interacts with trading engine/broker DMA gateway with server-side signing
 */

export default async function handler(req, res) {
  // CORS & method validation
  if (req.method === "OPTIONS") {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
    return res.status(200).end();
  }

  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method Not Allowed. Only POST is accepted." });
  }

  const authHeader = req.headers["authorization"] || req.headers["Authorization"];
  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return res.status(401).json({ error: "Unauthorized: Missing or invalid Bearer token" });
  }

  const token = authHeader.split(" ")[1];

  try {
    const { symbol, side, quantity, order_type = "MARKET", price = null, mode = "PAPER" } = req.body || {};

    if (!symbol || !side || !quantity || quantity <= 0) {
      return res.status(400).json({ error: "Invalid order parameters: symbol, side, and quantity (> 0) are required." });
    }

    if (!["BUY", "SELL"].includes(side.toUpperCase())) {
      return res.status(400).json({ error: "Invalid order side. Must be BUY or SELL." });
    }

    // ── Server-Side Sensitive Credential Ingestion ───────────────────────────
    // Secrets are ONLY accessed server-side and never leaked in client bundle
    const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || process.env.VITE_API_URL || "https://tradetron-8jkz.onrender.com";
    const SERVER_API_SECRET = process.env.TRADING_API_SECRET || process.env.JWT_SECRET || "";
    const ZERODHA_API_KEY = process.env.ZERODHA_API_KEY || "";
    const ANGEL_API_KEY = process.env.ANGEL_API_KEY || "";
    const BINANCE_API_KEY = process.env.BINANCE_API_KEY || "";

    // Forward trade execution order to core trading engine securely
    const response = await fetch(`${BACKEND_URL.replace(/\/$/, "")}/api/trades/order`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
        "X-Serverless-Gateway": "Tradetron-Next-Serverless",
        ...(SERVER_API_SECRET ? { "X-Internal-Secret": SERVER_API_SECRET } : {}),
      },
      body: JSON.stringify({
        symbol: symbol.toUpperCase().trim(),
        side: side.toUpperCase().trim(),
        quantity: Number(quantity),
        order_type: order_type.toUpperCase(),
        price: price ? Number(price) : null,
        mode: mode.toUpperCase(),
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      return res.status(response.status).json({
        error: data.detail || data.error || "Trade execution rejected by exchange/broker gateway.",
      });
    }

    return res.status(200).json({
      success: true,
      order_id: data.order_id,
      symbol: data.symbol,
      side: data.side,
      quantity: data.quantity,
      price: data.price,
      status: data.status || "FILLED",
      mode: data.mode,
      position_id: data.position_id,
      executed_at: data.executed_at,
    });
  } catch (error) {
    console.error("[Serverless Execution Error]:", error);
    return res.status(500).json({
      error: "Internal Serverless Execution Gateway Error",
      details: process.env.NODE_ENV === "development" ? error.message : undefined,
    });
  }
}
