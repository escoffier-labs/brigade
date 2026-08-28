"use strict";

const crypto = require("crypto");
const { Plugin } = require("obsidian");

const HOST_ID = "obsidian-local-rest-api";
const LOADED_EVENT = "obsidian-local-rest-api:loaded";
const MIN_API_VERSION = 2;
const ADAPTER_VERSION = "0.1.0";
const MAX_REPLACEMENT_BYTES = 262144;
const TOOL_DESCRIPTIONS = {
  grokbot_replace_canvas_v1: "Private canvas compare-and-swap v0.1.0",
  grokbot_replace_base_v1: "Private base compare-and-swap v0.1.0",
  grokbot_replace_excalidraw_v1: "Private excalidraw compare-and-swap v0.1.0",
};
const HEX64 = /^[0-9a-f]{64}$/;
const CANVAS_ROOTS = [
  "00 - Inbox/Agent Notes",
  "01 - Projects",
  "02 - Areas/07 - Agent Work Log",
  "03 - Resources",
];
const PROJECTS_ROOT = "01 - Projects";
const DASHBOARD_BASE = "01 - Projects/Dashboard.base";
const EXCALIDRAW_ROOT = "03 - Resources/Excalidraw";
const ENVELOPE_PREFIX =
  "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n\n# Excalidraw Data\n\n## Text Elements\n## Drawing\n```json\n";
const ENVELOPE_SUFFIX = "```\n%%\n";

function sha256Utf8(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function byteLength(text) {
  return Buffer.byteLength(text, "utf8");
}

function normalizeVaultPath(value) {
  if (typeof value !== "string" || !value || value.length > 512) {
    throw new Error("denied");
  }
  if (value.indexOf("\0") !== -1 || value.indexOf("\\") !== -1 || value.indexOf("\r") !== -1) {
    throw new Error("denied");
  }
  if (value.charAt(0) === "/" || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)) {
    throw new Error("denied");
  }
  const segments = value.split("/");
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    if (!segment || segment === "." || segment === ".." || segment.charAt(0) === "." || segment.charAt(0) === "-") {
      throw new Error("denied");
    }
  }
  return segments.join("/");
}

function underRoot(pathValue, root) {
  return pathValue === root || pathValue.indexOf(root + "/") === 0;
}

function authorizeCanvas(pathValue) {
  if (pathValue.slice(-7) !== ".canvas") {
    throw new Error("denied");
  }
  for (let index = 0; index < CANVAS_ROOTS.length; index += 1) {
    const root = CANVAS_ROOTS[index];
    if (pathValue.indexOf(root + "/") === 0 && pathValue.length > root.length + 1) {
      return pathValue;
    }
  }
  throw new Error("denied");
}

function authorizeBase(pathValue) {
  if (pathValue === DASHBOARD_BASE) {
    return pathValue;
  }
  if (pathValue.slice(-5) === ".base" && pathValue.indexOf(PROJECTS_ROOT + "/") === 0) {
    return pathValue;
  }
  throw new Error("denied");
}

function authorizeExcalidraw(pathValue) {
  if (!underRoot(pathValue, EXCALIDRAW_ROOT) || pathValue === EXCALIDRAW_ROOT) {
    throw new Error("denied");
  }
  if (pathValue.slice(-14) === ".excalidraw.md" || (pathValue.slice(-11) === ".excalidraw" && pathValue.slice(-14) !== ".excalidraw.md")) {
    return pathValue;
  }
  throw new Error("denied");
}

function parseJsonObject(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_error) {
    throw new Error("invalid_request");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("invalid_request");
  }
  return parsed;
}

function validateCanvas(text) {
  const parsed = parseJsonObject(text);
  if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges)) {
    throw new Error("invalid_request");
  }
}

function validateBase(text) {
  parseJsonObject(text);
}

function validateExcalidraw(text, pathValue) {
  if (pathValue.slice(-14) === ".excalidraw.md") {
    if (text.indexOf(ENVELOPE_PREFIX) !== 0 || text.slice(-ENVELOPE_SUFFIX.length) !== ENVELOPE_SUFFIX) {
      throw new Error("invalid_request");
    }
    const inner = text.slice(ENVELOPE_PREFIX.length, text.length - ENVELOPE_SUFFIX.length);
    const parsed = parseJsonObject(inner);
    if (!Array.isArray(parsed.elements) || parsed.elements.length < 1 || typeof parsed.appState !== "object") {
      throw new Error("invalid_request");
    }
    return;
  }
  const parsed = parseJsonObject(text);
  if (!Array.isArray(parsed.elements) || parsed.elements.length < 1 || typeof parsed.appState !== "object") {
    throw new Error("invalid_request");
  }
}

