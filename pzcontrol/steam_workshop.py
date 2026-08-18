"""Looks up Steam Workshop metadata (title, last-updated time) for mod IDs,
and searches the Workshop by text.

Lookup-by-ID uses ISteamRemoteStorage/GetPublishedFileDetails, which is public
and needs no API key. Text search is a different story: confirmed live that
IPublishedFileService/QueryFiles returns 403 Forbidden without a `key` --
Steam Web API keys are free (steamcommunity.com/dev/apikey, any placeholder
like "localhost" works for the required domain field) but search is opt-in
depending on whether the user has bothered to set one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

_DETAILS_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_QUERY_ENDPOINT = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
_PZ_APP_ID = "108600"
_QUERY_TYPE_RANKED_BY_TEXT_SEARCH = 12


class WorkshopError(RuntimeError):
    pass


class WorkshopAuthError(WorkshopError):
    pass


@dataclass
class WorkshopItem:
    workshop_id: str
    title: str
    time_updated: int | None  # unix epoch (UTC); None if the lookup failed
    found: bool
    description: str = ""  # full description, not the search endpoint's truncated short_description


def fetch_details(workshop_ids: list[str], timeout: float = 15.0) -> dict[str, WorkshopItem]:
    """One batched POST for every ID rather than N requests -- the endpoint
    accepts a `publishedfileids[i]` field per item plus an `itemcount`."""
    if not workshop_ids:
        return {}
    data = {"itemcount": str(len(workshop_ids))}
    for i, workshop_id in enumerate(workshop_ids):
        data[f"publishedfileids[{i}]"] = workshop_id
    try:
        resp = requests.post(_DETAILS_ENDPOINT, data=data, timeout=timeout)
    except requests.RequestException as exc:
        raise WorkshopError(f"Could not reach Steam Workshop: {exc}") from exc
    if resp.status_code >= 400:
        raise WorkshopError(f"Steam Workshop lookup failed: {resp.status_code}")
    entries = resp.json().get("response", {}).get("publishedfiledetails", [])
    results: dict[str, WorkshopItem] = {}
    for entry in entries:
        workshop_id = entry.get("publishedfileid", "")
        ok = entry.get("result") == 1
        results[workshop_id] = WorkshopItem(
            workshop_id=workshop_id,
            title=entry.get("title", "(untitled)") if ok else "(not found on Workshop)",
            time_updated=entry.get("time_updated") if ok else None,
            found=ok,
            description=entry.get("description", "") if ok else "",
        )
    return results


# Confirmed live: many PZ mod authors list their Workshop item's actual
# internal Mod ID(s) right in the description (e.g. "Mod ID: SomeModId"),
# specifically so admins can find it without having to download the content
# first. Best-effort only -- this is documentation the author wrote by hand,
# not verified against real files the way scanning mod.info is.
_MOD_ID_LINE_RE = re.compile(r"Mod ID:\s*(\S+)", re.IGNORECASE)

# Steam Workshop descriptions are BBCode, and authors routinely put "Mod ID:"
# right inside a header tag, e.g. "[h1]Mod ID: KRSolarOS[/h1]" with no space
# before the closing tag. \S+ above then swallows "[/h1]" onto the ID itself,
# which produces a Mods= entry that can never match a real mod folder --
# confirmed live, this is exactly what happened to KRSolarOS/KRCoreOS on a
# real server. Trim to the leading run of characters real PZ mod IDs use
# (letters/digits/underscore/hyphen, plus "/" for the workshopId/subModId
# form) so any trailing BBCode or stray punctuation gets dropped instead of
# silently written into the .ini.
_VALID_MOD_ID_RE = re.compile(r"[A-Za-z0-9_/-]+")


def parse_mod_ids_from_description(description: str) -> list[str]:
    seen: list[str] = []
    for match in _MOD_ID_LINE_RE.finditer(description):
        cleaned = _VALID_MOD_ID_RE.match(match.group(1).strip())
        mod_id = cleaned.group(0) if cleaned else ""
        if mod_id and mod_id not in seen:
            seen.append(mod_id)
    return seen


@dataclass
class WorkshopSearchResult:
    workshop_id: str
    title: str
    description: str
    time_updated: int | None


def search(api_key: str, query: str, page: int = 1, per_page: int = 20, timeout: float = 15.0) -> list[WorkshopSearchResult]:
    if not api_key:
        raise WorkshopAuthError("No Steam Web API key configured -- add one on the Browse Mods tab.")
    query = query.strip()
    if not query:
        return []
    params = {
        "key": api_key,
        "query_type": _QUERY_TYPE_RANKED_BY_TEXT_SEARCH,
        "page": page,
        "numperpage": per_page,
        "appid": _PZ_APP_ID,
        "search_text": query,
        "return_short_description": True,
    }
    try:
        resp = requests.get(_QUERY_ENDPOINT, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise WorkshopError(f"Could not reach Steam Workshop: {exc}") from exc
    if resp.status_code == 403:
        raise WorkshopAuthError("Steam rejected the API key -- double check it on the Browse Mods tab.")
    if resp.status_code >= 400:
        raise WorkshopError(f"Steam Workshop search failed: {resp.status_code}")
    entries = resp.json().get("response", {}).get("publishedfiledetails", [])
    results = []
    for entry in entries:
        if entry.get("result") != 1:
            continue
        results.append(
            WorkshopSearchResult(
                workshop_id=entry.get("publishedfileid", ""),
                title=entry.get("title") or "(untitled)",
                description=(entry.get("short_description") or "").strip(),
                time_updated=entry.get("time_updated"),
            )
        )
    return results
