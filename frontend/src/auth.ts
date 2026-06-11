const STORAGE_KEY = "cortex_access_key";

export function getApiKey(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setApiKey(value: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // storage unavailable; session-only auth still works via module state below
  }
}

export function clearApiKey(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function withAuth(init?: RequestInit): RequestInit {
  const key = getApiKey();
  if (!key) return init ?? {};
  const headers = new Headers(init?.headers);
  headers.set("X-API-Key", key);
  return { ...init, headers };
}

export function withToken(url: string): string {
  const key = getApiKey();
  if (!key) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(key);
}
