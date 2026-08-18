"""Main application window: player list, ban list, live console/chat, power controls."""

from __future__ import annotations

import os
import re
import textwrap
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QSettings, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from pzcontrol import ban_db
from pzcontrol.config import DEFAULT_JOIN_MESSAGE, DEFAULT_RESTART_WARNING_MESSAGE, AppSettings, ServerConfig
from pzcontrol.game_update import GameUpdateError, InstalledBuild, LatestBuild, MANIFEST_PATH, fetch_latest_build, parse_installed_build
from pzcontrol.ini_config import IniProperty, apply_property_changes as apply_ini_changes, parse_properties as parse_ini_properties
from pzcontrol.pterodactyl_client import ConsoleStream, PterodactylClient, PterodactylError
from pzcontrol.rcon_client import RconClient, RconError
from pzcontrol.sandbox_lua import LuaProperty, apply_property_changes as apply_lua_changes, parse_properties as parse_lua_properties
from pzcontrol.sftp_client import FileEntry, SftpClient, SftpError
from pzcontrol.steam_workshop import (
    WorkshopItem,
    WorkshopSearchResult,
    fetch_details as fetch_workshop_details,
    parse_mod_ids_from_description,
    search as search_workshop,
)

# Confirmed live: PZ logs this exact line (Mod-category WARN) during boot for
# every ID in Mods= it couldn't actually load -- whether that's because the ID
# itself doesn't exist in any downloaded Workshop item, or because a mod it
# requires is missing. Doesn't distinguish those cases, just "didn't load".
_REQUIRED_MOD_NOT_FOUND_RE = re.compile(r'required mod "([^"]+)" not found', re.IGNORECASE)
_MOD_INFO_ID_RE = re.compile(r"^id=(.+)$", re.IGNORECASE | re.MULTILINE)
_MOD_INFO_REQUIRE_RE = re.compile(r"^require=(.+)$", re.IGNORECASE | re.MULTILINE)
# Same "\"-prefix quirk as require= (see _MOD_INFO_REQUIRE_RE) -- marks
# alternate/exclusive submod variants within one Workshop item (e.g. a
# "Hard" difficulty version vs the normal one). A mod.info declaring this
# is telling us its own id= and the listed id(s) are mutually exclusive by
# design, not that the other one failed to load.
_MOD_INFO_INCOMPATIBLE_RE = re.compile(r"^incompatible=(.+)$", re.IGNORECASE | re.MULTILINE)
_WORKSHOP_APP_ID = "108600"

# Belt-and-suspenders for the Mods (Mod IDs) box: parse_mod_ids_from_description
# already avoids capturing these, but the box is also free-typed/pasted, and a
# Mod ID copied straight off a Workshop page's BBCode description (e.g.
# "KRSolarOS[/h1]") is a real, confirmed-live way to end up with a Mods=
# entry that can never match a real mod folder. Strip bracket tags out at
# Save time rather than trusting every path that can populate this box.
_MOD_ID_BBCODE_RE = re.compile(r"\[/?[A-Za-z0-9=,\s#]*\]")

# Slack for comparing a Workshop item's downloaded-content mtime against
# Steam's time_updated -- avoids false "Updated!" flags from ordinary clock
# skew between Steam's servers and the game server's filesystem.
_MTIME_STALENESS_TOLERANCE_SECONDS = 60

# Keys whose values are passed as Pterodactyl startup variables, overriding
# whatever is written in the .ini -- confirmed live that RCONPassword ships
# blank in the file itself, so it's almost certainly injected this way, same
# pattern as 7 Days to Die's TelnetPassword override. Add more here if the
# egg's startup command turns out to override other keys too.
_INI_STARTUP_OVERRIDES: frozenset[str] = frozenset({"RCONPassword"})

# These have their own dedicated editor (the Mods tab) with a friendlier
# one-ID-per-line UI -- shown here read-only so they're still visible/searchable
# from Server Settings, but edits happen in the place built for them.
_INI_MODS_TAB_FIELDS: frozenset[str] = frozenset({"WorkshopItems", "Mods"})

# "user" (i.e. normal player, no admin) isn't advertised by RCON's own `help`
# text for setaccesslevel -- that only lists the privileged levels -- but it's
# confirmed live to work anyway: the command accepts any name from the
# server's `role` table, and "user" is the row a fresh, non-admin player has.
# Listed first so the dialog defaults to the safe "no admin" choice rather
# than defaulting to granting admin on a stray Enter press.
_ACCESS_LEVELS = ("user", "admin", "moderator", "overseer", "gm", "observer")

# The live multiplayer save, and where the Backup tab keeps as many backup
# copies as the user wants -- each one its own timestamped folder under a
# "Backups" container one directory back from the save itself, i.e. still
# under Saves/. Same "chroot or not" ambiguity as _SERVER_DIR_CANDIDATES
# further down (confirmed live: some Pterodactyl nodes expose the SFTP
# account chrooted straight to the container, others show "home/container"
# as a real folder in the listing) -- resolved at use time by checking which
# candidate actually exists rather than assumed, so a backup/restore/reset
# never silently no-ops against a path that isn't there.
_SAVE_DIR_CANDIDATES = ("/.cache/Saves/Multiplayer", "/home/container/.cache/Saves/Multiplayer")
# Paired 1:1 with _SAVE_DIR_CANDIDATES -- kept as its own candidate list
# (rather than always derived from whichever save-dir candidate resolves)
# because the live save folder can legitimately not exist yet (server never
# started, or just Reset) while backups from an earlier session still do --
# resolving backups_dir from "whichever root has a Backups folder already"
# keeps delete/restore/list pointed at the same place a backup was actually
# created in, instead of flipping roots based on unrelated live-save state.
_BACKUPS_DIR_CANDIDATES = ("/.cache/Saves/Backups", "/home/container/.cache/Saves/Backups")
_BACKUP_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


class _Bridge(QObject):
    """Marshals results from background threads back onto the UI thread."""

    result = Signal(str, object)
    error = Signal(str, str)


