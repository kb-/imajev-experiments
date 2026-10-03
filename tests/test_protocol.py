from copy import deepcopy
import json
import httpx
import pytest
from app.config import Config, load_config
from app.inference.imajev_client import ImajevClient, InferenceError, parse_reply
from app.games.tic_tac_toe.game import TicTacToe, State, geometry_matches
from app.core.contracts import Stroke


def answer(request, choices, unknown=0., abstained=False):
    return {'model': 'imajev-2b', 'answers': {
        name: {'type': 'choice', 'choice': choices[name],
               'probabilities': {key: float(key == choices[name]) for key in q['criteria']},
               'unknown_probability': unknown, 'abstained': abstained}
        for name, q in request['questions'].items()}}


INK = (Stroke(((.06, .06), (.25, .25))), Stroke(((.25, .06), (.06, .25))))
game = TicTacToe()
request = game.recognition_request(State(), INK)


@pytest.mark.parametrize('field,value', [('unknown_probability', float('nan')), ('unknown_probability', -1),
                                        ('abstained', 0), ('choice', 'Z'), ('probabilities', {}), ('type', 'noul')])
def test_malformed_answer(field, value):
    raw = answer(request, {'symbol': 'X', 'cell': 'A1'})
    raw['answers']['symbol'][field] = value
    with pytest.raises(InferenceError):
        parse_reply(raw, request, 'imajev-2b')


def test_unknown_scales_recognition():
    reply = parse_reply(answer(request, {'symbol': 'X', 'cell': 'A1'}, unknown=.2), request, 'imajev-2b')
    assert reply.answers['symbol'].effective_probability == .8
    with pytest.raises(ValueError, match='could not identify'):
        game.decode_recognition(State(), INK, reply, .85)


@pytest.mark.parametrize('symbol,cell,message', [('O', 'A1', 'cross'), ('invalid', 'A1', 'could not'), ('X', 'invalid', 'could not'), ('X', 'B1', 'spans')])
def test_specific_rejections(symbol, cell, message):
    reply = parse_reply(answer(request, {'symbol': symbol, 'cell': cell}), request, 'imajev-2b')
    with pytest.raises(ValueError, match=message):
        game.decode_recognition(State(), INK, reply, .85)


def test_geometry_is_length_weighted():
    assert geometry_matches(INK, 0)
    assert not geometry_matches(INK + (Stroke(((.5, .5), (.8, .8))),), 0)
    assert not geometry_matches((Stroke(((-.1, .1), (.2, .2))),), 0)
    assert not geometry_matches((Stroke(((.1, .1),)),), 0)


def test_recognition_snapshot_excludes_committed_marks():
    state = game.apply_action(State(), 'place_B2', INK)
    assert game.render(state, INK, 'recognition').strokes == INK
    assert len(game.render(state, INK, 'display').strokes) == 4
    assert game.render(state, INK, 'decision').strokes == INK


def test_decision_does_not_use_handwriting_threshold():
    state = game.apply_action(State(), 'place_A1')
    req = game.decision_request(state, game.legal_actions(state))
    raw = answer(req, {'move': 'place_B1'}, unknown=.9)
    reply = parse_reply(raw, req, 'imajev-2b')
    assert game.decode_decision(state, reply) == 'place_B1'
    raw['answers']['move']['abstained'] = True
    with pytest.raises(ValueError):
        game.decode_decision(state, parse_reply(raw, req, 'imajev-2b'))


def test_loopback_only(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('imajev:\n  endpoint: https://example.com/v1/systemone')
    with pytest.raises(ValueError, match='loopback'):
        load_config(path)


def test_multipart_transport(monkeypatch):
    real_client = httpx.Client
    def handle(req):
        if req.url.path == '/v1/status':
            return httpx.Response(404)
        assert req.url.path == '/v1/systemone'
        assert b'name="request"' in req.content
        assert b'name="image"' in req.content
        assert b'filename="board.png"' in req.content
        return httpx.Response(200, json=answer(request, {'symbol': 'X', 'cell': 'A1'}))
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=transport, **kw))
    assert ImajevClient(Config()).decide(request, b'png').answers['cell'].choice == 'A1'


