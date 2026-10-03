import base64
import json
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                            QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget)
from app.core.registry import GAMES
from app.ui.canvas import Canvas
from app.core.contracts import Stroke
from app.ui.rendering import observation_png

STYLE = '''
QMainWindow, QWidget { background: #f0f2ed; color: #263e38; font-family: "Segoe UI", sans-serif; font-size: 14px; }
QLabel#title { font-size: 30px; font-weight: 600; }
QLabel#eyebrow { font-size: 11px; color: #728477; letter-spacing: 2px; }
QLabel#phase { font-size: 22px; font-weight: 600; }
QLabel#muted { color: #77847c; font-size: 12px; }
QFrame#panel { background: #fafbf7; border: 1px solid #dce1d7; border-radius: 16px; }
QFrame#panel QLabel { background: transparent; }
QPushButton { background: #fafbf7; border: 1px solid #d1d8cc; border-radius: 8px; padding: 11px 17px; }
QPushButton:hover { background: #e4eade; }
QPushButton:disabled { color: #a7b0a6; background: #ecefe8; }
QPushButton#primary { background: #286c5d; color: white; border: 0; font-weight: 600; }
QPushButton#primary:hover { background: #20584c; }
QPushButton#primary:disabled { background: #a3b6ac; }
QComboBox { background: #fafbf7; border: 1px solid #d1d8cc; padding: 8px 14px; border-radius: 8px; }
QTextEdit { background: #fafbf7; border: 1px solid #d1d8cc; border-radius: 8px; font-family: monospace; font-size: 11px; }
'''


