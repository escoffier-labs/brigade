"""Safe, versioned master-roster document contract for fleet synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .worklore_validate import contains_credential

SCHEMA = "brigade.fleet_master_roster.v1"
MAX_DOCUMENT_BYTES = 262144
MAX_SEATS = 128
MAX_TEXT = 512
_SEAT_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CLI_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_EXPLANATORY_SECRET_VALUE = (
    r"(?:required|optional|needed|necessary|available|configured(?!\s+(?:as|to|with)\b)|set(?!\s+to\b)|missing|unset|unavailable|"
    r"not\s+(?:required|optional|needed|necessary|available|configured|set|missing|unset|unavailable)|"
    r"confidential|managed(?:\s+by\s+Vault)?|provided|stored(?:\s+securely)?|encrypted|protected|"
    r"handled|generated|rotated|masked|redacted)(?!\s+(?:as|to|with)\b)(?=$|\s|[.,;!?](?:\s|$))"
)
_DANGEROUS_VALUE = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]+|(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{12,}|(?<![A-Za-z0-9_])xapp-[A-Za-z0-9-]{12,}|(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}|(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"\b(?:Proxy-)?Authorization\s*:\s*Bearer\s+\S+|\bBearer\s+[A-Za-z0-9._~+/-]{20,}|"
    r"\b(?:Proxy-)?Authorization\s*:\s*Basic\s+"
    r"[A-Za-z0-9+/]{4,}={0,2}(?![A-Za-z0-9+/=])|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?(?:(?:(?:is\s+)?provided\s+(?:as|with)|is|are|was|were|equals|will\s+be|has\s+been|have\s+been|set\s+to|configured\s+(?:as|to|with))\s+|[:=]\s*)"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}|selection)\b)"
    r"(?:[A-Za-z0-9_.-]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s*[:=]\s*" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}|selection)\b)"
    r"(?:[A-Za-z0-9_.-]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:named|called)\s+"
    r"(?:[A-Za-z0-9_.-]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\b(?:use|select|choose|load|set|activate|switch\s+to|connect\s+(?:through|with|using))\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?auth(?:entication)?[ _-]profiles?\s+"
    r"(?!(?:is|are|as|for|in|required|needed|necessary|optional|available|selection|rotation|management|regularly)\b)"
    r"(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\)|[A-Za-z0-9_.-]+)|"
    r"\b(?:use|select|choose|load|set|activate|switch\s+to|connect\s+(?:through|with|using))\s+"
    r"(?:the\s+)?(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\)|(?!(?:the)\b)[A-Za-z0-9_.-]+)\s+"
    r"auth(?:entication)?[ _-]profiles?\b|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s*[:=]\s*"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)"
    r"\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?"
    r"(?:provided\s+(?:as|with)|stored\s+(?:as|with)|generated\s+(?:as|with)|configured\s+(?:as|with|to))\s+"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)"
    r"\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?"
    r"(?:(?:is|are|was|were|equals|will\s+be|has\s+been|have\s+been|set\s+to)\s+|[:=]\s*)"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:set|configure(?:d)?|store(?:d)?|provide(?:d)?|generate(?:d)?)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s+"
    r"(?:to|as|with)\s+" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"(?:^|[^A-Za-z0-9_.-])(?:[A-Za-z]:(?:[/\\]|[^\s])|%[A-Za-z_][A-Za-z0-9_]*%|"
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][^}]*\})"
    r"(?::[A-Za-z_][A-Za-z0-9_]*)?(?:[/\\][^\s]*)?))",
    re.IGNORECASE,
)
_PATH_LABEL = r"(?:paths?|files?(?:\s+names?)?|filenames?|director(?:y|ies)|folders?|scripts?|repositor(?:y|ies)|repos?|locations?)"
_QUOTED_PROTECTED_LABEL = re.compile(
    r"[\"'“‘`](?P<label>passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|"
    r"auth(?:entication)?[ _-]?profiles?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|"
    r"client[_ -]?secrets?|private[_ -]?keys?|environment[ _-]?variables?|home\s+directory|"
    + _PATH_LABEL
    + r")[\"'”’`]",
    re.IGNORECASE,
)
_URL_CREDENTIAL_QUERY = re.compile(
    r"https?://[^\s<>\"']*?[?&](?:password|passwd|passphrase|secret|token|auth[_-]?token|"
    r"api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)=[^&#\s]+",
    re.IGNORECASE,
)
_ENVIRONMENT_ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9=<>!])[A-Z_][A-Z0-9_]*\s*=(?!=|>)")
_ROSTER_OWNERSHIP_QUALIFIER = (
    r"(?:\s+(?:for|of|used\s+by|assigned\s+to)\s+(?:(?:this|the)\s+)?"
    r"(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?))?"
)
_EXPLICIT_ENVIRONMENT_ASSIGNMENT = re.compile(
    r"\b(?:export\s+|env\s+|set\s+(?:environment\s+variable|env)\s+|"
    r"(?:(?:use|configure)\s+(?:the\s+)?)?environment\s+variable\s+)"
    r"(?:(?:named|called)\s+)?[\"'“‘`]?[A-Za-z_][A-Za-z0-9_]*[\"'”’`]?"
    + _ROSTER_OWNERSHIP_QUALIFIER
    + r"(?:\s*=(?!=|>)|\s+(?:value\s+)?(?:set\s+to|configured\s+(?:as|to|with)|to|as|is|are|was|were|equals|will\s+be|has\s+been|have\s+been)\s+(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+|\s+(?:value\s*)?(?:=(?!=|>)|:)\s*(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+|:\s*(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+)",
    re.IGNORECASE,
)
_PATH_DECLARATION_LINKER = r"(?:is|are|was|were|equals|will\s+be|has\s+been|have\s+been|set\s+to|is\s+set\s+to|was\s+set\s+to|configured\s+(?:as|to)|:|=)"
_PATH_DECLARATION_QUALIFIER = r"(?:\s+(?:(?:for|of|used\s+by|assigned\s+to)\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)))?"
_PATH_DECLARATION = (
    r"\b" + _PATH_LABEL + r"(?:\s+value)?" + _PATH_DECLARATION_QUALIFIER + r"\s*" + _PATH_DECLARATION_LINKER
)
_ROOTED_PATH = re.compile(r"(?<![\w.-])(?:~(?:[A-Za-z0-9_.-]+)?[/\\]|[/\\]|\.{1,2}[/\\])(?=[\w.~-])")
_EXPLICIT_TERMINAL_PATH = re.compile(
    r"\b(?:home\s+directory|"
    + _PATH_LABEL
    + r")(?:\s+value)?"
    + _PATH_DECLARATION_QUALIFIER
    + r"\s*"
    + _PATH_DECLARATION_LINKER
    + r"\s*[\"'“‘`()\[{]*\s*"
    r"(?:~|/|\\|\.{1,2})(?=$|[\s,;!?\"'”’)\]}])",
    re.IGNORECASE,
)
_CONTEXTUAL_TERMINAL_PATH = re.compile(
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:(?:from|at|in|to|into|under)\s+)?[\"'“‘`()\[{]*\s*"
    r"(?:~|/|\\|\.{1,2})(?=$|[,;!?\"'”’)\]}])",
    re.IGNORECASE,
)
_API_ROUTE_CONTEXT = re.compile(
    r"\b(?:call|invoke|serve|expose|request|hit|get|head|post|put|patch|delete|connect|options|trace)\s+(?:the\s+)?"
    r"(?P<path>/(?:[A-Za-z0-9._~%-]+(?:/[A-Za-z0-9._~%-]+)*)(?:\?[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_HTTP_METHOD_ROUTE_CONTEXT = re.compile(
    r"\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+"
    r"(?P<path>/(?:[A-Za-z0-9._~%-]+(?:/[A-Za-z0-9._~%-]+)*)(?:\?[^\s<>\"']*)?)"
)
_HTTP_METHOD_ROUTE_ROOTS = frozenset({"applications", "config", "users"})
_COMMON_API_RESOURCE_COMPONENTS = frozenset({"cache", "project", "run"})
_HTTP_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)


def _fully_unquote(value: str) -> str:
    while True:
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded


def _has_url_userinfo(value: str) -> bool:
    for match in _HTTP_URL.finditer(value):
        try:
            parsed = urlsplit(match.group())
            if parsed.username is not None or parsed.password is not None:
                return True
        except ValueError:
            continue
    return False


def _has_filesystem_path_in_url_query(value: str) -> bool:
    for match in _HTTP_URL.finditer(value):
        try:
            parsed = urlsplit(match.group())
            decoded_path = _fully_unquote(parsed.path)
            if (
                _DANGEROUS_VALUE.search(decoded_path)
                or _ENVIRONMENT_ASSIGNMENT.search(decoded_path)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(decoded_path)
            ):
                return True
            fragment_query = parsed.fragment.partition("?")[2] or parsed.fragment
            query_pairs = parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(
                fragment_query, keep_blank_values=True
            )
        except ValueError:
            continue
        for key, query_value in query_pairs:
            key = _fully_unquote(key)
            query_value = _fully_unquote(query_value)
            if (
                _DANGEROUS_VALUE.search(f"{key}={query_value}")
                or _ENVIRONMENT_ASSIGNMENT.search(f"{key}={query_value}")
                or _ENVIRONMENT_ASSIGNMENT.search(query_value)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(query_value)
            ):
                return True
            decoded_value = _fully_unquote(query_value)
            path_value = decoded_value.rstrip(",;!?")
            if path_value.endswith(".") and path_value not in {".", ".."}:
                path_value = path_value.rstrip(".")
            if _is_filesystem_path_value(path_value):
                return True
    return False


def _has_disallowed_api_route_query(value: str) -> bool:
    routes = (*_API_ROUTE_CONTEXT.finditer(value), *_HTTP_METHOD_ROUTE_CONTEXT.finditer(value))
    for match in routes:
        route = match.group("path")
        route_path, _, route_query = route.partition("?")
        decoded_path = _fully_unquote(route_path)
        decoded_components = [
            component.casefold() for component in decoded_path.replace("\\", "/").split("/") if component
        ]
        if any(component in {".", ".."} for component in decoded_components) or any(
            component.startswith(".")
            or component == "~"
            or component in _PRIVATE_KEY_FILES
            or (
                index == 0
                and component in _FILESYSTEM_COMPONENTS - _HTTP_METHOD_ROUTE_ROOTS - _COMMON_API_RESOURCE_COMPONENTS
            )
            or component in {"credentials", "passwd", "secret", "secrets"}
            for index, component in enumerate(decoded_components)
        ):
            return True
        if (
            _DANGEROUS_VALUE.search(decoded_path)
            or _ENVIRONMENT_ASSIGNMENT.search(decoded_path)
            or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(decoded_path)
        ):
            return True
        for key, query_value in parse_qsl(route_query, keep_blank_values=True):
            key = _fully_unquote(key)
            decoded_value = _fully_unquote(query_value)
            pair = f"{key}={decoded_value}"
            if (
                _DANGEROUS_VALUE.search(pair)
                or _ENVIRONMENT_ASSIGNMENT.search(pair)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(pair)
            ):
                return True
            path_value = decoded_value.rstrip(",;!?")
            if path_value.endswith(".") and path_value not in {".", ".."}:
                path_value = path_value.rstrip(".")
            if _is_filesystem_path_value(path_value):
                return True
    return False


_RELATIVE_PATH = re.compile(r"(?<![\w.-])(?P<path>(?:[\w.~-]+[/\\])+[\w.~-]+)(?![\w.~-])")
# Slash pairs used as noun modifiers are prose when followed by a plural noun.
_SLASH_PROSE_CONTINUATION = re.compile(
    r"\s+(?!(?:for|before|after|with|where|when|while|from|at|in|on|under|to|into|through)\b)[A-Za-z][A-Za-z-]*\b"
)
_FILESYSTEM_NOUN_CONTINUATION = re.compile(
    r"\s+(?:[A-Za-z][A-Za-z-]*\s+){0,2}(?:files?|directories|folders?|scripts?|helpers?|data|content|output)\b", re.I
)
_PATH_CONTEXT = re.compile(
    r"(?:\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store|"
    + _PATH_LABEL
    + r")\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?(?:(?:from|at|in|to|into|under)\s+)?"
    r"[\"'“‘]*\s*(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?|" + _PATH_DECLARATION + r"\s*)[\"'“‘`()\[{]*\s*$",
    re.IGNORECASE,
)
_EXPLICIT_NAVIGATION_CONTEXT = re.compile(
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?(?:(?:from|at|in|to|into|under)\s+)?[\"'“‘]*\s*$",
    re.IGNORECASE,
)
_EXPLICIT_PATH_DECLARATION = re.compile(
    _PATH_DECLARATION + r"\s*$",
    re.IGNORECASE,
)
_QUOTED_PATH_DECLARATION_CONTEXT = re.compile(
    _PATH_DECLARATION + r"\s*[\"'“‘][^\"'“”‘’]*$",
    re.IGNORECASE,
)
_QUOTED_PATH_CONTEXT = re.compile(
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?(?:(?:from|at|in|to|into|under)\s+)?"
    r"[\"'“‘][^\"'“”‘’]*$",
    re.IGNORECASE,
)
_SPACED_PATH_CONTEXT = re.compile(
    r"(?:\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store)\s+"
    r"(?:(?:from|at|in|to|into|under)\s+)?|" + _PATH_DECLARATION + r"\s+)"
    r"(?:[A-Za-z0-9_.~-]+\s+)+$",
    re.IGNORECASE,
)
_FILESYSTEM_COMPONENTS = frozenset(
    {
        "automation",
        "applications",
        "bin",
        "boot",
        "cache",
        "checks",
        "config",
        "credentials",
        "dev",
        "docs",
        "etc",
        "home",
        "lib",
        "lib64",
        "library",
        "logs",
        "media",
        "mnt",
        "network",
        "opt",
        "passwd",
        "private",
        "proc",
        "root",
        "run",
        "scripts",
        "sbin",
        "secret",
        "secrets",
        "srv",
        "sys",
        "tests",
        "tmp",
        "usr",
        "var",
        "workspace",
        "project",
        "system",
        "users",
        "volumes",
        "cores",
        "src",
    }
)


def _without_api_route_references(value: str) -> str:
    def remove_route(match: re.Match[str]) -> str:
        route = match.group("path")
        components = route.split("?", maxsplit=1)[0].strip("/").split("/")
        first_component = components[0].casefold()
        if any(component in {".", ".."} for component in components):
            return match.group(0)
        if first_component in _FILESYSTEM_COMPONENTS:
            return match.group(0)
        return match.group(0).replace(route, "")

    text = _HTTP_URL.sub("", value)

    def remove_http_method_route(match: re.Match[str]) -> str:
        path = match.group("path")
        components = path.split("?", maxsplit=1)[0].strip("/").split("/")
        if components[0].casefold() in _HTTP_METHOD_ROUTE_ROOTS and not any(
            component in {".", ".."} for component in components
        ):
            return match.group(0).replace(path, "")
        return match.group(0)

    text = _HTTP_METHOD_ROUTE_CONTEXT.sub(remove_http_method_route, text)
    return _API_ROUTE_CONTEXT.sub(remove_route, text)


_EXTENSIONLESS_FILES = frozenset(
    {"containerfile", "dockerfile", "gnumakefile", "makefile", "justfile", "procfile", "readme"}
)
_PRIVATE_KEY_FILES = frozenset({"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"})
_PROSE_SLASH_TERMS = frozenset(
    {
        "24/7",
        "and/or",
        "ci/cd",
        "clear/concise",
        "client/server",
        "input/output",
        "key/value",
        "planner/reviewer",
        "read/write",
        "review/approval",
        "request/response",
        "source/target",
        "source/proprietary",
        "time/effort",
        "xml/json",
        "yes/no",
    }
)
_PROSE_DOTTED_TERMS = frozenset({"e.g", "i.e", "node.js", "vs.code"})
_VERSION_REFERENCE = re.compile(r"\b[A-Z][A-Za-z0-9.+-]*/\d+(?:\.\d+)+")
_FILE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_MODEL_PATH_SUFFIX = re.compile(
    r"\.(?:7z|avif|bmp|bz2|c|cfg|conf|cpp|cs|css|csv|docx|exe|gif|go|gz|h|hpp|html|ico|ini|java|jpe?g|js|json|jsx|lock|md|mov|mp3|mp4|pdf|png|pptx|py|rar|rst|rs|sh|so|sql|svg|tar|tiff?|toml|ts|tsx|txt|wav|webp|woff2|xml|xlsx|yaml|yml|zip)$",
    re.IGNORECASE,
)
_STANDALONE_FILE = re.compile(r"(?<![\w.-])(?P<file>\.[\w-]+|[\w.-]+)(?![\w.-])")
_SURROGATE = re.compile("[\\ud800-\\udfff]")


def _has_path_context(text: str, position: int) -> bool:
    prefix = text[:position]
    return any(
        pattern.search(prefix) is not None
        for pattern in (
            _PATH_CONTEXT,
            _QUOTED_PATH_CONTEXT,
            _SPACED_PATH_CONTEXT,
            _QUOTED_PATH_DECLARATION_CONTEXT,
        )
    )


def _is_filesystem_path_value(value: str) -> bool:
    return bool(
        _EXPLICIT_TERMINAL_PATH.search(f"path: {value}") or _ROOTED_PATH.search(value) or _RELATIVE_PATH.search(value)
    )


_TOP_FIELDS = frozenset({"schema", "revision", "seats"})
_SEAT_FIELDS = frozenset({"name", "cli", "model", "purpose", "caveats", "fallbacks", "enabled"})


class MasterRosterError(ValueError):
    """A master-roster document is malformed or contains forbidden material."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value)


