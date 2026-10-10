"""Retry limits adjudicate the session without altering either game's board."""
import json
import threading

import pytest

from app.config import Config
from app.core.results import FORFEIT_MESSAGE
from app.core.session import SessionController
from app.games.boku.game import Boku
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.coach import _History, coach_messages
from app.inference.imajev_client import parse_reply
from app.ui.window import Window
from test_protocol import answer
from test_session import wait_for


class Client:
    def __init__(self):
        self.calls = []
        self.refuse = True
        self.error = None
        self.gate = threading.Event()
        self.gate.set()

    def decide(self, request, png):
        self.calls.append(request)
        self.gate.wait(3)
        if self.error:
            raise RuntimeError(self.error)
        if 'move' not in request['questions']:
            raw = answer(request, {'symbol': 'invalid', 'cell': 'invalid'})
            return parse_reply(raw, request, raw['model'])
        choices = {'move': next(iter(request['questions']['move']['criteria']))}
        raw = answer(request, choices)
        raw['answers']['move']['abstained'] = self.refuse
        return parse_reply(raw, request, raw['model'])

    def warmup(self, request, png):
        choices = {'symbol': 'invalid', 'cell': 'invalid'}
        raw = answer(request, choices)
        return parse_reply(raw, request, raw['model'])


def make(tmp_path, game_id='tic_tac_toe', mode='quoted'):
    game = Boku() if game_id == 'boku' else TicTacToe()
    c = SessionController(game, Client(), Config(
        game=game.id, prompt_variant=mode, tactical_guard=False, opening_suggestion=False,
        learning_directory=tmp_path/'learning', directory=tmp_path/'sessions'))
    c.ready = True
    c.state = game.apply_action(c.state, 'place_A1')
    c._coach = lambda: None  # Inspect the saved coaching request without running an LLM.
    return c


def decide(qapp, c, retry=False):
    c.retry() if retry else c._launch('decision')
    wait_for(qapp, lambda: not c.busy)


def forfeit(qapp, c):
    decide(qapp, c)
    for _ in range(3):
        decide(qapp, c, retry=True)


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
@pytest.mark.parametrize('mode', ['legacy', 'quoted', 'coached_quoted'])
def test_initial_refusal_plus_three_failed_retries_forfeits(qapp, tmp_path, game_id, mode):
    c = make(tmp_path, game_id, mode)
    before = c.state
    coached = []
    c._coach = lambda: coached.append(c.session_id)
    decide(qapp, c)
    assert c.failed_decision_retries == 0 and c.phase == 'error'
    for count in (1, 2):
        decide(qapp, c, retry=True)
        assert c.failed_decision_retries == count
        assert c.phase == 'error' and c.outcome.kind == 'ongoing'
        assert f'Failed retries: {count}/3' in c.message
    decide(qapp, c, retry=True)
    assert c.phase == 'over' and c.message == FORFEIT_MESSAGE
    assert c.outcome.winner == c.game.human_player and c.outcome.line is None
    assert c.state == before and c.game.outcome(c.state).kind == 'ongoing'
    assert not c.editable and c.retry_purpose is None
    assert all(not e.get('accepted_action') for e in c.events)
    assert coached == ([c.session_id] if mode == 'coached_quoted' else [])
    c.retry()
    assert len(c.client.calls) == 4


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_errors_do_not_consume_or_reset_retries_and_success_resets(qapp, tmp_path, game_id):
    c = make(tmp_path, game_id)
    decide(qapp, c)
    decide(qapp, c, retry=True)
    assert c.failed_decision_retries == 1
    for error in ('request timed out', 'malformed JSON', 'GPU unavailable'):
        c.client.error = error
        decide(qapp, c, retry=True)
        assert c.failed_decision_retries == 1 and c.outcome.kind == 'ongoing'
    c.client.error = None
    c.client.refuse = False
    decide(qapp, c, retry=True)
    assert c.failed_decision_retries == 0 and c.decision_attempt == 0
    assert c.phase == 'human' and c.outcome.kind == 'ongoing'
    c.state = c.game.apply_action(c.state, c.game.legal_actions(c.state)[-1].id)
    c.client.refuse = True
    decide(qapp, c)
    assert c.failed_decision_retries == 0


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_resume_retains_retry_limit_and_terminal_forfeit(qapp, tmp_path, game_id):
    c = make(tmp_path, game_id)
    decide(qapp, c)
    for _ in range(2):
        decide(qapp, c, retry=True)
    saved = tmp_path/'game.json'
    saved.write_text(json.dumps(c.record()))
    resumed = make(tmp_path, game_id)
    resumed.restore(saved)
    assert resumed.failed_decision_retries == 2
    # Resume automatically continues the pending computer turn as the next retry.
    resumed._after_move()
    wait_for(qapp, lambda: not resumed.busy)
    assert resumed.phase == 'over' and resumed.termination
    saved.write_text(json.dumps(resumed.record()))
    terminal = make(tmp_path, game_id)
    terminal.restore(saved)
    terminal.start()
    wait_for(qapp, lambda: not terminal.busy)
    assert terminal.phase == 'over' and terminal.outcome.winner == terminal.game.human_player
    assert terminal.client.calls == []


