from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from app.core.contracts import Scene


def paint_scene(painter: QPainter, rect: QRectF, scene: Scene) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(rect)
    painter.fillRect(rect, QColor('#faf8f1'))
    painter.translate(rect.topLeft())
    painter.scale(rect.width(), rect.height())
    painter.setPen(QPen(QColor('#d7dbd5'), .004))
    for x1, y1, x2, y2 in scene.lines:
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    font = QFont('Segoe UI')
    font.setPointSizeF(1)
    painter.setFont(font)
    # Labels use a separate pixel-space painter transform for consistent typography.
    painter.save()
    painter.scale(1 / rect.width(), 1 / rect.height())
    font.setPixelSize(max(10, round(rect.width() * .023)))
    painter.setFont(font)
    painter.setPen(QColor('#929b93'))
    for label, x, y in scene.labels:
        painter.drawText(QPointF(x * rect.width(), y * rect.height()), label)
    painter.restore()
    painter.setPen(QPen(QColor('#bc7854'), .015, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for x, y, radius in scene.circles:
        painter.drawEllipse(QPointF(x, y), radius, radius)
    for stroke in scene.strokes:
        painter.setPen(QPen(QColor(stroke.color), stroke.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if len(stroke.points) == 1:
            painter.drawPoint(QPointF(*stroke.points[0]))
        elif stroke.points:
            path = QPainterPath(QPointF(*stroke.points[0]))
            for point in stroke.points[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)
    painter.restore()


def observation_png(scene: Scene, size: int = 768) -> bytes:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    paint_scene(painter, QRectF(0, 0, size, size), scene)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('Could not render the observation.')
    return bytes(data)