def _has_relative_path_evidence(value: str, *, model_identifier: bool = False) -> bool:
    path_text = _VERSION_REFERENCE.sub("", _HTTP_URL.sub("", value))
    for match in _STANDALONE_FILE.finditer(path_text):
        filename = match.group("file").rstrip(".,;:!?")
        sensitive_name = filename.casefold() in _EXTENSIONLESS_FILES | _PRIVATE_KEY_FILES | {"credentials"}
        suffix = _MODEL_PATH_SUFFIX if model_identifier else _FILE_SUFFIX
        suffix_match = suffix.search(filename)
        trailing_text = path_text[match.end() :].strip(" .,;:!?")
        path_context = _has_path_context(path_text, match.start())
        path_declaration = any(
            pattern.search(path_text[: match.start()]) is not None
            for pattern in (_EXPLICIT_PATH_DECLARATION, _QUOTED_PATH_DECLARATION_CONTEXT)
        )
        file_like = filename.casefold() not in _PROSE_DOTTED_TERMS and (
            filename.startswith(".")
            or sensitive_name
            or path_declaration
            or (suffix_match and (path_context or not trailing_text or sensitive_name))
        )
        standalone = path_text.strip().rstrip(".,;:!?") == filename
        if file_like and (path_context or standalone):
            return True
    for match in _RELATIVE_PATH.finditer(path_text):
        components = re.split(r"[/\\]", match.group("path"))
        standalone = path_text.strip().rstrip(".,;:!?").casefold() == match.group("path").casefold()
        if standalone and not model_identifier and match.group("path").casefold() not in _PROSE_SLASH_TERMS:
            return True
        path_context = _has_path_context(path_text, match.start())
        explicit_navigation = _EXPLICIT_NAVIGATION_CONTEXT.search(path_text[: match.start()]) is not None
        continuation = path_text[match.end() :]
        prose_continuation = _SLASH_PROSE_CONTINUATION.match(continuation)
        filesystem_noun = _FILESYSTEM_NOUN_CONTINUATION.match(continuation)
        if (
            path_context
            and (not prose_continuation or filesystem_noun or explicit_navigation)
            and match.group("path").casefold() not in _PROSE_SLASH_TERMS
        ):
            return True
        if any(component.startswith(".") or component.casefold() in _FILESYSTEM_COMPONENTS for component in components):
            return True
        if (
            model_identifier
            and components[0].casefold() in {"app", "package", "web"}
            and _FILE_SUFFIX.search(components[-1])
        ):
            return True
        suffix = _MODEL_PATH_SUFFIX if model_identifier else _FILE_SUFFIX
        if components[-1].casefold() in _EXTENSIONLESS_FILES or suffix.search(components[-1]):
            return True
    return False


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MasterRosterError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: Any, field: str, *, check_paths: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MasterRosterError(f"{field} must be a non-empty string")
    if _SURROGATE.search(value):
        raise MasterRosterError(f"{field} must contain valid Unicode scalar values")
    if len(value) > MAX_TEXT or _has_control(value):
        raise MasterRosterError(f"{field} must be at most {MAX_TEXT} characters without controls or separators")
    screening_text = _QUOTED_PROTECTED_LABEL.sub(r"\g<label>", value)
    path_text = _without_api_route_references(screening_text)
    if (
        _DANGEROUS_VALUE.search(screening_text)
        or _URL_CREDENTIAL_QUERY.search(screening_text)
        or _has_url_userinfo(screening_text)
        or _has_filesystem_path_in_url_query(screening_text)
        or _has_disallowed_api_route_query(screening_text)
        or _ENVIRONMENT_ASSIGNMENT.search(path_text)
        or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(path_text)
        or contains_credential(screening_text)
        or (
            check_paths
            and (
                _EXPLICIT_TERMINAL_PATH.search(path_text)
                or _CONTEXTUAL_TERMINAL_PATH.search(path_text)
                or _ROOTED_PATH.search(path_text)
                or _has_relative_path_evidence(
                    path_text,
                    model_identifier=field.endswith(".model") and _MODEL_IDENTIFIER.fullmatch(value) is not None,
                )
            )
        )
    ):
        raise MasterRosterError(f"{field} contains a credential, environment assignment, or home path")
    return value


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise MasterRosterError(f"{where} keys must be strings")
    if any(_SURROGATE.search(key) for key in value):
        raise MasterRosterError(f"{where} keys must contain valid Unicode scalar values")
    if any(_has_control(key) for key in value):
        raise MasterRosterError(f"{where} keys must not contain controls or separators")
    unknown = set(value).difference(allowed)
    if unknown:
        raise MasterRosterError(f"unknown {where} field(s)")


