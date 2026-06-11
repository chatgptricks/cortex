import type { Post } from "./types";

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
const API_KEY: string | undefined = import.meta.env.VITE_API_KEY || undefined;

function withAuth(init?: RequestInit): RequestInit {
  if (!API_KEY) return init ?? {};
  const headers = new Headers(init?.headers);
  headers.set("X-API-Key", API_KEY);
  return { ...init, headers };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, withAuth(init));
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail ?? message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function runModalOcrBatch() {
  return request<{ eligible_count: number; processed_count: number; updated_count: number; posts: Post[] }>(
    "/api/post-db/ocr/modal-batch",
    { method: "POST" }
  );
}
