import { useCallback, useEffect, useState } from "react";
import { authFetch } from "../services/apiClient";

/**
 * Generic REST API hook with loading/error states and automatic Authorization token injection.
 *
 * @param {string} path - API path (e.g., "/api/strategies")
 * @param {object} options
 * @param {boolean} options.immediate - Fetch on mount (default: true)
 * @returns {{ data, loading, error, refetch, post, patch, del }}
 */
export function useApi(path, { immediate = true } = {}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch(path);
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
  }, [path]);

  useEffect(() => {
    if (immediate) fetchData();
  }, [fetchData, immediate]);

  const post = useCallback(
    async (body) => {
      setLoading(true);
      setError(null);
      try {
        const res = await authFetch(path, {
          method: "POST",
          body: JSON.stringify(body),
        });
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
    [path, fetchData]
  );

  const patch = useCallback(
    async (id, body) => {
      setLoading(true);
      setError(null);
      try {
        const res = await authFetch(`${path}/${id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
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
    [path, fetchData]
  );

  const del = useCallback(
    async (id) => {
      setLoading(true);
      setError(null);
      try {
        const res = await authFetch(`${path}/${id}`, {
          method: "DELETE",
        });
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
    [path, fetchData]
  );

  return { data, loading, error, refetch: fetchData, post, patch, del };
}
