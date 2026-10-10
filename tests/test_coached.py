from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from contextlib import contextmanager, nullcontext
import pytest
from app.config import Config, load_config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.storage.coached import CoachedStore, BASIC_QUOTED_STRATEGY, validate_strategy
from app.inference.coach import coach_pipeline, diagnose_then_coach, ContextBudget, parse_coach_output
from app.ui.window import Window
from test_session import Fake, wait_for


def controller(qapp, tmp_path):
    return SessionController(TicTacToe(), Fake(), replace(Config(), prompt_variant='coached_quoted', learning_directory=tmp_path))


def play(c, cells):
    for cell in cells:
        c.state = c.game.apply_action(c.state, 'place_' + cell)


@pytest.mark.parametrize('bad', ['', [], [''], [None], ['x'] * 13, ['x' * 161], ['é' * 100] * 12])
def test_invalid_rules(bad):
    with pytest.raises(ValueError): validate_strategy(bad)


def test_coach_json_order_and_truncation():
    assert parse_coach_output('{"strategy":["last first", "win later"]}', 'update')['strategy'] == ['last first', 'win later']
    for text in ('{"strategy":["x"],"analysis":"y"}', '[]'):
        with pytest.raises(ValueError): parse_coach_output(text, 'update')
    with pytest.raises(ValueError): parse_coach_output('{"strategy":["x"]}', 'update', True)


def test_coached_prompt_keeps_quoted_facts_and_retry_strategy():
    game = TicTacToe(); state = game.apply_action(game.initial_state(), 'place_A1'); actions = game.legal_actions(state)
    base = game.decision_request(state, actions, prompt_variant='quoted')
    request = game.decision_request(state, actions, prompt_variant='coached_quoted')
    assert request['state']['strategy'] == BASIC_QUOTED_STRATEGY
    assert request['questions'] == base['questions']
    assert request['state']['board'] == base['state']['board']
    rules = ['avoid a repeated mistake', 'then win']
    retry = game.retry_decision_request(state, actions, 2, prompt_variant='coached_quoted', strategy=rules)
    assert retry['state']['strategy'] == rules
    assert retry['questions']['move']['criteria'] == base['questions']['move']['criteria']
    assert 'opening_advice' not in retry['state']


@pytest.mark.parametrize('cells,expected', [(['A1','A2','B1','B2','C1'],True),
    (['A1','A2','B1','B2','C3','C2'],False),
    (['A1','B1','C1','B2','A2','C2','B3','A3','C3'],False)])
def test_loss_only_and_saved_history(qapp, tmp_path, cells, expected):
    c = controller(qapp,tmp_path); calls=[]; c._coach=lambda: calls.append(c.session_id)
    play(c,cells); c._after_move()
    assert bool(calls)==expected
    assert len(list((tmp_path/'games').glob('*.json')))==1
    if expected:
        request=c.learning_store.request(c.session_id)
        response={'strategy':['learned'], 'included_game_ids':request['included_game_ids']}
        c.learning_store.commit(request,response)
        saved=tmp_path/'sessions'/c.session_id/'session.json'
        restored=controller(qapp,tmp_path); restored.restore(saved)
        assert restored.session_id==c.session_id
        assert restored.strategy==BASIC_QUOTED_STRATEGY
        restored._coach=lambda: pytest.fail('Duplicate successful update')
        restored._after_move()
        assert restored.learning_store.ledger()['revision']==1


def test_forced_draw_not_coached_and_abandonment(qapp,tmp_path):
    c=controller(qapp,tmp_path); c._coach=lambda: pytest.fail('Unexpected coach')
    play(c,['A1','B1','C1','B2','A2','C2','B3','A3'])
    c._launch('decision')
    assert c.game.outcome(c.state).kind=='draw'
    assert c.events[-1]['forced_action']=='place_C3'
    c.new_game(); c.new_game()
    assert len(list((tmp_path/'games').glob('*.json')))==1


