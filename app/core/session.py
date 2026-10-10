from dataclasses import asdict, replace
from pathlib import Path
import uuid
import logging
import json
import random
from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal
from app.core.contracts import Stroke, Ticket
from app.inference.jobs import Job
from app.storage.session_store import SessionStore, session_record
from app.ui.rendering import observation_png
from app.storage.coached import CoachedStore, validate_strategy
from app.inference.coach import diagnose_then_coach, shared_coach
from app.config import validate_move_temperature
from app.core.move_sampling import select_move


logger = logging.getLogger(__name__)


class SessionController(QObject):
    changed = pyqtSignal()
    progress = pyqtSignal(str)
    diagnosis_ready = pyqtSignal(object, object)

    def __init__(self, game, client, config, parent=None):
        super().__init__(parent)
        self.game, self.client, self.config = game, client, config
        self.move_rng = random.Random()
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
        self.learning_store = CoachedStore(config.learning_directory)
        self.service_manager = None
        self.coaching_failed = False
        self.coach_diagnostics = ''
        self.coach_diagnosis = ''
        self.coach_request = None
        self.shutdown_done = False
        self.progress.connect(self._progress)
        self.diagnosis_ready.connect(self._diagnosis_ready)
        self.new_game()

    @property
    def editable(self):
        return self.phase == 'human' and not self.busy

    @property
    def coached(self):
        return self.config.prompt_variant == 'coached_quoted'

    @property
    def can_configure_game(self):
        return (not self.stopping and not self.coaching_failed and not self.source_session_id
                and self.game.revision(self.state) == 0
                and self.phase in ('loading', 'human', 'error')
                and (not self.busy or (self.inflight and self.inflight.purpose == 'startup')))

    def configure_unstarted_game(self, prompt_variant, move_temperature):
        """Apply choices before play without resetting ink, starter or warm-up."""
        if not self.can_configure_game:
            return False
        if prompt_variant not in ('legacy', 'quoted', 'coached_quoted'):
            raise ValueError('Move prompt must be legacy, quoted or coached_quoted.')
        temperature = validate_move_temperature(move_temperature)
        if prompt_variant != self.config.prompt_variant:
            ledger = self.learning_store.ledger() if prompt_variant == 'coached_quoted' else {'revision': 0, 'strategy': []}
            self.strategy, self.strategy_revision = ledger['strategy'].copy(), ledger['revision']
            self.next_strategy = ledger
        self.config = replace(self.config, prompt_variant=prompt_variant, move_temperature=temperature)
        self._publish()
        return True

    def _progress(self, message):
        self.message = message
        self.changed.emit()

    def _diagnosis_ready(self, ticket, analysis):
        if ticket != self.inflight:
            return
        self.coach_diagnosis = analysis['summary']
        self._publish()

    def new_game(self, alternate_starter=False):
        if self.coaching_failed or (self.busy and self.inflight and self.inflight.purpose in ('coach', 'continue', 'shutdown')):
            return
        ledger = self.learning_store.ledger() if self.coached else {'revision': 0, 'strategy': []}
        self.strategy, self.strategy_revision = ledger['strategy'].copy(), ledger['revision']
        self.next_strategy = ledger
        self.coach_diagnostics = ''
        self.coach_diagnosis = ''
        self.coach_request = None
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
        prompt_variant = record.get('prompt_variant', 'legacy')
        if prompt_variant not in ('legacy', 'quoted', 'coached_quoted'):
            raise ValueError('Saved move prompt must be legacy, quoted or coached_quoted.')
        temperature = validate_move_temperature(record.get('move_temperature', 0))
        self.state = self.game.decode_state(record['state'])
        self.config = replace(self.config, prompt_variant=prompt_variant, move_temperature=temperature)
        if self.coached:
            metadata = record.get('coaching', {})
            self.strategy = validate_strategy(metadata.get('strategy'))
            self.strategy_revision = metadata['revision']
            self.session_id = str(uuid.UUID(record['session_id']))
            self.next_strategy = self.learning_store.ledger()
            self.coach_diagnosis = metadata.get('diagnosis', '')
            self.coach_diagnostics = metadata.get('diagnostics', '')
            if not self.coach_diagnostics:
                revision = next((r for r in self.next_strategy['revisions']
                                 if r['trigger_game_id'] == self.session_id), None)
                if revision:
                    self.coach_diagnostics = json.dumps(revision, ensure_ascii=False, indent=2)
                    self.coach_diagnosis = (revision['response'].get('diagnosis') or {}).get('summary', '')
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
        if self.coaching_failed and not self.busy:
            self._coach()
            return
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
        state = self.game.initial_state() if purpose == 'startup' else self.state
        render_purpose = 'decision' if purpose == 'decision' else 'recognition'
        decision_options = {'opening_suggestion': self.config.opening_suggestion} if getattr(self.game, 'supports_opening_suggestion', False) else {}
        if getattr(self.game, 'supports_prompt_variants', False):
            decision_options['prompt_variant'] = self.config.prompt_variant
            if self.coached:
                decision_options['strategy'] = self.strategy.copy()
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
        if purpose == 'decision':
            event['move_temperature'] = self.config.move_temperature
        if purpose == 'decision' and getattr(self.game, 'supports_prompt_variants', False):
            event['prompt_variant'] = self.config.prompt_variant
            if self.config.prompt_variant in ('quoted', 'coached_quoted'):
                from app.games.tic_tac_toe.prompting import VERSION
                event['prompt_version'] = VERSION + ':' + self.config.prompt_variant
                if self.coached:
                    event['strategy_revision'] = self.strategy_revision
        try:
            event['image'] = self.store.image(ticket, png)
        except OSError as exc:
            self.storage_error = f'Could not save observation: {exc}'
        self.events.append(event)
        method = self.client.warmup if purpose == 'startup' else self.client.decide
        def operation():
            if purpose == 'startup' and self.service_manager:
                self.service_manager.start()
            return method(request, png)
        self.job = Job(ticket, operation)
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
                proposed, action, distribution = select_move(
                    self.game, self.state, reply, event['move_temperature'], self.move_rng)
                event['model_proposed_action'] = proposed
                selected = action
                if distribution:
                    event['move_sampling'] = {'temperature': event['move_temperature'],
                                              'probabilities': distribution, 'selected_action': selected}
                    self.diagnostics += (f'\nMove sampling · temperature {event["move_temperature"]:g}: '
                                         f'best {proposed}; sampled {selected} '
                                         f'({distribution[selected]:.1%} sampling probability).')
                    logger.info('move sampled temperature=%g model_best=%s selected=%s probability=%.4f',
                                event['move_temperature'], proposed, selected, distribution[selected])
                if self.config.tactical_guard and not self.coached and hasattr(self.game, 'tactical_choice'):
                    action, correction = self.game.tactical_choice(self.state, selected)
                    if correction:
                        event['tactical_correction'] = {'reason': correction, 'proposed': selected, 'committed': action}
                        self.diagnostics += f'\nTactical rule: {correction}; selected {selected}, committed {action}.'
                        logger.warning('tactical correction reason=%s proposed=%s committed=%s', correction, selected, action)
                self.state = self.game.apply_action(self.state, action)
                self.decision_attempt = 0
            event['accepted_action'] = action
            description = next(a["description"] for a in event["offered_actions"] if a["id"] == action)
            self.last_move = f'{event["player"]} · {description}'
            if event.get('tactical_correction'):
                self.last_move += ' · tactical rule'
            elif event.get('move_sampling'):
                self.last_move += ' · sampled'
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
            if self.coached and result.winner == self.game.human_player and not self.learning_store.updated(self.session_id):
                self._coach()
        elif self.game.current_player(self.state) == self.game.computer_player:
            self._launch('decision')
        else:
            self.phase, self.message = 'human', self.game.instruction
            self._publish()

    def _warmup_ready(self):
        if self.service_manager:
            self.service_manager.recover()
        state = self.game.initial_state()
        request = self.game.recognition_request(state, ())
        return self.client.warmup(request, observation_png(self.game.render(state, (), 'recognition'), self.config.observation_size))

    def _coach(self):
        if self.busy or self.stopping:
            return
        self.busy = True
        self.coaching_failed = False
        self.phase, self.message = 'coaching', 'Studying games…'
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), 'coach')
        self.inflight = ticket
        self.coach_request = None
        self.coach_diagnosis = ''
        self.coach_diagnostics = ''

        def operation():
            from app.games.tic_tac_toe.coaching import loss_context
            request = loss_context(self.learning_store.request(ticket.session_id))
            self.coach_request = request
            record = lambda stage: self.learning_store.attempt(ticket.session_id, stage)
            def pipeline(invoke):
                return diagnose_then_coach(
                    request, invoke, record, 7500 if self.config.coach_backend == 'ollama' else 9000,
                    on_diagnosis=lambda analysis: self.diagnosis_ready.emit(ticket, analysis))
            if self.config.coach_backend == 'ollama':
                if not self.service_manager:
                    raise RuntimeError('Ollama coaching requires managed inference.')
                response = self.service_manager.ollama(pipeline)
            else:
                def invoke(payload):
                    self.progress.emit('Diagnosing loss…' if payload['task'] == 'diagnose'
                                       else 'Studying games…' if payload['task'] == 'summarize' else 'Updating strategy…')
                    return shared_coach(self.client, payload)
                response = pipeline(invoke)
            self.progress.emit('Checking Imajev…')
            self._warmup_ready()
            return response

        self.job = Job(ticket, operation)
        self.job.signals.completed.connect(self._coach_completed)
        self._publish()
        self.pool.start(self.job)

    def _coach_completed(self, ticket, response, error, seconds):
        if ticket != self.inflight:
            return
        self.busy, self.inflight, self.job = False, None, None
        if response and response.get('diagnosis'):
            self.coach_diagnosis = response['diagnosis']['summary']
        if not error:
            try:
                self.next_strategy = self.learning_store.commit(self.coach_request, response)
            except (OSError, ValueError) as exc:
                error = str(exc)
        details = {'request': self.coach_request, 'response': response, 'error': error, 'seconds': seconds}
        try:
            self.learning_store.attempt(ticket.session_id, details)
        except OSError as exc:
            self.storage_error = str(exc)
        self.coach_diagnostics = json.dumps(details, ensure_ascii=False, indent=2)
        self.coaching_failed = bool(error)
        self.ready = not error
        self.phase = 'over'
        self.message = ('You won! Coaching failed: ' + error + '. Retry coaching or continue with previous rules.' if error
                        else 'You won! Strategy updated for the next game.')
        self._publish()

    def continue_coaching(self):
        if self.busy or not self.coaching_failed or self.stopping:
            return
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), 'continue')
        self.busy, self.inflight = True, ticket
        self.message = 'Checking Imajev before continuing…'
        self.job = Job(ticket, self._warmup_ready)
        self.job.signals.completed.connect(self._continue_completed)
        self._publish()
        self.pool.start(self.job)

    def _continue_completed(self, ticket, reply, error, seconds):
        if ticket != self.inflight:
            return
        self.busy, self.inflight, self.job = False, None, None
        self.coaching_failed = bool(error)
        self.ready = not error
        self.message = str(error) if error else 'You won! Previous strategy retained. Ready for a new game.'
        self._publish()

    def begin_shutdown(self):
        if self.busy or self.shutdown_done:
            return
        self.stopping = True
        if not self.service_manager:
            self.shutdown_done = True
            return
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), 'shutdown')
        self.busy, self.inflight = True, ticket
        self.job = Job(ticket, self.service_manager.shutdown)
        self.job.signals.completed.connect(self._shutdown_completed)
        self.pool.start(self.job)

    def _shutdown_completed(self, ticket, reply, error, seconds):
        self.busy, self.inflight, self.job = False, None, None
        self.shutdown_done = True
        if error:
            logger.error('Service shutdown: %s', error)

    def _error(self, purpose, message):
        logger.warning('inference error purpose=%s: %s', purpose, message)
        self.phase, self.retry_purpose, self.message = 'error', purpose, message
        self._publish()

    def record(self):
        record = session_record(self.session_id, self.game, self.state, self.pending, self.events, self.config)
        if self.coached:
            record['tactical_guard'] = False
            record['opening_suggestion'] = False
            record['coaching'] = {'version': 1, 'strategy': self.strategy.copy(), 'revision': self.strategy_revision}
            if self.coach_diagnosis:
                record['coaching']['diagnosis'] = self.coach_diagnosis
            if self.coach_diagnostics:
                record['coaching']['diagnostics'] = self.coach_diagnostics
        if self.source_session_id:
            record['source_session_id'] = self.source_session_id
        return record

    def _publish(self):
        try:
            record = self.record()
            self.store.write(self.session_id, record)
            if self.coached:
                self.learning_store.save(record, self.game, self.state)
        except (OSError, ValueError) as exc:
            self.storage_error = f'Could not save session: {exc}'
        self.changed.emit()