class _FocusSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _FocusDoubleSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _FocusComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _NumericSortTableWidgetItem(QTableWidgetItem):
    """Sorts by a separate numeric key instead of the displayed text.

    QTableWidgetItem treats Qt.EditRole and Qt.DisplayRole as the same slot --
    confirmed live that setData(EditRole, ...) silently overwrites the visible
    text too. Overriding __lt__ instead keeps display text and sort order
    fully independent.
    """

    def __init__(self, text: str, sort_key: int):
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other):
        if isinstance(other, _NumericSortTableWidgetItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


class BanDialog(QDialog):
    def __init__(self, default_identifier: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ban player")
        form = QFormLayout(self)
        self.identifier_edit = QLineEdit(default_identifier)
        self.kind_box = QComboBox()
        self.kind_box.addItems(["Username", "SteamID"])
        form.addRow("Identifier", self.identifier_edit)
        form.addRow("Type", self.kind_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def identifier(self) -> str:
        return self.identifier_edit.text().strip()

    def by_steamid(self) -> bool:
        return self.kind_box.currentText() == "SteamID"


class GiveItemDialog(QDialog):
    def __init__(self, player_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Give item to {player_name}")
        form = QFormLayout(self)
        self.item_edit = QLineEdit()
        self.item_edit.setPlaceholderText("e.g. Base.Axe")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(1)
        form.addRow("Item (full type)", self.item_edit)
        form.addRow("Count", self.count_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def item(self) -> str:
        return self.item_edit.text().strip()

    def count(self) -> int:
        return self.count_spin.value()


class AccessLevelDialog(QDialog):
    def __init__(self, player_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Set access level for {player_name}")
        form = QFormLayout(self)
        self.level_box = QComboBox()
        self.level_box.addItems(_ACCESS_LEVELS)
        form.addRow("Access level", self.level_box)
        hint = QLabel("\"user\" is a normal player -- pick it to remove admin/moderator/etc.")
        hint.setStyleSheet("color: palette(placeholder-text);")
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def level(self) -> str:
        return self.level_box.currentText()


class MainWindow(QMainWindow):
    PLAYER_REFRESH_INTERVAL_MS = 15_000
    AUTO_RESTART_CHECK_INTERVAL_MS = 5_000
    RCON_RECONNECT_INTERVAL_MS = 10_000
    SFTP_RECONNECT_INTERVAL_MS = 10_000
    SFTP_HEALTH_CHECK_INTERVAL_MS = 15_000
    RESTART_WARNING_SECONDS = 300
    RESTART_FINAL_COUNTDOWN_SECONDS = 20

    def __init__(self, config: ServerConfig, save_config_callback=None, app_settings: AppSettings | None = None, save_app_settings_callback=None):
        super().__init__()
        self.config = config
        self._save_config_callback = save_config_callback
        self.app_settings = app_settings or AppSettings()
        self._save_app_settings_callback = save_app_settings_callback
        self.setWindowTitle(f"pzcontrol -- {config.name}")
        self.resize(1000, 650)
        self._restore_window_geometry()

        self._rcon: RconClient | None = None
        self._ptero: PterodactylClient | None = None
        self._console: ConsoleStream | None = None
        self._sftp: SftpClient | None = None

        self._players: list[str] = []
        # name -> {"name": str, "last_seen": str}, so the Players tab can keep
        # showing people (as "Offline") after they disconnect. Unlike 7 Days to
        # Die's telnet, PZ's RCON `players` gives us no steamid -- name is the
        # only identifier available, so history is keyed by name.
        self._known_players: dict[str, dict] = {p["name"]: dict(p) for p in config.known_players if p.get("name")}
        # None until the first player-list poll, so everyone already online
        # when the app connects doesn't trigger a "just joined" broadcast.
        self._known_online_names: set[str] | None = None

        self._bans: list[ban_db.BanEntry] = []

        self._sftp_cwd = "/"
        self._sftp_entries: list[FileEntry] = []
        self._sftp_open_path: str | None = None
        self._sftp_dirty = False
        self._sftp_loading = False

        # Which _SAVE_DIR_CANDIDATES entry actually exists on this server's
        # SFTP account -- set by the first backup-status check, used to show
        # real paths in confirmation dialogs before the user commits to an
        # action. Falls back to the first candidate until then.
        self._resolved_save_dir = _SAVE_DIR_CANDIDATES[0]
        self._resolved_backups_dir = self._backups_dir_for(self._resolved_save_dir)
        self._backup_busy_base_message = ""

        self._ini_text: str | None = None
        self._ini_properties: list[IniProperty] = []
        self._ini_widgets: dict[str, QWidget] = {}
        self._ini_dirty = False
        self._ini_loading = False

        self._lua_text: str | None = None
        self._lua_properties: list[LuaProperty] = []
        self._lua_widgets: dict[str, QWidget] = {}
        self._lua_dirty = False
        self._lua_loading = False

        # Mods tab reads/writes the same ini text as the Server Settings tab
        # (WorkshopItems/Mods are just two of its keys) -- no separate load.
        self._mods_dirty = False
        self._mods_loading = False
        # Populated by the last Check for Updates -- {workshop_id: [missing mod ID, ...]}
        # for "Not working" items, so the right-click menu can offer to search
        # for whatever's missing.
        self._workshop_missing_deps: dict[str, list[str]] = {}
        # Populated by the last Check for Updates -- {workshop_id: [mod ID(s)
        # this item's downloaded files declare but aren't in the Mods= list yet]}.
        self._workshop_missing_mod_ids: dict[str, list[str]] = {}
        # Populated by the last Check for Updates -- {workshop_id: {mod_id, ...}}
        # -- used by drag-drop reordering to know which Mod IDs travel
        # together with which Workshop item.
        self._workshop_mod_id_map: dict[str, set[str]] = {}
        # Populated by the last Check for Updates -- workshop_ids currently
        # flagged "Not working" (before dismissal is applied), so the
        # right-click menu knows whether "Mark OK until next update" applies.
        self._workshop_not_working: set[str] = set()
        # Populated by the last Check for Updates -- {workshop_id: time_updated}
        # so a later "Mark OK until next update" click knows exactly which
        # Steam timestamp to pin the dismissal to.
        self._workshop_time_updated: dict[str, int | None] = {}
        # Cache of the last Check for Updates result tuple, so toggling a
        # dismissal can re-render the table locally without a fresh network
        # round-trip to Steam/the console log.
        self._last_workshop_check_result: tuple | None = None

        # Red/green flashing notice on the power bar, shown regardless of
        # which tab is active, when a Workshop Update Check finds a mod
        # that's changed since the last check (needs a reboot to pick up).
        # Cleared by clicking it or by sending a Restart.
        self._mod_update_flash_timer: QTimer | None = None
        self._mod_update_flash_state = False

        # Ground truth for the "auto-restart when an update is available and
        # the server is empty" rule -- reflects only active (non-frozen) mods,
        # since a frozen mod having an update doesn't affect join
        # compatibility. `_mod_update_restart_in_progress` latches once the
        # rule fires so it can't refire every 15s while the server is down
        # for that same restart -- cleared once a later check confirms the
        # update is actually resolved (both this AND the server build check
        # below have to be clear -- see _clear_update_restart_guard_if_resolved).
        self._active_mod_update_pending = False
        self._mod_update_restart_in_progress = False
        # Broader than _active_mod_update_pending above -- includes frozen
        # mods too, since a frozen mod having an update is still worth
        # flashing about (a reminder it might be worth unfreezing) even
        # though it doesn't drive auto-restart. Combined with
        # _server_build_update_pending below in _refresh_update_flash.
        self._any_mod_update_for_flash = False

        # Same idea as the mod-update tracking above, but for the dedicated
        # server binary itself (steamapps/appmanifest_380870.acf's buildid
        # vs. Steam's latest for that branch) -- feeds the same "auto-restart
        # when empty" rule, since AUTO_UPDATE=1 on the egg means a restart is
        # all it takes to pick up a new build, exactly like a changed mod.
        self._server_build_update_pending = False
        self._installed_server_build: InstalledBuild | None = None
        self._latest_server_build: LatestBuild | None = None

        # Re-runs the Workshop Update Check on the interval configured on
        # the Mods tab, when enabled -- otherwise it only ever runs on a
        # button click (or a freeze/unfreeze/remove action).
        self._mod_auto_check_timer: QTimer | None = None

        self._rcon_status_dot: QLabel | None = None
        self._rcon_reconnect_timer: QTimer | None = None

        self._sftp_status_dot: QLabel | None = None
        self._sftp_reconnect_timer: QTimer | None = None
        self._sftp_health_timer: QTimer | None = None

        self._ptero_status: str = ""

        self._next_autorestart_at: float | None = None
        self._restart_countdown_active = False
        self._restart_countdown_timer: QTimer | None = None
        self._restart_button: QPushButton | None = None
        self._start_button: QPushButton | None = None
        self._stop_button: QPushButton | None = None
        self._restart_seconds_remaining = 0
        self._restart_warned_one_minute = False

        self._bridge = _Bridge()
        self._bridge.result.connect(self._on_async_result)
        self._bridge.error.connect(self._on_async_error)

        self._build_ui()
        self._connect_backends()

        self._player_refresh_timer = QTimer(self)
        self._player_refresh_timer.timeout.connect(self.refresh_players)
        self._player_refresh_timer.start(self.PLAYER_REFRESH_INTERVAL_MS)

        self._autorestart_timer = QTimer(self)
        self._autorestart_timer.timeout.connect(self._check_autorestart)
        self._autorestart_timer.start(self.AUTO_RESTART_CHECK_INTERVAL_MS)

        self._apply_mod_auto_check_timer()

    # -- window geometry ----------------------------------------------------------

    def _restore_window_geometry(self) -> None:
        geometry = QSettings("pzcontrol", "pzcontrol").value("main_window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def closeEvent(self, event) -> None:
        QSettings("pzcontrol", "pzcontrol").setValue("main_window/geometry", self.saveGeometry())
        super().closeEvent(event)

    # -- UI construction --------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addLayout(self._build_power_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        splitter.addWidget(self._build_left_tabs())
        splitter.addWidget(self._build_console_panel())
        splitter.setSizes([550, 450])

        self.statusBar().showMessage("Connecting...")

    def _build_power_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"<b>{self.config.name}</b>"))

        if self.config.rcon_host:
            self._rcon_status_dot = self._add_status_indicator(bar, "RCON")
            self._set_rcon_status(False)

        if self.config.sftp_host:
            self._sftp_status_dot = self._add_status_indicator(bar, "SFTP")
            self._set_sftp_status(False)

        bar.addStretch(1)

        self._mod_update_notice_btn = QPushButton()
        self._mod_update_notice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mod_update_notice_btn.clicked.connect(self._dismiss_mod_update_notice)
        bar.addWidget(self._mod_update_notice_btn)
        self._set_mod_update_notice_idle()

        bar.addStretch(1)

        has_ptero = bool(self.config.pterodactyl_host)
        no_ptero_tip = "Requires a Pterodactyl connection (not configured for this server)"

        self.power_buttons: list[QPushButton] = []
        for label, action in (("Start", "start"), ("Restart", "restart"), ("Stop", "stop"), ("Kill", "kill")):
            btn = QPushButton(label)
            if action == "restart":
                btn.clicked.connect(self._on_restart_clicked)
                self._restart_button = btn
            else:
                btn.clicked.connect(lambda _checked=False, a=action: self._send_power_action(a))
            if action == "start":
                self._start_button = btn
            elif action == "stop":
                self._stop_button = btn
            if not has_ptero:
                btn.setEnabled(False)
                btn.setToolTip(no_ptero_tip)
            bar.addWidget(btn)
            self.power_buttons.append(btn)

        if not has_ptero:
            note = QLabel("(power controls need Pterodactyl)")
            note.setStyleSheet("color: palette(placeholder-text);")
            bar.addWidget(note)

        has_rcon = bool(self.config.rcon_host)
        save_btn = QPushButton("Save World")
        save_btn.clicked.connect(self._save_world)
        if not has_rcon:
            save_btn.setEnabled(False)
            save_btn.setToolTip("Requires an RCON connection")
        bar.addWidget(save_btn)

        return bar

    @staticmethod
    def _add_status_indicator(bar: QHBoxLayout, label: str) -> QLabel:
        dot = QLabel()
        dot.setFixedWidth(14)
        bar.addWidget(dot)
        bar.addWidget(QLabel(label))
        return dot

    @staticmethod
    def _paint_status_dot(dot: QLabel, connected: bool, label: str) -> None:
        color = "#2ecc71" if connected else "#e74c3c"
        dot.setText("●")
        dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        dot.setToolTip(f"{label} {'connected' if connected else 'disconnected'}")

    def _set_mod_update_notice_idle(self) -> None:
        # Resting state: a steady green "Up To Date" badge whenever the
        # feature's on, so the bar always shows *some* status rather than
        # only appearing once something's wrong -- hidden entirely when the
        # Mods tab checkbox has the feature turned off.
        self._mod_update_notice_btn.setText("Mods: Up To Date")
        self._mod_update_notice_btn.setToolTip("No mod or server build updates found by the last check")
        self._mod_update_notice_btn.setStyleSheet(
            "background-color: #2ecc71; color: white; font-weight: bold; padding: 3px 12px; border-radius: 4px;"
        )
        self._mod_update_notice_btn.setVisible(self.config.mod_update_flash_enabled)

    def _refresh_update_flash(self) -> None:
        """Single source of truth for the flash notice's on/off state --
        called after every mod check AND every server build check, since
        either one alone flipping to "clear" must not stomp on the other
        still having a pending update (each used to drive the flash
        independently, which meant whichever check ran last won)."""
        if self._any_mod_update_for_flash and self._server_build_update_pending:
            self._start_mod_update_flash("Mod + server update -- restart needed")
        elif self._any_mod_update_for_flash:
            self._start_mod_update_flash("Mod update -- restart needed")
        elif self._server_build_update_pending:
            self._start_mod_update_flash("Server update -- restart needed")
        else:
            # Nothing pending from either check -- clear any flash left over
            # from a previous one, otherwise it keeps flashing forever even
            # after everything's back to "Working"/"Up To Date".
            self._stop_mod_update_flash()

    def _start_mod_update_flash(self, message: str = "Mod update -- restart needed") -> None:
        if not self.config.mod_update_flash_enabled:
            return
        self._mod_update_notice_btn.setText(message)
        self._mod_update_notice_btn.setToolTip("Click to dismiss")
        self._mod_update_notice_btn.setVisible(True)
        if self._mod_update_flash_timer is None:
            timer = QTimer(self)
            timer.timeout.connect(self._tick_mod_update_flash)
            self._mod_update_flash_timer = timer
        if not self._mod_update_flash_timer.isActive():
            self._mod_update_flash_timer.start(500)
        self._tick_mod_update_flash()

    def _tick_mod_update_flash(self) -> None:
        self._mod_update_flash_state = not self._mod_update_flash_state
        color = "#e74c3c" if self._mod_update_flash_state else "#2ecc71"
        self._mod_update_notice_btn.setStyleSheet(
            f"background-color: {color}; color: white; font-weight: bold; padding: 3px 12px; border-radius: 4px;"
        )

    def _dismiss_mod_update_notice(self) -> None:
        was_flashing = self._mod_update_flash_timer is not None and self._mod_update_flash_timer.isActive()
        self._stop_mod_update_flash()
        if was_flashing:
            self.statusBar().showMessage("Mod update notice dismissed", 3000)

    def _stop_mod_update_flash(self) -> None:
        if self._mod_update_flash_timer is not None:
            self._mod_update_flash_timer.stop()
        self._set_mod_update_notice_idle()

    def _build_left_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_player_table(), "Players")
        tabs.addTab(self._build_ban_panel(), "Banned")
        tabs.addTab(self._build_files_panel(), "Files")
        tabs.addTab(self._build_backup_panel(), "Backup")
        tabs.addTab(self._build_ini_settings_panel(), "Server Settings")
        tabs.addTab(self._build_lua_settings_panel(), "Sandbox Settings")
        tabs.addTab(self._build_autorestart_panel(), "Auto Restart")
        tabs.addTab(self._build_broadcasts_panel(), "Broadcasts")
        tabs.addTab(self._build_mods_panel(), "Mods")
        self._browse_mods_tab = self._build_workshop_browse_panel()
        tabs.addTab(self._browse_mods_tab, "Browse Mods")
        tabs.setMovable(True)
        self._left_tabs = tabs
        self._apply_saved_tab_order()
        tabs.tabBar().tabMoved.connect(self._save_tab_order)
        return tabs

    def _apply_saved_tab_order(self) -> None:
        order = self.app_settings.tab_order
        tabs = self._left_tabs
        for target_index, name in enumerate(order):
            current_index = next((i for i in range(tabs.count()) if tabs.tabText(i) == name), None)
            if current_index is not None and current_index != target_index:
                tabs.tabBar().moveTab(current_index, target_index)

    def _save_tab_order(self, *_args) -> None:
        tabs = self._left_tabs
        self.app_settings.tab_order = [tabs.tabText(i) for i in range(tabs.count())]
        if self._save_app_settings_callback:
            self._save_app_settings_callback(self.app_settings)

    # -- players tab --------------------------------------------------------------

    def _build_player_table(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        note = QLabel(
            "PZ's RCON `players` only returns names -- no SteamID, position, or "
            "ping like the 7 Days to Die console gave us."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(note)

        self.player_table = QTableWidget(0, 3)
        self.player_table.setHorizontalHeaderLabels(["Name", "Status", "Last Seen"])
        self.player_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.player_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.player_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.player_table.customContextMenuRequested.connect(self._show_player_menu)
        header = self.player_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 70)
        header.resizeSection(2, 140)
        layout.addWidget(self.player_table)

        buttons_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh players")
        refresh_btn.clicked.connect(self.refresh_players)
        buttons_row.addWidget(refresh_btn)
        buttons_row.addStretch(1)
        clear_history_btn = QPushButton("Clear Player History")
        clear_history_btn.setToolTip("Remove everyone from the offline player list -- use after a server wipe.")
        clear_history_btn.clicked.connect(self._clear_player_history)
        buttons_row.addWidget(clear_history_btn)
        layout.addLayout(buttons_row)
        return wrapper

    def refresh_players(self) -> None:
        if self._rcon:
            self._run_async("list_players", self._rcon.list_players)

    def _populate_player_table(self) -> None:
        online = set(self._players)
        names = sorted(set(self._known_players) | online, key=str.lower)
        self.player_table.setRowCount(len(names))
        for row, name in enumerate(names):
            is_online = name in online
            name_item = QTableWidgetItem(name)
            status_item = QTableWidgetItem("Online" if is_online else "Offline")
            if not is_online:
                for item in (name_item, status_item):
                    item.setForeground(Qt.GlobalColor.gray)
            last_seen = self._known_players.get(name, {}).get("last_seen", "")
            last_seen_item = QTableWidgetItem(last_seen if not is_online else "now")
            self.player_table.setItem(row, 0, name_item)
            self.player_table.setItem(row, 1, status_item)
            self.player_table.setItem(row, 2, last_seen_item)

    def _update_known_players(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        current = set(self._players)
        self._check_join_broadcast(current)
        for name in current:
            self._known_players[name] = {"name": name, "last_seen": now}
        self._save_known_players()

    def _save_known_players(self) -> None:
        self.config.known_players = list(self._known_players.values())
        if self._save_config_callback:
            self._save_config_callback(self.config)

    def _clear_player_history(self) -> None:
        if QMessageBox.question(self, "Clear history", "Remove everyone from the offline player list?") != QMessageBox.StandardButton.Yes:
            return
        self._known_players = {name: data for name, data in self._known_players.items() if name in self._players}
        self._save_known_players()
        self._populate_player_table()

    def _remove_known_player(self, name: str) -> None:
        self._known_players.pop(name, None)
        self._save_known_players()
        self._populate_player_table()

    def _check_join_broadcast(self, current: set[str]) -> None:
        if self._known_online_names is not None and self.config.broadcast_join_enabled:
            for name in current - self._known_online_names:
                self._broadcast_message(self.config.broadcast_join_message.format(name=name))
        self._known_online_names = current

    def _show_player_menu(self, pos) -> None:
        row = self.player_table.rowAt(pos.y())
        item = self.player_table.item(row, 0)
        if row < 0 or item is None:
            return
        name = item.text()
        is_online = name in self._players
        menu = QMenu(self)

        if is_online:
            teleport_menu = menu.addMenu(f"Teleport '{name}' to")
            others = [p for p in self._players if p != name]
            if others:
                for other in others:
                    action = teleport_menu.addAction(other)
                    action.triggered.connect(lambda _checked=False, src=name, dst=other: self._teleport_to_player(src, dst))
            else:
                teleport_menu.addAction("(no other players online)").setEnabled(False)

            menu.addSeparator()
            godmode_on = menu.addAction("Godmode ON")
            godmode_on.triggered.connect(lambda: self._toggle_godmode(name, True))
            godmode_off = menu.addAction("Godmode OFF")
            godmode_off.triggered.connect(lambda: self._toggle_godmode(name, False))
            invis_on = menu.addAction("Invisible ON")
            invis_on.triggered.connect(lambda: self._toggle_invisible(name, True))
            invis_off = menu.addAction("Invisible OFF")
            invis_off.triggered.connect(lambda: self._toggle_invisible(name, False))
            give_action = menu.addAction("Give Item...")
            give_action.triggered.connect(lambda: self._give_item_dialog(name))
            access_action = menu.addAction("Set Access Level...")
            access_action.triggered.connect(lambda: self._set_access_level_dialog(name))

            menu.addSeparator()
            kick_action = menu.addAction("Kick")
            kick_action.triggered.connect(lambda: self._kick_player(name))
        else:
            menu.addAction(f"'{name}' is offline").setEnabled(False)
            menu.addSeparator()

        ban_action = menu.addAction("Ban...")
        ban_action.triggered.connect(lambda: self._ban_dialog_for(name))
        copy_action = menu.addAction("Copy Name")
        copy_action.triggered.connect(lambda: self._copy_to_clipboard(name))
        if not is_online:
            remove_action = menu.addAction("Remove from history")
            remove_action.triggered.connect(lambda: self._remove_known_player(name))

        menu.exec(self.player_table.viewport().mapToGlobal(pos))

    def _copy_to_clipboard(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied '{text}' to clipboard", 3000)

    def _teleport_to_player(self, source: str, target: str) -> None:
        if self._rcon:
            self._run_async("teleport", lambda: self._rcon.teleport_to_player(source, target))

    def _kick_player(self, name: str) -> None:
        if self._rcon:
            self._run_async("kick", lambda: self._rcon.kick(name))

    def _toggle_godmode(self, name: str, enabled: bool) -> None:
        if self._rcon:
            self._run_async("godmode", lambda: self._rcon.toggle_godmode(name, enabled))

    def _toggle_invisible(self, name: str, enabled: bool) -> None:
        if self._rcon:
            self._run_async("invisible", lambda: self._rcon.toggle_invisible(name, enabled))

    def _give_item_dialog(self, name: str) -> None:
        dialog = GiveItemDialog(name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.item() and self._rcon:
            item, count = dialog.item(), dialog.count()
            self._run_async("give_item", lambda: self._rcon.add_item(name, item, count))

    def _set_access_level_dialog(self, name: str) -> None:
        dialog = AccessLevelDialog(name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and self._rcon:
            level = dialog.level()
            self._run_async("set_access_level", lambda: self._rcon.set_access_level(name, level))

    def _ban_dialog_for(self, default_identifier: str) -> None:
        dialog = BanDialog(default_identifier, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.identifier() and self._rcon:
            identifier, by_steamid = dialog.identifier(), dialog.by_steamid()
            self._run_async("ban_add", lambda: self._rcon.ban_add(identifier, by_steamid))

    # -- banned tab -----------------------------------------------------------------

    def _build_ban_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        note = QLabel(
            "Tries an RCON ban-list command first; if this server build doesn't have one, "
            "falls back to reading the ban tables from the admin SQLite database over SFTP."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(note)

        self.ban_table = QTableWidget(0, 3)
        self.ban_table.setHorizontalHeaderLabels(["Identifier", "Type", "Reason"])
        self.ban_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ban_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ban_table.horizontalHeader().setStretchLastSection(True)
        self.ban_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ban_table.customContextMenuRequested.connect(self._show_ban_menu)
        layout.addWidget(self.ban_table)

        buttons = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_bans)
        add_btn = QPushButton("Add ban...")
        add_btn.clicked.connect(lambda: self._ban_dialog_for(""))
        buttons.addWidget(refresh_btn)
        buttons.addWidget(add_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return wrapper

    def refresh_bans(self) -> None:
        self._run_async("bans_load", self._do_bans_load)

    def _do_bans_load(self) -> list[ban_db.BanEntry]:
        if self._rcon:
            raw = self._rcon.list_bans()
            if raw:
                return [ban_db.BanEntry(identifier=line, kind="raw", reason="") for line in raw]
        if self._sftp:
            path = self.config.ban_db_path or ban_db.find_admin_db_path(self._sftp)
            if path:
                if path != self.config.ban_db_path:
                    self.config.ban_db_path = path
                    if self._save_config_callback:
                        self._save_config_callback(self.config)
                return ban_db.read_bans(self._sftp, path)
        return []

    def _populate_ban_table(self, entries: list[ban_db.BanEntry]) -> None:
        self._bans = entries
        self.ban_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.ban_table.setItem(row, 0, QTableWidgetItem(entry.identifier))
            self.ban_table.setItem(row, 1, QTableWidgetItem(entry.kind))
            self.ban_table.setItem(row, 2, QTableWidgetItem(entry.reason))

    def _show_ban_menu(self, pos) -> None:
        row = self.ban_table.rowAt(pos.y())
        if row < 0 or row >= len(self._bans):
            return
        entry = self._bans[row]
        menu = QMenu(self)
        unban_action = menu.addAction("Unban")
        unban_action.triggered.connect(lambda: self._unban(entry))
        copy_action = menu.addAction("Copy identifier")
        copy_action.triggered.connect(lambda: self._copy_to_clipboard(entry.identifier))
        menu.exec(self.ban_table.viewport().mapToGlobal(pos))

    def _unban(self, entry: ban_db.BanEntry) -> None:
        if self._rcon:
            self._run_async("ban_remove", lambda: self._rcon.ban_remove(entry.identifier, entry.kind == "steamid"))

    # -- files tab (generic SFTP browser) --------------------------------------------

    def _build_files_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.sftp_host:
            note = QLabel("SFTP is not configured for this server -- add it in the connection settings to browse and edit files.")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        nav_row = QHBoxLayout()
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(self._sftp_go_up)
        self.sftp_path_edit = QLineEdit(self._sftp_cwd)
        self.sftp_path_edit.setStyleSheet("font-family: monospace;")
        self.sftp_path_edit.setPlaceholderText("/path/to/folder")
        self.sftp_path_edit.returnPressed.connect(self._sftp_navigate_to_typed_path)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: self._sftp_browse(self._sftp_cwd))
        upload_files_btn = QPushButton("Upload Files...")
        upload_files_btn.clicked.connect(self._upload_sftp_files)
        upload_folder_btn = QPushButton("Upload Folder...")
        upload_folder_btn.clicked.connect(self._upload_sftp_folder)
        nav_row.addWidget(up_btn)
        nav_row.addWidget(self.sftp_path_edit, 1)
        nav_row.addWidget(upload_files_btn)
        nav_row.addWidget(upload_folder_btn)
        nav_row.addWidget(refresh_btn)
        layout.addLayout(nav_row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        self.sftp_list = QListWidget()
        self.sftp_list.itemDoubleClicked.connect(self._sftp_entry_activated)
        self.sftp_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sftp_list.customContextMenuRequested.connect(self._show_sftp_menu)
        splitter.addWidget(self.sftp_list)

        editor_wrapper = QWidget()
        editor_layout = QVBoxLayout(editor_wrapper)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.sftp_editor_label = QLabel("(no file open)")
        self.sftp_editor_label.setStyleSheet("font-family: monospace;")
        editor_layout.addWidget(self.sftp_editor_label)
        self.sftp_editor = QPlainTextEdit()
        self.sftp_editor.setPlaceholderText("Double-click a file on the left to view/edit it here")
        self.sftp_editor.textChanged.connect(self._on_sftp_editor_changed)
        editor_layout.addWidget(self.sftp_editor, 1)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.sftp_save_btn = QPushButton("Save")
        self.sftp_save_btn.setEnabled(False)
        self.sftp_save_btn.clicked.connect(self._save_sftp_file)
        save_row.addWidget(self.sftp_save_btn)
        editor_layout.addLayout(save_row)
        splitter.addWidget(editor_wrapper)
        splitter.setSizes([200, 300])

        return wrapper

    @staticmethod
    def _sftp_join(base: str, name: str) -> str:
        if base in ("", "/"):
            return f"/{name}"
        return f"{base.rstrip('/')}/{name}"

    def _sftp_browse(self, path: str) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        self._run_async("sftp_list", lambda: self._do_sftp_list(path))

    def _sftp_navigate_to_typed_path(self) -> None:
        path = self.sftp_path_edit.text().strip()
        if not path.startswith("/"):
            path = f"/{path}"
        if path != "/":
            path = path.rstrip("/")
        self._sftp_browse(path)

    def _sftp_go_up(self) -> None:
        if self._sftp_cwd in ("", "/"):
            return
        parent = self._sftp_cwd.rsplit("/", 1)[0] or "/"
        self._sftp_browse(parent)

    def _sftp_entry_activated(self, item: QListWidgetItem) -> None:
        entry: FileEntry = item.data(Qt.ItemDataRole.UserRole)
        if entry.name == "..":
            self._sftp_go_up()
        elif entry.is_dir:
            self._sftp_browse(self._sftp_join(self._sftp_cwd, entry.name))
        else:
            self._open_sftp_file(self._sftp_join(self._sftp_cwd, entry.name))

    def _populate_sftp_list(self) -> None:
        self.sftp_path_edit.setText(self._sftp_cwd)
        self.sftp_list.clear()
        if self._sftp_cwd not in ("", "/"):
            up_item = QListWidgetItem("..")
            up_item.setData(Qt.ItemDataRole.UserRole, FileEntry(name="..", is_dir=True, size=0))
            self.sftp_list.addItem(up_item)
        for entry in self._sftp_entries:
            label = f"{entry.name}/" if entry.is_dir else f"{entry.name}   ({entry.size:,} bytes)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.sftp_list.addItem(item)

    def _open_sftp_file(self, path: str) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        if self._sftp_dirty and path != self._sftp_open_path and not self._confirm_discard_sftp_changes():
            return
        self._run_async("sftp_read", lambda: self._do_sftp_read(path))

    def _confirm_discard_sftp_changes(self) -> bool:
        return QMessageBox.question(self, "Unsaved changes", f"Discard unsaved changes to '{self._sftp_open_path}'?") == QMessageBox.StandardButton.Yes

    def _on_sftp_editor_changed(self) -> None:
        if self._sftp_loading:
            return
        self._set_sftp_dirty(True)

    def _set_sftp_dirty(self, dirty: bool) -> None:
        self._sftp_dirty = dirty
        self.sftp_save_btn.setEnabled(dirty and self._sftp_open_path is not None)
        if self._sftp_open_path:
            self.sftp_editor_label.setText(f"{self._sftp_open_path}{' *' if dirty else ''}")
        else:
            self.sftp_editor_label.setText("(no file open)")

    def _save_sftp_file(self) -> None:
        if not self._sftp or not self._sftp_open_path:
            return
        path = self._sftp_open_path
        content = self.sftp_editor.toPlainText()
        self._run_async("sftp_write", lambda: self._do_sftp_write(path, content))

    def _show_sftp_menu(self, pos) -> None:
        item = self.sftp_list.itemAt(pos)
        if item is None:
            return
        entry: FileEntry = item.data(Qt.ItemDataRole.UserRole)
        if entry.name == "..":
            return
        path = self._sftp_join(self._sftp_cwd, entry.name)

        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self._delete_sftp_entry(entry, path))
        rename_action = menu.addAction("Rename...")
        rename_action.triggered.connect(lambda: self._rename_sftp_entry(entry, path))
        chmod_label = "Set Permissions (recursive)..." if entry.is_dir else "Set Permissions..."
        chmod_action = menu.addAction(chmod_label)
        chmod_action.triggered.connect(lambda: self._chmod_sftp_entry(entry, path))
        menu.exec(self.sftp_list.viewport().mapToGlobal(pos))

    def _delete_sftp_entry(self, entry: FileEntry, path: str) -> None:
        if not self._sftp:
            return
        if entry.is_dir:
            self.statusBar().showMessage("Deleting directories isn't supported here -- remove their files individually", 6000)
            return
        if QMessageBox.question(self, "Confirm", f"Delete '{entry.name}'? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        self._run_async("sftp_delete", lambda: self._do_sftp_delete(path))

    def _rename_sftp_entry(self, entry: FileEntry, path: str) -> None:
        if not self._sftp:
            return
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", QLineEdit.EchoMode.Normal, entry.name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == entry.name:
            return
        new_path = self._sftp_join(self._sftp_cwd, new_name)
        self._run_async("sftp_rename", lambda: self._do_sftp_rename(path, new_path))

    def _chmod_sftp_entry(self, entry: FileEntry, path: str) -> None:
        if not self._sftp:
            return
        default_mode = "755" if entry.is_dir else "644"
        prompt = f"Octal permission mode for '{entry.name}'"
        if entry.is_dir:
            prompt += " (applied to this folder and everything inside it)"
        text, ok = QInputDialog.getText(self, "Set Permissions", prompt + ":", QLineEdit.EchoMode.Normal, default_mode)
        text = text.strip()
        if not ok or not text:
            return
        try:
            mode = int(text, 8)
            if not (0 <= mode <= 0o7777):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Invalid mode", "Enter an octal permission mode, e.g. 755 or 644.")
            return
        self._run_async("sftp_chmod", lambda: self._do_sftp_chmod(path, mode, entry.is_dir))

    def _upload_sftp_files(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        local_paths, _ = QFileDialog.getOpenFileNames(self, "Select files to upload", "", "All Files (*)", options=QFileDialog.Option.DontUseNativeDialog)
        if not local_paths:
            return
        cwd = self._sftp_cwd
        self.statusBar().showMessage(f"Uploading {len(local_paths)} file(s)...", 4000)
        self._run_async("sftp_upload", lambda: self._do_sftp_upload_files(local_paths, cwd))

    def _upload_sftp_folder(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        local_dir = QFileDialog.getExistingDirectory(self, "Select folder to upload", "", options=QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if not local_dir:
            return
        cwd = self._sftp_cwd
        remote_dir = self._sftp_join(cwd, os.path.basename(local_dir.rstrip("/\\")))
        self.statusBar().showMessage(f"Uploading folder '{os.path.basename(local_dir)}'...", 4000)
        self._run_async("sftp_upload", lambda: self._do_sftp_upload_dir(local_dir, remote_dir, cwd))

    def _do_sftp_list(self, path: str):
        return path, self._sftp.list_dir(path)

    def _do_sftp_read(self, path: str):
        return path, self._sftp.read_file(path)

    def _do_sftp_write(self, path: str, content: str):
        self._sftp.write_file(path, content)
        return path

    def _do_sftp_delete(self, path: str):
        self._sftp.delete_file(path)
        return path

    def _do_sftp_rename(self, old_path: str, new_path: str):
        self._sftp.rename(old_path, new_path)
        return old_path, new_path

    def _do_sftp_chmod(self, path: str, mode: int, recursive: bool):
        if recursive:
            self._sftp.chmod_recursive(path, mode)
        else:
            self._sftp.chmod(path, mode)
        return path

    def _do_sftp_upload_files(self, local_paths: list[str], remote_dir: str):
        for local_path in local_paths:
            remote_path = self._sftp_join(remote_dir, os.path.basename(local_path))
            self._sftp.upload_file(local_path, remote_path)
        return remote_dir, len(local_paths)

    def _do_sftp_upload_dir(self, local_dir: str, remote_dir: str, refresh_dir: str):
        count = self._sftp.upload_dir(local_dir, remote_dir)
        return refresh_dir, count

    # -- backup tab (whole-save-folder copy over SFTP -- not Pterodactyl's own backup
    # system, which snapshots the entire container and is subject to its backup quota) --

    def _build_backup_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.sftp_host:
            note = QLabel("Backup/restore needs SFTP (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        intro = QLabel("Copies the live save folder into as many timestamped backup folders as you want, over SFTP.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(intro)

        # Filled in by _populate_backup_status once the real paths are known
        # (see _SAVE_DIR_CANDIDATES) -- placeholder text until the first check
        # completes, rather than guessing a path that might be wrong.
        self.backup_paths_label = QLabel("Paths: (checking...)" if self.config.sftp_host else "Paths: (SFTP not configured)")
        self.backup_paths_label.setWordWrap(True)
        self.backup_paths_label.setStyleSheet("color: palette(placeholder-text); font-family: monospace;")
        layout.addWidget(self.backup_paths_label)

        live_row = QHBoxLayout()
        live_row.addWidget(QLabel("Live save:"))
        self.backup_live_status_label = QLabel("(SFTP not configured)" if not self.config.sftp_host else "(connecting...)")
        live_row.addWidget(self.backup_live_status_label)
        live_row.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._backup_refresh_status)
        live_row.addWidget(refresh_btn)
        layout.addLayout(live_row)

        self.backup_list = QListWidget()
        self.backup_list.setToolTip("Every backup ever taken, newest first -- pick one to restore or delete.")
        layout.addWidget(self.backup_list, 1)

        # Dedicated to this tab rather than the shared statusBar() -- RCON and
        # SFTP each retry their own reconnect on a timer and call
        # statusBar().showMessage() themselves regardless of what's currently
        # showing there (confirmed live: RCON retries every 10s while the
        # server's down for a backup, which stomps a "Creating backup..."
        # status-bar message every single time, making an operation that's
        # genuinely still running look dead/stuck). This label answers only
        # to _set_backup_busy, so nothing else can clobber it.
        self.backup_busy_label = QLabel()
        self.backup_busy_label.setStyleSheet("font-weight: bold;")
        self.backup_busy_label.setVisible(False)
        layout.addWidget(self.backup_busy_label)

        buttons_row = QHBoxLayout()
        self.backup_create_btn = QPushButton("Create Backup...")
        self.backup_create_btn.setToolTip("Copy the live save into a new timestamped folder next to it. Server must be stopped.")
        self.backup_create_btn.clicked.connect(self._backup_create)
        self.backup_restore_btn = QPushButton("Restore Selected...")
        self.backup_restore_btn.setToolTip("Replace the live save with the selected backup. Server must be stopped.")
        self.backup_restore_btn.clicked.connect(self._backup_restore)
        self.backup_restore_btn.setEnabled(False)
        self.backup_delete_btn = QPushButton("Delete Selected...")
        self.backup_delete_btn.clicked.connect(self._backup_delete)
        self.backup_delete_btn.setEnabled(False)
        self.backup_reset_map_btn = QPushButton("Reset Map...")
        self.backup_reset_map_btn.setToolTip("Delete the live save outright -- the server creates a fresh one on next start. Server must be stopped.")
        self.backup_reset_map_btn.setStyleSheet("color: #e74c3c;")
        self.backup_reset_map_btn.clicked.connect(self._reset_map)
        buttons_row.addWidget(self.backup_create_btn)
        buttons_row.addWidget(self.backup_restore_btn)
        buttons_row.addWidget(self.backup_delete_btn)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.backup_reset_map_btn)
        layout.addLayout(buttons_row)

        warning = QLabel(
            "Create/Restore/Reset all check the server's power state first and refuse to run while it's "
            "running -- stop it from the power bar, then use these. After any of them completes you'll be "
            "asked whether to start the server back up."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: palette(placeholder-text); font-style: italic;")
        layout.addWidget(warning)

        return wrapper

    @staticmethod
    def _backups_dir_for(save_dir: str) -> str:
        return save_dir.rsplit("/", 1)[0] + "/Backups"

    def _resolve_save_dir(self) -> str:
        """Which _SAVE_DIR_CANDIDATES entry this server's SFTP account
        actually exposes -- checked fresh (not cached) so every action stays
        correct even if this is the very first call after connecting. Falls
        back to the first candidate if neither exists yet (e.g. the server
        has never been started), so callers still get a real path to name
        in their error instead of silently doing nothing.
        """
        for candidate in _SAVE_DIR_CANDIDATES:
            if self._sftp.file_exists(candidate):
                return candidate
        return _SAVE_DIR_CANDIDATES[0]

    def _resolve_backups_dir(self) -> str:
        """Prefer whichever _BACKUPS_DIR_CANDIDATES entry already exists, so
        list/delete/restore stay pointed at wherever backups actually were
        created even if the live save doesn't currently exist to confirm the
        root via _resolve_save_dir. Only falls back to mirroring the live
        save's resolved root when there's no Backups folder yet at all (i.e.
        before the very first backup)."""
        for candidate in _BACKUPS_DIR_CANDIDATES:
            if self._sftp.file_exists(candidate):
                return candidate
        return self._backups_dir_for(self._resolve_save_dir())

    @staticmethod
    def _make_backup_name(label: str) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_label = _BACKUP_NAME_RE.sub("-", label.strip()).strip("-")
        return f"{timestamp}_{safe_label}" if safe_label else timestamp

    def _selected_backup_name(self) -> str | None:
        item = self.backup_list.currentItem()
        return item.text() if item else None

    def _backup_refresh_status(self) -> None:
        if not self._sftp:
            self.backup_live_status_label.setText("(SFTP not connected)")
            self.backup_paths_label.setText("Paths: (SFTP not connected)")
            self.backup_list.clear()
            self.backup_restore_btn.setEnabled(False)
            self.backup_delete_btn.setEnabled(False)
            return
        self.backup_live_status_label.setText("(checking...)")
        self._run_async("backup_status", self._do_backup_status)

    def _do_backup_status(self) -> tuple[str, str, bool, list[str]]:
        save_dir = self._resolve_save_dir()
        backups_dir = self._resolve_backups_dir()
        live_exists = self._sftp.file_exists(save_dir)
        if self._sftp.file_exists(backups_dir):
            # Directories are pre-compression-era backups (raw copies);
            # .tar.gz entries are the current, fast, compressed format.
            names = sorted(
                (e.name for e in self._sftp.list_dir(backups_dir) if e.is_dir or e.name.endswith(".tar.gz")),
                reverse=True,
            )
        else:
            names = []
        return save_dir, backups_dir, live_exists, names

    def _populate_backup_status(self, save_dir: str, backups_dir: str, live_exists: bool, names: list[str]) -> None:
        self._resolved_save_dir = save_dir
        self._resolved_backups_dir = backups_dir
        self.backup_paths_label.setText(f"Paths:  live {save_dir}   |   backups {backups_dir}")
        self.backup_live_status_label.setText("Present" if live_exists else "Not found")
        selected = self._selected_backup_name()
        self.backup_list.clear()
        self.backup_list.addItems(names)
        if selected and selected in names:
            matches = self.backup_list.findItems(selected, Qt.MatchFlag.MatchExactly)
            if matches:
                self.backup_list.setCurrentItem(matches[0])
        has_backups = bool(names)
        self.backup_restore_btn.setEnabled(has_backups)
        self.backup_delete_btn.setEnabled(has_backups)

    def _set_backup_busy(self, busy: bool, message: str = "") -> None:
        """Disables the backup buttons and shows a status message that stays
        up for the whole check/create/restore/delete/reset cycle, in a label
        dedicated to this tab rather than the shared statusBar().

        A full folder copy over SFTP can easily run well past the ~4-8s a
        normal status message stays up for, AND -- confirmed live -- RCON
        and SFTP's own reconnect loops call statusBar().showMessage() on
        their own timers regardless of what's already showing there (RCON
        retries every 10s while the server's down, which is required for a
        backup to even start), so even a message posted with no expiry gets
        stomped repeatedly during the operation. A still-running backup
        showing RCON noise instead of its own status looked exactly like a
        stuck/dead app, which is what led to force-quitting and relaunching
        mid-copy. This label answers only to this method, so nothing else
        can overwrite it, and the disabled buttons make clear a click
        genuinely won't do anything yet rather than looking unresponsive.
        """
        for btn in (self.backup_create_btn, self.backup_restore_btn, self.backup_delete_btn, self.backup_reset_map_btn):
            btn.setEnabled(not busy)
        self._backup_busy_base_message = message
        self.backup_busy_label.setText(message)
        self.backup_busy_label.setVisible(busy)
        if not busy:
            # The subsequent _backup_refresh_status() call re-applies the
            # real has-backups gating on Restore/Delete -- this is just so
            # they're not left stuck disabled in the gap before that runs.
            has_backups = self.backup_list.count() > 0
            self.backup_restore_btn.setEnabled(has_backups)
            self.backup_delete_btn.setEnabled(has_backups)

    def _emit_backup_progress(self, count: int) -> None:
        # Called from the background copy thread -- marshal to the UI thread.
        # Throttled past the first 20 files so a huge tree doesn't flood the
        # event loop with a setText() per file.
        if count <= 20 or count % 10 == 0:
            self._bridge.result.emit("backup_progress", count)

    def _do_check_server_state(self) -> str | None:
        """Ground-truth power state via REST (see PterodactylClient.get_current_state),
        or None if there's no Pterodactyl connection to check with -- callers
        treat None as "can't verify, don't block on it"."""
        if self._ptero is None:
            return None
        return self._ptero.get_current_state()

    def _warn_server_running(self, action: str, state: str) -> None:
        QMessageBox.warning(
            self,
            "Server is running",
            f"Can't {action} while the server is running (currently '{state}'). Stop it first, then try again.",
        )

    def _maybe_offer_start_server(self) -> None:
        if self._ptero is None:
            return
        if QMessageBox.question(self, "Start server?", "Start the server back up now?") == QMessageBox.StandardButton.Yes:
            self._send_power_action("start")

    def _backup_create(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        self._set_backup_busy(True, "Checking server status...")
        self._run_async("backup_create_check", self._do_check_server_state)

    def _continue_backup_create(self, state: str | None) -> None:
        if state is not None and state != "offline":
            self._set_backup_busy(False)
            self._warn_server_running("create a backup", state)
            return
        label, ok = QInputDialog.getText(self, "Create backup", "Label (optional, e.g. 'before-mod-update'):")
        if not ok:
            self._set_backup_busy(False)
            return
        name = self._make_backup_name(label)
        busy_message = (
            "Creating backup..."
            if self._ptero is not None
            else "Creating backup... this can take a while for a larger save (no Pterodactyl connection, so this has to stream every file)."
        )
        self._set_backup_busy(True, busy_message)
        self._pause_sftp_health_check()
        self._run_async("backup_create", lambda: self._do_backup_create(name))

    def _do_backup_create(self, name: str) -> str:
        """Returns a short human-readable summary of what happened, shown as
        the completion status message."""
        save_dir = self._resolve_save_dir()
        if not self._sftp.file_exists(save_dir):
            raise SftpError(f"No live save found at {save_dir} -- nothing to back up.")
        backups_dir = self._resolve_backups_dir()
        self._sftp.ensure_dir(backups_dir)

        if self._ptero is not None:
            # Fast path: Wings archives the save on its own disk (no data
            # streamed through this process) and we just move the resulting
            # archive into place -- both confirmed live to work, unlike
            # files/copy (see PterodactylClient.compress_files). A backup of
            # 173k files this way is seconds, not the ~2.5 hours the SFTP
            # fallback below measured for the same tree.
            save_parent, save_name = save_dir.rsplit("/", 1)
            backups_name = backups_dir.rsplit("/", 1)[1]
            dst_name = f"{name}.tar.gz"
            if self._sftp.file_exists(f"{backups_dir}/{dst_name}"):
                raise SftpError(f"A backup named '{name}' already exists.")
            attrs = self._ptero.compress_files(save_parent, [save_name])
            self._ptero.rename_file(save_parent, attrs["name"], f"{backups_name}/{dst_name}")
            size = attrs.get("size") or 0
            return f"Backup created ({size:,} bytes, compressed)"

        # Fallback for servers with no Pterodactyl connection configured --
        # nothing to ask Wings to do the copy for, so stream it file by file.
        dst = f"{backups_dir}/{name}"
        if self._sftp.file_exists(dst):
            raise SftpError(f"A backup named '{name}' already exists.")
        count = self._sftp.copy_dir(save_dir, dst, on_file_copied=self._emit_backup_progress)
        return f"Backup created ({count} file(s) copied)"

    def _backup_restore(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        name = self._selected_backup_name()
        if not name:
            self.statusBar().showMessage("Select a backup to restore first", 4000)
            return
        self._set_backup_busy(True, "Checking server status...")
        self._run_async("backup_restore_check", lambda: (name, self._do_check_server_state()))

    def _continue_backup_restore(self, name: str, state: str | None) -> None:
        if state is not None and state != "offline":
            self._set_backup_busy(False)
            self._warn_server_running("restore a backup", state)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Restore backup")
        box.setText(
            f"Restore '{name}' over the live save?\n\n"
            f"  Backup:     {self._resolved_backups_dir}/{name}\n"
            f"  Live save:  {self._resolved_save_dir}\n\n"
            "This cannot be undone unless you back up the current save first."
        )
        backup_first_btn = box.addButton("Backup Current, Then Restore", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Just Replace", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(backup_first_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            self._set_backup_busy(False)
            return
        backup_current_first = clicked is backup_first_btn

        busy_message = (
            "Restoring backup..."
            if self._ptero is not None
            else "Restoring backup... this can take a while for a larger save (no Pterodactyl connection, so this has to stream every file)."
        )
        self._set_backup_busy(True, busy_message)
        self._pause_sftp_health_check()
        self._run_async("backup_restore", lambda: self._do_backup_restore(name, backup_current_first))

    def _do_backup_restore(self, name: str, backup_current_first: bool) -> None:
        save_dir = self._resolve_save_dir()
        backups_dir = self._resolve_backups_dir()
        src = f"{backups_dir}/{name}"
        if not self._sftp.file_exists(src):
            raise SftpError(f"Backup not found: {src}")

        if self._ptero is not None:
            save_parent, save_name = save_dir.rsplit("/", 1)
            backups_name = backups_dir.rsplit("/", 1)[1]

            # Prepare the replacement BEFORE touching the live save at all --
            # confirmed live that decompress can fail with a
            # DaemonConnectionException on a large archive even though
            # compress handles the exact same data fine (a Wings-side scale
            # limit somewhere between 1,500 and 173,161 files on at least one
            # real node). Getting the replacement ready first means a
            # decompress failure here just falls through to the slower SFTP
            # path below with the live save still fully intact, instead of
            # leaving the server with no save at all.
            if name.endswith(".tar.gz"):
                try:
                    self._ptero.decompress_file(backups_dir, name)
                except PterodactylError:
                    self._sftp_restore_via_download(src, save_dir, backup_current_first, backups_dir)
                    return
                # Decompressing recreates a folder named after whatever was
                # archived -- always save_name ("Multiplayer"), since that's
                # the only thing this app ever compresses.
                extracted_rel = f"{backups_name}/{save_name}"
            else:
                # Pre-compression-era backup: a raw folder whose contents
                # already mirror the live save directly (copy_dir copied
                # save_dir's *contents* into it, not save_dir itself) --
                # already sitting there ready to move into place.
                extracted_rel = f"{backups_name}/{name}"

            if backup_current_first and self._sftp.file_exists(save_dir):
                attrs = self._ptero.compress_files(save_parent, [save_name])
                pre_name = f"{self._make_backup_name('pre-restore')}.tar.gz"
                self._ptero.rename_file(save_parent, attrs["name"], f"{backups_name}/{pre_name}")

            if self._sftp.file_exists(save_dir):
                self._ptero.delete_files(save_parent, [save_name])
            self._ptero.rename_file(save_parent, extracted_rel, save_name)
            return

        # Fallback for servers with no Pterodactyl connection configured.
        if backup_current_first and self._sftp.file_exists(save_dir):
            self._sftp.ensure_dir(backups_dir)
            self._sftp.copy_dir(save_dir, f"{backups_dir}/{self._make_backup_name('pre-restore')}", on_file_copied=self._emit_backup_progress)
        if self._sftp.file_exists(save_dir):
            self._sftp.delete_dir(save_dir)
        self._sftp.copy_dir(src, save_dir, on_file_copied=self._emit_backup_progress)

    def _sftp_restore_via_download(self, archive_path: str, save_dir: str, backup_current_first: bool, backups_dir: str) -> None:
        """Last-resort fallback when Wings' own decompress can't handle an
        archive (see _do_backup_restore) -- downloads the one archive file
        (fast, it's a single file), extracts it locally, then re-uploads the
        save file by file. The upload side is back to the same per-file SFTP
        bottleneck the original slow implementation had, but it's correct
        regardless of whatever's wrong with decompress on this node, and the
        live save is never touched until the extracted replacement is
        confirmed ready on disk locally.
        """
        import tarfile
        import tempfile

        with tempfile.TemporaryDirectory(prefix="pzcontrol_restore_") as tmp:
            local_archive = os.path.join(tmp, "backup.tar.gz")
            self._sftp.download_to(archive_path, local_archive)
            extract_dir = os.path.join(tmp, "extracted")
            with tarfile.open(local_archive, "r:gz") as tar:
                tar.extractall(extract_dir)
            entries = os.listdir(extract_dir)
            local_save_root = os.path.join(extract_dir, entries[0]) if len(entries) == 1 else extract_dir

            if backup_current_first and self._sftp.file_exists(save_dir):
                self._sftp.ensure_dir(backups_dir)
                self._sftp.copy_dir(save_dir, f"{backups_dir}/{self._make_backup_name('pre-restore')}", on_file_copied=self._emit_backup_progress)
            if self._sftp.file_exists(save_dir):
                self._sftp.delete_dir(save_dir)
            self._sftp.upload_dir(local_save_root, save_dir, on_file_uploaded=self._emit_backup_progress)

    def _backup_delete(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        name = self._selected_backup_name()
        if not name:
            self.statusBar().showMessage("Select a backup to delete first", 4000)
            return
        if QMessageBox.question(self, "Delete backup", f"Permanently delete backup '{name}'? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        self._set_backup_busy(True, "Deleting backup...")
        self._run_async("backup_delete", lambda: self._do_backup_delete(name))

    def _do_backup_delete(self, name: str) -> None:
        backups_dir = self._resolve_backups_dir()
        path = f"{backups_dir}/{name}"
        if not self._sftp.file_exists(path):
            raise SftpError(f"Backup not found: {path}")
        if self._ptero is not None:
            # Also correct for the pre-compression raw-folder backups (which
            # delete_dir would need many round trips for) -- files/delete
            # handles files and directories the same way.
            self._ptero.delete_files(backups_dir, [name])
        elif name.endswith(".tar.gz"):
            self._sftp.delete_file(path)
        else:
            self._sftp.delete_dir(path)

    def _reset_map(self) -> None:
        if not self._sftp:
            self.statusBar().showMessage("SFTP is not connected", 4000)
            return
        self._set_backup_busy(True, "Checking server status...")
        self._run_async("reset_map_check", self._do_check_server_state)

    def _continue_reset_map(self, state: str | None) -> None:
        if state is not None and state != "offline":
            self._set_backup_busy(False)
            self._warn_server_running("reset the map", state)
            return
        if QMessageBox.question(
            self,
            "Reset map",
            f"This permanently DELETES the live save:\n  {self._resolved_save_dir}\n\n"
            "The server generates a brand new map from scratch the next time it starts. "
            "This cannot be undone (unless you made a backup first).\n\nContinue?",
        ) != QMessageBox.StandardButton.Yes:
            self._set_backup_busy(False)
            return
        self._set_backup_busy(True, "Deleting live save...")
        self._run_async("reset_map", self._do_reset_map)

    def _do_reset_map(self) -> None:
        save_dir = self._resolve_save_dir()
        if not self._sftp.file_exists(save_dir):
            raise SftpError(f"No live save found at {save_dir} -- nothing to reset.")
        if self._ptero is not None:
            save_parent, save_name = save_dir.rsplit("/", 1)
            self._ptero.delete_files(save_parent, [save_name])
        else:
            self._sftp.delete_dir(save_dir)

    # -- mods tab (WorkshopItems/Mods keys in the .ini) --------------------------------

    def _build_mods_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.sftp_host:
            note = QLabel("Editing mods needs SFTP (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        top_row = QHBoxLayout()
        self.mods_path_label = QLabel(f"<tt>{self.config.ini_path or '(auto-detecting...)'}</tt>")
        top_row.addWidget(self.mods_path_label)
        top_row.addStretch(1)
        self.mods_edit_lists_toggle_btn = QPushButton("Show Workshop Items / Mods Lists")
        self.mods_edit_lists_toggle_btn.setCheckable(True)
        self.mods_edit_lists_toggle_btn.setToolTip(
            "Raw editable lists, collapsed by default -- the Workshop Update Check table below covers most\n"
            "day-to-day needs now. Expand this for manual editing or to see the exact save-ready text."
        )
        self.mods_edit_lists_toggle_btn.toggled.connect(self._toggle_mods_edit_lists)
        top_row.addWidget(self.mods_edit_lists_toggle_btn)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load_ini_settings)
        top_row.addWidget(reload_btn)
        layout.addLayout(top_row)

        self.mod_update_flash_checkbox = QCheckBox("Flash notice when a mod has an update (needs restart)")
        self.mod_update_flash_checkbox.setToolTip(
            "When a Workshop Update Check finds a changed mod, flash a red/green notice on the\n"
            "power bar so it's visible from any tab, as a reminder the server needs a restart."
        )
        self.mod_update_flash_checkbox.setChecked(self.config.mod_update_flash_enabled)
        self.mod_update_flash_checkbox.toggled.connect(self._save_mod_update_flash_enabled)
        layout.addWidget(self.mod_update_flash_checkbox)

        auto_check_row = QHBoxLayout()
        self.mod_auto_check_checkbox = QCheckBox("Automatically re-run Check for Updates every")
        self.mod_auto_check_checkbox.setToolTip("Off by default -- when on, re-runs the check above on its own instead of only on a button click.")
        self.mod_auto_check_checkbox.setChecked(self.config.mod_auto_check_enabled)
        self.mod_auto_check_checkbox.toggled.connect(self._save_mod_auto_check_settings)
        auto_check_row.addWidget(self.mod_auto_check_checkbox)
        self.mod_auto_check_interval_spin = QSpinBox()
        self.mod_auto_check_interval_spin.setRange(5, 1440)
        self.mod_auto_check_interval_spin.setSuffix(" min")
        self.mod_auto_check_interval_spin.setValue(self.config.mod_auto_check_interval_minutes)
        self.mod_auto_check_interval_spin.valueChanged.connect(self._save_mod_auto_check_settings)
        auto_check_row.addWidget(self.mod_auto_check_interval_spin)
        auto_check_row.addStretch(1)
        layout.addLayout(auto_check_row)

        self.mod_update_autorestart_checkbox = QCheckBox("Auto-restart when an update is available AND the server is empty")
        self.mod_update_autorestart_checkbox.setToolTip(
            "Off by default. Both conditions must hold at the same time -- a changed active mod (Workshop\n"
            "Update Check) or a new server build (Server Build Check) found, and zero players currently\n"
            "online -- checked every time any of those change."
        )
        self.mod_update_autorestart_checkbox.setChecked(self.config.mod_update_autorestart_enabled)
        self.mod_update_autorestart_checkbox.toggled.connect(self._save_mod_update_autorestart_enabled)
        layout.addWidget(self.mod_update_autorestart_checkbox)

        server_build_row = QHBoxLayout()
        self.server_build_label = QLabel("Server Build: not checked yet")
        self.server_build_label.setStyleSheet("color: palette(placeholder-text);")
        server_build_row.addWidget(self.server_build_label, 1)
        layout.addLayout(server_build_row)

        columns = QHBoxLayout()

        self.mods_workshop_box = QGroupBox("Workshop Items (Steam Workshop IDs)")
        workshop_layout = QVBoxLayout(self.mods_workshop_box)
        self.mods_workshop_edit = QPlainTextEdit()
        self.mods_workshop_edit.setPlaceholderText("One Workshop ID per line, e.g.\n2794605601")
        self.mods_workshop_edit.textChanged.connect(self._mark_mods_dirty)
        workshop_layout.addWidget(self.mods_workshop_edit)
        columns.addWidget(self.mods_workshop_box, 1)

        self.mods_ids_box = QGroupBox("Mods (Mod IDs)")
        mods_layout = QVBoxLayout(self.mods_ids_box)
        self.mods_ids_edit = QPlainTextEdit()
        self.mods_ids_edit.setPlaceholderText("One Mod ID per line, e.g.\nBrita's Weapon Pack")
        self.mods_ids_edit.textChanged.connect(self._mark_mods_dirty)
        mods_layout.addWidget(self.mods_ids_edit)
        columns.addWidget(self.mods_ids_box, 1)

        # Collapsed by default -- the Workshop Update Check table covers most
        # of what these were needed for day-to-day now, but they're still the
        # fallback for manual editing, so keep them one click away.
        self.mods_workshop_box.setVisible(False)
        self.mods_ids_box.setVisible(False)

        workshop_updates_box = QGroupBox("Workshop Update Check")
        workshop_updates_layout = QVBoxLayout(workshop_updates_box)
        updates_top_row = QHBoxLayout()
        self.workshop_check_hint = QLabel("Pulls each item's last-updated time from the Steam Workshop, and checks the server build too.")
        self.workshop_check_hint.setStyleSheet("color: palette(placeholder-text);")
        updates_top_row.addWidget(self.workshop_check_hint, 1)
        self.workshop_check_btn = QPushButton("Check for Updates")
        self.workshop_check_btn.clicked.connect(self._check_for_updates)
        updates_top_row.addWidget(self.workshop_check_btn)
        workshop_updates_layout.addLayout(updates_top_row)

        self.workshop_table = QTableWidget(0, 4)
        self.workshop_table.setHorizontalHeaderLabels(["Workshop ID", "Title", "Last Updated", "Status"])
        self.workshop_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.workshop_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.workshop_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.workshop_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.workshop_table.customContextMenuRequested.connect(self._show_workshop_context_menu)
        # Drag rows to reorder -- PZ loads mods in Mods= order, so some setups
        # genuinely need a specific load order (e.g. a framework before
        # whatever depends on it).
        self.workshop_table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.workshop_table.setDragDropOverwriteMode(False)
        self.workshop_table.model().rowsMoved.connect(self._on_workshop_rows_reordered)
        header = self.workshop_table.horizontalHeader()
        # Interactive (not Fixed/Stretch) on every column so they can all be
        # dragged wider -- confirmed live that longer status text like
        # "Missing Mod ID(s)" was getting truncated at the old fixed width.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 100)
        header.resizeSection(1, 220)
        header.resizeSection(2, 140)
        header.resizeSection(3, 150)
        workshop_updates_layout.addWidget(self.workshop_table)
        columns.addWidget(workshop_updates_box, 2)

        layout.addLayout(columns, 1)

        bottom_row = QHBoxLayout()
        self.mods_hint = QLabel(
            "Order should match between the two lists when a Workshop item contains more than one mod."
        )
        self.mods_hint.setWordWrap(True)
        self.mods_hint.setStyleSheet("color: palette(placeholder-text);")
        bottom_row.addWidget(self.mods_hint, 1)
        self.mods_save_btn = QPushButton("Save")
        self.mods_save_btn.setEnabled(False)
        self.mods_save_btn.clicked.connect(self._save_mods_settings)
        bottom_row.addWidget(self.mods_save_btn)
        layout.addLayout(bottom_row)
        return wrapper

    def _populate_mods_form(self) -> None:
        self._mods_loading = True
        try:
            self.mods_workshop_edit.setPlainText(self._ini_list_value("WorkshopItems"))
            self.mods_ids_edit.setPlainText(self._ini_list_value("Mods"))
        finally:
            self._mods_loading = False
        self._set_mods_dirty(False)

    def _ini_list_value(self, name: str) -> str:
        for prop in self._ini_properties:
            if prop.name == name:
                return "\n".join(item.strip() for item in prop.value.split(";") if item.strip())
        return ""

    def _mark_mods_dirty(self, *_args) -> None:
        if self._mods_loading:
            return
        self._set_mods_dirty(True)

    def _toggle_mods_edit_lists(self, checked: bool) -> None:
        self.mods_workshop_box.setVisible(checked)
        self.mods_ids_box.setVisible(checked)
        self.mods_edit_lists_toggle_btn.setText("Hide Workshop Items / Mods Lists" if checked else "Show Workshop Items / Mods Lists")

    def _save_mod_update_flash_enabled(self, checked: bool) -> None:
        self.config.mod_update_flash_enabled = checked
        if self._save_config_callback:
            self._save_config_callback(self.config)
        if checked:
            self._refresh_update_flash()
        else:
            self._stop_mod_update_flash()

    def _save_mod_auto_check_settings(self, *_args) -> None:
        self.config.mod_auto_check_enabled = self.mod_auto_check_checkbox.isChecked()
        self.config.mod_auto_check_interval_minutes = self.mod_auto_check_interval_spin.value()
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self._apply_mod_auto_check_timer()

    def _save_mod_update_autorestart_enabled(self, checked: bool) -> None:
        self.config.mod_update_autorestart_enabled = checked
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self._maybe_auto_restart_for_update()

    def _apply_mod_auto_check_timer(self) -> None:
        if self._mod_auto_check_timer is None:
            timer = QTimer(self)
            timer.timeout.connect(self._check_for_updates)
            self._mod_auto_check_timer = timer
        if self.config.mod_auto_check_enabled:
            self._mod_auto_check_timer.start(self.config.mod_auto_check_interval_minutes * 60_000)
        else:
            self._mod_auto_check_timer.stop()

    def _set_mods_dirty(self, dirty: bool) -> None:
        self._mods_dirty = dirty
        self.mods_save_btn.setEnabled(dirty)

    def _sanitize_mod_ids(self, mod_ids: list[str]) -> list[str]:
        """Strips stray BBCode tags (e.g. "[/h1]") off any Mod ID before it
        can reach the .ini -- see _MOD_ID_BBCODE_RE for how these sneak in.
        Never blanks an entry outright: if a mod ID is nothing but tags
        (empty after stripping), it's left untouched rather than silently
        dropped, since that's more likely a typo worth the user's own eyes
        than something safe to guess about."""
        cleaned_ids = []
        changed: list[tuple[str, str]] = []
        for mod_id in mod_ids:
            cleaned = _MOD_ID_BBCODE_RE.sub("", mod_id).strip()
            if cleaned and cleaned != mod_id:
                changed.append((mod_id, cleaned))
                cleaned_ids.append(cleaned)
            else:
                cleaned_ids.append(mod_id)
        if changed:
            self.mods_ids_edit.setPlainText("\n".join(cleaned_ids))
            details = "\n".join(f"• {old}  →  {new}" for old, new in changed)
            QMessageBox.information(
                self,
                "Cleaned up Mod ID(s)",
                "Stripped stray BBCode tags from the following Mod ID(s) before saving -- these are "
                "usually copy/paste artifacts from a Workshop page's description text, and PZ can't "
                f"match a mod folder to an ID that still has them attached:\n\n{details}",
            )
        return cleaned_ids

    def _save_mods_settings(self) -> None:
        if not self._sftp or self._ini_text is None or not self.config.ini_path:
            return
        workshop_ids = [line.strip() for line in self.mods_workshop_edit.toPlainText().splitlines() if line.strip()]
        mod_ids = [line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip()]
        mod_ids = self._sanitize_mod_ids(mod_ids)
        changes = {}
        workshop_value = ";".join(workshop_ids)
        if workshop_value != self._ini_list_value("WorkshopItems").replace("\n", ";"):
            changes["WorkshopItems"] = workshop_value
        mods_value = ";".join(mod_ids)
        if mods_value != self._ini_list_value("Mods").replace("\n", ";"):
            changes["Mods"] = mods_value
        if not changes:
            self.statusBar().showMessage("No changes to save", 4000)
            return
        ini_text, path = self._ini_text, self.config.ini_path
        self._run_async("ini_save", lambda: self._do_ini_save(ini_text, changes, path))

    def _active_workshop_ids(self) -> list[str]:
        return [line.strip() for line in self.mods_workshop_edit.toPlainText().splitlines() if line.strip()]

    def _check_for_updates(self) -> None:
        """Entry point for the button and the auto-check timer -- runs both
        the Workshop mod check and the server build check together, since
        from the user's point of view "check for updates" means both."""
        self._check_workshop_updates()
        self._check_server_update()

    def _check_workshop_updates(self) -> None:
        active_ids = self._active_workshop_ids()
        # Frozen items stay in the table (not just the active list) so there's
        # something to right-click > Active on to bring them back.
        frozen_ids = [wid for wid in self.config.frozen_mods if wid not in active_ids]
        all_ids = active_ids + frozen_ids
        if not all_ids:
            self.statusBar().showMessage("No Workshop IDs to check", 4000)
            return
        self.workshop_check_btn.setEnabled(False)
        self.statusBar().showMessage(f"Checking {len(all_ids)} Workshop item(s) against Steam...", 6000)
        self._run_async("workshop_check", lambda: self._do_check_workshop_updates(all_ids, active_ids))

    def _check_server_update(self) -> None:
        if not self._sftp:
            return
        self._run_async("server_update_check", self._do_check_server_update)

    def _do_check_server_update(self) -> tuple[InstalledBuild, LatestBuild]:
        assert self._sftp is not None
        acf_text = self._sftp.read_file(MANIFEST_PATH)
        installed = parse_installed_build(acf_text)
        latest = fetch_latest_build(installed.branch)
        return installed, latest

    def _populate_server_build_status(self, result: tuple[InstalledBuild, LatestBuild]) -> None:
        installed, latest = result
        self._installed_server_build = installed
        self._latest_server_build = latest
        is_update = installed.buildid != latest.buildid
        self._server_build_update_pending = is_update
        latest_desc = latest.description or f"build {latest.buildid}"
        if is_update:
            self.server_build_label.setText(
                f"Server Build: update available -- {latest_desc} (currently on build {installed.buildid})"
            )
            self.server_build_label.setStyleSheet("color: #d4a017; font-weight: bold;")
        else:
            self.server_build_label.setText(f"Server Build: up to date ({latest_desc})")
            self.server_build_label.setStyleSheet("color: palette(placeholder-text);")
        self._refresh_update_flash()
        self._clear_update_restart_guard_if_resolved()
        self._maybe_auto_restart_for_update()

    def _on_workshop_rows_reordered(self, *_args) -> None:
        # Read the table's post-drop visual order back out (col 0 = Workshop
        # ID) rather than trying to compute the move from the rowsMoved
        # signal's indices -- simpler and correct regardless of how many
        # rows moved or in what direction.
        new_order: list[str] = []
        for row in range(self.workshop_table.rowCount()):
            item = self.workshop_table.item(row, 0)
            if item is None:
                continue
            workshop_id = item.text()
            if workshop_id in self.config.frozen_mods:
                continue  # frozen rows are shown but aren't part of the active list
            new_order.append(workshop_id)

        current_active = self._active_workshop_ids()
        if sorted(new_order) != sorted(current_active):
            # Table's stale relative to the text boxes (edited by hand since
            # the last check, or a row's membership changed) -- bail rather
            # than reorder based on a mismatched snapshot.
            return

        self.mods_workshop_edit.setPlainText("\n".join(new_order))

        # Carry Mods= along: for each Workshop item in its new position,
        # pull in its own Mod ID(s) (as a group, in their existing relative
        # order) so mods that need to load before/after another stay correct.
        current_mod_lines = [line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip()]
        assigned: set[str] = set()
        new_mod_lines: list[str] = []
        for workshop_id in new_order:
            own_mod_ids = self._workshop_mod_id_map.get(workshop_id, set())
            for mod_id in current_mod_lines:
                if mod_id not in assigned and mod_id in own_mod_ids:
                    new_mod_lines.append(mod_id)
                    assigned.add(mod_id)
        # Anything left over (not resolved to a Workshop item -- e.g. its
        # content hasn't downloaded yet, or it was typed in by hand) keeps
        # its original relative order, tacked on at the end rather than lost.
        new_mod_lines.extend(mod_id for mod_id in current_mod_lines if mod_id not in assigned)
        self.mods_ids_edit.setPlainText("\n".join(new_mod_lines))

        if not self._workshop_mod_id_map:
            self.statusBar().showMessage(
                "Reordered Workshop Items, but Mods weren't reordered -- run Check for Updates first so the "
                "Mod ID mapping is known, then click Save on the Mods tab to apply.",
                9000,
            )
        else:
            self.statusBar().showMessage("Load order updated -- click Save on the Mods tab to apply", 7000)

    def _console_log_path(self) -> str | None:
        # Derived from wherever ini_path was actually discovered rather than a
        # hardcoded guess, since the SFTP root's relationship to the server's
        # data dir already varies by egg/node (see _SERVER_DIR_CANDIDATES).
        if not self.config.ini_path:
            return None
        server_dir = self.config.ini_path.rsplit("/", 1)[0]
        if not server_dir.endswith("/Server"):
            return None
        cache_dir = server_dir.rsplit("/", 1)[0]
        return f"{cache_dir}/server-console.txt"

    def _workshop_content_dir(self) -> str | None:
        console_path = self._console_log_path()
        if not console_path:
            return None
        cache_dir = console_path.rsplit("/", 1)[0]
        container_root = cache_dir.rsplit("/", 1)[0]
        return f"{container_root}/steamapps/workshop/content/{_WORKSHOP_APP_ID}"

    def _find_mod_ids_for_workshop_item(self, workshop_id: str) -> set[str]:
        return self._scan_workshop_item_mod_info(workshop_id)[0]

    def _scan_workshop_item_mod_info(self, workshop_id: str) -> tuple[set[str], set[str], dict[str, set[str]]]:
        """Returns (mod IDs this item declares, mod IDs its mod.info files
        require, and a map of mod ID -> the other mod ID(s) within this same
        item it declares itself incompatible with -- e.g. a "Hard" variant
        vs the normal one, where you're meant to enable exactly one)."""
        mod_ids: set[str] = set()
        required_ids: set[str] = set()
        incompatible_map: dict[str, set[str]] = {}
        workshop_content_dir = self._workshop_content_dir()
        if not self._sftp or not workshop_content_dir:
            return mod_ids, required_ids, incompatible_map
        for mod_info_path in self._sftp.find_files(f"{workshop_content_dir}/{workshop_id}", lambda n: n.lower() == "mod.info"):
            try:
                text = self._sftp.read_file(mod_info_path, max_size=50_000)
            except SftpError:
                continue
            own_ids = [m.group(1).strip() for m in _MOD_INFO_ID_RE.finditer(text)]
            mod_ids.update(own_ids)
            for match in _MOD_INFO_REQUIRE_RE.finditer(text):
                # Confirmed live: older/version-subfolder mod.info files prefix
                # each entry with a stray "\", newer ones don't -- strip either way.
                required_ids.update(entry.strip().lstrip("\\").strip() for entry in match.group(1).split(",") if entry.strip())
            incompatible_ids = {
                entry.strip().lstrip("\\").strip()
                for match in _MOD_INFO_INCOMPATIBLE_RE.finditer(text)
                for entry in match.group(1).split(",")
                if entry.strip()
            }
            if incompatible_ids:
                # This mod.info's own id= is incompatible with each listed
                # id -- record both directions so the check works regardless
                # of which of the pair happens to be the active one.
                for own_id in own_ids:
                    incompatible_map.setdefault(own_id, set()).update(incompatible_ids)
                for other_id in incompatible_ids:
                    incompatible_map.setdefault(other_id, set()).update(own_ids)
        return mod_ids, required_ids, incompatible_map

    def _do_check_workshop_updates(
        self, all_ids: list[str], active_ids: list[str]
    ) -> tuple[dict[str, WorkshopItem], set[str], dict[str, set[str]], dict[str, set[str]], dict[str, int | None], dict[str, dict[str, set[str]]]]:
        details = fetch_workshop_details(all_ids)
        failed_mod_ids: set[str] = set()
        console_path = self._console_log_path()
        if self._sftp and console_path:
            try:
                log_text = self._sftp.read_file(console_path)
                failed_mod_ids = set(_REQUIRED_MOD_NOT_FOUND_RE.findall(log_text))
            except SftpError:
                pass  # no boot log yet, or path guess didn't pan out -- just skip the "Not working" check

        # Always scanned now (not just when something's failing): drag-drop
        # reordering needs to know which Mod IDs belong to which Workshop
        # item regardless of whether anything's currently broken. Frozen
        # items are deliberately excluded from both this and the
        # not-working check.
        mod_id_map: dict[str, set[str]] = {}
        required_id_map: dict[str, set[str]] = {}
        incompatible_id_map: dict[str, dict[str, set[str]]] = {}
        for workshop_id in active_ids:
            mod_ids, required_ids, incompatible_ids = self._scan_workshop_item_mod_info(workshop_id)
            mod_id_map[workshop_id] = mod_ids
            required_id_map[workshop_id] = required_ids
            incompatible_id_map[workshop_id] = incompatible_ids

        # Ground-truth staleness check: the newest mtime actually sitting in
        # this item's downloaded content dir, compared later against Steam's
        # time_updated. Covers all_ids (frozen included, since their content
        # is usually still on disk from before they were frozen) -- unlike
        # the mod-id scan above, this doesn't depend on the item being active.
        local_mtime_map: dict[str, int | None] = {}
        workshop_content_dir = self._workshop_content_dir()
        if self._sftp and workshop_content_dir:
            for workshop_id in all_ids:
                try:
                    local_mtime_map[workshop_id] = self._sftp.newest_mtime_under(f"{workshop_content_dir}/{workshop_id}")
                except SftpError:
                    local_mtime_map[workshop_id] = None
        return details, failed_mod_ids, mod_id_map, required_id_map, local_mtime_map, incompatible_id_map

    def _populate_workshop_table(
        self,
        result: tuple[
            dict[str, WorkshopItem], set[str], dict[str, set[str]], dict[str, set[str]], dict[str, int | None], dict[str, dict[str, set[str]]]
        ],
    ) -> None:
        details, failed_mod_ids, mod_id_map, required_id_map, local_mtime_map, incompatible_id_map = result
        all_known_mod_ids: set[str] = set().union(*mod_id_map.values()) if mod_id_map else set()
        current_mod_ids = set(line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip())
        self._workshop_missing_deps = {}
        self._workshop_missing_mod_ids = {}
        self._workshop_mod_id_map = mod_id_map
        self._workshop_not_working = set()
        self._workshop_time_updated = {}
        active_ids_ordered = self._active_workshop_ids()  # load order, not just membership
        active_ids = set(active_ids_ordered)
        frozen_ids = set(self.config.frozen_mods)
        all_ids = active_ids_ordered + [wid for wid in frozen_ids if wid not in active_ids]
        baseline = self.config.known_workshop_updates
        rows: list[tuple[str, str, int | None, list[str]]] = []
        any_new_update = False
        any_not_working = False
        any_missing_mod_ids = False
        any_frozen_update = False
        any_active_update = False

        def _is_stale(workshop_id: str, item: WorkshopItem, previous: int | None) -> bool:
            # Ground truth when we have it: what's actually sitting on disk
            # vs. what Steam says is current. Works on the very first check
            # (no prior baseline needed) and self-corrects once a restart
            # re-downloads the content, since the local mtime then moves past
            # time_updated on its own. The tolerance absorbs clock skew
            # between Steam's servers and the game server's filesystem.
            local_mtime = local_mtime_map.get(workshop_id)
            if local_mtime is not None and item.time_updated is not None:
                return item.time_updated > local_mtime + _MTIME_STALENESS_TOLERANCE_SECONDS
            # Fallback for when the content dir couldn't be resolved (SFTP
            # down, item never downloaded yet, egg layout not recognized):
            # the old last-checked-baseline heuristic.
            return previous is not None and item.time_updated != previous

        for workshop_id in all_ids:
            item = details.get(workshop_id)
            if item is None or not item.found:
                rows.append((workshop_id, item.title if item else "(lookup failed)", None, ["Frozen"] if workshop_id in frozen_ids else []))
                continue
            previous = baseline.get(workshop_id)
            is_new_update = _is_stale(workshop_id, item, previous)
            self._workshop_time_updated[workshop_id] = item.time_updated
            if workshop_id in frozen_ids:
                # Still worth tracking -- an update might be exactly what
                # fixes whatever got it frozen in the first place.
                statuses = ["Frozen", "Updated!"] if is_new_update else ["Frozen"]
                rows.append((workshop_id, item.title, item.time_updated, statuses))
                any_new_update = any_new_update or is_new_update
                any_frozen_update = any_frozen_update or is_new_update
                # Only ever set on first sighting -- once a change is flagged,
                # the baseline must stay pinned to the pre-update value so
                # "Updated!" keeps showing on every later check, not just the
                # one right after the change happened on Steam. It only
                # advances again once the server actually restarts (see
                # _send_power_action, which clears known_workshop_updates).
                if previous is None:
                    baseline[workshop_id] = item.time_updated
                continue
            statuses = ["Updated!"] if is_new_update else []
            own_ids = mod_id_map.get(workshop_id, set())
            is_not_working = bool(own_ids & failed_mod_ids)
            if is_not_working:
                self._workshop_not_working.add(workshop_id)
                missing_deps = required_id_map.get(workshop_id, set()) - all_known_mod_ids - own_ids
                if missing_deps:
                    self._workshop_missing_deps[workshop_id] = sorted(missing_deps)
            # Proactive check, independent of the log-based one above: this
            # item's actual downloaded mod.info files declare IDs that
            # aren't in the Mods= list at all yet -- catches a gap before a
            # restart even confirms it's broken, not just after.
            #
            # Confirmed live: some mods ship both a bare id= and a
            # "<workshop_id>/id=" prefixed one for the same submod (build-
            # compatibility variants, not two separate required mods) -- if
            # the prefixed form is already active, don't flag the bare form
            # as missing too, or vice versa. Only the *other* spelling
            # counts as "covered"; being active under its own exact name
            # still needs to actually be present.
            def _has_equivalent_active(mod_id: str) -> bool:
                if "/" in mod_id:
                    return mod_id.split("/", 1)[1] in current_mod_ids
                return f"{workshop_id}/{mod_id}" in current_mod_ids

            # Alternate/exclusive submod variants (mod.info's incompatible=,
            # e.g. a "Hard" difficulty version vs the normal one) aren't
            # missing just because you picked the other one on purpose --
            # only flag mid if none of the id(s) it's declared incompatible
            # with are the ones actually active.
            own_incompatible = incompatible_id_map.get(workshop_id, {})

            def _is_intentionally_unused(mod_id: str) -> bool:
                return bool(own_incompatible.get(mod_id, set()) & current_mod_ids)

            missing_mod_ids = {
                mid for mid in (own_ids - current_mod_ids) if not _has_equivalent_active(mid) and not _is_intentionally_unused(mid)
            }
            if missing_mod_ids:
                self._workshop_missing_mod_ids[workshop_id] = sorted(missing_mod_ids)

            # A right-click "Mark OK until next update" pins the dismissal to
            # this item's time_updated at the moment it was dismissed (e.g. an
            # old test-build subfolder's mod.info declaring an ID that's
            # irrelevant to the currently running game version -- a real
            # false positive, not a fix waiting to happen). Once Steam
            # reports a newer time_updated the pinned value stops matching
            # and the flag goes live again on its own -- no manual
            # "un-dismiss" step needed for a genuine future break to surface.
            dismissed_at = self.config.dismissed_mod_issues.get(workshop_id)
            if dismissed_at is not None and dismissed_at != item.time_updated:
                del self.config.dismissed_mod_issues[workshop_id]
                dismissed_at = None
            if dismissed_at is not None and (is_not_working or missing_mod_ids):
                statuses.append("Dismissed")
            else:
                if is_not_working:
                    statuses.append("Not working")
                    any_not_working = True
                if missing_mod_ids:
                    statuses.append("Missing Mod ID(s)")
                    any_missing_mod_ids = True
            if not statuses:
                statuses = ["Working"]
            rows.append((workshop_id, item.title, item.time_updated, statuses))
            any_new_update = any_new_update or is_new_update
            any_active_update = any_active_update or is_new_update
            # See the frozen branch above for why this is gated on `previous
            # is None` instead of unconditional.
            if previous is None:
                baseline[workshop_id] = item.time_updated

        self._last_workshop_check_result = result
        # No re-sorting -- rows stay in Mods=/WorkshopItems= load order
        # (frozen ones appended after) so the table is draggable-reorderable
        # and reflects what'll actually be written on Save. "Updated!"/"Not
        # working" rely on the color coding to stand out instead of being
        # sorted to the top.
        self.workshop_table.setRowCount(len(rows))
        for row, (workshop_id, title, timestamp, statuses) in enumerate(rows):
            date_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "--"
            if "Not working" in statuses or "Missing Mod ID(s)" in statuses:
                row_color = Qt.GlobalColor.red
            elif "Dismissed" in statuses:
                row_color = Qt.GlobalColor.gray  # flagged, but manually confirmed a false positive
            elif "Updated!" in statuses:
                row_color = Qt.GlobalColor.yellow
            elif "Frozen" in statuses:
                row_color = Qt.GlobalColor.cyan  # "blue like ice"
            else:
                row_color = Qt.GlobalColor.green  # active and running clean
            for col, text in enumerate((workshop_id, title, date_str, " / ".join(statuses))):
                cell = QTableWidgetItem(text)
                cell.setForeground(row_color)
                self.workshop_table.setItem(row, col, cell)

        self.workshop_check_btn.setEnabled(True)
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self._any_mod_update_for_flash = any_new_update or any_frozen_update
        self._refresh_update_flash()
        if any_not_working and any_frozen_update:
            self.statusBar().showMessage("Some mods aren't loading, and a frozen mod has an update -- worth trying Active again", 8000)
        elif any_not_working and any_missing_mod_ids:
            self.statusBar().showMessage("Some mods aren't loading, and some are missing Mod ID(s) -- see the table", 8000)
        elif any_not_working:
            self.statusBar().showMessage("Some mods aren't loading -- see 'Not working' in the table", 8000)
        elif any_missing_mod_ids:
            self.statusBar().showMessage("Some mods are missing Mod ID(s) they should have -- right-click > Find Mod ID(s) to add them", 8000)
        elif any_frozen_update:
            self.statusBar().showMessage("A frozen mod has an update -- might be worth unfreezing to try again", 8000)
        elif any_new_update:
            self.statusBar().showMessage("Some Workshop items have updated since the last check!", 8000)
        else:
            self.statusBar().showMessage("Workshop check complete", 5000)

        self._active_mod_update_pending = any_active_update
        self._clear_update_restart_guard_if_resolved()
        self._maybe_auto_restart_for_update()

    def _clear_update_restart_guard_if_resolved(self) -> None:
        # Rearms the auto-restart rule once BOTH sources confirm resolved --
        # gating on only one (e.g. a clean Workshop check right after a
        # server-build-triggered restart, before the build check itself has
        # re-run) would clear the guard while a real update is still pending
        # and let the rule refire immediately.
        if not self._active_mod_update_pending and not self._server_build_update_pending:
            self._mod_update_restart_in_progress = False

    def _maybe_auto_restart_for_update(self) -> None:
        """Restarts the server on its own when BOTH hold: an active mod or
        the server build itself has a pending update, and no players are
        online. Re-evaluated after every Workshop Update Check, every server
        build check, and every player-list refresh, since any of those can
        flip independently."""
        if not self.config.mod_update_autorestart_enabled:
            return
        if not self._ptero or self._restart_countdown_active or self._mod_update_restart_in_progress:
            return
        if not (self._active_mod_update_pending or self._server_build_update_pending) or self._players:
            return
        self._mod_update_restart_in_progress = True
        reason = "Mod update" if self._active_mod_update_pending else "Server build update"
        self.statusBar().showMessage(f"{reason} pending and server is empty -- auto-restarting to apply it", 6000)
        self._send_power_action("restart")

    # -- freeze / restore Workshop items (right-click on the update-check table) ------

    def _show_workshop_context_menu(self, pos) -> None:
        row = self.workshop_table.rowAt(pos.y())
        item = self.workshop_table.item(row, 0)
        if row < 0 or item is None:
            return
        workshop_id = item.text()
        is_frozen = workshop_id in self.config.frozen_mods
        menu = QMenu(self)
        active_action = menu.addAction("Active")
        active_action.setCheckable(True)
        active_action.setChecked(not is_frozen)
        active_action.setEnabled(is_frozen)
        active_action.triggered.connect(lambda: self._unfreeze_workshop_item(workshop_id))
        freeze_action = menu.addAction("Freeze")
        freeze_action.setCheckable(True)
        freeze_action.setChecked(is_frozen)
        freeze_action.setEnabled(not is_frozen)
        freeze_action.triggered.connect(lambda: self._freeze_workshop_item(workshop_id))

        menu.addSeparator()
        view_action = menu.addAction("View Workshop Page")
        view_action.triggered.connect(lambda: self._open_workshop_page(workshop_id))

        if not is_frozen:
            menu.addSeparator()
            resolve_action = menu.addAction("Find Mod ID(s)")
            resolve_action.setToolTip("Scan this item's downloaded files and add any Mod ID(s) found to the Mods list")
            resolve_action.triggered.connect(lambda: self._resolve_mod_ids(workshop_id))

            log_action = menu.addAction("Check Logs for Errors")
            log_action.setToolTip("Re-scan the console log for warning/error lines mentioning this item's Mod ID(s)")
            log_action.triggered.connect(lambda: self._check_workshop_item_logs(workshop_id))

            has_flagged_issue = workshop_id in self._workshop_not_working or workshop_id in self._workshop_missing_mod_ids
            is_dismissed = workshop_id in self.config.dismissed_mod_issues
            if is_dismissed:
                clear_dismiss_action = menu.addAction("Clear Dismissal")
                clear_dismiss_action.setToolTip("Stop suppressing this item's 'Not working'/'Missing Mod ID(s)' flag")
                clear_dismiss_action.triggered.connect(lambda: self._clear_mod_issue_dismissal(workshop_id))
            elif has_flagged_issue:
                dismiss_action = menu.addAction("Mark OK until next update")
                dismiss_action.setToolTip(
                    "Confirmed false positive -- suppress the flag for this item until it updates again on Steam Workshop"
                )
                dismiss_action.triggered.connect(lambda: self._mark_mod_issue_ok(workshop_id))

        menu.addSeparator()
        remove_action = menu.addAction("Remove")
        remove_action.triggered.connect(lambda: self._remove_workshop_item(workshop_id))

        missing_deps = self._workshop_missing_deps.get(workshop_id, [])
        if missing_deps:
            menu.addSeparator()
            if len(missing_deps) == 1:
                dep_action = menu.addAction(f"Add missing dependency: {missing_deps[0]}")
                dep_action.triggered.connect(lambda dep=missing_deps[0]: self._search_for_missing_dependency(dep))
            else:
                dep_menu = menu.addMenu("Add missing dependency")
                for dep in missing_deps:
                    dep_action = dep_menu.addAction(dep)
                    dep_action.triggered.connect(lambda _checked=False, d=dep: self._search_for_missing_dependency(d))

        menu.exec(self.workshop_table.viewport().mapToGlobal(pos))

    def _search_for_missing_dependency(self, mod_id: str) -> None:
        self._left_tabs.setCurrentWidget(self._browse_mods_tab)
        self.workshop_search_edit.setText(mod_id)
        self._search_workshop()

    def _resolve_mod_ids(self, workshop_id: str) -> None:
        # Covers the gap Add-from-search leaves: it can only add the Workshop
        # ID (the Mod ID(s) aren't knowable until the content downloads), so
        # this is the follow-up step for after that first restart.
        self.statusBar().showMessage(f"Looking up Mod ID(s) for {workshop_id}...", 4000)
        self._run_async("resolve_mod_ids", lambda: (workshop_id, sorted(self._find_mod_ids_for_workshop_item(workshop_id))))

    def _apply_resolved_mod_ids(self, workshop_id: str, mod_ids: list[str]) -> None:
        if not mod_ids:
            self.statusBar().showMessage(
                f"No downloaded mod.info found yet for {workshop_id} -- has the server restarted since it was added?",
                8000,
            )
            return
        mod_lines = [line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip()]
        new_ids = [mid for mid in mod_ids if mid not in mod_lines]
        if not new_ids:
            self.statusBar().showMessage(f"Mod ID(s) for {workshop_id} were already in the list", 5000)
            return
        mod_lines.extend(new_ids)
        self.mods_ids_edit.setPlainText("\n".join(mod_lines))
        self.statusBar().showMessage(
            f"Added Mod ID(s) for {workshop_id}: {', '.join(new_ids)} -- click Save to apply", 8000
        )
        self._prompt_mod_add_next_step()

    # -- log check / dismiss false positives (right-click on the update-check table) --

    def _check_workshop_item_logs(self, workshop_id: str) -> None:
        mod_ids = sorted(self._workshop_mod_id_map.get(workshop_id, set()))
        self.statusBar().showMessage(f"Checking console log for {workshop_id}...", 4000)
        self._run_async("check_workshop_logs", lambda: (workshop_id, mod_ids, self._do_check_workshop_item_logs(mod_ids)))

    def _do_check_workshop_item_logs(self, mod_ids: list[str]) -> list[str]:
        """Re-reads the same console log the "Not working" check uses (see
        _do_check_workshop_updates) and pulls out every warning/error line
        mentioning one of this item's own Mod ID(s) -- so a flagged item can
        be inspected on the spot instead of having to go dig through the
        Files tab by hand."""
        console_path = self._console_log_path()
        if not self._sftp or not console_path or not mod_ids:
            return []
        try:
            log_text = self._sftp.read_file(console_path)
        except SftpError:
            return []
        needles = [mid.lower() for mid in mod_ids if mid]
        matches = []
        for line in log_text.splitlines():
            low = line.lower()
            if any(needle in low for needle in needles) and any(
                marker in low for marker in ("warn", "error", "exception", "not found")
            ):
                matches.append(line)
        return matches

    def _apply_workshop_log_check(self, workshop_id: str, mod_ids: list[str], matches: list[str]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Log check -- {workshop_id}")
        dialog.resize(720, 420)
        layout = QVBoxLayout(dialog)
        ids_text = ", ".join(mod_ids) if mod_ids else "(no Mod ID(s) known yet -- has the server restarted since this item was added?)"
        summary = f"Found {len(matches)} matching line(s)" if matches else "No matching warning/error lines found"
        layout.addWidget(QLabel(f"{summary} for Mod ID(s): {ids_text}"))
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText("\n".join(matches) if matches else "(nothing matched in the console log)")
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _mark_mod_issue_ok(self, workshop_id: str) -> None:
        time_updated = self._workshop_time_updated.get(workshop_id)
        if time_updated is None:
            self.statusBar().showMessage("Can't dismiss -- no known Workshop update time for this item yet", 6000)
            return
        self.config.dismissed_mod_issues[workshop_id] = time_updated
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self.statusBar().showMessage("Marked OK -- flag suppressed until this item updates again on Steam Workshop", 6000)
        if self._last_workshop_check_result is not None:
            self._populate_workshop_table(self._last_workshop_check_result)

    def _clear_mod_issue_dismissal(self, workshop_id: str) -> None:
        if self.config.dismissed_mod_issues.pop(workshop_id, None) is None:
            return
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self.statusBar().showMessage("Dismissal cleared -- flag will show again if still broken", 6000)
        if self._last_workshop_check_result is not None:
            self._populate_workshop_table(self._last_workshop_check_result)

    def _remove_from_active_lists(self, workshop_id: str, mod_ids: list[str]) -> None:
        workshop_lines = [wid for wid in self._active_workshop_ids() if wid != workshop_id]
        self.mods_workshop_edit.setPlainText("\n".join(workshop_lines))
        if mod_ids:
            mod_lines = [
                mid
                for mid in (line.strip() for line in self.mods_ids_edit.toPlainText().splitlines())
                if mid and mid not in mod_ids
            ]
            self.mods_ids_edit.setPlainText("\n".join(mod_lines))

    def _freeze_workshop_item(self, workshop_id: str) -> None:
        self.statusBar().showMessage(f"Freezing Workshop item {workshop_id}...", 4000)
        self._run_async("freeze_mod", lambda: (workshop_id, sorted(self._find_mod_ids_for_workshop_item(workshop_id))))

    def _apply_freeze(self, workshop_id: str, mod_ids: list[str]) -> None:
        self._remove_from_active_lists(workshop_id, mod_ids)
        self.config.frozen_mods[workshop_id] = mod_ids
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self.statusBar().showMessage(f"Froze Workshop item {workshop_id} -- click Save to apply", 8000)
        self._check_workshop_updates()

    def _remove_workshop_item(self, workshop_id: str) -> None:
        if (
            QMessageBox.question(
                self,
                "Remove Workshop item",
                f"Remove Workshop item {workshop_id} and its Mod ID(s) from the server's lists?\n\n"
                "Unlike Freeze, this isn't remembered anywhere -- you'd need to search/add it again to bring it back.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if workshop_id in self.config.frozen_mods:
            # Already parked with known Mod IDs -- no SFTP round-trip needed.
            mod_ids = self.config.frozen_mods.pop(workshop_id, [])
            self._apply_removal(workshop_id, mod_ids)
        else:
            self.statusBar().showMessage(f"Removing Workshop item {workshop_id}...", 4000)
            self._run_async("remove_mod", lambda: (workshop_id, sorted(self._find_mod_ids_for_workshop_item(workshop_id))))

    def _apply_removal(self, workshop_id: str, mod_ids: list[str]) -> None:
        self._remove_from_active_lists(workshop_id, mod_ids)
        self.config.frozen_mods.pop(workshop_id, None)
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self.statusBar().showMessage(f"Removed Workshop item {workshop_id} -- click Save to apply", 8000)
        self._check_workshop_updates()

    def _unfreeze_workshop_item(self, workshop_id: str) -> None:
        mod_ids = self.config.frozen_mods.pop(workshop_id, [])
        workshop_lines = self._active_workshop_ids()
        if workshop_id not in workshop_lines:
            workshop_lines.append(workshop_id)
            self.mods_workshop_edit.setPlainText("\n".join(workshop_lines))
        if mod_ids:
            mod_lines = [line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip()]
            for mod_id in mod_ids:
                if mod_id not in mod_lines:
                    mod_lines.append(mod_id)
            self.mods_ids_edit.setPlainText("\n".join(mod_lines))
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self.statusBar().showMessage(f"Restored Workshop item {workshop_id} to active -- click Save to apply", 6000)
        self._check_workshop_updates()

    # -- browse mods tab (Steam Workshop search) ---------------------------------------

    def _build_workshop_browse_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("Steam Web API key:"))
        self.steam_api_key_edit = QLineEdit(self.app_settings.steam_api_key)
        self.steam_api_key_edit.setPlaceholderText("from steamcommunity.com/dev/apikey")
        key_row.addWidget(self.steam_api_key_edit, 1)
        save_key_btn = QPushButton("Save Key")
        save_key_btn.clicked.connect(self._save_steam_api_key)
        key_row.addWidget(save_key_btn)
        layout.addLayout(key_row)

        key_hint_row = QHBoxLayout()
        key_hint = QLabel(
            'Free from steamcommunity.com/dev/apikey -- any placeholder like "localhost" works for the '
            "required domain field. Shared across every server profile, not just this one."
        )
        key_hint.setWordWrap(True)
        key_hint.setStyleSheet("color: white;")
        key_hint_row.addWidget(key_hint, 1)
        get_key_btn = QPushButton("Get API Key")
        get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://steamcommunity.com/dev/apikey")))
        key_hint_row.addWidget(get_key_btn)
        layout.addLayout(key_hint_row)

        search_row = QHBoxLayout()
        self.workshop_search_edit = QLineEdit()
        self.workshop_search_edit.setPlaceholderText("Search the Project Zomboid Workshop...")
        self.workshop_search_edit.returnPressed.connect(self._search_workshop)
        search_row.addWidget(self.workshop_search_edit, 1)
        self.workshop_search_btn = QPushButton("Search")
        self.workshop_search_btn.clicked.connect(self._search_workshop)
        search_row.addWidget(self.workshop_search_btn)
        layout.addLayout(search_row)

        self.workshop_search_table = QTableWidget(0, 5)
        self.workshop_search_table.setHorizontalHeaderLabels(["Workshop ID", "Title", "Last Updated", "Description", ""])
        self.workshop_search_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.workshop_search_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.workshop_search_table.verticalHeader().setDefaultSectionSize(48)
        header = self.workshop_search_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 100)
        header.resizeSection(1, 220)
        header.resizeSection(2, 100)
        header.resizeSection(4, 120)
        layout.addWidget(self.workshop_search_table, 1)
        return wrapper

    def _save_steam_api_key(self) -> None:
        self.app_settings.steam_api_key = self.steam_api_key_edit.text().strip()
        if self._save_app_settings_callback:
            self._save_app_settings_callback(self.app_settings)
        self.statusBar().showMessage("Steam API key saved -- used for every server profile", 4000)

    def _search_workshop(self) -> None:
        query = self.workshop_search_edit.text().strip()
        if not query:
            return
        if not self.app_settings.steam_api_key:
            self.statusBar().showMessage("Add and save a Steam Web API key above first", 6000)
            return
        self.workshop_search_btn.setEnabled(False)
        self.statusBar().showMessage(f"Searching Workshop for '{query}'...", 4000)
        api_key = self.app_settings.steam_api_key
        self._run_async("workshop_search", lambda: search_workshop(api_key, query))

    def _workshop_item_membership(self, workshop_id: str) -> str:
        if workshop_id in self._active_workshop_ids():
            return "active"
        if workshop_id in self.config.frozen_mods:
            return "frozen"
        return "new"

    def _populate_workshop_search_table(self, results: list[WorkshopSearchResult]) -> None:
        self.workshop_search_btn.setEnabled(True)
        # Sorting mid-insertion reorders rows out from under setItem/setCellWidget's
        # row indices -- disable while filling, enable after so header clicks work.
        self.workshop_search_table.setSortingEnabled(False)
        self.workshop_search_table.setRowCount(len(results))
        for row, result in enumerate(results):
            date_str = datetime.fromtimestamp(result.time_updated).strftime("%Y-%m-%d") if result.time_updated else "--"
            id_item = _NumericSortTableWidgetItem(result.workshop_id, int(result.workshop_id))
            self.workshop_search_table.setItem(row, 0, id_item)
            self.workshop_search_table.setItem(row, 1, QTableWidgetItem(result.title))
            date_item = _NumericSortTableWidgetItem(date_str, result.time_updated or 0)
            self.workshop_search_table.setItem(row, 2, date_item)
            description_item = QTableWidgetItem(result.description)
            # Full text on hover, not just the truncated "..." display -- wrapped
            # to a fixed width since Qt tooltips otherwise run as one long line.
            description_item.setToolTip(textwrap.fill(result.description, width=50))
            self.workshop_search_table.setItem(row, 3, description_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 2, 2, 2)
            actions_layout.setSpacing(4)

            add_button = QPushButton()
            add_button.setFixedWidth(50)
            # Qt's default disabled-button style dims the text a lot -- fine
            # for sighted users, but confirmed to be close to unreadable for
            # a colorblind one. Force full-contrast text in both states so
            # the label itself (not a color) carries the meaning.
            add_button.setStyleSheet("QPushButton:disabled { color: palette(button-text); }")
            membership = self._workshop_item_membership(result.workshop_id)
            if membership == "active":
                add_button.setText("Added")
                add_button.setEnabled(False)
            elif membership == "frozen":
                add_button.setText("Frozen")
                add_button.setEnabled(False)
                add_button.setToolTip("Already added but frozen -- use the Mods tab to reactivate it")
            else:
                add_button.setText("Add")
                add_button.clicked.connect(
                    lambda _checked=False, wid=result.workshop_id, btn=add_button: self._add_workshop_search_result(wid, btn)
                )
            actions_layout.addWidget(add_button)

            view_button = QPushButton("View")
            view_button.setFixedWidth(50)
            view_button.clicked.connect(lambda _checked=False, wid=result.workshop_id: self._open_workshop_page(wid))
            actions_layout.addWidget(view_button)

            self.workshop_search_table.setCellWidget(row, 4, actions)
        self.workshop_search_table.setSortingEnabled(True)
        self.statusBar().showMessage(f"{len(results)} result(s)" if results else "No results", 4000)

    def _add_workshop_search_result(self, workshop_id: str, button: QPushButton) -> None:
        ids = self._active_workshop_ids()
        if workshop_id not in ids:
            ids.append(workshop_id)
            self.mods_workshop_edit.setPlainText("\n".join(ids))
        button.setText("Added")
        button.setEnabled(False)
        self.statusBar().showMessage(f"Added {workshop_id} to Workshop Items -- checking its description for Mod ID(s)...", 6000)
        self._run_async("add_workshop_mod_ids", lambda: (workshop_id, self._do_fetch_description_mod_ids(workshop_id)))

    def _do_fetch_description_mod_ids(self, workshop_id: str) -> list[str]:
        # Confirmed live: many mod authors list "Mod ID: X" lines right in
        # their Workshop description, specifically so this is knowable before
        # the content ever downloads -- unlike scanning mod.info (see
        # _find_mod_ids_for_workshop_item), which needs a restart first.
        details = fetch_workshop_details([workshop_id])
        item = details.get(workshop_id)
        if item is None or not item.found:
            return []
        return parse_mod_ids_from_description(item.description)

    def _apply_description_mod_ids(self, workshop_id: str, mod_ids: list[str]) -> None:
        if not mod_ids:
            self.statusBar().showMessage(
                f"No Mod ID(s) listed in {workshop_id}'s description -- restart the server once, then "
                "right-click it in the Check for Updates table and use 'Find Mod ID(s)' instead.",
                10000,
            )
            # The Workshop ID itself was still added (see _add_workshop_search_result)
            # and a restart is needed before Mod ID resolution can go any further
            # anyway -- worth offering Save here rather than only after a Mod ID
            # is actually known.
            self._prompt_mod_add_next_step()
            return
        mod_lines = [line.strip() for line in self.mods_ids_edit.toPlainText().splitlines() if line.strip()]
        new_ids = [mid for mid in mod_ids if mid not in mod_lines]
        if new_ids:
            mod_lines.extend(new_ids)
            self.mods_ids_edit.setPlainText("\n".join(mod_lines))
        self.statusBar().showMessage(
            f"Found Mod ID(s) in {workshop_id}'s description: {', '.join(mod_ids)} -- added to the Mods list. "
            "That's the author's own documentation, not verified against the real files yet -- worth "
            "double-checking with 'Find Mod ID(s)' after your next restart.",
            12000,
        )
        if new_ids:
            self._prompt_mod_add_next_step()

    def _prompt_mod_add_next_step(self) -> None:
        """Asked right after a mod add actually changes the pending Mods
        list -- easy to add several mods in a row and forget the list still
        needs Save (and the server a restart) before any of them take
        effect. "Keep Adding" just dismisses so the same Browse Mods flow
        can continue; "Save Now" runs the normal Save so the server's ready
        to restart with what's been added so far."""
        box = QMessageBox(self)
        box.setWindowTitle("Mod added")
        box.setText("Keep browsing for more mods, or save now so the server's ready to restart with this one?")
        keep_btn = box.addButton("Keep Adding", QMessageBox.ButtonRole.RejectRole)
        save_btn = box.addButton("Save Now", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(save_btn)
        box.exec()
        if box.clickedButton() is save_btn:
            self._save_mods_settings()

    def _open_workshop_page(self, workshop_id: str) -> None:
        QDesktopServices.openUrl(QUrl(f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"))

    # -- server settings tab (.ini) ---------------------------------------------------

    def _build_ini_settings_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.sftp_host:
            note = QLabel("Editing the server .ini needs SFTP (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        top_row = QHBoxLayout()
        self.ini_path_label = QLabel(f"<tt>{self.config.ini_path or '(auto-detecting...)'}</tt>")
        top_row.addWidget(self.ini_path_label)
        top_row.addStretch(1)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load_ini_settings)
        top_row.addWidget(reload_btn)
        layout.addLayout(top_row)

        self.ini_search = QLineEdit()
        self.ini_search.setPlaceholderText("Search settings...")
        self.ini_search.setClearButtonEnabled(True)
        self.ini_search.textChanged.connect(self._filter_ini_form)
        layout.addWidget(self.ini_search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.ini_form_widget = QWidget()
        self.ini_form = QFormLayout(self.ini_form_widget)
        scroll.setWidget(self.ini_form_widget)
        layout.addWidget(scroll, 1)

        bottom_row = QHBoxLayout()
        self.ini_hint = QLabel("")
        self.ini_hint.setStyleSheet("color: palette(placeholder-text);")
        bottom_row.addWidget(self.ini_hint, 1)
        self.ini_save_btn = QPushButton("Save")
        self.ini_save_btn.setEnabled(False)
        self.ini_save_btn.clicked.connect(self._save_ini_settings)
        bottom_row.addWidget(self.ini_save_btn)
        layout.addLayout(bottom_row)
        return wrapper

    def _load_ini_settings(self) -> None:
        if not self._sftp or not self.config.ini_path:
            return
        if self._ini_dirty and not self._confirm_discard_ini_changes():
            return
        path = self.config.ini_path
        self._run_async("ini_load", lambda: (path, self._sftp.read_file(path)))

    def _confirm_discard_ini_changes(self) -> bool:
        return QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes to server settings?") == QMessageBox.StandardButton.Yes

    def _populate_ini_form(self) -> None:
        while self.ini_form.rowCount():
            self.ini_form.removeRow(0)
        self._ini_widgets.clear()
        self._ini_loading = True
        try:
            for prop in self._ini_properties:
                widget = self._make_ini_widget(prop)
                self._ini_widgets[prop.name] = widget
                if prop.name in _INI_STARTUP_OVERRIDES:
                    note_text = "controlled by Pterodactyl startup variable"
                elif prop.name in _INI_MODS_TAB_FIELDS:
                    note_text = "please update using the Mods tab"
                else:
                    note_text = None
                if note_text:
                    widget.setEnabled(False)
                    container = QWidget()
                    row_layout = QHBoxLayout(container)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.addWidget(widget)
                    note = QLabel(note_text)
                    note.setStyleSheet("color: palette(placeholder-text); font-style: italic;")
                    row_layout.addWidget(note)
                    row_layout.addStretch(1)
                    self.ini_form.addRow(prop.name, container)
                else:
                    self.ini_form.addRow(prop.name, widget)
        finally:
            self._ini_loading = False
        self._filter_ini_form(self.ini_search.text())
        self._set_ini_dirty(False)

    def _filter_ini_form(self, text: str) -> None:
        query = text.strip().lower()
        for row, prop in enumerate(self._ini_properties):
            self.ini_form.setRowVisible(row, not query or query in prop.name.lower())

    def _make_ini_widget(self, prop: IniProperty) -> QWidget:
        if prop.kind == "bool":
            widget = _FocusComboBox()
            widget.addItems(["true", "false"])
            widget.setCurrentText(prop.value.lower())
            widget.currentTextChanged.connect(self._mark_ini_dirty)
            return widget
        if prop.kind == "int":
            widget = _FocusSpinBox()
            widget.setRange(-2_000_000_000, 2_000_000_000)
            widget.setValue(int(prop.value))
            widget.valueChanged.connect(self._mark_ini_dirty)
            return widget
        if prop.kind == "float":
            widget = _FocusDoubleSpinBox()
            widget.setRange(-1_000_000_000.0, 1_000_000_000.0)
            decimals = len(prop.value.split(".", 1)[1]) if "." in prop.value else 1
            widget.setDecimals(max(decimals, 1))
            widget.setValue(float(prop.value))
            widget.valueChanged.connect(self._mark_ini_dirty)
            return widget
        widget = QLineEdit(prop.value)
        widget.textEdited.connect(self._mark_ini_dirty)
        return widget

    @staticmethod
    def _ini_widget_value(prop: IniProperty, widget: QWidget) -> str:
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if prop.kind == "int":
            return str(widget.value())
        if prop.kind == "float":
            return f"{widget.value():.{widget.decimals()}f}"
        return widget.text()

    def _mark_ini_dirty(self, *_args) -> None:
        if self._ini_loading:
            return
        self._set_ini_dirty(True)

    def _set_ini_dirty(self, dirty: bool) -> None:
        self._ini_dirty = dirty
        self.ini_save_btn.setEnabled(dirty)
        self.ini_hint.setText("Unsaved changes -- restart the server after saving for them to take effect." if dirty else "")

    def _save_ini_settings(self) -> None:
        if not self._sftp or self._ini_text is None or not self.config.ini_path:
            return
        changes = {}
        for prop in self._ini_properties:
            widget = self._ini_widgets.get(prop.name)
            if widget is None:
                continue
            new_value = self._ini_widget_value(prop, widget)
            if new_value != prop.value:
                changes[prop.name] = new_value
        if not changes:
            self.statusBar().showMessage("No changes to save", 4000)
            return
        ini_text, path = self._ini_text, self.config.ini_path
        self._run_async("ini_save", lambda: self._do_ini_save(ini_text, changes, path))

    def _do_ini_save(self, ini_text: str, changes: dict[str, str], path: str) -> str:
        new_text = apply_ini_changes(ini_text, changes)
        self._sftp.write_file(path, new_text)
        return new_text

    # -- sandbox settings tab (_SandboxVars.lua) -------------------------------------

    def _build_lua_settings_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.sftp_host:
            note = QLabel("Editing sandbox settings needs SFTP (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        top_row = QHBoxLayout()
        self.lua_path_label = QLabel(f"<tt>{self.config.sandboxvars_path or '(auto-detecting...)'}</tt>")
        top_row.addWidget(self.lua_path_label)
        top_row.addStretch(1)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load_lua_settings)
        top_row.addWidget(reload_btn)
        layout.addLayout(top_row)

        self.lua_search = QLineEdit()
        self.lua_search.setPlaceholderText("Search settings...")
        self.lua_search.setClearButtonEnabled(True)
        self.lua_search.textChanged.connect(self._filter_lua_form)
        layout.addWidget(self.lua_search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.lua_form_widget = QWidget()
        self.lua_form = QFormLayout(self.lua_form_widget)
        scroll.setWidget(self.lua_form_widget)
        layout.addWidget(scroll, 1)

        bottom_row = QHBoxLayout()
        self.lua_hint = QLabel("")
        self.lua_hint.setStyleSheet("color: palette(placeholder-text);")
        bottom_row.addWidget(self.lua_hint, 1)
        self.lua_save_btn = QPushButton("Save")
        self.lua_save_btn.setEnabled(False)
        self.lua_save_btn.clicked.connect(self._save_lua_settings)
        bottom_row.addWidget(self.lua_save_btn)
        layout.addLayout(bottom_row)
        return wrapper

    def _load_lua_settings(self) -> None:
        if not self._sftp or not self.config.sandboxvars_path:
            return
        if self._lua_dirty and not self._confirm_discard_lua_changes():
            return
        path = self.config.sandboxvars_path
        self._run_async("lua_load", lambda: (path, self._sftp.read_file(path)))

    def _confirm_discard_lua_changes(self) -> bool:
        return QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes to sandbox settings?") == QMessageBox.StandardButton.Yes

    def _populate_lua_form(self) -> None:
        while self.lua_form.rowCount():
            self.lua_form.removeRow(0)
        self._lua_widgets.clear()
        # row index -> the LuaProperty it holds, or None for a section-header
        # row -- tracked explicitly rather than inferred from the layout,
        # since row content can't be reliably introspected back out.
        self._lua_form_rows: list[tuple[int, LuaProperty | None]] = []
        self._lua_loading = True
        try:
            last_section = object()  # sentinel that never equals a real section string
            for prop in self._lua_properties:
                if prop.section != last_section:
                    header = QLabel(f"<b>{prop.section or 'General'}</b>")
                    self.lua_form.addRow(header)
                    self._lua_form_rows.append((self.lua_form.rowCount() - 1, None))
                    last_section = prop.section
                widget = self._make_lua_widget(prop)
                self._lua_widgets[prop.path] = widget
                self.lua_form.addRow(prop.name, widget)
                self._lua_form_rows.append((self.lua_form.rowCount() - 1, prop))
        finally:
            self._lua_loading = False
        self._filter_lua_form(self.lua_search.text())
        self._set_lua_dirty(False)

    def _filter_lua_form(self, text: str) -> None:
        query = text.strip().lower()
        section_has_match: dict[str, bool] = {}
        for _row, prop in self._lua_form_rows:
            if prop is not None:
                visible = not query or query in prop.name.lower() or query in (prop.section or "").lower()
                self.lua_form.setRowVisible(_row, visible)
                section_has_match[prop.section] = section_has_match.get(prop.section, False) or visible

        # A section header stays visible if any row underneath it matched,
        # even when the section name itself didn't match the query. Walk
        # backwards so each header sees the section that follows it.
        pending_section = None
        for row, prop in reversed(self._lua_form_rows):
            if prop is None:
                self.lua_form.setRowVisible(row, section_has_match.get(pending_section, not query))
            else:
                pending_section = prop.section

    def _make_lua_widget(self, prop: LuaProperty) -> QWidget:
        if prop.kind == "bool":
            widget = _FocusComboBox()
            widget.addItems(["true", "false"])
            widget.setCurrentText(prop.value)
            widget.currentTextChanged.connect(self._mark_lua_dirty)
        elif prop.kind == "int":
            widget = _FocusSpinBox()
            widget.setRange(-2_000_000_000, 2_000_000_000)
            widget.setValue(int(prop.value))
            widget.valueChanged.connect(self._mark_lua_dirty)
        elif prop.kind == "float":
            widget = _FocusDoubleSpinBox()
            widget.setRange(-1_000_000_000.0, 1_000_000_000.0)
            decimals = len(prop.value.split(".", 1)[1]) if "." in prop.value else 2
            widget.setDecimals(max(decimals, 1))
            widget.setValue(float(prop.value))
            widget.valueChanged.connect(self._mark_lua_dirty)
        else:
            widget = QLineEdit(prop.value.strip('"'))
            widget.textEdited.connect(self._mark_lua_dirty)
        if prop.help_text:
            widget.setToolTip(prop.help_text)
        return widget

    @staticmethod
    def _lua_widget_value(prop: LuaProperty, widget: QWidget) -> str:
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if prop.kind == "int":
            return str(widget.value())
        if prop.kind == "float":
            return f"{widget.value():.{widget.decimals()}f}"
        return f'"{widget.text()}"'

    def _mark_lua_dirty(self, *_args) -> None:
        if self._lua_loading:
            return
        self._set_lua_dirty(True)

    def _set_lua_dirty(self, dirty: bool) -> None:
        self._lua_dirty = dirty
        self.lua_save_btn.setEnabled(dirty)
        self.lua_hint.setText("Unsaved changes -- restart the server after saving for them to take effect." if dirty else "")

    def _save_lua_settings(self) -> None:
        if not self._sftp or self._lua_text is None or not self.config.sandboxvars_path:
            return
        changes = {}
        for prop in self._lua_properties:
            widget = self._lua_widgets.get(prop.path)
            if widget is None:
                continue
            new_value = self._lua_widget_value(prop, widget)
            if new_value != prop.value:
                changes[prop.path] = new_value
        if not changes:
            self.statusBar().showMessage("No changes to save", 4000)
            return
        lua_text, path = self._lua_text, self.config.sandboxvars_path
        self._run_async("lua_save", lambda: self._do_lua_save(lua_text, changes, path))

    def _do_lua_save(self, lua_text: str, changes: dict[str, str], path: str) -> str:
        new_text = apply_lua_changes(lua_text, changes)
        self._sftp.write_file(path, new_text)
        return new_text

    # -- config path auto-discovery --------------------------------------------------

    def _discover_config_paths(self) -> None:
        if self.config.ini_path and self.config.sandboxvars_path:
            self._load_ini_settings()
            self._load_lua_settings()
            self.refresh_bans()
            return
        self._run_async("discover_paths", self._do_discover_paths)

    # Candidate roots for the "Server" folder holding the .ini/_SandboxVars.lua --
    # varies by Pterodactyl egg/node: some chroot SFTP straight to the server's
    # data dir (".cache/Server" at the SFTP root), others expose the container's
    # full home dir instead, one level higher (confirmed live: "home/container"
    # showed up as real folders in the SFTP browser, not just a chroot illusion).
    _SERVER_DIR_CANDIDATES = ("/.cache/Server", "/home/container/.cache/Server")

    def _do_discover_paths(self) -> tuple[str | None, str | None]:
        ini_matches: list[str] = []
        lua_matches: list[str] = []
        for candidate in self._SERVER_DIR_CANDIDATES:
            if not ini_matches:
                ini_matches = self._sftp.find_files(candidate, lambda n: n.lower().endswith(".ini"))
            if not lua_matches:
                lua_matches = self._sftp.find_files(candidate, lambda n: n.lower().endswith("_sandboxvars.lua"))
            if ini_matches and lua_matches:
                break
        if not ini_matches:
            ini_matches = self._sftp.find_files("/", lambda n: n.lower().endswith(".ini"), max_depth=6)
        if not lua_matches:
            lua_matches = self._sftp.find_files("/", lambda n: n.lower().endswith("_sandboxvars.lua"), max_depth=6)
        return (ini_matches[0] if ini_matches else None, lua_matches[0] if lua_matches else None)

    # -- auto restart -----------------------------------------------------------------

    def _build_autorestart_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.pterodactyl_host:
            note = QLabel("Automatic restarts require a Pterodactyl connection (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        enabled_row = QHBoxLayout()
        self.autorestart_enabled_box = QComboBox()
        self.autorestart_enabled_box.addItems(["Automatic Restarts: OFF", "Automatic Restarts: ON"])
        self.autorestart_enabled_box.setCurrentIndex(1 if self.config.autorestart_enabled else 0)
        enabled_row.addWidget(self.autorestart_enabled_box)
        enabled_row.addStretch(1)
        layout.addLayout(enabled_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Restart:"))
        self.autorestart_mode_box = QComboBox()
        self.autorestart_mode_box.addItems(["At a specific time each day", "Every N hours"])
        self.autorestart_mode_box.setCurrentIndex(1 if self.config.autorestart_mode == "interval" else 0)
        mode_row.addWidget(self.autorestart_mode_box)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.autorestart_time_widget = QWidget()
        time_row = QHBoxLayout(self.autorestart_time_widget)
        time_row.addWidget(QLabel("Time of day:"))
        self.autorestart_time_edit = QTimeEdit()
        self.autorestart_time_edit.setDisplayFormat("HH:mm")
        hh, _, mm = self.config.autorestart_time.partition(":")
        self.autorestart_time_edit.setTime(QTime(int(hh or 0), int(mm or 0)))
        time_row.addWidget(self.autorestart_time_edit)
        time_row.addStretch(1)
        layout.addWidget(self.autorestart_time_widget)

        self.autorestart_interval_widget = QWidget()
        interval_row = QHBoxLayout(self.autorestart_interval_widget)
        interval_row.addWidget(QLabel("Every:"))
        self.autorestart_interval_spin = QDoubleSpinBox()
        self.autorestart_interval_spin.setRange(0.5, 168.0)
        self.autorestart_interval_spin.setSingleStep(0.5)
        self.autorestart_interval_spin.setSuffix(" hours")
        self.autorestart_interval_spin.setValue(self.config.autorestart_interval_hours)
        interval_row.addWidget(self.autorestart_interval_spin)
        interval_row.addStretch(1)
        layout.addWidget(self.autorestart_interval_widget)

        warning_hint = QLabel("Broadcast 5 minutes and 1 minute before the restart, then a countdown for the last 20 seconds. Use {minutes}.")
        warning_hint.setWordWrap(True)
        warning_hint.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(warning_hint)
        self.autorestart_warning_edit = QLineEdit(self.config.autorestart_warning_message)
        layout.addWidget(self.autorestart_warning_edit)

        self.autorestart_status_label = QLabel("")
        self.autorestart_status_label.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(self.autorestart_status_label)

        layout.addStretch(1)

        self.autorestart_enabled_box.currentIndexChanged.connect(self._save_autorestart_settings)
        self.autorestart_mode_box.currentIndexChanged.connect(self._save_autorestart_settings)
        self.autorestart_time_edit.timeChanged.connect(self._save_autorestart_settings)
        self.autorestart_interval_spin.valueChanged.connect(self._save_autorestart_settings)
        self.autorestart_warning_edit.editingFinished.connect(self._save_autorestart_settings)

        self._update_autorestart_mode_visibility()
        self._update_autorestart_status_label()
        return wrapper

    def _update_autorestart_mode_visibility(self) -> None:
        is_interval = self.autorestart_mode_box.currentIndex() == 1
        self.autorestart_time_widget.setVisible(not is_interval)
        self.autorestart_interval_widget.setVisible(is_interval)

    def _update_autorestart_status_label(self) -> None:
        if not self.config.autorestart_enabled:
            self.autorestart_status_label.setText("Automatic restarts are off.")
        elif self.config.autorestart_mode == "interval":
            self.autorestart_status_label.setText(f"Restarts the server every {self.config.autorestart_interval_hours:g} hours while this app is running.")
        else:
            self.autorestart_status_label.setText(f"Restarts the server at {self.config.autorestart_time} every day while this app is running.")

    def _save_autorestart_settings(self) -> None:
        self.config.autorestart_enabled = self.autorestart_enabled_box.currentIndex() == 1
        self.config.autorestart_mode = "interval" if self.autorestart_mode_box.currentIndex() == 1 else "time"
        self.config.autorestart_time = self.autorestart_time_edit.time().toString("HH:mm")
        self.config.autorestart_interval_hours = self.autorestart_interval_spin.value()
        self.config.autorestart_warning_message = self.autorestart_warning_edit.text().strip() or DEFAULT_RESTART_WARNING_MESSAGE
        if self._save_config_callback:
            self._save_config_callback(self.config)
        self._next_autorestart_at = None
        self._update_autorestart_mode_visibility()
        self._update_autorestart_status_label()

    def _next_time_mode_restart_at(self, now: datetime) -> datetime:
        hh, _, mm = self.config.autorestart_time.partition(":")
        target = now.replace(hour=int(hh or 0), minute=int(mm or 0), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _check_autorestart(self) -> None:
        if not self.config.autorestart_enabled or not self._ptero or self._restart_countdown_active:
            return
        if self.config.autorestart_mode == "interval":
            interval_seconds = self.config.autorestart_interval_hours * 3600
            if self._next_autorestart_at is None:
                self._next_autorestart_at = time.monotonic() + interval_seconds
                return
            seconds_until = self._next_autorestart_at - time.monotonic()
        else:
            seconds_until = (self._next_time_mode_restart_at(datetime.now()) - datetime.now()).total_seconds()
        if seconds_until <= self.RESTART_WARNING_SECONDS:
            self._begin_restart_countdown(max(seconds_until, 0))

    def _begin_restart_countdown(self, seconds_until: float) -> None:
        self._restart_countdown_active = True
        self._restart_seconds_remaining = max(round(seconds_until), 0)
        self._restart_warned_one_minute = False
        minutes = max(round(seconds_until / 60), 1)
        self._broadcast_message(self.config.autorestart_warning_message.format(minutes=minutes))
        self._update_restart_button_text()
        timer = QTimer(self)
        timer.timeout.connect(self._tick_restart_countdown)
        timer.start(1000)
        self._restart_countdown_timer = timer

    def _tick_restart_countdown(self) -> None:
        self._restart_seconds_remaining -= 1
        remaining = self._restart_seconds_remaining
        self._update_restart_button_text()
        if remaining <= 0:
            self._finish_restart_countdown()
        elif remaining == 60 and not self._restart_warned_one_minute:
            self._restart_warned_one_minute = True
            self._broadcast_message(self.config.autorestart_warning_message.format(minutes=1))
        elif remaining <= self.RESTART_FINAL_COUNTDOWN_SECONDS:
            self._broadcast_message(str(remaining))

    def _update_restart_button_text(self) -> None:
        if self._restart_button is None:
            return
        if self._restart_countdown_active:
            minutes, seconds = divmod(max(self._restart_seconds_remaining, 0), 60)
            self._restart_button.setText(f"Cancel Restart ({minutes}:{seconds:02d})")
        else:
            self._restart_button.setText("Restart")

    def _update_start_stop_button_text(self) -> None:
        if self._start_button is not None:
            self._start_button.setText("Started" if self._ptero_status == "running" else "Start")
        if self._stop_button is not None:
            self._stop_button.setText("Stopped" if self._ptero_status == "offline" else "Stop")

    def _stop_restart_countdown_timer(self) -> None:
        if self._restart_countdown_timer is not None:
            self._restart_countdown_timer.stop()
            self._restart_countdown_timer = None

    def _cancel_restart_countdown(self) -> None:
        if not self._restart_countdown_active:
            return
        self._restart_countdown_active = False
        self._stop_restart_countdown_timer()
        self._update_restart_button_text()
        self._next_autorestart_at = None
        self.statusBar().showMessage("Restart cancelled", 5000)
        self._broadcast_message("Restart cancelled -- carry on!")

    def _finish_restart_countdown(self) -> None:
        self._stop_restart_countdown_timer()
        self._restart_countdown_active = False
        self._update_restart_button_text()
        self._next_autorestart_at = None
        self.statusBar().showMessage("Restart triggered", 6000)
        self._broadcast_message("Restarting now -- back shortly!")
        self._send_power_action("restart")

    def _on_restart_clicked(self) -> None:
        if self._restart_countdown_active:
            self._cancel_restart_countdown()
        else:
            self._send_power_action("restart")

    # -- broadcasts (join only -- see config.py for why death/level-up aren't here) --

    def _build_broadcasts_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if not self.config.rcon_host:
            note = QLabel("Server broadcasts require an RCON connection (not configured for this server).")
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(note)

        join_row = QHBoxLayout()
        self.broadcast_join_enabled_box = QComboBox()
        self.broadcast_join_enabled_box.addItems(["Join Messages: OFF", "Join Messages: ON"])
        self.broadcast_join_enabled_box.setCurrentIndex(1 if self.config.broadcast_join_enabled else 0)
        join_row.addWidget(self.broadcast_join_enabled_box)
        join_row.addStretch(1)
        layout.addLayout(join_row)

        join_hint = QLabel(
            "Sent when a player's name appears in the RCON player list that wasn't there on the "
            "previous poll (up to 15s delay). Use {name}."
        )
        join_hint.setWordWrap(True)
        join_hint.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(join_hint)
        self.broadcast_join_message_edit = QLineEdit(self.config.broadcast_join_message)
        layout.addWidget(self.broadcast_join_message_edit)

        death_note = QLabel(
            "Death/level-up broadcasts aren't included -- they'd need parsing PZ's console log "
            "for events that haven't been verified against a live server yet."
        )
        death_note.setWordWrap(True)
        death_note.setStyleSheet("color: palette(placeholder-text); font-style: italic;")
        layout.addWidget(death_note)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_broadcast_settings)
        save_row.addWidget(save_btn)
        layout.addLayout(save_row)

        layout.addStretch(1)

        self.broadcast_join_enabled_box.currentIndexChanged.connect(self._save_broadcast_settings)
        return wrapper

    def _save_broadcast_settings(self, *_args) -> None:
        self.config.broadcast_join_enabled = self.broadcast_join_enabled_box.currentIndex() == 1
        self.config.broadcast_join_message = self.broadcast_join_message_edit.text().strip() or DEFAULT_JOIN_MESSAGE
        if self._save_config_callback:
            self._save_config_callback(self.config)

    def _broadcast_message(self, text: str) -> None:
        if self._rcon:
            self._run_async("say", lambda: self._rcon.say(text))

    # -- console / chat -----------------------------------------------------------

    def _build_console_panel(self) -> QWidget:
        tabs = QTabWidget()
        has_ptero = bool(self.config.pterodactyl_host)

        self.console_view, self.console_input = self._add_console_tab(
            tabs,
            "Console",
            note=None if has_ptero else "Live console needs Pterodactyl (not configured for this server).",
            view_placeholder="" if has_ptero else "(no live console -- connect Pterodactyl to see server output here)",
            input_placeholder="Type an RCON command and press Enter",
            on_send=self._send_console_command,
        )
        self.chat_view, self.chat_input = self._add_console_tab(
            tabs,
            "Chat",
            note="Only shows messages sent from this app -- inbound chat detection isn't verified "
            "against this server's console log format yet. Full raw output (including real player "
            "chat) is visible in the Console tab.",
            view_placeholder="",
            input_placeholder="Type a chat message and press Enter",
            on_send=self._send_chat_message,
        )
        return tabs

    def _add_console_tab(
        self,
        tabs: QTabWidget,
        title: str,
        *,
        note: str | None,
        view_placeholder: str,
        input_placeholder: str,
        on_send: Callable[[], None],
    ) -> tuple[QPlainTextEdit, QLineEdit]:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        if note:
            label = QLabel(note)
            label.setWordWrap(True)
            label.setStyleSheet("color: palette(placeholder-text);")
            layout.addWidget(label)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setMaximumBlockCount(5000)
        view.setPlaceholderText(view_placeholder)
        layout.addWidget(view, 1)

        send_row = QHBoxLayout()
        line_edit = QLineEdit()
        line_edit.setPlaceholderText(input_placeholder)
        line_edit.returnPressed.connect(on_send)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(on_send)
        send_row.addWidget(line_edit, 1)
        send_row.addWidget(send_btn)
        layout.addLayout(send_row)

        tabs.addTab(wrapper, title)
        return view, line_edit

    def _start_console_stream(self) -> None:
        if not self._ptero:
            return
        self._console = ConsoleStream(self._ptero, on_line=self._append_console_line, on_status=self._on_ptero_status)
        self._console.start()

    def _append_console_line(self, line: str) -> None:
        # Called from the websocket thread -- marshal to the UI thread.
        self._bridge.result.emit("console_line", line)

    def _on_ptero_status(self, status: str) -> None:
        self._bridge.result.emit("ptero_status", status)

    def _send_console_command(self) -> None:
        text = self.console_input.text().strip()
        if not text:
            return
        if not self._rcon:
            self.statusBar().showMessage("RCON is not connected", 4000)
            return
        self.console_input.clear()
        self.console_view.appendPlainText(f"> {text}")
        self._run_async("console_command", lambda: self._rcon.run_command(text))

    def _send_chat_message(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        if not self._rcon:
            self.statusBar().showMessage("RCON is not connected", 4000)
            return
        self.chat_input.clear()
        self.chat_view.appendPlainText(f"You: {text}")
        self._run_async("chat_message", lambda: self._rcon.say(text))

    # -- power controls ---------------------------------------------------------------

    def _send_power_action(self, action: str) -> None:
        if not self._ptero:
            return
        if action in ("stop", "kill") and QMessageBox.question(self, "Confirm", f"Send '{action}' to the server?") != QMessageBox.StandardButton.Yes:
            return
        if action in ("stop", "restart") and self._rcon:
            self.statusBar().showMessage(f"Saving world before sending '{action}'...", 8000)
        if action in ("restart", "start"):
            # The server (re)downloads Workshop content on boot, so whatever
            # Steam reports right now is what it'll actually be running --
            # clear the pinned baseline so the next Check for Updates
            # re-establishes it fresh instead of comparing against a
            # pre-restart snapshot and flagging a false "Updated!".
            self.config.known_workshop_updates.clear()
            self._stop_mod_update_flash()
            if self._save_config_callback:
                self._save_config_callback(self.config)
        self._run_async("power_action", lambda: self._do_power_action(action))

    def _do_power_action(self, action: str) -> str:
        # Stop/restart otherwise drop straight to a container shutdown with no
        # warning -- save first so it's not an instant, unsaved-progress kill.
        # Kill is deliberately excluded: it's meant for a server that's already
        # unresponsive, where waiting on an RCON round-trip could just hang.
        if action in ("stop", "restart") and self._rcon:
            try:
                self._rcon.save_world()
            except RconError:
                pass  # best-effort -- still send the power action even if the save failed
        self._ptero.send_power_action(action)
        return action

    def _save_world(self) -> None:
        if self._rcon:
            self._run_async("save_world", self._rcon.save_world)

    # -- backend connections -----------------------------------------------------

    def _connect_backends(self) -> None:
        if self.config.rcon_host:
            self._run_async("rcon_connect", self._connect_rcon)
        if self.config.pterodactyl_host and self.config.pterodactyl_api_key and self.config.pterodactyl_server_id:
            self._run_async("ptero_connect", self._connect_pterodactyl)
        else:
            self.statusBar().showMessage("No Pterodactyl connection configured -- power controls and live console/chat are disabled", 8000)
        if self.config.sftp_host and self.config.sftp_username:
            self._run_async("sftp_connect", self._connect_sftp)

    def _connect_rcon(self):
        client = RconClient(self.config.rcon_host, self.config.rcon_port, self.config.rcon_password, on_disconnect=self._handle_rcon_disconnected)
        client.connect()
        return client

    def _connect_pterodactyl(self):
        return PterodactylClient(self.config.pterodactyl_base_url, self.config.pterodactyl_api_key, self.config.pterodactyl_server_id)

    def _connect_sftp(self):
        client = SftpClient(self.config.sftp_host, self.config.sftp_port, self.config.sftp_username, self.config.sftp_password)
        client.connect()
        return client

    # -- rcon status / reconnection ---------------------------------------------

    def _set_rcon_status(self, connected: bool) -> None:
        if self._rcon_status_dot is not None:
            self._paint_status_dot(self._rcon_status_dot, connected, "RCON")

    def _set_sftp_status(self, connected: bool) -> None:
        if self._sftp_status_dot is not None:
            self._paint_status_dot(self._sftp_status_dot, connected, "SFTP")

    def _handle_rcon_disconnected(self) -> None:
        # Called from the RCON socket's calling thread -- marshal to the UI thread.
        self._bridge.result.emit("rcon_disconnected", None)

    def _schedule_rcon_reconnect(self) -> None:
        if self._rcon_reconnect_timer is not None or not self.config.rcon_host:
            return
        self._rcon_reconnect_timer = QTimer(self)
        self._rcon_reconnect_timer.setSingleShot(True)
        self._rcon_reconnect_timer.timeout.connect(self._attempt_rcon_reconnect)
        self._rcon_reconnect_timer.start(self.RCON_RECONNECT_INTERVAL_MS)

    def _attempt_rcon_reconnect(self) -> None:
        self._rcon_reconnect_timer = None
        if self._rcon is not None:
            return
        self.statusBar().showMessage("Reconnecting to RCON...", 4000)
        self._run_async("rcon_reconnect", self._connect_rcon)

    # -- sftp status / reconnection ------------------------------------------------

    def _start_sftp_health_check(self) -> None:
        if self._sftp_health_timer is not None:
            return
        self._sftp_health_timer = QTimer(self)
        self._sftp_health_timer.timeout.connect(self._check_sftp_connection)
        self._sftp_health_timer.start(self.SFTP_HEALTH_CHECK_INTERVAL_MS)

    def _check_sftp_connection(self) -> None:
        if self._sftp is not None and not self._sftp.is_connected():
            self._handle_sftp_disconnected()

    def _pause_sftp_health_check(self) -> None:
        # A backup/restore cycle holds onto self._sftp for a long stretch (stop
        # wait + a full folder copy -- routinely well over a minute), unlike
        # every other SFTP action in this app which is sub-second. If the
        # health check fires mid-copy and decides the connection is dead, it
        # closes it out from under the background thread still using it and
        # swaps self._sftp to None/a fresh reconnect -- confirmed as the likely
        # cause of a backup finishing (visible on the server) but the app's own
        # list never picking it back up afterward. Paused for the duration of
        # the operation and resumed once it reports back (success or error).
        if self._sftp_health_timer is not None:
            self._sftp_health_timer.stop()

    def _resume_sftp_health_check(self) -> None:
        if self._sftp_health_timer is not None:
            self._sftp_health_timer.start(self.SFTP_HEALTH_CHECK_INTERVAL_MS)

    def _handle_sftp_disconnected(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        self._set_sftp_status(False)
        self.statusBar().showMessage("SFTP connection lost -- will retry...", 4000)
        self._schedule_sftp_reconnect()

    def _schedule_sftp_reconnect(self) -> None:
        if self._sftp_reconnect_timer is not None or not self.config.sftp_host:
            return
        self._sftp_reconnect_timer = QTimer(self)
        self._sftp_reconnect_timer.setSingleShot(True)
        self._sftp_reconnect_timer.timeout.connect(self._attempt_sftp_reconnect)
        self._sftp_reconnect_timer.start(self.SFTP_RECONNECT_INTERVAL_MS)

    def _attempt_sftp_reconnect(self) -> None:
        self._sftp_reconnect_timer = None
        if self._sftp is not None:
            return
        self.statusBar().showMessage("Reconnecting to SFTP...", 4000)
        self._run_async("sftp_reconnect", self._connect_sftp)

    # -- async helper -------------------------------------------------------------

    def _run_async(self, tag: str, fn: Callable[[], object]) -> None:
        def task():
            try:
                value = fn()
            except Exception as exc:  # noqa: BLE001 -- surfaced to the UI as a message
                self._bridge.error.emit(tag, str(exc))
            else:
                self._bridge.result.emit(tag, value)

        threading.Thread(target=task, daemon=True).start()

    def _on_async_result(self, tag: str, value: object) -> None:
        if tag in ("rcon_connect", "rcon_reconnect"):
            self._rcon = value  # type: ignore[assignment]
            self._set_rcon_status(True)
            self.statusBar().showMessage("RCON connected", 5000)
            self.refresh_players()
            self.refresh_bans()
        elif tag == "rcon_disconnected":
            self._rcon = None
            self._set_rcon_status(False)
            self.statusBar().showMessage("RCON connection lost -- reconnecting...", 6000)
            self._schedule_rcon_reconnect()
        elif tag == "ptero_connect":
            self._ptero = value  # type: ignore[assignment]
            self.statusBar().showMessage("Pterodactyl connected", 5000)
            self._start_console_stream()
        elif tag == "list_players":
            self._players = value  # type: ignore[assignment]
            self._update_known_players()
            self._populate_player_table()
            self._maybe_auto_restart_for_update()
        elif tag == "bans_load":
            self._populate_ban_table(value)  # type: ignore[arg-type]
        elif tag == "discover_paths":
            ini_path, lua_path = value  # type: ignore[misc]
            changed = False
            if ini_path and ini_path != self.config.ini_path:
                self.config.ini_path = ini_path
                self.ini_path_label.setText(f"<tt>{ini_path}</tt>")
                self.mods_path_label.setText(f"<tt>{ini_path}</tt>")
                changed = True
            if lua_path and lua_path != self.config.sandboxvars_path:
                self.config.sandboxvars_path = lua_path
                self.lua_path_label.setText(f"<tt>{lua_path}</tt>")
                changed = True
            if changed and self._save_config_callback:
                self._save_config_callback(self.config)
            if not ini_path and not lua_path:
                self.statusBar().showMessage("Couldn't find a .ini or _SandboxVars.lua under /.cache/Server", 8000)
            self._load_ini_settings()
            self._load_lua_settings()
        elif tag == "ini_load":
            path, text = value  # type: ignore[misc]
            self._ini_text = text
            self._ini_properties = parse_ini_properties(text)
            self._populate_ini_form()
            self._populate_mods_form()
        elif tag == "ini_save":
            self._ini_text = value  # type: ignore[assignment]
            self._ini_properties = parse_ini_properties(self._ini_text)
            self._set_ini_dirty(False)
            self._populate_ini_form()
            self._populate_mods_form()
            self.statusBar().showMessage("Server settings saved -- restart the server for changes to take effect", 8000)
        elif tag == "lua_load":
            path, text = value  # type: ignore[misc]
            self._lua_text = text
            self._lua_properties = parse_lua_properties(text)
            self._populate_lua_form()
        elif tag == "lua_save":
            self._lua_text = value  # type: ignore[assignment]
            self._lua_properties = parse_lua_properties(self._lua_text)
            self._set_lua_dirty(False)
            self.statusBar().showMessage("Sandbox settings saved -- restart the server for changes to take effect", 8000)
        elif tag in ("kick", "ban_add", "ban_remove", "console_command", "chat_message", "teleport", "godmode", "invisible", "give_item", "set_access_level", "save_world"):
            self.statusBar().showMessage(f"{tag} OK", 4000)
            if tag in ("ban_add", "ban_remove"):
                self.refresh_bans()
            if tag == "kick":
                self.refresh_players()
            if tag == "console_command" and value:
                self.console_view.appendPlainText(str(value))
        elif tag == "power_action":
            self.statusBar().showMessage(f"Power action '{value}' sent", 4000)
        elif tag == "console_line":
            self.console_view.appendPlainText(str(value))
        elif tag == "ptero_status":
            self._ptero_status = str(value)
            self._update_start_stop_button_text()
        elif tag == "sftp_connect":
            self._sftp = value  # type: ignore[assignment]
            self._set_sftp_status(True)
            self._start_sftp_health_check()
            self.statusBar().showMessage("SFTP connected", 5000)
            self._sftp_browse("/")
            self._discover_config_paths()
            self._backup_refresh_status()
        elif tag == "sftp_reconnect":
            self._sftp = value  # type: ignore[assignment]
            self._set_sftp_status(True)
            self._start_sftp_health_check()
            self.statusBar().showMessage("SFTP reconnected", 5000)
            self._sftp_browse(self._sftp_cwd)
            self._backup_refresh_status()
        elif tag == "sftp_list":
            path, entries = value  # type: ignore[misc]
            self._sftp_cwd = path
            self._sftp_entries = entries
            self._populate_sftp_list()
        elif tag == "sftp_read":
            path, content = value  # type: ignore[misc]
            self._sftp_open_path = path
            self._sftp_loading = True
            try:
                self.sftp_editor.setPlainText(content)
            finally:
                self._sftp_loading = False
            self._set_sftp_dirty(False)
        elif tag == "sftp_write":
            self._set_sftp_dirty(False)
            self.statusBar().showMessage(f"Saved '{value}'", 4000)
        elif tag in ("sftp_delete", "sftp_rename", "sftp_chmod"):
            self._sftp_browse(self._sftp_cwd)
        elif tag == "sftp_upload":
            refresh_dir, count = value  # type: ignore[misc]
            self.statusBar().showMessage(f"Uploaded {count} file(s)", 5000)
            self._sftp_browse(refresh_dir)
        elif tag == "backup_status":
            save_dir, backups_dir, live_exists, names = value  # type: ignore[misc]
            self._populate_backup_status(save_dir, backups_dir, live_exists, names)
        elif tag == "backup_progress":
            if self.backup_busy_label.isVisible():
                self.backup_busy_label.setText(f"{self._backup_busy_base_message} ({value} file(s) copied so far)")
        elif tag == "backup_create_check":
            self._continue_backup_create(value)  # type: ignore[arg-type]
        elif tag == "backup_create":
            self._set_backup_busy(False)
            self.statusBar().showMessage(str(value), 6000)
            self._resume_sftp_health_check()
            self._backup_refresh_status()
            self._maybe_offer_start_server()
        elif tag == "backup_restore_check":
            name, state = value  # type: ignore[misc]
            self._continue_backup_restore(name, state)
        elif tag == "backup_restore":
            self._set_backup_busy(False)
            self.statusBar().showMessage("Backup restored", 8000)
            self._resume_sftp_health_check()
            self._backup_refresh_status()
            self._maybe_offer_start_server()
        elif tag == "backup_delete":
            self._set_backup_busy(False)
            self.statusBar().showMessage("Backup deleted", 4000)
            self._backup_refresh_status()
        elif tag == "reset_map_check":
            self._continue_reset_map(value)  # type: ignore[arg-type]
        elif tag == "reset_map":
            self._set_backup_busy(False)
            self.statusBar().showMessage("Live save deleted -- a new map will be created next time the server starts", 8000)
            self._backup_refresh_status()
            self._maybe_offer_start_server()
        elif tag == "workshop_check":
            self._populate_workshop_table(value)  # type: ignore[arg-type]
        elif tag == "server_update_check":
            self._populate_server_build_status(value)  # type: ignore[arg-type]
        elif tag == "freeze_mod":
            workshop_id, mod_ids = value  # type: ignore[misc]
            self._apply_freeze(workshop_id, mod_ids)
        elif tag == "remove_mod":
            workshop_id, mod_ids = value  # type: ignore[misc]
            self._apply_removal(workshop_id, mod_ids)
        elif tag == "resolve_mod_ids":
            workshop_id, mod_ids = value  # type: ignore[misc]
            self._apply_resolved_mod_ids(workshop_id, mod_ids)
        elif tag == "add_workshop_mod_ids":
            workshop_id, mod_ids = value  # type: ignore[misc]
            self._apply_description_mod_ids(workshop_id, mod_ids)
        elif tag == "workshop_search":
            self._populate_workshop_search_table(value)  # type: ignore[arg-type]
        elif tag == "check_workshop_logs":
            workshop_id, mod_ids, matches = value  # type: ignore[misc]
            self._apply_workshop_log_check(workshop_id, mod_ids, matches)

    def _on_async_error(self, tag: str, message: str) -> None:
        if tag in ("rcon_connect", "rcon_reconnect"):
            self._set_rcon_status(False)
            self.statusBar().showMessage(f"RCON connection failed: {message}", 8000)
            self._schedule_rcon_reconnect()
        elif tag in ("sftp_connect", "sftp_reconnect"):
            self._set_sftp_status(False)
            self.statusBar().showMessage(f"SFTP connection failed: {message}", 8000)
            self._schedule_sftp_reconnect()
        elif tag == "ptero_connect":
            self.statusBar().showMessage(f"Pterodactyl connection failed: {message}", 8000)
        elif tag in ("ini_load", "lua_load"):
            # A cached path can go stale (e.g. wrong SFTP account was used the
            # first time it was discovered) -- drop it and re-discover instead
            # of failing the same way on every reconnect.
            if tag == "ini_load":
                self.config.ini_path = ""
            else:
                self.config.sandboxvars_path = ""
            if self._save_config_callback:
                self._save_config_callback(self.config)
            self.statusBar().showMessage(f"{message} -- re-discovering config file paths...", 8000)
            self._discover_config_paths()
        elif tag == "workshop_check":
            self.workshop_check_btn.setEnabled(True)
            self.statusBar().showMessage(f"Workshop check failed: {message}", 8000)
        elif tag == "server_update_check":
            self.server_build_label.setText(f"Server Build: check failed -- {message}")
            self.server_build_label.setStyleSheet("color: #e74c3c;")
        elif tag == "workshop_search":
            self.workshop_search_btn.setEnabled(True)
            self.statusBar().showMessage(f"Workshop search failed: {message}", 8000)
        elif tag == "backup_status":
            self.backup_live_status_label.setText("(check failed)")
            self.statusBar().showMessage(f"Backup status check failed: {message}", 8000)
        elif tag in ("backup_create_check", "backup_restore_check", "reset_map_check"):
            self._set_backup_busy(False)
            self.statusBar().showMessage(f"Couldn't check server status: {message}", 8000)
            QMessageBox.warning(self, "Couldn't check server status", f"Couldn't confirm whether the server is running, so this was cancelled:\n\n{message}")
        elif tag in ("backup_create", "backup_restore", "backup_delete", "reset_map"):
            # These are destructive/near-destructive actions -- a message that
            # can auto-hide in a few seconds isn't enough for "it silently
            # didn't do what you asked", so put it in front of the user too.
            self._set_backup_busy(False)
            if tag in ("backup_create", "backup_restore"):
                self._resume_sftp_health_check()
            self.statusBar().showMessage(f"{tag} failed: {message}", 8000)
            QMessageBox.warning(self, "Backup action failed", message)
            self._backup_refresh_status()
        else:
            self.statusBar().showMessage(f"{tag} failed: {message}", 8000)