def test_window_history_checkpoint_and_failed_commit(qapp,tmp_path):
    c=controller(qapp,tmp_path); c._coach=lambda: None
    play(c,['A1','B1','C1','B2','A2','C2','B3','A3','C3']); c._after_move()
    draw_id=c.session_id
    c.new_game(); play(c,['A1','A2','B1','B2','C1']); c._after_move()
    first=c.learning_store.request(c.session_id)
    assert first['statistics']=={'draw':1,'loss':1}
    assert first['included_game_ids'][0]==c.session_id
    with pytest.raises(ValueError): c.learning_store.commit(first,{'strategy':[], 'included_game_ids':first['included_game_ids']})
    assert c.learning_store.ledger()['revision']==0
    ledger=c.learning_store.commit(first,{'strategy':['new'], 'included_game_ids':first['included_game_ids']})
    assert draw_id in ledger['consumed_game_ids']
    c.learning_store.commit(first,{'strategy':['duplicate']})
    c.new_game(); assert c.strategy==['new']
    play(c,['A1','A2','B1','B2','C1']); c._after_move()
    second=c.learning_store.request(c.session_id)
    assert second['included_game_ids']==[c.session_id]
    assert second['previous_strategy']==['new']
    text=json.dumps(second)
    assert all(k not in text for k in ('image','drawing','points','transport'))


def test_history_batches_cover_every_game():
    games=[{'game_id':str(i), 'moves':'x'*200} for i in range(21)]
    request={'task':'update','previous_strategy':['win'],'games':games,'included_game_ids':[g['game_id'] for g in games]}
    records=[]; seen=[]
    def invoke(payload):
        if payload['task']=='update':
            if len(payload['games'])>1: raise ContextBudget('Too large')
            return {'strategy':['new rule']}
        if len(payload.get('games',payload.get('summaries',[])))>3: raise ContextBudget('Too large')
        seen.extend(g['game_id'] for g in payload.get('games',[]))
        return {'summary':'short evidence'}
    result=coach_pipeline(request,invoke,records.append)
    assert set(seen)==set(str(i) for i in range(1,21))
    assert result['included_game_ids']==request['included_game_ids']
    assert records[-1]['request']['games']==[games[0]]
    assert any(r['error'] for r in records)


def test_no_reduction_progress_preserves_rules():
    request={'task':'update','previous_strategy':['win'],'games':[{'game_id':'loss'}, {'game_id':'other'}], 'included_game_ids':['loss','other']}
    def invoke(p):
        if p['task']=='update': raise ContextBudget('Too large')
        return {'summary':'grows'*300}
    with pytest.raises(ContextBudget): coach_pipeline(request,invoke,lambda _: None)


def test_inference_snapshot_no_correction_and_abstention_no_coach(qapp,tmp_path):
    c=controller(qapp,tmp_path); c.ready=True
    c.state=c.game.apply_action(c.state,'place_A1')
    c.game.tactical_choice=lambda *_: pytest.fail('Coached move corrected')
    c._coach=lambda: pytest.fail('Abstention coached')
    c._launch('decision'); wait_for(qapp,lambda: not c.busy)
    assert c.client.calls[-1]['state']['strategy']==BASIC_QUOTED_STRATEGY
    assert c.events[-1]['strategy_revision']==0
    assert not c.record()['tactical_guard']
    c.strategy=['changed snapshot']; c.decision_attempt=1
    c.state=c.game.apply_action(c.state,next(a.id for a in c.game.legal_actions(c.state)))
    c._launch('decision'); wait_for(qapp,lambda:not c.busy)
    assert c.client.calls[-1]['state']['strategy']==['changed snapshot']


def test_retry_continue_and_responsive_ui(qapp,tmp_path,monkeypatch):
    c=controller(qapp,tmp_path); c.ready=True; c.start=lambda:None
    play(c,['A1','A2','B1','B2','C1'])
    monkeypatch.setattr('app.core.session.shared_coach',lambda *_: (_ for _ in ()).throw(RuntimeError('coach failed')))
    c._after_move(); wait_for(qapp,lambda:not c.busy)
    w=Window(c); w.refresh()
    assert c.coaching_failed and not w.new_button.isEnabled()
    assert w.retry_button.text()=='Retry coaching'
    before=c.session_id; w.new_game(); assert c.session_id==before
    gate=threading.Event()
    def ready(): gate.wait(3)
    c._warmup_ready=ready
    c.continue_coaching(); assert c.busy; w.refresh(); assert not w.continue_button.isEnabled()
    qapp.processEvents(); assert c.busy
    gate.set(); wait_for(qapp,lambda:not c.busy)
    assert not c.coaching_failed and c.learning_store.ledger()['revision']==0
    w.close()


