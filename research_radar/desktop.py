"""PySide6 Windows-friendly desktop companion with true RGBA transparency, tray icon, and toast bubble."""

import argparse
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
import webbrowser

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .events import list_events
from .models import Settings
from .service import RUN_LABELS, daily_overview, enqueue_daily, open_store

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PET_IMAGE = Path(__file__).with_name("pet-mascot.png")

STATE_HEX = {
    "queued": "#2563EB",
    "running": "#1E3A5F",
    "succeeded": "#15803D",
    "failed": "#DC2626",
    "partial": "#B45309",
    None: "#64748B",
}

DRAWER_QSS = """
QFrame#DrawerCard {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
}
QLabel#StatusPill {
    background-color: #F1F5F9;
    color: #0F172A;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
}
QLabel#MetaText {
    color: #475569;
    font-size: 12px;
}
QLabel#BadgeChip {
    background-color: #FAFAFA;
    color: #0F172A;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
    font-weight: 600;
}
QComboBox {
    background-color: #FAFAFA;
    color: #0F172A;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 28px;
    font-size: 13px;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QPushButton {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #F1F5F9;
    border-color: #CBD5E1;
}
QPushButton:disabled {
    color: #94A3B8;
    background-color: #F8FAFC;
}
QPushButton#PrimaryBtn {
    background-color: #1E3A5F;
    color: #FFFFFF;
    border: 1px solid #1E3A5F;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#PrimaryBtn:hover {
    background-color: #172E4B;
}
QPushButton#PrimaryBtn:disabled {
    background-color: #94A3B8;
    border-color: #94A3B8;
    color: #FFFFFF;
}
QPushButton#SubtleBtn {
    background-color: transparent;
    color: #475569;
    border: 1px solid #E2E8F0;
    font-size: 12px;
    padding: 5px 8px;
}
QPushButton#SubtleBtn:hover {
    background-color: #F1F5F9;
    color: #0F172A;
}
"""

BUBBLE_QSS = """
QFrame#BubbleCard {
    background-color: #09090B;
    color: #F4F4F5;
    border: 1px solid #27272A;
    border-radius: 10px;
}
QLabel#BubbleTitle {
    color: #60A5FA;
    font-size: 12px;
    font-weight: 700;
}
QLabel#BubbleBody {
    color: #F4F4F5;
    font-size: 12px;
}
QPushButton#BubbleClose {
    background: transparent;
    color: #A1A1AA;
    border: none;
    font-size: 13px;
    font-weight: 700;
    padding: 0 4px;
}
QPushButton#BubbleClose:hover {
    color: #FFFFFF;
}
"""


def clamp_to_screen(x: int, y: int, width: int, height: int, avail: QRect) -> tuple[int, int]:
    """Clamp widget top-left coordinates inside an available screen rectangle."""
    min_x, min_y = avail.x(), avail.y()
    max_x = max(min_x, avail.x() + avail.width() - width)
    max_y = max(min_y, avail.y() + avail.height() - height)
    return max(min_x, min(x, max_x)), max(min_y, min(y, max_y))


def status_summary(store, topic_id: str | None) -> dict:
    """Collect current topic status, today's paper count, unread events, and active research jobs."""
    topics = {topic.id: topic for topic in store.topics() if not topic.archived}
    if not topic_id or topic_id not in topics:
        topic_id = next(iter(topics), None)
    run, report = daily_overview(store, topic_id) if topic_id else (None, None)
    state = run["status"] if run else None
    followed = topic_id is not None and topics[topic_id].enabled
    label = (
        "请在网页建立主题" if not topic_id
        else RUN_LABELS.get(state, "等待检索" if followed else "仅手动检索")
    )
    events = list_events(store, topic_id or "", limit=10) if topic_id else []
    unnotified_events = [e for e in events if not e.get("notified_at")]
    active_research = store.one("SELECT COUNT(*) AS n FROM research_jobs WHERE status IN ('queued','running')")["n"]
    return {
        "topics": topics,
        "topic_id": topic_id,
        "run": run,
        "report": report,
        "state": state,
        "label": label,
        "paper_count": len(report["papers"]) if report else 0,
        "event_count": len(unnotified_events),
        "research_count": active_research,
        "latest_event": events[0] if events else None,
    }


