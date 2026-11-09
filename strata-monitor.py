"""Strata Monitor: a small, read-only Windows desktop telemetry widget."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PySide6.QtCore import QEvent, QRect, Qt, QTimer, QUrl, QSettings
    from PySide6.QtGui import QAction, QActionGroup
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout,
                                   QLabel, QMenu, QSizePolicy, QVBoxLayout, QWidget)
except ImportError as exc:  # PySide6 is an explicit desktop dependency, not a model/runtime dependency.
    message = "Strata Monitor needs PySide6. See README.md for installation in an isolated Python venv."
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{message}\n\nDetails: {exc}", "Strata Monitor", 0x10)
    else:
        print(f"{message}\nDetails: {exc}", file=sys.stderr)
    raise SystemExit(2)

from strata_monitor.discovery import discover_engine_servers, discover_host_url
from strata_monitor.metrics import (activity_label, adaptive_metrics, model_name,
                                    responsive_scale, tokens_per_second)


APP_STYLE = """
QWidget#root {{
  background: {surface}; color: {text}; border: 1px solid {border};
  border-radius: 12px; font-family: 'Segoe UI';
}}
QLabel {{ background: transparent; }}
QFrame#metricCard {{ background: {card}; border: 1px solid {border}; border-radius: {card_radius}; }}
QLabel#title {{ color: {text}; font-size: 13px; font-weight: 650; }}
QLabel#activity {{ border-radius: 8px; padding: 3px 7px; font-size: 9px; font-weight: 650; }}
QLabel#model {{ color: {muted}; font-size: 10px; }}
QLabel#metricTitle {{ color: {muted}; font-size: 9px; font-weight: 600; }}
QLabel#metricValue {{ color: {text}; font-size: 20px; font-weight: 650; }}
QLabel#metricNote {{ color: {subtle}; font-size: 8px; }}
"""

THEMES = {
    "White": {"surface": "#ffffff", "card": "#f4f6f8", "border": "#d9e0e8",
              "text": "#172033", "muted": "#667085", "subtle": "#7b8493", "icon": "#3977c3"},
    "Midnight": {"surface": "#151923", "card": "#202633", "border": "#343d4d",
                 "text": "#f1f5f9", "muted": "#b2bdcc", "subtle": "#8995a8", "icon": "#8bbcff"},
    "Ocean": {"surface": "#eaf4fb", "card": "#f5faff", "border": "#c7e0ee",
              "text": "#12344d", "muted": "#537184", "subtle": "#668699", "icon": "#087ea4"},
    "Forest": {"surface": "#edf5ef", "card": "#f7fbf7", "border": "#cfe0d1",
               "text": "#1c3522", "muted": "#5b7460", "subtle": "#708574", "icon": "#388451"},
    "Lavender": {"surface": "#f5f1fb", "card": "#fbf9ff", "border": "#e5dbf2",
                 "text": "#342747", "muted": "#756487", "subtle": "#8b7c9d", "icon": "#8157b2"},
}

ACTIVITY_COLORS = {
    "Generating": ("#dcfce7", "#166534"),
    "Idle": ("#e5e7eb", "#374151"),
    "Reading": ("#ede9fe", "#6d28d9"),
    "Processing": ("#fef3c7", "#92400e"),
    "Active": ("#dbeafe", "#1d4ed8"),
    "Unknown": ("#f3f4f6", "#4b5563"),
    "Disconnected": ("#fee2e2", "#b91c1c"),
}


def _api_request(url, api_key=""):
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", b"application/json")
    parsed = QUrl(url)
    origin = f"{parsed.scheme()}://{parsed.authority()}"
    request.setRawHeader(b"Origin", origin.encode("ascii", errors="ignore"))
    request.setRawHeader(b"X-Strata-Client", b"ui")
    if api_key:
        request.setRawHeader(b"Authorization", f"Bearer {api_key}".encode("utf-8"))
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout(1800)
    return request


class MetricCard(QFrame):
    def __init__(self, title, value="N/A", note="", circle=False):
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        self.title = QLabel(title)
        self.title.setObjectName("metricTitle")
        self.value = QLabel(value)
        self.value.setObjectName("metricValue")
        self.value.setMinimumWidth(0)
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.note = QLabel(note)
        self.note.setObjectName("metricNote")
        self.note.setWordWrap(False)
        layout.addWidget(self.title)
        layout.addWidget(self.value, 1)
        layout.addWidget(self.note)
        self.set_shape(circle)

    def set_shape(self, circle):
        self.circle = bool(circle)
        if self.circle:
            self.setFixedSize(72, 72)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            self.layout().setContentsMargins(3, 2, 3, 2)
            self.layout().setSpacing(0)
            alignment = Qt.AlignmentFlag.AlignCenter
        else:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.layout().setContentsMargins(8, 6, 8, 6)
            self.layout().setSpacing(1)
            alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for label in (self.title, self.value, self.note):
            label.setAlignment(alignment)

    def set_data(self, title, value, note, tooltip=""):
        self.title.setText(title)
        self.value.setText(value)
        self.note.setText(note)
        self.setToolTip(tooltip)
        self.value.setToolTip(tooltip)


class StrataMonitor(QWidget):
    # Local prompt-reading windows can be brief; poll often enough to catch the
    # engine's reported prefill rate without reusing a previous request's rate.
    POLL_MS = 250

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        # This build defaults to circles regardless of its filename, so renaming the EXE is safe.
        circle_default = True
        self.setWindowTitle("Strata Monitor")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(190, 175)
        self._settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                   "StrataMonitor", "widget")
        self._settings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                               str(Path.home() / "AppData" / "Roaming" / "StrataMonitor"))
        default_shape = "circle" if circle_default else "square"
        self._circle_style = self._settings.value("card_shape", default_shape, type=str) == "circle"
        self._always_on_top = self._settings.value("always_on_top", False, type=bool)
        self._theme_name = self._settings.value("theme", "White", type=str)
        if self._theme_name not in THEMES:
            self._theme_name = "White"
        self._transparency = self._settings.value("transparency", 0, type=int)
        self._drag = None
        self._manager = QNetworkAccessManager(self)
        self._inflight = set()
        self._host_url = discover_host_url()
        self._servers = []
        self._server_index = 0
        self._next_discovery = 0.0
        self._last_good = 0.0
        self._metrics = None
        self._agent_status = None
        self._host_monitor = None
        self._last_error = ""
        self._build_ui()
        self._apply_theme(self._theme_name, persist=False)
        self._apply_transparency(self._transparency, persist=False)
        self._restore_window()
        self._set_topmost(self._always_on_top, persist=False)

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._root_layout = root
        root.setContentsMargins(8, 2, 8, 2) if self._circle_style else root.setContentsMargins(12, 10, 12, 11)
        root.setSpacing(3 if self._circle_style else 5)

        header = QHBoxLayout()
        self._header_layout = header
        header.setSpacing(4 if self._circle_style else 6)
        self.icon = QLabel("◈")
        self.icon.setStyleSheet("color:#3977c3;font-size:15px;font-weight:700")
        self.title = QLabel("Strata Monitor")
        self.title.setObjectName("title")
        self.activity = QLabel("Unknown")
        self.activity.setObjectName("activity")
        self.activity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.icon)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.activity)
        root.addLayout(header)

        self.model = QLabel("N/A")
        self.model.setObjectName("model")
        self.model.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.model.setToolTip("Active model reported by Strata")
        root.addWidget(self.model)

        self.grid = QGridLayout()
        self._grid_layout = self.grid
        self.grid.setContentsMargins(0, 0 if self._circle_style else 2, 0, 0)
        self.grid.setSpacing(3 if self._circle_style else 7)
        self.tts = MetricCard("TTS · —", "N/A", "No completed tasks", self._circle_style)
        self.tsr = MetricCard("TSR · —", "N/A", "0 evaluated", self._circle_style)
        self.speed = MetricCard("Tokens/s", "N/A", "Live", self._circle_style)
        self.ttft = MetricCard("TTFT · —", "N/A", "No reported samples", self._circle_style)
        card_alignment = Qt.AlignmentFlag.AlignCenter if self._circle_style else Qt.AlignmentFlag(0)
        self.grid.addWidget(self.tts, 0, 0, card_alignment)
        self.grid.addWidget(self.tsr, 0, 1, card_alignment)
        self.grid.addWidget(self.speed, 1, 0, card_alignment)
        self.grid.addWidget(self.ttft, 1, 1, card_alignment)
        self.grid.setRowStretch(0, 1)
        self.grid.setRowStretch(1, 1)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        root.addLayout(self.grid, 1)

        self.installEventFilter(self)
        self._install_child_filters(self)

    def _install_child_filters(self, widget):
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)
            child.setMouseTracking(True)

    def _restore_window(self):
        geometry = self._settings.value("geometry")
        compact_applied = self._settings.value("compact_layout_v3", False, type=bool)
        if geometry:
            self.restoreGeometry(geometry)
            if not compact_applied:
                center = self.frameGeometry().center()
                self.resize(200, 190)
                self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
            screens = QApplication.screens()
            if screens and not any(screen.availableGeometry().intersects(self.frameGeometry()) for screen in screens):
                available = QApplication.primaryScreen().availableGeometry()
                self.move(available.right() - self.width() - 24, available.top() + 48)
        else:
            screen = QApplication.primaryScreen()
            available = screen.availableGeometry() if screen else QRect(0, 0, 1200, 800)
            self.resize(200, 190)
            self.move(available.right() - self.width() - 24, available.top() + 48)

    def _set_topmost(self, enabled, persist=True):
        self._always_on_top = bool(enabled)
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()
        if persist:
            self._settings.setValue("always_on_top", self._always_on_top)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        shape_menu = menu.addMenu("Card shape")
        shape_group = QActionGroup(shape_menu)
        shape_group.setExclusive(True)
        for label, circle in (("Circles", True), ("Squares (classic)", False)):
            shape_action = QAction(label, shape_menu)
            shape_action.setCheckable(True)
            shape_action.setChecked(circle == self._circle_style)
            shape_group.addAction(shape_action)
            shape_menu.addAction(shape_action)
            shape_action.triggered.connect(lambda checked, value=circle: self._set_circle_style(value))

        theme_menu = menu.addMenu("Style")
        theme_group = QActionGroup(theme_menu)
        theme_group.setExclusive(True)
        for name in THEMES:
            action = QAction(name, theme_menu)
            action.setCheckable(True)
            action.setChecked(name == self._theme_name)
            theme_group.addAction(action)
            theme_menu.addAction(action)
            action.triggered.connect(lambda checked, value=name: self._apply_theme(value))

        transparency_menu = menu.addMenu("Transparency")
        opacity_group = QActionGroup(transparency_menu)
        opacity_group.setExclusive(True)
        for value in (0, *range(10, 100, 10)):
            label = "0% (opaque)" if value == 0 else f"{value}% transparent"
            action = QAction(label, transparency_menu)
            action.setCheckable(True)
            action.setChecked(value == self._transparency)
            opacity_group.addAction(action)
            transparency_menu.addAction(action)
            action.triggered.connect(lambda checked, amount=value: self._apply_transparency(amount))

        menu.addSeparator()
        action = QAction("Always on top", menu)
        action.setCheckable(True)
        action.setChecked(self._always_on_top)
        action.triggered.connect(lambda checked: self._set_topmost(checked))
        menu.addAction(action)
        menu.addSeparator()
        close_action = QAction("Close", menu)
        close_action.triggered.connect(self.close)
        menu.addAction(close_action)
        menu.exec(event.globalPos())

    def _set_circle_style(self, enabled, persist=True):
        self._circle_style = bool(enabled)
        alignment = Qt.AlignmentFlag.AlignCenter if self._circle_style else Qt.AlignmentFlag(0)
        for card in (self.tts, self.tsr, self.speed, self.ttft):
            card.set_shape(self._circle_style)
            self._grid_layout.setAlignment(card, alignment)
        self._apply_layout_spacing(responsive_scale(self.width(), self.height()))
        self._apply_theme(self._theme_name, persist=False)
        self._root_layout.activate()
        self._grid_layout.activate()
        if persist:
            self._settings.setValue("card_shape", "circle" if self._circle_style else "square")

    def _apply_layout_spacing(self, scale):
        if self._circle_style:
            self._root_layout.setContentsMargins(round(8 * scale), round(2 * scale),
                                                 round(8 * scale), round(2 * scale))
            self._root_layout.setSpacing(round(3 * scale))
            self._header_layout.setSpacing(round(4 * scale))
            self._grid_layout.setContentsMargins(0, 0, 0, 0)
            self._grid_layout.setSpacing(round(3 * scale))
            for card in (self.tts, self.tsr, self.speed, self.ttft):
                card.layout().setContentsMargins(round(3 * scale), round(2 * scale),
                                                 round(3 * scale), round(2 * scale))
                card.layout().setSpacing(0)
        else:
            self._root_layout.setContentsMargins(round(9 * scale), round(7 * scale),
                                                 round(9 * scale), round(8 * scale))
            self._root_layout.setSpacing(round(4 * scale))
            self._header_layout.setSpacing(round(5 * scale))
            self._grid_layout.setContentsMargins(0, round(2 * scale), 0, 0)
            self._grid_layout.setSpacing(round(5 * scale))
            for card in (self.tts, self.tsr, self.speed, self.ttft):
                card.layout().setContentsMargins(round(8 * scale), round(6 * scale),
                                                 round(8 * scale), round(6 * scale))
                card.layout().setSpacing(max(1, round(scale)))
        self._style_text_for_theme()

    def _apply_theme(self, name, persist=True):
        if name not in THEMES:
            return
        self._theme_name = name
        self.setStyleSheet(APP_STYLE.format(**THEMES[name], card_radius="35px" if self._circle_style else "8px"))
        colors = THEMES[name]
        self.icon.setStyleSheet(f"color:{colors['icon']};font-size:15px;font-weight:700")
        if persist:
            self._settings.setValue("theme", name)
        if hasattr(self, "activity"):
            self._set_activity(self.activity.text())
        if hasattr(self, "title"):
            self._style_text_for_theme()

    def _style_text_for_theme(self):
        colors = THEMES[self._theme_name]
        scale = responsive_scale(self.width(), self.height())
        self.title.setStyleSheet(
            f"font-size:{max(10, round(13 * scale))}px;font-weight:650;color:{colors['text']}"
        )
        self.model.setStyleSheet(
            f"font-size:{max(8, round(10 * scale))}px;color:{colors['muted']}"
        )
        for card in (self.tts, self.tsr, self.speed, self.ttft):
            title_size = max(7, round(8 * scale)) if card.circle else max(8, round(9 * scale))
            note_size = max(6, round(7 * scale)) if card.circle else max(7, round(8 * scale))
            value_size = max(12, round(15 * scale)) if card.circle else max(13, round(20 * scale))
            card.title.setStyleSheet(
                f"font-size:{title_size}px;font-weight:600;color:{colors['muted']}"
            )
            card.note.setStyleSheet(
                f"font-size:{note_size}px;color:{colors['subtle']}"
            )
            card.value.setStyleSheet(
                f"font-size:{value_size}px;font-weight:650;color:{colors['text']}"
            )

    def _apply_transparency(self, percentage, persist=True):
        self._transparency = max(0, min(90, int(percentage)))
        self.setWindowOpacity(1 - self._transparency / 100)
        if persist:
            self._settings.setValue("transparency", self._transparency)

    def refresh(self):
        import time
        now = time.monotonic()
        if now >= self._next_discovery:
            self._servers = discover_engine_servers()
            self._host_url = discover_host_url()
            self._next_discovery = now + 15
            if self._server_index >= len(self._servers):
                self._server_index = 0
        if self._inflight:
            self._render()
            return

        if self._servers:
            base, key = self._servers[self._server_index]
            self._get("metrics", f"{base}/metrics?requests=all", key)
        else:
            self._get("metrics", f"{self._host_url}/api/monitor")
        self._get("agent", f"{self._host_url}/api/agent/status")
        self._get("host_monitor", f"{self._host_url}/api/monitor")
        self._render()

    def _get(self, kind, url, key=""):
        reply = self._manager.get(_api_request(url, key))
        reply.setProperty("kind", kind)
        reply.setProperty("url", url)
        self._inflight.add(reply)
        reply.finished.connect(lambda r=reply: self._reply_finished(r))

    def _reply_finished(self, reply):
        import json
        import time
        kind = reply.property("kind")
        error = reply.error() != QNetworkReply.NetworkError.NoError
        body = bytes(reply.readAll())
        reply.deleteLater()
        self._inflight.discard(reply)
        if not error:
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    if kind == "metrics":
                        self._metrics = data
                        self._last_good = time.monotonic()
                        self._last_error = ""
                    elif kind == "agent":
                        self._agent_status = data
                    elif kind == "host_monitor":
                        self._host_monitor = data
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._last_error = "Invalid Strata telemetry response"
        else:
            self._last_error = reply.errorString()
            if kind == "metrics" and self._servers:
                self._server_index = (self._server_index + 1) % len(self._servers)
                self._get("metrics", f"{self._host_url}/api/monitor")
        self._render()

    def _render(self):
        import time
        online = self._metrics is not None and time.monotonic() - self._last_good <= 5
        if not online:
            self._set_activity("Disconnected" if self._last_error else "Unknown")
            self.model.setText("N/A")
            self.tts.set_data("TTS · —", "N/A", "Offline")
            self.tsr.set_data("TSR · —", "N/A", "Offline")
            self.speed.set_data("Tokens/s", "N/A", "Offline")
            self.ttft.set_data("TTFT · —", "N/A", "No reported samples")
            return

        self._set_activity(activity_label(self._metrics, self._agent_status))
        name = model_name(self._metrics)
        self.model.setText(name)
        self.model.setToolTip(name)
        requests = self._metrics.get("recent_requests", self._metrics.get("requests", []))
        host_requests = (self._host_monitor or {}).get("requests", [])
        result = adaptive_metrics(requests, self._metrics.get("time"), ttft_requests=host_requests)
        interval = result["window_minutes"]
        window = f"{interval}m"
        tts_value = "N/A" if result["tts_seconds"] is None else f"{result['tts_seconds']:.1f}s"
        tts_note = f"n={result['tts_samples']} success"
        if result["tts_samples"] == 0:
            tts_note = "No successes"
        tsr_value = "N/A" if result["tsr_percent"] is None else f"{result['tsr_percent']:.1f}%"
        tsr_note = f"n={result['tsr_samples']} evaluated"
        if result["insufficient_data"]:
            tsr_note = f"n={result['tsr_samples']} insufficient"
        self.tts.set_data(f"TTS · {window}", tts_value, tts_note,
                          "Mean duration of successfully completed tasks in the selected adaptive window.")
        self.tsr.set_data(f"TSR · {window}", tsr_value, tsr_note,
                          "Successes divided by tasks with a verified success/failure outcome.")
        live = self._metrics.get("live") if isinstance(self._metrics.get("live"), dict) else {}
        live_state = str(live.get("state", self._metrics.get("phase", ""))).lower()
        rate = tokens_per_second(self._metrics)
        speed_value = "N/A" if rate is None else f"{rate:.1f}"
        if live_state == "reading":
            speed_title = "Read tok/s"
            speed_note = "Reading prompt" if rate is not None else "Read rate unavailable"
        else:
            speed_title = "Tokens/s"
            speed_note = "Live" if live_state == "generating" and rate is not None else "Idle" if live_state in ("idle", "unloaded") else "Rate unavailable"
        self.speed.set_data(speed_title, speed_value, speed_note)
        ttft_value = "N/A" if result["ttft_ms"] is None else f"{result['ttft_ms'] / 1000:.2f}s"
        ttft_note = f"n={result['ttft_samples']} reported"
        if result["ttft_samples"] == 0:
            ttft_note = "No reported samples"
        elif result["ttft_samples"] < 10:
            ttft_note = f"n={result['ttft_samples']} insufficient"
        self.ttft.set_data(f"TTFT · {window}", ttft_value, ttft_note,
                           "Mean time to first token for requests with a reported TTFT in the selected adaptive window.")

    def _set_activity(self, label):
        self.activity.setText(label)
        background, foreground = ACTIVITY_COLORS.get(label, ACTIVITY_COLORS["Unknown"])
        scale = responsive_scale(self.width(), self.height())
        self.activity.setStyleSheet(
            f"background:{background};color:{foreground};border-radius:8px;"
            f"padding:2px 6px;font-size:{max(8, round(9 * scale))}px;font-weight:650"
        )

    def _edge_at(self, global_pos):
        p = self.mapFromGlobal(global_pos)
        margin = 7
        edges = Qt.Edge(0)
        if p.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        elif p.x() >= self.width() - margin:
            edges |= Qt.Edge.RightEdge
        if p.y() <= margin:
            edges |= Qt.Edge.TopEdge
        elif p.y() >= self.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    def eventFilter(self, watched, event):
        typ = event.type()
        if typ == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            edges = self._edge_at(point)
            self._drag = {"point": point, "geometry": self.geometry(), "edges": edges}
            event.accept()
            return True
        if typ == QEvent.Type.MouseMove:
            point = event.globalPosition().toPoint()
            if self._drag:
                state = self._drag
                delta = point - state["point"]
                if state["edges"]:
                    rect = QRect(state["geometry"])
                    edges = state["edges"]
                    if edges & Qt.Edge.LeftEdge:
                        rect.setLeft(min(rect.right() - self.minimumWidth() + 1, rect.left() + delta.x()))
                    if edges & Qt.Edge.RightEdge:
                        rect.setRight(max(rect.left() + self.minimumWidth() - 1, rect.right() + delta.x()))
                    if edges & Qt.Edge.TopEdge:
                        rect.setTop(min(rect.bottom() - self.minimumHeight() + 1, rect.top() + delta.y()))
                    if edges & Qt.Edge.BottomEdge:
                        rect.setBottom(max(rect.top() + self.minimumHeight() - 1, rect.bottom() + delta.y()))
                    self.setGeometry(rect)
                else:
                    self.move(state["geometry"].topLeft() + delta)
                event.accept()
                return True
        if typ == QEvent.Type.MouseButtonRelease and self._drag:
            self._drag = None
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        scale = responsive_scale(self.width(), self.height())
        self._apply_layout_spacing(scale)
        self._set_activity(self.activity.text())
        super().resizeEvent(event)

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("always_on_top", self._always_on_top)
        self._settings.setValue("theme", self._theme_name)
        self._settings.setValue("transparency", self._transparency)
        self._settings.setValue("card_shape", "circle" if self._circle_style else "square")
        self._settings.setValue("card_shape", "circle" if self._circle_style else "square")
        self._settings.setValue("compact_layout_v3", True)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Strata Monitor")
    app.setOrganizationName("StrataMonitor")
    widget = StrataMonitor()
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
