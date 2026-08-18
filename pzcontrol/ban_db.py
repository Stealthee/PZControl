"""Reads the ban list from Project Zomboid's admin SQLite database.

Vanilla RCON has no confirmed list-bans command, so the Banned tab falls
back to this: PZ keeps its ban/whitelist records in a separate SQLite
database at /.cache/db/<ServerName>.db (auto-discovered alongside the
.ini and _SandboxVars.lua, confirmed against a live server), distinct
from the per-world players.db under Saves/Multiplayer/<world>/ which only
holds player character data.

Confirmed live schema:
    bannedid  (steamid TEXT, reason TEXT)
    bannedip  (ip TEXT, username TEXT, reason TEXT)

Bans are still added/removed live via RconClient.ban_add/ban_remove
(banid/banuser over RCON) -- this module is read-only, just for listing
what's already banned.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pzcontrol.sftp_client import SftpClient


@dataclass
class BanEntry:
    identifier: str  # steamid or IP
    kind: str  # "steamid" | "ip"
    reason: str
    username: str = ""  # only known for IP bans


def find_admin_db_path(sftp: SftpClient) -> str | None:
    candidates = sftp.find_files("/.cache/db", lambda name: name.lower().endswith(".db"))
    # Pterodactyl.db is the panel's own bookkeeping, not the game's admin DB.
    candidates = [c for c in candidates if not c.endswith("/Pterodactyl.db")]
    return candidates[0] if candidates else None


def read_bans(sftp: SftpClient, remote_db_path: str) -> list[BanEntry]:
    with tempfile.TemporaryDirectory() as tmp:
        local_path = str(Path(tmp) / "admin.db")
        sftp.download_to(remote_db_path, local_path)
        conn = sqlite3.connect(local_path)
        try:
            entries: list[BanEntry] = []
            for steamid, reason in conn.execute("SELECT steamid, reason FROM bannedid"):
                entries.append(BanEntry(identifier=steamid, kind="steamid", reason=reason or ""))
            for ip, username, reason in conn.execute("SELECT ip, username, reason FROM bannedip"):
                entries.append(BanEntry(identifier=ip, kind="ip", reason=reason or "", username=username or ""))
            return entries
        finally:
            conn.close()
