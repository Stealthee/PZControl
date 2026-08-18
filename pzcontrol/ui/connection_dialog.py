"""Dialog for entering server connection details.

Port fields are pre-filled with the protocol defaults but remain editable --
the placeholder text reminds the user what the default is even if they clear
the field.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from pzcontrol.config import DEFAULT_PTERODACTYL_PORT, DEFAULT_RCON_PORT, DEFAULT_SFTP_PORT, ServerConfig


class ConnectionDialog(QDialog):
    def __init__(self, config: ServerConfig | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Server")
        self.setMinimumWidth(420)
        config = config or ServerConfig()

        layout = QVBoxLayout(self)

        self.name_edit = QLineEdit(config.name)
        layout.addLayout(self._labeled_row("Server name", self.name_edit))

        rcon_box = QGroupBox("RCON (game console)")
        rcon_form = QFormLayout(rcon_box)
        self.rcon_host = QLineEdit(config.rcon_host)
        self.rcon_host.setPlaceholderText("e.g. 192.0.2.10")
        self.rcon_port = QSpinBox()
        self.rcon_port.setRange(1, 65535)
        self.rcon_port.setValue(config.rcon_port or DEFAULT_RCON_PORT)
        self.rcon_port.setSuffix(f"   (default {DEFAULT_RCON_PORT})")
        self.rcon_password = QLineEdit(config.rcon_password)
        rcon_hint = QLabel(
            "RCONPort/RCONPassword from the server's .ini -- if RCONPassword\n"
            "is blank there, it's likely set via a Pterodactyl startup variable\n"
            "instead. Check the Startup tab in Pterodactyl."
        )
        rcon_hint.setWordWrap(True)
        rcon_hint.setStyleSheet("color: palette(placeholder-text);")
        rcon_form.addRow(rcon_hint)
        rcon_form.addRow("Host", self.rcon_host)
        rcon_form.addRow("Port", self.rcon_port)
        rcon_form.addRow("Password", self.rcon_password)
        layout.addWidget(rcon_box)

        ptero_box = QGroupBox("Pterodactyl (panel) -- optional")
        ptero_form = QFormLayout(ptero_box)
        ptero_hint = QLabel(
            "Leave blank to skip. Without it you won't get power controls\n"
            "(start/stop/restart) or live console/chat -- only RCON features\n"
            "(players, teleport, kick, ban) will be available."
        )
        ptero_hint.setWordWrap(True)
        ptero_hint.setStyleSheet("color: palette(placeholder-text);")
        ptero_form.addRow(ptero_hint)
        self.ptero_host = QLineEdit(config.pterodactyl_host)
        self.ptero_host.setPlaceholderText("panel.example.com")
        self.ptero_use_tls = QCheckBox("Use HTTPS")
        self.ptero_use_tls.setChecked(config.pterodactyl_use_tls)
        self.ptero_use_tls.toggled.connect(self._sync_default_port)
        self.ptero_port = QSpinBox()
        self.ptero_port.setRange(1, 65535)
        self.ptero_port.setValue(config.pterodactyl_port or DEFAULT_PTERODACTYL_PORT)
        self.ptero_api_key = QLineEdit(config.pterodactyl_api_key)
        self.ptero_api_key.setPlaceholderText("ptlc_...")
        self.ptero_server_id = QLineEdit(config.pterodactyl_server_id)
        self.ptero_server_id.setPlaceholderText("server identifier, e.g. d3aac109")
        ptero_form.addRow("Host", self.ptero_host)
        ptero_form.addRow(self.ptero_use_tls)
        ptero_form.addRow("Port", self.ptero_port)
        ptero_form.addRow("API key", self.ptero_api_key)
        ptero_form.addRow("Server ID", self.ptero_server_id)
        layout.addWidget(ptero_box)

        sftp_box = QGroupBox("SFTP (file manager) -- optional")
        sftp_form = QFormLayout(sftp_box)
        sftp_hint = QLabel(
            "Leave blank to skip. Found on the panel's server page, e.g.\n"
            "sftp://username.serverid@host:2022 -- use your panel account\n"
            "password here, not the API key. Needed for Server Settings /\n"
            "Sandbox Settings tabs (they edit files over SFTP)."
        )
        sftp_hint.setWordWrap(True)
        sftp_hint.setStyleSheet("color: palette(placeholder-text);")
        sftp_form.addRow(sftp_hint)
        self.sftp_host = QLineEdit(config.sftp_host)
        self.sftp_host.setPlaceholderText("panel.example.com")
        self.sftp_port = QSpinBox()
        self.sftp_port.setRange(1, 65535)
        self.sftp_port.setValue(config.sftp_port or DEFAULT_SFTP_PORT)
        self.sftp_port.setSuffix(f"   (default {DEFAULT_SFTP_PORT})")
        self.sftp_username = QLineEdit(config.sftp_username)
        self.sftp_username.setPlaceholderText("username.serverid")
        self.sftp_password = QLineEdit(config.sftp_password)
        sftp_form.addRow("Host", self.sftp_host)
        sftp_form.addRow("Port", self.sftp_port)
        sftp_form.addRow("Username", self.sftp_username)
        sftp_form.addRow("Password", self.sftp_password)
        layout.addWidget(sftp_box)

        self._update_port_suffix()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _labeled_row(self, label: str, widget) -> QFormLayout:
        form = QFormLayout()
        form.addRow(label, widget)
        return form

    def _sync_default_port(self, use_tls: bool) -> None:
        default = 443 if use_tls else 80
        # Only override if the field still holds the *other* scheme's default,
        # so a user-entered custom port is left alone.
        other_default = 80 if use_tls else 443
        if self.ptero_port.value() == other_default:
            self.ptero_port.setValue(default)
        self._update_port_suffix()

    def _update_port_suffix(self) -> None:
        default = 443 if self.ptero_use_tls.isChecked() else 80
        self.ptero_port.setSuffix(f"   (default {default})")

    def result_config(self) -> ServerConfig:
        return ServerConfig(
            name=self.name_edit.text().strip() or "My Server",
            rcon_host=self.rcon_host.text().strip(),
            rcon_port=self.rcon_port.value(),
            rcon_password=self.rcon_password.text(),
            pterodactyl_host=self.ptero_host.text().strip(),
            pterodactyl_port=self.ptero_port.value(),
            pterodactyl_use_tls=self.ptero_use_tls.isChecked(),
            pterodactyl_api_key=self.ptero_api_key.text().strip(),
            pterodactyl_server_id=self.ptero_server_id.text().strip(),
            sftp_host=self.sftp_host.text().strip(),
            sftp_port=self.sftp_port.value(),
            sftp_username=self.sftp_username.text().strip(),
            sftp_password=self.sftp_password.text(),
        )
