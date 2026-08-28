"use strict";

const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PLUGIN_DIR = path.resolve(__dirname, "..", "obsidian-plugin", "grokbot-operator-adapter");
const PLUGIN_PATH = path.join(PLUGIN_DIR, "main.js");
const VENDOR_ZOD_PATH = path.join(PLUGIN_DIR, "vendor", "zod-3.25.76.cjs");
const CAS_TOOLS = [
  "grokbot_replace_canvas_v1",
  "grokbot_replace_base_v1",
  "grokbot_replace_excalidraw_v1",
];
const EXPECTED_TOOLS = [
  ...CAS_TOOLS,
  "grokbot_lint_note_v1",
  "grokbot_auto_move_note_v1",
  "grokbot_sr_open_review_v1",
  "grokbot_homepage_open_v1",
  "grokbot_omnisearch_v1",
  "grokbot_excalidraw_open_v1",
  "grokbot_excalidraw_export_v1",
];
const LIVE_WORKFLOW_TOOLS = [
  "grokbot_lint_note_v1",
  "grokbot_sr_open_review_v1",
  "grokbot_omnisearch_v1",
  "grokbot_excalidraw_open_v1",
];
const INPUT_KEYS = ["expected_sha256", "path", "replacement_utf8"];
const WORKFLOW_INPUT_KEYS = {
  grokbot_lint_note_v1: ["expected_sha256", "path"],
  grokbot_auto_move_note_v1: ["expected_sha256", "path"],
  grokbot_sr_open_review_v1: [],
  grokbot_homepage_open_v1: [],
  grokbot_omnisearch_v1: ["limit", "query"],
  grokbot_excalidraw_open_v1: ["path"],
  grokbot_excalidraw_export_v1: ["format", "path"],
};

