from dataclasses import replace
import base64
import json
import logging
import time
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout,
                            QMainWindow, QMessageBox, QPushButton, QScrollArea, QTabWidget, QTextEdit, QVBoxLayout, QWidget)
from app.core.registry import GAMES
from app.ui.canvas import Canvas
from app.core.contracts import Stroke
from app.ui.rendering import observation_png

logger = logging.getLogger(__name__)

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


def format_game_explanation(game, config, mode, temperature, status):
    """Describe the configured experiment; actual per-turn prompts appear below it."""
    policy = game.session_policy
    info = policy.explanation(config, mode)
    recognition, opponent = config.recognition_settings, config.opponent_settings
    score = 'effective probability' if recognition.backend == 'imajev' else 'choice probability'
    fallback = (f' After {config.fallback_after_rejections} rejected drawings at the same board position, '
                f'{config.recognition_fallback.model} retries recognition with an effective-probability threshold '
                f'of {config.fallback_threshold:g}.' if config.recognition_fallback else '')
    model_input = ('a rendered board image and the symbolic request' if opponent.input_mode == 'text_image'
                   else 'the symbolic request, without a board image')
    variety = ('The app takes the model’s reported legal choice.' if temperature == 0 else
               f'The app samples a legal choice from reported move probabilities at temperature {temperature:g}.')
    guard = (info['guard'] if config.tactical_guard and mode != 'coached_quoted' else
             'Tactical corrections are off for coached learning.' if mode == 'coached_quoted' else
             'Tactical corrections are disabled by configuration; the legal model choice is played as proposed.')
    return '\n'.join((
        'HOW THIS GAME WORKS',
        f'{game.name} · {dict(policy.prompt_choices).get(mode, mode)} · {status}',
        '', 'RULES', info['rules'],
        '', 'WHAT THE MODELS RECEIVE',
        f'Recognition: {recognition.model} ({recognition.backend}) sees your drawing in a labelled game view '
        f'and chooses its symbol and cell. Each answer must pass the {score} threshold {config.threshold:g}; '
        'the app then checks drawing geometry and game legality.' + fallback,
        f'Opponent: {opponent.model} ({opponent.backend}, {opponent.protocol}) receives {model_input}. '
        + info['opponent_input'],
        '', 'HOW DECISIONS ARE HANDLED',
        variety + ' The rules engine rejects illegal or abstained answers and retries; three rejected '
        'decision retries after the initial failure forfeit the game.',
        guard,
        'The session records the model proposal, any sampled choice, any tactical correction, and the accepted action. '
        'The Activity log tab shows the exact question instructions and choices sent to the model.',
    ))


