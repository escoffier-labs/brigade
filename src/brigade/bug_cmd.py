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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import __version__
from .bug_collect import collect, collect_doctor
from .bug_redaction import Redactor, ReportBlocked, assert_clean
from .component_paths import data_root
from .localio import canonical_json_digest, write_json, write_text_atomic
from .inbox_lock import held_file_lock

BUG_EMAIL = "me@solomonneas.dev"  # content-guard: allow pii/email
REPOSITORY = "escoffier-labs/brigade"
ISSUES_URL = f"https://github.com/{REPOSITORY}/issues"
URL_BUDGET = 6000
EMAIL_BUDGET = 1800
BUNDLE_CAP = 60 * 1024
_SECURITY = re.compile(
    r"\b(?:security|vulnerabilit\w*|exploit\w*|credential leak|token leak|remote code execution|path traversal)\b", re.I
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


def fingerprint(last_error: dict[str, Any] | None, check_names: list[str]) -> str:
    error = last_error or {}
    message = str(error.get("message_head", ""))
    message = re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s<>]+", "<path>", message)
    message = re.sub(r"\b[\w.-]*(?:[0-9][\w.-]*)\b", "<id>", message)
    identity = [
        error.get("command", ""),
        error.get("exception_type", ""),
        sorted(check_names),
        " ".join(message.split()),
        ".".join(__version__.split(".")[:2]),
    ]
    return "bf-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def render(bundle: dict[str, Any]) -> str:
    runtime, user = bundle["runtime"], bundle["user"]
    # A full JSON rendering keeps every schema field visible. No source text is
    # interpolated into commands, headers, or hidden HTML comments.
    return (
        f"### What happened\n\n{user['summary']}\n\n"
        f"### Brigade version\n\n{bundle['brigade']['version']}\n\n"
        f"### Python version\n\n{runtime['python']}\n\n"
        f"### OS\n\n{runtime['os']} {runtime['release']} {runtime['arch']}\n\n"
        f"### Fingerprint\n\n{bundle['fingerprint']}\n\n"
        f"### Doctor summary\n\n{json.dumps(bundle['doctor'], ensure_ascii=False, indent=2)}\n\n"
        f"### Full output\n\n{json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)}\n"
    )


def _duplicates(bundle: dict[str, Any], redactor: Redactor) -> tuple[list[dict[str, Any]], str]:
    try:
        queries = [bundle["fingerprint"]]
        error = bundle.get("last_error") or {}
        if error.get("exception_type") and error.get("command"):
            queries.append(f"{error['exception_type']} {error['command']}")
        raw = []
        for query in queries:
            raw = json.loads(
                gh("search", "issues", query, "--repo", REPOSITORY, "--json", "number,title,state,url", "--limit", "10")
            )
            if not isinstance(raw, list):
                raise ValueError("invalid search")
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
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], "dedup skipped"


def _bounded_link(base: str, fixed: dict[str, str], variable: dict[str, str], budget: int) -> str:
    """Measure the fully percent-encoded UTF-8 URL, including fixed query fields."""
    values = dict(variable)
    while True:
        link = base + "?" + urlencode({**fixed, **values})
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
            "title": bundle["user"]["title"],
            "what-happened": bundle["user"]["summary"],
            "logs": "Full redacted bundle: attach the local bundle.md shown by Brigade.\n"
            + json.dumps(bundle["doctor"], ensure_ascii=False),
        },
        URL_BUDGET,
    )


def _limit_submissions(root: Path, fp: str, now: datetime) -> None:
    daily = 0
    for path in root.glob("*/receipt.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not receipt.get("submitted") and receipt.get("status") != "pending":
                continue
            when = datetime.fromisoformat(receipt["created_at"])
            if when.date() == now.date():
                daily += 1
            if (receipt.get("fingerprint") == fp or receipt.get("status") == "pending") and (
                now - when
            ).total_seconds() < 300:
                raise ReportBlocked("Bug report blocked: fingerprint cooldown is 5 minutes")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ReportBlocked):
                raise
            continue
    if daily >= 5:
        raise ReportBlocked("Bug report blocked: daily cap is 5 submissions")


