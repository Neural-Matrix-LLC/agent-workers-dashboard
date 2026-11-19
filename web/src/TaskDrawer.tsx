import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Artifact, Conversation, TaskAction, TaskDetail } from "./api";
import { renderMarkdown, Run, size, time } from "./ui";

const TERMINAL = ["done", "failed", "cancelled"];
const NON_TERMINAL = ["open", "assigned", "in_progress", "blocked"];

/** The owner's actions, from the transition table in design M4 section 5.2 (the owner may take every one). */
const FROM: Record<string, string[]> = {
  assign: ["open", "assigned"], unassign: ["assigned"], start: ["assigned"], block: ["in_progress"],
  unblock: ["blocked"], finish: ["assigned", "in_progress", "blocked"], fail: ["assigned", "in_progress", "blocked"],
  cancel: NON_TERMINAL, reopen: ["done", "failed"], note: [...NON_TERMINAL, ...TERMINAL],
};
// In an agent-to-agent DM the owner may only moderate (open decision OD-3).
const MODERATION = ["assign", "unassign", "cancel"];
const NEEDS: Record<string, { field: keyof TaskAction; label: string } | undefined> = {
  block: { field: "blocked_reason", label: "Why is it blocked?" },
  finish: { field: "result", label: "Result" }, fail: { field: "result", label: "What went wrong?" },
  reopen: { field: "note", label: "Why reopen it?" }, note: { field: "note", label: "Note (@name wakes a member)" },
  cancel: { field: "note", label: "Reason (optional)" },
};
const LABELS: Record<string, string> = {
  assign: "Reassign", unassign: "Unassign", start: "Start", block: "Block", unblock: "Unblock", finish: "Finish",
  fail: "Fail", cancel: "Cancel", reopen: "Reopen", note: "Add note",
};

const words = (state: string | null) => (state ?? "").replace("_", " ");

function eventLine(event: TaskDetail["events"][number]): string {
  const who = event.actor;
  switch (event.type) {
    case "created": return `created by ${who}${event.data.assignee ? `, assigned to ${event.data.assignee}` : ""}`;
    case "claimed": return `claimed by ${who}`;
    case "assigned": return `${who} assigned it to ${event.data.assignee}`;
    case "released": return event.data.unassign ? `${who} unassigned ${event.data.previous}` : `released by ${who}`;
    case "state": return `${who}: ${words(event.from_state)} → ${words(event.to_state)}${event.note ? ` (${event.note.slice(0, 200)})` : ""}`;
    case "note": return `${who}: ${event.note ?? ""}`;
    case "edited": return `${who} edited ${(event.data.fields ?? []).join(", ")}`;
    case "artifact": return event.data.removed ? `file ${event.data.name ?? ""} removed` : `${who} attached ${event.data.name}`;
    case "children_done": return `all subtasks finished${event.data.private ? " (private: shown to you only)" : ""}`;
    default: return `${event.type} by ${who}`;
  }
}

