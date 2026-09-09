// RED reproduction for the single-flight 401-token-refresh race condition in
// client/src/services/apiClient.js authFetch().
//
// The claim: when a 401 is received, authFetch subscribes a retry callback to
// refreshSubscribers AFTER onRefreshed() has already run (because the leader
// awaits refreshAccessToken() then calls onRefreshed() BEFORE the caller has a
// chance to addRefreshSubscriber). Therefore neither the leader nor any
// concurrent request's retry Promise is ever resolved -> the caller hangs
// forever (infinite loading spinner) whenever the access token has expired.
//
// This script faithfully mirrors the exact algorithm in apiClient.js lines
// 201-226 and proves the hang empirically.

let isRefreshing = false;
let refreshSubscribers = [];

function onRefreshed(newToken) {
  refreshSubscribers.forEach((cb) => cb(newToken));
  refreshSubscribers = [];
}

function addRefreshSubscriber(cb) {
  refreshSubscribers.push(cb);
}

// Mock refresh that always succeeds with a fresh token.
const refreshAccessToken = async () => "NEW_TOKEN_123";

// Faithful copy of the authFetch 401 branch (apiClient.js ~201-226).
async function authFetchLike() {
  const res = Object.freeze({ status: 401 });
  // getRefreshToken() is present => true
  if (res.status === 401 && true) {
    if (!isRefreshing) {
      isRefreshing = true;
      const newToken = await refreshAccessToken();
      isRefreshing = false;
      if (newToken) onRefreshed(newToken);
    }
    return new Promise((resolve) => {
      addRefreshSubscriber(async (newToken) => {
        if (!newToken) {
          resolve(res);
          return;
        }
        resolve({ status: 200, token: newToken });
      });
    });
  }
  return res;
}

(async () => {
  const leader = authFetchLike();
  const concurrent = authFetchLike();
  const timeout = (ms) => new Promise((r) => setTimeout(r, ms));

  const winner = await Promise.race([
    Promise.all([leader, concurrent]),
    timeout(400).then(() => "TIMEOUT_HANG"),
  ]);

  if (winner === "TIMEOUT_HANG") {
    console.log("RED: authFetch retry promises HUNG (never resolved).");
    process.exit(1);
  } else {
    console.log("GREEN: resolved:", winner);
    process.exit(0);
  }
})();
