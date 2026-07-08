from __future__ import annotations

import re

from .types import Rule

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
            r"|(?<!\w)\(555\)\s*\d{3}[\s.-]?\d{4}(?!\w)"
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
            r"\b[A-Z0-9._%+-]+@(?:"
            r"(?:[A-Z0-9-]+\.)*example\.(?:com|org|net)"
            r"|(?:[A-Z0-9-]+\.)*(?:test|invalid|localhost|local)"
            r")\b"
        ),
        replacement="<EXAMPLE_EMAIL>",
        description="Reserved-for-documentation email (RFC 2606 / mDNS .local — illustrative).",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="ssh-private-target",
        category="infrastructure",
        pattern=r"\b[\w.-]+@(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\S+)?",
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
        pattern=r"\blocalhost:\d+\b",
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
        pattern=r"\bport\s+\d{4,5}\b",
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
        id="email",
        category="pii",
        # (?!:\S) keeps SSH/scp remotes like git@github.com:owner/repo.git from
        # matching while emails followed by ": " in prose still do.
        pattern=r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b(?!:\S)",
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
            r"|(?<!\w)\(\d{3}\)\s*\d{3}[\s.-]?\d{4}(?!\w)"
            r"|(?<!\w)\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
            r"|\b(?:phone|tel|call)[\s:]+\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
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
        pattern=r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        replacement="[redacted-jwt]",
        description="JSON Web Token.",
    ),
    Rule(
        id="bearer-token",
        category="secret",
        pattern=r"\bBearer\s+[A-Za-z0-9_~+/=-](?:[A-Za-z0-9._~+/=-]{18,}[A-Za-z0-9_~+/=-])\b",
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
        # variable, not a hardcoded secret. Quoted 20+ char literals always
        # match.
        pattern=(
            r"\b(?:api[_-]?key|token|secret)\s*[:=]\s*"
            r"(?!(?:process\.env|os\.environ|os\.getenv|config\.|cfg\.|settings\.|env\.|\$\{|\$\(|null\b|None\b|undefined\b|''|\"\"))"
            r"(?:"
            r"['\"][A-Za-z0-9._~+/=-]{20,}['\"]"
            r"|"
            r"(?!(?:[A-Za-z_$][A-Za-z0-9_$]*\.)+[A-Za-z_$][A-Za-z0-9_$]*(?![A-Za-z0-9._~+/=-]))"
            r"(?=[A-Za-z._~+/=-]*[0-9])"
            r"[A-Za-z0-9_~+/=-](?:[A-Za-z0-9._~+/=-]{18,}[A-Za-z0-9_~+/=-])"
            r")"
        ),
        replacement="[redacted-secret]",
        description="Likely API key, token, or secret assignment.",
        flags=re.IGNORECASE,
    ),
    Rule(
        id="private-key-block",
        category="secret",
        pattern=r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        replacement="[redacted-private-key]",
        description="PEM private key block.",
    ),
)
