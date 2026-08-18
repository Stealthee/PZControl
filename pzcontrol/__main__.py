import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from pzcontrol import storage
from pzcontrol.ui.main_window import MainWindow
from pzcontrol.ui.server_list_dialog import ServerListDialog

if TYPE_CHECKING:
    from pzcontrol.config import ServerConfig

# Held open for the lifetime of the process so the OS lock stays active.
_lock_fh = None


def _acquire_singleton_lock() -> bool:
    """Cross-platform single-instance guard via an exclusive lock on a temp file.

    fcntl (POSIX) and msvcrt (Windows) lock files in incompatible ways, so only
    the locking call itself branches on platform.
    """
    global _lock_fh
    lock_path = Path(tempfile.gettempdir()) / "pzcontrol.lock"
    fh = open(lock_path, "w+")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.write("locked")
            fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh = fh  # keep reference alive -- releasing it drops the lock
        return True
    except OSError:
        fh.close()
        return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("pzcontrol")
    app.setOrganizationName("pzcontrol")
    # Without this, KDE/GNOME (esp. on Wayland) can't tie the running window
    # back to ~/.local/share/applications/pzcontrol.desktop -- confirmed live
    # that's exactly what was causing the generic fallback icon and no "Pin
    # to Task Manager" option. Must match that .desktop file's basename.
    app.setDesktopFileName("pzcontrol")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / "icon.png")))

    if not _acquire_singleton_lock():
        QMessageBox.information(None, "pzcontrol already running", "pzcontrol is already open.")
        return 0

    servers = storage.load_servers()
    picker = ServerListDialog(servers)
    if picker.exec() != QDialog.DialogCode.Accepted:
        storage.save_servers(picker.servers)
        return 0

    storage.save_servers(picker.servers)
    config = picker.chosen_config()
    if config is None:
        return 0

    servers = picker.servers

    def save_config(updated: "ServerConfig") -> None:
        for i, s in enumerate(servers):
            if s.name == updated.name and s.rcon_host == updated.rcon_host:
                servers[i] = updated
                break
        storage.save_servers(servers)

    app_settings = storage.load_app_settings()

    window = MainWindow(
        config,
        save_config_callback=save_config,
        app_settings=app_settings,
        save_app_settings_callback=storage.save_app_settings,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