export function TaskDrawer(props: {
  taskId: string; conversations: Conversation[]; names: Record<string, string>; version: number; run: Run;
  onOpen: (task: string) => void; onClose: () => void; onSeen: (task: string, version: number) => void;
}) {
  const { taskId, conversations, names, version, run, onOpen, onClose, onSeen } = props;
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<string | null>(null);     // the action whose text box is open
  const [text, setText] = useState("");
  const [assignee, setAssignee] = useState("");
  const [preview, setPreview] = useState<{ artifact: Artifact; html?: string; text?: string; url?: string } | null>(null);
  const load = useCallback(() => run(async () => setTask(await api.task(taskId))), [taskId, run]);

  useEffect(() => { setNotice(""); setPending(null); setPreview(null); }, [taskId]);
  useEffect(() => void load(), [load, version]);          // `version` changes on every live task event
  useEffect(() => { if (task) onSeen(task.id, task.version); }, [task, onSeen]);
  useEffect(() => () => { if (preview?.url) URL.revokeObjectURL(preview.url); }, [preview]);

  if (!task) return <aside className="drawer"><p className="muted">Loading…</p></aside>;
  const conversation = conversations.find((c) => c.id === task.conversation_id);
  const observed = !!conversation?.observed;
  const archived = !!conversation?.archived_at;
  const members = (conversation?.members ?? []).filter((m) => m !== task.assignee);
  const actions = archived ? [] : Object.keys(FROM).filter((a) =>
    FROM[a].includes(task.state) && (!observed || MODERATION.includes(a)) && (a !== "assign" || members.length > 0));

  const send = (action: string, extra: Partial<TaskAction> = {}) => run(async () => {
    const body: TaskAction = { action, expected_version: task.version, ...extra };
    const need = NEEDS[action];
    if (need && text.trim()) (body as any)[need.field] = text.trim();
    if (action === "cancel" && task.children.open > 0) {
      if (!confirm(`${task.ref} has ${task.children.open} unfinished subtasks. Cancel them too?`)) return;
      body.cascade = true;
    }
    if (action === "finish" && task.children.open > 0) {
      if (!confirm(`${task.ref} has ${task.children.open} unfinished subtasks. Finish it anyway?`)) return;
      body.force = true;
    }
    try {
      await api.patchTask(task.id, body);
      setPending(null);
      setText("");
      setNotice("");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict") {
        const latest = await api.task(task.id);
        setTask(latest);
        setNotice(`Changed by ${latest.events.at(-1)?.actor ?? "someone"}; reloaded. Check it and try again.`);
        return;
      }
      throw e;
    }
  });
  const start = (action: string) => {
    const need = NEEDS[action];
    if (!need && action !== "assign") return void send(action);
    setPending(action);
    setText("");
    setAssignee(members[0] ?? "");
  };

  const view = (artifact: Artifact) => run(async () => {
    const blob = await api.artifactContent(artifact.id);
    if (artifact.media_type.startsWith("image/")) {
      setPreview({ artifact, url: URL.createObjectURL(blob) });          // a blob: URL, never the artifact URL
    } else if (artifact.media_type === "text/markdown") {
      setPreview({ artifact, html: renderMarkdown(await blob.text()) });
    } else {
      setPreview({ artifact, text: await blob.text() });                  // shown as text, never as HTML
    }
  });
  const download = (artifact: Artifact) => run(async () => {
    const url = URL.createObjectURL(await api.artifactContent(artifact.id));
    const link = document.createElement("a");
    link.href = url;
    link.download = artifact.name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const upload = (file: File | undefined) => file && run(async () => { await api.uploadArtifact(task.id, file); await load(); });
  const parent = task.parent_id;

  return (
    <aside className="drawer" aria-label={`Task ${task.ref}`}>
      <header>
        <span className="muted small">{task.ref}</span>
        <h2 className="drawer-title">{task.title}</h2>
        <button aria-label="Close" onClick={onClose}>×</button>
      </header>
      {notice && <div className="bar">{notice}</div>}
      <p className="small">
        State: <strong>{task.state.replace("_", " ")}</strong> · Assignee: <strong>{task.assignee ? names[task.assignee] ?? task.assignee : "nobody"}</strong>
        {" "}· v{task.version} · Priority: {task.priority}
        {observed && <span className="tag">observe</span>}
      </p>
      <p className="small muted">
        Created by {task.creator} {time(task.created_at)}
        {parent && <> · Parent: <button className="link" onClick={() => onOpen(parent)}>open parent</button></>}
        {task.labels.length > 0 && <> · {task.labels.map((l) => <span key={l} className="tag">{l}</span>)}</>}
      </p>
      {task.blocked_reason && <div className="bar">Blocked: {task.blocked_reason}</div>}
      {task.description && <div className="body" dangerouslySetInnerHTML={{ __html: renderMarkdown(task.description) }} />}
      {task.acceptance && <><h3>Acceptance</h3><div className="body" dangerouslySetInnerHTML={{ __html: renderMarkdown(task.acceptance) }} /></>}
      {task.result && <><h3>Result</h3><div className="body" dangerouslySetInnerHTML={{ __html: renderMarkdown(task.result) }} /></>}

      <h3>Subtasks ({task.children_list.length})</h3>
      {task.children_list.map((child) => (
        <button key={child.id} className="link block" onClick={() => onOpen(child.id)}>
          {child.ref} {child.title} — {child.state.replace("_", " ")}{child.assignee ? ` (@${child.assignee})` : ""}
        </button>
      ))}

      <h3>Files ({task.artifacts.length})</h3>
      {task.artifacts.map((artifact) => (
        <div key={artifact.id} className={`file small ${artifact.deleted_at ? "removed" : ""}`}>
          <span>{artifact.name} <span className="muted">{size(artifact.size)} · {artifact.uploader}</span></span>
          {!artifact.deleted_at && (
            <span className="row">
              {artifact.media_type !== "application/pdf" && <button onClick={() => view(artifact)}>View</button>}
              <button onClick={() => download(artifact)}>Download</button>
              <button className="danger" onClick={() => run(async () => { await api.deleteArtifact(artifact.id); await load(); })}>Remove</button>
            </span>
          )}
          {artifact.deleted_at && <span className="muted">removed</span>}
        </div>
      ))}
      {preview && (
        <div className="preview">
          <div className="row small"><strong>{preview.artifact.name}</strong><span className="spacer" />
            <button onClick={() => setPreview(null)}>Close preview</button></div>
          {preview.url && <img src={preview.url} alt={preview.artifact.name} />}
          {preview.html !== undefined && <div className="body" dangerouslySetInnerHTML={{ __html: preview.html }} />}
          {preview.text !== undefined && <pre>{preview.text}</pre>}
        </div>
      )}
      {!observed && !archived && (
        <label className="small upload">Upload a file <input type="file" onChange={(e) => upload(e.target.files?.[0])} /></label>
      )}

      <h3>Actions</h3>
      {actions.length === 0 && <p className="muted small">{archived ? "The room is archived." : "No actions in this state."}</p>}
      <div className="row">
        {actions.map((action) => (
          <button key={action} className={action === "cancel" ? "danger" : ""} onClick={() => start(action)}>{LABELS[action]}</button>
        ))}
      </div>
      {pending && (
        <div className="action-form">
          {pending === "assign" && (
            <select aria-label="New assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              {members.map((m) => <option key={m} value={m}>{names[m] ?? m}</option>)}
            </select>
          )}
          {NEEDS[pending] && (
            <textarea rows={3} aria-label={NEEDS[pending]!.label} placeholder={NEEDS[pending]!.label}
              value={text} onChange={(e) => setText(e.target.value)} />
          )}
          <div className="row">
            <button className="primary" disabled={pending !== "cancel" && pending !== "assign" && !text.trim()}
              onClick={() => send(pending, pending === "assign" ? { assignee } : {})}>{LABELS[pending]}</button>
            <button onClick={() => setPending(null)}>Back</button>
          </div>
        </div>
      )}

      <h3>History</h3>
      <ol className="history small">
        {task.events.map((event) => (
          <li key={event.id}><time className="muted">{time(event.at)}</time> {eventLine(event)}</li>
        ))}
      </ol>
    </aside>
  );
}
