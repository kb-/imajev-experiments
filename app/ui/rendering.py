from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QRadialGradient
from app.core.contracts import Scene


def paint_scene(painter: QPainter, rect: QRectF, scene: Scene) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(rect)
    painter.fillRect(rect, QColor(scene.background))
    painter.translate(rect.topLeft())
    painter.scale(rect.width(), rect.height())
    for x, y, radius in scene.pockets:
        gradient = QRadialGradient(QPointF(x, y), radius)
        gradient.setColorAt(0, QColor('#b69669'))
        gradient.setColorAt(.8, QColor('#c5aa80'))
        gradient.setColorAt(1, QColor('#f4dfbb'))
        painter.setPen(QPen(QColor('#b99b73'), .0015))
        painter.setBrush(gradient)
        painter.drawEllipse(QPointF(x, y), radius, radius)
    for stone in scene.stones:
        x, y, radius = stone.x, stone.y, stone.radius
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(45, 31, 17, 70))
        painter.drawEllipse(QPointF(x+.004, y+.006), radius*1.02, radius)
        gradient = QRadialGradient(QPointF(x-radius*.3, y-radius*.35), radius*1.5)
        light, middle, dark, edge = stone.colors
        gradient.setColorAt(0, QColor(light))
        gradient.setColorAt(.35, QColor(middle))
        gradient.setColorAt(1, QColor(dark))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(edge), .0015))
        painter.drawEllipse(QPointF(x, y), radius, radius)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for x, y, radius, color in scene.highlights:
        painter.setPen(QPen(QColor(color), .004))
        painter.drawEllipse(QPointF(x, y), radius, radius)
    painter.setPen(QPen(QColor('#d7dbd5'), scene.line_width))
    for x1, y1, x2, y2 in scene.lines:
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    def draw_labels():
        # Pixel-space text keeps typography and its protection patch consistent.
        painter.save()
        painter.scale(1 / rect.width(), 1 / rect.height())
        font = QFont('Segoe UI')
        font.setPixelSize(max(10, round(rect.width() * .023)))
        painter.setFont(font)
        painter.setPen(QColor(scene.label_color))
        for label, x, y in scene.labels:
            point = QPointF(x * rect.width(), y * rect.height())
            if scene.protect_labels:
                bounds = painter.fontMetrics().boundingRect(label).translated(
                    round(point.x()), round(point.y())).adjusted(-2, -2, 2, 2)
                painter.fillRect(bounds, QColor(scene.background))
            painter.drawText(point, label)
        painter.restore()
    if not scene.protect_labels:
        draw_labels()
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
    if scene.protect_labels:
        draw_labels()
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
