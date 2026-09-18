import { adminApiPrefix } from "./adminRuntime";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  const adminPrefix = adminApiPrefix();
  const csrfKey = adminPrefix && path.startsWith(`${adminPrefix}/`) ? "admin_csrf" : "user_csrf";
  const csrf = window.sessionStorage.getItem(csrfKey);
  if (csrf && init?.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    let message = `${init?.method ?? "GET"} ${path} failed: ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the status-based fallback.
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
