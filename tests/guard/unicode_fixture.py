"""The guard's Unicode fixture: every default rule, with Unicode text at its edges.

The native engine's UTF mode is compared with Python `re` on this text, span
for span (`test_native_utf.py`). Each line carries non-ASCII characters next
to a rule's boundaries, classes or literals. Lines marked `diverges` show a
listed reason in `brigade.guard.utf_parity`, so a stale listing fails.

Trigger tokens are split into fragments so this tracked file holds no
contiguous match for the content-guard job. All values are fake: the 012 area
code cannot exist, 555 is the guard's own example prefix, and the names are
not accounts. Every character is assigned in Unicode 3.2, so the Unicode
database version of either engine cannot change the result.
"""

from __future__ import annotations

_LH = "local" + "host"
_HOME = "/ho" + "me"
_ROOT = "/ro" + "ot"
_WIN = "C:\\" + "Users"
_HOST = "build.inter" + "nal"
_IP10 = "10." + "0.0.5"
_IP192 = "192." + "168.0.7"
_LOOP = "127." + "0.0.1"
_PHONE = "012-345" + "-6789"
_JWT = "ey" + "JhbGciOiJI." + "eyJzdWIiOiJ4." + "c2lnbmF0dXJl"
_BEARER = "Bear" + "er "
_TOKEN = "abcdefghij" + "0123456789xyz"
_PAT = "gh" + "p_"
_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_END = "-----END " + "PRIVATE KEY-----"
_COAUTHOR = "Co-author" + "ed-by:"
_AT = "@"

# Characters with a known engine difference (see brigade.guard.utf_parity).
MVS = "\u180e"  # PCRE2 \s, not Python \s
DOTTED_I = "\u0130"  # Python caseless i, not PCRE2
DOTLESS_I = "\u0131"  # Python caseless i, not PCRE2
MARK = "\u0301"  # PCRE2 10.43+ \w, not Python \w
CONNECTOR = "\u203f"  # PCRE2 10.43+ \w, not Python \w