def test_coach_success_next_game_snapshot(qapp,tmp_path,monkeypatch):
    c=controller(qapp,tmp_path); c.ready=True
    play(c,['A1','A2','B1','B2','C1'])
    requests=[]
    def coach(_,request):
        requests.append(request)
        if request['task']=='diagnose':
            return {'summary':'Mistake: O allowed two immediate winning replies. Lesson: prevent the fork.'}
        return {'strategy':['new first','new second']}
    monkeypatch.setattr('app.core.session.shared_coach',coach)
    c._after_move(); wait_for(qapp,lambda:not c.busy)
    assert requests[0]['games'][0]['result_for_O']=='loss'
    assert requests[0]['games'][0]['turns'][0]['board_before']==['X..','...','...']
    assert len(requests[0]['winning_lines'])==8
    assert [r['task'] for r in requests]==['diagnose','update']
    assert requests[1]['loss_analysis']==c.coach_diagnosis
    assert c.strategy==BASIC_QUOTED_STRATEGY
    assert c.next_strategy['strategy']==['new first','new second']
    c.new_game(); assert c.strategy==['new first','new second']


def test_config_coached(tmp_path):
    assert load_config(__import__('pathlib').Path('config.coached-quoted.yaml')).prompt_variant=='coached_quoted'
    p=tmp_path/'c.yaml'; p.write_text('learning:\n  coach_backend: ollama\n')
    with pytest.raises(ValueError,match='learning.model'): load_config(p)


@pytest.mark.parametrize('failure',[None,RuntimeError('fail'),KeyboardInterrupt()])
def test_adapter_restored_and_readout_preserved(monkeypatch,failure):
    import sys
    from scripts.serve_local import generate_strategy
    events=[]
    class Tensor:
        shape=(1,10)
        def to(self,_): return self
        def __getitem__(self,_): return self
        def tolist(self): return [1,2]
    class Tokenizer:
        pad_token_id=0; eos_token_id=2
        def apply_chat_template(self,*_,**kwargs):
            assert kwargs['enable_thinking'] is False
            return 'prompt'
        def __call__(self,*_,**kwargs): return {'input_ids':Tensor()}
        def decode(self,*_,**kwargs): return '{"strategy":["new"]}'
    class Model:
        @contextmanager
        def disable_adapter(self):
            events.append('disabled')
            try: yield
            finally: events.append('restored')
        def generate(self,**kwargs):
            assert kwargs['pad_token_id']==0 and kwargs['do_sample'] is False
            if failure: raise failure
            return Tensor()
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(inference_mode=nullcontext,cuda=SimpleNamespace(is_available=lambda:False)))
    engine=SimpleNamespace(processor=SimpleNamespace(tokenizer=Tokenizer()),model=Model(),device='fake',readout=object())
    readout=engine.readout
    if failure:
        with pytest.raises(type(failure)): generate_strategy(engine,{'task':'update','games':[]})
    else: assert generate_strategy(engine,{'task':'update','games':[]})['strategy']==['new']
    assert events==['disabled','restored'] and engine.readout is readout


def test_abstention_retry_preserves_rules_without_coaching(qapp,tmp_path):
    from app.inference.imajev_client import parse_reply
    from test_protocol import answer
    c=controller(qapp,tmp_path); c.ready=True
    c.state=c.game.apply_action(c.state,'place_A1')
    calls=[]
    def decide(request,image):
        calls.append(request)
        return parse_reply(answer(request,{'move':'B2'},abstained=len(calls)==1),request,'imajev-2b')
    c.client.decide=decide
    c._coach=lambda:pytest.fail('Abstention triggered coaching')
    c._launch('decision'); wait_for(qapp,lambda:not c.busy)
    assert c.phase=='error' and c.state.revision==1
    c.retry(); wait_for(qapp,lambda:not c.busy)
    assert c.state.board[4]=='O'
    assert all(p['state']['strategy']==BASIC_QUOTED_STRATEGY for p in calls)
    assert calls[1]['state']['retry_attempt']==1


def test_atomic_commit_failure_does_not_consume_history(qapp,tmp_path,monkeypatch):
    c=controller(qapp,tmp_path); c._coach=lambda:None
    play(c,['A1','A2','B1','B2','C1']); c._after_move()
    request=c.learning_store.request(c.session_id)
    monkeypatch.setattr('app.storage.coached.os.replace',lambda *_: (_ for _ in ()).throw(OSError('disk failed')))
    with pytest.raises(OSError):
        c.learning_store.commit(request,{'strategy':['new'],'included_game_ids':request['included_game_ids']})
    assert c.learning_store.ledger()['revision']==0
    assert c.learning_store.request(c.session_id)['included_game_ids']==request['included_game_ids']
    assert not list(tmp_path.glob('*.tmp'))


