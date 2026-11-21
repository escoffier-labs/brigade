# @escoffier-labs/brigade

This is the compatibility package for the Brigade CLI. The real package is
[`brigadestack`](https://www.npmjs.com/package/brigadestack). From 0.32.0,
`@escoffier-labs/brigade@X` depends on exactly `brigadestack@X` and declares its
own `brigade` command, so `npm update --global @escoffier-labs/brigade` keeps a
working `brigade`. npm links only the commands of the package you installed,
which is why this package carries a command of its own.

New installs should use `brigadestack`:

```sh
npm install --global brigadestack
brigade --version
```

Install only one of the two packages globally. Both link a `brigade` command, so
npm refuses the second install unless you pass `--force`. To switch, run
`npm uninstall --global @escoffier-labs/brigade`, then
`npm install --global brigadestack`.

Requires Node.js 22 or newer. Keep optional dependencies enabled: `brigadestack` needs its platform runtime
package. The first launch provisions managed Python and may need network access.
Supported runtimes are Linux with glibc (x64 or arm64), macOS (x64 or arm64), and
Windows x64.

The launcher resolves its `brigadestack` dependency independently of your working
directory and `PATH`, then runs it with the current Node executable. Arguments,
standard streams, environment, working directory, and CLI exit status pass
through. On POSIX with Node.js 22.15 or newer in the 22.x line, or 23.11 or
newer, `process.execve` replaces this launcher with the dependency, preserving
its PID, process group, controlling terminal, and shell job control. On older
Node.js 22 and on Windows the launcher runs the dependency as an attached child
that inherits the console and forwards a direct termination signal.

Commands, Python imports, workspace files, and storage formats keep their existing
identities. A previous npm install keeps its provisioned runtime under
`~/.cache/brigade-npm/payloads`. The new package provisions its own runtime beside
it and leaves the old one untouched.

[Homepage](https://brigade.tools) |
[Documentation](https://brigade.tools/docs) |
[Source](https://github.com/escoffier-labs/brigade/tree/main/distribution/escoffier-labs-brigade/npm) |
[Issues](https://github.com/escoffier-labs/brigade/issues)
