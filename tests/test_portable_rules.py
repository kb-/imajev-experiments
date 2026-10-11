"""Portable rules seed isolated interactive runs and survive session resume."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.config import Config
from app.core.registry import get_game
from app.core.session import SessionController
from app.storage.atomic import atomic_json
from app.storage.coached import CoachedStore
from app.storage.rules import load_rules, save_rules, rule_selection, validate_rule_arguments
from app.ui.window import Window
from test_coached import play
from test_protocol import INK
from test_session import Fake, wait_for


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_portable_round_trip_has_no_history_or_model_requirements(tmp_path, game_id):
    path = tmp_path / 'nested' / 'rules.json'
    save_rules(path, game_id, [' first ', 'second'])
    assert json.loads(path.read_text()) == {'version': 1, 'game': game_id, 'strategy': ['first', 'second']}
    assert load_rules(path, game_id) == ['first', 'second']
    other = 'boku' if game_id == 'tic_tac_toe' else 'tic_tac_toe'
    with pytest.raises(ValueError, match='Rules are for'):
        load_rules(path, other)


@pytest.mark.parametrize('value', [None, [], {}, {'version': True, 'game': 'boku', 'strategy': ['win']},
    {'version': 2, 'game': 'boku', 'strategy': ['win']},
    {'version': 1, 'game': 'boku', 'strategy': []},
    {'version': 1, 'game': 'boku', 'strategy': ['x' * 161]},
    {'version': 1, 'game': 'boku', 'strategy': [42]},
    {'version': 1, 'game': 'boku', 'strategy': ['win'], 'history': []}])
def test_invalid_portable_file_is_rejected(tmp_path, value):
    path = tmp_path / 'rules.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_rules(path, 'boku')
    path.write_text('{"broken":')
    with pytest.raises(ValueError):
        load_rules(path, 'boku')


def make_controller(tmp_path, game_id='tic_tac_toe', source=None):
    config = Config(game=game_id, prompt_variant='coached_quoted', learning_directory=tmp_path,
                    directory=tmp_path / 'ordinary-sessions')
    return SessionController(get_game(game_id), Fake(), config, rules_source=source)


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
@pytest.mark.parametrize('kind', ['file', 'basic'])
def test_explicit_rules_seed_once_preserve_old_history_and_resume(qapp, tmp_path, game_id, kind):
    game = get_game(game_id)
    policy = game.session_policy.coaching
    old = CoachedStore(policy.storage_root(tmp_path), policy)
    ledger = old.ledger()
    ledger.update(revision=5, strategy=['old rule'], consumed_game_ids=['old-game'], revisions=[{'old': True}])
    atomic_json(old.root / 'strategy.json', ledger)
    atomic_json(old.root / 'games' / 'old.json', {'old': 'history'})
    old_files = {path: path.read_bytes() for path in old.root.rglob('*.json')}
    path = tmp_path / 'rules.json'
    save_rules(path, game_id, ['imported', 'ordered'])
    source = rule_selection(game, path if kind == 'file' else None, kind == 'basic')
    c = make_controller(tmp_path, game_id, source)
    context = c.learning_contexts[game_id].copy()
    root = c.learning_store.root
    assert root != old.root and root.is_relative_to(tmp_path / 'runs')
    expected = ['imported', 'ordered'] if kind == 'file' else policy.initial_strategy
    assert c.strategy == expected and c.strategy_revision == 0
    assert c.learning_store.ledger()['revisions'] == []
    assert c.learning_store.ledger()['consumed_game_ids'] == []
    assert not (root / 'games').exists()
    assert all(path.read_bytes() == value for path, value in old_files.items())
    c.new_game()
    assert c.learning_store.root == root and c.learning_contexts[game_id] == context
    saved = tmp_path / 'session.json'
    atomic_json(saved, c.record())
    path.unlink()
    restored = make_controller(tmp_path, game_id)
    restored.restore(saved)
    assert restored.learning_contexts[game_id] == context
    assert restored.learning_store.root == root and restored.strategy == expected
    assert restored.record()['coaching']['rule_origin'] == source['origin']
    assert restored.record()['coaching']['run_id'] == context['run_id']
    restored.new_game()
    assert restored.learning_store.root == root


def test_legacy_resume_uses_original_learning_directory(qapp, tmp_path):
    c = make_controller(tmp_path)
    record = c.record()
    assert record['coaching'].pop('run_id') is None
    record['coaching'].pop('rule_origin')
    path = tmp_path / 'session.json'
    atomic_json(path, record)
    restored = make_controller(tmp_path)
    restored.restore(path)
    assert restored.learning_store.root == tmp_path


def test_learning_contexts_remain_separate_when_switching_games(qapp, tmp_path):
    c = make_controller(tmp_path)
    c.select_rules('basic')
    ttt = c.learning_store.root
    c.select_game(get_game('boku'))
    c.configure_unstarted_game('coached_quoted', 0)
    c.select_rules('basic')
    boku = c.learning_store.root
    assert boku != ttt
    c.select_game(get_game('tic_tac_toe'))
    assert c.coached and c.learning_store.root == ttt
    c.select_game(get_game('boku'))
    assert c.coached and c.learning_store.root == boku
    assert c.record()['coaching']['run_id'] == c.learning_contexts['boku']['run_id']


def test_imported_rules_can_continue_coaching_and_export_next_game_rules(qapp, tmp_path, monkeypatch):
    c = make_controller(tmp_path, source={'strategy': ['imported'], 'origin': 'Transfer'})
    c.ready = True
    c.start = lambda: None
    root = c.learning_store.root
    contexts = c.learning_contexts.copy()
    calls = []
    def coach(_, request):
        calls.append(request)
        return {'summary': 'Mistake: failed to block.'} if request['task'] == 'diagnose' else {'strategy': ['learned first', 'learned second']}
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    assert calls[0]['previous_strategy'] == ['imported']
    assert c.strategy == ['imported'] and c.next_strategy['strategy'] == ['learned first', 'learned second']
    w = Window(c)
    export = tmp_path / 'export.json'
    monkeypatch.setattr('app.ui.window.QFileDialog.getSaveFileName', lambda *_: (str(export), ''))
    w.save_rules()
    assert load_rules(export, c.game.id) == ['learned first', 'learned second']
    assert 'learned first' in w.strategy_label.text() and 'PREVIEW' in w.strategy_label.text()
    c.select_rules('continue')
    c.new_game()
    assert c.strategy_revision == 1 and c.strategy == ['learned first', 'learned second']
    assert c.learning_contexts == contexts and c.learning_store.root == root
    w.close()


def test_ui_pending_rules_apply_before_opponent_opening(qapp, tmp_path, monkeypatch):
    c = make_controller(tmp_path)
    c.ready = True
    c.start = lambda: None
    c.new_game()
    w = Window(c)
    game_id = c.session_id
    c.set_pending(INK)
    w.basic_rules_button.click()
    root = c.learning_store.root
    assert c.session_id == game_id and c.pending == INK and c.state.starting_player == 'X'
    assert len(list((tmp_path / 'runs').iterdir())) == 1
    assert 'Basic rules' in w.strategy_label.text() and 'PREVIEW' not in w.strategy_label.text()
    c.state = c.game.apply_action(c.state, 'place_A1')
    path = tmp_path / 'shared.json'
    save_rules(path, c.game.id, ['selected before opening'])
    monkeypatch.setattr('app.ui.window.QFileDialog.getOpenFileName', lambda *_: (str(path), ''))
    w.load_rules_button.click()
    assert c.learning_store.root == root and c.strategy != ['selected before opening']
    assert 'PREVIEW' in w.strategy_label.text() and 'selected before opening' in w.explanation.toPlainText()
    assert 'selected before opening' not in w.diag.toPlainText()
    export = tmp_path / 'pending.json'
    c.export_rules(export)
    assert load_rules(export, c.game.id) == ['selected before opening']
    w.continue_rules_button.click()
    assert not c.pending_rules and c.learning_store.root == root
    w.load_rules_button.click()
    path.unlink()  # A selection is already validated and frozen.
    c.client.gate.clear()
    try:
        w.new_game()
        assert c.state.starting_player == 'O' and c.busy
        assert c.learning_store.root != root
        assert c.strategy == ['selected before opening'] and not c.pending_rules
        wait_for(qapp, lambda: len(c.client.calls) == 1)
        assert c.client.calls[0]['state']['strategy'] == ['selected before opening']
    finally:
        c.client.gate.set()
        wait_for(qapp, lambda: not c.busy)
        w.close()


def test_queued_rules_wait_for_coached_mode_before_being_applied(qapp, tmp_path):
    c = make_controller(tmp_path)
    c.configure_unstarted_game('quoted', 0)
    c.select_rules('basic')
    assert not (tmp_path / 'runs').exists()
    c.new_game()
    assert not (tmp_path / 'runs').exists()
    c.configure_unstarted_game('coached_quoted', 0)
    assert c.coached and c.strategy_revision == 0 and not c.pending_rules
    assert len(list((tmp_path / 'runs').iterdir())) == 1


def test_invalid_load_or_failed_seed_keeps_current_run(qapp, tmp_path, monkeypatch):
    c = make_controller(tmp_path)
    path = tmp_path / 'bad.json'
    save_rules(path, 'boku', ['wrong game'])
    root, strategy = c.learning_store.root, c.strategy.copy()
    with pytest.raises(ValueError):
        c.select_rules('file', path)
    assert not c.pending_rules and c.learning_store.root == root and c.strategy == strategy
    monkeypatch.setattr('app.storage.atomic.os.replace', lambda *_: (_ for _ in ()).throw(OSError('disk failed')))
    with pytest.raises(OSError):
        c.select_rules('basic')
    assert not c.learning_contexts and c.learning_store.root == root and c.strategy == strategy
    assert not list((tmp_path / 'runs').rglob('*.tmp'))


@pytest.mark.parametrize('mode,resume,rules_file,basic,save', [
    ('quoted', False, None, True, None), ('legacy', False, Path('rules.json'), False, None),
    ('quoted', False, None, False, Path('out.json')),
    ('coached_quoted', True, Path('rules.json'), False, None),
    ('coached_quoted', True, None, True, None)])
def test_rule_flags_require_coached_new_run(mode, resume, rules_file, basic, save):
    with pytest.raises(ValueError):
        validate_rule_arguments(SimpleNamespace(resume=resume, rules_file=rules_file, basic_rules=basic, save_rules=save), mode)


def test_launcher_rejects_rule_flags_before_creating_controller(qapp, tmp_path, monkeypatch):
    import app.main as launcher
    config = tmp_path / 'config.yaml'
    config.write_text('opponent:\n  prompt_variant: quoted\n')
    monkeypatch.setattr(sys, 'argv', ['imajev-game', '--config', str(config), '--basic-rules'])
    monkeypatch.setattr(launcher, 'QApplication', lambda _: qapp)
    errors = []
    monkeypatch.setattr(launcher.QMessageBox, 'critical', lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(launcher, 'SessionController', lambda *_args, **_kwargs: pytest.fail('Controller created before rule flags validated'))
    assert launcher.main() == 1
    assert 'Coached quoted' in errors[-1]
