#!/usr/bin/env python3
"""nanotorrent - a small, fast BitTorrent client by Batarong Studios.

Engine: libtorrent (the same engine qBittorrent uses).
UI:     PySide6 (Qt).

Install:  pip install libtorrent PySide6
Run:      python nanotorrent.py [file.torrent | magnet:?xt=...]
"""
import json
import sys
import time
from pathlib import Path

import libtorrent as lt
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QStyle,
    QStyledItemDelegate, QStyleOptionProgressBar, QTableWidget,
    QTableWidgetItem, QToolBar, QWidget,
)

APP_NAME = "nanotorrent"
APP_VERSION = "0.1"
APP_DIR = Path.home() / ".nanotorrent"
RESUME_DIR = APP_DIR / "resume"
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULTS = {
    "save_path": str(Path.home() / "Downloads"),
    "auto_stop_seeding": True,   # pause a torrent as soon as it finishes
    "max_down_kbps": 0,          # 0 = unlimited
    "max_up_kbps": 0,
    "connections": 500,
}

DHT_NODES = ("router.bittorrent.com:6881,router.utorrent.com:6881,"
             "dht.transmissionbt.com:6881,dht.libtorrent.org:25401")


# ----------------------------------------------------------------- helpers
def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except Exception:
        pass
    return s


def save_settings(s):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_speed(n):
    return fmt_size(n) + "/s" if n >= 1 else ""


def fmt_eta(seconds):
    if seconds < 0 or seconds > 8640000:
        return "∞"
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def info_hash(handle):
    try:
        return str(handle.info_hashes().get_best())
    except AttributeError:
        return str(handle.info_hash())


# ------------------------------------------------------------------ engine
class Engine:
    """Thin wrapper around a libtorrent session."""

    def __init__(self, settings):
        self.settings = settings
        self.manual_seed = set()   # torrents the user resumed on purpose
        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        self.ses = lt.session(self._pack())
        self._load_resume()

    def _pack(self):
        s = self.settings
        return {
            "user_agent": f"{APP_NAME}/{APP_VERSION} libtorrent/{lt.__version__}",
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "dht_bootstrap_nodes": DHT_NODES,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connections_limit": int(s["connections"]),
            "active_downloads": 8,
            "active_limit": 200,
            "download_rate_limit": int(s["max_down_kbps"]) * 1024,
            "upload_rate_limit": int(s["max_up_kbps"]) * 1024,
            "alert_mask": (lt.alert.category_t.status_notification
                           | lt.alert.category_t.error_notification),
        }

    def apply_settings(self):
        self.ses.apply_settings(self._pack())

    # -- adding / removing
    def _save_path(self):
        p = Path(self.settings["save_path"])
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def add_file(self, path):
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(str(path))
        atp.save_path = self._save_path()
        return self.ses.add_torrent(atp)

    def add_magnet(self, uri):
        atp = lt.parse_magnet_uri(uri)
        atp.save_path = self._save_path()
        return self.ses.add_torrent(atp)

    def handles(self):
        return self.ses.get_torrents()

    def by_hash(self, key):
        for h in self.handles():
            if h.is_valid() and info_hash(h) == key:
                return h
        return None

    def remove(self, handle, delete_files=False):
        key = info_hash(handle)
        flags = lt.session.delete_files if delete_files else 0
        self.ses.remove_torrent(handle, flags)
        self.manual_seed.discard(key)
        (RESUME_DIR / f"{key}.resume").unlink(missing_ok=True)

    # -- pause / resume
    def pause(self, handle):
        handle.unset_flags(lt.torrent_flags.auto_managed)
        handle.pause()

    def resume(self, handle):
        # resuming a finished torrent is an explicit "please seed this"
        self.manual_seed.add(info_hash(handle))
        handle.unset_flags(lt.torrent_flags.auto_managed)
        handle.resume()

    # -- persistence
    @staticmethod
    def _flag():
        return getattr(lt.torrent_handle, "save_info_dict", 0)

    def _write_resume(self, alert):
        key = info_hash(alert.handle)
        data = lt.write_resume_data_buf(alert.params)
        (RESUME_DIR / f"{key}.resume").write_bytes(data)

    def _load_resume(self):
        for f in RESUME_DIR.glob("*.resume"):
            try:
                atp = lt.read_resume_data(f.read_bytes())
                self.ses.add_torrent(atp)
            except Exception:
                pass

    def process_alerts(self):
        for a in self.ses.pop_alerts():
            if isinstance(a, lt.torrent_finished_alert):
                a.handle.save_resume_data(self._flag())
            elif isinstance(a, lt.save_resume_data_alert):
                self._write_resume(a)

    def shutdown(self):
        self.ses.pause()
        pending = 0
        for h in self.handles():
            if h.is_valid():
                h.save_resume_data(self._flag())
                pending += 1
        deadline = time.time() + 5
        while pending > 0 and time.time() < deadline:
            if self.ses.wait_for_alert(500) is None:
                continue
            for a in self.ses.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    self._write_resume(a)
                    pending -= 1
                elif isinstance(a, lt.save_resume_data_failed_alert):
                    pending -= 1