def test_interrupted_retry_does_not_consume_limit_on_resume(qapp, tmp_path):
    c = make(tmp_path)
    decide(qapp, c)
    for _ in range(2):
        decide(qapp, c, retry=True)
    c.client.gate.clear()
    c.retry()
    saved = tmp_path/'interrupted.json'
    saved.write_text(json.dumps(c.record()))
    c.new_game()  # Supersede the unfinished request.
    c.client.gate.set()
    wait_for(qapp, lambda: not c.busy)
    resumed = make(tmp_path)
    resumed.restore(saved)
    assert resumed.failed_decision_retries == 2 and resumed.decision_attempt == 3
    resumed._after_move()
    wait_for(qapp, lambda: not resumed.busy)
    assert resumed.termination and len(resumed.client.calls) == 1


@pytest.mark.parametrize('field', ['winner', 'state_revision', 'failed_retries', 'reason', 'evidence', 'progress'])
def test_resume_rejects_inconsistent_forfeit(qapp, tmp_path, field):
    c = make(tmp_path)
    forfeit(qapp, c)
    record = c.record()
    if field == 'evidence':
        record['events'] = record['events'][:-1]
        record.pop('decision_retries')
    elif field == 'progress':
        record['decision_retries']['failed'] = 1
    else:
        record['termination'][field] = 'invalid'
    saved = tmp_path/'invalid.json'
    saved.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='[Ff]orfeit|retry progress'):
        make(tmp_path).restore(saved)


def test_legacy_progress_reconstruction_and_excluded_errors(qapp, tmp_path):
    c = make(tmp_path)
    decide(qapp, c)
    c.client.error = 'timeout'
    decide(qapp, c, retry=True)
    c.client.error = None
    decide(qapp, c, retry=True)
    record = c.record()
    record.pop('decision_retries')
    for event in record['events']:
        event.pop('decision_attempt', None)
    saved = tmp_path/'legacy.json'
    saved.write_text(json.dumps(record))
    resumed = make(tmp_path)
    resumed.restore(saved)
    assert resumed.failed_decision_retries == 1
    assert resumed.decision_attempt == 3


def test_legacy_record_already_at_limit_is_terminal_on_restore(qapp, tmp_path):
    c = make(tmp_path)
    forfeit(qapp, c)
    record = c.record()
    record.pop('decision_retries')
    record.pop('termination')
    for event in record['events']:
        event.pop('decision_attempt', None)
    saved = tmp_path/'legacy-loop.json'
    saved.write_text(json.dumps(record))
    resumed = make(tmp_path)
    resumed.restore(saved)
    resumed._after_move()
    assert resumed.termination and resumed.phase == 'over'
    assert resumed.client.calls == []


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_forfeit_coaching_has_cause_and_does_not_repeat_after_resume(qapp, tmp_path, game_id):
    c = make(tmp_path, game_id, 'coached_quoted')
    forfeit(qapp, c)
    request = c.learning_store.request(c.session_id)
    context = c.policy.coaching.context(request)
    game = context['games'][0]
    assert game['outcome'] == 'win' and game['winner'] == c.game.human_player
    assert game['termination'] == c.termination and len(game['forfeit_evidence']) == 4
    assert c.game.outcome(c.state).kind == 'ongoing'
    for task in ('diagnose', 'summarize', 'update'):
        messages = coach_messages(dict(context, task=task))
        assert 'not a board victory' in messages[0]['content']
    c.learning_store.commit(request, {'strategy': c.strategy, 'included_game_ids': request['included_game_ids']})
    saved = tmp_path/'coached.json'
    saved.write_text(json.dumps(c.record()))
    resumed = make(tmp_path, game_id, 'coached_quoted')
    resumed.restore(saved)
    assert resumed.session_id == c.session_id
    resumed._coach = lambda: pytest.fail('Duplicate coaching')
    resumed._after_move()
    assert resumed.phase == 'over'
    assert resumed.learning_store.ledger()['revision'] == 1


