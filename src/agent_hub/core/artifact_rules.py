"""Rules for file artifacts (design M4 sections 9.2 to 9.6). Pure: no I/O.

Media types come from the bytes, not from the client. The secret scan is a safety net, not a guarantee.
"""

import fnmatch
import hashlib
import json
import re
import unicodedata

import yaml

MAX_NAME = 120

# Magic numbers of the binary types the allowlist can contain.
MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "text/x-diff", "application/json", "application/x-yaml")
EXTENSION_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
                   ".diff": "text/x-diff", ".patch": "text/x-diff", ".json": "application/json",
                   ".yaml": "application/x-yaml", ".yml": "application/x-yaml"}

FORBIDDEN_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "credentials*", "*.kdbx")

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}")),
    ("xai-key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer-token", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    # NAME_TOKEN=value, API_KEY=value, ..._SECRET=value, PASSWORD=value. A placeholder such as <token> or
    # $VAR is not a value.
    ("secret-assignment", re.compile(
        r"\b(?:[A-Z][A-Z0-9_]*_(?:TOKEN|KEY|SECRET)|(?:[A-Z][A-Z0-9_]*_)?PASSWORD)\s*=\s*[\"']?(?![<$\s\"'])\S")),
]
# Runs of characters that could be one of the hub's own agent tokens; each is hashed and compared.
TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{16,128}")


def sanitize_name(name: str | None) -> str:
    """The display name: last path component, NFC, no control characters, slashes, or leading dots."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[/\\]", name)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = name.lstrip(". ").strip()
    return name[:MAX_NAME] or "artifact"


def forbidden_name(name: str) -> bool:
    """Checked on the name as given (last path component) and as cleaned, so ".env" cannot pass as "env"."""
    raw = re.split(r"[/\\]", str(name or ""))[-1].strip().lower()
    clean = sanitize_name(name).lower()
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in (raw, clean) for pattern in FORBIDDEN_NAMES)


def decode_text(data: bytes) -> str | None:
    """The text if the bytes are UTF-8 without NUL characters, else None."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def sniff(data: bytes, hint: str | None, name: str) -> tuple[str | None, str | None]:
    """(media type, text). The type is None when the bytes are of no type the hub knows.

    `hint` (the client's media type) and the name's extension only choose between text types.
    """
    for magic, media_type in MAGIC:
        if data.startswith(magic):
            return media_type, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", None
    text = decode_text(data)
    if text is None:
        return None, None
    hint = (hint or "").split(";")[0].strip().lower()
    if hint not in TEXT_TYPES:
        hint = EXTENSION_TYPES.get(_extension(name), "text/plain")
    if hint == "application/json":
        try:
            json.loads(text)
        except ValueError:
            hint = "text/plain"
    elif hint == "application/x-yaml":
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            hint = "text/plain"
    return hint, text


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def is_text_type(media_type: str) -> bool:
    return media_type in TEXT_TYPES


def scan(text: str, token_hashes: set[str] = frozenset()) -> tuple[str, int] | None:
    """(rule, line number) of the first secret found, else None. Never returns the matched text."""
    for number, line in enumerate(text.splitlines() or [text], 1):
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                return rule, number
        if token_hashes:
            for candidate in TOKEN_CANDIDATE.findall(line):
                if hashlib.sha256(candidate.encode()).hexdigest() in token_hashes:
                    return "hub-agent-token", number
    return None
