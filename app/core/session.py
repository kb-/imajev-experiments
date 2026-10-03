from dataclasses import asdict
from pathlib import Path
import uuid
import logging
import json
from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal
from app.core.contracts import Stroke, Ticket
from app.inference.jobs import Job
from app.storage.session_store import SessionStore, session_record
from app.ui.rendering import observation_png


logger = logging.getLogger(__name__)


class SessionController(QObject):
    changed = pyqtSignal()

    def __init__(self, game, client, config, parent=None):
        super().__init__(parent)
        self.game, self.client, self.config = game, client, config
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.store = SessionStore(config.directory, config.save_sessions)
        self.busy = False
        self.inflight = None
        self.stopping = False
        self.ready = False
        self.active = None
        self.job = None
        self.retry_purpose = None
        self.decision_attempt = 0
        self.source_session_id = None
        self.last_move = 'No moves yet'
        self.events = []
        self.diagnostics = ''
        self.storage_error = ''
        self.starting_players = {}
        self.new_game()

    @property
    def editable(self):
        return self.phase == 'human' and not self.busy

    def new_game(self, alternate_starter=False):
        self.active = None  # Invalidates every old response, even when worker cannot cancel GPU work.
        self.session_id = str(uuid.uuid4())
        if hasattr(self.game, 'initial_state_for_player'):
            previous = self.starting_players.get(self.game.id, self.game.computer_player)
            starter = (self.game.computer_player if previous == self.game.human_player else self.game.human_player) if alternate_starter else self.game.human_player
            self.starting_players[self.game.id] = starter
            self.state = self.game.initial_state_for_player(starter)
        else:
            self.state = self.game.initial_state()
        self.pending: tuple[Stroke, ...] = ()
        self.events = []
        self.retry_purpose = None
        self.decision_attempt = 0
        self.source_session_id = None
        self.last_move = 'No moves yet'
        self.diagnostics = ''
        self.phase = 'loading' if self.busy or not self.ready else 'human'
        self.message = 'Waiting for the current local request…' if self.busy else (self.game.instruction if self.ready else 'Connecting to the local image service…')
        self._publish()
        if self.ready and not self.busy:
            self._after_move()

    def restore(self, path: Path):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('version') != 1 or record.get('state', {}).get('game') != self.game.id:
            raise ValueError('This session record does not match the selected game.')
        self.state = self.game.decode_state(record['state'])
        if hasattr(self.state, 'starting_player'):
            self.starting_players[self.game.id] = self.state.starting_player
        self.pending = tuple(Stroke(tuple(tuple(point) for point in stroke['points']), stroke['width'], stroke['color'])
                             for stroke in record.get('pending_ink', []))
        self.events = [dict(event) for event in record.get('events', [])]
        for event in self.events:
            if event.get('image'):
                event['image'] = str((path.parent / event['image']).resolve())
        self.source_session_id = record.get('session_id')
        for event in reversed(self.events):
            action = event.get('accepted_action')
            if action:
                description = next((item['description'] for item in event.get('offered_actions', []) if item['id'] == action), action)
                self.last_move = f"{event.get('player', '')} · {description}"
                break
        self.decision_attempt = sum(event.get('ticket', {}).get('purpose') == 'decision'
                                    and event.get('ticket', {}).get('state_revision') == self.game.revision(self.state)
                                    and bool(event.get('rejection')) for event in self.events)
        self.phase = 'loading'
        self.message = 'Restoring saved game and warming up Imajev…'
        self._publish()

    def start(self):
        if self.busy or self.stopping:
            return
        self._launch('startup')

    def set_pending(self, strokes):
        if self.editable:
            self.pending = tuple(strokes)
            logger.debug('pending ink: strokes=%d points=%d', len(self.pending), sum(len(s.points) for s in self.pending))
            self.changed.emit()

    def undo(self):
        if self.editable and self.pending:
            self.pending = self.pending[:-1]
            self.message = self.game.instruction
            self._publish()

    def clear(self):
        if self.editable:
            self.pending = ()
            self.message = self.game.instruction
            self._publish()

    def submit(self):
        if self.editable and self.pending:
            self._launch('recognition')

    def retry(self):
        if self.phase == 'error' and not self.busy and self.retry_purpose:
            if self.retry_purpose == 'decision':
                self.decision_attempt += 1
            self._launch(self.retry_purpose)

    def _launch(self, purpose):
        if self.busy or self.stopping:
            return
        actions = self.game.legal_actions(self.state)
        if purpose == 'decision' and len(actions) <= 1:
            if not actions and self.game.outcome(self.state).kind == 'ongoing':
                self._error('decision', 'The game module returned no legal actions for an ongoing game.')
                return
            if actions:
                self.state = self.game.apply_action(self.state, actions[0].id)
                self.last_move = f'{self.game.computer_player} · {actions[0].description} (forced move)'
                self.events.append({'purpose': 'decision', 'forced_action': actions[0].id})
                self.diagnostics = 'Forced move · one legal action; no model request.'
            self._after_move()
            return
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), purpose)
        drawing = self.pending
        state = self.state
        render_purpose = 'decision' if purpose == 'decision' else 'recognition'
        decision_options = {'opening_suggestion': self.config.opening_suggestion} if getattr(self.game, 'supports_opening_suggestion', False) else {}
        request = self.game.decision_request(state, actions, **decision_options) if purpose == 'decision' else self.game.recognition_request(state, drawing)
        if purpose == 'decision' and self.decision_attempt and hasattr(self.game, 'retry_decision_request'):
            request = self.game.retry_decision_request(state, actions, self.decision_attempt, **decision_options)
        png = observation_png(self.game.render(state, drawing, render_purpose), self.config.observation_size)
        self.phase = {'recognition': 'recognising', 'decision': 'computer', 'startup': 'loading'}[purpose]
        self.message = {'recognition': 'Reading your move…', 'decision': 'Imajev is choosing…', 'startup': 'Connecting and warming up Imajev…'}[purpose]
        self.busy, self.active, self.inflight, self.retry_purpose = True, ticket, ticket, None
        logger.info('inference started purpose=%s request=%s revision=%s', purpose, ticket.request_id, ticket.state_revision)
        event = {'ticket': asdict(ticket), 'request': request, 'state': self.game.encode_state(state),
                 'drawing': [asdict(s) for s in drawing], 'prompt_version': getattr(self.game, 'prompt_version', '1'),
                 'player': self.game.current_player(state), 'offered_actions': [asdict(a) for a in actions],
                 'decision_attempt': self.decision_attempt if purpose == 'decision' else None}
        try:
            event['image'] = self.store.image(ticket, png)
        except OSError as exc:
            self.storage_error = f'Could not save observation: {exc}'
        self.events.append(event)
        method = self.client.warmup if purpose == 'startup' else self.client.decide
        self.job = Job(ticket, lambda: method(request, png))
        self.job.signals.completed.connect(self._completed)
        self._publish()
        self.pool.start(self.job)

    def _completed(self, ticket, reply, error, seconds):
        if ticket != self.inflight:
            return
        self.busy = False
        self.inflight = None
        self.job = None
        if self.stopping:
            return
        expected = {'startup': 'loading', 'recognition': 'recognising', 'decision': 'computer'}[ticket.purpose]
        if self.active != ticket or ticket.session_id != self.session_id or ticket.state_revision != self.game.revision(self.state) or self.phase != expected:
            # New game cannot start another request until this worker finishes.
            if self.phase == 'loading':
                if self.ready:
                    self._after_move()
                else:
                    self.start()
            return
        self.active = None
        logger.info('inference completed purpose=%s seconds=%.2f error=%s', ticket.purpose, seconds, error)
        event = self.events[-1]
        event.update(seconds=seconds, error=error, reply=dict(reply.raw) if reply else None)
        self.diagnostics = f'{ticket.purpose.title()} · {seconds:.2f} s\n'
        if reply:
            logger.debug('model answers request=%s model=%s answers=%s', ticket.request_id, reply.model, json.dumps(reply.raw.get('answers', {}), allow_nan=False))
            for question, answer in reply.answers.items():
                logger.info('model answer question=%s choice=%s probability=%.4f unknown=%.4f effective=%.4f abstained=%s',
                            question, answer.choice, answer.probabilities[answer.choice], answer.unknown_probability, answer.effective_probability, answer.abstained)
            score_name = 'preference' if ticket.purpose == 'decision' else 'effective probability'
            self.diagnostics += '\n'.join(f'{key}: {a.choice} · {score_name} {(a.probabilities[a.choice] if ticket.purpose == "decision" else a.effective_probability):.3f} · unknown {a.unknown_probability:.3f}' for key, a in reply.answers.items())
        if error:
            self._error(ticket.purpose, error)
            return
        if ticket.purpose == 'startup':
            self.ready = True
            if self.game.current_player(self.state) == self.game.computer_player or self.game.outcome(self.state).kind != 'ongoing':
                self._after_move()
            else:
                self.phase, self.message = 'human', self.game.instruction
                self._publish()
            return
        try:
            if ticket.purpose == 'recognition':
                action = self.game.decode_recognition(self.state, self.pending, reply, self.config.threshold)
                self.state = self.game.apply_action(self.state, action, self.pending)
                self.pending = ()
            else:
                proposed = self.game.decode_decision(self.state, reply)
                event['model_proposed_action'] = proposed
                action = proposed
                if self.config.tactical_guard and hasattr(self.game, 'tactical_choice'):
                    action, correction = self.game.tactical_choice(self.state, proposed)
                    if correction:
                        event['tactical_correction'] = {'reason': correction, 'proposed': proposed, 'committed': action}
                        self.diagnostics += f'\nTactical rule: {correction}; Imajev proposed {proposed}, committed {action}.'
                        logger.warning('tactical correction reason=%s proposed=%s committed=%s', correction, proposed, action)
                self.state = self.game.apply_action(self.state, action)
                self.decision_attempt = 0
            event['accepted_action'] = action
            description = next(a["description"] for a in event["offered_actions"] if a["id"] == action)
            self.last_move = f'{event["player"]} · {description}'
            if event.get('tactical_correction'):
                self.last_move += ' · tactical rule'
        except ValueError as exc:
            event['rejection'] = str(exc)
            logger.warning('move rejected request=%s purpose=%s reason=%s', ticket.request_id, ticket.purpose, exc)
            self.diagnostics += f'\nRejected: {exc}'
            if ticket.purpose == 'recognition':
                self.phase, self.message = 'human', str(exc)
                self._publish()
            else:
                self._error('decision', str(exc))
            return
        self._after_move()

    def _after_move(self):
        result = self.game.outcome(self.state)
        if result.kind != 'ongoing':
            self.phase = 'over'
            self.message = 'A draw. Nicely played.' if result.kind == 'draw' else ('You won!' if result.winner == self.game.human_player else 'Imajev wins. Try another game?')
            self._publish()
        elif self.game.current_player(self.state) == self.game.computer_player:
            self._launch('decision')
        else:
            self.phase, self.message = 'human', self.game.instruction
            self._publish()

    def _error(self, purpose, message):
        logger.warning('inference error purpose=%s: %s', purpose, message)
        self.phase, self.retry_purpose, self.message = 'error', purpose, message
        self._publish()

    def record(self):
        record = session_record(self.session_id, self.game, self.state, self.pending, self.events, self.config)
        if self.source_session_id:
            record['source_session_id'] = self.source_session_id
        return record

    def _publish(self):
        try:
            self.store.write(self.session_id, self.record())
        except (OSError, ValueError) as exc:
            self.storage_error = f'Could not save session: {exc}'
        self.changed.emit()
