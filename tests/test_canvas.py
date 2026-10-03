from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from app.ui.canvas import Canvas
from app.ui.rendering import observation_png
from test_session import make


def test_strokes_resize_undo_and_capture(qapp):
    c, fake = make(qapp)
    canvas = Canvas(c)
    canvas.drawing_changed.connect(c.set_pending)
    canvas.resize(540, 540)
    canvas.show()
    qapp.processEvents()
    for start, end in ((QPoint(50, 50), QPoint(140, 140)), (QPoint(140, 50), QPoint(50, 140))):
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert len(c.pending) == 2
    frozen = c.pending
    image = observation_png(c.game.render(c.state, c.pending, 'recognition'))
    canvas.resize(900, 700)
    qapp.processEvents()
    assert c.pending == frozen
    assert observation_png(c.game.render(c.state, c.pending, 'recognition')) == image
    c.undo()
    assert len(c.pending) == 1
    c.clear()
    assert not c.pending
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 100))
    QTest.mouseMove(canvas, QPoint(300, 150))
    QApplication.sendEvent(canvas, QEvent(QEvent.Type.UngrabMouse))
    assert len(c.pending) == 1 and not canvas.current
    c.clear()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 100))
    QTest.mouseMove(canvas, QPoint(0, 100))
    assert len(c.pending) == 1 and not canvas.current
    assert all(0 <= v <= 1 for s in c.pending for p in s.points for v in p)
    canvas.close()


def test_blocked_click_is_logged_and_drawing_resumes_when_ready(qapp, caplog):
    import logging
    c, fake = make(qapp)
    c.ready = False
    c.new_game()
    canvas = Canvas(c)
    canvas.drawing_changed.connect(c.set_pending)
    canvas.resize(540, 540)
    canvas.show()
    qapp.processEvents()
    with caplog.at_level(logging.DEBUG, logger='app.ui.canvas'):
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
        QTest.mouseMove(canvas, QPoint(140, 140))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(140, 140))
    assert not c.pending and not canvas.current
    assert 'mouse_press' in caplog.text and 'mouse_release' in caplog.text
    assert 'drawing disabled; phase=loading' in caplog.text
    c.ready = True
    c.new_game()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    QTest.mouseMove(canvas, QPoint(140, 140))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(140, 140))
    assert len(c.pending) == 1
    canvas.close()