def test_busy_service_never_receives_retry_image(monkeypatch):
    real_client = httpx.Client
    calls = []
    def handle(req):
        calls.append(req.method)
        return httpx.Response(200, json={'busy': True})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(InferenceError, match='still running'):
        ImajevClient(Config()).decide(request, b'png')
    assert calls == ['GET']


def test_timeout_blocks_unverifiable_retry(monkeypatch):
    real_client = httpx.Client
    calls = []
    def handle(req):
        calls.append(req.method)
        if req.method == 'GET':
            return httpx.Response(404)
        raise httpx.ReadTimeout('timeout', request=req)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    client = ImajevClient(Config())
    with pytest.raises(InferenceError, match='timed out'):
        client.decide(request, b'png')
    with pytest.raises(InferenceError, match='guarded'):
        client.decide(request, b'png')
    assert calls == ['GET', 'POST', 'GET']


@pytest.mark.parametrize('status,body,message', [(500, 'CUDA out of memory', 'GPU ran out'), (503, 'unavailable', 'HTTP 503'), (200, '{garbage', 'malformed JSON')])
def test_transport_failures(monkeypatch, status, body, message):
    real_client = httpx.Client
    def handle(req):
        return httpx.Response(404) if req.method == 'GET' else httpx.Response(status, text=body)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(InferenceError, match=message):
        ImajevClient(Config()).decide(request, b'png')


def test_model_mismatch_and_nonfinite_distribution():
    raw = answer(request, {'symbol': 'X', 'cell': 'A1'})
    with pytest.raises(InferenceError, match='Expected'):
        parse_reply(raw, request, 'different-model')
    raw['answers']['symbol']['probabilities']['X'] = float('inf')
    with pytest.raises(InferenceError, match='probability'):
        parse_reply(raw, request, 'imajev-2b')


def test_warmup_uses_image_and_validates_ready_model(monkeypatch):
    real_client = httpx.Client
    calls = []
    def handle(req):
        calls.append(req.url.path)
        if req.url.path == '/v1/models':
            return httpx.Response(200, json={'loaded': True, 'model': 'imajev-2b', 'backend': 'torch'})
        if req.url.path == '/v1/status':
            return httpx.Response(200, json={'busy': False, 'runtime_id': 'test', 'provenance': {'upstream_commit': 'test'}})
        assert b'filename="board.png"' in req.content
        return httpx.Response(200, json=answer(request, {'symbol': 'invalid', 'cell': 'invalid'}, abstained=True))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    reply = ImajevClient(Config()).warmup(request, b'png')
    assert reply.answers['symbol'].abstained
    assert reply.raw['service']['runtime_id'] == 'test'
    assert calls == ['/v1/models', '/v1/status', '/v1/systemone']


def test_rejection_identifies_low_symbol_confidence():
    raw = answer(request, {'symbol': 'X', 'cell': 'A1'})
    raw['answers']['symbol']['probabilities'] = {'X': .6, 'O': .1, 'invalid': .3}
    reply = parse_reply(raw, request, 'imajev-2b')
    with pytest.raises(ValueError, match='X scored 60.0%; 85% is required'):
        game.decode_recognition(State(), INK, reply, .85)


def test_rejection_identifies_low_cell_confidence():
    raw = answer(request, {'symbol': 'X', 'cell': 'A1'})
    raw['answers']['cell']['probabilities'] = {cell: (.8 if cell == 'A1' else .2 if cell == 'invalid' else 0) for cell in request['questions']['cell']['criteria']}
    reply = parse_reply(raw, request, 'imajev-2b')
    with pytest.raises(ValueError, match='A1 scored 80.0%; 85% is required'):
        game.decode_recognition(State(), INK, reply, .85)


def test_recognition_abstention_and_circle_correction_are_specific():
    raw = answer(request, {'symbol': 'X', 'cell': 'A1'}, abstained=True)
    with pytest.raises(ValueError, match='abstained on the symbol'):
        game.decode_recognition(State(), INK, parse_reply(raw, request, 'imajev-2b'), .85)
    raw = answer(request, {'symbol': 'O', 'cell': 'A1'})
    raw['answers']['cell']['unknown_probability'] = .5
    with pytest.raises(ValueError, match='cross instead of a circle'):
        game.decode_recognition(State(), INK, parse_reply(raw, request, 'imajev-2b'), .85)