def parse(raw: Mapping[str, Any] | bytes | str) -> dict[str, Any]:
    """Validate and copy a bounded v1 roster, returning its canonical SHA-256."""
    if isinstance(raw, (bytes, str)):
        try:
            encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise MasterRosterError(f"document must be at most {MAX_DOCUMENT_BYTES} bytes")
            decoded = json.loads(encoded.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys)
        except MasterRosterError:
            raise
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise MasterRosterError("document must be valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise MasterRosterError("document must be a JSON object")
        source: Mapping[str, Any] = decoded
    elif isinstance(raw, Mapping):
        source = raw
    else:
        raise MasterRosterError("document must be an object")

    _unknown_fields(source, _TOP_FIELDS, "document")
    if source.get("schema") != SCHEMA:
        raise MasterRosterError(f"schema must be {SCHEMA}")
    revision = source.get("revision")
    if type(revision) is not int or revision < 1:
        raise MasterRosterError("revision must be a positive integer")
    seats = source.get("seats")
    if not isinstance(seats, list) or len(seats) > MAX_SEATS:
        raise MasterRosterError(f"seats must be a list of at most {MAX_SEATS} entries")

    clean_seats: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_seat in enumerate(seats):
        where = f"seats[{index}]"
        if not isinstance(raw_seat, Mapping):
            raise MasterRosterError(f"{where} must be an object")
        _unknown_fields(raw_seat, _SEAT_FIELDS, where)
        name = _text(raw_seat.get("name"), f"{where}.name", check_paths=False)
        cli = _text(raw_seat.get("cli"), f"{where}.cli", check_paths=False)
        model = _text(raw_seat.get("model"), f"{where}.model")
        if _URL_SCHEME.match(model):
            raise MasterRosterError(f"{where}.model must be a provider/model identifier, not a URL")
        if not _MODEL_IDENTIFIER.fullmatch(model):
            raise MasterRosterError(f"{where}.model must be a model identifier")
        purpose = _text(raw_seat.get("purpose"), f"{where}.purpose")
        if not _SEAT_NAME.fullmatch(name):
            raise MasterRosterError(f"{where}.name is not a valid seat name")
        if not _CLI_NAME.fullmatch(cli):
            raise MasterRosterError(f"{where}.cli must be a single executable name")
        if name in names:
            raise MasterRosterError(f"duplicate seat name: {name}")
        names.add(name)
        caveats = raw_seat.get("caveats", [])
        fallbacks = raw_seat.get("fallbacks", [])
        if not isinstance(caveats, list) or len(caveats) > 16:
            raise MasterRosterError(f"{where}.caveats must be a list of at most 16 entries")
        if not isinstance(fallbacks, list) or len(fallbacks) > MAX_SEATS:
            raise MasterRosterError(f"{where}.fallbacks must be a list of seat names")
        if type(raw_seat.get("enabled")) is not bool:
            raise MasterRosterError(f"{where}.enabled must be a boolean")
        clean_seats.append(
            {
                "name": name,
                "cli": cli,
                "model": model,
                "purpose": purpose,
                "caveats": [_text(item, f"{where}.caveats[{i}]") for i, item in enumerate(caveats)],
                "fallbacks": [
                    _text(item, f"{where}.fallbacks[{i}]", check_paths=False) for i, item in enumerate(fallbacks)
                ],
                "enabled": raw_seat["enabled"],
            }
        )
    seats_by_name = {seat["name"]: seat for seat in clean_seats}
    for seat in clean_seats:
        for fallback in seat["fallbacks"]:
            if not _SEAT_NAME.fullmatch(fallback) or fallback not in names or fallback == seat["name"]:
                raise MasterRosterError(f"{seat['name']} has an invalid fallback seat: {fallback}")
            if seats_by_name[fallback]["fallbacks"]:
                raise MasterRosterError(f"{seat['name']} fallback target {fallback!r} must not define its own fallback")

    document = {"schema": SCHEMA, "revision": revision, "seats": clean_seats}
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (ValueError, RecursionError) as exc:
        raise MasterRosterError("document cannot be represented as bounded JSON") from exc
    if len(canonical.encode("ascii")) > MAX_DOCUMENT_BYTES:
        raise MasterRosterError(f"document must be at most {MAX_DOCUMENT_BYTES} bytes")
    result = copy.deepcopy(document)
    result["sha256"] = "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return result
