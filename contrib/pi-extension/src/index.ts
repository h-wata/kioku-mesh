/**
 * pi-kioku-mesh — kioku-mesh shared memory tools for the pi coding agent.
 *
 * A thin shim over the `kioku-mesh-mcp` stdio server (installed separately via
 * `uv tool install kioku-mesh`). The server is spawned lazily on the first tool
 * call and spoken to over plain JSON-RPC — no MCP SDK dependency, so the whole
 * extension stays a single file with zero runtime deps.
 *
 * Agent identity (ADR-0019): agent_family / client_id are passed to the server
 * through environment variables, never through tool arguments. They default to
 * "pi"/"pi" and can be overridden with KIOKU_MESH_AGENT_FAMILY /
 * KIOKU_MESH_CLIENT_ID in the environment pi runs in.
 */
import { spawn, spawnSync } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const SERVER_COMMAND = process.env.KIOKU_MESH_MCP_COMMAND ?? "kioku-mesh-mcp";
const INSTALL_HINT =
  `${SERVER_COMMAND} not found on PATH. Install it with \`uv tool install kioku-mesh\` ` +
  "(or `pip install kioku-mesh`), then run `kioku-mesh init --mode local`.";

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

interface McpToolResult {
  content?: Array<{ type: string; text?: string }>;
  isError?: boolean;
}

/**
 * Minimal JSON-RPC client for a single stdio MCP server.
 *
 * Spawned lazily on the first call; if the child dies, all in-flight calls are
 * rejected and the next call respawns it.
 */
export class KiokuMcpClient {
  private child: ChildProcessWithoutNullStreams | null = null;
  private starting: Promise<void> | null = null;
  private nextId = 1;
  private pending = new Map<number, PendingRequest>();

  async callTool(name: string, args: Record<string, unknown>): Promise<McpToolResult> {
    await this.ensureStarted();
    return (await this.request("tools/call", { name, arguments: args })) as McpToolResult;
  }

  stop(): void {
    this.child?.kill();
  }

  private async ensureStarted(): Promise<void> {
    if (this.child) return;
    this.starting ??= this.start().catch((error: Error) => {
      this.starting = null;
      throw error;
    });
    await this.starting;
  }

  private async start(): Promise<void> {
    const child = spawn(SERVER_COMMAND, [], {
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env,
        KIOKU_MESH_AGENT_FAMILY: process.env.KIOKU_MESH_AGENT_FAMILY ?? "pi",
        KIOKU_MESH_CLIENT_ID: process.env.KIOKU_MESH_CLIENT_ID ?? "pi",
      },
    });
    child.on("error", (error) => this.teardown(new Error(`${SERVER_COMMAND}: ${error.message}. ${INSTALL_HINT}`)));
    child.on("exit", (code) => this.teardown(new Error(`${SERVER_COMMAND} exited (code ${code ?? "?"})`)));
    createInterface({ input: child.stdout }).on("line", (line) => this.onLine(line));
    child.stderr.resume(); // drain server logs so the pipe never fills up
    this.child = child;
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "pi-kioku-mesh", version: "0.1.0" },
    });
    this.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  private teardown(error: Error): void {
    this.child = null;
    this.starting = null;
    const waiting = [...this.pending.values()];
    this.pending.clear();
    for (const waiter of waiting) waiter.reject(error);
  }

  private onLine(line: string): void {
    let message: { id?: unknown; method?: string; result?: unknown; error?: { message?: string } };
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (typeof message.id !== "number" || message.method !== undefined) return;
    const waiter = this.pending.get(message.id);
    if (!waiter) return;
    this.pending.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error.message ?? "kioku-mesh MCP error"));
    else waiter.resolve(message.result);
  }

  private request(method: string, params: unknown): Promise<unknown> {
    const id = this.nextId++;
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.send({ jsonrpc: "2.0", id, method, params });
    return promise;
  }

  private send(message: object): void {
    this.child?.stdin.write(`${JSON.stringify(message)}\n`);
  }
}

function textOf(result: McpToolResult | undefined): string {
  const content = Array.isArray(result?.content) ? result.content : [];
  return content
    .filter((item) => item?.type === "text" && typeof item.text === "string")
    .map((item) => item.text)
    .join("\n");
}

function serverBinaryOnPath(): boolean {
  const probe = process.platform === "win32" ? "where" : "which";
  return spawnSync(probe, [SERVER_COMMAND], { stdio: "ignore" }).status === 0;
}

const MemoryType = Type.Union([
  Type.Literal("note"),
  Type.Literal("decision"),
  Type.Literal("bug"),
  Type.Literal("pattern"),
  Type.Literal("config"),
  Type.Literal("summary"),
]);

const SearchMode = Type.Union([Type.Literal("and"), Type.Literal("or"), Type.Literal("and_or")]);

