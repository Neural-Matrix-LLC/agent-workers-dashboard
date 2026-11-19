export type TaskRef = { id: string; ref: string; event: string; state: string };
export type Message = {
  id: string; conversation_id: string; from: string; role: string; kind: string; body: string;
  reply_to: string | null; at: string; hop: number; turn_id: string | null; mentions: string[];
  actor?: string; task?: TaskRef; task_id?: string;
};
export type Conversation = {
  id: string; kind: "room" | "dm"; title: string | null; mode: string; fanout: string | null;
  moderator: string | null; members: string[]; observed: boolean; messages: number;
  last_message_id: string | null; source: "config" | "ui" | null; archived_at: string | null;
};
export type Turn = {
  id: string; agent: string; conversation_id: string; state: string; hop: number; error: string | null;
  cost_usd: number | null; tokens_in: number | null; tokens_out: number | null;
  created_at: string; started_at: string | null; finished_at: string | null; task_id: string | null;
};
export type TaskState = "open" | "assigned" | "in_progress" | "blocked" | "done" | "failed" | "cancelled";
export type Task = {
  id: string; ref: string; number: number; conversation_id: string; parent_id: string | null; depth: number;
  title: string; description: string; acceptance: string | null; priority: "low" | "normal" | "high";
  labels: string[]; creator: string; assignee: string | null; state: TaskState; result: string | null;
  blocked_reason: string | null; artifact_count: number; reassignments: number; version: number;
  children: { total: number; open: number; done: number; failed: number; cancelled: number };
  created_at: string; updated_at: string; closed_at: string | null;
};
export type TaskEvent = {
  id: string; type: string; actor: string; from_state: string | null; to_state: string | null;
  note: string | null; data: Record<string, any>; message_id: string | null; hop: number; at: string;
};
export type Artifact = {
  id: string; task_id: string; task_ref: string; name: string; description: string | null; media_type: string;
  size: number; sha256: string; uploader: string; supersedes: string | null; created_at: string;
  deleted_at?: string | null;
};
export type TaskDetail = Task & { events: TaskEvent[]; children_list: Task[]; artifacts: Artifact[] };
export type TaskBoard = { tasks: Task[]; counts: Record<TaskState, number>; next_before: string | null };
export type NewTask = {
  conversation_id: string; title: string; description?: string; acceptance?: string; assignee?: string;
  parent_id?: string; priority?: string; labels?: string[];
};
export type TaskAction = {
  action: string; note?: string; result?: string; blocked_reason?: string; assignee?: string;
  fields?: Record<string, unknown>; cascade?: boolean; expected_version?: number; force?: boolean;
};
export type NewRoom = { id: string; title?: string; mode?: string; members: string[] };
export type Budget = { usd_per_day?: number; turns_per_day?: number };
export type Agent = {
  id: string; display_name: string; connector: string; delivery: string; description: string;
  status: "offline" | "idle" | "working" | "paused"; has_token: boolean; turns_today: number;
  usd_today: number; budget: Budget; queued: number; awaiting_approval: Turn[]; turn_id: string | null;
  mode: string | null; modes: string[]; pending_permissions: Permission[]; open_tasks: number;
};
export type Permission = { id: string; turn_id: string; conversation_id: string; tool: string; summary: string; at: string };
export type Participant = { id: string; display_name: string; role: string; status: string; description: string };
export type Settings = { paused: boolean; approval_mode: boolean; max_hops: number; turns_per_minute: number };
export type Me = { owner: { id: string; display_name: string }; settings: Settings; participants: Participant[] };
export type TraceEvent = { id: string; type: string; payload: any; at: string };
export type Trace = { turn: Turn; events: TraceEvent[] };
export type HubEvent =
  | { type: "message"; data: Message }
  | { type: "agents"; data: Agent[] }
  | { type: "turn"; data: Turn }
  | { type: "trace"; data: { turn_id: string; agent: string } }
  | { type: "settings"; data: Settings }
  | { type: "conversation"; data: { id: string } }
  | { type: "task"; data: Task }
  | { type: "artifact"; data: Artifact };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public data: any = {}) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined || form ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, data.error ?? "error", data.message ?? response.statusText, data);
  }
  return data as T;
}

