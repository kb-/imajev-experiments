from dataclasses import asdict, replace
from pathlib import Path
import uuid
import logging
import json
import random
from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal
from app.core.contracts import Stroke, Ticket, MoveResult, RecognitionResult
from app.inference.opponents import create_opponent, metadata, role_metadata
from app.inference.recognition import create_recognizer, metadata as recognition_metadata
from app.inference.jobs import Job
from app.storage.session_store import SessionStore, session_record
from app.ui.rendering import observation_png
from app.storage.coached import CoachedStore
from app.storage.strategy import validate_strategy
from app.storage.atomic import atomic_json
from app.storage.rules import rule_selection, save_rules
from app.inference.coach import diagnose_then_coach, shared_coach, prepared_transport
from app.config import validate_move_temperature
from app.core.move_sampling import select_move
from app.core.results import (DECISION_RETRY_LIMIT, FORFEIT_MESSAGE, retry_progress,
                              retry_forfeit, session_outcome)


logger = logging.getLogger(__name__)


class SessionController(QObject):
    changed = pyqtSignal()
    progress = pyqtSignal(str)
    diagnosis_ready = pyqtSignal(object, object)
    request_ready = pyqtSignal(object, object, object)

    def __init__(self, game, client, config, parent=None, opponent=None, rules_source=None):
        super().__init__(parent)
        self.game, self.config = game, config
        config.validate_recognition_policy()
        self.recognizer = client if hasattr(client, 'recognize') else create_recognizer(config, client)
        self.fallback_recognizer = (create_recognizer(replace(config, recognition=config.recognition_fallback))
                                    if config.recognition_fallback else None)
        # The raw ImaJEV client is used only by its legacy coaching transport.
        self.client = getattr(self.recognizer, 'client', self.recognizer)
        self.opponent = opponent or create_opponent(config, self.client)
        self.policy = game.session_policy
        self.game_preferences = {}
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
        self.failed_recognition_retries = 0
        self.source_session_id = None
        self.last_move = 'No moves yet'
        self.events = []
        self.diagnostics = ''
        self.storage_error = ''
        self.starting_players = {}
        self.learning_contexts = {}
        self.pending_rules = {game.id: rules_source} if rules_source else {}
        self.learning_store = self._coaching_store()
        self.service_manager = None
        self.coaching_failed = False
        self.coach_diagnostics = ''
        self.coach_diagnosis = ''
        self.coach_request = None
        self.shutdown_done = False
        self.progress.connect(self._progress)
        self.diagnosis_ready.connect(self._diagnosis_ready)
        self.request_ready.connect(self._request_ready)
        self.new_game()

    def _coaching_store(self):
        policy = self.policy.coaching
        root = self.config.learning_directory
        context = self.learning_contexts.get(self.game.id, {})
        if context.get('run_id'):
            root = Path(root) / 'runs' / context['run_id']
        return CoachedStore(policy.storage_root(root), policy) if policy else None

    def select_rules(self, kind, path=None, game_id=None):
        """Select validated rules now, or queue them until a new coached game."""
        from app.core.registry import get_game
        game_id = game_id or self.game.id
        game = get_game(game_id)
        if self.stopping or self.coaching_failed or (self.busy and self.inflight
                and self.inflight.purpose in ('coach', 'continue', 'shutdown')):
            raise ValueError('Finish coaching before changing the learning run.')
        if kind == 'continue':
            self.pending_rules.pop(game_id, None)
        else:
            if kind not in ('basic', 'file') or (kind == 'file' and path is None):
                raise ValueError('Choose basic rules or a rule file.')
            selection = rule_selection(game, path if kind == 'file' else None, kind == 'basic')
            self.pending_rules[game_id] = selection
            if game_id == self.game.id and self.coached and self.can_configure_game:
                self._apply_pending_rules()
                ledger = self.learning_store.ledger()
                self.strategy, self.strategy_revision = ledger['strategy'].copy(), ledger['revision']
                self.next_strategy = ledger
        self._publish()

    def _apply_pending_rules(self):
        if not self.coached or self.game.id not in self.pending_rules:
            return
        selection = self.pending_rules[self.game.id]
        context = {'run_id': str(uuid.uuid4()), 'rule_origin': selection['origin']}
        policy = self.policy.coaching
        root = policy.storage_root(Path(self.config.learning_directory) / 'runs' / context['run_id'])
        store = CoachedStore(root, policy)
        ledger = store.ledger()
        ledger['strategy'] = validate_strategy(selection['strategy'])
        atomic_json(root / 'strategy.json', ledger)
        self.learning_contexts[self.game.id] = context
        self.learning_store = store
        del self.pending_rules[self.game.id]

    def rule_snapshot(self, game_id=None):
        """What the rules controls display and export, including next-game rules."""
        from app.core.registry import get_game
        game_id = game_id or self.game.id
        if game_id in self.pending_rules:
            return {**self.pending_rules[game_id], 'revision': 0, 'pending': True}
        context = self.learning_contexts.get(game_id, {})
        policy = get_game(game_id).session_policy.coaching
        root = Path(self.config.learning_directory)
        if context.get('run_id'):
            root = root / 'runs' / context['run_id']
        ledger = (self.next_strategy if game_id == self.game.id and self.coached
                  else CoachedStore(policy.storage_root(root), policy).ledger())
        return {'strategy': ledger['strategy'].copy(), 'revision': ledger['revision'],
                'origin': context.get('rule_origin', 'Current learning'), 'pending': False}

    def export_rules(self, path, game_id=None):
        game_id = game_id or self.game.id
        save_rules(path, game_id, self.rule_snapshot(game_id)['strategy'])

    @property
    def editable(self):
        return self.phase == 'human' and not self.busy

    @property
    def coached(self):
        return self.policy.coaching is not None and self.config.prompt_variant == 'coached_quoted'

    @property
    def instruction(self):
        return self.game.instruction_for(self.state)

    @property
    def outcome(self):
        return session_outcome(self.game, self.state, self.termination)

    @property
    def win_message(self):
        return FORFEIT_MESSAGE.replace('Imajev', self.config.opponent_name) if self.termination else 'You won!'

    def select_game(self, game, alternate_starter=False):
        """Switch via a reset; the active ticket still gates GPU work."""
        if self.coaching_failed or (self.busy and self.inflight and self.inflight.purpose in ('coach', 'continue', 'shutdown')):
            return False
        self.game_preferences[self.game.id] = (self.config.prompt_variant, self.config.move_temperature)
        self.game = game
        self.policy = game.session_policy
        prompt, temperature = self.game_preferences.get(game.id, (self.policy.default_prompt, 0))
        self.config = replace(self.config, game=game.id, prompt_variant=prompt, move_temperature=temperature)
        self.learning_store = self._coaching_store()
        self.new_game(alternate_starter=alternate_starter)
        return True

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
        self.policy.validate_prompt(prompt_variant)
        self.config.validate_recognition_policy(prompt_variant)
        temperature = validate_move_temperature(move_temperature)
        if self.config.opponent_settings.protocol == 'chat' and temperature:
            raise ValueError('Chat opponents supply no move distribution; use zero move variety.')
        if prompt_variant != self.config.prompt_variant:
            previous_config = self.config
            self.config = replace(self.config, prompt_variant=prompt_variant)
            try:
                self._apply_pending_rules()
                ledger = self.learning_store.ledger() if self.coached else {'revision': 0, 'strategy': []}
            except (OSError, ValueError):
                self.config = previous_config
                raise
            self.strategy, self.strategy_revision = ledger['strategy'].copy(), ledger['revision']
            self.next_strategy = ledger
        self.config = replace(self.config, prompt_variant=prompt_variant, move_temperature=temperature)
        self._publish()
        return True

    def _progress(self, message):
        self.message = self.win_message + ' ' + message if self.termination and self.phase == 'coaching' else message
        self.changed.emit()

    def _diagnosis_ready(self, ticket, analysis):
        if ticket != self.inflight:
            return
        self.coach_diagnosis = analysis['summary']
        self._publish()

    def _request_ready(self, ticket, request, evidence):
        if ticket != self.active or ticket != self.inflight or self.stopping:
            return
        self.events[-1]['request'] = request
        if evidence is not None:
            self.events[-1]['tactical_evidence'] = evidence
        self.message = self.config.opponent_name + ' is choosing…'
        self._publish()

    def new_game(self, alternate_starter=False):
        if self.coaching_failed or (self.busy and self.inflight and self.inflight.purpose in ('coach', 'continue', 'shutdown')):
            return
        self.config.validate_recognition_policy()
        self._apply_pending_rules()
        ledger = self.learning_store.ledger() if self.coached else {'revision': 0, 'strategy': []}
        self.strategy, self.strategy_revision = ledger['strategy'].copy(), ledger['revision']
        self.next_strategy = ledger
        self.coach_diagnostics = ''
        self.coach_diagnosis = ''
        self.coach_request = None
        self.active = None  # Invalidates every old response, even when worker cannot cancel GPU work.
        self.session_id = str(uuid.uuid4())
        previous = self.starting_players.get(self.game.id, self.game.computer_player)
        self.state = self.policy.initial_state(self.game, previous, alternate_starter)
        self.starting_players[self.game.id] = self.game.current_player(self.state)
        self.pending: tuple[Stroke, ...] = ()
        self.events = []
        self.retry_purpose = None
        self.decision_attempt = 0
        self.failed_decision_retries = 0
        self.failed_recognition_retries = 0
        self.termination = None
        self.source_session_id = None
        self.last_move = 'No moves yet'
        self.diagnostics = ''
        self.phase = 'loading' if self.busy or not self.ready else 'human'
        self.message = 'Waiting for the current local request…' if self.busy else (self.instruction if self.ready else 'Connecting to the local image service…')
        self._publish()
        if self.ready and not self.busy:
            self._after_move()

    def restore(self, path: Path):
        record = json.loads(path.read_text(encoding='utf-8'))
        if ('recognition_fallback' in record and record['recognition_fallback']
                != json.loads(json.dumps(self._fallback_metadata()))):
            raise ValueError('Saved recognition fallback differs from this configuration.')
        if record.get('inference') is not None and record['inference'] != json.loads(json.dumps(role_metadata(self.config))):
            raise ValueError('Saved inference roles differ from this configuration; select the matching --config.')
        if record.get('version') != 1 or record.get('state', {}).get('game') != self.game.id:
            raise ValueError('This session record does not match the selected game.')
        prompt_variant = self.policy.restore_prompt(record)
        self.config.validate_recognition_policy(prompt_variant)
        temperature = validate_move_temperature(record.get('move_temperature', 0))
        self.state = self.game.decode_state(record['state'])
        self.config = replace(self.config, prompt_variant=prompt_variant, move_temperature=temperature)
        if self.coached:
            metadata = record.get('coaching', {})
            context = {}
            if metadata.get('run_id') is not None:
                context = {'run_id': str(uuid.UUID(metadata['run_id'])),
                           'rule_origin': metadata.get('rule_origin', 'Imported rules')}
                if not isinstance(context['rule_origin'], str):
                    raise ValueError('Invalid saved rule origin.')
            self.learning_contexts[self.game.id] = context
            self.pending_rules.pop(self.game.id, None)
            self.learning_store = self._coaching_store()
            if context and not (self.learning_store.root / 'strategy.json').is_file():
                raise ValueError('The saved learning run is missing from the configured learning directory.')
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
        revision = self.game.revision(self.state)
        self.failed_recognition_retries = min(self.config.fallback_after_rejections, sum(
            1 for event in self.events
            if event.get('ticket', {}).get('purpose') == 'recognition'
            and event['ticket'].get('state_revision') == revision
            and event.get('rejection') and not event.get('fallback')))
        stored_recognition = record.get('recognition_retries')
        if stored_recognition is not None:
            if (not isinstance(stored_recognition, dict) or stored_recognition.get('version') != 1
                    or stored_recognition.get('state_revision') != revision
                    or type(stored_recognition.get('failed')) is not int
                    or not 0 <= stored_recognition['failed'] <= self.config.fallback_after_rejections):
                raise ValueError('Saved recognition retry progress is invalid.')
            self.failed_recognition_retries = stored_recognition['failed']
        self.failed_decision_retries, self.decision_attempt = retry_progress(
            self.events, revision)
        progress = record.get('decision_retries')
        expected_progress = {'version': 1, 'state_revision': self.game.revision(self.state),
                             'failed': self.failed_decision_retries}
        if progress is not None and (progress != expected_progress or not isinstance(progress, dict)
                                     or any(type(value) is not int for value in progress.values())):
            raise ValueError('Saved retry progress does not match decision events.')
        self.termination = record.get('termination')
        if self.termination is None and self.failed_decision_retries == DECISION_RETRY_LIMIT:
            self.termination = retry_forfeit(self.game, self.state)
        session_outcome(self.game, self.state, self.termination, self.events)
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
            self.failed_recognition_retries = 0
            self.message = self.instruction
            self._publish()

    def clear(self):
        if self.editable:
            self.pending = ()
            self.failed_recognition_retries = 0
            self.message = self.instruction
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
        if purpose == 'decision' and self.outcome.kind != 'ongoing':
            self._after_move()
            return
        actions = self.game.legal_actions(self.state)
        if purpose == 'decision' and len(actions) <= 1:
            if not actions and self.game.outcome(self.state).kind == 'ongoing':
                self._error('decision', 'The game module returned no legal actions for an ongoing game.')
                return
            if actions:
                before = self.game.encode_state(self.state)
                player = self.game.current_player(self.state)
                self.state = self.game.apply_action(self.state, actions[0].id)
                self.decision_attempt = self.failed_decision_retries = 0
                self.last_move = f'{self.game.computer_player} · {actions[0].description} (forced move)'
                self.events.append({'purpose': 'decision', 'forced_action': actions[0].id,
                                    'accepted_action': actions[0].id, 'player': player, 'state': before,
                                    'offered_actions': [asdict(a) for a in actions]})
                self.diagnostics = 'Forced move · one legal action; no model request.'
            self._after_move()
            return
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), purpose)
        fallback = (purpose == 'recognition' and self.fallback_recognizer is not None
                    and self.failed_recognition_retries >= self.config.fallback_after_rejections)
        recognizer = self.fallback_recognizer if fallback else self.recognizer
        recognition_threshold = self.config.fallback_threshold if fallback else self.config.threshold
        drawing = self.pending
        state = self.game.initial_state() if purpose == 'startup' else self.state
        render_purpose = 'decision' if purpose == 'decision' else 'recognition'
        prepare_in_worker = purpose == 'decision' and self.policy.prepare_decision_in_worker
        game, policy, config = self.game, self.policy, self.config
        strategy, attempt = self.strategy.copy(), self.decision_attempt
        request = (None if prepare_in_worker else
                   self.policy.decision(self.game, state, actions, self.config, self.strategy, self.decision_attempt)
                   if purpose == 'decision' else self.game.recognition_request(state, drawing))
        png = observation_png(self.game.render(state, drawing, render_purpose), self.config.observation_size)
        self.phase = {'recognition': 'recognising', 'decision': 'computer', 'startup': 'loading'}[purpose]
        self.message = {'recognition': 'Reading your move with ' + recognizer.settings.model + '…', 'decision': self.config.opponent_name + ' is choosing…', 'startup': 'Connecting and warming up ' + self.config.recognition_settings.model + '…'}[purpose]
        if prepare_in_worker:
            self.message = 'Checking move consequences…'
        self.busy, self.active, self.inflight, self.retry_purpose = True, ticket, ticket, None
        logger.info('inference started purpose=%s request=%s revision=%s', purpose, ticket.request_id, ticket.state_revision)
        event = {'ticket': asdict(ticket), 'request': request, 'state': self.game.encode_state(state),
                 'drawing': [asdict(s) for s in drawing], 'prompt_version': getattr(self.game, 'prompt_version', '1'),
                 'player': self.game.current_player(state), 'offered_actions': [asdict(a) for a in actions],
                 'decision_attempt': self.decision_attempt if purpose == 'decision' else None}
        if purpose == 'decision':
            event['move_temperature'] = self.config.move_temperature
            event['opponent'] = role_metadata(self.config)['opponent']
        if purpose == 'decision':
            event.update(self.policy.decision_metadata(self.game, self.config, self.strategy_revision))
        try:
            event['image'] = self.store.image(ticket, png)
        except OSError as exc:
            self.storage_error = f'Could not save observation: {exc}'
        if purpose != 'decision':
            role_config = replace(config, recognition=recognizer.settings, threshold=recognition_threshold)
            event['recognition'] = role_metadata(role_config)['recognition']
            event['recognition_threshold'] = recognition_threshold
            event['fallback'] = fallback
            event['failed_recognition_retries'] = self.failed_recognition_retries
        self.events.append(event)
        self.opponent.manager = self.service_manager
        recognizer.manager = self.service_manager
        method = self.opponent.choose_move if purpose == 'decision' else recognizer.warmup if purpose == 'startup' else recognizer.recognize
        def operation():
            prepared = request
            if prepare_in_worker:
                prepared, evidence = policy.prepare_decision(game, state, actions, config, strategy, attempt)
                if self.active != ticket or self.stopping:
                    raise RuntimeError('Decision preparation was superseded.')
                self.request_ready.emit(ticket, prepared, evidence)
            return method(prepared, png)
        self.job = Job(ticket, operation)
        self.job.signals.completed.connect(self._completed)
        self._publish()
        self.pool.start(self.job)

    def _completed(self, ticket, reply, error, seconds):
        if ticket != self.inflight:
            return
        raw_error = getattr(self.job, 'error_raw', None)
        self.busy = False
        self.inflight = None
        self.job = None
        if self.stopping:
            logger.info('Inflight request finished during shutdown purpose=%s seconds=%.2f error=%s',
                        ticket.purpose, seconds, error)
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
        event.update(seconds=seconds, error=error, reply=dict(reply.raw) if reply else raw_error)
        self.diagnostics = f'{ticket.purpose.title()} · {seconds:.2f} s\n'
        if isinstance(reply, MoveResult):
            event['move_result'] = metadata(reply)
            event['model_proposed_choice'] = reply.choice
            self.diagnostics += f'Model: {reply.model}\nChoice: {reply.choice}'
            if reply.probabilities is not None:
                self.diagnostics += f'\nPreference: {reply.probabilities.get(reply.choice)}'
            if reply.rejection:
                self.diagnostics += '\n' + reply.rejection
        elif isinstance(reply, RecognitionResult):
            event['recognition_result'] = recognition_metadata(reply)
            self.diagnostics += f'Model: {reply.model} · {reply.backend}\nScore: {reply.score_kind}\n'
            for name, answer in reply.answers.items():
                score = reply.score(name)
                logger.info('recognition answer question=%s choice=%s score_kind=%s score=%.4f unknown=%s abstained=%s',
                            name, answer.choice, reply.score_kind, score, answer.unknown_probability, answer.abstained)
                self.diagnostics += f'{name}: {answer.choice} · {score:.3f}'
                if answer.unknown_probability is not None:
                    self.diagnostics += f' · unknown {answer.unknown_probability:.3f}'
                if answer.confidence is not None:
                    self.diagnostics += f' · confidence {answer.confidence:.3f}'
                self.diagnostics += '\n'
        if reply and 'runtime_reused' in reply.raw:
            self.diagnostics += ('\nRuntime: ' + ('reused' if reply.raw['runtime_reused'] else 'loaded')
                                 + f" · PID {reply.raw.get('runtime_pid')}"
                                 + f" · activation {reply.raw.get('activation_seconds', 0):.3f} s"
                                 + f" · inference {reply.raw.get('inference_seconds', 0):.3f} s")
        if error:
            self._error(ticket.purpose, error)
            return
        if ticket.purpose == 'startup':
            self.ready = True
            if (self.fallback_recognizer and self.pending
                    and self.failed_recognition_retries >= self.config.fallback_after_rejections
                    and self.game.current_player(self.state) == self.game.human_player):
                self.phase, self.message = 'human', 'Resuming drawing recognition with ImaJEV…'
                self._publish()
                self._launch('recognition')
                return
            if self.game.current_player(self.state) == self.game.computer_player or self.outcome.kind != 'ongoing':
                self._after_move()
            else:
                self.phase, self.message = 'human', self.instruction
                self._publish()
            return
        try:
            if ticket.purpose == 'recognition':
                action = self.game.decode_recognition(self.state, self.pending, reply, event['recognition_threshold'])
                self.state = self.game.apply_action(self.state, action, self.pending)
                self.pending = ()
                self.failed_recognition_retries = 0
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
                action, correction = self.policy.correct_action(
                    self.game, self.state, selected, self.config,
                    event.get('tactical_evidence'), reply.probabilities)
                if correction:
                    event['tactical_correction'] = {'reason': correction, 'proposed': selected, 'committed': action}
                    self.diagnostics += f'\nTactical rule: {correction}; selected {selected}, committed {action}.'
                    logger.warning('tactical correction reason=%s proposed=%s committed=%s', correction, selected, action)
                self.state = self.game.apply_action(self.state, action)
                self.decision_attempt = self.failed_decision_retries = 0
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
                if not event['fallback'] and self.fallback_recognizer:
                    self.failed_recognition_retries = min(
                        self.config.fallback_after_rejections, self.failed_recognition_retries + 1)
                    event['failed_recognition_retries'] = self.failed_recognition_retries
                self.phase, self.message = 'human', str(exc)
                self._publish()
                if (not event['fallback'] and self.fallback_recognizer
                        and self.failed_recognition_retries == self.config.fallback_after_rejections):
                    logger.info('recognition fallback activated after %d rejections', self.failed_recognition_retries)
                    self._launch('recognition')
            else:
                if event['decision_attempt'] > 0:
                    self.failed_decision_retries += 1
                event['failed_decision_retries'] = self.failed_decision_retries
                if self.failed_decision_retries >= DECISION_RETRY_LIMIT:
                    self.termination = retry_forfeit(self.game, self.state)
                    self.retry_purpose = None
                    self.diagnostics += '\n' + FORFEIT_MESSAGE
                    logger.info('decision retry limit reached session=%s revision=%s',
                                self.session_id, self.game.revision(self.state))
                    self._after_move()
                    return
                self._error('decision', str(exc))
            return
        self._after_move()

    def _after_move(self):
        result = self.outcome
        if result.kind != 'ongoing':
            self.phase = 'over'
            self.retry_purpose = None
            self.message = 'A draw. Nicely played.' if result.kind == 'draw' else (self.win_message if result.winner == self.game.human_player else self.config.opponent_name + ' wins. Try another game?')
            self._publish()
            if self.coached and result.winner == self.game.human_player and not self.learning_store.updated(self.session_id):
                self._coach()
        elif self.game.current_player(self.state) == self.game.computer_player:
            self._launch('decision')
        else:
            self.phase, self.message = 'human', self.instruction
            self._publish()

    def _warmup_ready(self):
        if self.service_manager:
            self.service_manager.recover(self.config.recognition_settings)
        state = self.game.initial_state()
        request = self.game.recognition_request(state, ())
        self.recognizer.manager = self.service_manager
        return self.recognizer.warmup(request, observation_png(self.game.render(state, (), 'recognition'), self.config.observation_size))

    def _coach(self):
        if self.busy or self.stopping:
            return
        self.busy = True
        self.coaching_failed = False
        self.phase, self.message = 'coaching', 'Studying games…'
        if self.termination:
            self.message = self.win_message + ' ' + self.message
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), 'coach')
        self.inflight = ticket
        self.coach_request = None
        self.coach_diagnosis = ''
        self.coach_diagnostics = ''

        def operation():
            request = self.policy.coaching.context(self.learning_store.request(ticket.session_id))
            self.coach_request = request
            record = lambda stage: self.learning_store.attempt(ticket.session_id, stage)
            def pipeline(invoke):
                return diagnose_then_coach(
                    request, invoke, record, 18000 if self.config.coach_backend == 'ollama' else 14000,
                    on_diagnosis=lambda analysis: self.diagnosis_ready.emit(ticket, analysis))
            if self.config.coach_backend == 'ollama':
                if not self.service_manager:
                    raise RuntimeError('Ollama coaching requires managed inference.')
                response = self.service_manager.ollama(
                    lambda transport: pipeline(prepared_transport(transport, self.progress.emit)))
            else:
                def invoke(payload):
                    self.progress.emit('Diagnosing loss…' if payload['task'] == 'diagnose'
                                       else 'Studying games…' if payload['task'] == 'summarize' else 'Updating strategy…')
                    return shared_coach(self.client, payload)
                response = pipeline(invoke)
            self.progress.emit('Checking ' + self.config.recognition_settings.model + '…')
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
        self.message = (self.win_message + ' Coaching failed: ' + error + '. Retry coaching or continue with previous rules.' if error
                        else self.win_message + ' Strategy updated for the next game.')
        self._publish()

    def continue_coaching(self):
        if self.busy or not self.coaching_failed or self.stopping:
            return
        ticket = Ticket(self.session_id, self.game.revision(self.state), str(uuid.uuid4()), 'continue')
        self.busy, self.inflight = True, ticket
        self.message = 'Checking ' + self.config.recognition_settings.model + ' before continuing…'
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
        self.message = str(error) if error else self.win_message + ' Previous strategy retained. Ready for a new game.'
        self._publish()

    def begin_shutdown(self):
        if self.busy or self.shutdown_done:
            return
        self.stopping = True
        logger.info('Service cleanup starting session=%s managed=%s', self.session_id,
                    self.service_manager is not None)
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
        self.shutdown_error = error
        logger.info('Service cleanup finished seconds=%.2f error=%s', seconds, error)
        if error:
            logger.error('Service shutdown: %s', error)

    def _error(self, purpose, message):
        logger.warning('inference error purpose=%s: %s', purpose, message)
        self.phase, self.retry_purpose, self.message = 'error', purpose, message
        if purpose == 'decision':
            self.message += f' Failed retries: {self.failed_decision_retries}/{DECISION_RETRY_LIMIT}.'
        self._publish()

    def _fallback_metadata(self):
        if self.config.recognition_fallback is None:
            return None
        return {'settings': asdict(self.config.recognition_fallback),
                'min_effective_probability': self.config.fallback_threshold,
                'after_rejections': self.config.fallback_after_rejections}

    def record(self):
        record = session_record(self.session_id, self.game, self.state, self.pending, self.events, self.config)
        record['decision_retries'] = {'version': 1, 'state_revision': self.game.revision(self.state),
                                      'failed': self.failed_decision_retries}
        record['recognition_retries'] = {'version': 1, 'state_revision': self.game.revision(self.state),
                                         'failed': self.failed_recognition_retries}
        if self.config.recognition_fallback:
            record['recognition_fallback'] = self._fallback_metadata()
        if self.termination:
            record['termination'] = self.termination.copy()
        if self.coached:
            record['coaching'] = {'version': 1, 'strategy': self.strategy.copy(), 'revision': self.strategy_revision,
                                  'run_id': None, 'rule_origin': 'Current learning',
                                  **self.learning_contexts.get(self.game.id, {})}
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
