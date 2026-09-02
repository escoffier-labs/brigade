# Content-guard allow markers

<!-- content-guard: allow private-ipv4 file -->

Inline `content-guard: allow` comments tell the embedded scanner that a match
is intentional. There are two scopes.

## Line-scoped

`content-guard: allow <rule-id>` (or `all`) applies to the marker's line and
the following line. Typical forms:

```markdown
<!-- content-guard: allow localhost-port -->
A local service might run on localhost:8080.
```

```python
# content-guard: allow private-ipv4
gateway = "192.168.1.10"
```

A copied display id (`category/rule-id`) is accepted as well as the bare rule
id.

## File-scoped

`content-guard: allow <rule-id> file` (or `allow all file`) applies to the
entire file, but only when the marker is in a **comment or directive
position**:

- line-leading (optional indent, then the marker)
- after a comment prefix (`#`, `//`, `--`, `;`, `/*`, or a `*` block-comment
  continuation)
- an HTML comment on its own line (`<!-- content-guard: allow all file -->`)
- a trailing line comment after other code (`count = 1  # content-guard: allow all file`)

Placement anywhere in the file is fine. A quoted string is not a directive.

## String literals are not directives

A file-scoped marker inside a string literal is ignored. The scanner keeps
enforcing other findings in that file and emits a `tooling/ignored-file-allow`
warning so the ignored marker is visible:

```python
INTERNAL_HOST = "192.168.77.31"
text = "<!-- content-guard: allow all file -->\npayload"
```

The private IPv4 still `BLOCK`s. The string on the later line does not switch
the file off. Test files that mention the marker as data must use a real
comment if they intend a whole-file exemption.

`--no-allow-comments` disables both honoring and the ignored-marker warning.