class LocalServices:
    def __init__(self):
        self.worker = None
        self.web = None
        self.web_url = ""
        self.loopback = build_opener(ProxyHandler({}))

    @staticmethod
    def start(*arguments):
        interpreter = Path(sys.executable)
        if interpreter.name.lower() == "pythonw.exe":
            interpreter = interpreter.with_name("python.exe")
        return subprocess.Popen(
            [str(interpreter), "-B", "-m", *arguments],
            cwd=PROJECT_ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start_worker(self):
        if not self.worker or self.worker.poll() is not None:
            self.worker = self.start("research_radar.cli", "scheduler", "--daily-only")

    def start_web(self):
        if self.web and self.web.poll() is None:
            return
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.web = self.start(
            "research_radar.cli", "serve-api", "--host", "127.0.0.1", "--port", str(port),
        )
        self.web_url = f"http://127.0.0.1:{port}"

    def restart_web(self):
        if self.web and self.web.poll() is None:
            self.web.terminate()
            try:
                self.web.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.web.kill()
                self.web.wait()
        self.web = None
        self.start_web()

    def web_ready(self):
        if not self.web or self.web.poll() is not None:
            return False
        try:
            with self.loopback.open(self.web_url + "/health", timeout=0.35) as response:
                return response.status == 200
        except OSError:
            return False

    def close(self):
        for process in (self.web, self.worker):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


class PetBubble(QFrame):
    clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(260)
        self.target_page = "overview"

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("BubbleCard")
        card.setStyleSheet(BUBBLE_QSS)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        head = QHBoxLayout()
        self.title_lbl = QLabel("科研动态")
        self.title_lbl.setObjectName("BubbleTitle")
        close_btn = QPushButton("×")
        close_btn.setObjectName("BubbleClose")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        head.addWidget(self.title_lbl)
        head.addStretch()
        head.addWidget(close_btn)

        self.body_lbl = QLabel("")
        self.body_lbl.setObjectName("BubbleBody")
        self.body_lbl.setWordWrap(True)
        card_layout.addLayout(head)
        card_layout.addWidget(self.body_lbl)
        root_layout.addWidget(card)

        self.auto_hide = QTimer(self)
        self.auto_hide.setSingleShot(True)
        self.auto_hide.timeout.connect(self.hide)

    def popup(self, title: str, text: str, page: str, anchor: QRect) -> None:
        self.title_lbl.setText(title)
        self.body_lbl.setText(text)
        self.target_page = page
        self.adjustSize()
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x, y = clamp_to_screen(
            anchor.x() + (anchor.width() - self.width()) // 2,
            anchor.y() - self.height() - 10,
            self.width(),
            self.height(),
            avail,
        )
        self.move(x, y)
        self.show()
        self.raise_()
        self.auto_hide.start(6500)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.hide()
            self.clicked.emit(self.target_page)


class CompanionDrawer(QWidget):
    def __init__(self, pet_window):
        super().__init__()
        self.pet = pet_window
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(340)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("DrawerCard")
        card.setStyleSheet(DRAWER_QSS)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Top row: Status pill + collapse
        top = QHBoxLayout()
        top.setSpacing(8)
        self.status_lbl = QLabel("等待主题")
        self.status_lbl.setObjectName("StatusPill")
        top.addWidget(self.status_lbl)
        top.addStretch()
        hide_btn = QPushButton("收起")
        hide_btn.setObjectName("SubtleBtn")
        hide_btn.setCursor(Qt.PointingHandCursor)
        hide_btn.clicked.connect(self.hide)
        top.addWidget(hide_btn)
        layout.addLayout(top)

        self.report_lbl = QLabel("暂无日报")
        self.report_lbl.setObjectName("MetaText")
        layout.addWidget(self.report_lbl)

        # Live stat badges
        badges = QHBoxLayout()
        badges.setSpacing(8)
        self.paper_badge = QLabel("重点论文 0")
        self.paper_badge.setObjectName("BadgeChip")
        self.event_badge = QLabel("新事件 0")
        self.event_badge.setObjectName("BadgeChip")
        self.research_badge = QLabel("专题 0")
        self.research_badge.setObjectName("BadgeChip")
        for b in (self.paper_badge, self.event_badge, self.research_badge):
            badges.addWidget(b)
        layout.addLayout(badges)

        # Topic selector
        self.topic_box = QComboBox()
        self.topic_box.currentIndexChanged.connect(lambda _idx: self.pet.refresh_status())
        layout.addWidget(self.topic_box)

        # Primary CTA
        self.scan_btn = QPushButton("立即检索当前主题")
        self.scan_btn.setObjectName("PrimaryBtn")
        self.scan_btn.setCursor(Qt.PointingHandCursor)
        self.scan_btn.clicked.connect(self.pet.scan)
        layout.addWidget(self.scan_btn)

        # 2x3 Navigation Grid
        grid = QGridLayout()
        grid.setSpacing(8)
        nav_items = [
            ("研究概览", "overview", 0, 0),
            ("对话助手", "assistant", 0, 1),
            ("论文库", "library", 0, 2),
            ("科研项目", "projects", 1, 0),
            ("日报趋势", "reports", 1, 1),
            ("系统配置", "settings", 1, 2),
        ]
        self.nav_buttons = {}
        for title, page, r, c in nav_items:
            btn = QPushButton(title)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, p=page: self.pet.open_page(p))
            grid.addWidget(btn, r, c)
            self.nav_buttons[page] = btn
        layout.addLayout(grid)

        # Message & Footer
        self.message_lbl = QLabel("")
        self.message_lbl.setObjectName("MetaText")
        self.message_lbl.setWordWrap(True)
        layout.addWidget(self.message_lbl)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        restart_btn = QPushButton("重启本地网页")
        restart_btn.setObjectName("SubtleBtn")
        restart_btn.setCursor(Qt.PointingHandCursor)
        restart_btn.clicked.connect(self.pet.restart_web)
        tray_btn = QPushButton("隐藏到托盘")
        tray_btn.setObjectName("SubtleBtn")
        tray_btn.setCursor(Qt.PointingHandCursor)
        tray_btn.clicked.connect(self.pet.hide_to_tray)
        footer.addWidget(restart_btn)
        footer.addWidget(tray_btn)
        layout.addLayout(footer)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


