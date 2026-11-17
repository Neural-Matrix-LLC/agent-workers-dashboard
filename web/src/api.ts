export type Message = {
  id: string; conversation_id: string; from: string; role: string; kind: string; body: string;
  reply_to: string | null; at: string; hop: number; turn_id: string | null; mentions: string[];
};
export type Conversation = {
  id: string; kind: "room" | "dm"; title: string | null; mode: string; fanout: string | null;
  members: string[]; observed: boolean; messages: number; last_message_id: string | null;
};
export type Turn = {
  id: string; agent: string; conversation_id: string; state: string; hop: number; error: string | null;
  cost_usd: number | null; tokens_in: number | null; tokens_out: number | null;
  created_at: string; started_at: string | null; finished_at: string | null;
};
export type Budget = { usd_per_day?: number; turns_per_day?: number };
export type Agent = {
  id: string; display_name: string; connector: string; delivery: string; description: string;
  status: "offline" | "idle" | "working" | "paused"; has_token: boolean; turns_today: number;
  usd_today: number; budget: Budget; queued: number; awaiting_approval: Turn[]; turn_id: string | null;
  mode: string | null; modes: string[]; pending_permissions: Permission[];
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
  | { type: "conversation"; data: { id: string } };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.error ?? "error", data.message ?? response.statusText);
  return data as T;
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
