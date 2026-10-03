from dataclasses import replace
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
    assert window.diag.isVisible()
    assert window.diag.parentWidget() is window.centralWidget()
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
    assert not window.diag.isVisible()
    window.close()