// Tool names, parameter schemas, and the behavioral guidance in the
// descriptions mirror kioku-mesh's own MCP server (src/kioku_mesh/mcp_server.py).
// Optional fields left unset fall through to the server-side defaults.
const TOOLS = [
  {
    name: "save_observation",
    label: "kioku-mesh: save",
    description:
      "Persist a work note / decision / discovery into the shared kioku-mesh memory. " +
      "Call this PROACTIVELY after ANY decision, bug fix, discovery, or convention — do not wait for the user to ask. " +
      "Save: design decisions, non-obvious bug root causes, reusable patterns, config changes with rationale, session conclusions. " +
      "Skip: routine status ticks, restated PR/commit content, transient notes, generic 'tests pass'. " +
      "Prefer memory_type decision/bug/pattern/config over summary. Returns the generated observation_id.",
    parameters: Type.Object({
      content: Type.String({ description: "Full-text body of the observation." }),
      subject: Type.String({
        description: 'Short topic / symbol name (e.g. "get_position latency"). Placeholders ("-", "N/A", "TBD") are rejected.',
      }),
      summary: Type.String({
        description: "One-line abstract shown in search results. Placeholders are rejected.",
      }),
      project: Type.Optional(Type.String({ description: "Project tag to scope the entry." })),
      tags: Type.Optional(Type.Array(Type.String(), { description: "Keyword tags." })),
      memory_type: Type.Optional(MemoryType),
      importance: Type.Optional(
        Type.Integer({ minimum: 1, maximum: 5, description: "1 (trivial) to 5 (critical). 4-5 = project-wide or durable assumption changes." }),
      ),
      source_files: Type.Optional(Type.Array(Type.String(), { description: "Related file paths for traceability." })),
      references: Type.Optional(Type.Array(Type.String(), { description: "Related PR / Issue / external identifiers." })),
      supersedes: Type.Optional(Type.Array(Type.String(), { description: "observation_ids this entry replaces." })),
      visibility: Type.Optional(
        Type.String({ description: 'Replication scope: "user", "team", "mesh", or omit for the server default.' }),
      ),
      expires_at: Type.Optional(
        Type.String({ description: "ISO 8601 instant after which the entry is disposable. Omit for durable memory." }),
      ),
      ttl_sec: Type.Optional(Type.Integer({ description: "Seconds-from-now form of expires_at. Ignored when expires_at is set." })),
    }),
  },
  {
    name: "search_memory",
    label: "kioku-mesh: search",
    description:
      "Search the shared kioku-mesh memory. Returns matching observations with full 32-char ids. " +
      "An empty 'and' search automatically retries as 'or'. If results are unexpectedly empty for work you know happened, " +
      "that is a signal save_observation was skipped — save what is still in context now.",
    parameters: Type.Object({
      query: Type.Optional(Type.String({ description: "Search terms. Empty lists recent entries after filters." })),
      agent_family: Type.Optional(Type.String({ description: "Filter by agent family (e.g. claude, codex, pi)." })),
      client_id: Type.Optional(Type.String()),
      pc_id: Type.Optional(Type.String()),
      session_id: Type.Optional(Type.String()),
      project: Type.Optional(Type.String()),
      since_iso: Type.Optional(Type.String({ description: "Lower created_at bound (ISO 8601)." })),
      limit: Type.Optional(Type.Integer({ minimum: 1, description: "Default 50, server-side clamped." })),
      include_superseded: Type.Optional(Type.Boolean({ description: "Also return superseded observations." })),
      search_mode: Type.Optional(SearchMode),
    }),
  },
  {
    name: "get_memory",
    label: "kioku-mesh: get",
    description:
      "Get full content and metadata for a single observation by its full 32-character id. Use after search_memory.",
    parameters: Type.Object({
      observation_id: Type.String({ description: "Full 32-character observation id." }),
    }),
  },
  {
    name: "recall_context",
    label: "kioku-mesh: recall",
    description:
      "Recall current context from kioku-mesh memory as grouped Markdown, with additive filters for memory_types, " +
      "source_files, and references. Empty query browses recent context. Good first call when starting work on a project.",
    parameters: Type.Object({
      query: Type.Optional(Type.String({ description: "Recall intent. Empty = browse recent context." })),
      project: Type.Optional(Type.String({ description: "Exact project filter." })),
      memory_types: Type.Optional(Type.Array(MemoryType)),
      source_files: Type.Optional(Type.Array(Type.String(), { description: "Exact-match source_files filter." })),
      references: Type.Optional(Type.Array(Type.String(), { description: "Exact-match references filter." })),
      since_iso: Type.Optional(Type.String({ description: "Lower created_at bound (ISO 8601)." })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100, description: "Default 20." })),
      search_mode: Type.Optional(SearchMode),
    }),
  },
];

export default function (pi: ExtensionAPI) {
  const client = new KiokuMcpClient();

  pi.on("session_start", async (_event, ctx) => {
    if (!serverBinaryOnPath()) ctx.ui.notify(INSTALL_HINT, "error");
  });

  pi.on("session_shutdown", async () => {
    client.stop();
  });

  for (const tool of TOOLS) {
    pi.registerTool({
      name: tool.name,
      label: tool.label,
      description: tool.description,
      parameters: tool.parameters,
      async execute(_toolCallId: string, params: Record<string, unknown>) {
        const result = await client.callTool(tool.name, params);
        const text = textOf(result);
        if (result?.isError) throw new Error(text || `kioku-mesh tool ${tool.name} failed`);
        return { content: [{ type: "text" as const, text }], details: undefined };
      },
    });
  }
}
