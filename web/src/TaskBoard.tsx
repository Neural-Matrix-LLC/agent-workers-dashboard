import { FormEvent, useMemo, useState } from "react";
import { Agent, api, Conversation, Task, TaskState } from "./api";
import { conversationLabel, Run } from "./ui";

export type BoardFilters = { conversation: string; assignee: string; label: string; q: string };
export const NO_FILTERS: BoardFilters = { conversation: "", assignee: "", label: "", q: "" };

const COLUMNS: { title: string; states: TaskState[] }[] = [
  { title: "Open", states: ["open"] },
  { title: "Assigned", states: ["assigned"] },
  { title: "In progress", states: ["in_progress"] },
  { title: "Blocked", states: ["blocked"] },
  { title: "Done", states: ["done"] },
];
const CLOSED: TaskState[] = ["failed", "cancelled"];
const PRIORITY = { high: 0, normal: 1, low: 2 };

/** Filters live in the URL hash (#tasks?c=room:main&a=hermes) so a view can be bookmarked. */
export function boardFromHash(hash: string): BoardFilters | null {
  if (!hash.startsWith("#tasks")) return null;
  const params = new URLSearchParams(hash.split("?")[1] ?? "");
  return { conversation: params.get("c") ?? "", assignee: params.get("a") ?? "", label: params.get("l") ?? "",
    q: params.get("q") ?? "" };
}

export function boardHash(filters: BoardFilters): string {
  const params = new URLSearchParams(Object.entries({ c: filters.conversation, a: filters.assignee,
    l: filters.label, q: filters.q }).filter(([, v]) => v));
  const text = params.toString();
  return `#tasks${text ? `?${text}` : ""}`;
}

function sortCards(a: Task, b: Task): number {
  return PRIORITY[a.priority] - PRIORITY[b.priority] || b.updated_at.localeCompare(a.updated_at);
}

export function TaskBoard(props: {
  tasks: Task[]; conversations: Conversation[]; agents: Agent[]; names: Record<string, string>;
  filters: BoardFilters; onFilters: (filters: BoardFilters) => void; onOpen: (task: string) => void; run: Run;
}) {
  const { tasks, conversations, agents, names, filters, onFilters, onOpen, run } = props;
  const [showClosed, setShowClosed] = useState(false);
  const [creating, setCreating] = useState(false);
  const byId = useMemo(() => Object.fromEntries(tasks.map((t) => [t.id, t])), [tasks]);
  const labels = useMemo(() => [...new Set(tasks.flatMap((t) => t.labels))].sort(), [tasks]);
  const status = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.status])), [agents]);
  const shown = tasks.filter((t) =>
    (!filters.conversation || t.conversation_id === filters.conversation) &&
    (!filters.assignee || (filters.assignee === "unassigned" ? !t.assignee : t.assignee === filters.assignee)) &&
    (!filters.label || t.labels.includes(filters.label)) &&
    (!filters.q || t.title.toLowerCase().includes(filters.q.toLowerCase())));
  const set = (patch: Partial<BoardFilters>) => onFilters({ ...filters, ...patch });
  const waitingOn = (task: Task) =>
    tasks.filter((t) => t.parent_id === task.id && !["done", "failed", "cancelled"].includes(t.state)).map((t) => t.ref);

  const card = (task: Task) => {
    const conversation = conversations.find((c) => c.id === task.conversation_id);
    const waiting = task.children.open > 0 ? waitingOn(task) : [];
    return (
      <button key={task.id} className={`card priority-${task.priority} ${conversation?.observed ? "observed" : ""}`}
        onClick={() => onOpen(task.id)}>
        <span className="small muted">{task.ref}</span>
        <strong>{task.title}</strong>
        <span className="small card-meta">
          {task.assignee ? (
            <span><span className={`dot ${status[task.assignee] ?? "idle"}`} /> @{names[task.assignee] ?? task.assignee}</span>
          ) : <span className="muted">unassigned</span>}
          <span className="tag">{conversationLabel(conversation, task.conversation_id)}{conversation?.observed ? " · observe" : ""}</span>
          {task.priority !== "normal" && <span className="tag">{task.priority}</span>}
          {task.labels.map((label) => <span key={label} className="tag">{label}</span>)}
          {task.children.total > 0 && <span className="muted">{task.children.done}/{task.children.total} done</span>}
          {task.artifact_count > 0 && <span className="muted">{task.artifact_count} file{task.artifact_count > 1 ? "s" : ""}</span>}
        </span>
        {task.parent_id && byId[task.parent_id] && <span className="small muted">under {byId[task.parent_id].ref}</span>}
        {waiting.length > 0 && <span className="small muted">waiting on {waiting.join(", ")}</span>}
      </button>
    );
  };

  return (
    <div className="board">
      <div className="filters">
        <select aria-label="Conversation" value={filters.conversation} onChange={(e) => set({ conversation: e.target.value })}>
          <option value="">All conversations</option>
          {conversations.map((c) => <option key={c.id} value={c.id}>{conversationLabel(c, c.id)}</option>)}
        </select>
        <select aria-label="Assignee" value={filters.assignee} onChange={(e) => set({ assignee: e.target.value })}>
          <option value="">Anyone</option>
          <option value="unassigned">Unassigned</option>
          <option value="owner">{names.owner ?? "Owner"}</option>
          {agents.map((a) => <option key={a.id} value={a.id}>{a.display_name}</option>)}
        </select>
        {labels.length > 0 && (
          <select aria-label="Label" value={filters.label} onChange={(e) => set({ label: e.target.value })}>
            <option value="">Any label</option>
            {labels.map((label) => <option key={label} value={label}>{label}</option>)}
          </select>
        )}
        <input aria-label="Search titles" placeholder="Search titles" value={filters.q} onChange={(e) => set({ q: e.target.value })} />
        <span className="spacer" />
        <button className="primary" onClick={() => setCreating(!creating)}>+ New task</button>
      </div>
      {creating && (
        <NewTaskForm conversations={conversations} names={names} tasks={tasks} run={run}
          onDone={(id) => { setCreating(false); if (id) onOpen(id); }} />
      )}
      <div className="columns">
        {COLUMNS.map((column) => {
          const cards = shown.filter((t) => column.states.includes(t.state)).sort(sortCards);
          return (
            <section key={column.title} className="column">
              <h2>{column.title} ({cards.length})</h2>
              {cards.map(card)}
            </section>
          );
        })}
        <section className={`column ${showClosed ? "" : "collapsed"}`}>
          <button className="column-toggle" onClick={() => setShowClosed(!showClosed)}
            title="Failed and cancelled tasks">
            <h2>Failed / cancelled ({shown.filter((t) => CLOSED.includes(t.state)).length})</h2>
          </button>
          {showClosed && shown.filter((t) => CLOSED.includes(t.state)).sort(sortCards).map(card)}
        </section>
      </div>
      {tasks.length === 0 && <p className="muted empty">No tasks yet. Agents create them with create_task, or use + New task.</p>}
    </div>
  );
}

