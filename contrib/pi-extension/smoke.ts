// Smoke test: drive the real kioku-mesh-mcp server through KiokuMcpClient.
// Run with: npx tsx smoke.ts   (not shipped — dev scratch file)
import { KiokuMcpClient } from "./src/index.ts";

const client = new KiokuMcpClient();

function text(r: { content?: Array<{ type: string; text?: string }> }): string {
  return (r.content ?? []).map((c) => c.text ?? "").join("\n");
}

const saved = await client.callTool("save_observation", {
  content: "Smoke test: pi-kioku-mesh extension shim talks JSON-RPC to kioku-mesh-mcp.",
  subject: "pi-kioku-mesh smoke test",
  summary: "pi extension shim round-trip works",
  project: "pi-kioku-mesh",
  memory_type: "note",
  tags: ["smoke-test"],
  ttl_sec: 3600,
});
console.log("save_observation →", text(saved));

const found = await client.callTool("search_memory", { query: "pi-kioku-mesh smoke" });
console.log("search_memory →\n", text(found));

const recall = await client.callTool("recall_context", { project: "pi-kioku-mesh", limit: 5 });
console.log("recall_context →\n", text(recall));

const id = text(saved).match(/[0-9a-f]{32}/)?.[0];
if (id) {
  const got = await client.callTool("get_memory", { observation_id: id });
  console.log("get_memory →\n", text(got).slice(0, 400));
}

const bad = await client.callTool("get_memory", { observation_id: "tooshort" });
console.log("get_memory (invalid id) →", text(bad), "isError:", bad.isError ?? false);

client.stop();
