"""Parsing/editing helpers for Project Zomboid's <ServerName>.ini.

Confirmed live format (no [section] headers, unlike typical INI files):

    # A comment line documenting the next key
    Key=Value

    # Another comment
    AnotherKey=

We don't need a real config-file round-trip here -- a targeted per-line
regex lets us read and rewrite just the `Key=Value` lines we care about
while leaving comments, blank lines, and ordering untouched. Same
targeted-substitution philosophy as 7 Days to Die's serverconfig.xml
handling, just a simpler grammar since there's no XML involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+)=(?P<value>.*)$")

PropertyKind = str  # "bool" | "int" | "float" | "string"


@dataclass
class IniProperty:
    name: str
    value: str
    kind: PropertyKind


def parse_properties(ini_text: str) -> list[IniProperty]:
    properties = []
    seen = set()
    for line in ini_text.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        name = match["name"]
        if name in seen:
            continue  # duplicate keys would make value substitution ambiguous
        seen.add(name)
        properties.append(IniProperty(name=name, value=match["value"], kind=_infer_kind(match["value"])))
    return properties


def _infer_kind(value: str) -> PropertyKind:
    if value.lower() in ("true", "false"):
        return "bool"
    if re.fullmatch(r"-?\d+", value):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", value):
        return "float"
    return "string"


def apply_property_changes(ini_text: str, changes: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        name = match["name"]
        if name in changes:
            return f"{name}={changes[name]}"
        return match.group(0)

    lines = [_LINE_RE.sub(repl, line) for line in ini_text.splitlines()]
    # splitlines() drops a trailing newline if present -- restore it so the
    # file doesn't lose its final EOL on every save.
    result = "\n".join(lines)
    if ini_text.endswith("\n"):
        result += "\n"
    return result
