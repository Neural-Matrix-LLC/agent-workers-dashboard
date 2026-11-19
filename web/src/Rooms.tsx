import { FormEvent, useState } from "react";
import { Agent, api, Conversation } from "./api";
import { Run } from "./ui";

/** Create a room at run time (design M5d). Rooms in agents.yaml are edited in the file. */
export function RoomDialog(props: { agents: Agent[]; run: Run; onDone: (room: string | null) => void }) {
  const { agents, run, onDone } = props;
  const [slug, setSlug] = useState("");
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState("mention");
  const [members, setMembers] = useState<string[]>(agents.map((a) => a.id));
  const valid = /^[a-z0-9][a-z0-9-]{0,31}$/.test(slug);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const room = await api.createRoom({ id: `room:${slug}`, ...(title.trim() ? { title: title.trim() } : {}), mode, members });
      onDone(room.id);
    });
  };
  return (
    <form className="dialog" onSubmit={submit}>
      <h3>New room</h3>
      <label>Id <span className="muted small">room:</span>
        <input autoFocus required value={slug} placeholder="research" onChange={(e) => setSlug(e.target.value.toLowerCase())} />
      </label>
      {slug && !valid && <p className="error small">Use a-z, 0-9 and -, up to 32 characters.</p>}
      <label>Title <input value={title} placeholder="Research" onChange={(e) => setTitle(e.target.value)} /></label>
      <label>Mode
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="mention">mention</option><option value="round_robin">round robin</option>
        </select>
      </label>
      <MemberChecks agents={agents} members={members} onChange={setMembers} />
      <div className="row">
        <button type="submit" className="primary" disabled={!valid}>Create</button>
        <button type="button" onClick={() => onDone(null)}>Cancel</button>
      </div>
    </form>
  );
}

export function RoomMembers(props: { room: Conversation; agents: Agent[]; run: Run; onDone: () => void }) {
  const { room, agents, run, onDone } = props;
  const [members, setMembers] = useState(room.members.filter((m) => m !== "owner"));
  const [title, setTitle] = useState(room.title ?? "");
  const [result, setResult] = useState("");
  const fromFile = room.source !== "ui";
  const save = () => run(async () => {
    if ((title || null) !== room.title) await api.patchRoom(room.id, { title: title.trim() || null });
    if (!fromFile) {
      const changed = await api.roomMembers(room.id, members);
      const parts = [
        changed.added.length && `added ${changed.added.join(", ")}`,
        changed.removed.length && `removed ${changed.removed.join(", ")}`,
        changed.released_tasks.length && `unassigned ${changed.released_tasks.join(", ")}`,
      ].filter(Boolean);
      setResult(parts.length ? `Done: ${parts.join("; ")}.` : "No change.");
    } else {
      onDone();
    }
  });
  const archive = () => {
    if (!confirm(`Archive ${room.id}? It becomes read-only for everyone, and its agents' turns there are cancelled.`)) return;
    void run(async () => { await api.archiveRoom(room.id); onDone(); });
  };
  return (
    <div className="dialog">
      <h3>{room.id}</h3>
      <label>Title <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      {fromFile ? (
        <p className="muted small">This room is defined in agents.yaml. Change its members there and restart the hub.</p>
      ) : (
        <>
          <MemberChecks agents={agents} members={members} onChange={setMembers} />
          <p className="muted small">A removed agent loses access at once: its turns here are cancelled and its open
            tasks here are unassigned. A new member starts reading at the latest message.</p>
        </>
      )}
      {result && <p className="small">{result}</p>}
      <div className="row">
        <button className="primary" onClick={save}>Save</button>
        <button onClick={onDone}>Close</button>
        <span className="spacer" />
        {room.id !== "room:main" && <button className="danger" onClick={archive}>Archive</button>}
      </div>
    </div>
  );
}

function MemberChecks(props: { agents: Agent[]; members: string[]; onChange: (members: string[]) => void }) {
  const { agents, members, onChange } = props;
  return (
    <fieldset className="members">
      <legend className="small">Members (you are always one)</legend>
      {agents.map((agent) => (
        <label key={agent.id} className="small">
          <input type="checkbox" checked={members.includes(agent.id)}
            onChange={(e) => onChange(e.target.checked ? [...members, agent.id] : members.filter((m) => m !== agent.id))} />
          {" "}{agent.display_name}
        </label>
      ))}
    </fieldset>
  );
}
