"""User-initiated bug reports with local previews and explicit submission consent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import __version__, bug_io
from .bug_collect import collect, collect_diagnostics
from .bug_redaction import Redactor, ReportBlocked, assert_clean, assert_terminal_safe
from .component_paths import data_root
from .localio import canonical_json_digest, write_json, write_text_atomic
from .inbox_lock import held_file_lock

BUG_EMAIL = "me@solomonneas.dev"  # content-guard: allow pii/email
REPOSITORY = "escoffier-labs/brigade"
ISSUES_URL = f"https://github.com/{REPOSITORY}/issues"
URL_BUDGET = 6000
EMAIL_BUDGET = 1800
BUNDLE_CAP = 60 * 1024
OUTPUT_TAIL_CAP = 16 * 1024
_CREDENTIAL = r"(?:private[- ]keys?|(?:API|access|refresh|auth(?:entication)?|session)[- ](?:keys?|tokens?)|credentials?|secrets?|personal data)"
_SECURITY = re.compile(
    r"\b(?:security (?:vulnerability|issue)|vulnerabilit\w*|exploit\w*|CVE-\d{4}-\d+|"
    r"(?:SQL|command|code|shell|LDAP|template) injection|(?:(?:remote|arbitrary) )?code execution|"
    r"(?:authentication|authorization|auth) bypass|privilege escalation|cross[- ]site (?:scripting|request forgery)|"
    r"XSS|CSRF|SSRF|RCE|XXE|insecure deserialization|directory traversal|path traversal|"
    r"symlink (?:race|attack|vulnerability)|TOCTOU|(?:sandbox|container) escape|"
    r"(?:elevated|unauthorized) privileges?|"
    r"arbitrary file (?:access|read|write|overwrite)|(?:read\w*|writ\w*) outside|guard bypass|"
    rf"(?:leak\w*|expos\w*|disclos\w*)[^.\n]{{0,80}}\b{_CREDENTIAL}|"
    rf"{_CREDENTIAL}[^.\n]{{0,80}}\b(?:leak\w*|expos\w*|disclos\w*)|"
    r"leak of (?:keys?|tokens?)|"
    r"buffer overflow|use[- ]after[- ]free|denial[- ]of[- ]service)\b",
    re.I,
)


def gh(*args: str) -> str:
    """The only gh subprocess boundary. Tests replace this with an offline fake."""
    binary = shutil.which("gh")
    if binary is None:
        raise OSError("gh unavailable")
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise OSError("gh request failed")
    return result.stdout.strip()


def _normalize_fingerprint(text: str) -> str:
    text = re.sub(
        r"""["'](?:[A-Za-z]:[\\/]|/)[^"'\r\n]+["']|"""
        r"""(?:[A-Za-z]:[\\/]|/)(?:[^\r\n<>"'\\/]+[\\/])*[^\s<>"'\\/]*""",
        "<path>",
        text,
    )
    text = re.sub(r"\b[\w.-]*(?:[0-9][\w.-]*)\b", "<id>", text)
    return " ".join(text.split())


def fingerprint(last_error: dict[str, Any] | None, check_names: list[str], summary: str = "") -> str:
    error = last_error or {}
    identity = [
        error.get("command", ""),
        error.get("exception_type", ""),
        error.get("exit_code"),
        sorted(_normalize_fingerprint(name) for name in check_names),
        ".".join(__version__.split(".")[:2]),
        hashlib.sha256(_normalize_fingerprint(summary.lower()).encode()).hexdigest(),
    ]
    return "bf-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def _code_block(text: str, language: str = "text") -> str:
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}"


def _prose(text: str) -> str:
    return text.replace("@", "@\u200b")


