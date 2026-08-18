"""Checks the installed Project Zomboid dedicated server build against Steam.

Uses the public steamcmd.net app-info mirror -- a community-run cache of the
same `app_info_print` data SteamCMD itself would return, reachable over plain
HTTP with no Steam Web API key needed (unlike Workshop search).

The installed side is read straight from the server's own Steam appmanifest
(steamapps/appmanifest_380870.acf) -- ground truth, not anything cached or
remembered, same philosophy as the Workshop mtime check in steam_workshop.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

APP_ID = "380870"  # Project Zomboid Dedicated Server
MANIFEST_PATH = f"steamapps/appmanifest_{APP_ID}.acf"

_INFO_ENDPOINT = f"https://api.steamcmd.net/v1/info/{APP_ID}"

# The .acf format is Valve's KeyValues -- regex extraction of these two fields
# is enough here and avoids pulling in a full VDF parser for one file.
_BUILDID_RE = re.compile(r'"buildid"\s+"(\d+)"')
_BETAKEY_RE = re.compile(r'"BetaKey"\s+"([^"]*)"')


class GameUpdateError(RuntimeError):
    pass


@dataclass
class InstalledBuild:
    buildid: str
    branch: str  # "public" when no beta key is set


@dataclass
class LatestBuild:
    buildid: str
    description: str  # e.g. "Build 42.19.1" -- not populated on every branch
    time_updated: int | None  # unix epoch (UTC)


def parse_installed_build(acf_text: str) -> InstalledBuild:
    buildid_match = _BUILDID_RE.search(acf_text)
    if not buildid_match:
        raise GameUpdateError("Could not find a buildid in the server's appmanifest")
    betakey_match = _BETAKEY_RE.search(acf_text)
    branch = betakey_match.group(1) if betakey_match and betakey_match.group(1) else "public"
    return InstalledBuild(buildid=buildid_match.group(1), branch=branch)


def fetch_latest_build(branch: str, timeout: float = 15.0) -> LatestBuild:
    try:
        resp = requests.get(_INFO_ENDPOINT, timeout=timeout)
    except requests.RequestException as exc:
        raise GameUpdateError(f"Could not reach steamcmd.net: {exc}") from exc
    if resp.status_code >= 400:
        raise GameUpdateError(f"steamcmd.net lookup failed: {resp.status_code}")
    try:
        branches = resp.json()["data"][APP_ID]["depots"]["branches"]
        info = branches[branch]
    except (KeyError, ValueError, TypeError) as exc:
        raise GameUpdateError(f"Branch '{branch}' not found in Steam app info") from exc
    time_updated = info.get("timeupdated")
    return LatestBuild(
        buildid=info["buildid"],
        description=info.get("description", ""),
        time_updated=int(time_updated) if time_updated else None,
    )
