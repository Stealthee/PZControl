"""Connection settings for a single Project Zomboid server.

Three independent channels are configured here:
  * RCON -- the game server's own admin protocol (player list, kick, ban,
    teleport, chat, sandbox toggles). Source-RCON compatible, unlike 7 Days
    to Die's plain-text telnet console.
  * Pterodactyl -- the panel's Client API + websocket (power actions, live
    console/chat, sending commands as an alternative to RCON).
  * SFTP -- the panel's file manager access, for browsing and editing the
    server's files directly (e.g. sftp://username.serverid@host:2022),
    including the <ServerName>.ini and <ServerName>_SandboxVars.lua config
    files, whose exact paths are auto-discovered rather than hardcoded
    since they're derived from the server's name.

Defaults reflect what each service ships with; every value is editable.
"""

from dataclasses import dataclass, field

DEFAULT_RCON_PORT = 27015
DEFAULT_PTERODACTYL_PORT = 443
DEFAULT_SFTP_PORT = 2022

DEFAULT_JOIN_MESSAGE = "{name} has entered the zone. Stay safe out there."
DEFAULT_RESTART_WARNING_MESSAGE = "Server restarting in {minutes} minute(s) -- back shortly!"


@dataclass
class ServerConfig:
    name: str = "My Server"
    is_default: bool = False

    rcon_host: str = ""
    rcon_port: int = DEFAULT_RCON_PORT
    rcon_password: str = ""

    pterodactyl_host: str = ""
    pterodactyl_port: int = DEFAULT_PTERODACTYL_PORT
    pterodactyl_use_tls: bool = True
    pterodactyl_api_key: str = ""
    pterodactyl_server_id: str = ""

    sftp_host: str = ""
    sftp_port: int = DEFAULT_SFTP_PORT
    sftp_username: str = ""
    sftp_password: str = ""

    # Auto-discovered on first successful SFTP connect (server name varies
    # per profile, so these can't be derived from static defaults). Cached
    # here afterwards so reconnects don't need to re-search the filesystem.
    ini_path: str = ""
    sandboxvars_path: str = ""
    ban_db_path: str = ""

    # Roster of every player ever seen on this server, so the Players tab can
    # show people as "Offline" instead of forgetting them on disconnect.
    # Each entry: {"name": str, "last_seen": str}
    known_players: list[dict] = field(default_factory=list)

    autorestart_enabled: bool = False
    autorestart_mode: str = "time"  # "time" (daily HH:MM) or "interval" (every N hours)
    autorestart_time: str = "04:00"
    autorestart_interval_hours: float = 6.0
    autorestart_warning_message: str = DEFAULT_RESTART_WARNING_MESSAGE

    # Detected by diffing successive RCON `players` polls (reliable, since it's
    # the same data source the Players tab already trusts) -- not by parsing
    # console log text. Death/level-up broadcasts from ratty aren't included:
    # PZ's console log format for those events hasn't been verified live, and
    # a guessed regex risks misfiring on unrelated lines.
    broadcast_join_enabled: bool = True
    broadcast_join_message: str = DEFAULT_JOIN_MESSAGE

    # Last-seen Steam Workshop "time_updated" per mod ID, from the last time
    # the Mods tab checked -- lets it flag which ones changed since then
    # instead of just showing an absolute date every time. {workshop_id: unix_ts}
    known_workshop_updates: dict[str, int] = field(default_factory=dict)

    # Workshop items "Frozen" from the Mods tab's right-click menu -- pulled
    # out of the active WorkshopItems=/Mods= lists (so they stop loading) but
    # remembered here along with the Mod IDs they carried, so "Active" can put
    # them back without the user having to re-find/re-type anything.
    # {workshop_id: [mod_id, ...]}
    frozen_mods: dict[str, list[str]] = field(default_factory=dict)

    # Workshop items whose "Not working" / "Missing Mod ID(s)" flag was
    # right-clicked and confirmed a false positive (e.g. an old test-build
    # subfolder's mod.info declaring an ID irrelevant to the currently
    # running game version) -- suppressed in the Mods tab until the item
    # actually updates again on Steam Workshop, at which point the stored
    # timestamp stops matching and the flag re-evaluates from scratch.
    # {workshop_id: time_updated at the moment it was dismissed}
    dismissed_mod_issues: dict[str, int] = field(default_factory=dict)

    # Whether a Workshop Update Check that finds a changed mod flashes the
    # red/green notice on the main window (a reminder that the server needs
    # a reboot to pick up the change). On by default; toggled from the Mods tab.
    mod_update_flash_enabled: bool = True

    # Whether the Mods tab re-runs the Workshop Update Check on a timer
    # instead of only when the user clicks the button (or freezes/unfreezes/
    # removes a mod). Off by default -- opt in from the Mods tab, since it
    # means periodic Steam Workshop API calls happening in the background.
    mod_auto_check_enabled: bool = False
    mod_auto_check_interval_minutes: int = 60

    # Restarts the server on its own when BOTH hold: a Workshop Update Check
    # found a changed active (non-frozen) mod, AND no players are currently
    # online. Off by default -- opt in from the Mods tab, since it's a
    # background action that touches the live server without a confirmation
    # prompt each time.
    mod_update_autorestart_enabled: bool = False

    @property
    def pterodactyl_base_url(self) -> str:
        scheme = "https" if self.pterodactyl_use_tls else "http"
        default_port = 443 if self.pterodactyl_use_tls else 80
        if self.pterodactyl_port == default_port:
            return f"{scheme}://{self.pterodactyl_host}"
        return f"{scheme}://{self.pterodactyl_host}:{self.pterodactyl_port}"


@dataclass
class AppSettings:
    """Settings that apply across every saved server profile, not just one --
    a Steam Web API key is tied to a Steam account, not to any given server,
    so it lives here instead of on ServerConfig."""

    steam_api_key: str = ""

    # Left-hand tab order, saved by name after a drag-to-reorder -- a UI
    # layout preference, not something tied to any one server either.
    tab_order: list[str] = field(default_factory=list)
