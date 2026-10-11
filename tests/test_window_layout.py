from dataclasses import replace
import pytest
from PyQt6.QtCore import QPoint
from app.ui.window import Window
from test_session import make


def test_inline_diagnostics_keeps_turn_message_readable(qapp):
    controller, _ = make(qapp)
    controller.config = replace(controller.config, diagnostics=True)
    window = Window(controller)
    window.resize(880, 690)
    window.show()
    qapp.processEvents()
    assert window.details_tabs.isVisible()
    assert window.explanation.isVisible()
    assert window.details_tabs.parentWidget() is window.centralWidget()
    assert window.phase_label.height() >= window.phase_label.sizeHint().height()
    assert window.message.height() >= window.message.sizeHint().height()
    message_bottom = window.message.mapTo(window.panel_scroll.viewport(), QPoint(0, window.message.height())).y()
    assert message_bottom <= window.panel_scroll.viewport().height()

    controller.message = 'A long diagnostic message that explains an invalid drawing or model error. ' * 7
    window.refresh()
    qapp.processEvents()
    assert window.message.height() >= window.message.sizeHint().height()
    assert window.panel_scroll.verticalScrollBar().maximum() > 0
    window.diag_button.setChecked(False)
    qapp.processEvents()
    assert not window.details_tabs.isVisible()
    window.close()


@pytest.mark.parametrize('phase', ['human', 'computer'])
def test_move_prompt_fits_sidebar_with_long_model_name(qapp, monkeypatch, phase):
    controller, _ = make(qapp)
    monkeypatch.setattr(controller, 'start', lambda: None)
    controller.config = replace(controller.config, opponent=replace(
        controller.config.opponent_settings, model='liquid-d1-3b-q8'))
    controller.phase = phase
    window = Window(controller)
    window.resize(880, 690)
    window.show()
    qapp.processEvents()
    viewport = window.panel_scroll.viewport()
    selector = window.prompt_selector
    left = selector.mapTo(viewport, QPoint(0, 0)).x()
    right = selector.mapTo(viewport, QPoint(selector.width(), 0)).x()
    assert left >= 0 and right <= viewport.width()
    assert window.panel_scroll.widget().width() == viewport.width()
    window.close()