class RadarPetWindow(QWidget):
    def __init__(self, store, services, *, reduce_motion: bool = False):
        super().__init__()
        self.store = store
        self.services = services
        self.reduce_motion = reduce_motion
        self.topic_ids: list[str] = []
        self.topic_labels: list[str] = []
        self.current_state: str | None = None
        self.last_state: tuple[str | None, str | None] | None = None
        self.last_event_id: str | None = None
        self.drag_origin: tuple[QPoint, QPoint] | None = None
        self.dragging = False
        self.web_request: dict | None = None
        self.web_deadline = 0.0

        self.setWindowTitle("ResearchRadar 桌宠")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(132, 132)

        raw = QPixmap(str(PET_IMAGE))
        self.pet_pixmap = raw.scaled(116, 116, Qt.KeepAspectRatio, Qt.SmoothTransformation) if not raw.isNull() else QPixmap()
        self.app_icon = QIcon(self.pet_pixmap) if not self.pet_pixmap.isNull() else QIcon()

        self.drawer = CompanionDrawer(self)
        self.bubble = PetBubble()
        self.bubble.clicked.connect(self.open_page)

        self.menu = QMenu(self)
        self._build_menu(self.menu)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.app_icon, self)
            self.tray.setToolTip("ResearchRadar 科研助手")
            tray_menu = QMenu()
            self._build_menu(tray_menu, for_tray=True)
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()

        self.bob_offset = 0
        self.bob_anim = QPropertyAnimation(self, b"pos", self)
        self.bob_anim.setDuration(420)
        self.bob_anim.setEasingCurve(QEasingCurve.OutCubic)

        self.web_timer = QTimer(self)
        self.web_timer.timeout.connect(self._poll_web)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_status)
        self.poll_timer.start(3000)

        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self._idle_nod)
        self.idle_timer.start(12000)

        self.center_on_primary()
        self.refresh_status()

    def _build_menu(self, menu: QMenu, *, for_tray: bool = False):
        if for_tray:
            menu.addAction("显示 / 置顶桌宠", self.restore_from_tray)
        menu.addAction("复位到主屏右下角", self.center_on_primary)
        menu.addAction("展开 / 收起面板", self.toggle_drawer)
        menu.addSeparator()
        menu.addAction("打开研究概览", lambda: self.open_page("overview"))
        menu.addAction("打开对话助手", lambda: self.open_page("assistant"))
        menu.addAction("打开科研项目", lambda: self.open_page("projects"))
        menu.addAction("打开系统配置", lambda: self.open_page("settings"))
        menu.addSeparator()
        motion_act = QAction("减弱动效", menu, checkable=True)
        motion_act.setChecked(self.reduce_motion)
        motion_act.toggled.connect(self._set_reduce_motion)
        menu.addAction(motion_act)
        menu.addAction("重启本地网页", self.restart_web)
        if not for_tray:
            menu.addAction("隐藏到系统托盘", self.hide_to_tray)
        menu.addSeparator()
        menu.addAction("退出 ResearchRadar", self.quit_app)

    def _set_reduce_motion(self, checked: bool):
        self.reduce_motion = checked

    def _tray_activated(self, reason):
        if reason in {QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick}:
            self.restore_from_tray()

    def restore_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        self.drawer.hide()
        self.bubble.hide()
        if self.tray and self.tray.isVisible():
            self.hide()
            self.tray.showMessage("ResearchRadar", "已最小化到系统托盘，点击图标可随时呼出。",
                                  QSystemTrayIcon.Information, 2500)
        else:
            self.center_on_primary()

    def center_on_primary(self):
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x = avail.x() + avail.width() - self.width() - 36
        y = avail.y() + avail.height() - self.height() - 36
        cx, cy = clamp_to_screen(x, y, self.width(), self.height(), avail)
        self.move(cx, cy)
        self.show()
        self.raise_()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.pet_pixmap.isNull():
            px = (self.width() - self.pet_pixmap.width()) // 2
            py = (self.height() - self.pet_pixmap.height()) // 2
            painter.drawPixmap(px, py, self.pet_pixmap)
        # Status indicator ring + core dot at bottom-right
        color = QColor(STATE_HEX.get(self.current_state, "#64748B"))
        cx, cy = self.width() - 22, self.height() - 22
        painter.setPen(QPen(QColor("#FFFFFF"), 2.5))
        painter.setBrush(color)
        painter.drawEllipse(cx - 8, cy - 8, 16, 16)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_origin = (event.globalPosition().toPoint(), self.pos())
            self.dragging = False
        elif event.button() == Qt.RightButton:
            self.menu.exec(QCursor.pos())

    def mouseMoveEvent(self, event):
        if self.drag_origin is None or not (event.buttons() & Qt.LeftButton):
            return
        start_cursor, start_pos = self.drag_origin
        delta = event.globalPosition().toPoint() - start_cursor
        if not self.dragging and delta.manhattanLength() < 6:
            return
        self.dragging = True
        self.drawer.hide()
        screen = QGuiApplication.screenAt(event.globalPosition().toPoint()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        nx, ny = clamp_to_screen(start_pos.x() + delta.x(), start_pos.y() + delta.y(),
                                 self.width(), self.height(), avail)
        self.move(nx, ny)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.drag_origin is not None:
            if not self.dragging:
                self.toggle_drawer()
            self.drag_origin = None
            self.dragging = False

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key_Return, Qt.Key_Space}:
            self.toggle_drawer()
        elif event.key() == Qt.Key_Escape:
            self.drawer.hide()
        else:
            super().keyPressEvent(event)

    def toggle_drawer(self):
        if self.drawer.isVisible():
            self.drawer.hide()
        else:
            self.show_drawer()

    def show_drawer(self):
        self.refresh_status()
        self.drawer.adjustSize()
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        left_x = self.x() - self.drawer.width() - 12
        right_x = self.x() + self.width() + 12
        preferred_x = left_x if left_x >= avail.x() else right_x
        preferred_y = self.y() + self.height() - self.drawer.height()
        x, y = clamp_to_screen(preferred_x, preferred_y, self.drawer.width(), self.drawer.height(), avail)
        self.drawer.move(x, y)
        self.drawer.show()
        self.drawer.raise_()

    def _idle_nod(self):
        if self.reduce_motion or self.dragging or not self.isVisible() or self.drawer.isVisible():
            return
        base = self.pos()
        self.bob_anim.stop()
        self.bob_anim.setStartValue(base)
        self.bob_anim.setKeyValueAt(0.5, QPoint(base.x(), max(0, base.y() - 6)))
        self.bob_anim.setEndValue(base)
        self.bob_anim.start()

    def selected_topic(self) -> str | None:
        idx = self.drawer.topic_box.currentIndex()
        return self.topic_ids[idx] if 0 <= idx < len(self.topic_ids) else None

    def refresh_status(self):
        selected = self.selected_topic()
        summary = status_summary(self.store, selected)
        topics = summary["topics"]
        labels = [t.name for t in topics.values()]
        if self.topic_ids != list(topics) or self.topic_labels != labels:
            self.topic_ids, self.topic_labels = list(topics), labels
            self.drawer.topic_box.blockSignals(True)
            self.drawer.topic_box.clear()
            self.drawer.topic_box.addItems(labels)
            if topics:
                self.drawer.topic_box.setCurrentIndex(
                    self.topic_ids.index(selected) if selected in topics else 0
                )
            self.drawer.topic_box.blockSignals(False)

        key = self.selected_topic()
        if key != selected:
            summary = status_summary(self.store, key)
        state = summary["state"]
        self.current_state = state
        self.drawer.status_lbl.setText(summary["label"])
        report = summary["report"]
        self.drawer.report_lbl.setText(f"最新日报 · {report['local_day']}" if report else "暂无日报")
        self.drawer.paper_badge.setText(f"重点论文 {summary['paper_count']}")
        self.drawer.event_badge.setText(f"新事件 {summary['event_count']}")
        self.drawer.research_badge.setText(f"专题 {summary['research_count']}")
        self.drawer.scan_btn.setEnabled(bool(key and state not in {"queued", "running"}))
        self.drawer.nav_buttons["reports"].setEnabled(report is not None)
        self.update()

        prev = self.last_state
        self.last_state = (key, state)
        if prev and prev[0] == key and prev[1] != state and state == "succeeded":
            self._idle_nod()
            if self.isVisible():
                self.bubble.popup("日报已生成", f"主题最新日报已就绪，点击查看重点推荐。", "reports", self.frameGeometry())

        latest_ev = summary["latest_event"]
        if latest_ev and self.last_event_id and latest_ev["id"] != self.last_event_id and self.isVisible():
            title = latest_ev["payload"].get("title") or latest_ev["payload"].get("label") or "发现新动态"
            self.bubble.popup("科研事件提醒", str(title)[:90], "reports", self.frameGeometry())
        if latest_ev:
            self.last_event_id = latest_ev["id"]

    def scan(self):
        key = self.selected_topic()
        if not key:
            return
        try:
            self.services.start_worker()
            enqueue_daily(self.store, key)
            run, _ = daily_overview(self.store, key)
            self.drawer.message_lbl.setText(f"今日检索 · {RUN_LABELS[run['status']]}")
            self.refresh_status()
        except (OSError, ValueError) as error:
            self.drawer.message_lbl.setText(str(error))

    def restart_web(self):
        try:
            self.services.restart_web()
            self.drawer.message_lbl.setText("正在重启本地网页…")
        except OSError:
            self.drawer.message_lbl.setText("重启本地网页失败")

    def open_page(self, page: str):
        try:
            self.services.start_web()
        except OSError:
            self.drawer.message_lbl.setText("无法启动本地网页")
            return
        route_map = {
            "overview": ("/briefing", {}),
            "assistant": ("/assistant", {}),
            "library": ("/workbench", {}),
            "projects": ("/research", {}),
            "reports": ("/trends", {}),
            "settings": ("/settings", {}),
        }
        path, extra = route_map.get(page, ("/briefing", {}))
        params = dict(extra)
        if key := self.selected_topic():
            params["topic"] = key
        self.web_request = {"path": path, "params": params}
        if not self._try_open_web():
            self.drawer.message_lbl.setText("正在唤醒本地网页…")
            self.web_deadline = time.monotonic() + 22
            self.web_timer.start(180)

    def _try_open_web(self) -> bool:
        if not self.web_request:
            return True
        if self.services.web_ready():
            path = self.web_request["path"]
            params = self.web_request["params"]
            query = ("?" + urlencode(params)) if params else ""
            url = self.services.web_url + path + query
            self.web_request = None
            self.web_timer.stop()
            self.drawer.message_lbl.setText("网页已打开" if webbrowser.open_new_tab(url) else url)
            return True
        return False

    def _poll_web(self):
        if self._try_open_web():
            return
        if time.monotonic() >= self.web_deadline or (self.services.web and self.services.web.poll() is not None):
            self.web_request = None
            self.web_timer.stop()
            self.drawer.message_lbl.setText("网页启动超时，请检查 FastAPI 服务后重试")

    def quit_app(self):
        self.poll_timer.stop()
        self.idle_timer.stop()
        self.web_timer.stop()
        self.drawer.close()
        self.bubble.close()
        if self.tray:
            self.tray.hide()
        self.close()
        QApplication.quit()


def system_reduces_motion() -> bool:
    if sys.platform != "win32":
        return False
    from ctypes import byref, windll, wintypes
    enabled = wintypes.BOOL()
    available = windll.user32.SystemParametersInfoW(0x1042, 0, byref(enabled), 0)
    return bool(available and not enabled.value)


def main():
    parser = argparse.ArgumentParser(description="ResearchRadar 桌面伴侣")
    parser.add_argument("--open-web", action="store_true", help="启动桌宠后同时打开网页")
    parser.add_argument("--panel", action="store_true", help="启动时展开桌宠控制面板")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    services = LocalServices()
    try:
        store = open_store(Settings.from_env())
        services.start_worker()
        pet = RadarPetWindow(store, services, reduce_motion=system_reduces_motion())
        if not pet.app_icon.isNull():
            app.setWindowIcon(pet.app_icon)
        pet.show()
        if args.panel:
            QTimer.singleShot(50, pet.show_drawer)
        if args.open_web:
            QTimer.singleShot(100, lambda: pet.open_page("overview"))
        return app.exec()
    finally:
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
