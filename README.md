# pzcontrol

A desktop GUI for managing a **Project Zomboid** dedicated server hosted behind a **Pterodactyl** panel — built with Python and PySide6. Three-channel design: **RCON** for admin commands, **Pterodactyl** for power controls/live console/fast backups, **SFTP** for browsing and editing files.

## Requirements

- Python 3.10+
- A Project Zomboid server on a standard [Pterodactyl](https://pterodactyl.io/) panel, with:
  - RCON enabled (for player/admin commands)
  - A Pterodactyl **Client API key** (Account → API Credentials) and the server's UUID
  - SFTP file access (on by default for any Pterodactyl server)

Nothing here needs SSH or shell access to the node — only RCON, the Pterodactyl Client API, and SFTP, all of which any standard Pterodactyl install already exposes.

## Install

```bash
git clone https://github.com/Stealthee/PZControl.git
cd PZControl
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pzcontrol
```

On first launch you'll get an empty server list — click **Add...** to set up a profile (see below). Config is stored per-OS user at `~/.config/pzcontrol/` (or `$XDG_CONFIG_HOME/pzcontrol/`), never inside this repo — safe to `git pull` updates without touching your saved servers.

### Running it persistently (optional)

pzcontrol is a normal desktop app — nothing stops you from adding it to your DE's autostart (`~/.config/autostart/` on Linux) if you want it running whenever you're at your machine. It only talks to the network when connected, otherwise idles.

## Connecting to a server

Click **Add...** and fill in:

- **RCON** — host/port/password from the server's `.ini` (`RCONPort`, `RCONPassword`). If `RCONPassword` is blank in the file, it's likely set via a Pterodactyl startup variable instead — check the Startup tab in Pterodactyl.
- **Pterodactyl** — panel host, API key (Account → API Credentials), server ID. Optional, but needed for power controls, live console, and the fast path on the Backup tab.
- **SFTP** — panel host, `username.serverid`, panel account password. Optional, but needed for the Files, Backup, Server Settings, and Sandbox Settings tabs, and for the Banned tab's fallback ban-list source.

The `.ini` and `_SandboxVars.lua` paths are auto-discovered over SFTP on first connect and cached in the profile afterwards. Checked in order: `/.cache/Server/`, then `/home/container/.cache/Server/` (confirmed live — the SFTP root isn't chrooted straight to the server's data dir on every node/egg, so the extra `home/container` prefix is sometimes needed), then a slower full search from `/` as a last resort. If a cached path ever goes stale (e.g. it was discovered against the wrong server by mistake), the app clears it and re-discovers automatically on the next failed load. The Backup tab resolves the save folder's real path the same way.

## Features

- **Power bar** — Start/Restart/Stop/Kill and a one-click Save World, live RCON/SFTP connection status dots, and a red/green flash notice when a mod or the server build has an update waiting (needs a restart to apply).
- **Players** — online/offline roster with join history. Right-click a player for teleport-to-another-player, godmode on/off, invisible on/off, give item, set access level, kick, or ban; offline entries can be removed from history.
- **Banned** — RCON ban list if the build supports it, otherwise falls back to reading the admin SQLite DB over SFTP. Add/unban from the same tab.
- **Console & Chat** — live raw console output plus an RCON command box; a separate Chat tab for sending messages (inbound player chat isn't parsed yet, see Known limitations — use the Console tab to see it in the raw log).
- **Files** — a general SFTP browser/editor: navigate, edit files inline, upload files or a whole folder, rename, delete, and set permissions (recursively for folders).
- **Backup** — see below.
- **Server Settings** / **Sandbox Settings** — edit the `.ini` and `_SandboxVars.lua` through a form instead of raw text.
- **Auto Restart** — daily-time or every-N-hours restarts, with a templated warning broadcast 5 minutes and 1 minute out, then a final 20-second countdown.
- **Broadcasts** — templated join messages, sent when a player's name appears in the RCON player list that wasn't there on the previous poll.
- **Mods** — edit `WorkshopItems=`/`Mods=` directly, or use the Workshop Update Check table (compares installed mods against Steam Workshop, flags ones that changed or won't load, lets you Freeze/unfreeze or remove one, auto-runs on a timer if you want); also checks the dedicated server binary itself for a new build. Optional auto-restart when an update's available and the server is currently empty.
- **Browse Mods** — search the Steam Workshop (needs a free Steam Web API key, one-click link to get one) and add results straight into your mod list.

### Backup tab

Create, restore, delete, and reset the live save, entirely through the Pterodactyl file manager — never streams your save through this app's own process:

- **Create Backup** — asks for an optional label, then has the panel `compress` the save into a `.tar.gz` on the node's own disk and `rename` it into place. A full backup of a save with 170k+ files takes a few seconds, not hours.
- **Restore Backup** — pick a backup from the list; choose to snapshot the current save first or just replace it. Uses the panel's `decompress` + `rename`, with an automatic slower-but-correct SFTP fallback if the panel's decompress ever fails on a given node/Wings version.
- **Delete Backup** / **Reset Map** — remove a backup, or wipe the live save outright (the server generates a fresh map on next start).
- Every action first checks the server is actually stopped via the Pterodactyl API (not a cached status) and refuses to run otherwise, since backing up or restoring a save the server has open is unsafe. After it finishes, it offers to start the server back up for you.
- Falls back automatically to a slower, correctness-only per-file SFTP copy for both create and restore if no Pterodactyl connection is configured for that server profile.

## Known limitations

- **Players tab has no SteamID/position/ping.** PZ's RCON `players` command only returns bare names, unlike 7 Days to Die's telnet `listplayers`. Ban/kick/teleport work by name.
- **Ban list**: tries an RCON list-bans command first; PZ vanilla RCON doesn't appear to have one, so it falls back to reading the `bannedid`/`bannedip` tables from the server's admin SQLite database (`/.cache/db/<ServerName>.db`, confirmed live) over SFTP.
- **Chat tab only shows messages sent from this app.** Detecting inbound player chat would need parsing Pterodactyl's console log for PZ's chat-line format, which hasn't been verified against a live server. Full raw console output (including real chat) is visible in the Console tab.
- **Join broadcasts** are detected by diffing successive RCON `players` polls (reliable, reuses data the Players tab already trusts) rather than console log parsing. **Death/level-up broadcasts aren't implemented** — PZ has no unified player level, and death-event log parsing is unverified.
- **RCON command syntax** — every command the app sends has now been exercised against a live server: `teleportplayer`, `godmodeplayer`, `invisibleplayer`, `setaccesslevel`, `additem` (note the `*player` forms — the bare `teleport`/`godmode`/`invisible` commands target the console's own character, not the specified player), `kickuser`, `banuser`/`unbanuser`, `servermsg` (say), and `save`. One live-confirmed gotcha: `setaccesslevel` and `banuser` both persist to the server's whitelist DB even for a username that was never actually connected -- there's no confirmation step, so double-check the name before using either from the Players tab.
- **Backup tab's fast path depends on the node's Wings version** supporting `files/compress`/`files/decompress`/`files/rename`/`files/delete` correctly. `files/copy` was found broken (`DaemonConnectionException`) on at least one real Wings v1.12.1 node — the app never uses it. If `decompress` ever fails on your node, restore automatically falls back to the slower SFTP method rather than failing outright.

## Dependencies

| Package | Purpose |
|---|---|
| `PySide6` | Desktop GUI |
| `requests` | Pterodactyl REST API |
| `websocket-client` | Pterodactyl live console |
| `paramiko` | SFTP file access |

## License

[MIT](LICENSE) — use it, mod it, ship it.

## Support

If this saved you some time, a thank-you is more than enough. If you'd like to buy me a coffee too, Cash App: **$j71rivera**. Never required, always appreciated.
