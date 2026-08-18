"""SFTP wrapper for browsing and editing a server's files.

Pterodactyl exposes each server's file manager over SFTP, with credentials
of the form `username.serverid@host:2022` and the panel account's password
(not the API key). This is a thin synchronous wrapper around paramiko --
callers are expected to run it off the UI thread.

Game-agnostic -- reused as-is from the 7 Days to Die control panel.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass

import paramiko


class SftpError(RuntimeError):
    pass


@dataclass
class FileEntry:
    name: str
    is_dir: bool
    size: int


class SftpClient:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._transport: paramiko.Transport | None = None
        self._sftp: paramiko.SFTPClient | None = None
        # paramiko's SFTPClient shares a single channel and deadlocks when used
        # concurrently from multiple threads -- serialize all operations on it.
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0) -> None:
        with self._lock:
            try:
                transport = paramiko.Transport((self.host, self.port))
                transport.connect(username=self.username, password=self.password)
                self._transport = transport
                self._sftp = paramiko.SFTPClient.from_transport(transport)
            except (paramiko.SSHException, OSError) as exc:
                raise SftpError(f"Could not connect: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._sftp is not None:
                self._sftp.close()
                self._sftp = None
            if self._transport is not None:
                self._transport.close()
                self._transport = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.is_active()

    def _client(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise SftpError("Not connected")
        return self._sftp

    def list_dir(self, path: str) -> list[FileEntry]:
        with self._lock:
            client = self._client()
            entries = []
            for attr in client.listdir_attr(path):
                mode = attr.st_mode or 0
                is_dir = stat.S_ISDIR(mode)
                if stat.S_ISLNK(mode):
                    # Resolve symlinks so valid ones are navigable as directories
                    # and broken ones don't blow up the whole listing.
                    full_path = f"{path.rstrip('/')}/{attr.filename}"
                    try:
                        target = client.stat(full_path)
                    except OSError:
                        is_dir = False
                    else:
                        is_dir = stat.S_ISDIR(target.st_mode or 0)
                entries.append(
                    FileEntry(
                        name=attr.filename,
                        is_dir=is_dir,
                        size=attr.st_size or 0,
                    )
                )
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return entries

    def find_files(self, path: str, predicate, max_depth: int = 6) -> list[str]:
        """Recursively search under `path` for files where predicate(filename) is True.

        Used to auto-discover the server's .ini / SandboxVars.lua without a
        hardcoded path, since the filename is derived from the server's name.
        """
        with self._lock:
            client = self._client()
            found: list[str] = []
            self._find_files_recursive(client, path, predicate, 0, max_depth, found)
            return found

    def _find_files_recursive(self, client, path, predicate, depth, max_depth, found) -> None:
        if depth > max_depth:
            return
        try:
            entries = client.listdir_attr(path)
        except OSError:
            return
        for attr in entries:
            full_path = f"{path.rstrip('/')}/{attr.filename}"
            mode = attr.st_mode or 0
            if stat.S_ISDIR(mode):
                self._find_files_recursive(client, full_path, predicate, depth + 1, max_depth, found)
            elif predicate(attr.filename):
                found.append(full_path)

    def read_file(self, path: str, max_size: int = 2_000_000) -> str:
        with self._lock:
            client = self._client()
            size = client.stat(path).st_size or 0
            if size > max_size:
                raise SftpError(f"File is too large to edit here ({size:,} bytes, limit {max_size:,})")
            with client.open(path, "r") as fh:
                data = fh.read()
            return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

    def write_file(self, path: str, content: str) -> None:
        with self._lock:
            with self._client().open(path, "w") as fh:
                fh.write(content.encode("utf-8"))

    def download_to(self, remote_path: str, local_path: str) -> None:
        """Fetch a remote file's raw bytes to a local path -- for binary files
        (e.g. PZ's SQLite admin database) that read_file's utf-8 decode can't
        handle."""
        with self._lock:
            self._client().get(remote_path, local_path)

    def delete_file(self, path: str) -> None:
        with self._lock:
            self._client().remove(path)

    def rename(self, old_path: str, new_path: str) -> None:
        with self._lock:
            self._client().rename(old_path, new_path)

    def newest_mtime_under(self, path: str, max_depth: int = 6) -> int | None:
        """Newest file mtime found anywhere under `path`, recursively. None if
        the path doesn't exist or has no files.

        Used to tell whether a server's already-downloaded Workshop content is
        stale relative to Steam's last-updated time -- a ground-truth signal
        that works on the very first check and self-corrects after a restart
        re-downloads the content, unlike comparing against a remembered
        "last checked" timestamp.
        """
        with self._lock:
            client = self._client()
            return self._newest_mtime_recursive(client, path, 0, max_depth)

    def _newest_mtime_recursive(self, client, path, depth, max_depth) -> int | None:
        if depth > max_depth:
            return None
        try:
            entries = client.listdir_attr(path)
        except OSError:
            return None
        newest: int | None = None
        for attr in entries:
            full_path = f"{path.rstrip('/')}/{attr.filename}"
            mode = attr.st_mode or 0
            if stat.S_ISDIR(mode):
                candidate = self._newest_mtime_recursive(client, full_path, depth + 1, max_depth)
            else:
                candidate = attr.st_mtime
            if candidate is not None and (newest is None or candidate > newest):
                newest = candidate
        return newest

    def file_exists(self, path: str) -> bool:
        with self._lock:
            try:
                self._client().stat(path)
                return True
            except OSError:
                return False

    def chmod(self, path: str, mode: int) -> None:
        with self._lock:
            self._client().chmod(path, mode)

    def chmod_recursive(self, path: str, mode: int) -> None:
        with self._lock:
            self._chmod_recursive(self._client(), path, mode)

    def _chmod_recursive(self, client: paramiko.SFTPClient, path: str, mode: int) -> None:
        client.chmod(path, mode)
        attr = client.stat(path)
        if stat.S_ISDIR(attr.st_mode or 0):
            for child in client.listdir_attr(path):
                self._chmod_recursive(client, f"{path.rstrip('/')}/{child.filename}", mode)

    def delete_dir(self, path: str) -> None:
        """Recursively delete a remote directory tree."""
        with self._lock:
            self._delete_dir_recursive(self._client(), path)

    def _delete_dir_recursive(self, client: paramiko.SFTPClient, path: str) -> None:
        for attr in client.listdir_attr(path):
            child = f"{path.rstrip('/')}/{attr.filename}"
            if stat.S_ISDIR(attr.st_mode or 0):
                self._delete_dir_recursive(client, child)
            else:
                client.remove(child)
        client.rmdir(path)

    def ensure_dir(self, path: str, mode: int = 0o755) -> None:
        """Create `path` if it doesn't already exist (its parent must)."""
        with self._lock:
            self._ensure_remote_dir(self._client(), path, mode)

    def copy_dir(self, src_path: str, dst_path: str, on_file_copied=None) -> int:
        """Recursively copy a remote directory tree to a new path.

        SFTP has no server-side copy -- each file is streamed through this
        process (read in chunks, then written back out over the same
        connection) rather than moved by the server in place. Fine for a
        save-folder-sized tree, but not a cheap metadata-only op like
        rename()/delete_dir(). Returns the number of files copied.

        `on_file_copied`, if given, is called with the running total after
        every file (from whatever thread called copy_dir -- callers doing UI
        work in it must marshal back to the UI thread themselves). Exists so
        a caller waiting on this can tell "still actively copying" apart
        from "silently stuck" instead of seeing nothing until it returns.
        """
        with self._lock:
            count_box = [0]
            self._copy_dir_recursive(self._client(), src_path, dst_path, on_file_copied, count_box)
            return count_box[0]

    def _copy_dir_recursive(self, client: paramiko.SFTPClient, src_path: str, dst_path: str, on_file_copied, count_box: list[int]) -> None:
        src_mode = client.stat(src_path).st_mode or 0
        self._ensure_remote_dir(client, dst_path, src_mode & 0o777 or 0o755)
        for attr in client.listdir_attr(src_path):
            src_child = f"{src_path.rstrip('/')}/{attr.filename}"
            dst_child = f"{dst_path.rstrip('/')}/{attr.filename}"
            mode = attr.st_mode or 0
            if stat.S_ISDIR(mode):
                self._copy_dir_recursive(client, src_child, dst_child, on_file_copied, count_box)
            else:
                with client.open(src_child, "rb") as src_fh, client.open(dst_child, "wb") as dst_fh:
                    while True:
                        chunk = src_fh.read(1024 * 1024)
                        if not chunk:
                            break
                        dst_fh.write(chunk)
                client.chmod(dst_child, mode & 0o777)
                count_box[0] += 1
                if on_file_copied is not None:
                    on_file_copied(count_box[0])

    @staticmethod
    def _ensure_remote_dir(client: paramiko.SFTPClient, path: str, mode: int = 0o755) -> None:
        try:
            client.stat(path)
        except FileNotFoundError:
            client.mkdir(path, mode)
            client.chmod(path, mode)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a single file, then mirror its local permission bits.

        paramiko's put() always creates the remote file with the server's
        default permissions (typically 644), regardless of the local file's
        mode -- so an executable script/binary loses its +x bit on upload.
        Re-applying the local mode afterwards is what lets Pterodactyl
        actually run uploaded executables.
        """
        with self._lock:
            client = self._client()
            client.put(local_path, remote_path)
            client.chmod(remote_path, os.stat(local_path).st_mode & 0o777)

    def upload_dir(self, local_dir: str, remote_dir: str, on_file_uploaded=None) -> int:
        """Recursively upload a local directory tree, preserving permission bits.

        `on_file_uploaded`, if given, is called with the running total after
        every file -- same purpose as copy_dir's on_file_copied. Returns the
        number of files uploaded.
        """
        with self._lock:
            client = self._client()
            count = 0
            for root, _dirs, files in os.walk(local_dir):
                rel = os.path.relpath(root, local_dir)
                remote_root = remote_dir if rel == "." else f"{remote_dir}/{rel.replace(os.sep, '/')}"
                self._ensure_remote_dir(client, remote_root)
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = f"{remote_root}/{name}"
                    client.put(local_path, remote_path)
                    client.chmod(remote_path, os.stat(local_path).st_mode & 0o777)
                    count += 1
                    if on_file_uploaded is not None:
                        on_file_uploaded(count)
            return count