class Window(QMainWindow):
    closed = pyqtSignal()

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
        self.selector.currentIndexChanged.connect(self.configure_selected_game)
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
        self.game_label = self.label('', 'eyebrow')
        side.addWidget(self.game_label)
        self.phase_label = self.label('Your turn', 'phase')
        self.phase_label.setWordWrap(True)
        side.addWidget(self.phase_label)
        self.message = QLabel()
        self.message.setWordWrap(True)
        side.addWidget(self.message)
        self.players_label = self.label('', 'muted')
        self.players_label.setWordWrap(True)
        side.addWidget(self.players_label)
        self.game_details = self.label('', 'muted')
        self.game_details.setWordWrap(True)
        side.addWidget(self.game_details)
        self.starter_label = self.label('', 'muted')
        self.starter_label.setWordWrap(True)
        side.addWidget(self.starter_label)
        side.addStretch()
        side.addWidget(self.label('LAST ACCEPTED MOVE', 'eyebrow'))
        self.last = QLabel()
        self.last.setWordWrap(True)
        side.addWidget(self.last)
        self.prompt_heading = self.label('MOVE PROMPT', 'eyebrow')
        side.addWidget(self.prompt_heading)
        self.prompt_selector = QComboBox()
        self.prompt_game_id = controller.game.id
        self.populate_prompt_selector()
        self.prompt_selector.setAccessibleName('Move prompt')
        self.prompt_selector.setToolTip('Choose a prompt before your first move. After play begins, click New game to apply changes.')
        side.addWidget(self.prompt_selector)
        self.prompt_label = self.label('', 'muted')
        self.prompt_label.setWordWrap(True)
        side.addWidget(self.prompt_label)
        self.prompt_availability = self.label('', 'muted')
        self.prompt_availability.setWordWrap(True)
        side.addWidget(self.prompt_availability)
        self.prompt_selector.currentIndexChanged.connect(self.configure_game)
        side.addWidget(self.label('MOVE VARIETY', 'eyebrow'))
        self.move_temperature = QDoubleSpinBox()
        self.move_temperature.setRange(0, 3)
        self.move_temperature.setDecimals(2)
        self.move_temperature.setSingleStep(.25)
        self.move_temperature.setSpecialValueText('Off · best move')
        self.move_temperature.setValue(controller.config.move_temperature)
        self.move_temperature.setAccessibleName('Computer move temperature')
        self.move_temperature.setToolTip('0 chooses the best move. 1 samples from the opponent’s reported preferences. Higher values spread choices more evenly. Applies immediately before your first move; otherwise on New game.')
        side.addWidget(self.move_temperature)
        self.variety_label = self.label('', 'muted')
        self.variety_label.setWordWrap(True)
        side.addWidget(self.variety_label)
        self.move_temperature.valueChanged.connect(self.configure_game)
        side.addWidget(self.label('LOCAL MODEL', 'eyebrow'))
        self.model = QLabel()
        self.model.setWordWrap(True)
        side.addWidget(self.model)
        self.tactics_label = self.label('', 'muted')
        self.tactics_label.setWordWrap(True)
        side.addWidget(self.tactics_label)
        self.strategy_label = QLabel()
        self.strategy_label.setWordWrap(True)
        side.addWidget(self.strategy_label)
        self.rule_controls = QWidget()
        rule_layout = QVBoxLayout(self.rule_controls)
        rule_layout.setContentsMargins(0, 0, 0, 0)
        self.continue_rules_button = QPushButton('Continue current learning')
        self.basic_rules_button = QPushButton('Start with basic rules')
        self.load_rules_button = QPushButton('Load rules…')
        self.save_rules_button = QPushButton('Save rules…')
        self.continue_rules_button.setToolTip('Keep this game’s current learning run and cancel any pending rule selection.')
        self.basic_rules_button.setToolTip('Start an isolated learning run with the game’s initial strategy.')
        self.load_rules_button.setToolTip('Start an isolated learning run from a portable rule file for this game.')
        self.save_rules_button.setToolTip('Export the displayed ordered rules, including changes for the next game.')
        self.continue_rules_button.clicked.connect(lambda: self.choose_rules('continue'))
        self.basic_rules_button.clicked.connect(lambda: self.choose_rules('basic'))
        self.load_rules_button.clicked.connect(self.load_rules)
        self.save_rules_button.clicked.connect(self.save_rules)
        for button in (self.continue_rules_button, self.basic_rules_button,
                       self.load_rules_button, self.save_rules_button):
            rule_layout.addWidget(button)
        side.addWidget(self.rule_controls)
        self.continue_button = QPushButton('Continue with previous strategy')
        self.continue_button.clicked.connect(controller.continue_coaching)
        side.addWidget(self.continue_button)
        self.retry_button = QPushButton('Retry')
        self.retry_button.clicked.connect(controller.retry)
        side.addWidget(self.retry_button)
        self.diag_button = QPushButton('Details ▾')
        self.diag_button.setCheckable(True)
        self.diag_button.setChecked(True)
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
        self.details_tabs = QTabWidget()
        self.details_tabs.setAccessibleName('Game details')
        self.details_tabs.setMinimumHeight(160)
        self.details_tabs.setMaximumHeight(320)
        self.explanation = QTextEdit()
        self.explanation.setReadOnly(True)
        self.explanation.setAccessibleName('How it works')
        self.diag = QTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setAccessibleName('Activity log')
        self.details_tabs.addTab(self.explanation, 'How it works')
        self.details_tabs.addTab(self.diag, 'Activity log')
        layout.addWidget(self.details_tabs)
        self._focus_explanation = True
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

    def populate_prompt_selector(self):
        c = self.controller
        self.prompt_selector.clear()
        self.unavailable_prompts = []
        for key, label in c.policy.prompt_choices:
            self.prompt_selector.addItem(label, key)
            try:
                c.config.validate_recognition_policy(key)
            except ValueError as exc:
                item = self.prompt_selector.model().item(self.prompt_selector.count() - 1)
                item.setEnabled(False)
                item.setToolTip(str(exc) + ' Set learning.model to an installed coach model.')
                self.unavailable_prompts.append(f'{label} unavailable: configure an Ollama coach.')
        self.prompt_selector.setCurrentIndex(self.prompt_selector.findData(c.config.prompt_variant))

    def configure_game(self):
        self.focus_explanation()
        try:
            self.controller.configure_unstarted_game(
                self.prompt_selector.currentData(), self.move_temperature.value())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot apply game settings', str(exc))
        self.refresh()

    def choose_rules(self, kind, path=None):
        try:
            self.controller.select_rules(kind, path, self.selector.currentData())
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, 'Cannot select rules', str(exc))
        self.focus_explanation()
        self.refresh()

    def load_rules(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load rules', '', 'Rule files (*.json)')
        if path:
            self.choose_rules('file', Path(path))

    def save_rules(self):
        game_id = self.selector.currentData()
        path, _ = QFileDialog.getSaveFileName(self, 'Save rules', f'{game_id}-rules.json', 'Rule files (*.json)')
        if path:
            try:
                self.controller.export_rules(Path(path), game_id)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, 'Cannot save rules', str(exc))

    def configure_selected_game(self):
        self.focus_explanation()
        c = self.controller
        if c.can_configure_game and self.selector.currentData() != c.game.id:
            self.canvas.current = []
            c.select_game(GAMES[self.selector.currentData()])
        self.refresh()

    def new_game(self):
        if self.controller.coaching_failed or (self.controller.busy and self.controller.inflight and self.controller.inflight.purpose in ('coach', 'continue', 'shutdown')):
            return
        self.focus_explanation()
        self.canvas.current = []
        self.controller.config = replace(self.controller.config, prompt_variant=self.prompt_selector.currentData(),
                                         move_temperature=self.move_temperature.value())
        self.controller.select_game(GAMES[self.selector.currentData()], alternate_starter=True)
        if not self.controller.ready and not self.controller.busy:
            self.controller.start()

    def toggle_diagnostics(self, checked):
        self.details_tabs.setVisible(checked)
        self.diag_button.setText('Details ▾' if checked else 'Details ▸')

    def focus_explanation(self):
        self._focus_explanation = True
        self.diag_button.setChecked(True)
        self.details_tabs.setCurrentWidget(self.explanation)

    @staticmethod
    def update_details_text(editor, content, reset_scroll=False):
        scrollbar = editor.verticalScrollBar()
        position = scrollbar.value()
        if editor.toPlainText() != content:
            editor.setPlainText(content)
            scrollbar.setValue(0 if reset_scroll else min(position, scrollbar.maximum()))
        elif reset_scroll:
            scrollbar.setValue(0)

    def refresh(self):
        c = self.controller
        if self.prompt_game_id != c.game.id:
            self.prompt_selector.blockSignals(True)
            self.move_temperature.blockSignals(True)
            self.populate_prompt_selector()
            self.move_temperature.setValue(c.config.move_temperature)
            self.prompt_selector.blockSignals(False)
            self.move_temperature.blockSignals(False)
            self.prompt_game_id = c.game.id
        self.game_label.setText(c.game.name.upper())
        self.players_label.setText(f'YOU   {c.game.human_player}     /     COMPUTER   {c.game.computer_player}')
        self.canvas.setAccessibleName(f'{c.game.name} drawing board. {c.instruction}')
        details = c.policy.details(c.state)
        self.game_details.setVisible(bool(details))
        self.game_details.setText(details)
        phases = {'loading': 'Warming up', 'human': 'Your turn', 'recognising': 'Reading your ink',
                  'computer': c.config.opponent_name + '’s turn', 'coaching': 'Studying games', 'over': 'Game complete', 'error': 'Needs attention'}
        self.phase_label.setText(c.message.rstrip('…') if c.phase == 'coaching' else phases[c.phase])
        starter = getattr(c.state, 'starting_player', None)
        self.starter_label.setText(f'You started · {c.game.human_player}' if starter == c.game.human_player else f'{c.config.opponent_name} started · {c.game.computer_player}' if starter == c.game.computer_player else '')
        current_prompt = dict(c.policy.prompt_choices).get(c.config.prompt_variant, c.config.prompt_variant)
        pending = self.prompt_selector.currentData() != c.config.prompt_variant
        self.prompt_label.setText(f'Playing: {current_prompt}. ' + (
            'Click New game to apply selection.' if pending else
            'Choose a mode before your first move.' if c.can_configure_game else 'Selection applies to new games.'))
        self.prompt_availability.setText('\n'.join(self.unavailable_prompts))
        self.prompt_availability.setVisible(bool(self.unavailable_prompts))
        supports_prompts = bool(c.policy.prompt_choices)
        self.prompt_selector.setEnabled(supports_prompts)
        for widget in (self.prompt_heading, self.prompt_selector, self.prompt_label):
            widget.setVisible(supports_prompts)
        pending_game = self.selector.currentData() != c.game.id
        self.selector.setToolTip('Click New game to switch games.' if pending_game else 'Choose a game before your first move; otherwise click New game.')
        temperature = c.config.move_temperature
        self.move_temperature.setEnabled(c.config.opponent_settings.protocol != 'chat')
        pending_variety = self.move_temperature.value() != temperature
        variety = 'best move' if temperature == 0 else f'temperature {temperature:g}'
        self.variety_label.setText(f'Playing: {variety}. ' + (
            'Click New game to apply selection.' if pending_variety else '0 picks best; higher adds variety.'))
        tactics = c.policy.tactics_caption(c.config)
        self.tactics_label.setText(tactics)
        self.tactics_label.setVisible(bool(tactics))
        self.message.setText(c.message)
        self.message.setMinimumHeight(self.message.sizeHint().height())
        self.last.setText(c.last_move)
        self.last.setMinimumHeight(self.last.sizeHint().height())
        status = 'Working' if c.busy else ('Attention needed' if c.phase == 'error' else ('Ready' if c.ready else 'Connecting'))
        recognition_name = c.config.recognition_settings.model
        if c.config.recognition_fallback:
            if (c.busy and c.inflight and c.inflight.purpose == 'recognition'
                    and c.events and c.events[-1].get('fallback')):
                recognition_name = c.config.recognition_fallback.model + ' · fallback active'
            else:
                recognition_name += (f' · fallback {c.config.recognition_fallback.model}'
                                     f' after {c.config.fallback_after_rejections} rejections')
        self.model.setText(f'Recognition: {recognition_name}\nOpponent: {c.config.opponent_settings.model} · {status}')
        self.model.setMinimumHeight(self.model.sizeHint().height())
        self.new_button.setEnabled(not c.coaching_failed and not (c.busy and c.inflight and c.inflight.purpose in ('coach', 'continue', 'shutdown')))
        self.continue_button.setVisible(c.coaching_failed)
        self.continue_button.setEnabled(not c.busy)
        selected_game_id = self.selector.currentData()
        rule_mode = (self.prompt_selector.currentData() if selected_game_id == c.game.id else
                     c.game_preferences.get(selected_game_id, (GAMES[selected_game_id].session_policy.default_prompt, 0))[0])
        show_rules = rule_mode == 'coached_quoted'
        self.rule_controls.setVisible(show_rules)
        self.strategy_label.setVisible(show_rules)
        rules_text = ''
        if show_rules:
            try:
                snapshot = c.rule_snapshot(selected_game_id)
                next_game = (snapshot['pending'] or selected_game_id != c.game.id or not c.coached
                             or snapshot['revision'] != c.strategy_revision)
                rules_text = (f'Rules: {snapshot["origin"]}\n'
                              f'{"PREVIEW — applies on New game" if next_game else "Current learning"}'
                              f' · revision {snapshot["revision"]}\n'
                              + '\n'.join(f'{i}. {rule}' for i, rule in enumerate(snapshot['strategy'], 1)))
            except (OSError, ValueError, KeyError) as exc:
                rules_text = f'Cannot read rules: {exc}'
            self.strategy_label.setText(rules_text)
        rule_change_allowed = self.new_button.isEnabled() and not c.stopping
        for button in (self.continue_rules_button, self.basic_rules_button, self.load_rules_button):
            button.setEnabled(rule_change_allowed)
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
            self.input_hint.setText('Drawing is disabled until the recognition service is ready at ' + c.config.recognition_settings.endpoint)
        else:
            self.input_hint.setText('Drawing is paused. ' + c.message)
        log_info = f'\nApplication log: {c.log_path}' if getattr(c, 'log_path', None) else ''
        coach_info = c.coach_diagnostics or (json.dumps(c.coach_request, ensure_ascii=False, indent=2) if c.coach_request else '')
        diagnosis = '\n\nCOACH DIAGNOSIS\n' + c.coach_diagnosis if c.coach_diagnosis else ''
        fallback = (f'Fallback: {c.config.recognition_fallback.model} · after '
                    f'{c.config.fallback_after_rejections} rejections\n') if c.config.recognition_fallback else ''
        roles = (f'Recognition: {c.config.recognition_settings.model} · {c.config.recognition_settings.backend}\n'
                 + fallback + f'Opponent: {c.config.opponent_settings.model} · {c.config.opponent_settings.backend}\n\n')
        summary = roles + c.diagnostics + diagnosis + ('\n\nCOACHING\n' + coach_info if coach_info else '') + ('\n' + c.storage_error if c.storage_error else '') + log_info
        selected_game_id = self.selector.currentData()
        if selected_game_id != c.game.id:
            explained_game = GAMES[selected_game_id]
            explained_mode, explained_temperature = c.game_preferences.get(
                selected_game_id, (explained_game.session_policy.default_prompt, 0))
            status = f'PREVIEW — applies on New game; currently playing {c.game.name} / {current_prompt}'
        else:
            explained_game = c.game
            explained_mode = self.prompt_selector.currentData() or c.config.prompt_variant
            explained_temperature = self.move_temperature.value()
            status = (f'PREVIEW — applies on New game; currently playing {current_prompt}'
                      if pending or pending_variety or c.game.id in c.pending_rules else 'ACTIVE')
        explanation = format_game_explanation(
            explained_game, c.config, explained_mode, explained_temperature, status)
        if show_rules:
            explanation += '\n\nORDERED RULES\n' + rules_text
        activity = (summary + '\n\nQUESTIONS ASKED (NEWEST FIRST)\n' + format_question_history(c.events))
        self.update_details_text(self.explanation, explanation, self._focus_explanation)
        self.update_details_text(self.diag, activity)
        self._focus_explanation = False

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
            logger.info('Window close accepted; shutdown complete')
            event.accept()
            self.closed.emit()
            return
        if not hasattr(self, 'close_started'):
            self.close_started = time.monotonic()
            self.close_last_warning = self.close_started
            logger.info('Window close requested session=%s phase=%s busy=%s inflight=%s',
                        c.session_id, c.phase, c.busy, c.inflight)
        c.active = None
        c.stopping = True
        self.hide()
        if not c.busy:
            c.begin_shutdown()
        if c.shutdown_done:
            logger.info('Window close accepted; shutdown complete')
            event.accept()
            self.closed.emit()
            return
        if not hasattr(self, 'close_timer'):
            self.close_timer = QTimer(self)
            self.close_timer.timeout.connect(self.finish_close)
            self.close_timer.start(100)
        event.ignore()

    def finish_close(self):
        c = self.controller
        now = time.monotonic()
        if not c.shutdown_done and now - self.close_last_warning >= 30:
            logger.warning('Window hidden; shutdown still pending seconds=%.1f busy=%s inflight=%s',
                           now - self.close_started, c.busy, c.inflight)
            self.close_last_warning = now
        if not c.busy:
            c.begin_shutdown()
        if c.shutdown_done:
            self.close_timer.stop()
            self.close()
