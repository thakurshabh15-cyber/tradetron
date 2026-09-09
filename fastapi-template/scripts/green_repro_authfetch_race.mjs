// GREEN verification for the FIXED single-flight 401-token-refresh in
// client/src/services/apiClient.js authFetch().
//
// Mirrors the corrected algorithm: every 401 request registers its retry
// subscriber BEFORE the leader triggers the refresh; onRefreshed() flushes the
// queue exactly once (success or failure). The leader AND concurrent requests
// must all resolve — never hang.

let isRefreshing = false;
let refreshSubscribers = [];

function onRefreshed(newToken) {
  refreshSubscribers.forEach((cb) => cb(newToken));
  refreshSubscribers = [];
}

function addRefreshSubscriber(cb) {
  refreshSubscribers.push(cb);
}

const refreshAccessToken = async () => "NEW_TOKEN_123";
let refreshCalls = 0;
const trackedRefresh = async () => {
  refreshCalls += 1;
  return refreshAccessToken();
};

// Faithful copy of the FIXED authFetch 401 branch.
async function authFetchLike(marker) {
  const res = Object.freeze({ status: 401 });
  if (res.status === 401 && true) {
    return new Promise((resolve) => {
      addRefreshSubscriber(async (newToken) => {
        if (!newToken) {
          resolve({ marker, status: res.status });
          return;
        }
        resolve({ marker, status: 200, token: newToken });
      });
      if (!isRefreshing) {
        isRefreshing = true;
        trackedRefresh()
          .then((newToken) => onRefreshed(newToken))
          .catch(() => onRefreshed(null))
          .finally(() => {
            isRefreshing = false;
          });
      }
    });
  }
  return res;
}

(async () => {
  const leader = authFetchLike("leader");
  const concurrent = authFetchLike("concurrent");
  const timeout = (ms) => new Promise((r) => setTimeout(r, ms));

  const winner = await Promise.race([
    Promise.all([leader, concurrent]),
    timeout(400).then(() => "TIMEOUT_HANG"),
  ]);

  if (winner === "TIMEOUT_HANG") {
    console.log("FAIL: still hanging");
    process.exit(1);
  }

  const ok =
    Array.isArray(winner) &&
    winner[0].status === 200 &&
    winner[1].status === 200 &&
    winner[0].token === "NEW_TOKEN_123" &&
    winner[1].token === "NEW_TOKEN_123" &&
    refreshCalls === 1; // single flight: exactly ONE refresh for both requests

  if (ok) {
    console.log("GREEN: leader + concurrent resolved with fresh token; exactly 1 refresh call.");
    process.exit(0);
  }
  console.log("FAIL: unexpected result", winner, "refreshCalls=", refreshCalls);
  process.exit(1);
})();