def render(bundle: dict[str, Any]) -> str:
    runtime, user = bundle["runtime"], bundle["user"]
    # Every untrusted field is literal code, including JSON string contents.
    # A content-derived fence prevents embedded backticks from closing it.
    return (
        f"### What happened\n\n{_code_block(user['summary'])}\n\n"
        f"### Brigade version\n\n{_code_block(bundle['brigade']['version'])}\n\n"
        f"### Python version\n\n{_code_block(runtime['python'])}\n\n"
        f"### OS\n\n{_code_block(' '.join(runtime[key] for key in ('os', 'release', 'arch')))}\n\n"
        f"### Fingerprint\n\n{bundle['fingerprint']}\n\n"
        f"### Bug diagnostics\n\n{_code_block(json.dumps(bundle['bug_diagnostics'], ensure_ascii=False, indent=2, sort_keys=True), 'json')}\n\n"
        f"### Full output\n\n{_code_block(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), 'json')}\n"
    )


def _search_issues(query: str) -> list[Any]:
    try:
        raw = json.loads(
            gh("search", "issues", query, "--repo", REPOSITORY, "--json", "number,title,state,url", "--limit", "10")
        )
    except (OSError, subprocess.SubprocessError):
        url = "https://api.github.com/search/issues?" + urlencode(
            {"q": f"repo:{REPOSITORY} is:issue {query}", "per_page": "10"}
        )
        request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "brigade-cli"})
        with urlopen(request, timeout=10) as response:
            body = response.read(BUNDLE_CAP + 1)
        if len(body) > BUNDLE_CAP:
            raise ValueError("search response exceeds budget") from None
        raw = json.loads(body)["items"]
    if not isinstance(raw, list):
        raise ValueError("invalid search")
    return raw


def _duplicates(bundle: dict[str, Any], redactor: Redactor) -> tuple[list[dict[str, Any]], str]:
    try:
        queries = [bundle["fingerprint"]]
        error = bundle.get("last_error") or {}
        if error.get("exception_type") and error.get("command"):
            queries.append(f"{error['exception_type']} {error['command']}")
        raw = []
        for query in queries:
            raw = _search_issues(query)
            if raw:
                break
        found = []
        for item in raw[:10]:
            if not isinstance(item, dict) or type(item.get("number")) is not int or item["number"] < 1:
                continue
            title = redactor.text(str(item.get("title", ""))[:200])
            assert_clean(title)
            found.append(
                {
                    "number": item["number"],
                    "url": f"{ISSUES_URL}/{item['number']}",
                    "title": title,
                    "state": "closed" if str(item.get("state")).lower() == "closed" else "open",
                }
            )
        return found, "searched"
    except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return [], "dedup skipped"