function sha256(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function loadHostZod() {
  const loaded = require(VENDOR_ZOD_PATH);
  const z = loaded && loaded.z ? loaded.z : loaded;
  if (!z || typeof z.object !== "function") {
    throw new Error("vendor zod 3.25.76 must export z.object");
  }
  return z;
}

function zodFieldToJsonSchema(field) {
  let current = field;
  while (current && current._def && current._def.innerType) {
    current = current._def.innerType;
  }
  const typeName = current && current._def ? current._def.typeName : "";
  const out = {};
  if (typeName === "ZodString") {
    out.type = "string";
  } else if (typeName === "ZodNumber") {
    out.type = "number";
  } else if (typeName === "ZodBoolean") {
    out.type = "boolean";
  } else if (typeName === "ZodEnum") {
    out.type = "string";
    out.enum = current._def.values.slice().sort();
  } else {
    throw new Error(`unsupported Zod field ${typeName}`);
  }
  for (const check of current._def.checks || []) {
    if (check.kind === "regex" && check.regex) {
      out.pattern = check.regex.source;
    }
    if (check.kind === "min") {
      out.minLength = check.value;
    }
    if (check.kind === "max") {
      out.maxLength = check.value;
    }
  }
  return out;
}

function jsonSchemaFromObject(schema) {
  const shape = typeof schema.shape === "function" ? schema.shape() : schema.shape;
  const properties = {};
  const required = [];
  for (const key of Object.keys(shape).sort()) {
    properties[key] = zodFieldToJsonSchema(shape[key]);
    required.push(key);
  }
  return {
    type: "object",
    properties,
    required,
  };
}

function assertHostZodShape(schema, z, inputKeys) {
  if (schema == null || typeof schema !== "object" || Array.isArray(schema) || typeof schema.parse === "function") {
    throw new Error("host contract requires a Zod shape, not a ZodObject");
  }
  const wrapped = z.object(schema);
  const json = jsonSchemaFromObject(wrapped);
  assert.deepStrictEqual(Object.keys(json.properties), inputKeys);
  assert.deepStrictEqual(json.required, inputKeys);
  assert.strictEqual(json.type, "object");
  if (inputKeys.includes("path")) {
    assert.strictEqual(json.properties.path.type, "string");
  }
  if (inputKeys.includes("expected_sha256")) {
    assert.strictEqual(json.properties.expected_sha256.type, "string");
    assert.strictEqual(json.properties.expected_sha256.pattern, "^[0-9a-f]{64}$");
  }
  if (inputKeys.includes("replacement_utf8")) {
    assert.strictEqual(json.properties.replacement_utf8.type, "string");
    assert.strictEqual(json.properties.replacement_utf8.minLength, 1);
  }
  return wrapped;
}

function loadPluginClass() {
  const source = fs.readFileSync(PLUGIN_PATH, "utf8");
  const sandbox = {
    module: { exports: {} },
    exports: {},
    require(name) {
      if (name === "obsidian") {
        class Plugin {
          constructor(app, manifest) {
            this.app = app;
            this.manifest = manifest;
          }
          async onload() {}
          onunload() {}
        }
        return { Plugin };
      }
      if (name === "crypto") {
        return crypto;
      }
      throw new Error(`unexpected require: ${name}`);
    },
    console,
    Buffer,
    setTimeout,
    clearTimeout,
  };
  sandbox.module.exports = sandbox.exports;
  vm.runInNewContext(source, sandbox, { filename: PLUGIN_PATH, displayErrors: true });
  const exported = sandbox.module.exports;
  if (typeof exported !== "function") {
    throw new Error("plugin must export a CommonJS class");
  }
  return exported;
}

function makeWorkspace() {
  const listeners = new Map();
  let activeFile = null;
  let activeView = null;
  return {
    on(name, handler) {
      const list = listeners.get(name) || [];
      list.push(handler);
      listeners.set(name, list);
    },
    off(name, handler) {
      const list = listeners.get(name) || [];
      listeners.set(
        name,
        list.filter((item) => item !== handler),
      );
    },
    trigger(name) {
      for (const handler of listeners.get(name) || []) {
        handler();
      }
    },
    listenerCount(name) {
      return (listeners.get(name) || []).length;
    },
    getLeaf() {
      return {
        async openFile(file) {
          activeFile = file;
        },
      };
    },
    getActiveFile() {
      return activeFile;
    },
    getActiveView() {
      return activeView;
    },
    setActiveFile(file) {
      activeFile = file;
    },
    setActiveView(view) {
      activeView = view;
    },
  };
}

function makeVault(initial) {
  const files = new Map(Object.entries(initial || {}));
  const calls = [];
  return {
    files,
    calls,
    getAbstractFileByPath(filePath) {
      calls.push(["getAbstractFileByPath", filePath]);
      if (!files.has(filePath)) {
        return null;
      }
      return { path: filePath };
    },
    async process(file, callback) {
      calls.push(["process", file.path]);
      if (!files.has(file.path)) {
        throw new Error("missing");
      }
      const current = files.get(file.path);
      const next = callback(current);
      files.set(file.path, next);
      return next;
    },
    async read(file) {
      calls.push(["read", file.path]);
      if (!files.has(file.path)) {
        throw new Error("missing");
      }
      return files.get(file.path);
    },
    getMarkdownFiles() {
      return Array.from(files.keys())
        .filter((filePath) => filePath.endsWith(".md"))
        .map((filePath) => ({ path: filePath }));
    },
    async modify() {
      calls.push(["modify"]);
      throw new Error("vault.modify is forbidden");
    },
    async create() {
      calls.push(["create"]);
      throw new Error("vault.create is forbidden");
    },
  };
}

function makeHost({ apiVersion = 2, throwOnGet = false } = {}) {
  const tools = [];
  let unregisterCount = 0;
  let getPublicApiCount = 0;
  const api = {
    apiVersion,
    addMcpTool(name, description, schema, callback) {
      if (schema == null || typeof schema !== "object" || Array.isArray(schema) || typeof schema.parse === "function") {
        throw new Error("zod schema required");
      }
      const keys = Object.keys(schema).sort();
      for (const key of keys) {
        const field = schema[key];
        if (!field || typeof field !== "object" || !field._def || typeof field.parse !== "function") {
          throw new Error("zod schema required");
        }
      }
      if (tools.some((tool) => tool.name === name)) {
        throw new Error("duplicate tool");
      }
      tools.push({ name, description, keys, callback, schema });
    },
    unregister() {
      unregisterCount += 1;
      tools.length = 0;
    },
  };
  return {
    tools,
    get unregisterCount() {
      return unregisterCount;
    },
    get getPublicApiCount() {
      return getPublicApiCount;
    },
    getPublicApi(manifest) {
      getPublicApiCount += 1;
      if (throwOnGet) {
        throw new Error("getPublicApi failed");
      }
      if (!manifest || !manifest.id) {
        throw new Error("manifest required");
      }
      return api;
    },
  };
}

function makeApp({ host = null, vault = null, commands = null, plugins = null, manifests = null } = {}) {
  return {
    plugins: { plugins: { ...(host ? { "obsidian-local-rest-api": host } : {}), ...(plugins || {}) }, manifests: manifests || {} },
    workspace: makeWorkspace(),
    vault: vault || makeVault(),
    commands: commands || null,
  };
}

function fixedCommands(ids) {
  return { commands: Object.fromEntries(ids.map((id) => [id, {}])), async executeCommandById() { return true; } };
}

async function loadPlugin(app, PluginClass) {
  const plugin = new PluginClass(app, {
    id: "grokbot-operator-adapter",
    version: "0.1.0",
  });
  await plugin.onload();
  return plugin;
}

async function run() {
  const PluginClass = loadPluginClass();
  const z = loadHostZod();

  {
    const host = makeHost({ apiVersion: 2 });
    const vault = makeVault({
      "01 - Projects/Board.canvas": '{"nodes":[],"edges":[]}\n',
    });
    const plugin = await loadPlugin(makeApp({ host, vault }), PluginClass);
    assert.strictEqual(host.getPublicApiCount, 1);
    assert.strictEqual(host.tools.length, CAS_TOOLS.length);
    assert.strictEqual(plugin._registered, true);
    assert.strictEqual(plugin._host, host);
    assert.deepStrictEqual(
      host.tools.map((tool) => tool.name),
      CAS_TOOLS,
    );
    for (const tool of host.tools) {
      assertHostZodShape(
        tool.schema,
        z,
        INPUT_KEYS,
      );
    }
    plugin.onunload();
    assert.strictEqual(host.unregisterCount, 1);
    assert.strictEqual(plugin._host, null);
  }

  {
    const host = makeHost({ apiVersion: 1 });
    const plugin = await loadPlugin(makeApp({ host }), PluginClass);
    assert.strictEqual(host.tools.length, 0);
    plugin.onunload();
    assert.strictEqual(host.unregisterCount, 0);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const commands = fixedCommands([
      "obsidian-linter:lint-file",
      "obsidian-spaced-repetition:srs-open-review-queue-view",
      "obsidian-excalidraw-plugin:excalidraw-open",
    ]);
    const plugin = await loadPlugin(
      makeApp({
        host,
        commands,
        plugins: { omnisearch: { api: { async search() { return []; } } } },
        manifests: { omnisearch: { version: "1.30.1" } },
      }),
      PluginClass,
    );
    assert.deepStrictEqual(host.tools.map((tool) => tool.name), [...CAS_TOOLS, ...LIVE_WORKFLOW_TOOLS]);
    for (const deferred of ["grokbot_auto_move_note_v1", "grokbot_homepage_open_v1", "grokbot_excalidraw_export_v1"]) {
      assert.ok(!host.tools.some((tool) => tool.name === deferred));
    }
    plugin.onunload();
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const plugin = await loadPlugin(
      makeApp({
        host,
        commands: fixedCommands([
          "obsidian-linter:lint-file",
          "obsidian-spaced-repetition:srs-open-review-queue-view",
          "obsidian-excalidraw-plugin:excalidraw-open",
        ]),
        plugins: { omnisearch: { api: { async search() { return []; } } } },
        manifests: { omnisearch: { version: "1.30.0" } },
      }),
      PluginClass,
    );
    assert.deepStrictEqual(host.tools.map((tool) => tool.name), [
      ...CAS_TOOLS,
      "grokbot_lint_note_v1",
      "grokbot_sr_open_review_v1",
      "grokbot_excalidraw_open_v1",
    ]);
    assert.ok(!host.tools.some((tool) => tool.name === "grokbot_omnisearch_v1"));
    for (const deferred of ["grokbot_auto_move_note_v1", "grokbot_homepage_open_v1", "grokbot_excalidraw_export_v1"]) {
      assert.ok(!host.tools.some((tool) => tool.name === deferred));
    }
    plugin.onunload();
  }

  {
    const app = makeApp();
    const plugin = await loadPlugin(app, PluginClass);
    assert.strictEqual(app.workspace.listenerCount("obsidian-local-rest-api:loaded"), 1);
    const host = makeHost({ apiVersion: 2 });
    app.plugins.plugins["obsidian-local-rest-api"] = host;
    app.workspace.trigger("obsidian-local-rest-api:loaded");
    app.workspace.trigger("obsidian-local-rest-api:loaded");
    assert.strictEqual(host.getPublicApiCount, 1);
    assert.strictEqual(host.tools.length, CAS_TOOLS.length);
    plugin.onunload();
    assert.strictEqual(host.unregisterCount, 1);
    assert.strictEqual(app.workspace.listenerCount("obsidian-local-rest-api:loaded"), 0);
  }

  {
    const first = makeHost({ apiVersion: 2 });
    const app = makeApp({ host: first });
    const plugin = await loadPlugin(app, PluginClass);
    assert.strictEqual(first.getPublicApiCount, 1);
    assert.strictEqual(first.tools.length, CAS_TOOLS.length);
    const second = makeHost({ apiVersion: 2 });
    app.plugins.plugins["obsidian-local-rest-api"] = second;
    app.workspace.trigger("obsidian-local-rest-api:loaded");
    assert.strictEqual(first.unregisterCount, 1);
    assert.strictEqual(first.tools.length, 0);
    assert.strictEqual(second.getPublicApiCount, 1);
    assert.strictEqual(second.tools.length, CAS_TOOLS.length);
    assert.strictEqual(plugin._host, second);
    app.workspace.trigger("obsidian-local-rest-api:loaded");
    assert.strictEqual(second.getPublicApiCount, 1);
    plugin.onunload();
    assert.strictEqual(second.unregisterCount, 1);
    assert.strictEqual(plugin._api, null);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const current = '{"nodes":[{"id":"a","type":"text","x":0,"y":0,"width":1,"height":1,"text":"A"}],"edges":[]}\n';
    const next = '{"nodes":[{"id":"b","type":"text","x":0,"y":0,"width":1,"height":1,"text":"B"}],"edges":[]}\n';
    const vault = makeVault({ "01 - Projects/Board.canvas": current });
    const plugin = await loadPlugin(makeApp({ host, vault }), PluginClass);
    vault.files.set("01 - Projects/Board.canvas", next);
    await assert.rejects(
      () =>
        plugin._replace("canvas", {
          path: "01 - Projects/Board.canvas",
          expected_sha256: sha256(current),
          replacement_utf8: '{"nodes":[],"edges":[]}\n',
        }),
    );
    assert.strictEqual(vault.files.get("01 - Projects/Board.canvas"), next);
    assert.ok(!vault.calls.some((call) => call[0] === "modify"));
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const current = '{"nodes":[],"edges":[]}\n';
    const replacement = '{"nodes":[{"id":"n","type":"text","x":0,"y":0,"width":10,"height":10,"text":"ok"}],"edges":[]}\n';
    const vault = makeVault({ "01 - Projects/Board.canvas": current });
    const plugin = await loadPlugin(makeApp({ host, vault }), PluginClass);
    const result = await plugin._replace("canvas", {
      path: "01 - Projects/Board.canvas",
      expected_sha256: sha256(current),
      replacement_utf8: replacement,
    });
    const body = JSON.parse(result.content[0].text);
    assert.deepStrictEqual(Object.keys(body).sort(), ["previous_sha256", "resulting_sha256"]);
    assert.strictEqual(body.previous_sha256, sha256(current));
    assert.strictEqual(body.resulting_sha256, sha256(replacement));
    assert.strictEqual(vault.files.get("01 - Projects/Board.canvas"), replacement);
    await assert.rejects(
      () =>
        plugin._replace("canvas", {
          path: "01 - Projects/Board.canvas",
          expected_sha256: sha256(replacement),
          replacement_utf8: replacement,
          extra: true,
        }),
    );
    await assert.rejects(
      () =>
        plugin._replace("canvas", {
          path: "01 - Projects/Board.canvas",
          expected_sha256: "not-a-hash",
          replacement_utf8: replacement,
        }),
    );
    assert.strictEqual(vault.files.get("01 - Projects/Board.canvas"), replacement);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const current = '{"filters":{}}\n';
    const vault = makeVault({ "01 - Projects/Dashboard.base": current });
    const originalProcess = vault.process.bind(vault);
    vault.process = async function failProcess(file, callback) {
      callback(this.files.get(file.path));
      throw new Error("callback-failure");
    };
    const plugin = await loadPlugin(makeApp({ host, vault }), PluginClass);
    await assert.rejects(
      () =>
        plugin._replace("base", {
          path: "01 - Projects/Dashboard.base",
          expected_sha256: sha256(current),
          replacement_utf8: '{"views":[]}\n',
        }),
    );
    assert.strictEqual(vault.files.get("01 - Projects/Dashboard.base"), current);
    vault.process = originalProcess;
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const current =
      "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n\n# Excalidraw Data\n\n## Text Elements\n## Drawing\n```json\n" +
      '{"elements":[{"id":"a"}],"appState":{},"version":2}\n```\n%%\n';
    const replacement =
      "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n\n# Excalidraw Data\n\n## Text Elements\n## Drawing\n```json\n" +
      '{"elements":[{"id":"b"}],"appState":{},"version":2}\n```\n%%\n';
    const vault = makeVault({ "03 - Resources/Excalidraw/scene.excalidraw.md": current });
    const plugin = await loadPlugin(makeApp({ host, vault }), PluginClass);
    const result = await plugin._replace("excalidraw", {
      path: "03 - Resources/Excalidraw/scene.excalidraw.md",
      expected_sha256: sha256(current),
      replacement_utf8: replacement,
    });
    const body = JSON.parse(result.content[0].text);
    assert.strictEqual(body.resulting_sha256, sha256(replacement));
    assert.strictEqual(vault.files.get("03 - Resources/Excalidraw/scene.excalidraw.md"), replacement);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const original = "# Note\n";
    const linted = "# Note\n\nLinted.\n";
    const vault = makeVault({ "01 - Projects/note.md": original });
    const commandCalls = [];
    const app = makeApp({
      host,
      vault,
      commands: {
        async executeCommandById(command) {
          commandCalls.push(command);
          assert.strictEqual(command, "obsidian-linter:lint-file");
          vault.files.set("01 - Projects/note.md", linted);
          return true;
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    const result = await plugin._lintNote({ path: "01 - Projects/note.md", expected_sha256: sha256(original) });
    assert.deepStrictEqual(JSON.parse(result.content[0].text), {
      path: "01 - Projects/note.md",
      before_sha256: sha256(original),
      after_sha256: sha256(linted),
    });
    assert.deepStrictEqual(commandCalls, ["obsidian-linter:lint-file"]);
    await assert.rejects(() => plugin._lintNote({ path: "01 - Projects/note.md", expected_sha256: sha256(linted), command: "anything" }));
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const original = "# Note\n";
    const vault = makeVault({ "01 - Projects/note.md": "# Changed\n" });
    let commandCalls = 0;
    const plugin = await loadPlugin(
      makeApp({ host, vault, commands: { async executeCommandById() { commandCalls += 1; return true; } } }),
      PluginClass,
    );
    await assert.rejects(() => plugin._lintNote({ path: "01 - Projects/note.md", expected_sha256: sha256(original) }));
    assert.strictEqual(commandCalls, 0);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const original = "# Note\n";
    const vault = makeVault({ "01 - Projects/note.md": original, "01 - Projects/other.md": "# Other\n" });
    let app;
    app = makeApp({
      host,
      vault,
      commands: {
        async executeCommandById(command) {
          assert.strictEqual(command, "obsidian-linter:lint-file");
          app.workspace.setActiveFile({ path: "01 - Projects/other.md" });
          return true;
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    await assert.rejects(() => plugin._lintNote({ path: "01 - Projects/note.md", expected_sha256: sha256(original) }));
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const original = "# Note\n";
    const vault = makeVault({ "01 - Projects/note.md": original });
    let release;
    const started = [];
    const app = makeApp({
      host,
      vault,
      commands: {
        async executeCommandById(command) {
          started.push(command);
          if (started.length === 1) await new Promise((resolve) => { release = resolve; });
          return true;
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    const args = { path: "01 - Projects/note.md", expected_sha256: sha256(original) };
    const first = plugin._lintNote(args);
    for (let index = 0; index < 6 && started.length === 0; index += 1) await Promise.resolve();
    const second = plugin._lintNote(args);
    for (let index = 0; index < 6; index += 1) await Promise.resolve();
    assert.deepStrictEqual(started, ["obsidian-linter:lint-file"]);
    release();
    await Promise.all([first, second]);
    assert.deepStrictEqual(started, ["obsidian-linter:lint-file", "obsidian-linter:lint-file"]);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const drawing = "03 - Resources/Excalidraw/scene.excalidraw";
    const vault = makeVault({ [drawing]: '{"elements":[{"id":"a"}],"appState":{}}\n' });
    let app;
    app = makeApp({
      host,
      vault,
      commands: {
        async executeCommandById(command) {
          if (command === "obsidian-spaced-repetition:srs-open-review-queue-view") {
            app.workspace.setActiveView({ getViewType() { return "review-queue"; } });
            return true;
          }
          assert.strictEqual(command, "obsidian-excalidraw-plugin:excalidraw-open");
          return true;
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    assert.deepStrictEqual(JSON.parse((await plugin._openSpacedReview({})).content[0].text), { view_id: "review-queue", opened: true });
    assert.deepStrictEqual(JSON.parse((await plugin._openExcalidraw({ path: drawing })).content[0].text), { path: drawing, view_type: "excalidraw" });
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const note = "01 - Projects/note.md";
    const drawing = "03 - Resources/Excalidraw/scene.excalidraw";
    const vault = makeVault({ [note]: "# Note\n", [drawing]: '{"elements":[{"id":"a"}],"appState":{}}\n' });
    const started = [];
    let release;
    const app = makeApp({
      host,
      vault,
      commands: {
        async executeCommandById(command) {
          started.push(command);
          if (command === "obsidian-linter:lint-file") await new Promise((resolve) => { release = resolve; });
          return true;
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    const lint = plugin._lintNote({ path: note, expected_sha256: sha256("# Note\n") });
    for (let index = 0; index < 6 && started.length === 0; index += 1) await Promise.resolve();
    const open = plugin._openExcalidraw({ path: drawing });
    for (let index = 0; index < 6; index += 1) await Promise.resolve();
    assert.deepStrictEqual(started, ["obsidian-linter:lint-file"]);
    release();
    await Promise.all([lint, open]);
    assert.deepStrictEqual(started, ["obsidian-linter:lint-file", "obsidian-excalidraw-plugin:excalidraw-open"]);
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const app = makeApp({ host, manifests: { omnisearch: { version: "1.30.0" } }, plugins: { omnisearch: { api: { async search() { return []; } } } } });
    const plugin = await loadPlugin(app, PluginClass);
    await assert.rejects(() => plugin._omnisearch({ query: "needle", limit: 1 }));
    await assert.rejects(() => plugin._omnisearch({ query: "x".repeat(513), limit: 1 }));
    await assert.rejects(() => plugin._omnisearch({ query: "needle", limit: 11 }));
  }

  {
    const host = makeHost({ apiVersion: 2 });
    const app = makeApp({
      host,
      manifests: { omnisearch: { version: "1.30.1" } },
      plugins: {
        omnisearch: {
          api: {
            async search() {
              return [{ path: "01 - Projects/note.md", title: "Note", snippet: "x".repeat(65536), score: 1 }];
            },
          },
        },
      },
    });
    const plugin = await loadPlugin(app, PluginClass);
    await assert.rejects(() => plugin._omnisearch({ query: "needle", limit: 1 }));
  }

  {
    const source = fs.readFileSync(PLUGIN_PATH, "utf8");
    assert.doesNotMatch(source, /vault_write|command_execute|_vaultPut|fetch\(/);
    assert.doesNotMatch(source, /addMcpTool\(\s*[A-Za-z_]+/);
    assert.doesNotMatch(source, /function inputSchema/);
    assert.doesNotMatch(source, /parse\(value\)/);
    assert.match(source, /zod@3\.25\.76/);
    assert.match(
      source,
      /sha512-gzUt\/qt81nXsFGKIFcC3YnfEAx5NkunCfnDlvuBSSFS02bcXu4Lmea0AFIUwbLWxWPx3d9p8S5QoaujKcNQxcQ==/,
    );
    assert.match(source, /Private canvas compare-and-swap v0\.1\.0/);
    assert.match(source, /Private base compare-and-swap v0\.1\.0/);
    assert.match(source, /Private excalidraw compare-and-swap v0\.1\.0/);
    assert.ok(EXPECTED_TOOLS.every((name) => source.includes(name)));
    assert.deepStrictEqual(INPUT_KEYS, ["expected_sha256", "path", "replacement_utf8"]);
    assert.doesNotMatch(source, /run_plugin_command|command_execute|open_file|vault_write|_vaultPut/);
    assert.doesNotMatch(source, /destination:\s*args|command:\s*args|tool:\s*args/);
    assert.match(source, /obsidian-linter:lint-file/);
    assert.match(source, /obsidian-spaced-repetition:srs-open-review-queue-view/);
    assert.match(source, /obsidian-excalidraw-plugin:excalidraw-open/);
    assert.doesNotMatch(source, /auto-note-mover:Move-the-note|homepage:open-homepage/);
    assert.doesNotMatch(source, /obsidian-excalidraw-plugin:export-image/);
  }

  console.log("obsidian operator plugin harness ok");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