/** An artifact's bytes. Fetched as a blob so the browser never opens an artifact URL as a document. */
async function fetchBlob(path: string): Promise<Blob> {
  const response = await fetch(`/api${path}`, { credentials: "same-origin" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(response.status, data.error ?? "error", data.message ?? response.statusText, data);
  }
  return response.blob();
}

function query(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "") as [string, string][];
  return entries.length ? `?${new URLSearchParams(entries)}` : "";
}

const id = encodeURIComponent;

export const api = {
  login: (password: string) => call("POST", "/login", { password }),
  logout: () => call("POST", "/logout"),
  me: () => call<Me>("GET", "/me"),
  conversations: () => call<Conversation[]>("GET", "/conversations"),
  messages: (conversation: string) => call<Message[]>("GET", `/conversations/${id(conversation)}/messages?limit=300`),
  post: (conversation: string, body: string) => call("POST", `/conversations/${id(conversation)}/messages`, { body }),
  activity: () => call<Message[]>("GET", "/activity?limit=300"),
  agents: () => call<Agent[]>("GET", "/agents"),
  trace: (turn: string) => call<Trace>("GET", `/turns/${id(turn)}/trace`),
  control: (agent: string, action: "pause" | "resume" | "stop") => call("POST", `/agents/${id(agent)}/${action}`),
  budget: (agent: string, budget: Budget) => call("PUT", `/agents/${id(agent)}/budget`, budget),
  mode: (conversation: string, mode: string) => call("PUT", `/conversations/${id(conversation)}/mode`, { mode }),
  continueChain: (notice: string) => call("POST", `/notices/${id(notice)}/continue`),
  decide: (turn: string, decision: "approve" | "reject") => call("POST", `/turns/${id(turn)}/${decision}`),
  agentMode: (agent: string, mode: string) => call("PUT", `/agents/${id(agent)}/mode`, { mode }),
  permission: (request: string, decision: "allow" | "deny") => call("POST", `/permissions/${id(request)}/${decision}`),
  settings: (approval_mode: boolean) => call<Settings>("PUT", "/settings", { approval_mode }),
  tasks: () => call<TaskBoard>("GET", `/tasks${query({ limit: "500" })}`),
  task: (task: string) => call<TaskDetail>("GET", `/tasks/${id(task)}`),
  createTask: (task: NewTask) => call<{ task: Task }>("POST", "/tasks", task),
  patchTask: (task: string, action: TaskAction) => call<{ task: Task }>("PATCH", `/tasks/${id(task)}`, action),
  uploadArtifact: (task: string, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return call<{ artifact: Artifact }>("POST", `/tasks/${id(task)}/artifacts`, form);
  },
  artifactContent: (artifact: string) => fetchBlob(`/artifacts/${id(artifact)}/content`),
  deleteArtifact: (artifact: string) => call("DELETE", `/artifacts/${id(artifact)}`),
  createRoom: (room: NewRoom) => call<Conversation>("POST", "/rooms", room),
  patchRoom: (room: string, fields: { title?: string | null; mode?: string }) =>
    call<Conversation>("PATCH", `/rooms/${id(room)}`, fields),
  roomMembers: (room: string, members: string[]) =>
    call<{ added: string[]; removed: string[]; released_tasks: string[] }>("PUT", `/rooms/${id(room)}/members`, { members }),
  archiveRoom: (room: string) => call("POST", `/rooms/${id(room)}/archive`),
};

/** Live feed. Reconnects on its own; `onOpen` lets the caller reload what it missed. */
export function connect(onEvent: (event: HubEvent) => void, onOpen: () => void, onDenied: () => void): () => void {
  let socket: WebSocket | null = null;
  let closed = false;
  let delay = 500;
  const open = () => {
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
    socket.onopen = () => { delay = 500; onOpen(); };
    socket.onmessage = (message) => onEvent(JSON.parse(message.data));
    socket.onclose = (event) => {
      if (closed) return;
      if (event.code === 4401) return onDenied();
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 10000);
    };
  };
  open();
  return () => { closed = true; socket?.close(); };
}