def _output_tail(path: Path | None) -> str:
    if path is None:
        return ""
    # Bounded read even for a multi-gigabyte selected log. UTF-8 replacement is
    # preferable to preserving undecodable bytes from a private file.
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - BUNDLE_CAP))
        return "\n".join(handle.read(BUNDLE_CAP).decode("utf-8", errors="replace").splitlines()[-200:])


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
        raw = gh("issue", "create", "--repo", REPOSITORY, "--title", bundle["user"]["title"], "--body-file", str(body))
        match = re.fullmatch(re.escape(ISSUES_URL) + r"/[1-9][0-9]*", raw)
        if match is None:
            raise OSError("gh returned no issue URL, check GitHub before retrying")
        return raw


def report(args: argparse.Namespace) -> int:
    try:
        return _report(args)
    except (OSError, ValueError, subprocess.SubprocessError):
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


def _report(args: argparse.Namespace) -> int:
    if args.kind == "security":
        raise ReportBlocked("Bug report blocked: report vulnerabilities privately using SECURITY.md")
    if os.environ.get("BRIGADE_BUG_REPORT", "").lower() == "never" and not args.dry_run and args.via != "file":
        raise ReportBlocked("Bug report blocked: BRIGADE_BUG_REPORT=never disables submission")
    summary = args.summary_file.read_text(encoding="utf-8") if args.summary_file else (args.summary or "")
    if not summary.strip():
        raise ReportBlocked("Bug report blocked: a nonempty --summary or --summary-file is required")
    if _SECURITY.search(summary):
        raise ReportBlocked("Bug report blocked: report security concerns privately using SECURITY.md")
    target = args.target.resolve()
    doctor = collect_doctor(target)
    if any(
        c.get("status") == "FAIL" and _SECURITY.search(str(c.get("name")) + " " + str(c.get("detail")))
        for c in doctor.get("checks", [])
    ):
        raise ReportBlocked("Bug report blocked: security-category failure, use SECURITY.md")
    redactor = Redactor(target)
    bundle = redactor.project(
        collect(
            target,
            summary=summary,
            title=args.title,
            kind=args.kind,
            doctor=doctor,
            output=_output_tail(args.include_output),
        )
    )
    # Schema and package metadata are fixed public constants, not local input.
    bundle["schema"] = "brigade.bug-report.v1"
    bundle["brigade"]["version"] = __version__
    bundle["fingerprint"] = fingerprint(
        bundle["last_error"], [c["name"] for c in bundle["doctor"]["checks"] if c["status"] == "FAIL"]
    )
    markdown = render(bundle)
    encoded = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)
    if max(len(encoded.encode()), len(markdown.encode())) > BUNDLE_CAP:
        raise ReportBlocked("Bug report blocked: redacted bundle exceeds 60 KB")
    assert_clean(encoded)
    assert_clean(markdown)
    # Nothing remote happens until both final serialized forms pass the scan.
    duplicates, dedup_status = (
        ([], "dedup skipped") if args.dry_run or args.via == "file" else _duplicates(bundle, redactor)
    )
    root = Path(data_root()) / "brigade" / "bug-reports"
    report_id = uuid.uuid4().hex
    directory = root / report_id
    directory.mkdir(parents=True, mode=0o700)
    write_json(directory / "bundle.json", bundle)
    write_text_atomic(directory / "bundle.md", markdown)
    receipt: dict[str, Any] = {
        "schema": "brigade.bug-report-receipt.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_id": report_id,
        "fingerprint": bundle["fingerprint"],
        "bundle_digest": canonical_json_digest(bundle),
        "redaction": redactor.record(),
        "destination": "file",
        "consent": {"mode": "none"},
        "duplicates": duplicates,
        "duplicate_choice": "new",
        "submitted": False,
        "status": "prepared",
        "issue_url": None,
    }
    receipt_path = directory / "receipt.json"
    write_json(receipt_path, receipt)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.json
    if not args.json:
        print(markdown, end="")
        print(f"Redactions: {json.dumps(redactor.record())}\nDedup: {dedup_status}")
        for duplicate in duplicates:
            print(f"Duplicate #{duplicate['number']}: {duplicate['url']} ({duplicate['state']}) {duplicate['title']}")
        print(f"Destination: {args.via}. GitHub reports are public. Email goes to the maintainer.")
        print(f"Full redacted bundle: {directory / 'bundle.md'}")
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
        receipt.update(destination=via, status="pending", duplicate_choice="comment" if comment_on else "new")
        with held_file_lock(root / "submission.lock", deadline_seconds=1):
            _limit_submissions(root, bundle["fingerprint"], datetime.now(timezone.utc))
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
        "redaction": redactor.record(),
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
