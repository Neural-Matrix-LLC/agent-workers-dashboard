import DOMPurify from "dompurify";
import { marked } from "marked";
import { Conversation } from "./api";

export const ACTIVITY = "activity";
export const TASKS = "tasks";

export function hue(id: string): number {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return hash;
}

export function time(at: string): string {
  const date = new Date(at);
  const today = new Date().toDateString() === date.toDateString();
  return today
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Agent output and file content are untrusted: markdown is rendered, then sanitized. */
export function renderMarkdown(body: string): string {
  return DOMPurify.sanitize(marked.parse(body, { breaks: true, async: false }) as string, {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "img"],
  });
}

export function conversationLabel(conversation: Conversation | undefined, id: string): string {
  if (id === ACTIVITY) return "All activity";
  if (id === TASKS) return "Tasks";
  if (!conversation) return id;
  if (conversation.kind === "room") return `# ${conversation.title ?? id.replace(/^room:/, "")}`;
  const members = conversation.members;
  return conversation.observed ? members.join(" <> ") : members.filter((m) => m !== "owner").join(", ");
}

export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function readJson<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "null") ?? fallback;
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage can be full or blocked; the UI works without it.
  }
}

export type Run = (action: () => Promise<unknown>) => Promise<void>;
