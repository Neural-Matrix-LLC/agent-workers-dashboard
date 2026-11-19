import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Agent, api, ApiError, connect, Conversation, HubEvent, Me, Message, Settings, Task, Trace, TraceEvent } from "./api";
import { RoomDialog, RoomMembers } from "./Rooms";
import { boardFromHash, BoardFilters, boardHash, NO_FILTERS, TaskBoard } from "./TaskBoard";
import { TaskDrawer } from "./TaskDrawer";
import { ACTIVITY, conversationLabel, hue, readJson, renderMarkdown, TASKS, time, writeJson } from "./ui";

const MODE_NAMES: Record<string, string> = {
  plan: "Plan (no edits)", manual: "Manual (ask for everything)",
  acceptEdits: "Edit automatically", auto: "Auto (safety check decides)",
};
const SEEN_KEY = "agent-hub-seen";
const SEEN_TASKS_KEY = "agent-hub-seen-tasks";       // task id -> version the owner last opened

function loadSeen(): Record<string, number> | null {
  return readJson<Record<string, number> | null>(SEEN_KEY, null);
}

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const load = useCallback(() => api.me().then(setMe).catch(() => setMe(null)), []);
  const logout = useCallback(() => setMe(null), []);
  useEffect(() => void load(), [load]);
  if (me === undefined) return null;
  if (me === null) return <Login onDone={load} />;
  return <Hub me={me} onLogout={logout} />;
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.login(password);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? "Wrong password." : "The hub cannot be reached.");
    }
  };
  return (
    <form className="login" onSubmit={submit}>
      <h1>Agent Hub</h1>
      <label htmlFor="password">Owner password</label>
      <input id="password" type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
      <button type="submit" className="primary" disabled={!password}>Log in</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function Hub({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [settings, setSettings] = useState<Settings>(me.settings);
  const [selected, setSelected] = useState(() => (boardFromHash(location.hash) ? TASKS : "room:main"));
  const [messages, setMessages] = useState<Message[]>([]);
  const [seen, setSeen] = useState<Record<string, number> | null>(loadSeen);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [tick, setTick] = useState(0);           // bumped when the live feed reconnects
  const [traceTick, setTraceTick] = useState(0);
  const [error, setError] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [filters, setFilters] = useState<BoardFilters>(() => boardFromHash(location.hash) ?? NO_FILTERS);
  const [drawer, setDrawer] = useState<string | null>(null);       // the task open in the drawer
  const [drawerTick, setDrawerTick] = useState(0);
  const [seenTasks, setSeenTasks] = useState<Record<string, number>>(() => readJson(SEEN_TASKS_KEY, {}));
  const [dialog, setDialog] = useState<"room" | "members" | null>(null);
  const selectedRef = useRef(selected);
  const agentRef = useRef(agentId);
  const drawerRef = useRef(drawer);
  selectedRef.current = selected;
  agentRef.current = agentId;
  drawerRef.current = drawer;

  const names = useMemo(() => Object.fromEntries(me.participants.map((p) => [p.id, p.display_name])), [me]);
  const run = useCallback(async (action: () => Promise<unknown>) => {
    try {
      setError("");
      await action();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onLogout();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [onLogout]);

  const loadConversations = useCallback(() => run(async () => setConversations(await api.conversations())), [run]);

  useEffect(() => {
    const onEvent = (event: HubEvent) => {
      if (event.type === "message") {
        const message = event.data;
        if (selectedRef.current === ACTIVITY || selectedRef.current === message.conversation_id) {
          setMessages((list) => (list.some((m) => m.id === message.id) ? list : [...list, message]));
        }
        setConversations((list) => {
          if (!list.some((c) => c.id === message.conversation_id)) {
            void loadConversations();           // a DM that did not exist a moment ago
            return list;
          }
          return list.map((c) => (c.id === message.conversation_id ? { ...c, messages: c.messages + 1 } : c));
        });
      } else if (event.type === "agents") setAgents(event.data);
      else if (event.type === "settings") setSettings(event.data);
      else if (event.type === "conversation") void loadConversations();
      else if (event.type === "task") {
        const task = event.data;
        setTasks((list) => [task, ...list.filter((t) => t.id !== task.id)]);
        if (drawerRef.current === task.id) setDrawerTick((n) => n + 1);
      } else if (event.type === "artifact" && drawerRef.current === event.data.task_id) setDrawerTick((n) => n + 1);
      else if ((event.type === "trace" || event.type === "turn") && event.data.agent === agentRef.current) {
        setTraceTick((n) => n + 1);
      }
    };
    return connect(onEvent, () => setTick((n) => n + 1), onLogout);
  }, [loadConversations, onLogout]);

  useEffect(() => {
    void loadConversations();
    void run(async () => setAgents(await api.agents()));
    void run(async () => setTasks((await api.tasks()).tasks));        // the board reloads on every reconnect
  }, [tick, loadConversations, run]);

  useEffect(() => {
    setMessages([]);
    if (selected === TASKS) return;
    void run(async () => setMessages(selected === ACTIVITY ? await api.activity() : await api.messages(selected)));
  }, [selected, tick, run]);

  useEffect(() => {
    const hash = selected === TASKS ? boardHash(filters) : "";
    if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname + location.search);
  }, [selected, filters]);

  const markSeen = useCallback((task: string, version: number) => setSeenTasks((current) => {
    if (current[task] === version) return current;
    const next = { ...current, [task]: version };
    writeJson(SEEN_TASKS_KEY, next);
    return next;
  }), []);
  const taskAlerts = tasks.filter((t) => (t.state === "blocked" || t.state === "failed") && seenTasks[t.id] !== t.version).length;
  const openTask = (task: string) => setDrawer(task);
  const showTasksOf = (agent: string) => { setFilters({ ...NO_FILTERS, assignee: agent }); setSelected(TASKS); };

  // Unread counts live in this browser: the number of messages seen per conversation.
  useEffect(() => {
    if (!conversations.length) return;
    setSeen((current) => {
      const next = { ...(current ?? Object.fromEntries(conversations.map((c) => [c.id, c.messages]))) };
      const open = conversations.find((c) => c.id === selected);
      if (open) next[open.id] = open.messages;
      if (selected === ACTIVITY) for (const c of conversations) next[c.id] = c.messages;
      const same = current && Object.keys(next).length === Object.keys(current).length &&
        Object.entries(next).every(([key, value]) => current[key] === value);
      if (same) return current;
      localStorage.setItem(SEEN_KEY, JSON.stringify(next));
      return next;
    });
  }, [conversations, selected]);

  const agent = agents.find((a) => a.id === agentId) ?? null;
  const turnId = agent?.turn_id ?? null;
  useEffect(() => {
    if (!turnId) return setTrace(null);
    void run(async () => setTrace(await api.trace(turnId)));
  }, [turnId, traceTick, run]);

  const conversation = conversations.find((c) => c.id === selected);
  const unread = (c: Conversation) => Math.max(0, c.messages - (seen?.[c.id] ?? c.messages));
  const section = (title: string, list: Conversation[], observed = false) =>
    list.length > 0 && (
      <>
        <h2>{title}</h2>
        {list.map((c) => (
          <button key={c.id} className={`nav ${selected === c.id ? "on" : ""} ${observed ? "observed" : ""}`}
            onClick={() => setSelected(c.id)}>
            <span>{conversationLabel(c, c.id)}</span>
            {unread(c) > 0 && <span className="badge">{unread(c)}</span>}
          </button>
        ))}
      </>
    );

  return (
    <div className="hub">
      <nav className="left">
        <div className="brand">Agent Hub</div>
        {section("Rooms", conversations.filter((c) => c.kind === "room"))}
        {section("My DMs", conversations.filter((c) => c.kind === "dm" && !c.observed))}
        {section("Agent DMs (observe)", conversations.filter((c) => c.observed), true)}
        <button className="nav add" onClick={() => setDialog("room")}>+ New room</button>
        <h2>Everything</h2>
        <button className={`nav ${selected === ACTIVITY ? "on" : ""}`} onClick={() => setSelected(ACTIVITY)}>
          All activity
        </button>
        <button className={`nav ${selected === TASKS ? "on" : ""}`} onClick={() => setSelected(TASKS)}>
          <span>Tasks</span>
          {taskAlerts > 0 && <span className="badge" title="Blocked or failed tasks you have not opened">{taskAlerts}</span>}
        </button>
        <button className="nav logout" onClick={() => run(async () => { await api.logout(); onLogout(); })}>
          Log out
        </button>
      </nav>

      <main>
        <header>
          <h1>{conversationLabel(conversation, selected)}</h1>
          {conversation?.observed && <span className="tag">read-only</span>}
          {conversation?.archived_at && <span className="tag">archived</span>}
          {conversation?.kind === "room" && !conversation.archived_at && (
            <button onClick={() => setDialog("members")}>Room settings</button>
          )}
          {conversation?.kind === "room" && !conversation.archived_at && (
            <label>
              mode{" "}
              <select value={conversation.mode}
                onChange={(e) => run(async () => { await api.mode(conversation.id, e.target.value); })}>
                <option value="mention">mention</option>
                <option value="round_robin">round robin</option>
                <option value="moderated">moderated</option>
              </select>
            </label>
          )}
          <span className="spacer" />
          <label title="Agent-to-agent wakes wait for your approval">
            <input type="checkbox" checked={settings.approval_mode}
              onChange={(e) => run(async () => setSettings(await api.settings(e.target.checked)))} />{" "}
            approval
          </label>
          <button onClick={() => run(() => api.control("all", settings.paused ? "resume" : "pause"))}>
            {settings.paused ? "Resume all" : "Pause all"}
          </button>
          <button className="danger" onClick={() => run(() => api.control("all", "stop"))}>Stop all</button>
        </header>
        {error && <div className="error bar" onClick={() => setError("")}>{error}</div>}
        {settings.paused && <div className="bar">All agents are paused. No turns are handed out.</div>}
        {dialog === "room" && (
          <RoomDialog agents={agents} run={run} onDone={(room) => {
            setDialog(null);
            if (room) { void loadConversations(); setSelected(room); }
          }} />
        )}
        {dialog === "members" && conversation?.kind === "room" && (
          <RoomMembers room={conversation} agents={agents} run={run} onDone={() => { setDialog(null); void loadConversations(); }} />
        )}
        {selected === TASKS ? (
          <TaskBoard tasks={tasks} conversations={conversations} agents={agents} names={names} filters={filters}
            onFilters={setFilters} onOpen={openTask} run={run} />
        ) : (
          <>
            <MessageList messages={messages} names={names} conversations={conversations}
              tagged={selected === ACTIVITY} onContinue={(id) => run(() => api.continueChain(id))} onOpenTask={openTask} />
            <Composer key={selected} conversation={conversation} activity={selected === ACTIVITY}
              conversations={conversations} names={names} onSelect={setSelected}
              onSend={(body) => run(() => api.post(selected, body))} />
          </>
        )}
      </main>

      <aside className="right">
        {drawer ? (
          <TaskDrawer taskId={drawer} conversations={conversations} names={names} version={drawerTick} run={run}
            onOpen={openTask} onClose={() => setDrawer(null)} onSeen={markSeen} />
        ) : (
          <>
            <h2>Agents</h2>
            {agents.map((a) => (
              <AgentCard key={a.id} agent={a} selected={a.id === agentId} run={run}
                onSelect={() => setAgentId(a.id === agentId ? null : a.id)} onTasks={() => showTasksOf(a.id)} />
            ))}
            {agents.length === 0 && <p className="muted">No agents in agents.yaml.</p>}
            {agent && <TracePanel agent={agent} trace={trace} tasks={tasks} onOpenTask={openTask} />}
          </>
        )}
      </aside>
    </div>
  );
}

function MessageList(props: {
  messages: Message[]; names: Record<string, string>; conversations: Conversation[]; tagged: boolean;
  onContinue: (id: string) => void; onOpenTask: (task: string) => void;
}) {
  const { messages, names, conversations, tagged, onContinue, onOpenTask } = props;
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    if (pinned.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [messages]);
  const onScroll = () => {
    const el = box.current!;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  return (
    <div className="messages" ref={box} onScroll={onScroll}>
      {messages.length === 0 && <p className="muted empty">No messages yet.</p>}
      {messages.map((m) => {
        const where = tagged && (
          <span className="tag">{conversationLabel(conversations.find((c) => c.id === m.conversation_id), m.conversation_id)}</span>
        );
        if (m.kind === "task" && m.task) {
          // A task change: one compact line naming the actor, with a link that opens the task.
          const task = m.task;
          const rest = m.body.replace(task.ref, "\u0000").split("\u0000");
          return (
            <div key={m.id} className="task-line">
              <span aria-hidden="true">▣</span>
              {where}
              <span>{rest[0]}<button className="link" onClick={() => onOpenTask(task.id)}>{task.ref}</button>{rest.slice(1).join(task.ref)}</span>
              <time className="muted" dateTime={m.at}>{time(m.at)}</time>
              {m.hop > 0 && <span className="muted small">hop {m.hop}</span>}
            </div>
          );
        }
        if (m.kind === "notice") {
          return (
            <div key={m.id} className="notice">
              {where} {m.body}
              {m.reply_to && <button onClick={() => onContinue(m.id)}>Continue</button>}
            </div>
          );
        }
        return (
          <article key={m.id} className={m.role === "owner" ? "owner" : ""}>
            <div className="meta">
              <strong style={{ color: `hsl(${hue(m.from)} 55% 48%)` }}>{names[m.from] ?? m.from}</strong>
              {where}
              <time dateTime={m.at}>{time(m.at)}</time>
              {m.hop > 0 && <span className="muted" title="Agent-to-agent steps since the owner spoke">hop {m.hop}</span>}
            </div>
            <div className="body" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.body) }} />
          </article>
        );
      })}
    </div>
  );
}

function Composer(props: {
  conversation: Conversation | undefined; activity: boolean; conversations: Conversation[];
  names: Record<string, string>; onSelect: (id: string) => void; onSend: (body: string) => Promise<void>;
}) {
  const { conversation, activity, conversations, names, onSelect, onSend } = props;
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [pick, setPick] = useState(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const readOnly = activity || !conversation || conversation.observed || !!conversation.archived_at;
  const targets = conversations.filter((c) => !c.observed && !c.archived_at);

  // "@" autocomplete for the members of a room.
  const match = conversation?.kind === "room" ? /(^|\s)@([\w-]*)$/.exec(text.slice(0, caret)) : null;
  const options = match
    ? ["all", ...conversation!.members.filter((m) => m !== "owner")].filter((m) => m.startsWith(match[2].toLowerCase()))
    : [];
  const complete = (name: string) => {
    const start = caret - match![2].length;
    const next = `${text.slice(0, start)}${name} ${text.slice(caret)}`;
    setText(next);
    setCaret(start + name.length + 1);
    setPick(0);
    input.current?.focus();
  };
  const send = async () => {
    const body = text.trim();
    if (!body || readOnly) return;
    setText("");
    await onSend(body);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (options.length && (event.key === "Tab" || event.key === "Enter")) {
      event.preventDefault();
      complete(options[Math.min(pick, options.length - 1)]);
    } else if (options.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      setPick((n) => (n + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
    } else if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };
  return (
    <footer className="composer">
      <label>
        To{" "}
        <select value={readOnly ? "" : conversation!.id} onChange={(e) => onSelect(e.target.value)}>
          {readOnly && <option value="">(read-only view)</option>}
          {targets.map((c) => (
            <option key={c.id} value={c.id}>
              {c.kind === "room" ? `Everyone in ${conversationLabel(c, c.id)}` : names[c.members.find((m) => m !== "owner")!] ?? c.id}
            </option>
          ))}
        </select>
      </label>
      <div className="field">
        {options.length > 0 && (
          <ul className="suggest">
            {options.map((name, index) => (
              <li key={name} className={index === pick ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); complete(name); }}>
                @{name}
              </li>
            ))}
          </ul>
        )}
        <textarea ref={input} rows={2} value={text} disabled={readOnly} autoFocus
          placeholder={readOnly ? "You are observing. Post in the room or DM an agent to step in."
            : conversation!.kind === "room" ? "Message the room. @name gives an agent a turn; @all asks everyone."
            : "Message (a DM always gives the agent a turn)"}
          onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart); setPick(0); }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart)} onKeyDown={onKeyDown} />
      </div>
      <button className="primary" disabled={readOnly || !text.trim()} onClick={() => void send()}>Send</button>
    </footer>
  );
}