def test_window_close_waits_for_owned_shutdown(qapp,tmp_path):
    c=controller(qapp,tmp_path); c.start=lambda:None
    gate=threading.Event(); stopped=[]
    def stop():
        gate.wait(3); stopped.append(True)
    c.service_manager=SimpleNamespace(shutdown=stop)
    w=Window(c); w.close()
    assert c.busy and not c.shutdown_done
    qapp.processEvents(); assert c.busy
    gate.set(); wait_for(qapp,lambda:c.shutdown_done)
    assert stopped
    w.close()


@pytest.mark.parametrize('text',[
    '{"strategy":["win","block"]}`',
    '`{"strategy":["win","block"]}`',
    '```json\n{"strategy":["win","block"]}\n```',
    '1. win\n2. block',
    '1) win\n\n2) block',
    '```text\n1. win\n2. block\n```',
])
def test_coach_accepts_presentation_wrappers_and_numbered_rules(text):
    assert parse_coach_output(text,'update')['strategy']==['win','block']
    with pytest.raises(ValueError): parse_coach_output(text,'update',truncated=True)


@pytest.mark.parametrize('text',[
    'Here are the rules:\n1. win\n2. block',
    '1. win\n2. block\nThese will help.',
    '2. win\n3. block',
    '1. win\n1. block',
    '1. win\n3. block',
    '{"strategy":["win"]} {"strategy":["block"]}',
    '{"strategy":["win"]} explanation',
    '{"strategy":["win"]',
    '```json\n{"strategy":["win"]}\n```\nextra',
    '1. win\n2. '+ 'x'*161,
])
def test_coach_does_not_repair_content_or_discard_prose(text):
    with pytest.raises(ValueError): parse_coach_output(text,'update')


def test_coach_prompt_focuses_on_evidence_and_preserves_context():
    from app.inference.coach import coach_messages
    request={'task':'update','previous_strategy':['win','block'],'games':[{'game_id':'loss','moves':[]}], 'included_game_ids':['loss']}
    messages=coach_messages(request)
    assert 'earliest avoidable mistake' in messages[0]['content']
    assert 'numbered list' in messages[0]['content']
    assert 'duplicate rules' in messages[0]['content']
    context=json.loads(messages[1]['content'])
    assert context['previous_strategy']==request['previous_strategy']
    assert context['games']==request['games']
    for task in ('update','summarize','diagnose'):
        assert 'thinking' not in coach_messages(dict(request,task=task))[0]['content'].lower()


def test_coach_reconstructed_boards_match_accepted_moves():
    from app.games.tic_tac_toe.coaching import enrich_game
    game={'starting_player':'O','moves':[{'player':'O','action':'place_B2'}, {'player':'X','action':'place_C3'}, {'player':'O','action':'place_B3'}]}
    trace=enrich_game(game)['turns']
    assert trace[0]['board_before']==['...','...','...']
    assert trace[0]['board_after']==['...','.O.','...']
    assert trace[0]['next_X_move']=='C3'
    assert trace[0]['move_number']==1
    assert trace[0]['board_after_X_reply']==['...','.O.','..X']
    assert trace[1]['board_before']==['...','.O.','..X']
    assert trace[1]['board_after']==['...','.O.','.OX']
    assert 'B2' not in trace[1]['legal_cells'] and 'C3' not in trace[1]['legal_cells']


def test_loss_context_preserves_history_and_labels_outcomes():
    from app.games.tic_tac_toe.coaching import loss_context
    moves=[{'player':'X','action':'place_A1'}, {'player':'O','action':'place_B2'},
           {'player':'X','action':'place_B1'}, {'player':'O','action':'place_C2'},
           {'player':'X','action':'place_C1'}]
    loss={'game_id':'loss','starting_player':'X','outcome':'win','winner':'X','moves':moves}
    win={'game_id':'win','starting_player':'O','outcome':'win','winner':'O','moves':[]}
    draw={'game_id':'draw','starting_player':'O','outcome':'draw','winner':None,'moves':[]}
    request={'games':[loss,win,draw], 'included_game_ids':['loss','win','draw'], 'previous_strategy':['win','block']}
    context=loss_context(request)
    assert [g['result_for_O'] for g in context['games']]==['loss','win','draw']
    assert context['games'][0]['moves']==moves
    assert all('turns' not in g for g in (loss,win,draw))
    assert len(context['winning_lines'])==8 and ['A1','B2','C3'] in context['winning_lines']
    assert context['included_game_ids']==request['included_game_ids']
    assert context['previous_strategy']==request['previous_strategy']