# ---------------------------------------------------------------------- UI
class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        value = index.data(Qt.ItemDataRole.UserRole) or 0
        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(3, 3, -3, -3)
        opt.minimum, opt.maximum, opt.progress = 0, 1000, int(value)
        opt.text = f"{value / 10:.1f}%"
        opt.textVisible = True
        opt.state = QStyle.StateFlag.State_Enabled
        QApplication.style().drawControl(
            QStyle.ControlElement.CE_ProgressBar, opt, painter)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        form = QFormLayout(self)

        self.path = QLineEdit(settings["save_path"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Save to:", holder)

        self.auto_stop = QCheckBox("Stop seeding as soon as a download finishes")
        self.auto_stop.setChecked(settings["auto_stop_seeding"])
        form.addRow(self.auto_stop)

        self.down = self._spin(settings["max_down_kbps"], " KB/s", "unlimited")
        self.up = self._spin(settings["max_up_kbps"], " KB/s", "unlimited")
        self.conns = self._spin(settings["connections"], "", None, 20, 5000)
        form.addRow("Max download speed:", self.down)
        form.addRow("Max upload speed:", self.up)
        form.addRow("Max connections:", self.conns)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _spin(value, suffix, zero_text, lo=0, hi=10_000_000):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(int(value))
        sp.setSuffix(suffix)
        if zero_text:
            sp.setSpecialValueText(zero_text)
        return sp

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save downloads to",
                                             self.path.text())
        if d:
            self.path.setText(d)

    def values(self):
        return {
            "save_path": self.path.text().strip() or DEFAULTS["save_path"],
            "auto_stop_seeding": self.auto_stop.isChecked(),
            "max_down_kbps": self.down.value(),
            "max_up_kbps": self.up.value(),
            "connections": self.conns.value(),
        }


COLS = ["Name", "Size", "Progress", "Status", "↓ Speed", "↑ Speed",
        "Peers", "ETA"]


class MainWindow(QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 520)
        self.setAcceptDrops(True)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegateForColumn(2, ProgressDelegate(self.table))
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 3, 4, 5, 6, 7):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 170)
        self.setCentralWidget(self.table)

        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        self._action(bar, "Add Torrent", self.add_torrent_dialog, "Ctrl+O")
        self._action(bar, "Add Magnet", self.add_magnet_dialog, "Ctrl+M")
        bar.addSeparator()
        self._action(bar, "Pause", self.pause_selected)
        self._action(bar, "Resume", self.resume_selected)
        self._action(bar, "Remove", self.remove_selected, "Delete")
        bar.addSeparator()
        self._action(bar, "Settings", self.open_settings, "Ctrl+,")

        self.auto_stop_box = QCheckBox("Stop seeding when finished")
        self.auto_stop_box.setChecked(engine.settings["auto_stop_seeding"])
        self.auto_stop_box.toggled.connect(self._toggle_auto_stop)
        self.statusBar().addWidget(self.auto_stop_box)
        self.speed_label = QLabel()
        self.statusBar().addPermanentWidget(self.speed_label)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def _action(self, bar, text, slot, shortcut=None):
        act = QAction(text, self)
        act.triggered.connect(slot)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        bar.addAction(act)
        return act

    # -- adding
    def add_any(self, text):
        try:
            if text.lower().startswith("magnet:"):
                self.engine.add_magnet(text)
            else:
                self.engine.add_file(text)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Could not add torrent:\n{e}")

    def add_torrent_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add torrent files", "", "Torrent files (*.torrent)")
        for f in files:
            self.add_any(f)
        self.refresh()

    def add_magnet_dialog(self):
        clip = QGuiApplication.clipboard().text().strip()
        default = clip if clip.lower().startswith("magnet:") else ""
        text, ok = QInputDialog.getText(self, "Add magnet link",
                                        "Magnet link:", text=default)
        if ok and text.strip():
            self.add_any(text.strip())
            self.refresh()

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                if url.isLocalFile() and url.toLocalFile().endswith(".torrent"):
                    self.add_any(url.toLocalFile())
        elif md.text().lower().startswith("magnet:"):
            self.add_any(md.text().strip())
        self.refresh()

    # -- selection actions
    def selected_handles(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        out = []
        for r in rows:
            h = self.engine.by_hash(
                self.table.item(r, 0).data(Qt.ItemDataRole.UserRole))
            if h:
                out.append(h)
        return out

    def pause_selected(self):
        for h in self.selected_handles():
            self.engine.pause(h)
        self.refresh()

    def resume_selected(self):
        for h in self.selected_handles():
            self.engine.resume(h)
        self.refresh()

    def remove_selected(self):
        handles = self.selected_handles()
        if not handles:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Remove torrents")
        box.setText(f"Remove {len(handles)} torrent(s)?")
        keep = box.addButton("Remove", QMessageBox.ButtonRole.AcceptRole)
        wipe = box.addButton("Remove and delete files",
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked in (keep, wipe):
            for h in handles:
                self.engine.remove(h, delete_files=(clicked is wipe))
        self.refresh()

    # -- settings
    def open_settings(self):
        dlg = SettingsDialog(self.engine.settings, self)
        if dlg.exec():
            self.engine.settings.update(dlg.values())
            save_settings(self.engine.settings)
            self.engine.apply_settings()
            self.auto_stop_box.blockSignals(True)
            self.auto_stop_box.setChecked(
                self.engine.settings["auto_stop_seeding"])
            self.auto_stop_box.blockSignals(False)

    def _toggle_auto_stop(self, on):
        self.engine.settings["auto_stop_seeding"] = on
        save_settings(self.engine.settings)

    # -- refresh loop
    @staticmethod
    def _status_text(st, paused):
        if st.errc.value():
            return "Error"
        if paused:
            return "Completed" if st.is_finished else "Paused"
        names = {
            "checking_files": "Checking",
            "checking_resume_data": "Checking",
            "downloading_metadata": "Fetching metadata",
            "downloading": "Downloading",
            "finished": "Finished",
            "seeding": "Seeding",
            "allocating": "Allocating",
        }
        raw = getattr(st.state, "name", str(st.state))
        return names.get(raw, raw)

    def refresh(self):
        self.engine.process_alerts()
        auto_stop = self.engine.settings["auto_stop_seeding"]

        items = []
        for h in self.engine.handles():
            if h.is_valid():
                items.append((info_hash(h), h, h.status()))

        alive = {k for k, _, _ in items}
        for r in range(self.table.rowCount() - 1, -1, -1):
            key = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            if key not in alive:
                self.table.removeRow(r)
        rows = {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole): r
                for r in range(self.table.rowCount())}
        for key, _, _ in items:
            if key not in rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                for c in range(len(COLS)):
                    self.table.setItem(r, c, QTableWidgetItem())
                self.table.item(r, 0).setData(Qt.ItemDataRole.UserRole, key)
                rows[key] = r

        total_down = total_up = 0
        for key, h, st in items:
            paused = bool(st.flags & lt.torrent_flags.paused)
            if (auto_stop and st.is_finished and not paused
                    and key not in self.engine.manual_seed):
                self.engine.pause(h)
                paused = True

            r = rows[key]
            down, up = st.download_payload_rate, st.upload_payload_rate
            total_down += down
            total_up += up
            remaining = st.total_wanted - st.total_wanted_done
            eta = fmt_eta(remaining / down) if (down > 0 and remaining > 0) else ""
            texts = [
                st.name or "Fetching metadata…",
                fmt_size(st.total_wanted) if st.has_metadata else "?",
                None,
                self._status_text(st, paused),
                fmt_speed(down),
                fmt_speed(up),
                f"{st.num_seeds} seeds, {max(st.num_peers - st.num_seeds, 0)} peers",
                eta,
            ]
            for c, text in enumerate(texts):
                if text is not None:
                    self.table.item(r, c).setText(text)
            self.table.item(r, 2).setData(Qt.ItemDataRole.UserRole,
                                          int(st.progress * 1000))

        self.table.viewport().update()
        self.speed_label.setText(
            f"↓ {fmt_size(total_down)}/s    ↑ {fmt_size(total_up)}/s")

    def closeEvent(self, event):
        self.timer.stop()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.engine.shutdown()
        QApplication.restoreOverrideCursor()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    engine = Engine(load_settings())
    win = MainWindow(engine)
    win.show()
    for arg in sys.argv[1:]:
        win.add_any(arg)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
