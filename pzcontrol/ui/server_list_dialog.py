"""Startup dialog: pick a saved server, or add/edit/remove profiles."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from pzcontrol.config import ServerConfig
from pzcontrol.ui.connection_dialog import ConnectionDialog


class ServerListDialog(QDialog):
    AUTO_CONNECT_SECONDS = 5

    def __init__(self, servers: list[ServerConfig], parent=None):
        super().__init__(parent)
        self.setWindowTitle("pzcontrol -- Select a Server")
        self.setMinimumSize(380, 320)
        self.servers = list(servers)
        self._chosen: ServerConfig | None = None
        self._countdown_timer: QTimer | None = None
        self._countdown_seconds = 0

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Saved servers:"))

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._connect())
        self.list_widget.itemClicked.connect(lambda _item: self._stop_countdown())
        layout.addWidget(self.list_widget, 1)

        self.countdown_label = QLabel("")
        self.countdown_label.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(self.countdown_label)

        self._reload_list()

        buttons = QHBoxLayout()
        connect_btn = QPushButton("Connect")
        connect_btn.setDefault(True)
        connect_btn.clicked.connect(self._connect)
        default_btn = QPushButton("Set as Default")
        default_btn.clicked.connect(self._toggle_default)
        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(self._add)
        edit_btn = QPushButton("Edit...")
        edit_btn.clicked.connect(self._edit)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove)
        for b in (connect_btn, default_btn, add_btn, edit_btn, remove_btn):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        self._maybe_start_countdown()

    # -- helpers ---------------------------------------------------------------

    def _reload_list(self, select_index: int | None = None) -> None:
        self.list_widget.clear()
        for server in self.servers:
            detail = server.rcon_host or server.pterodactyl_host or "(no connection info)"
            star = "★ " if server.is_default else ""
            item = QListWidgetItem(f"{star}{server.name}  --  {detail}")
            self.list_widget.addItem(item)
        if select_index is not None and 0 <= select_index < self.list_widget.count():
            self.list_widget.setCurrentRow(select_index)
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def chosen_config(self) -> ServerConfig | None:
        return self._chosen

    # -- auto-connect countdown --------------------------------------------------

    def _maybe_start_countdown(self) -> None:
        default_index = next((i for i, s in enumerate(self.servers) if s.is_default), None)
        if default_index is None:
            return
        self.list_widget.setCurrentRow(default_index)
        self._countdown_seconds = self.AUTO_CONNECT_SECONDS
        self._update_countdown_label()
        timer = QTimer(self)
        timer.timeout.connect(self._tick_countdown)
        timer.start(1000)
        self._countdown_timer = timer

    def _tick_countdown(self) -> None:
        self._countdown_seconds -= 1
        if self._countdown_seconds <= 0:
            self._stop_countdown()
            self._connect()
            return
        self._update_countdown_label()

    def _update_countdown_label(self) -> None:
        row = self.list_widget.currentRow()
        name = self.servers[row].name if 0 <= row < len(self.servers) else ""
        self.countdown_label.setText(
            f"Connecting to '{name}' in {self._countdown_seconds}s... (click a server to cancel)"
        )

    def _stop_countdown(self) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self.countdown_label.setText("")

    # -- actions ----------------------------------------------------------------

    def _connect(self) -> None:
        self._stop_countdown()
        row = self.list_widget.currentRow()
        if row < 0:
            QMessageBox.information(self, "No server selected", "Add a server first, or select one from the list.")
            return
        self._chosen = self.servers[row]
        self.accept()

    def _toggle_default(self) -> None:
        self._stop_countdown()
        row = self.list_widget.currentRow()
        if row < 0:
            return
        target = self.servers[row]
        target.is_default = not target.is_default
        if target.is_default:
            for i, server in enumerate(self.servers):
                if i != row:
                    server.is_default = False
        self._reload_list(select_index=row)

    def _add(self) -> None:
        self._stop_countdown()
        dialog = ConnectionDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.servers.append(dialog.result_config())
            self._reload_list(select_index=len(self.servers) - 1)

    # Fields ConnectionDialog's form actually edits -- everything else on
    # ServerConfig (known players, autorestart, broadcasts, is_default,
    # discovered ini/lua paths, ...) must survive an edit untouched.
    _CONNECTION_FIELDS = (
        "name",
        "rcon_host", "rcon_port", "rcon_password",
        "pterodactyl_host", "pterodactyl_port", "pterodactyl_use_tls",
        "pterodactyl_api_key", "pterodactyl_server_id",
        "sftp_host", "sftp_port", "sftp_username", "sftp_password",
    )

    def _edit(self) -> None:
        self._stop_countdown()
        row = self.list_widget.currentRow()
        if row < 0:
            return
        existing = self.servers[row]
        dialog = ConnectionDialog(existing, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            edited = dialog.result_config()
            changes = {field: getattr(edited, field) for field in self._CONNECTION_FIELDS}
            self.servers[row] = replace(existing, **changes)
            self._reload_list(select_index=row)

    def _remove(self) -> None:
        self._stop_countdown()
        row = self.list_widget.currentRow()
        if row < 0:
            return
        server = self.servers[row]
        if QMessageBox.question(self, "Remove server", f"Remove '{server.name}' from the list?") != QMessageBox.StandardButton.Yes:
            return
        del self.servers[row]
        self._reload_list(select_index=min(row, len(self.servers) - 1))
