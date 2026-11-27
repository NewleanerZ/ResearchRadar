from pathlib import Path
import py_compile

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from research_radar.desktop import LocalServices, RadarPetWindow, clamp_to_screen, status_summary
from research_radar.events import record_event
from test_radar import FixtureSources, preprint, radar, run_daily, worker


def test_pyside6_desktop_pet_offscreen_and_launchers(radar, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    store, topic = radar
    run_daily(store, topic, worker(store, FixtureSources([preprint()])))
    record_event(store, topic.id, None, "hotspot_heating", "heat:1", {"label": "点云"})

    summary = status_summary(store, topic.id)
    assert summary["topic_id"] == topic.id and summary["paper_count"] == 1 and summary["event_count"] == 1

    avail = QRect(0, 0, 1920, 1040)
    assert clamp_to_screen(2500, 1500, 132, 132, avail) == (1788, 908)
    assert clamp_to_screen(-50, -20, 132, 132, avail) == (0, 0)

    app = QApplication.instance() or QApplication([])
    services = LocalServices()
    pet = RadarPetWindow(store, services, reduce_motion=True)
    pet.show()
    pet.show_drawer()
    assert pet.drawer.isVisible() and pet.drawer.paper_badge.text() == "重点论文 1"
    pet.bubble.popup("科研事件提醒", "发现新代码开源", "reports", pet.frameGeometry())
    assert pet.bubble.isVisible()
    pet.toggle_drawer()
    assert not pet.drawer.isVisible()
    pet.quit_app()
    app.processEvents()

    assert Path("launch_radar.bat").exists()
    py_compile.compile(str(Path("launch_radar.pyw")), doraise=True)
