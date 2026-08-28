#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const LOCK_PATH = path.join(ROOT, "vendor", "zod.lock.json");
const VENDOR_CJS = path.join(ROOT, "vendor", "zod-3.25.76.cjs");
const ADAPTER_SRC = path.join(ROOT, "src", "adapter.js");
const MAIN_JS = path.join(ROOT, "main.js");
const SUMS = path.join(ROOT, "SHA256SUMS");

const lock = JSON.parse(fs.readFileSync(LOCK_PATH, "utf8"));

function sha256Hex(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

function sha512Integrity(buf) {
  return "sha512-" + crypto.createHash("sha512").update(buf).digest("base64");
}

function download(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (response) => {
        if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
          download(response.headers.location).then(resolve, reject);
          return;
        }
        if (response.statusCode !== 200) {
          reject(new Error(`download failed: ${response.statusCode}`));
          return;
        }
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => resolve(Buffer.concat(chunks)));
        response.on("error", reject);
      })
      .on("error", reject);
  });
}

function verifyTarball(buf) {
  const integrity = sha512Integrity(buf);
  const digest = sha256Hex(buf);
  if (integrity !== lock.integrity) {
    throw new Error(`zod tarball integrity mismatch: ${integrity}`);
  }
  if (digest !== lock.tarball_sha256) {
    throw new Error(`zod tarball sha256 mismatch: ${digest}`);
  }
}

function extractPackage(tarball) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grokbot-zod-"));
  fs.writeFileSync(path.join(tmp, "zod.tgz"), tarball);
  const extracted = spawnSync("tar", ["-xzf", "zod.tgz"], { cwd: tmp, encoding: "utf8" });
  if (extracted.status !== 0) {
    throw new Error(extracted.stderr || "tar extract failed");
  }
  return path.join(tmp, "package");
}

function resolveId(fromId, spec) {
  if (!spec.startsWith(".")) {
    throw new Error(`non-relative require ${spec} from ${fromId}`);
  }
  const fromDir = path.posix.dirname(fromId);
  let resolved = path.posix.normalize(`${fromDir}/${spec}`);
  if (resolved.startsWith("../") || path.posix.isAbsolute(resolved)) {
    throw new Error(`require escaped package root: ${spec}`);
  }
  if (!resolved.endsWith(".cjs") && !resolved.endsWith(".js")) {
    resolved += ".cjs";
  }
  return resolved;
}