function AgentCard(props: {
  agent: Agent; selected: boolean; onSelect: () => void; onTasks: () => void;
  run: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const { agent, selected, onSelect, onTasks, run } = props;
  const [editing, setEditing] = useState(false);
  const [usd, setUsd] = useState(String(agent.budget.usd_per_day ?? ""));
  const [turns, setTurns] = useState(String(agent.budget.turns_per_day ?? ""));
  const save = () => run(async () => {
    await api.budget(agent.id, {
      ...(usd.trim() ? { usd_per_day: Number(usd) } : {}),
      ...(turns.trim() ? { turns_per_day: Number(turns) } : {}),
    });
    setEditing(false);
  });
  const limit = [
    agent.budget.usd_per_day != null && `$${agent.budget.usd_per_day}/day`,
    agent.budget.turns_per_day != null && `${agent.budget.turns_per_day} turns/day`,
  ].filter(Boolean).join(", ");
  return (
    <section className={`agent ${selected ? "on" : ""}`}>
      <button className="who" onClick={onSelect} title={agent.description}>
        <span className={`dot ${agent.status}`} />
        <strong>{agent.display_name}</strong>
        <span className="muted">{agent.status}</span>
      </button>
      <div className="muted small">
        {agent.connector === "none" ? `self-driven, ${agent.delivery}` : agent.connector}
        {" · "}${agent.usd_today.toFixed(2)} and {agent.turns_today} turns today
        {limit && ` · limit ${limit}`}
        {agent.queued > 0 && ` · ${agent.queued} queued`}
      </div>
      {agent.open_tasks > 0 && (
        <button className="link small" onClick={onTasks}>
          {agent.open_tasks} open task{agent.open_tasks > 1 ? "s" : ""}
        </button>
      )}
      {!agent.has_token && <div className="error small">No token. Run: agent-hub token {agent.id}</div>}
      {agent.modes.length > 0 && (
        <label className="row small mode" title="How much this agent may do without asking you. Applies from its next turn.">
          Mode{" "}
          <select value={agent.mode ?? ""} onChange={(e) => run(() => api.agentMode(agent.id, e.target.value))}>
            {agent.modes.map((mode) => <option key={mode} value={mode}>{MODE_NAMES[mode] ?? mode}</option>)}
          </select>
        </label>
      )}
      {agent.pending_permissions.map((request) => (
        <div key={request.id} className="approval permission small">
          <span>Wants to use <strong>{request.tool}</strong> in {request.conversation_id}</span>
          <code>{request.summary}</code>
          <span className="row">
            <button className="primary" onClick={() => run(() => api.permission(request.id, "allow"))}>Allow</button>
            <button className="danger" onClick={() => run(() => api.permission(request.id, "deny"))}>Deny</button>
          </span>
        </div>
      ))}
      {agent.awaiting_approval.map((turn) => (
        <div key={turn.id} className="approval small">
          Wants a turn in {turn.conversation_id}
          <button onClick={() => run(() => api.decide(turn.id, "approve"))}>Approve</button>
          <button onClick={() => run(() => api.decide(turn.id, "reject"))}>Reject</button>
        </div>
      ))}
      <div className="row">
        <button onClick={() => run(() => api.control(agent.id, agent.status === "paused" ? "resume" : "pause"))}>
          {agent.status === "paused" ? "Resume" : "Pause"}
        </button>
        <button onClick={() => run(() => api.control(agent.id, "stop"))}>Stop</button>
        <button onClick={() => setEditing(!editing)}>Budget</button>
      </div>
      {editing && (
        <div className="row small">
          <input aria-label="USD per day" placeholder="$ / day" inputMode="decimal" value={usd} onChange={(e) => setUsd(e.target.value)} />
          <input aria-label="Turns per day" placeholder="turns / day" inputMode="numeric" value={turns} onChange={(e) => setTurns(e.target.value)} />
          <button className="primary" onClick={save}>Save</button>
        </div>
      )}
    </section>
  );
}

function traceLine(event: TraceEvent): string {
  const payload = event.payload ?? {};
  const text = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value));
  if (event.type === "tool_call") return `${payload.tool ?? "tool"} ${text(payload.args ?? "")}`;
  if (event.type === "tool_result") return `→ ${text(payload.result ?? "")}`;
  if (event.type === "text_delta") return payload.text ?? "";
  return `${event.type}: ${text(payload)}`;
}

function TracePanel({ agent, trace, tasks, onOpenTask }: {
  agent: Agent; trace: Trace | null; tasks: Task[]; onOpenTask: (task: string) => void;
}) {
  const task = trace?.turn.task_id ? tasks.find((t) => t.id === trace.turn.task_id) : undefined;
  return (
    <section className="trace">
      <h2>Trace: {agent.display_name}</h2>
      {!trace && <p className="muted">No turn yet.</p>}
      {trace && (
        <>
          <p className="muted small">
            {trace.turn.state} in {trace.turn.conversation_id}
            {trace.turn.error && ` (${trace.turn.error})`}
            {trace.turn.cost_usd != null && ` · $${trace.turn.cost_usd.toFixed(4)}`}
            {task && <> · about <button className="link" onClick={() => onOpenTask(task.id)}>{task.ref}</button></>}
          </p>
          {trace.events.length === 0 && <p className="muted small">Nothing recorded for this turn.</p>}
          <ol>
            {trace.events.map((event) => (
              <li key={event.id} className={event.type}>{traceLine(event).slice(0, 400)}</li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
