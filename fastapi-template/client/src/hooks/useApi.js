import { useCallback, useEffect, useState } from "react";
import { authFetch, publicFetch } from "../services/apiClient";

/**
 * Generic REST API hook with loading/error states and automatic Authorization token injection.
 *
 * @param {string} path - API path (e.g., "/api/strategies")
 * @param {object} options
 * @param {boolean} options.immediate - Fetch on mount (default: true)
 * @param {boolean} options.public - Use unauthenticated fetch (default: false)
 * @returns {{ data, loading, error, refetch, post, patch, del }}
 */
export function useApi(path, { immediate = true, public: isPublic = false, cacheTtlMs = 0 } = {}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(immediate);
  const [error, setError] = useState(null);

  const fetcher = isPublic ? publicFetch : authFetch;

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // cacheTtlMs is carried ONLY for GET fetches: static/reference endpoints
      // may opt into the shared short-TTL cache; trading-truth endpoints keep
      // cacheTtlMs=0 (correctness wins over request count).  The apiClient
      // additionally coalesces concurrent identical GETs transparently.
      const fetchOpts = cacheTtlMs > 0 ? { cacheTtlMs } : undefined;
      const res = await fetcher(path, fetchOpts);
      // publicFetch returns null on 401/403 — treat as "no data, not an error"
      if (res === null) {
        setData(null);
        return null;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      return json;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setLoading(false);
    }
    // isPublic is intentionally NOT a dependency: it only selects `fetcher`
    // above, which is already in the dependency array.
  }, [path, fetcher, cacheTtlMs]);

  useEffect(() => {
    if (immediate) fetchData();
  }, [fetchData, immediate]);

  const post = useCallback(
    async (body) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetcher(path, {
          method: "POST",
          body: JSON.stringify(body),
        });
        if (res === null) return null;
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || `HTTP ${res.status}`);
        }
        const json = await res.json();
        await fetchData();
        return json;
      } catch (err) {
        setError(err.message);
        return null;
      } finally {
        setLoading(false);
      }
    },
    [path, fetchData, fetcher]
  );

  const patch = useCallback(
    async (id, body) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetcher(`${path}/${id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        if (res === null) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        await fetchData();
        return json;
      } catch (err) {
        setError(err.message);
        return null;
      } finally {
        setLoading(false);
      }
    },
    [path, fetchData, fetcher]
  );

  const del = useCallback(
    async (id) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetcher(`${path}/${id}`, {
          method: "DELETE",
        });
        if (res === null) return false;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await fetchData();
        return true;
      } catch (err) {
        setError(err.message);
        return false;
      } finally {
        setLoading(false);
      }
    },
    [path, fetchData, fetcher]
  );

  return { data, loading, error, refetch: fetchData, post, patch, del };
}
