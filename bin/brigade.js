#!/usr/bin/env node

"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

function executableNames(command) {
  if (process.platform !== "win32") return [command];
  const extensions = (process.env.PATHEXT || ".EXE;.CMD;.BAT;.COM").split(";");
  return [command, ...extensions.map((extension) => command + extension.toLowerCase())];
}

function findCommand(command, excluded) {
  for (const directory of (process.env.PATH || "").split(path.delimiter)) {
    for (const name of executableNames(command)) {
      const candidate = path.resolve(directory || ".", name);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        const isOwnFile = excluded && fs.realpathSync(candidate) === excluded;
        const isOwnShim =
          excluded &&
          /\.(cmd|bat)$/i.test(candidate) &&
          fs.readFileSync(candidate, "utf8").includes(path.basename(excluded));
        if (!isOwnFile && !isOwnShim) return candidate;
      } catch (_) {
        // Keep searching PATH.
      }
    }
  }
  return null;
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(`brigade npm launcher: ${result.error.message}`);
    return 1;
  }
  return result.status === null ? 1 : result.status;
}

const ownPath = fs.realpathSync(process.argv[1]);
const args = process.argv.slice(2);
const brigade = findCommand("brigade", ownPath);

if (brigade) {
  process.exitCode = run(brigade, args);
} else {
  const pipx = findCommand("pipx");
  if (pipx) {
    process.exitCode = run(pipx, ["run", "brigade-cli", ...args]);
  } else {
    console.error("brigade npm launcher: no Brigade CLI or pipx executable was found.");
    console.error("Install it with `pipx install brigade-cli` (recommended), or");
    console.error("`python -m pip install brigade-cli`, then retry this command.");
    process.exitCode = 1;
  }
}
