/**
 * Deploy-target resolution for the Marketplace DeploymentModal.
 *
 * LIVE deployment must target a REAL, currently-connected broker account owned
 * by the caller — never a hardcoded broker name. PAPER always targets the
 * simulated engine. When no connected account exists, LIVE is reported as
 * `unavailable` so the UI prompts the trader instead of guessing.
 *
 * Broker account records come from GET /api/brokers/accounts and expose
 * `status`, `is_token_expired`, `broker_name`, `account_name`, and `id`.
 */

/**
 * @param {Array<object>} [accounts] - broker account records from /api/brokers/accounts
 * @returns {Array<{broker_id: string, broker_name: string, label: string}>}
 *   only connected, non-expired accounts, ready for a <select>.
 */
export function deployBrokerOptions(accounts = []) {
  const list = Array.isArray(accounts) ? accounts : [];
  return list
    .filter((a) => a && a.status === "CONNECTED" && !a.is_token_expired)
    .map((a) => {
      const name = a.broker_name || a.name || "Connected Broker";
      return {
        broker_id: a.id,
        broker_name: name,
        label: `${a.account_name || "Trading Account"} · ${String(name).replace(/_/g, " ")}`,
      };
    });
}

/**
 * @param {string} executionMode - "PAPER" | "LIVE"
 * @param {Array<object>} [connectedAccounts] - broker account records
 * @returns {{mode: string, broker_id: string|null, broker_name: string|null, unavailable: boolean}}
 */
export function resolveDeployTarget(executionMode, connectedAccounts) {
  if (executionMode !== "LIVE") {
    return { mode: "PAPER", broker_id: null, broker_name: "Simulated", unavailable: false };
  }
  const opts = deployBrokerOptions(connectedAccounts);
  const first = opts[0] || null;
  if (!first) {
    return { mode: "LIVE", broker_id: null, broker_name: null, unavailable: true };
  }
  return { mode: "LIVE", broker_id: first.broker_id, broker_name: first.broker_name, unavailable: false };
}