# Redacted Secret Example

The copied terminal output below uses fake values that match common credential
shapes. Do not replace these with real credentials.

```text
CONTENT_GUARD_API_KEY=abcdefghijklmnopqrstuvwxyz123456
content-guard: allow bearer-token
authorization: Bearer exampletoken1234567890exampletoken1234567890
```

Expected handling: secret-shaped strings should be blocked even when surrounding
content looks like ordinary setup notes.
