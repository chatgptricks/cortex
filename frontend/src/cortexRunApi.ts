import type { AbTest, Calibration, Health, LlmReport, MetadataOptions, Post } from "./types";
import { withAuth, withToken } from "./auth";

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
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

export function mediaUrl(url?: string | null): string | undefined {
  if (!url) {
    return undefined;
  }
  return url.startsWith("http") ? withToken(url) : withToken(`${API_BASE}${url}`);
}

export function getHealth() {
  return request<Health>("/api/health");
}

export function getCalibration() {
  return request<Calibration>("/api/calibration");
}

export function getPosts(section?: string) {
  const suffix = section ? `?section=${encodeURIComponent(section)}` : "";
  return request<{ posts: Post[]; calibration: Calibration }>(`/api/posts${suffix}`);
}

export function getPost(id: number) {
  return request<{ post: Post }>(`/api/posts/${id}`);
}

export function getMetadataOptions() {
  return request<MetadataOptions>("/api/metadata-options");
}

export function createPost(form: FormData) {
  return request<{ post: Post }>("/api/posts", {
    method: "POST",
    body: form
  });
}

export function createPostFromInstagramLink(form: FormData) {
  return request<{ post: Post; created: boolean }>("/api/posts/instagram-link", {
    method: "POST",
    body: form
  });
}

export function createBatchPosts(form: FormData) {
  return request<{ posts: Post[] }>("/api/posts/batch", {
    method: "POST",
    body: form
  });
}

export function updatePost(id: number, form: FormData) {
  return request<{ post: Post }>(`/api/posts/${id}`, {
    method: "PATCH",
    body: form
  });
}

export function analyzePost(id: number) {
  const form = new FormData();
  return request<{ post: Post }>(`/api/posts/${id}/analyze`, {
    method: "POST",
    body: form
  });
}

export function generatePostReport(id: number, force = false) {
  const suffix = force ? "?force=true" : "";
  return request<{ report: LlmReport }>(`/api/posts/${id}/report${suffix}`, {
    method: "POST"
  });
}

export function runModalOcrBatch() {
  return request<{ eligible_count: number; processed_count: number; updated_count: number; posts: Post[] }>(
    "/api/post-db/ocr/modal-batch",
    { method: "POST" }
  );
}

export function deletePost(id: number) {
  return request<{ ok: boolean; deleted_post_id: number; deleted_files: number }>(`/api/posts/${id}`, {
    method: "DELETE"
  });
}

export function getAbTests() {
  return request<{ tests: AbTest[] }>("/api/ab-tests");
}

export function createAbTest(form: FormData) {
  return request<{ test: AbTest; candidates: Post[] }>("/api/ab-tests", {
    method: "POST",
    body: form
  });
}

export function getAbTest(id: number) {
  return request<{ test: AbTest; candidates: Post[] }>(`/api/ab-tests/${id}`);
}

export function deleteAbTest(id: number) {
  return request<{
    ok: boolean;
    deleted_test_id: number;
    deleted_post_ids: number[];
    deleted_files: number;
  }>(`/api/ab-tests/${id}`, {
    method: "DELETE"
  });
}
