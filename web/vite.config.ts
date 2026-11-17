import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the UI on :5173 for UI development and forwards /api to the hub.
// The hub address comes from $AGENT_HUB_URL, else from hub.url or the first hub.bind in
// ../agents.yaml (the repo root), else http://127.0.0.1:8700.
function hubUrl(): string {
  if (process.env.AGENT_HUB_URL) return process.env.AGENT_HUB_URL;
  let text: string;
  try {
    text = readFileSync(fileURLToPath(new URL("../agents.yaml", import.meta.url)), "utf8");
  } catch {
    return "http://127.0.0.1:8700";
  }
  const hub = text.split(/^(?=\S)/m).find((section) => section.startsWith("hub:")) ?? "";
  const url = hub.match(/^\s+url:\s*["']?([^\s"'#]+)/m);
  if (url) return url[1];
  const bind = hub.match(/^\s+bind:\s*\[?\s*["']?([^\s"',\]#]+)/m);
  if (!bind) return "http://127.0.0.1:8700";
  return "http://" + bind[1].replace(/^0\.0\.0\.0:/, "127.0.0.1:");
}

const hub = hubUrl();
console.log(`Agent Hub UI dev server: forwarding /api to ${hub}`);

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: hub, ws: true } } },
});
