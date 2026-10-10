from dataclasses import replace
import base64
import json
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout,
                            QMainWindow, QMessageBox, QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget)
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
QFrame#panel QScrollArea, QFrame#panel QScrollArea QWidget { background: #fafbf7; border: 0; }
QPushButton { background: #fafbf7; border: 1px solid #d1d8cc; border-radius: 8px; padding: 11px 17px; }
QPushButton:hover { background: #e4eade; }
QPushButton:disabled { color: #a7b0a6; background: #ecefe8; }
QPushButton#primary { background: #286c5d; color: white; border: 0; font-weight: 600; }
QPushButton#primary:hover { background: #20584c; }
QPushButton#primary:disabled { background: #a3b6ac; }
QComboBox, QDoubleSpinBox { background: #fafbf7; border: 1px solid #d1d8cc; padding: 8px 14px; border-radius: 8px; }
QTextEdit { background: #fafbf7; border: 1px solid #d1d8cc; border-radius: 8px; font-family: monospace; font-size: 11px; }
'''


def format_question_history(events):
    """Show the exact question instructions sent to the model, newest first."""
    entries = []
    for event in reversed(events):
        request = event.get('request')
        if not request:
            continue
        purpose = event['ticket']['purpose']
        attempt = event.get('decision_attempt')
        heading = purpose.title() + (f' · retry {attempt}' if purpose == 'decision' and attempt else '')
        lines = [heading]
        for name, question in request.get('questions', {}).items():
            lines.append(f'{name}: {question.get("instructions", "")}')
            choices = question.get('criteria', {})
            if choices:
                lines.append('Choices: ' + ', '.join(choices))
        if event.get('rejection'):
            lines.append('Result: ' + event['rejection'])
        elif event.get('error'):
            lines.append('Result: ' + str(event['error']))
        elif event.get('accepted_action'):
            proposed = event.get('model_proposed_action')
            result = f'Result: {event["accepted_action"]}'
            if proposed and proposed != event['accepted_action']:
                result += f' (model proposed {proposed})'
            lines.append(result)
        elif event.get('reply'):
            answers = event['reply'].get('answers', {})
            lines.append('Result: ' + ', '.join(
                f'{name}={answer.get("choice")}{" (abstained)" if answer.get("abstained") else ""}'
                for name, answer in answers.items()))
        else:
            lines.append('Result: waiting for model')
        if event.get('move_sampling'):
            sampling = event['move_sampling']
            lines.append(f'Move sampling: temperature {sampling["temperature"]:g}, selected {sampling["selected_action"]}')
        entries.append('\n'.join(lines))
    return '\n\n'.join(entries) if entries else 'No model questions yet.'


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
        new = self.new_button = QPushButton('New game')
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
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_scroll = QScrollArea(panel)
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel_layout.addWidget(self.panel_scroll)
        side_content = QWidget()
        side = QVBoxLayout(side_content)
        side.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        side.setContentsMargins(22, 18, 22, 18)
        side.setSpacing(10)
        self.panel_scroll.setWidget(side_content)
        side.addWidget(self.label('TIC-TAC-TOE / 01', 'eyebrow'))
        self.phase_label = self.label('Your turn', 'phase')
        side.addWidget(self.phase_label)
        self.message = QLabel()
        self.message.setWordWrap(True)
        side.addWidget(self.message)
        side.addWidget(self.label('YOU   X     /     IMAJEV   O', 'muted'))
        self.starter_label = self.label('', 'muted')
        side.addWidget(self.starter_label)
        side.addStretch()
        side.addWidget(self.label('LAST ACCEPTED MOVE', 'eyebrow'))
        self.last = QLabel()
        self.last.setWordWrap(True)
        side.addWidget(self.last)
        side.addWidget(self.label('MOVE PROMPT', 'eyebrow'))
        self.prompt_selector = QComboBox()
        self.prompt_selector.addItem('Original', 'legacy')
        self.prompt_selector.addItem('Quoted', 'quoted')
        self.prompt_selector.addItem('Coached quoted', 'coached_quoted')
        self.prompt_selector.setCurrentIndex(self.prompt_selector.findData(controller.config.prompt_variant))
        self.prompt_selector.setAccessibleName('Move prompt')
        self.prompt_selector.setToolTip('Choose a prompt before your first move. After play begins, click New game to apply changes.')
        side.addWidget(self.prompt_selector)
        self.prompt_label = self.label('', 'muted')
        self.prompt_label.setWordWrap(True)
        side.addWidget(self.prompt_label)
        self.prompt_selector.currentIndexChanged.connect(self.configure_game)
        side.addWidget(self.label('MOVE VARIETY', 'eyebrow'))
        self.move_temperature = QDoubleSpinBox()
        self.move_temperature.setRange(0, 3)
        self.move_temperature.setDecimals(2)
        self.move_temperature.setSingleStep(.25)
        self.move_temperature.setSpecialValueText('Off · best move')
        self.move_temperature.setValue(controller.config.move_temperature)
        self.move_temperature.setAccessibleName('Computer move temperature')
        self.move_temperature.setToolTip('0 chooses the best move. 1 samples from Imajev’s preferences. Higher values spread choices more evenly. Applies immediately before your first move; otherwise on New game.')
        side.addWidget(self.move_temperature)
        self.variety_label = self.label('', 'muted')
        self.variety_label.setWordWrap(True)
        side.addWidget(self.variety_label)
        self.move_temperature.valueChanged.connect(self.configure_game)
        side.addWidget(self.label('LOCAL MODEL', 'eyebrow'))
        self.model = QLabel()
        self.model.setWordWrap(True)
        side.addWidget(self.model)
        self.tactics_label = self.label('Immediate wins and blocks enforced', 'muted')
        self.tactics_label.setWordWrap(True)
        side.addWidget(self.tactics_label)
        self.strategy_label = QLabel()
        self.strategy_label.setWordWrap(True)
        side.addWidget(self.strategy_label)
        self.continue_button = QPushButton('Continue with previous strategy')
        self.continue_button.clicked.connect(controller.continue_coaching)
        side.addWidget(self.continue_button)
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
        self.diag.setMinimumHeight(160)
        self.diag.setMaximumHeight(240)
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

    def configure_game(self):
        try:
            self.controller.configure_unstarted_game(
                self.prompt_selector.currentData(), self.move_temperature.value())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot apply game settings', str(exc))
        self.refresh()

    def new_game(self):
        if self.controller.coaching_failed or (self.controller.busy and self.controller.inflight and self.controller.inflight.purpose in ('coach', 'continue', 'shutdown')):
            return
        self.canvas.current = []
        self.controller.game = GAMES[self.selector.currentData()]
        self.controller.config = replace(self.controller.config, prompt_variant=self.prompt_selector.currentData(),
                                         move_temperature=self.move_temperature.value())
        self.controller.new_game(alternate_starter=True)
        if not self.controller.ready and not self.controller.busy:
            self.controller.start()

    def toggle_diagnostics(self, checked):
        self.diag.setVisible(checked)
        self.diag_button.setText('Diagnostics ▾' if checked else 'Diagnostics ▸')

    def refresh(self):
        c = self.controller
        phases = {'loading': 'Warming up', 'human': 'Your turn', 'recognising': 'Reading your ink',
                  'computer': 'Imajev’s turn', 'coaching': 'Studying games', 'over': 'Game complete', 'error': 'Needs attention'}
        self.phase_label.setText(c.message.rstrip('…') if c.phase == 'coaching' else phases[c.phase])
        starter = getattr(c.state, 'starting_player', None)
        self.starter_label.setText('You started · X' if starter == c.game.human_player else 'Imajev started · O' if starter == c.game.computer_player else '')
        current_prompt = {'legacy': 'Original', 'quoted': 'Quoted', 'coached_quoted': 'Coached quoted'}[c.config.prompt_variant]
        pending = self.prompt_selector.currentData() != c.config.prompt_variant
        self.prompt_label.setText(f'Playing: {current_prompt}. ' + (
            'Click New game to apply selection.' if pending else
            'Choose a mode before your first move.' if c.can_configure_game else 'Selection applies to new games.'))
        self.prompt_selector.setEnabled(getattr(c.game, 'supports_prompt_variants', False))
        temperature = c.config.move_temperature
        pending_variety = self.move_temperature.value() != temperature
        variety = 'best move' if temperature == 0 else f'temperature {temperature:g}'
        self.variety_label.setText(f'Playing: {variety}. ' + (
            'Click New game to apply selection.' if pending_variety else '0 picks best; higher adds variety.'))
        self.tactics_label.setVisible(c.config.tactical_guard and not c.coached)
        self.message.setText(c.message)
        self.message.setMinimumHeight(self.message.sizeHint().height())
        self.last.setText(c.last_move)
        self.last.setMinimumHeight(self.last.sizeHint().height())
        status = 'Working' if c.busy else ('Attention needed' if c.phase == 'error' else ('Ready' if c.ready else 'Connecting'))
        self.model.setText(f'{c.config.expected_model} · {status}')
        self.model.setMinimumHeight(self.model.sizeHint().height())
        self.new_button.setEnabled(not c.coaching_failed and not (c.busy and c.inflight and c.inflight.purpose in ('coach', 'continue', 'shutdown')))
        self.continue_button.setVisible(c.coaching_failed)
        self.continue_button.setEnabled(not c.busy)
        self.strategy_label.setVisible(c.coached)
        if c.coached:
            ledger = c.next_strategy
            self.strategy_label.setText(f'{"Next game strategy" if ledger["revision"] != c.strategy_revision else "Strategy"} · revision {ledger["revision"]}\n' + '\n'.join(f'{i}. {rule}' for i, rule in enumerate(ledger['strategy'], 1)))
        self.retry_button.setText('Retry coaching' if c.coaching_failed else 'Retry')
        self.retry_button.setVisible(c.phase == 'error' or c.coaching_failed)
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
        coach_info = c.coach_diagnostics or (json.dumps(c.coach_request, ensure_ascii=False, indent=2) if c.coach_request else '')
        diagnosis = '\n\nCOACH DIAGNOSIS\n' + c.coach_diagnosis if c.coach_diagnosis else ''
        summary = c.diagnostics + diagnosis + ('\n\nCOACHING\n' + coach_info if coach_info else '') + ('\n' + c.storage_error if c.storage_error else '') + log_info
        self.diag.setPlainText(summary + '\n\nQUESTIONS ASKED (NEWEST FIRST)\n' + format_question_history(c.events))

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
        c = self.controller
        if c.shutdown_done:
            event.accept()
            return
        c.active = None
        c.stopping = True
        self.hide()
        if not c.busy:
            c.begin_shutdown()
        if c.shutdown_done:
            event.accept()
            return
        if not hasattr(self, 'close_timer'):
            self.close_timer = QTimer(self)
            self.close_timer.timeout.connect(self.finish_close)
            self.close_timer.start(100)
        event.ignore()

    def finish_close(self):
        c = self.controller
        if not c.busy:
            c.begin_shutdown()
        if c.shutdown_done:
            self.close_timer.stop()
            self.close()