def _bounded_link(
    base: str, fixed: dict[str, str], variable: dict[str, str], budget: int, *, fenced: tuple[str, ...] = ()
) -> str:
    """Measure the fully percent-encoded UTF-8 URL, including fixed query fields."""
    values = dict(variable)
    while True:
        link = (
            base
            + "?"
            + urlencode(
                {**fixed, **{key: _code_block(value) if key in fenced else value for key, value in values.items()}},
                quote_via=quote,
            )
        )
        if len(link.encode("utf-8")) <= budget:
            return link
        key = max(values, key=lambda name: len(values[name].encode("utf-8")))
        if not values[key]:
            raise ReportBlocked("Bug report blocked: destination URL exceeds budget")
        values[key] = values[key][: len(values[key]) // 2]


def destination_link(via: str, bundle: dict[str, Any]) -> str:
    if via == "email":
        return _bounded_link(
            f"mailto:{BUG_EMAIL}",
            {},
            {
                "subject": bundle["user"]["title"],
                "body": bundle["fingerprint"]
                + "\n"
                + bundle["user"]["summary"]
                + "\nFull redacted bundle: attach the local bundle.md shown by Brigade.",
            },
            EMAIL_BUDGET,
        )
    return _bounded_link(
        ISSUES_URL + "/new",
        {
            "template": "bug.yml",
            "fingerprint": bundle["fingerprint"],
            "version": bundle["brigade"]["version"],
            "python": bundle["runtime"]["python"],
            "os": bundle["runtime"]["os"],
        },
        {
            "title": _prose(bundle["user"]["title"]),
            "what-happened": bundle["user"]["summary"],
            "logs": "Full redacted bundle: attach the local bundle.md shown by Brigade.\n"
            + json.dumps(bundle["bug_diagnostics"], ensure_ascii=False),
        },
        URL_BUDGET,
        fenced=("what-happened",),
    )


def _attempts(root: Path, day: datetime) -> list[dict[str, Any]]:
    path = root / "attempts" / f"{day.date().isoformat()}.jsonl"
    try:
        raw = bug_io.read_bytes(path, limit=BUNDLE_CAP)
    except FileNotFoundError:
        return []
    except ValueError:
        raise ReportBlocked("Bug report blocked: submission attempt log exceeds 60 KB") from None
    lines = raw.splitlines()
    if len(lines) > 5:
        raise ReportBlocked("Bug report blocked: invalid submission attempt log")
    attempts = []
    for line in lines:
        attempt = json.loads(line)
        if (
            not isinstance(attempt, dict)
            or re.fullmatch(r"[0-9a-f]{32}", str(attempt.get("report_id"))) is None
            or re.fullmatch(r"bf-[0-9a-f]{12}", str(attempt.get("fingerprint"))) is None
        ):
            raise ReportBlocked("Bug report blocked: invalid submission attempt log")
        when = datetime.fromisoformat(str(attempt.get("attempted_at")))
        if when.tzinfo is None or when.astimezone(timezone.utc).date() != day.date():
            raise ReportBlocked("Bug report blocked: invalid submission attempt timestamp")
        attempts.append(attempt)
    return attempts


def _append_attempt(path: Path, record: dict[str, Any]) -> None:
    bug_io.append_bytes(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def _limit_submissions(root: Path, fp: str, now: datetime) -> None:
    # Only today's and yesterday's logs are needed, including across midnight.
    # Each has at most five entries and a bounded byte read. Mutable receipts
    # establish uncertain outcomes, but never determine the attempt count.
    today = _attempts(root, now)
    if len(today) >= 5:
        raise ReportBlocked("Bug report blocked: daily cap is 5 submissions")
    for attempt in today + _attempts(root, now - timedelta(days=1)):
        when = datetime.fromisoformat(str(attempt.get("attempted_at")))
        if (now - when).total_seconds() >= 300:
            continue
        pending = True
        if attempt["fingerprint"] != fp:
            try:
                receipt = json.loads(_read_bounded(root / attempt["report_id"] / "receipt.json"))
                pending = not isinstance(receipt, dict) or receipt.get("status") not in {"submitted", "handoff"}
            except (OSError, ValueError, TypeError):
                pass
        if attempt["fingerprint"] == fp or pending:
            raise ReportBlocked("Bug report blocked: fingerprint cooldown is 5 minutes")


def _output_tail(path: Path | None) -> str:
    if path is None:
        return ""
    # Bounded read even for a multi-gigabyte selected log. UTF-8 replacement is
    # preferable to preserving undecodable bytes from a private file.
    raw = bug_io.read_tail(path, limit=BUNDLE_CAP)
    return "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-200:])


def _submit(via: str, bundle: dict[str, Any], markdown: str, comment_on: int | None) -> str:
    if via in {"url", "email"}:
        return destination_link(via, bundle)
    if via == "file":
        return ""
    # Use a private, fresh body file containing the reviewed in-memory bytes.
    # A changed bundle.md on disk cannot silently alter a later submission.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="brigade-bug-") as temporary:
        body = Path(temporary) / "body.md"
        write_text_atomic(body, markdown)
        if comment_on is not None:
            gh("issue", "comment", str(comment_on), "--repo", REPOSITORY, "--body-file", str(body))
            return f"{ISSUES_URL}/{comment_on}"
        raw = gh(
            "issue",
            "create",
            "--repo",
            REPOSITORY,
            "--title",
            _prose(bundle["user"]["title"]),
            "--body-file",
            str(body),
        )
        match = re.fullmatch(re.escape(ISSUES_URL) + r"/[1-9][0-9]*", raw)
        if match is None:
            raise OSError("gh returned no issue URL, check GitHub before retrying")
        return raw


def report(args: argparse.Namespace) -> int:
    try:
        return _report(args)
    except (EOFError, KeyboardInterrupt):
        print("Bug report cancelled. Check the local receipt before retrying.", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Diagnostics and transport exceptions can carry local paths or tokens.
        # Only our own fixed refusal messages are safe to print verbatim.
        exc = sys.exc_info()[1]
        print(
            str(exc)
            if isinstance(exc, ReportBlocked)
            else "Bug report blocked: local collection or submission failed. Check the local receipt before retrying.",
            file=sys.stderr,
        )
        return 2


def _read_bounded(path: Path) -> bytes:
    try:
        return bug_io.read_bytes(path, limit=BUNDLE_CAP)
    except ValueError:
        raise ReportBlocked("Bug report blocked: selected input exceeds 60 KB") from None


def _check_security(bundle: dict[str, Any], *, not_security: bool = False) -> None:
    user = bundle.get("user", {})
    if user.get("kind") == "security":
        raise ReportBlocked("Bug report blocked: report security concerns privately using SECURITY.md")
    if not_security:
        return
    texts = [str(user.get(key, "")) for key in ("summary", "title", "output_tail")]
    texts.append(str((bundle.get("last_error") or {}).get("message_head", "")))
    if any(_SECURITY.search(text) for text in texts):
        raise ReportBlocked("Bug report blocked: report security concerns privately using SECURITY.md")
    if any(
        c.get("status") == "FAIL" and _SECURITY.search(str(c.get("name")) + " " + str(c.get("detail")))
        for c in bundle.get("bug_diagnostics", {}).get("checks", [])
    ):
        raise ReportBlocked("Bug report blocked: security-category failure, use SECURITY.md")


def _load_prepared(report_id: str, *, not_security: bool = False) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    if re.fullmatch(r"[0-9a-f]{32}", report_id) is None:
        raise ReportBlocked("Bug report blocked: invalid --report-id")
    directory = Path(data_root()).resolve() / "brigade" / "bug-reports" / report_id
    bundle = json.loads(_read_bounded(directory / "bundle.json"))
    markdown = _read_bounded(directory / "bundle.md").decode("utf-8")
    receipt = json.loads(_read_bounded(directory / "receipt.json"))
    if (
        not isinstance(bundle, dict)
        or not isinstance(receipt, dict)
        or bundle.get("schema") != "brigade.bug-report.v1"
        or "doctor" in bundle
        or not isinstance(bundle.get("bug_diagnostics"), dict)
        or receipt.get("schema") != "brigade.bug-report-receipt.v1"
        or receipt.get("report_id") != report_id
        or receipt.get("bundle_digest") != canonical_json_digest(bundle)
        or receipt.get("preview_digest") != hashlib.sha256(markdown.encode()).hexdigest()
    ):
        raise ReportBlocked("Bug report blocked: prepared bundle digest mismatch, prepare a new preview")
    if receipt.get("status") == "submitted":
        issue_url = str(receipt.get("issue_url", ""))
        if re.fullmatch(re.escape(ISSUES_URL) + r"/[1-9][0-9]*", issue_url) is None:
            issue_url = "check the local receipt"
        raise ReportBlocked(f"Bug report blocked: already submitted: {issue_url}")
    for rows, keys in (
        (bundle.get("bug_diagnostics", {}).get("checks", []), {"name", "status"}),
        (bundle.get("recent_failures", {}).get("runs", []), {"id", "status"}),
    ):
        if any(not isinstance(row, dict) or set(row) != keys for row in rows):
            raise ReportBlocked("Bug report blocked: outdated diagnostic projection, prepare a new preview")
    assert_terminal_safe(bundle)
    assert_clean(json.dumps(bundle, ensure_ascii=False, sort_keys=True))
    assert_clean(markdown)
    _check_security(bundle, not_security=not_security)
    if markdown != render(bundle):
        raise ReportBlocked("Bug report blocked: outdated preview rendering, prepare a new preview")
    return directory, bundle, markdown, receipt


def _prepare(args: argparse.Namespace) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    summary = _read_bounded(args.summary_file).decode("utf-8") if args.summary_file else (args.summary or "")
    output = _output_tail(args.include_output)
    if not summary.strip():
        raise ReportBlocked("Bug report blocked: a nonempty --summary or --summary-file is required")
    if not args.not_security and any(_SECURITY.search(text) for text in (summary, args.title or "", output)):
        raise ReportBlocked("Bug report blocked: report security concerns privately using SECURITY.md")
    target = args.target.resolve()
    diagnostics = collect_diagnostics(target)
    collected = collect(
        target,
        summary=summary,
        title=args.title or "Brigade bug report",
        kind=args.kind,
        diagnostics=diagnostics,
        output=output,
    )
    _check_security(collected, not_security=args.not_security)
    redactor = Redactor(target)
    bundle = redactor.project(collected)
    # Schema and package metadata are fixed public constants, not local input.
    bundle["schema"] = "brigade.bug-report.v1"
    bundle["brigade"]["version"] = __version__
    bundle["fingerprint"] = fingerprint(
        bundle["last_error"],
        [c["name"] for c in bundle["bug_diagnostics"]["checks"] if c["status"] == "FAIL"],
        bundle["user"]["summary"],
    )
    user = bundle["user"]
    tail = user["output_tail"].encode("utf-8")
    budget = OUTPUT_TAIL_CAP
    while True:
        user["output_tail"] = tail[-budget:].decode("utf-8", errors="ignore") if budget else ""
        kept = len(user["output_tail"].encode("utf-8"))
        bundle["included_output"] = {"bytes_kept": kept, "truncated": len(tail) > kept}
        markdown = render(bundle)
        encoded = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if max(len(encoded.encode()), len(markdown.encode())) <= BUNDLE_CAP:
            break
        if not budget or not tail:
            raise ReportBlocked("Bug report blocked: redacted bundle exceeds 60 KB")
        budget //= 2
    assert_clean(encoded)
    assert_clean(markdown)
    root = Path(data_root()).resolve() / "brigade" / "bug-reports"
    directory = root / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    write_text_atomic(directory / "bundle.json", encoded)
    write_text_atomic(directory / "bundle.md", markdown)
    receipt: dict[str, Any] = {
        "schema": "brigade.bug-report-receipt.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_id": directory.name,
        "fingerprint": bundle["fingerprint"],
        "bundle_digest": canonical_json_digest(bundle),
        "preview_digest": hashlib.sha256(markdown.encode()).hexdigest(),
        "redaction": redactor.record(),
        "destination": "file",
        "consent": {"mode": "none"},
        "security_confirmation": {"mode": "none", "not_security": None},
        "duplicates": [],
        "duplicate_choice": "new",
        "submitted": False,
        "status": "prepared",
        "issue_url": None,
    }
    write_json(directory / "receipt.json", receipt)
    return directory, bundle, markdown, receipt


def _report(args: argparse.Namespace) -> int:
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.json
    if args.kind == "security":
        raise ReportBlocked("Bug report blocked: report vulnerabilities privately using SECURITY.md")
    if os.environ.get("BRIGADE_BUG_REPORT", "").lower() == "never" and not args.dry_run and args.via != "file":
        raise ReportBlocked("Bug report blocked: BRIGADE_BUG_REPORT=never disables submission")
    if args.yes and not args.report_id:
        raise ReportBlocked("Bug report blocked: --yes requires the approved preview's --report-id")
    if args.report_id and any((args.summary, args.summary_file, args.title, args.include_output)):
        raise ReportBlocked("Bug report blocked: --report-id cannot be combined with new report content")
    if args.yes and not interactive and not args.not_security and not args.dry_run and args.via != "file":
        raise ReportBlocked(
            "Bug report blocked: --yes also requires --not-security. Use SECURITY.md for vulnerabilities"
        )
    directory, bundle, markdown, receipt = (
        _load_prepared(args.report_id, not_security=args.not_security) if args.report_id else _prepare(args)
    )
    root = directory.parent
    report_id = directory.name
    receipt_path = directory / "receipt.json"
    # Nothing remote happens until the saved bundle's digest and both serialized forms pass.
    duplicates, dedup_status = (
        ([], "dedup skipped") if args.dry_run or args.via == "file" else _duplicates(bundle, Redactor(args.target))
    )
    receipt["duplicates"] = duplicates
    if not args.json:
        print(markdown, end="")
        print(f"Redactions: {json.dumps(receipt['redaction'])}\nDedup: {dedup_status}")
        for duplicate in duplicates:
            print(f"Duplicate #{duplicate['number']}: {duplicate['url']} ({duplicate['state']}) {duplicate['title']}")
        print(f"Destination: {args.via}. GitHub reports are public. Email goes to the maintainer.")
        print(f"Full redacted bundle: {directory / 'bundle.md'}")
        print(f"Report ID: {report_id}")
    via = args.via
    if via == "auto":
        via = "file"
        if not args.dry_run and (args.yes or interactive):
            try:
                gh("auth", "status")
                via = "gh"
            except (OSError, subprocess.SubprocessError):
                via = "url"
    approved = args.yes and not args.dry_run and via != "file"
    comment_on = args.comment_on
    if interactive and not args.dry_run and via != "file":
        answer = input("Is this a security vulnerability? [y/N] ").strip().lower()
        if answer not in {"y", "yes", "n", "no", ""}:
            raise ReportBlocked("Bug report blocked: answer yes or no to the security question")
        receipt["security_confirmation"] = {"mode": "prompt", "not_security": answer not in {"y", "yes"}}
        write_json(receipt_path, receipt)
        if answer in {"y", "yes"}:
            raise ReportBlocked("Bug report blocked: report vulnerabilities privately using SECURITY.md")
    if interactive and not args.dry_run and via != "file" and not args.yes:
        if duplicates and comment_on is None:
            choice = input("Duplicate found. Enter its number to comment, 'new', or 'abort': ").strip()
            if choice == "abort":
                via = "file"
            elif choice != "new":
                try:
                    comment_on = int(choice)
                except ValueError:
                    raise ReportBlocked("Bug report blocked: choose a shown duplicate, new, or abort") from None
        if via != "file":
            approved = input(f"Share the complete preview via {via}? [y/N] ").strip().lower() in {"y", "yes"}
            receipt["consent"]["mode"] = "prompt" if approved else "none"
    if comment_on is not None and (via != "gh" or comment_on not in [item["number"] for item in duplicates]):
        raise ReportBlocked("Bug report blocked: --comment-on must name a shown duplicate and use gh")
    destination_url = ""
    if approved:
        if os.environ.get("BRIGADE_BUG_REPORT", "").lower() == "never":
            raise ReportBlocked("Bug report blocked: BRIGADE_BUG_REPORT=never disables submission")
        assert_clean(markdown)
        if args.yes:
            receipt["consent"]["mode"] = "flag"
            if not interactive:
                receipt["security_confirmation"] = {"mode": "flag", "not_security": True}
        receipt.update(
            destination=via,
            status="pending",
            duplicate_choice="comment" if comment_on else "new",
        )
        with held_file_lock(root / "submission.lock", deadline_seconds=1):
            _load_prepared(report_id, not_security=args.not_security)
            now = datetime.now(timezone.utc)
            _limit_submissions(root, bundle["fingerprint"], now)
            receipt["attempted_at"] = now.isoformat()
            attempts = root / "attempts"
            record = {key: receipt[key] for key in ("report_id", "fingerprint", "attempted_at")}
            # Reserve capacity durably before transport, even if the outcome is lost.
            _append_attempt(attempts / f"{now.date().isoformat()}.jsonl", record)
            write_json(receipt_path, receipt)
        destination_url = _submit(via, bundle, markdown, comment_on)
        receipt.update(
            submitted=True,
            status="submitted" if via == "gh" else "handoff",
            issue_url=destination_url if via == "gh" else None,
        )
    write_json(receipt_path, receipt)
    payload = {
        "report_id": report_id,
        "fingerprint": bundle["fingerprint"],
        "preview_path": str(directory / "bundle.md"),
        "preview_markdown": markdown,
        "receipt_path": str(receipt_path),
        "redaction": receipt["redaction"],
        "duplicates": duplicates,
        "dedup_status": dedup_status,
        "submitted": receipt["submitted"],
        "destination": receipt["destination"],
        "destination_url": destination_url,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif destination_url:
        print(destination_url)
        print(f"Full redacted bundle: {directory / 'bundle.md'}")
    return 0