UNICODE_FIXTURE_LINES = (
    "guard unicode fixture: synthetic values only, ünïcödé throughout",
    # example-phone-555: ideographic space and NBSP are \s in both engines
    "desk +1\u3000555\u00a0010 0199 ext; é555-010-0199 stays; 555-010-0199é stays",
    # example-phone-555 diverges: U+180E as the separator
    "desk 555" + MVS + "010" + MVS + "0199",
    # example-email-reserved: an ASCII local part after a letter, and Kelvin/long s folding
    "mail zoë.k" + _AT + "example.org, \u212aim" + _AT + "te\u017ft and ünï" + _AT + "x.invalid",
    # example-email-reserved diverges: dotless and dotted I fold to i in Python only
    "mail " + DOTLESS_I + "van" + _AT + "example.org or ana" + _AT + "x." + DOTTED_I + "nvalid",
    # ssh-private-target: Unicode word characters in the user, Arabic-Indic digits in the address
    "ssh zoë" + _AT + _IP10 + ":/srv/data or λ" + _AT + "10.\u0660.\u0660.\u0665 today",
    # ssh-private-target diverges: \S stops at U+180E in PCRE2 only
    "ssh dev" + _AT + _IP10 + ":/srv" + MVS + "data",
    # private-ipv4: Unicode digits after the literal prefix, Unicode letters as boundaries
    "net " + _IP192 + " and 10.\u0661.\u0662.\u0663; not é" + _IP10 + " nor " + _IP10 + "λ; « " + _IP10 + " »",
    # loopback-ipv4: CJK letters as boundaries, fullwidth digits as \d
    "loop 中 " + _LOOP + " 中, not 中" + _LOOP + ", and 127.\uff10.\uff10.\uff11",
    # localhost-port: long s folds to s, Devanagari digits are \d, a letter blocks \b
    "svc " + _LH + ":\u0968\u0966\u0968\u0966 and LOCALHO\u017fT:8080 not " + _LH + ":8080é",
    # localhost-bare: guillemets bound it, letters before or after do not
    "bare «" + _LH + "» and LOCALHO\u017fT; not é" + _LH + " nor " + _LH + "λ",
    # port-reference: ideographic space, Thai digits
    "use port\u30008080 or PORT \u0e58\u0e50\u0e58\u0e50",
    # port-reference diverges: U+180E as the separator
    "use port" + MVS + "8080",
    # coauthored-by-trailer: Unicode names and NEL inside the trailer line
    _COAUTHOR + " Zoë Ünïcödé <z" + _AT + "x.invalid>\u0085tail",
    # coauthored-by-trailer diverges: Python \s* stops at U+180E, PCRE2 crosses the newline
    _COAUTHOR + " " + MVS,
    "next line",
    # windows-home-path: long s in Users, Unicode text after the path
    "open " + _WIN + "\\example-user\\notes.txt ünï and C:\\U\u017fers\\demo",
    # windows-home-path diverges: dotless I in the user segment
    "open " + _WIN + "\\" + DOTLESS_I + "van\\notes.txt",
    # home-path: stops at the first non-ASCII character, not after a letter
    "notes " + _HOME + "/zoë/notes and " + _HOME + "/ana/ü.txt; not 中" + _HOME + "/ana",
    # root-path: Unicode in a component ends the match; quotes still bound spaces
    "edit « " + _ROOT + "/cfg/ü.txt » and '" + _ROOT + "/My Files/ä b.txt' not λ" + _ROOT + "/x",
    # email: ASCII parts beside Unicode letters
    "reach zoë" + _AT + "corp.example, ana" + _AT + "corp.exampleé and \u212a" + _AT + "corp.example",
    # email diverges: dotless I in the local part, and (?!:\S) after the domain sees U+180E
    "reach " + DOTLESS_I + "van" + _AT + "corp.example or ana" + _AT + "corp.example:" + MVS,
    # us-phone: Arabic-Indic and Bengali digits, ideographic space
    "dial \u0660\u0661\u0662-\u0663\u0664\u0665-\u0666\u0667\u0668\u0669 or (012)\u3000345-6789 or \u09e6\u09e7\u09e8.345.6789",
    # us-phone diverges: the cue word reaches past U+180E only in PCRE2
    "tel:" + MVS + _PHONE,
    # jwt-token: guillemets bound it, a letter before it removes \b
    "jwt «" + _JWT + "» but not é" + _JWT,
    # bearer-token: ideographic space, long s in the keyword is not a fold of e
    "auth " + _BEARER + _TOKEN + "\u00e9 and BEARER\u3000" + _TOKEN,
    # bearer-token diverges: U+180E as the separator, dotless I in the token
    "auth Bearer" + MVS + _TOKEN + " and " + _BEARER + "abc" + DOTLESS_I + "defghij0123456789xyz",
    # api-key-assignment: Unicode letters before the keyword and after the value
    "cfg é api" + "_key = '" + _TOKEN[:20] + "' ü and TOK" + "EN=" + _TOKEN + "λ",
    # api-key-assignment diverges: dotless I in the value
    "cfg secret = '" + DOTLESS_I * 20 + "'",
    # bare-token-prefix: letters bound it, case-sensitive families keep ASCII
    "key " + _PAT + "a" * 24 + " and é" + _PAT + "a" * 24 + " and GHP_" + "B" * 24 + "λ",
    # bare-token-prefix diverges: dotless I in a caseless body
    "key " + _PAT + DOTLESS_I * 24,
    # private-key-block: Unicode body, spaces and newlines inside
    _BEGIN + "\nünïcödé ✓ 中文\u3000\u180e\n" + _END,
    # private-hostname: guillemets bound it, letters before it do not
    "host «" + _HOST + "» and BUILD.INTER" + "NAL; not é" + _HOST + " nor " + _HOST + "λ",
    # private-hostname diverges: dotless I in the suffix
    "host build." + DOTLESS_I + "nternal",
    # PCRE2 10.43+ only: a combining mark or connector beside each word-class boundary
    "marks e"
    + MARK
    + _IP10
    + " e"
    + MARK
    + _LOOP
    + " e"
    + MARK
    + _LH
    + ":80 e"
    + MARK
    + _LH
    + " e"
    + MARK
    + _HOME
    + "/ana",
    "marks "
    + CONNECTOR
    + _ROOT
    + "/cfg e"
    + MARK
    + _JWT
    + " e"
    + MARK
    + "555-010-0199 e"
    + MARK
    + "zoë.k"
    + _AT
    + "example.org",
    "marks e"
    + MARK
    + "ana"
    + _AT
    + _IP10
    + " e"
    + MARK
    + "po"
    + "rt 8080 "
    + _WIN
    + "\\ana"
    + MARK
    + "x e"
    + MARK
    + "ana"
    + _AT
    + "corp.example",
    "marks e" + MARK + _PHONE + " e" + MARK + _BEARER + _TOKEN + " e" + MARK + "tok" + "en=" + _TOKEN,
    "marks e" + MARK + _PAT + "a" * 24 + " e" + MARK + _HOST,
)
UNICODE_FIXTURE = "\n".join(UNICODE_FIXTURE_LINES) + "\n"