function publicSchema() {
  return {
    path: z.string(),
    expected_sha256: z.string().regex(HEX64),
    replacement_utf8: z.string().min(1),
  };
}

function parseInput(raw) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("invalid_request");
  }
  const keys = Object.keys(raw).sort();
  if (keys.length !== 3 || keys[0] !== "expected_sha256" || keys[1] !== "path" || keys[2] !== "replacement_utf8") {
    throw new Error("invalid_request");
  }
  const expected = raw.expected_sha256;
  const pathValue = raw.path;
  const replacement = raw.replacement_utf8;
  if (typeof expected !== "string" || !HEX64.test(expected)) {
    throw new Error("invalid_request");
  }
  if (typeof replacement !== "string" || byteLength(replacement) < 1 || byteLength(replacement) > MAX_REPLACEMENT_BYTES) {
    throw new Error("invalid_request");
  }
  return {
    expected_sha256: expected,
    path: pathValue,
    replacement_utf8: replacement,
  };
}

class GrokbotOperatorAdapter extends Plugin {
  constructor(app, manifest) {
    super(app, manifest);
    this._api = null;
    this._host = null;
    this._registered = false;
    this._onLoaded = null;
  }

  async onload() {
    const self = this;
    this._onLoaded = function onLoaded() {
      self._tryRegister();
    };
    this.app.workspace.on(LOADED_EVENT, this._onLoaded);
    this._tryRegister();
  }

  onunload() {
    if (this._onLoaded) {
      this.app.workspace.off(LOADED_EVENT, this._onLoaded);
      this._onLoaded = null;
    }
    this._clearRegistration();
  }

  _hostIsLive(host) {
    return !!(host && typeof host.getPublicApi === "function");
  }

  _clearRegistration() {
    if (this._api) {
      try {
        this._api.unregister();
      } catch (_error) {
        // dead handle
      }
    }
    this._api = null;
    this._host = null;
    this._registered = false;
  }

  _tryRegister() {
    const host = this.app.plugins.plugins[HOST_ID];
    if (this._host && (this._host !== host || !this._hostIsLive(this._host))) {
      this._clearRegistration();
    }
    if (this._host === host && this._api) {
      return;
    }
    if (!this._hostIsLive(host)) {
      return;
    }
    let api;
    try {
      api = host.getPublicApi(this.manifest);
    } catch (_error) {
      return;
    }
    const version = api && typeof api.apiVersion === "number" ? api.apiVersion : 0;
    if (!api || version < MIN_API_VERSION) {
      return;
    }
    if (typeof api.addMcpTool !== "function" || typeof api.unregister !== "function") {
      return;
    }
    const self = this;
    try {
      const schema = publicSchema();
      api.addMcpTool(
        "grokbot_replace_canvas_v1",
        TOOL_DESCRIPTIONS.grokbot_replace_canvas_v1,
        schema,
        function replaceCanvas(args) {
          return self._replace("canvas", args);
        },
      );
      api.addMcpTool(
        "grokbot_replace_base_v1",
        TOOL_DESCRIPTIONS.grokbot_replace_base_v1,
        schema,
        function replaceBase(args) {
          return self._replace("base", args);
        },
      );
      api.addMcpTool(
        "grokbot_replace_excalidraw_v1",
        TOOL_DESCRIPTIONS.grokbot_replace_excalidraw_v1,
        schema,
        function replaceExcalidraw(args) {
          return self._replace("excalidraw", args);
        },
      );
    } catch (_error) {
      try {
        api.unregister();
      } catch (_cleanup) {
        // fail closed
      }
      return;
    }
    this._host = host;
    this._api = api;
    this._registered = true;
  }

  async _replace(kind, raw) {
    const input = parseInput(raw);
    const pathValue = normalizeVaultPath(input.path);
    if (kind === "canvas") {
      authorizeCanvas(pathValue);
      validateCanvas(input.replacement_utf8);
    } else if (kind === "base") {
      authorizeBase(pathValue);
      validateBase(input.replacement_utf8);
    } else {
      authorizeExcalidraw(pathValue);
      validateExcalidraw(input.replacement_utf8, pathValue);
    }
    const file = this.app.vault.getAbstractFileByPath(pathValue);
    if (!file) {
      throw new Error("not_found");
    }
    let previous = "";
    let resulting = "";
    await this.app.vault.process(file, function swap(current) {
      previous = sha256Utf8(current);
      if (previous !== input.expected_sha256) {
        throw new Error("stale");
      }
      resulting = sha256Utf8(input.replacement_utf8);
      return input.replacement_utf8;
    });
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify({ previous_sha256: previous, resulting_sha256: resulting }),
        },
      ],
    };
  }
}

module.exports = GrokbotOperatorAdapter;