function collectSources(pkgRoot) {
  const sources = new Map();
  const queue = [lock.entry];
  while (queue.length) {
    const id = queue.shift();
    if (sources.has(id)) {
      continue;
    }
    if (!lock.files.includes(id)) {
      throw new Error(`unexpected zod file ${id}`);
    }
    const abs = path.join(pkgRoot, id);
    const text = fs.readFileSync(abs, "utf8");
    sources.set(id, text);
    const requires = text.matchAll(/require\((['"])([^'"]+)\1\)/g);
    for (const match of requires) {
      queue.push(resolveId(id, match[2]));
    }
  }
  const missing = lock.files.filter((id) => !sources.has(id));
  if (missing.length) {
    throw new Error(`lock files not reached: ${missing.join(",")}`);
  }
  return sources;
}

function emitVendorCjs(sources) {
  const factories = [];
  for (const id of lock.files) {
    const body = sources
      .get(id)
      .replace(/^["']use strict["'];\r?\n/, "")
      .replace(/[ \t]+$/gm, "");
    factories.push(`  ${JSON.stringify(id)}: function (exports, module, require) {\n${body}\n  }`);
  }
  return [
    '"use strict";',
    "/*",
    ` * Build-time bundle of ${lock.name}@${lock.version}`,
    ` * resolved: ${lock.resolved}`,
    ` * integrity: ${lock.integrity}`,
    " * MIT License. See vendor/LICENSE and NOTICE.",
    " */",
    "function __grokbotLoadZod() {",
    "  const cache = Object.create(null);",
    "  const factories = {",
    factories.join(",\n"),
    "  };",
    "  function resolveId(fromId, spec) {",
    "    if (!spec.startsWith(\".\")) {",
    "      throw new Error(\"non-relative require \" + spec);",
    "    }",
    '    const parts = fromId.split("/").slice(0, -1).concat(spec.split("/"));',
    "    const out = [];",
    "    for (let i = 0; i < parts.length; i += 1) {",
    '      const part = parts[i];',
    '      if (!part || part === ".") {',
    "        continue;",
    "      }",
    '      if (part === "..") {',
    "        if (!out.length) {",
    '          throw new Error("require escaped package root");',
    "        }",
    "        out.pop();",
    "        continue;",
    "      }",
    "      out.push(part);",
    "    }",
    '    let resolved = out.join("/");',
    '    if (!/\\.(cjs|js)$/.test(resolved)) {',
    '      resolved += ".cjs";',
    "    }",
    "    return resolved;",
    "  }",
    "  function load(id) {",
    "    if (cache[id]) {",
    "      return cache[id].exports;",
    "    }",
    "    const factory = factories[id];",
    "    if (!factory) {",
    '      throw new Error("missing zod module " + id);',
    "    }",
    "    const module = { exports: {} };",
    "    cache[id] = module;",
    "    factory(module.exports, module, function requireRel(spec) {",
    "      return load(resolveId(id, spec));",
    "    });",
    "    return module.exports;",
    "  }",
    `  return load(${JSON.stringify(lock.entry)});`,
    "}",
    "module.exports = __grokbotLoadZod();",
    "",
  ].join("\n");
}

function emitMainJs(vendorSource, adapterSource) {
  const header = [
    '"use strict";',
    "/*",
    " * Generated by scripts/bundle-zod.js. Do not edit main.js by hand.",
    ` * Bundled ${lock.name}@${lock.version}`,
    ` * integrity: ${lock.integrity}`,
    " * MIT License. See vendor/LICENSE and NOTICE.",
    " */",
    "const { z } = (function grokbotZodBundle() {",
    "  const module = { exports: {} };",
    "  const exports = module.exports;",
    vendorSource.replace(/^["']use strict["'];\r?\n/, ""),
    "  return module.exports;",
    "})();",
    "",
  ].join("\n");
  const adapter = adapterSource.replace(/^["']use strict["'];\r?\n/, "");
  return `${header}${adapter}`;
}

function emitSums(mainBuf, manifestBuf) {
  return [`${sha256Hex(mainBuf)}  main.js`, `${sha256Hex(manifestBuf)}  manifest.json`].join("\n") + "\n";
}

function writeSums() {
  fs.writeFileSync(
    SUMS,
    emitSums(fs.readFileSync(MAIN_JS), fs.readFileSync(path.join(ROOT, "manifest.json"))),
  );
}

function checkGenerated() {
  const vendorSource = fs.readFileSync(VENDOR_CJS, "utf8");
  const adapterSource = fs.readFileSync(ADAPTER_SRC, "utf8");
  const expectedMain = emitMainJs(vendorSource, adapterSource);
  const actualMain = fs.readFileSync(MAIN_JS, "utf8");
  if (expectedMain !== actualMain) {
    throw new Error("main.js does not match vendor/zod-3.25.76.cjs + src/adapter.js");
  }
  const expectedSums = emitSums(Buffer.from(expectedMain), fs.readFileSync(path.join(ROOT, "manifest.json")));
  const actualSums = fs.readFileSync(SUMS, "utf8");
  if (expectedSums !== actualSums) {
    throw new Error("SHA256SUMS does not match reconstructed main.js and manifest.json");
  }
}

async function main() {
  const args = new Set(process.argv.slice(2));
  if (args.has("--check")) {
    checkGenerated();
    return;
  }
  let tarball;
  const localTarball = process.env.ZOD_TARBALL;
  if (localTarball) {
    tarball = fs.readFileSync(localTarball);
  } else {
    tarball = await download(lock.resolved);
  }
  verifyTarball(tarball);
  const pkgRoot = extractPackage(tarball);
  try {
    const sources = collectSources(pkgRoot);
    const vendorSource = emitVendorCjs(sources);
    fs.writeFileSync(VENDOR_CJS, vendorSource);
    if (!args.has("--vendor-only")) {
      const adapterSource = fs.readFileSync(ADAPTER_SRC, "utf8");
      fs.writeFileSync(MAIN_JS, emitMainJs(vendorSource, adapterSource));
      writeSums();
    }
  } finally {
    fs.rmSync(path.dirname(pkgRoot), { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