class Window(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle('Imajev · Drawing game')
        self.resize(1080, 820)
        self.setMinimumSize(880, 690)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(18)
        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.addWidget(self.label('THE DRAWING ROOM', 'eyebrow'))
        title_column.addWidget(self.label('Your move, Imajev.', 'title'))
        header.addLayout(title_column)
        header.addStretch()
        self.selector = QComboBox()
        for game in GAMES.values():
            self.selector.addItem(game.name, game.id)
        self.selector.setCurrentIndex(self.selector.findData(controller.game.id))
        self.selector.setAccessibleName('Game selector')
        header.addWidget(self.selector)
        new = QPushButton('New game')
        new.clicked.connect(self.new_game)
        header.addWidget(new)
        layout.addLayout(header)
        body = QHBoxLayout()
        self.canvas = Canvas(controller)
        self.canvas.setAccessibleName('Drawing board. Draw an X using the mouse, then submit.')
        self.canvas.drawing_changed.connect(controller.set_pending)
        body.addWidget(self.canvas, 1)
        panel = QFrame()
        panel.setObjectName('panel')
        panel.setFixedWidth(260)
        side = QVBoxLayout(panel)
        side.setContentsMargins(22, 24, 22, 24)
        side.setSpacing(16)
        side.addWidget(self.label('TIC-TAC-TOE / 01', 'eyebrow'))
        self.phase_label = self.label('Your turn', 'phase')
        side.addWidget(self.phase_label)
        self.message = QLabel()
        self.message.setWordWrap(True)
        side.addWidget(self.message)
        side.addWidget(self.label('YOU   X     /     IMAJEV   O', 'muted'))
        side.addStretch()
        side.addWidget(self.label('LAST ACCEPTED MOVE', 'eyebrow'))
        self.last = QLabel()
        self.last.setWordWrap(True)
        side.addWidget(self.last)
        side.addWidget(self.label('LOCAL MODEL', 'eyebrow'))
        self.model = QLabel()
        self.model.setWordWrap(True)
        side.addWidget(self.model)
        if controller.config.tactical_guard:
            tactics = self.label('Immediate wins and blocks enforced', 'muted')
            tactics.setWordWrap(True)
            side.addWidget(tactics)
        self.retry_button = QPushButton('Retry')
        self.retry_button.clicked.connect(controller.retry)
        side.addWidget(self.retry_button)
        self.diag_button = QPushButton('Diagnostics ▾' if controller.config.diagnostics else 'Diagnostics ▸')
        self.diag_button.setCheckable(True)
        self.diag_button.setChecked(controller.config.diagnostics)
        self.diag_button.toggled.connect(self.toggle_diagnostics)
        side.addWidget(self.diag_button)
        body.addWidget(panel)
        layout.addLayout(body, 1)
        controls = QHBoxLayout()
        self.undo_button = QPushButton('Undo stroke')
        self.undo_button.clicked.connect(controller.undo)
        self.clear_button = QPushButton('Clear drawing')
        self.clear_button.clicked.connect(controller.clear)
        self.submit_button = QPushButton('Submit move →')
        self.submit_button.setObjectName('primary')
        self.submit_button.clicked.connect(self.submit)
        controls.addWidget(self.undo_button)
        controls.addWidget(self.clear_button)
        controls.addStretch()
        controls.addWidget(self.submit_button)
        layout.addLayout(controls)
        self.input_hint = self.label('', 'muted')
        self.input_hint.setWordWrap(True)
        layout.addWidget(self.input_hint)
        self.diag = QTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setMaximumHeight(130)
        self.diag.setVisible(controller.config.diagnostics)
        layout.addWidget(self.diag)
        footer = QHBoxLayout()
        footer.addWidget(self.label('A little ink. A little strategy.   ·   Runs on your local model', 'muted'))
        footer.addStretch()
        export = QPushButton('Export session')
        export.clicked.connect(self.export)
        footer.addWidget(export)
        layout.addLayout(footer)
        controller.changed.connect(self.refresh)
        self.refresh()
        QTimer.singleShot(0, controller.start)

    @staticmethod
    def label(text, name):
        label = QLabel(text)
        label.setObjectName(name)
        return label

    def submit(self):
        self.canvas.finish_stroke()
        self.controller.submit()

    def new_game(self):
        self.canvas.current = []
        self.controller.game = GAMES[self.selector.currentData()]
        self.controller.new_game()
        if not self.controller.ready and not self.controller.busy:
            self.controller.start()

    def toggle_diagnostics(self, checked):
        self.diag.setVisible(checked)
        self.diag_button.setText('Diagnostics ▾' if checked else 'Diagnostics ▸')

    def refresh(self):
        c = self.controller
        phases = {'loading': 'Warming up', 'human': 'Your turn', 'recognising': 'Reading your ink',
                  'computer': 'Imajev’s turn', 'over': 'Game complete', 'error': 'Needs attention'}
        self.phase_label.setText(phases[c.phase])
        self.message.setText(c.message)
        self.last.setText(c.last_move)
        status = 'Working' if c.busy else ('Attention needed' if c.phase == 'error' else ('Ready' if c.ready else 'Connecting'))
        self.model.setText(f'{c.config.expected_model} · {status}')
        self.retry_button.setVisible(c.phase == 'error')
        self.retry_button.setEnabled(not c.busy)
        self.undo_button.setEnabled(c.editable and bool(c.pending))
        self.clear_button.setEnabled(c.editable and bool(c.pending))
        self.submit_button.setEnabled(c.editable and bool(c.pending))
        if c.editable:
            self.input_hint.setText('Click and drag with the left mouse button to draw. Release to finish a stroke.')
        elif c.phase == 'over':
            self.input_hint.setText('This game has ended. Choose New game to draw again.')
        elif not c.ready:
            self.input_hint.setText('Drawing is disabled until the local Imajev service is ready at ' + c.config.endpoint)
        else:
            self.input_hint.setText('Drawing is paused. ' + c.message)
        log_info = f'\nMouse diagnostics: {c.log_path}' if getattr(c, 'log_path', None) else ''
        self.diag.setPlainText(c.diagnostics + ('\n' + c.storage_error if c.storage_error else '') + log_info)

    def export(self):
        c = self.controller
        path, _ = QFileDialog.getSaveFileName(self, 'Export session', f'imajev-{c.session_id[:8]}.json', 'JSON records (*.json)')
        if path:
            try:
                record = c.record()
                record['events'] = [dict(e) for e in record['events']]
                for event in record['events']:
                    if 'ticket' in event:
                        state = c.game.decode_state(event['state'])
                        drawing = tuple(Stroke(tuple(tuple(p) for p in s['points']), s['width'], s['color']) for s in event['drawing'])
                        purpose = 'decision' if event['ticket']['purpose'] == 'decision' else 'recognition'
                        png = observation_png(c.game.render(state, drawing, purpose), c.config.observation_size)
                        event['image_png_base64'] = base64.b64encode(png).decode('ascii')
                        event['image'] = None
                Path(path).write_text(json.dumps(record, indent=2, allow_nan=False), encoding='utf-8')
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, 'Export failed', str(exc))

    def closeEvent(self, event):
        if self.controller.busy:
            # Do not destroy a running QThreadPool or block the UI waiting for a GPU request.
            self.controller.active = None
            self.controller.stopping = True
            self.hide()
            self.close_timer = QTimer(self)
            self.close_timer.timeout.connect(self.finish_close)
            self.close_timer.start(100)
            event.ignore()
        else:
            event.accept()

    def finish_close(self):
        if not self.controller.busy:
            self.close_timer.stop()
            self.close()
