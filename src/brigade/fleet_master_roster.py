"""Safe, versioned master-roster document contract for fleet synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from .worklore_validate import contains_credential

SCHEMA = "brigade.fleet_master_roster.v1"
MAX_DOCUMENT_BYTES = 262144
MAX_SEATS = 128
MAX_TEXT = 512
_SEAT_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CLI_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ROSTER_SUBJECT = r"[^\W\d_][\w.-]*"
_EXPLANATORY_SECRET_VALUE = (
    r"(?:required|optional|needed|necessary|available|configured(?!\s+(?:as|to|with)\b)|set(?!\s+to\b)|missing|unset|unavailable|"
    r"not\s+(?:required|optional|needed|necessary|available|configured|set|missing|unset|unavailable)|"
    r"confidential|authentication|management|rotation|support|handling|managed(?:\s+by\s+Vault)?|provided|stored(?:\s+securely)?|encrypted|protected|"
    r"handled|generated|rotated|masked|redacted)(?!\s+(?:as|to|with)\b)(?=$|\s|[.,;!?](?:\s|$))"
)
_DANGEROUS_VALUE_PATTERN = (
    r"(?:(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]+|(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{12,}|(?<![A-Za-z0-9_])xapp-[A-Za-z0-9-]{12,}|(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}|(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"\b(?:Proxy-)?Authorization\s*:\s*Bearer\s+\S+|\bBearer\s+[A-Za-z0-9._~+/-]{20,}|"
    r"\b(?:Proxy-)?Authorization\s*:\s*Basic\s+"
    r"[A-Za-z0-9+/]{4,}={0,2}(?![A-Za-z0-9+/=])|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?(?:(?:(?:is\s+)?provided\s+(?:as|with)|is|are|was|were|equals|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|has\s+been|have\s+been|had\s+been|will\s+have\s+been|set\s+to|configured\s+(?:as|to|with))\s+|[:=]\s*)"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}|selection)\b)"
    r"(?:[^\s.,;!?]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s*[:=]\s*" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}|selection)\b)"
    r"(?:[^\s.,;!?]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:named|called)\s+"
    r"(?:[^\s.,;!?]+|\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\bauth(?:entication)?[ _-]profiles?\s+(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\))|"
    r"\b(?:(?:this|the|a|an|my|your|our|their|his|her|its)\s+)?"
    + _ROSTER_SUBJECT
    + r"(?:\s+(?:seat|service|agent))?\s+(?:uses?|used|is\s+using|are\s+using|was\s+using|were\s+using|(?:can|could|may|might|must|shall|should|will|would)\s+be\s+using|has|have|had)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?auth(?:entication)?[ _-]profiles?\s+"
    r"(?!(?:is|are|as|for|in|required|needed|necessary|optional|available|selection|rotation|management|regularly)\b)"
    r"(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|"
    r"\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\)|[^\s.,;!?]+)|"
    r"\b(?:use|select|choose|load|set|activate|switch\s+to|connect\s+(?:through|with|using))\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?auth(?:entication)?[ _-]profiles?\s+"
    r"(?!(?:is|are|as|for|in|required|needed|necessary|optional|available|selection|rotation|management|regularly)\b)"
    r"(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\)|[^\s.,;!?]+)|"
    r"\b(?:use|select|choose|load|set|activate|switch\s+to|connect\s+(?:through|with|using))\s+"
    r"(?:the\s+)?(?:\"[^\"\n]+\"|'[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*'|`[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*`|\[[A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\]|\([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*\)|(?!(?:the)\b)[^\s.,;!?]+)\s+"
    r"auth(?:entication)?[ _-]profiles?\b|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s*(?:->|=>|[:=])\s*"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)"
    r"\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?"
    r"(?:provided\s+(?:as|with)|stored\s+(?:as|with)|generated\s+(?:as|with)|configured\s+(?:as|with|to))\s+"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)"
    r"\s+(?:(?:for\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|of\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|assigned\s+to\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|used\s+by\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)|value)(?:\s+|(?=[:=])))?"
    r"(?:(?:is|are|was|were|equals|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|has\s+been|have\s+been|had\s+been|will\s+have\s+been|set\s+to)\s+|[:=]\s*)"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\buse\s+(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s+"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE})\b)\S+|"
    r"\b(?:use|set|configure(?:d)?|store(?:d)?|provide(?:d)?|generate(?:d)?)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s+"
    r"(?:to|as|with)\s+" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}))\S+|"
    r"\b(?:(?:this|the|a|an|my|your|our|their|his|her|its)\s+)?"
    + _ROSTER_SUBJECT
    + r"(?:\s+(?:seat|service|agent))?\s+(?:uses?|used|is\s+using|are\s+using|was\s+using|were\s+using|(?:can|could|may|might|must|shall|should|will|would)\s+be\s+using|has|have|had)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s+"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE})\b)\S+|"
    r"(?:^|[^A-Za-z0-9_.-])(?:[A-Za-z]:(?:[/\\]|[^\s])|%[A-Za-z_][A-Za-z0-9_]*%|"
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][^}]*\})"
    r"(?::[A-Za-z_][A-Za-z0-9_]*)?(?:[/\\][^\s]*)?))"
)
_DANGEROUS_VALUE = re.compile(
    _DANGEROUS_VALUE_PATTERN.replace(r"used\s+by", r"used\s+(?:by|on|for)").replace(
        r"has\s+been", r"has\s+(?:been|(?:(?:the|a)\s+)?value(?:\s+of)?)"
    ),
    re.IGNORECASE,
)
_ADVERB_QUALIFIED_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|"
    r"api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\s+"
    r"(?:(?:[A-Za-z]+ly|now|still|just|already|currently|presently|always|never|eventually|temporarily)\s+)*"
    r"(?:is|are|was|were|equals|remains|remain|has\s+been|have\s+been|had\s+been|"
    r"will\s+be|will\s+have\s+been)\s+" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE})\b)\S+",
    re.IGNORECASE,
)
_VALUE_FIRST_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:(?:use|set|configure|store|provide|supply)\s+)?" + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE})\b)"
    r"[^\s.,;!?]+\s+(?:(?:is|was|equals|as|for)\s+)"
    r"(?:(?:the|a|an)\s+)?"
    r"(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|"
    r"api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?)\b",
    re.IGNORECASE,
)
_SENSITIVE_CLI_ARGUMENT = re.compile(
    r"(?<![\w-])--(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:=|\s+)(?P<value>\S+)",
    re.IGNORECASE,
)
_SENSITIVE_CLI_ARGUMENT_SUFFIXES = frozenset(
    {
        ("password",),
        ("passwords",),
        ("passwd",),
        ("passwds",),
        ("passphrase",),
        ("passphrases",),
        ("secret",),
        ("secrets",),
        ("credential",),
        ("credentials",),
        ("token",),
        ("tokens",),
        ("api", "key"),
        ("api", "keys"),
        ("auth", "token"),
        ("auth", "tokens"),
        ("access", "token"),
        ("access", "tokens"),
        ("client", "secret"),
        ("client", "secrets"),
        ("private", "key"),
        ("private", "keys"),
    }
)
_CLI_ARGUMENT_PROSE_VALUES = frozenset(
    {"is", "are", "was", "were", "equals", "remains", "remain", "option", "argument", "flag", "parameter", "setting"}
)
_COMPOUND_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>[A-Za-z][A-Za-z0-9_.\[\]-]*)\s*=(?!=|>)\s*(?P<value>[^\s&,;]+)"
)
_COMPOUND_NAMED_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>[A-Za-z][A-Za-z0-9_.\[\]-]*)\s*(?::(?!:)\s*|"
    r"(?:is|are|was|were|equals|remains|remain|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|"
    r"has\s+(?:(?:the|a)\s+)?value(?:\s+of)?|set\s+to|configured\s+(?:as|to|with))\s+)"
    r"(?P<value>[^\s&,;]+)",
    re.IGNORECASE,
)
_PATH_LABEL = r"(?:paths?|files?(?:\s+names?)?|filenames?|director(?:y|ies)|folders?|scripts?|repositor(?:y|ies)|repos?|locations?)"
_VALUE_FIRST_PATH_DECLARATION = re.compile(
    r"\b(?:(?:set|choose|select|use|configure|store|save|copy|move)\s+)?"
    r"(?P<path>(?:[\w.~-]+[/\\])+[\w.~-]+)\s+"
    r"(?:(?:is|are|was|were|equals|means)\s+(?:(?:the|a|an)\s+)?|"
    r"(?:as|for)\s+(?:(?:the|a|an)\s+)?)" + _PATH_LABEL + r"\b",
    re.IGNORECASE,
)
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
_MAPPING_ENVIRONMENT_ASSIGNMENT = re.compile(
    r"\b(?:(?:os\s*\.\s*)?environ|env)\s*\[\s*(['\"`])[A-Za-z_][A-Za-z0-9_]*\1\s*\]"
    r"\s*=(?!=|>)\s*\S+",
    re.IGNORECASE,
)
_ROSTER_OWNERSHIP_QUALIFIER = (
    r"(?:\s+(?:for|of|on|used\s+(?:by|on|for)|assigned\s+to)\s+(?:(?:this|the)\s+)?"
    r"(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?))?"
)
_ON_OWNED_PROTECTED_ASSIGNMENT = re.compile(
    r"\b(?:passwords?|passwds?|passphrases?|secrets?|credentials?|tokens?|auth[_ -]?tokens?|"
    r"api[_ -]?keys?|access[_ -]?tokens?|client[_ -]?secrets?|private[_ -]?keys?|"
    r"auth(?:entication)?[ _-]?profiles?)\s+on\s+"
    r"(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|"
    r"service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|"
    r"[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)\s+"
    r"(?:(?:is|are|was|were|equals|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|"
    r"has\s+been|have\s+been|had\s+been|will\s+have\s+been|set\s+to|configured\s+(?:as|to|with))\s+|[:=]\s*)"
    + rf"(?!(?:{_EXPLANATORY_SECRET_VALUE}|selection)\b)\S+",
    re.IGNORECASE,
)
_BARE_AUTH_PROFILE_NAME = re.compile(
    r"\bauth(?:entication)?[ _-]profiles?\s+"
    r"(?!(?:this|the|a|an|is|are|was|were|has|have|had|will|can|could|should|must|may|might|require|requires|"
    r"selection|rotation|management|support|configuration|policy|policies|availability|required|needed|names|values|"
    r"necessary|optional|available|regularly|for|in|as|to|with)\b)"
    r"(?:\"[^\"\n]+\"|'[^'\n]+'|`[^`\n]+`|[^\s.,;!?]+)",
    re.IGNORECASE,
)
_SUBJECT_FIRST_AUTH_PROFILE = re.compile(
    r"\bauth(?:entication)?[ _-]profiles?\s+"
    r"(?!(?:this|the|a|an|selection|rotation|management|support|configuration|policy|availability)\b)"
    r"[^\s.,;!?]+\s+(?:is|are|was|were|has\s+been|have\s+been|had\s+been|will\s+be|"
    r"remains|became)\s+(?:active|enabled|selected|configured|available|in\s+use)\b",
    re.IGNORECASE,
)
_EXPLICIT_ENVIRONMENT_ASSIGNMENT = re.compile(
    r"\b(?:"
    r"(?:export\s+|env\s+|set\s+(?:environment\s+variable|env)\s+|"
    r"(?:(?:use|configure)\s+(?:the\s+)?)?environment\s+variable\s+)"
    r"(?:(?:named|called)\s+)?[\"'“‘`]?[A-Za-z_][A-Za-z0-9_]*[\"'”’`]?|"
    r"(?:the\s+)?[A-Za-z_][A-Za-z0-9_]*\s+environment\s+variable)"
    + _ROSTER_OWNERSHIP_QUALIFIER
    + r"(?:\s*=(?!=|>)|\s+(?:(?:[A-Za-z]+ly|now|still|just|already|currently|presently|always|never|eventually|temporarily)\s+)*(?:value\s+)?(?:set\s+to|configured\s+(?:as|to|with)|to|as|is|are|was|were|equals|has\s+(?:(?:the|a)\s+)?value(?:\s+of)?|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|has\s+been|have\s+been|had\s+been|will\s+have\s+been)\s+(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+|\s+(?:value\s*)?(?:=(?!=|>)|:)\s*(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+|:\s*(?!(?:"
    + _EXPLANATORY_SECRET_VALUE
    + r"))\S+)",
    re.IGNORECASE,
)
_PATH_DECLARATION_LINKER = r"(?:(?:[A-Za-z]+ly|now|still|just|already|currently|presently|always|never|eventually|temporarily)\s+)*(?:is|are|was|were|equals|(?:can|could|may|might|must|shall|should|will|would)(?:\s+be)?|has\s+(?:(?:the|a)\s+)?value(?:\s+of)?|has\s+been|have\s+been|had\s+been|will\s+have\s+been|set\s+to|is\s+set\s+to|was\s+set\s+to|configured\s+(?:as|to)|:|=)"
_PATH_DECLARATION_QUALIFIER = r"(?:\s+(?:(?:for|of|on|used\s+(?:by|on|for)|assigned\s+to)\s+(?:(?:this|the)\s+)?(?:seat(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|service(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|agent(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?|[A-Za-z][A-Za-z0-9_.-]*(?:\s+(?:seat|service|agent))?)))?"
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
    r"(?:~[A-Za-z0-9_.-]*|/|\\|\.{1,2})(?=$|[\s,;!?\"'”’)\]}])",
    re.IGNORECASE,
)
_CONTEXTUAL_TERMINAL_PATH = re.compile(
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?"
    r"(?:(?:from|at|in|to|into|under)\s+)?[\"'“‘`()\[{]*\s*"
    r"(?:~[A-Za-z0-9_.-]*|/|\\|\.{1,2})(?=$|[,;!?\"'”’)\]}])",
    re.IGNORECASE,
)
_API_ROUTE_CONTEXT = re.compile(
    r"\b(?:call|invoke|serve|expose|request|hit|get|head|post|put|patch|delete|connect|options|trace)\s+(?:the\s+)?"
    r"(?P<path>/(?:[A-Za-z0-9._~%=-]+(?:/[A-Za-z0-9._~%=-]+)*)(?:\?[^\s<>\"']*)?(?:#[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_HTTP_METHOD_ROUTE_CONTEXT = re.compile(
    r"\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+"
    r"(?P<path>/(?:[A-Za-z0-9._~%=-]+(?:/[A-Za-z0-9._~%=-]+)*)(?:\?[^\s<>\"']*)?(?:#[^\s<>\"']*)?)"
)
_HTTP_METHOD_ROUTE_ROOTS = frozenset({"applications", "config", "users"})
_FILESYSTEM_ROUTE_ROOTS = frozenset(
    {
        "applications",
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "opt",
        "private",
        "proc",
        "root",
        "sbin",
        "srv",
        "sys",
        "system",
        "tmp",
        "usr",
        "var",
        "volumes",
    }
)
_HTTP_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)


def _fully_unquote(value: str) -> str:
    while True:
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded


def _fully_unquote_plus(value: str) -> str:
    while True:
        decoded = unquote_plus(value)
        if decoded == value:
            return value
        value = decoded


def _normalize_protected_labels(value: str) -> str:
    value = re.sub(r"“([^”\n]+)”", r'"\1"', value)
    value = re.sub(r"‘([^’\n]+)’", r"'\1'", value)
    return _QUOTED_PROTECTED_LABEL.sub(r"\g<label>", value)


_OWNERSHIP_TARGET_WORD = (
    r"(?!(?:this|the|seat|service|agent|is|are|was|were|equals|value|set|configured)\b)"
    r"[^\W\d_][\w.-]*"
)
_OWNERSHIP_TARGET = re.compile(
    r"(?P<qualifier>\b(?:for|of|on|used\s+(?:by|on|for)|assigned\s+to)\s+)"
    r"(?P<determiner>(?:(?:this|the)\s+)?)"
    r"(?:(?P<prefix_role>seat|service|agent)\s+)?"
    + rf"(?P<subject>{_OWNERSHIP_TARGET_WORD}(?:\s+{_OWNERSHIP_TARGET_WORD}){{0,2}})"
    r"(?P<suffix_role>\s+(?:seat|service|agent))?",
    re.IGNORECASE,
)


def _normalize_ownership_targets(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        subject = match.group("subject")
        if subject.isascii() and len(subject.split()) == 1:
            return match.group()
        return (
            match.group("qualifier")
            + match.group("determiner")
            + ((match.group("prefix_role") + " ") if match.group("prefix_role") else "")
            + "rosterowner"
            + (match.group("suffix_role") or "")
        )

    return _OWNERSHIP_TARGET.sub(replace, value)


def _has_sensitive_assignment_key(key: str, value: str, *, include_environment: bool = True) -> bool:
    """Check each query-key component and suffix as a possible assignment label."""
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", key)
    components = [component for component in re.split(r"[.\[\]_-]+", key) if component]
    candidates = {
        separator.join(components[index:]) for index in range(len(components)) for separator in ("_", "-", " ")
    }
    return any(
        _DANGEROUS_VALUE.search(f"{candidate}={value}")
        or (
            include_environment
            and (
                _ENVIRONMENT_ASSIGNMENT.search(f"{candidate}={value}")
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(f"{candidate}={value}")
            )
        )
        for candidate in candidates
    )


def _has_compound_sensitive_assignment(value: str) -> bool:
    return any(
        not re.fullmatch(_EXPLANATORY_SECRET_VALUE, match.group("value"), re.IGNORECASE)
        and _has_sensitive_assignment_key(match.group("key"), match.group("value"), include_environment=False)
        for pattern in (_COMPOUND_ASSIGNMENT, _COMPOUND_NAMED_ASSIGNMENT)
        for match in pattern.finditer(value)
    )


def _has_sensitive_cli_argument(value: str) -> bool:
    """Reject command-line options whose compound names disclose a protected value."""
    for match in _SENSITIVE_CLI_ARGUMENT.finditer(value):
        name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", match.group("name")).casefold()
        components = tuple(component for component in re.split(r"[-_]+", name) if component)
        if not any(components[-len(suffix) :] == suffix for suffix in _SENSITIVE_CLI_ARGUMENT_SUFFIXES):
            continue
        argument = match.group("value")
        if argument.casefold() in _CLI_ARGUMENT_PROSE_VALUES or re.fullmatch(
            _EXPLANATORY_SECRET_VALUE, argument, re.IGNORECASE
        ):
            continue
        return True
    return False


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
            decoded_path, _, embedded_query = decoded_path.partition("?")
            matrix_path_values = re.findall(
                rf"(?:^|;)(?:{_PATH_LABEL})(?:\[[^;\]]*\]|\.[^;=]+)*=([^;]*)",
                decoded_path,
                re.IGNORECASE,
            )
            if (
                _DANGEROUS_VALUE.search(decoded_path)
                or _ENVIRONMENT_ASSIGNMENT.search(decoded_path)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(decoded_path)
                or "//" in decoded_path.replace("\\", "/")
                or any(_is_filesystem_path_value(_fully_unquote(path_value)) for path_value in matrix_path_values)
                or _has_disallowed_api_route_query(f"GET {decoded_path}")
                or any(
                    _has_disallowed_api_route_query(f"Call {decoded_path[index:]}")
                    for index, character in enumerate(decoded_path)
                    if character in "/\\" and index > 0
                )
            ):
                return True
            decoded_query = _fully_unquote(parsed.query)
            decoded_fragment = _fully_unquote(parsed.fragment)
            query_pairs = (
                parse_qsl(embedded_query, keep_blank_values=True)
                + parse_qsl(decoded_query, keep_blank_values=True)
                + _query_shaped_fragment_pairs(decoded_fragment)
            )
        except ValueError:
            continue
        for key, query_value in query_pairs:
            key = _fully_unquote(key)
            query_value = _fully_unquote(query_value)
            if (
                _DANGEROUS_VALUE.search(f"{key}={query_value}")
                or _has_sensitive_assignment_key(key, query_value)
                or _ENVIRONMENT_ASSIGNMENT.search(f"{key}={query_value}")
                or _ENVIRONMENT_ASSIGNMENT.search(query_value)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(query_value)
            ):
                return True
            decoded_value = _fully_unquote(query_value)
            path_key = key.rstrip(",;!?")
            if path_key.endswith(".") and path_key not in {".", ".."}:
                path_key = path_key.rstrip(".")
            path_value = decoded_value.rstrip(",;!?")
            if path_value.endswith(".") and path_value not in {".", ".."}:
                path_value = path_value.rstrip(".")
            if _is_filesystem_path_value(path_key) or _is_filesystem_path_value(path_value):
                return True
    return False


def _has_disallowed_api_route_query(value: str) -> bool:
    routes = (*_API_ROUTE_CONTEXT.finditer(value), *_HTTP_METHOD_ROUTE_CONTEXT.finditer(value))
    for match in routes:
        method_route = re.match(r"(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+", match.group(), re.I)
        route = match.group("path")
        decoded_route = _fully_unquote(route)
        route_without_fragment, _, route_fragment = decoded_route.partition("#")
        decoded_path, _, route_query = route_without_fragment.partition("?")
        normalized_path = decoded_path.replace("\\", "/")
        if "//" in normalized_path:
            return True
        route_components = [component.casefold() for component in normalized_path.split("/") if component]
        if any(component in {".", ".."} for component in route_components):
            return True
        application_resource_route = route_components[:1] == ["applications"] and len(route_components) == 2
        decoded_components = [component.rstrip(".,;!?") for component in route_components]
        if any(
            (component.startswith(".") and component != ".well-known")
            or component.startswith("~")
            or component in _PRIVATE_KEY_FILES
            or (
                index == 0 and component in _FILESYSTEM_ROUTE_ROOTS and not (method_route or application_resource_route)
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
        route_pairs = parse_qsl(route_query, keep_blank_values=True) + _query_shaped_fragment_pairs(route_fragment)
        for key, query_value in route_pairs:
            key = _fully_unquote(key)
            decoded_value = _fully_unquote(query_value)
            pair = f"{key}={decoded_value}"
            if (
                _DANGEROUS_VALUE.search(pair)
                or _has_sensitive_assignment_key(key, decoded_value)
                or _ENVIRONMENT_ASSIGNMENT.search(pair)
                or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(pair)
            ):
                return True
            path_value = decoded_value.rstrip(",;!?")
            if path_value.endswith(".") and path_value not in {".", ".."}:
                path_value = path_value.rstrip(".")
            if _is_filesystem_path_value(key) or _is_filesystem_path_value(path_value):
                return True
    return False


def _query_shaped_fragment_pairs(fragment: str) -> list[tuple[str, str]]:
    if "?" in fragment:
        query = fragment.partition("?")[2]
    elif "=" in fragment:
        query = fragment
    else:
        return []
    return parse_qsl(query, keep_blank_values=True)


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
    r"delete|remove|edit|update|modify|rename|create|touch|" + _PATH_LABEL + r")\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?(?:(?:from|at|in|to|into|under)\s+)?"
    r"[\"'“‘]*\s*(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?|" + _PATH_DECLARATION + r"\s*)[\"'“‘`()\[{]*\s*$",
    re.IGNORECASE,
)
_EXPLICIT_NAVIGATION_CONTEXT = re.compile(
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store|"
    r"delete|remove|edit|update|modify|rename|create|touch)\s+"
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
    r"\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store|"
    r"delete|remove|edit|update|modify|rename|create|touch)\s+"
    r"(?:(?:the|a|an|my|your|our|their|his|her|its)\s+)?(?:(?:from|at|in|to|into|under)\s+)?"
    r"[\"'“‘][^\"'“”‘’]*$",
    re.IGNORECASE,
)
_SPACED_PATH_CONTEXT = re.compile(
    r"(?:\b(?:cd|change\s+directory|start|operate|move|run|work|execute|inspect|open|read|write|load|save|navigate|copy|store|"
    r"delete|remove|edit|update|modify|rename|create|touch)\s+"
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
        application_resource_route = first_component == "applications" and len(components) == 2
        if first_component in _FILESYSTEM_ROUTE_ROOTS and not application_resource_route:
            return match.group(0)
        return match.group(0).replace(route, "")

    def remove_valid_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group())
        except ValueError:
            return match.group()
        if not parsed.netloc or not parsed.hostname:
            return match.group()
        return ""

    text = _HTTP_URL.sub(remove_valid_url, value)

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


def _decode_explicit_path_declarations(value: str) -> str:
    decoded = value
    for match in reversed(tuple(re.finditer(_PATH_DECLARATION, value, re.IGNORECASE))):
        decoded = decoded[: match.end()] + _fully_unquote(decoded[match.end() :])
    return decoded


def _decode_contextual_path_values(value: str) -> str:
    decoded = value
    for match in reversed(tuple(re.finditer(r"%[0-9a-fA-F]{2}[^\s<>\"'“”‘’)\]},;!?]*", value))):
        if _has_path_context(value, match.start()):
            decoded = decoded[: match.start()] + _fully_unquote(match.group()) + decoded[match.end() :]
    return decoded


_TOP_FIELDS = frozenset({"schema", "revision", "seats"})
_SEAT_FIELDS = frozenset({"name", "cli", "model", "purpose", "caveats", "fallbacks", "enabled"})


class MasterRosterError(ValueError):
    """A master-roster document is malformed or contains forbidden material."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value)