function NewTaskForm(props: {
  conversations: Conversation[]; names: Record<string, string>; tasks: Task[]; run: Run;
  onDone: (task: string | null) => void;
}) {
  const { conversations, names, tasks, run, onDone } = props;
  const homes = conversations.filter((c) => !c.observed && !c.archived_at);
  const [conversation, setConversation] = useState(homes[0]?.id ?? "");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [assignee, setAssignee] = useState("");
  const [parent, setParent] = useState("");
  const [priority, setPriority] = useState("normal");
  const [labels, setLabels] = useState("");
  const members = conversations.find((c) => c.id === conversation)?.members ?? [];
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const { task } = await api.createTask({
        conversation_id: conversation, title: title.trim(),
        ...(description.trim() ? { description } : {}), ...(acceptance.trim() ? { acceptance } : {}),
        ...(assignee ? { assignee } : {}), ...(parent ? { parent_id: parent } : {}), priority,
        labels: labels.split(",").map((l) => l.trim()).filter(Boolean),
      });
      onDone(task.id);
    });
  };
  return (
    <form className="new-task" onSubmit={submit}>
      <label>Conversation
        <select value={conversation} onChange={(e) => { setConversation(e.target.value); setAssignee(""); }}>
          {homes.map((c) => <option key={c.id} value={c.id}>{conversationLabel(c, c.id)}</option>)}
        </select>
      </label>
      <label>Title<input required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>Description<textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <label>Acceptance<textarea rows={2} value={acceptance} onChange={(e) => setAcceptance(e.target.value)} /></label>
      <div className="row">
        <label>Assignee
          <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
            <option value="">Nobody (open)</option>
            {members.map((m) => <option key={m} value={m}>{names[m] ?? m}</option>)}
          </select>
        </label>
        <label>Priority
          <select value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="low">low</option><option value="normal">normal</option><option value="high">high</option>
          </select>
        </label>
        <label>Parent
          <select value={parent} onChange={(e) => setParent(e.target.value)}>
            <option value="">None</option>
            {tasks.filter((t) => !["done", "failed", "cancelled"].includes(t.state)).map((t) => (
              <option key={t.id} value={t.id}>{t.ref} {t.title.slice(0, 40)}</option>
            ))}
          </select>
        </label>
        <label>Labels<input placeholder="tests, ui" value={labels} onChange={(e) => setLabels(e.target.value)} /></label>
      </div>
      <div className="row">
        <button type="submit" className="primary" disabled={!title.trim() || !conversation}>Create</button>
        <button type="button" onClick={() => onDone(null)}>Cancel</button>
      </div>
    </form>
  );
}