def test_boku_history_reduction_keeps_forfeit_evidence(qapp, tmp_path):
    c = make(tmp_path, 'boku', 'coached_quoted')
    forfeit(qapp, c)
    context = c.policy.coaching.context(c.learning_store.request(c.session_id))
    calls = []
    def invoke(payload):
        calls.append(payload)
        return {'summary': 'Black won by retry-limit forfeit; no board victory occurred.'}
    history = _History(context, invoke, lambda stage: None, 18000)
    reduced = history.reduce_game(context['games'][0])
    assert reduced['termination'] == c.termination
    assert reduced['forfeit_evidence'] == context['games'][0]['forfeit_evidence']
    assert calls[0]['segments'][-1]['termination'] == c.termination
    summary = history.summarize([context['games'][0]], 'games')
    assert summary[0]['forfeits'] == [{'game_id': c.session_id, 'termination': c.termination}]
    repeated = history.summarize(summary, 'summaries')
    assert repeated[0]['forfeits'] == summary[0]['forfeits']


def test_reset_and_switch_discard_stale_failures(qapp, tmp_path):
    c = make(tmp_path)
    decide(qapp, c)
    decide(qapp, c, retry=True)
    c.client.gate.clear()
    c.retry()
    old_ticket = c.inflight
    c.select_game(Boku())
    assert c.failed_decision_retries == 0 and c.termination is None
    c.client.gate.set()
    wait_for(qapp, lambda: not c.busy)
    c._completed(old_ticket, None, 'stale error', 1)
    assert c.failed_decision_retries == 0 and c.termination is None
    assert c.phase == 'human'


def test_forfeit_gui_hides_retry_and_keeps_new_game(qapp, tmp_path):
    c = make(tmp_path)
    window = Window(c)
    window.show()
    # Window's scheduled startup may run during the first decision; it is gated.
    forfeit(qapp, c)
    assert not window.retry_button.isVisible()
    assert window.new_button.isEnabled() and not window.submit_button.isEnabled()
    assert window.message.text() == FORFEIT_MESSAGE
    window.close()


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_rejected_move_answers_also_count(qapp, tmp_path, monkeypatch, game_id):
    c = make(tmp_path, game_id)
    c.client.refuse = False
    def reject(state, reply):
        raise ValueError('Imajev selected an illegal action. Retry.')
    monkeypatch.setattr(c.game, 'decode_decision', reject)
    forfeit(qapp, c)
    assert c.termination and c.failed_decision_retries == 3
    assert all(not e['reply']['answers']['move']['abstained'] for e in c.events)


def test_forfeit_during_boku_capture_keeps_pending_capture_board(qapp, tmp_path):
    c = make(tmp_path, 'boku')
    c.state = c.game.initial_state()
    for cell in ('A2', 'A1', 'A3', 'K1', 'K2', 'A4'):
        c.state = c.game.apply_action(c.state, 'place_'+cell)
    before = c.state
    assert before.phase == 'capture' and before.next_player == 'White'
    forfeit(qapp, c)
    assert c.state == before and c.outcome.winner == 'Black'
    assert c.game.outcome(c.state).kind == 'ongoing'


def test_accepted_boku_capture_resets_progress(qapp, tmp_path):
    c = make(tmp_path, 'boku')
    c.state = c.game.initial_state()
    for cell in ('A2', 'A1', 'A3', 'K1', 'K2', 'A4'):
        c.state = c.game.apply_action(c.state, 'place_'+cell)
    decide(qapp, c)
    for _ in range(2):
        decide(qapp, c, retry=True)
    c.client.refuse = False
    decide(qapp, c, retry=True)
    assert c.state.history[-1].action.startswith('capture_')
    assert c.phase == 'human' and c.failed_decision_retries == 0


