"""Parsing/editing helpers for Project Zomboid's <ServerName>_SandboxVars.lua.

Confirmed live format -- a plain Lua table, one level of nesting deep:

    SandboxVars = {
        VERSION = 6,
        -- Changing this also sets the "Population Multiplier"...
        -- 1 = Insane
        -- 2 = Very High
        Zombies = 4,
        ...
        DAMN = {
            AllowWreckyMcChevySpawns = true,
            ...
        },
        LTPMS = {
            Whitelist = "",
        },
    }

Root-level keys are core game sandbox options; the nested tables (`DAMN`,
`bdtmre`, `LTPMS`, ...) are per-mod option groups added by installed mods.
Unlike 7 Days to Die's compiled sandbox-code strings, nothing needs
decoding here -- every option is already plain text, and the `--` comment
lines immediately above a key *are* its documentation (often including the
enum meaning of each integer value), so we capture them as free-text help
rather than reconstructing a data table of allowed values.

Keys are addressed by dotted path ("Zombies" for a root key, "DAMN.AllowWreckyMcChevySpawns"
for a nested one) so root and per-mod keys can never collide when applying changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECTION_OPEN_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*$")
_SECTION_CLOSE_RE = re.compile(r"^\s*\},?\s*$")
_ASSIGN_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?),\s*$")
_COMMENT_RE = re.compile(r"^\s*--\s?(?P<text>.*)$")

PropertyKind = str  # "bool" | "int" | "float" | "string"


@dataclass
class LuaProperty:
    path: str  # dotted path, e.g. "Zombies" or "DAMN.AllowWreckyMcChevySpawns"
    name: str  # bare key name
    section: str  # "" for root-level options, else the enclosing table's name
    value: str  # raw Lua literal, e.g. "true", "4", '""'
    kind: PropertyKind
    help_text: str = field(default="")


def _infer_kind(value: str) -> PropertyKind:
    if value in ("true", "false"):
        return "bool"
    if value.startswith('"') and value.endswith('"'):
        return "string"
    if re.fullmatch(r"-?\d+", value):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", value):
        return "float"
    return "string"


def parse_properties(lua_text: str) -> list[LuaProperty]:
    properties: list[LuaProperty] = []
    seen_paths: set[str] = set()
    pending_comments: list[str] = []
    # A stack of enclosing table names. Depth 1 is the outer `SandboxVars = {`
    # wrapper itself, which isn't a real option group -- only depth >= 2
    # (per-mod tables like `DAMN = { ... }`) should prefix key paths.
    section_stack: list[str] = []

    def current_section() -> str:
        return section_stack[-1] if len(section_stack) >= 2 else ""

    for line in lua_text.splitlines():
        comment_match = _COMMENT_RE.match(line)
        if comment_match:
            pending_comments.append(comment_match["text"])
            continue

        section_open = _SECTION_OPEN_RE.match(line)
        if section_open:
            section_stack.append(section_open["name"])
            pending_comments.clear()
            continue

        if _SECTION_CLOSE_RE.match(line):
            if section_stack:
                section_stack.pop()
            pending_comments.clear()
            continue

        assign_match = _ASSIGN_RE.match(line)
        if assign_match:
            name = assign_match["name"]
            section = current_section()
            if name == "VERSION" and not section:
                pending_comments.clear()
                continue  # file format version, not a gameplay option
            path = f"{section}.{name}" if section else name
            if path in seen_paths:
                pending_comments.clear()
                continue  # duplicate path would make value substitution ambiguous
            seen_paths.add(path)
            value = assign_match["value"]
            properties.append(
                LuaProperty(
                    path=path,
                    name=name,
                    section=section,
                    value=value,
                    kind=_infer_kind(value),
                    help_text="\n".join(pending_comments),
                )
            )
            pending_comments.clear()
            continue

        # Blank line or anything unrecognized resets the pending comment run
        # so stray text doesn't get attached to an unrelated later key.
        pending_comments.clear()

    return properties


def apply_property_changes(lua_text: str, changes: dict[str, str]) -> str:
    """`changes` maps dotted path -> new raw Lua literal (e.g. "true", "7", '"foo"')."""
    section_stack: list[str] = []

    def current_section() -> str:
        return section_stack[-1] if len(section_stack) >= 2 else ""

    out_lines = []
    for line in lua_text.splitlines():
        section_open = _SECTION_OPEN_RE.match(line)
        if section_open:
            section_stack.append(section_open["name"])
            out_lines.append(line)
            continue
        if _SECTION_CLOSE_RE.match(line):
            if section_stack:
                section_stack.pop()
            out_lines.append(line)
            continue

        assign_match = _ASSIGN_RE.match(line)
        if assign_match:
            name = assign_match["name"]
            path = f"{current_section()}.{name}" if current_section() else name
            if path in changes:
                indent = assign_match["indent"]
                out_lines.append(f"{indent}{name} = {changes[path]},")
                continue
        out_lines.append(line)

    result = "\n".join(out_lines)
    if lua_text.endswith("\n"):
        result += "\n"
    return result
