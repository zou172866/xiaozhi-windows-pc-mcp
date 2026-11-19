export type Settings = Record<string, string | number | boolean | null>;
export type Character = {
  id: string;
  name: string;
  persona: string;
  memory_policy: "automatic" | "review" | "off";
  relationship_enabled: boolean;
  romance_enabled: boolean;
  timezone: string;
  core_persona: string;
  appearance: string;
  voice_id: string;
};
export type Bootstrap = {
  characters: Character[];
  appearance_references?: Record<string, ImageArtifact | null>;
  settings: Settings;
  preview_mode: boolean;
  capabilities: Record<string, boolean>;
  credential_storage: { save_destination: string };
};
export type Turn = {
  id: string;
  cursor: number;
  user_message: string;
  assistant_reply: string;
  created_at: number;
  image_result?: ChatImageResult | null;
};
export type ImageArtifact = { artifact: string; mime: string; bytes: number };
export type ChatImageResult = {
  status: "succeeded" | "failed" | "unavailable" | "needs_appearance";
  artifacts?: ImageArtifact[];
  error?: string;
};
export type Pending = { id: string; request_id: string; user_message: string };
export type Conversation = { turns: Turn[]; pending: Pending | null };
export type Memory = {
  id: string;
  key: string;
  text: string;
  category: string;
  source_quote: string;
  source_turn_id: string | null;
  origin: string;
  revision: number;
  updated_at: number;
  details: Record<string, unknown>;
};
export type MemoryPage = { facts: Memory[]; total: number; offset: number };
export type Inspection = {
  turn_id: string;
  reply: string;
  user_message: string;
  status: string;
  image_result?: ChatImageResult | null;
  generation: Record<string, unknown> | null;
  context: Record<string, unknown> | null;
  proposals: { memories: { text: string }[]; corrections: unknown[] };
};
export type ChatResult = {
  reply: string;
  turn_id: string;
  committed: boolean;
  needs_review: boolean;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export function initialToken() {
  const params = new URLSearchParams(location.hash.slice(1));
  const value = params.get("token");
  if (value) {
    sessionStorage.setItem("companion-token", value);
    history.replaceState(null, "", location.pathname + location.search);
  }
  return value || sessionStorage.getItem("companion-token") || "";
}
export async function api<T>(
  token: string,
  action: string,
  body?: unknown,
  query?: Record<string, string>,
): Promise<T> {
  const url =
    "/api/" + action + (query ? "?" + new URLSearchParams(query) : "");
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: "Bearer " + token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.error || "The request could not be completed.",
      response.status,
    );
  return data;
}
export function errorText(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}
export function fileData(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("This file could not be read."));
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
}
export const portrait = (id: string) =>
  id.startsWith("rina-")
    ? "/portraits/rina.jpg"
    : id.startsWith("sean-")
      ? "/portraits/sean.jpg"
      : null;
export function downloadJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
