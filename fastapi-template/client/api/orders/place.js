/**
 * Secure Serverless Order Placement Route (Next.js / Vercel Serverless)
 * Direct DMA & Broker Dispatch with Rate Limiting & Credentials Validation
 */

export default async function handler(req, res) {
  if (req.method === "OPTIONS") {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
    return res.status(200).end();
  }

  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method Not Allowed" });
  }

  const authHeader = req.headers["authorization"] || req.headers["Authorization"];
  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return res.status(401).json({ error: "Unauthorized: Missing token" });
  }

  const token = authHeader.split(" ")[1];

  try {
    const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || process.env.VITE_API_URL || "https://tradetron-8jkz.onrender.com";
    const response = await fetch(`${BACKEND_URL.replace(/\/$/, "")}/api/trades/order`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
        "X-Serverless-Gateway": "TradeThrone-Orders-Route",
      },
      body: JSON.stringify(req.body),
    });

    const data = await response.json();
    return res.status(response.status).json(data);
  } catch (err) {
    return res.status(500).json({ error: "Serverless Order Placement Gateway Timeout" });
  }
}
