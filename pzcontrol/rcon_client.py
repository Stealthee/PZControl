"""RCON client for the Project Zomboid server console.

Unlike 7 Days to Die's plain-text telnet console, Project Zomboid speaks the
Source RCON protocol: a binary-framed TCP protocol (the same one Valve games
use). Every packet is:

    int32 size   (little-endian, byte count of everything after this field)
    int32 id     (request id, echoed back in the response so replies can be
                  correlated -- we don't actually need this since we
                  serialize commands with a lock, but the protocol requires it)
    int32 type   (3 = SERVERDATA_AUTH, 2 = SERVERDATA_EXECCOMMAND,
                  0 = SERVERDATA_RESPONSE_VALUE, 2 = SERVERDATA_AUTH_RESPONSE)
    body         (ASCII, null-terminated)
    ""           (a second, empty null-terminated string -- always present)

Because responses are framed and carry the request id, there's no need for
the "wait for the socket to go quiet" heuristic 7 Days to Die's raw telnet
console required -- we just read exactly one framed packet per command.
"""

from __future__ import annotations

import re
import socket
import struct
import threading
from collections.abc import Callable

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(RuntimeError):
    pass


class RconAuthError(RconError):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, on_disconnect: Callable[[], None] | None = None):
        self.host = host
        self.port = port
        self.password = password
        self._sock: socket.socket | None = None
        self._on_disconnect = on_disconnect
        self._command_lock = threading.Lock()
        self._next_id = 1

    # -- connection lifecycle -------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock = sock
        try:
            self._authenticate()
        except Exception:
            self.close()
            raise

    def _authenticate(self) -> None:
        request_id = self._send_packet(SERVERDATA_AUTH, self.password)
        # Confirmed live against a real PZ server: it sends an extra empty
        # SERVERDATA_RESPONSE_VALUE (type 0) packet before the real
        # SERVERDATA_AUTH_RESPONSE (type 2) -- a known quirk inherited from
        # the classic Source RCON protocol. Reading only one packet here left
        # that stray packet in the socket, which silently shifted every
        # later command's response by one (e.g. `players` returning `help`'s
        # answer). Discard packets until the real auth response arrives.
        response_id = -1
        response_type = -1
        while response_type != SERVERDATA_AUTH_RESPONSE:
            response_id, response_type, _body = self._read_packet()
        if response_id != request_id:
            raise RconAuthError("RCON authentication failed -- check the RCON password")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None

    # -- wire protocol ----------------------------------------------------------

    def _send_packet(self, packet_type: int, body: str) -> int:
        if self._sock is None:
            raise RconError("Not connected")
        request_id = self._next_id
        self._next_id += 1
        payload = body.encode("utf-8") + b"\x00\x00"
        header = struct.pack("<ii", request_id, packet_type)
        packet = struct.pack("<i", len(header) + len(payload)) + header + payload
        try:
            self._sock.sendall(packet)
        except OSError as exc:
            self._handle_disconnect()
            raise RconError(f"Send failed: {exc}") from exc
        return request_id

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                self._handle_disconnect()
                raise RconError("Connection closed while reading response")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_packet(self) -> tuple[int, int, str]:
        try:
            size_bytes = self._recv_exact(4)
            (size,) = struct.unpack("<i", size_bytes)
            body_bytes = self._recv_exact(size)
        except OSError as exc:
            self._handle_disconnect()
            raise RconError(f"Read failed: {exc}") from exc
        response_id, response_type = struct.unpack("<ii", body_bytes[:8])
        text = body_bytes[8:-2].decode("utf-8", errors="replace")
        return response_id, response_type, text

    def _handle_disconnect(self) -> None:
        was_connected = self._sock is not None
        self._sock = None
        if was_connected and self._on_disconnect:
            self._on_disconnect()

    # -- command execution -----------------------------------------------------

    def run_command(self, command: str) -> str:
        with self._command_lock:
            self._send_packet(SERVERDATA_EXECCOMMAND, command)
            # Confirmed live: a long reply (e.g. `help`) spans multiple RCON
            # packets, and EXECCOMMAND has no built-in end-of-response marker
            # (the classic Source RCON multi-packet problem). The standard
            # fix: send a second, trivially-fast command right after and read
            # packets until its response comes back -- everything before that
            # belongs to the real command.
            terminator_id = self._send_packet(SERVERDATA_EXECCOMMAND, "")
            parts: list[str] = []
            while True:
                response_id, _response_type, text = self._read_packet()
                if response_id == terminator_id:
                    break
                parts.append(text)
            return "".join(parts)

    # -- high level commands ----------------------------------------------------

    _PLAYER_LINE_RE = re.compile(r"^\s*-\s*(?P<name>.+?)\s*$")

    def list_players(self) -> list[str]:
        """Names of currently connected players.

        PZ's `players` command replies with a header line and one `- Name`
        line per connected player -- no steamid/position/health/ping like
        7 Days to Die's telnet `listplayers` provided.
        """
        players = []
        for line in self.run_command("players").splitlines():
            if not line.strip() or line.lower().startswith("players connected"):
                continue
            match = self._PLAYER_LINE_RE.match(line)
            if match:
                players.append(match["name"])
        return players

    def kick(self, player: str, reason: str = "") -> str:
        # Confirmed live: the bullet in `help` is labeled "kick" but its own
        # usage text (and the actual working command) is "kickuser".
        cmd = f'kickuser "{player}"'
        if reason:
            cmd += f' -r "{reason}"'
        return self.run_command(cmd)

    def ban_add(self, identifier: str, by_steamid: bool = False) -> str:
        # Confirmed live -- also confirmed this persists in the same admin
        # whitelist table `list_bans`/ban_db.py reads (role 1 = "banned"),
        # even for a username that was never actually connected.
        cmd = "banid" if by_steamid else "banuser"
        return self.run_command(f'{cmd} "{identifier}"')

    def ban_remove(self, identifier: str, by_steamid: bool = False) -> str:
        # Confirmed live -- note this resets the whitelist row back to
        # role 2 ("user") rather than deleting it; harmless, just not a
        # full cleanup (removeuserfromwhitelist is the only way to do that,
        # and isn't wired up here since normal unban shouldn't also purge
        # someone's whitelist membership).
        cmd = "unbanid" if by_steamid else "unbanuser"
        return self.run_command(f'{cmd} "{identifier}"')

    def list_bans(self) -> list[str] | None:
        """No RCON list-bans command exists -- confirmed against a live `help`
        call on this server build. Caller should fall back to reading the
        admin SQLite database over SFTP (see ban_db.py) instead."""
        return None

    def teleport_to_player(self, player: str, target: str) -> str:
        # `teleport` moves the console/admin's own character -- `teleportplayer`
        # is the one that actually moves `player` to `target`, confirmed
        # against a live `help` call.
        return self.run_command(f'teleportplayer "{player}" "{target}"')

    def say(self, message: str) -> str:
        return self.run_command(f'servermsg "{message}"')

    def save_world(self) -> str:
        return self.run_command("save")

    def set_access_level(self, player: str, level: str) -> str:
        """`help` only advertises admin/moderator/overseer/gm/observer, but
        the command actually accepts any name from the server's `role`
        table -- confirmed live that "user" works too and is the way to
        fully remove admin/etc, dropping a player back to a normal member
        with no special capabilities (verified against the whitelist.db
        `role` column, which flipped from 7/admin to 2/user)."""
        return self.run_command(f'setaccesslevel "{player}" "{level}"')

    def toggle_godmode(self, player: str, enabled: bool) -> str:
        # `godmode` affects the console/admin's own character -- `godmodeplayer`
        # is the one that targets `player`, confirmed against a live `help` call.
        return self.run_command(f'godmodeplayer "{player}" -{"true" if enabled else "false"}')

    def toggle_invisible(self, player: str, enabled: bool) -> str:
        # Same self-vs-player split as godmode: `invisibleplayer` targets `player`.
        return self.run_command(f'invisibleplayer "{player}" -{"true" if enabled else "false"}')

    def add_item(self, player: str, item: str, count: int = 1) -> str:
        # Confirmed live: the documented example passes the item type
        # unquoted (/additem "rj" Base.Axe 5), unlike the other string args.
        return self.run_command(f'additem "{player}" {item} {count}')

    def help_text(self) -> str:
        """Authoritative command list for this exact server build -- use this
        to spot-check the command syntax above against the live server."""
        return self.run_command("help")
