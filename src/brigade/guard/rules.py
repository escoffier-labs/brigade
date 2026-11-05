from __future__ import annotations

import re

from .types import Rule

# Bound retryable spans shared by the memory scanners. Address local parts
# permit 256 characters, token bodies 4096, provider suffixes 8192 and PEM bodies 8192. Longer uninterrupted
# candidates need another rule or human review. Delimited paths stay streaming.
DEFAULT_RULES: tuple[Rule, ...] = (
    # Example-pattern rules placed BEFORE general patterns so they claim
    # their spans first (engine uses first-match-wins via _overlaps).
    # These cover reserved-for-documentation ranges that should warn, not block:
    #   - NANPA reserved 555-area phone numbers (only the 555-01XX range is
    #     truly reserved, but 555-XXXX in general is widely used in fiction
    #     and examples and almost never a real subscriber line)
    #   - RFC 2606 reserved example.{com,org,net} email domains
    Rule(
        id="example-phone-555",
        category="pii",
        pattern=(
            r"(?:"
            r"(?<!\w)\+1[\s.-]?\(?555\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
            r"|(?<!\w)\(555\)\s{0,256}\d{3}[\s.-]?\d{4}(?!\w)"
            r"|(?<!\w)555[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
            r")"
        ),
        replacement="<EXAMPLE_PHONE>",
        description="NANPA-reserved 555-area phone number (illustrative/example).",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="example-email-reserved",
        category="pii",
        # Reserved-for-non-real-use email TLDs:
        #   *.example.{com,org,net}       — RFC 2606
        #   *.test                        — RFC 2606
        #   *.invalid                     — RFC 2606
        #   *.localhost                   — RFC 2606
        #   *.local                       — mDNS / RFC 6762 (link-local, not real internet domains)
        # Also matches *@<host>.example for completeness.
        pattern=(
            r"\b[A-Z0-9._%+-]{1,256}@(?:"
            r"(?:[A-Z0-9-]{1,63}\.){0,127}example\.(?:com|org|net)"
            r"|(?:[A-Z0-9-]{1,63}\.){0,127}(?:test|invalid|localhost|local)"
            r")\b"
        ),
        replacement="<EXAMPLE_EMAIL>",
        description="Reserved-for-documentation email (RFC 2606 / mDNS .local — illustrative).",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="ssh-private-target",
        category="infrastructure",
        pattern=r"\b[\w.-]{1,256}@(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\S{1,4096})?",
        replacement="[redacted-target]",
        description="SSH or SCP target on a private IP address.",
    ),
    Rule(
        id="private-ipv4",
        category="infrastructure",
        pattern=r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
        replacement="[redacted-ip]",
        description="RFC 1918 private IPv4 address.",
    ),
    Rule(
        id="loopback-ipv4",
        category="infrastructure",
        pattern=r"\b127\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        replacement="[redacted-ip]",
        description="Loopback IPv4 address.",
    ),
    Rule(
        id="localhost-port",
        category="infrastructure",
        pattern=r"\blocalhost:\d{1,256}\b",
        replacement="[redacted-service]",
        description="Local service endpoint.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="localhost-bare",
        category="infrastructure",
        pattern=r"(?<![\w.-])local" r"host(?![\w.-])",
        replacement="[redacted-service]",
        description="Bare local host reference.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="port-reference",
        category="infrastructure",
        pattern=r"\bport\s{1,256}\d{4,5}\b",
        replacement="port [redacted]",
        description="Likely internal service port reference.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="coauthored-by-trailer",
        category="attribution",
        pattern=r"^Co-authored-by:\s*.+(?:\r?\n)?",
        replacement="",
        description="Git co-author trailer.",
        flags=re.IGNORECASE | re.MULTILINE,
    ),
    Rule(
        id="windows-home-path",
        category="pii",
        # Match before home-path so /Users inside a drive-qualified path
        # cannot claim a partial span and suppress the complete Windows path.
        # Quotes bound paths with spaced directory leaves. Without quotes,
        # spaces need a following separator or filename extension so prose
        # after the path is not consumed. Forward slashes and raw or escaped
        # backslashes can be mixed independently at each component boundary.
        pattern=(
            r"(?<![\w.-])(?:(?<=(?P<windows_quote>[\"'`]))|(?<![\"'`]))"
            r"[A-Z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)"
            r"(?(windows_quote)"
            r"[A-Za-z0-9._-](?:(?!(?P=windows_quote))[^\r\n<>:|?*])*?(?=(?P=windows_quote))"
            r"|"
            r"(?:[A-Za-z0-9._@+-]+(?: +[A-Za-z0-9._@+-]+)*(?:\\{1,2}|/))*"
            r"(?:[A-Za-z0-9_@+-]+(?:[ .]+[A-Za-z0-9_@+-]+)*?\.[A-Za-z0-9]+"
            r"(?![\w-]|\.[\w-])|[A-Za-z0-9._@+-]+)"
            r")"
        ),
        replacement="<PRIVATE_HOME_PATH>",
        description="Windows filesystem path under a user home directory.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="home-path",
        category="pii",
        # Matches absolute paths under conventional home roots including the
        # username segment and any subpath. The username leaks personal data,
        # so the whole path is redacted.
        # Tool config directories rendered directly under a sandbox home root
        # (no username segment) are excluded; every other first segment,
        # including `.`, `..` and other dot names, still matches.
        pattern=(
            r"(?<![\w.-])/(?:home|Users)/"
            r"(?!\.(?:agents|cache|claude|codebuddy|codex|commandcode|config|copilot|cursor|factory|gemini|grok"
            r"|kimi|local|localcode|pi|tabby)(?:/|$|[^A-Za-z0-9._-]))"
            r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._@+-]+)*"
        ),
        replacement="<PRIVATE_HOME_PATH>",
        description="Filesystem path under a user home directory.",
    ),
    Rule(
        id="root-path",
        category="infrastructure",
        # Matching quotes bound spaced components without consuming the
        # quotes themselves. Unquoted paths keep their whitespace boundary.
        pattern=(
            r"(?<![\w.-])(?:"
            r"(?<=(?P<root_quote>[\"'`]))/root(?:/[A-Za-z0-9._@+ -]+)+(?=(?P=root_quote))"
            r"|/root(?:/[A-Za-z0-9._@+-]+)+)"
        ),
        replacement="[redacted-root-path]",
        description="Filesystem path under the root user's home directory.",
    ),
    Rule(
        id="email",
        category="pii",
        # (?!:\S) keeps SSH/scp remotes like git@github.com:owner/repo.git from
        # matching while emails followed by ": " in prose still do.
        pattern=r"\b[A-Z0-9._%+-]{1,256}@[A-Z0-9.-]{1,256}\.[A-Z]{2,63}\b(?!:\S)",
        replacement="<PRIVATE_EMAIL>",
        description="Email address.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="us-phone",
        category="pii",
        pattern=(
            r"(?:"
            r"(?<!\w)\+1[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
            r"|(?<!\w)\(\d{3}\)\s{0,256}\d{3}[\s.-]?\d{4}(?!\w)"
            r"|(?<!\w)\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
            r"|\b(?:phone|tel|call)[\s:]{1,256}\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
            r")"
        ),
        replacement="<PRIVATE_PHONE>",
        description="US-style phone number with phone-shape separators or a phone/tel/call cue word.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="jwt-token",
        category="secret",
        # All standard JWTs base64url-encode a JSON header, so they start with
        # eyJ. A dedicated rule keeps recall when api-key-assignment skips
        # dotted identifier chains (JWT segments would otherwise look like a
        # three-segment chain).
        pattern=r"\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\b",
        replacement="[redacted-jwt]",
        description="JSON Web Token.",
    ),
    Rule(
        id="bearer-token",
        category="secret",
        pattern=r"\bBearer\s{1,256}[A-Za-z0-9_~+/=-](?:[A-Za-z0-9._~+/=-]{18,4094}[A-Za-z0-9_~+/=-])(?![A-Za-z0-9_~+/=-])",
        replacement="Bearer [redacted-secret]",
        description="Bearer token.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="api-key-assignment",
        category="secret",
        # Negative lookahead skips assignments whose RHS is a safe getter:
        #   apiKey = process.env.X      (Node env lookup)
        #   apiKey = os.environ["X"]    (Python env lookup)
        #   apiKey = os.getenv("X")     (Python env lookup)
        #   apiKey = "${...}"           (shell interpolation)
        #   apiKey = config.x or cfg.x  (config object access)
        # These are code reading from a secret, not the secret itself.
        # Unquoted values must contain a digit and must not be a dotted
        # identifier chain (apiKeys.anthropicApiKey) or a bare camelCase
        # identifier (daemonConfigPrimaryToken): those are code passing a
        # variable, not a hardcoded secret. Quoted 20 through 4096 character literals always
        # match.
        pattern=(
            r"\b(?:api[_-]?key|token|secret)\s{0,256}[:=]\s{0,256}"
            r"(?!(?:process\.env|os\.environ|os\.getenv|config\.|cfg\.|settings\.|env\.|\$\{|\$\(|null\b|None\b|undefined\b|''|\"\"))"
            r"(?:"
            r"['\"][A-Za-z0-9._~+/=-]{20,4096}['\"]"
            r"|"
            r"(?!(?:[A-Za-z_$][A-Za-z0-9_$]*\.)+[A-Za-z_$][A-Za-z0-9_$]*(?![A-Za-z0-9._~+/=-]))"
            r"(?=[A-Za-z._~+/=-]{0,4095}[0-9])"
            r"[A-Za-z0-9_~+/=-](?:[A-Za-z0-9._~+/=-]{18,4094}[A-Za-z0-9_~+/=-])"
            r")"
        ),
        replacement="[redacted-secret]",
        description="Likely API key, token, or secret assignment.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="bare-token-prefix",
        category="secret",
        # Match the new families case-sensitively, like the worklore validator.
        # Google and GitLab bodies can end in a hyphen; a trailing word
        # boundary would truncate that character or miss a minimum-length key.
        pattern=(
            r"\b(?:(?:github_pat_[A-Za-z0-9_]{20,8192}|gh[pousr]_[A-Za-z0-9]{20,8192}"
            r"|xox[baprs]-[A-Za-z0-9-]{8,8192}|sk-(?:live|test|proj|ant)-[A-Za-z0-9_-]{8,8192}"
            r"|AKIA[A-Z0-9]{16}|(?-i:ASIA[A-Z0-9]{16}))\b"
            r"|(?-i:(?:AIza[A-Za-z0-9_-]{30,8192}|glpat-[A-Za-z0-9_-]{16,8192})(?![A-Za-z0-9_-])))"
        ),
        replacement="[redacted-secret]",
        description="Credential with a well-known provider token prefix.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="private-key-block",
        category="secret",
        pattern=r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]{0,64}PRIVATE KEY-----",
        replacement="[redacted-private-key]",
        description="PEM private key block.",
    ),
    Rule(
        id="private-hostname",
        # engine._hostname_context skips code-shaped identifiers. Bare dotted
        # names without string, URL, prose or config context are ambiguous.
        category="infrastructure",
        pattern=(
            r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"(?:local|internal|lan|corp|home|intranet|localdomain)(?![\w-]|\.[\w-]|\()"
        ),
        replacement="[redacted-host]",
        description="Private or local fully qualified hostname.",
        flags=re.IGNORECASE,
    ),
)
