"""Architecture boundaries and Boku's game-specific coaching lifecycle."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import threading
import pytest

from app.config import Config, load_config
from app.core.session import SessionController
from app.games.boku.game import Boku, RULES
from app.games.boku.geometry import CELLS, COORDINATES, AXES
from app.games.boku.coaching import BASIC_QUOTED_STRATEGY, loss_context, position
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.coach import coach_messages, diagnose_then_coach, ContextBudget, shared_coach, prepared_transport
from app.inference.imajev_client import ImajevClient
from app.storage.atomic import atomic_json
from app.ui.window import Window
from test_boku import BokuFake, ink
from test_session import wait_for


LOSS = ['A1','K1','A2','K2','A3','K3','A4','K4','A5']
WHITE_WIN = ['A1','K1','A3','K2','B1','K3','B3','K4','C1','K5']


def make_controller(tmp_path, mode='coached_quoted'):
    c = SessionController(Boku(), BokuFake(), Config(game='boku', prompt_variant=mode,
                         learning_directory=tmp_path/'learning', directory=tmp_path/'sessions'))
    c.ready = True
    return c


def play(c, sequence):
    for cell in sequence:
        c.state = c.game.apply_action(c.state, 'place_'+cell)


@pytest.mark.parametrize('directory', ['core', 'inference', 'storage', 'ui'])
def test_shared_modules_have_no_concrete_game_dependencies(directory):
    for path in Path('app', directory).glob('*.py'):
        if path.name == 'registry.py':
            continue
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('app.games.'), path
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith('app.games.') for alias in node.names), path
        assert "== 'boku'" not in source and "== 'tic_tac_toe'" not in source, path


def test_games_do_not_import_each_other():
    for game, other in [('boku','tic_tac_toe'), ('tic_tac_toe','boku')]:
        for path in Path('app/games', game).glob('*.py'):
            assert 'app.games.'+other not in path.read_text(), path


@pytest.mark.parametrize('mode', ['legacy','quoted','coached_quoted'])
def test_boku_strategy_and_retries_cover_both_phases(mode):
    game = Boku()
    placement = game.apply_action(game.initial_state(), 'place_A1')
    capture = game.initial_state()
    for cell in ('A2','A1','A3','K1','K2','A4'):
        capture = game.apply_action(capture, 'place_'+cell)
    for state in (placement, capture):
        rules = ['First preserve a winning line through mandatory capture.', 'Then apply the lesson.']
        request = game.decision_request(state, game.legal_actions(state), prompt_variant=mode, strategy=rules)
        retry = game.retry_decision_request(state, game.legal_actions(state), 2, prompt_variant=mode, strategy=rules)
        assert request['state']['rules'] == RULES
        assert request['questions']['move']['criteria'] == retry['questions']['move']['criteria']
        if mode == 'legacy':
            assert 'strategy' not in retry['state']
        else:
            assert retry['state']['strategy'] == (rules if mode == 'coached_quoted' else BASIC_QUOTED_STRATEGY)
        assert 'place_B2' not in retry['questions']['move']['instructions']


@pytest.mark.parametrize('sequence,trigger', [(LOSS,True), (WHITE_WIN,False)])
def test_boku_loss_only_history_and_isolation(qapp,tmp_path,sequence,trigger):
    c = make_controller(tmp_path)
    calls = []
    c._coach = lambda: calls.append(c.session_id)
    play(c,sequence)
    c._after_move()
    assert bool(calls) == trigger
    assert len(list((tmp_path/'learning'/'boku'/'games').glob('*.json'))) == 1
    assert not (tmp_path/'learning'/'strategy.json').exists()
    if trigger:
        request = c.policy.coaching.context(c.learning_store.request(c.session_id))
        assert request['statistics'] == {'loss':1}
        assert request['games'][0]['final_position'] == position(c.state)
        assert request['games'][0]['result_for_computer'] == 'loss'
        assert request['previous_strategy'] == BASIC_QUOTED_STRATEGY
        payload = json.loads(coach_messages(request)[1]['content'])
        assert payload['game_rules'].startswith(RULES)
        assert len(payload['geometry']['cells']) == 80
        assert payload['geometry']['axes'] == [list(a) for a in AXES]
        assert not any(key in json.dumps(payload) for key in ('winning_lines','result_for_O','drawing','image_png'))


def test_boku_draw_and_abandoned_games_do_not_coach(qapp,tmp_path):
    c = make_controller(tmp_path)
    c._coach = lambda: pytest.fail('Unexpected coaching')
    c.new_game(); c.new_game()
    assert not list((tmp_path/'learning'/'boku'/'games').glob('*.json'))
    c.state = replace(c.state, reserves=(0,36))
    c._after_move()
    assert c.phase == 'over' and c.game.outcome(c.state).kind == 'draw'
    assert len(list((tmp_path/'learning'/'boku'/'games').glob('*.json'))) == 1


def test_capture_replay_context_and_tampering(qapp,tmp_path):
    c = make_controller(tmp_path)
    for action in ('place_A1','place_A2','place_K1','place_A3','place_A4','capture_A2',
                   'place_K2','place_F1','place_K3','place_F2','place_J1','place_F3','place_J2','place_F4','place_J3','place_F5'):
        c.state = c.game.apply_action(c.state, action)
    c._coach = lambda: None
    c._after_move()
    request = c.policy.coaching.context(c.learning_store.request(c.session_id))
    game = request['games'][0]
    assert game['actions'][5]['action'] == 'capture_A2'
    assert game['actions'][5]['eligible_captures'] == ['A2','A3']
    assert game['_segments'][0]['end_position']['reserves'] == {'Black':32, 'White':34}
    assert game['final_position'] == position(c.state)
    bad = dict(request['games'][0], winner='White')
    with pytest.raises(ValueError, match='result'):
        loss_context(dict(request,games=[bad]))


def test_boku_success_resume_and_next_game_injection(qapp,tmp_path,monkeypatch):
    c = make_controller(tmp_path)
    play(c,LOSS)
    calls = []
    def coach(client,request):
        calls.append(request)
        return {'summary':'Mistake: White ignored the open line. Alternative: block it.'} if request['task']=='diagnose' else {'strategy':['win immediately','block an open four before extending your line']}
    monkeypatch.setattr('app.core.session.shared_coach',coach)
    c._after_move()
    wait_for(qapp,lambda:not c.busy)
    assert not c.coaching_failed and c.next_strategy['revision'] == 1
    assert [r['task'] for r in calls] == ['diagnose','update']
    assert all(len(r['coach_context']['geometry']['cells']) == 80 for r in calls)
    saved = tmp_path/'learning'/'boku'/'sessions'/c.session_id/'session.json'
    restored = make_controller(tmp_path)
    restored.restore(saved)
    assert restored.session_id == c.session_id and restored.strategy == BASIC_QUOTED_STRATEGY
    restored._coach = lambda: pytest.fail('Duplicate successful update')
    restored._after_move()
    assert restored.phase == 'over'
    c.new_game()
    assert c.strategy == ['win immediately','block an open four before extending your line']
    c.state = c.game.apply_action(c.state, 'place_F5')
    c._launch('decision')
    wait_for(qapp,lambda:not c.busy)
    assert c.client.calls[-1]['state']['strategy'] == c.strategy
    assert c.events[-1]['strategy_revision'] == 1
    assert 'tactical_guard' not in c.record() and 'opening_suggestion' not in c.record()


def test_boku_controls_settings_are_per_game_and_legacy_resume(qapp,tmp_path):
    c = make_controller(tmp_path,'quoted')
    w = Window(c)
    w.show()
    wait_for(qapp,lambda:not c.busy)
    try:
        assert w.prompt_selector.isVisible() and w.move_temperature.isVisible()
        w.prompt_selector.setCurrentIndex(w.prompt_selector.findData('coached_quoted'))
        w.move_temperature.setValue(.5)
        assert c.coached and c.strategy == BASIC_QUOTED_STRATEGY
        boku_id = c.session_id
        c.set_pending(ink('F5'))
        assert c.session_id == boku_id
        w.selector.setCurrentIndex(w.selector.findData('tic_tac_toe'))
        assert not c.coached and w.move_temperature.value() == 0
        assert not w.game_details.isVisible()
        w.move_temperature.setValue(.25)
        w.selector.setCurrentIndex(w.selector.findData('boku'))
        assert c.coached and w.move_temperature.value() == .5
        assert 'Reserves' in w.game_details.text()
        old = c.record()
        old.pop('coaching'); old.pop('game_settings_version')
        old['prompt_variant'] = 'coached_quoted'  # This field was ignored before Boku coaching existed.
        path = tmp_path/'old-boku.json'; path.write_text(json.dumps(old))
        restored = make_controller(tmp_path)
        restored.restore(path)
        assert not restored.coached and restored.config.prompt_variant == 'legacy'
    finally:
        w.close()


def test_boku_failure_diagnosis_and_recovery_ui(qapp,tmp_path,monkeypatch):
    c = make_controller(tmp_path)
    play(c,LOSS)
    gate = threading.Event()
    def invoke(_,request):
        if request['task']=='diagnose': return {'summary':'Mistake: an unblocked Black line.'}
        gate.wait(3)
        raise RuntimeError('Revision failed')
    monkeypatch.setattr('app.core.session.shared_coach',invoke)
    c._after_move()
    w = Window(c)
    try:
        wait_for(qapp,lambda:bool(c.coach_diagnosis))
        assert c.busy and not w.new_button.isEnabled()
        assert c.game.outcome(c.state).winner == 'Black'
        assert 'unblocked Black line' in w.diag.toPlainText()
    finally:
        gate.set()
        wait_for(qapp,lambda:not c.busy)
    assert c.coaching_failed and c.learning_store.ledger()['revision'] == 0
    w.refresh()
    assert w.retry_button.text() == 'Retry coaching'
    c.continue_coaching()
    assert c.busy
    wait_for(qapp,lambda:not c.busy)
    assert not c.coaching_failed and c.ready
    w.close()


def test_long_boku_history_reduction_keeps_rules_and_complete_coverage():
    cgame = Boku()
    state = cgame.initial_state()
    for cell in LOSS:
        state = cgame.apply_action(state,'place_'+cell)
    game = {'game_id':'loss','starting_player':'Black','outcome':'win','winner':'Black',
            'moves':[{'player':m.player,'action':m.action} for m in state.history]}
    request = loss_context({'games':[game,dict(game,game_id='older')], 'previous_strategy':BASIC_QUOTED_STRATEGY,
                            'included_game_ids':['loss','older']})
    # Force every full game to be segmented, independently of local byte estimates.
    calls = []; records = []
    def invoke(payload):
        calls.append(payload)
        messages = coach_messages(payload)
        content = json.loads(messages[1]['content'])
        assert content['game_rules'].startswith(RULES)
        assert content['geometry']['cells'] == {c:list(qr) for c,qr in COORDINATES.items()}
        if any('_segments' in g for g in payload.get('games',[])):
            raise ContextBudget('Token budget')
        if payload['task']=='update': return {'strategy':['win immediately','otherwise block the open four']}
        return {'summary':'Inspect the earliest White placement that allowed Black’s open row.'}
    result = diagnose_then_coach(request,invoke,records.append,byte_budget=18000)
    assert result['included_game_ids'] == ['loss','older']
    seen = {(s['game_id'], tuple(s['action_range'])) for p in calls for s in p.get('segments',[])}
    assert seen == {('loss',(1,8)),('loss',(9,9)),('older',(1,8)),('older',(9,9))}
    assert calls[-1]['games'][0]['final_position'] == position(state)
    assert result['stages'] == records


def test_raw_transport_parses_outside_backend():
    seen = []
    def transport(request):
        seen.append(request)
        return {'response_text':'1. win immediately\n2. block Black', 'truncated':False, 'model':'coach'}
    invoke = prepared_transport(transport)
    result = invoke({'task':'update','coach_context':Boku().session_policy.coaching.context({'games':[]})['coach_context']})
    assert result['strategy'] == ['win immediately','block Black']
    assert set(seen[0]) == {'messages'}


def test_shared_coach_rejects_old_protocol_before_post(monkeypatch):
    client = ImajevClient(Config())
    client._check_busy = lambda _: setattr(client,'service_metadata',{'coaching':True,'coach_protocol':1})
    class Transport:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def post(self,*_,**__): pytest.fail('Old service called')
    monkeypatch.setattr('app.inference.coach.httpx.Client',Transport)
    with pytest.raises(RuntimeError,match='Restart'):
        shared_coach(client,{'task':'update'})


def test_all_registered_games_supply_coaching_and_prompt_choices():
    from app.core.registry import GAMES
    for game in GAMES.values():
        policy = game.session_policy
        assert policy.coaching is not None
        assert policy.coaching.game_id == game.id
        assert dict(policy.prompt_choices).keys() == {'legacy','quoted','coached_quoted'}
        assert policy.default_prompt == 'quoted'
    assert 'app.games' not in Path('scripts/serve_local.py').read_text()


def test_large_boku_game_is_bounded_without_losing_action_segments():
    import random
    from app.games.boku.coaching import enrich_game
    game = Boku(); state = game.initial_state(); rng = random.Random(8)
    while game.outcome(state).kind == 'ongoing':
        actions = list(game.legal_actions(state)); rng.shuffle(actions)
        # Prefer a nonwinning continuation to exercise reserve exhaustion and long history.
        choices = [a for a in actions if game.outcome(game.apply_action(state,a.id)).kind == 'ongoing']
        state = game.apply_action(state,(choices or actions)[0].id)
    assert len(state.history) >= 60
    outcome = game.outcome(state)
    old = enrich_game({'game_id':'long','starting_player':'Black','outcome':outcome.kind,'winner':outcome.winner,
                       'moves':[{'player':m.player,'action':m.action} for m in state.history]})
    loss_state = game.initial_state()
    for cell in LOSS: loss_state = game.apply_action(loss_state,'place_'+cell)
    trigger = {'game_id':'loss','starting_player':'Black','outcome':'win','winner':'Black',
               'moves':[{'player':m.player,'action':m.action} for m in loss_state.history]}
    request = loss_context({'games':[trigger], 'previous_strategy':BASIC_QUOTED_STRATEGY,
                            'included_game_ids':['loss','long']})
    request['games'].append(old)
    seen = []
    def invoke(payload):
        assert len(json.dumps(coach_messages(payload),ensure_ascii=False).encode()) <= 14000
        seen.extend(payload.get('segments',[]))
        if payload['task'] == 'update': return {'strategy':['win immediately','otherwise block Black']}
        return {'summary':'White should address the opponent’s contiguous threats before expanding elsewhere.'}
    result = diagnose_then_coach(request,invoke,lambda stage: None)
    assert result['included_game_ids'] == ['loss','long']
    covered = {i for segment in seen for i in range(segment['action_range'][0],segment['action_range'][1]+1)}
    assert covered == set(range(1,len(state.history)+1))


def test_history_order_is_trigger_first_then_newest_and_excludes_other_game(qapp,tmp_path):
    c = make_controller(tmp_path)
    c._coach = lambda:None
    play(c,LOSS); c._after_move()
    first = c.session_id
    root = tmp_path/'learning'/'boku'/'games'
    first_record = json.loads((root/(first+'.json')).read_text())
    for name,date,game in [('older','2000','boku'),('newer','2030','boku'),('foreign','2050','tic_tac_toe')]:
        atomic_json(root/(name+'.json'),dict(first_record,game_id=name,completed_at=date,game=game))
    request = c.learning_store.request(first)
    assert request['included_game_ids'] == [first,'newer','older']


def test_shared_coach_sends_prepared_messages_and_parses_raw_text(monkeypatch):
    import httpx
    real_client = httpx.Client
    seen = []
    def handle(request):
        if request.url.path == '/v1/status':
            return httpx.Response(200,json={'busy':False,'coaching':True,'coach_protocol':2})
        assert request.url.path == '/v1/coach'
        data = json.loads(request.content)
        assert set(data) == {'messages'}
        seen.extend(data['messages'])
        return httpx.Response(200,json={'response_text':'1. win immediately\n2. prevent Black winning',
                                       'truncated':False, 'usage':{'input_tokens':100}, 'model':'base'})
    monkeypatch.setattr('app.inference.coach.httpx.Client',
                        lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    from app.games.boku.coaching import coaching_context
    result = shared_coach(ImajevClient(Config()),{'task':'update','coach_context':coaching_context()})
    assert result['strategy'] == ['win immediately','prevent Black winning']
    assert result['usage']['input_tokens'] == 100
    assert json.loads(seen[1]['content'])['game_rules'].startswith(RULES)
