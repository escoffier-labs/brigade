import { spawnSync } from "node:child_process";

const BRIDGE = ["mcp", "pi-bridge"];

function runBridge(subcommand, target, extraArgs = []) {
  const result = spawnSync(
    "brigade",
    [...BRIDGE, subcommand, "--target", target, "--json", ...extraArgs],
    {
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  if (result.error) {
    throw result.error;
  }
  const stdout = (result.stdout || "").trim();
  if (!stdout) {
    throw new Error(`brigade ${subcommand} returned no output (exit ${result.status})`);
  }
  let payload;
  try {
    payload = JSON.parse(stdout);
  } catch (error) {
    throw new Error(`brigade ${subcommand} returned invalid JSON: ${error.message}`);
  }
  if (result.status !== 0 && !payload.error && !(payload.errors && payload.errors.length)) {
    throw new Error(`brigade ${subcommand} failed with exit ${result.status}`);
  }
  return payload;
}

function schemaForTool(tool) {
  const inputSchema = tool.input_schema && typeof tool.input_schema === "object" ? tool.input_schema : {};
  const properties = inputSchema.properties && typeof inputSchema.properties === "object" ? inputSchema.properties : {};
  const required = Array.isArray(inputSchema.required) ? inputSchema.required : [];
  return {
    type: "object",
    properties,
    required,
    additionalProperties: inputSchema.additionalProperties !== false,
  };
}

function registerDiscoveredTools(pi, target) {
  const payload = runBridge("discover", target);
  const tools = Array.isArray(payload.tools) ? payload.tools : [];
  for (const tool of tools) {
    if (!tool || typeof tool.qualified_name !== "string") {
      continue;
    }
    const qualifiedName = tool.qualified_name;
    const server = tool.server || qualifiedName.split("__")[0] || "unknown";
    const nativeName = tool.name || qualifiedName.split("__").slice(1).join("__") || qualifiedName;
    pi.registerTool({
      name: qualifiedName,
      label: qualifiedName,
      description: tool.description || `MCP tool ${nativeName} from server ${server}`,
      parameters: schemaForTool(tool),
      async execute(_toolCallId, params) {
        const argsJson = JSON.stringify(params || {});
        const call = runBridge("call", target, ["--tool", qualifiedName, "--args-json", argsJson]);
        if (call.error) {
          const message = [
            `MCP tool failed: ${qualifiedName}`,
            `server: ${call.server || server}`,
            `tool: ${call.tool || nativeName}`,
            call.message || "unknown error",
          ].join("\n");
          return {
            content: [{ type: "text", text: message }],
            details: {
              error: true,
              server: call.server || server,
              tool: call.tool || nativeName,
              qualified_name: qualifiedName,
              failure_class: call.failure_class || "tool_failure",
            },
            isError: true,
          };
        }
        const text =
          typeof call.result === "string"
            ? call.result
            : JSON.stringify(call.result ?? {}, null, 2);
        return {
          content: [{ type: "text", text }],
          details: {
            server: call.server || server,
            tool: call.tool || nativeName,
            qualified_name: qualifiedName,
          },
        };
      },
    });
  }
}

export default function brigadeMcpBridge(pi) {
  pi.on("session_start", async (_event, ctx) => {
    const target = ctx.cwd || process.cwd();
    try {
      registerDiscoveredTools(pi, target);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      ctx.ui.notify(`Brigade MCP bridge failed to load tools: ${message}`, "error");
    }
  });
}