def test_forced_move_resets_retry_progress(qapp, tmp_path):
    c = make(tmp_path)
    c.state = c.game.initial_state()
    for cell in ('A1', 'B1', 'C1', 'B2', 'A2', 'C2', 'B3', 'A3'):
        c.state = c.game.apply_action(c.state, 'place_'+cell)
    c.failed_decision_retries, c.decision_attempt = 2, 3
    c._launch('decision')
    assert c.outcome.kind == 'draw'
    assert c.failed_decision_retries == c.decision_attempt == 0
    assert c.client.calls == []


def test_recognition_and_startup_failures_never_forfeit(qapp, tmp_path):
    from test_protocol import INK
    c = make(tmp_path)
    c.state = c.game.initial_state()
    c.phase = 'human'
    c.pending = INK
    for _ in range(4):
        c.submit()
        wait_for(qapp, lambda: not c.busy)
        assert c.phase == 'human' and c.failed_decision_retries == 0
    def broken_warmup(request, png):
        raise RuntimeError('startup unavailable')
    c.client.warmup = broken_warmup
    c.start()
    wait_for(qapp, lambda: not c.busy)
    for _ in range(4):
        c.retry()
        wait_for(qapp, lambda: not c.busy)
        assert c.retry_purpose == 'startup' and c.failed_decision_retries == 0
    assert c.termination is None


def test_duplicate_completion_does_not_count_twice(qapp, tmp_path):
    c = make(tmp_path)
    c._launch('decision')
    ticket = c.inflight
    wait_for(qapp, lambda: not c.busy)
    c._completed(ticket, None, 'duplicate', 1)
    assert len(c.events) == 1 and c.failed_decision_retries == 0


def test_coaching_failure_then_retry_retains_forfeit(qapp, tmp_path, monkeypatch):
    c = make(tmp_path, mode='coached_quoted')
    forfeit(qapp, c)
    c._coach = SessionController._coach.__get__(c)
    calls = []
    failing = True
    def coach(client, payload):
        calls.append(payload)
        if failing:
            raise RuntimeError('coach unavailable')
        return ({'summary': 'Loss by retry-limit forfeit; no board victory occurred.'}
                if payload['task'] == 'diagnose' else {'strategy': c.strategy})
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    c._coach()
    wait_for(qapp, lambda: not c.busy)
    assert c.coaching_failed and FORFEIT_MESSAGE in c.message
    assert c.failed_decision_retries == 3 and c.termination
    failing = False
    c.retry()
    wait_for(qapp, lambda: not c.busy)
    assert not c.coaching_failed and c.learning_store.updated(c.session_id)
    assert c.outcome.winner == c.game.human_player and FORFEIT_MESSAGE in c.message
    assert c.failed_decision_retries == 3


def test_continue_after_coaching_failure_checks_readiness(qapp, tmp_path, monkeypatch):
    c = make(tmp_path, mode='coached_quoted')
    forfeit(qapp, c)
    c._coach = SessionController._coach.__get__(c)
    def fail(client, payload):
        raise RuntimeError('coach unavailable')
    monkeypatch.setattr('app.core.session.shared_coach', fail)
    c._coach()
    wait_for(qapp, lambda: not c.busy)
    c.continue_coaching()
    wait_for(qapp, lambda: not c.busy)
    assert c.ready and not c.coaching_failed and c.termination
    assert FORFEIT_MESSAGE in c.message


def test_forfeit_result_visible_while_coaching_and_new_game_is_gated(qapp, tmp_path, monkeypatch):
    c = make(tmp_path, mode='coached_quoted')
    forfeit(qapp, c)
    c._coach = SessionController._coach.__get__(c)
    entered, gate = threading.Event(), threading.Event()
    def coach(client, payload):
        entered.set()
        gate.wait(3)
        return ({'summary': 'Loss by retry-limit forfeit.'}
                if payload['task'] == 'diagnose' else {'strategy': c.strategy})
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    window = Window(c)
    window.show()
    try:
        c._coach()
        wait_for(qapp, entered.is_set)
        assert c.busy and c.phase == 'coaching' and FORFEIT_MESSAGE in c.message
        assert not window.new_button.isEnabled() and not window.submit_button.isEnabled()
        game_id = c.session_id
        c.new_game()
        c.continue_coaching()
        assert c.session_id == game_id and c.busy
        gate.set()
        wait_for(qapp, lambda: not c.busy)
        assert c.phase == 'over' and window.new_button.isEnabled()
    finally:
        gate.set()
        wait_for(qapp, lambda: not c.busy)
        window.close()