def _has_relative_path_evidence(value: str, *, model_identifier: bool = False) -> bool:
    path_text = _VERSION_REFERENCE.sub("", _HTTP_URL.sub("", value))
    if _VALUE_FIRST_PATH_DECLARATION.search(path_text):
        return True
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
    screening_text = _normalize_ownership_targets(_normalize_protected_labels(value))
    decoded_sensitive_text = _normalize_ownership_targets(_normalize_protected_labels(_fully_unquote(screening_text)))
    decoded_form_text = _normalize_ownership_targets(_normalize_protected_labels(_fully_unquote_plus(screening_text)))
    path_text = _without_api_route_references(screening_text)
    decoded_sensitive_path_text = _without_api_route_references(decoded_sensitive_text)
    decoded_form_path_text = _without_api_route_references(decoded_form_text)
    decoded_paths = _decode_contextual_path_values(_decode_explicit_path_declarations(screening_text))
    decoded_path_text = _without_api_route_references(decoded_paths)
    path_texts = (decoded_path_text, decoded_sensitive_path_text, decoded_form_path_text)
    if (
        _DANGEROUS_VALUE.search(decoded_sensitive_text)
        or _DANGEROUS_VALUE.search(decoded_form_text)
        or _has_sensitive_cli_argument(decoded_sensitive_text)
        or _has_sensitive_cli_argument(decoded_form_text)
        or _VALUE_FIRST_CREDENTIAL_ASSIGNMENT.search(decoded_sensitive_text)
        or _VALUE_FIRST_CREDENTIAL_ASSIGNMENT.search(decoded_form_text)
        or _ADVERB_QUALIFIED_CREDENTIAL_ASSIGNMENT.search(decoded_sensitive_text)
        or _ADVERB_QUALIFIED_CREDENTIAL_ASSIGNMENT.search(decoded_form_text)
        or _has_compound_sensitive_assignment(decoded_sensitive_text)
        or _has_compound_sensitive_assignment(decoded_form_text)
        or _ON_OWNED_PROTECTED_ASSIGNMENT.search(decoded_sensitive_text)
        or _ON_OWNED_PROTECTED_ASSIGNMENT.search(decoded_form_text)
        or _BARE_AUTH_PROFILE_NAME.search(decoded_sensitive_text)
        or _BARE_AUTH_PROFILE_NAME.search(decoded_form_text)
        or _SUBJECT_FIRST_AUTH_PROFILE.search(decoded_sensitive_text)
        or _SUBJECT_FIRST_AUTH_PROFILE.search(decoded_form_text)
        or _URL_CREDENTIAL_QUERY.search(decoded_sensitive_text)
        or _URL_CREDENTIAL_QUERY.search(decoded_form_text)
        or _has_url_userinfo(decoded_sensitive_text)
        or _has_url_userinfo(decoded_form_text)
        or _has_filesystem_path_in_url_query(decoded_sensitive_text)
        or _has_filesystem_path_in_url_query(decoded_form_text)
        or _has_disallowed_api_route_query(screening_text)
        or _has_disallowed_api_route_query(decoded_sensitive_text)
        or _has_disallowed_api_route_query(decoded_form_text)
        or _ENVIRONMENT_ASSIGNMENT.search(path_text)
        or _ENVIRONMENT_ASSIGNMENT.search(decoded_sensitive_path_text)
        or _ENVIRONMENT_ASSIGNMENT.search(decoded_form_path_text)
        or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(path_text)
        or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(decoded_sensitive_path_text)
        or _EXPLICIT_ENVIRONMENT_ASSIGNMENT.search(decoded_form_path_text)
        or _MAPPING_ENVIRONMENT_ASSIGNMENT.search(decoded_sensitive_text)
        or _MAPPING_ENVIRONMENT_ASSIGNMENT.search(decoded_form_text)
        or contains_credential(decoded_sensitive_text)
        or contains_credential(decoded_form_text)
        or (
            check_paths
            and any(
                _EXPLICIT_TERMINAL_PATH.search(candidate)
                or _CONTEXTUAL_TERMINAL_PATH.search(candidate)
                or _ROOTED_PATH.search(candidate)
                or _has_relative_path_evidence(
                    candidate,
                    model_identifier=field.endswith(".model") and _MODEL_IDENTIFIER.fullmatch(value) is not None,
                )
                for candidate in path_texts
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
        if len(set(seat["fallbacks"])) != len(seat["fallbacks"]):
            raise MasterRosterError(f"{seat['name']} has duplicate fallback seats")
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