def test_loss_context_rejects_inconsistent_move_sequence():
    from app.games.tic_tac_toe.coaching import enrich_game
    with pytest.raises(ValueError,match='player sequence'):
        enrich_game({'starting_player':'X','moves':[{'player':'O','action':'place_B2'}]})


def test_optional_coach_threat_facts_identify_the_fork_before_the_final_block():
    from app.games.tic_tac_toe.coaching import enrich_game
    cells=['A1','B2','C3','C1','A3','B3','A2']
    moves=[{'player':'X' if i%2==0 else 'O','action':'place_'+cell} for i,cell in enumerate(cells)]
    game={'starting_player':'X','moves':moves}
    trace=enrich_game(game,include_threats=True)['turns']
    assert trace[1]['move_number']==4 and trace[1]['chosen_cell']=='C1'
    assert trace[1]['next_X_move']=='A3'
    assert trace[1]['X_winning_cells_before_O_move']==[]
    assert trace[1]['X_winning_cells_after_reply']==['A2','B3']
    assert trace[1]['O_winning_cells_after_reply']==[]
    assert trace[2]['X_winning_cells_before_O_move']==['A2','B3']
    assert trace[2]['X_winning_cells_after_O_move']==['A2']
    assert 'X_winning_cells_after_reply' not in enrich_game(game)['turns'][1]


def test_numbered_coach_limits_still_apply():
    for text in ('\n'.join(f'{i}. rule' for i in range(1,14)), '1. '+ 'x'*161):
        with pytest.raises(ValueError): parse_coach_output(text,'update')


def test_diagnosis_precedes_revision_and_preserves_history():
    request={'task':'update','previous_strategy':['win','block'],'games':[{'game_id':'loss'}],
             'included_game_ids':['loss'],'winning_lines':[['A1','B1','C1']]}
    records=[]; seen=[]; diagnoses=[]
    def invoke(payload):
        seen.append(payload)
        return {'summary':'Mistake: an avoidable fork.'} if payload['task']=='diagnose' else {'strategy':['win','block','prevent forks']}
    response=diagnose_then_coach(request,invoke,records.append,on_diagnosis=diagnoses.append)
    assert [p['task'] for p in seen]==['diagnose','update']
    assert diagnoses==[response['diagnosis']]
    assert seen[1]['loss_analysis']==response['diagnosis']['summary']
    assert seen[1]['previous_strategy']==request['previous_strategy']
    assert seen[1]['games']==request['games']
    assert 'loss_analysis' not in request
    assert response['included_game_ids']==['loss']
    assert response['stages']==records


@pytest.mark.parametrize('diagnosis',[{'summary':''},{'summary':'x','truncated':True},{'summary':'x'*2049}])
def test_failed_diagnosis_prevents_revision(diagnosis):
    calls=[]; records=[]
    def invoke(payload):
        calls.append(payload['task']); return diagnosis
    with pytest.raises(ValueError,match='diagnosis'):
        diagnose_then_coach({'games':[{'game_id':'loss'}],'winning_lines':[]},invoke,records.append)
    assert calls==['diagnose'] and records[0]['error']


def test_diagnosis_visible_before_update_and_survives_failure_resume(qapp,tmp_path,monkeypatch):
    c=controller(qapp,tmp_path); c.ready=True; c.start=lambda:None
    play(c,['A1','A2','B1','B2','C1'])
    gate=threading.Event(); text='Mistake: O failed to prevent a fork. Alternative: a legal side.'
    def invoke(_,request):
        if request['task']=='diagnose':return {'summary':text}
        gate.wait(3); raise RuntimeError('Revision failed')
    monkeypatch.setattr('app.core.session.shared_coach',invoke)
    c._after_move()
    w=Window(c)
    try:
        wait_for(qapp,lambda:c.coach_diagnosis==text)
        w.refresh()
        assert c.busy and 'COACH DIAGNOSIS\n'+text in w.diag.toPlainText()
        assert w.phase_label.text()=='Updating strategy'
    finally:
        gate.set(); wait_for(qapp,lambda:not c.busy)
    assert c.coaching_failed and c.learning_store.ledger()['revision']==0
    saved=tmp_path/'sessions'/c.session_id/'session.json'
    restored=controller(qapp,tmp_path); restored.restore(saved)
    assert restored.coach_diagnosis==text
    assert 'Revision failed' in restored.coach_diagnostics
    before=restored.inflight
    restored._diagnosis_ready('stale',{'summary':'wrong game'})
    assert restored.inflight==before and restored.coach_diagnosis==text
    c.ready=False; w.close()
