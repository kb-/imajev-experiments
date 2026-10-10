import logging
from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget
from app.core.contracts import Stroke
from app.ui.rendering import paint_scene


logger = logging.getLogger(__name__)


class Canvas(QWidget):
    drawing_changed = pyqtSignal(object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.current = []
        self.setMinimumSize(360, 360)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        controller.changed.connect(self.sync)

    def board_rect(self):
        size = min(self.width(), self.height()) - 24
        return QRectF((self.width()-size)/2, (self.height()-size)/2, size, size)

    def sync(self):
        if not self.controller.editable:
            self.current = []
        self.setCursor(Qt.CursorShape.CrossCursor if self.controller.editable else Qt.CursorShape.ArrowCursor)
        self.setToolTip(self.controller.instruction if self.controller.editable else f'Drawing disabled: {self.controller.message}')
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.board_rect()
        drawing = self.controller.pending + ((Stroke(tuple(self.current)),) if self.current else ())
        scene = self.controller.game.render(self.controller.state, drawing, 'display')
        paint_scene(painter, rect, scene)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#d7dbd5'), 1))
        painter.drawRect(rect)
        outcome = self.controller.game.outcome(self.controller.state)
        if outcome.line:
            painter.setPen(QPen(QColor('#708763'), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(*(QPointF(rect.x()+x*rect.width(), rect.y()+y*rect.height()) for x, y in outcome.line))
        painter.end()

    def point(self, position, clip=True):
        rect = self.board_rect()
        x, y = (position.x()-rect.x())/rect.width(), (position.y()-rect.y())/rect.height()
        return (max(0., min(1., x)), max(0., min(1., y))) if clip else (x, y)

    def log_mouse(self, kind, event):
        logger.debug('mouse_%s x=%.1f y=%.1f button=%s buttons=%s inside=%s editable=%s phase=%s current_points=%d',
                     kind, event.position().x(), event.position().y(), event.button().name,
                     event.buttons().name, self.board_rect().contains(event.position()),
                     self.controller.editable, self.controller.phase, len(self.current))

    def mousePressEvent(self, event):
        self.log_mouse('press', event)
        if event.button() != Qt.MouseButton.LeftButton:
            logger.debug('press ignored: use the left mouse button')
            return
        if not self.controller.editable:
            logger.info('press ignored: drawing disabled; phase=%s busy=%s reason=%s',
                        self.controller.phase, self.controller.busy, self.controller.message)
            return
        if self.board_rect().contains(event.position()):
            self.current = [self.point(event.position())]
            logger.debug('stroke started normalized=%s', self.current[0])
            self.update()
        else:
            logger.debug('press ignored: outside the square board')

    def mouseMoveEvent(self, event):
        self.log_mouse('move', event)
        if self.current:
            point = self.point(event.position(), clip=False)
            if not self.board_rect().contains(event.position()):
                ax, ay = self.current[-1]
                bx, by = point
                fractions = [1.]
                if bx < 0: fractions.append((0 - ax)/(bx - ax))
                if bx > 1: fractions.append((1 - ax)/(bx - ax))
                if by < 0: fractions.append((0 - ay)/(by - ay))
                if by > 1: fractions.append((1 - ay)/(by - ay))
                t = min(fractions)
                point = (max(0., min(1., ax+t*(bx-ax))), max(0., min(1., ay+t*(by-ay))))
            self.current.append(point)
            # End at the edge. Re-entering starts a new stroke, avoiding lines along the border.
            if not self.board_rect().contains(event.position()):
                self.finish_stroke('board boundary')
            self.update()

    def mouseReleaseEvent(self, event):
        self.log_mouse('release', event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self.current and self.board_rect().contains(event.position()):
                self.current.append(self.point(event.position()))
            self.finish_stroke('mouse release')

    def finish_stroke(self, reason='control action'):
        if self.current:
            stroke = Stroke(tuple(self.current))
            self.current = []
            logger.debug('stroke finished reason=%s points=%d editable=%s', reason, len(stroke.points), self.controller.editable)
            if self.controller.editable:
                self.drawing_changed.emit(self.controller.pending + (stroke,))
            self.update()

    def event(self, event):
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut):
            logger.debug('mouse capture ended: %s', event.type().name)
            self.finish_stroke(event.type().name)
        return super().event(event)
