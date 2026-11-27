import os
from pathlib import Path
import py_compile

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
import pytest

from research_radar.desktop import LocalServices, RadarPetWindow, clamp_to_screen, status_summary
from research_radar.events import record_event
from research_radar.ui_theme import brand_header_html, paper_card_html, status_pill, svg_icon, theme_css
from test_radar import FixtureSources, preprint, radar, run_daily, worker


def test_ui_theme_tokens_svg_and_paper_cards():
    light = theme_css("light", "comfortable")
    dark = theme_css("dark", "compact")
    assert "--color-primary: #7C3AED;" in light and "--space-card: 1.12rem;" in light
    assert "--color-background: #0F0D1A;" in dark and "--space-card: 0.72rem;" in dark
    assert 'aria-hidden="true"' in svg_icon("radar")
    assert "ResearchRadar" in brand_header_html()
    pill = status_pill("已发表", "success", "check")
    assert "rr-pill-success" in pill and "已发表" in pill
    card = paper_card_html(
        title="Point <script>Cloud</script>",
        url="https://arxiv.org/abs/2611.00001",
        authors="Ada Lee",
        date_label="2026-11-25",
        pub_label="已发表",
        pub_tone="success",
        relevance=0.88,
        summary="Summary text",
        inference="Matches 3D AD",
        links=[{"label": "arXiv", "url": "https://arxiv.org/abs/2611.00001"}],
    )
    assert "&lt;script&gt;" in card and "相关性 88%" in card and "Matches 3D AD" in card


def test_streamlit_dark_mode_compact_and_all_pages(radar, monkeypatch):
    from streamlit.testing.v1 import AppTest

    store, topic = radar
    run_daily(store, topic, worker(store, FixtureSources([preprint()])))
    monkeypatch.setenv("RADAR_DATA_DIR", str(store.directory))

    app = AppTest.from_file(str(Path("streamlit_app.py").resolve()), default_timeout=15).run()
    assert not app.exception
    app.sidebar.toggle(key="rr_dark_mode").set_value(True).run()
    app.sidebar.toggle(key="rr_compact_mode").set_value(True).run()
    assert not app.exception
    for route in ("assistant", "library", "reports", "projects", "topics", "runs", "settings", "overview"):
        app.sidebar.radio[0].set_value(route).run()
        assert not app.exception


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
