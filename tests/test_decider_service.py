import threading

import httpx

from app.inference.decider_service import create_server


def test_bridge_preserves_ids_rejects_context_and_serializes_gpu():
    entered, release = threading.Event(), threading.Event()
    class Model:
        def _system_one_items(self, *args):
            return None, None, [{'ids': list(range(args[0]['tokens']))}]
        def system_one(self, state, questions, **kwargs):
            assert kwargs['layout'] == 'state_first'
            assert kwargs['max_state_tokens'] == 1_000_000
            entered.set()
            release.wait(5)
            return {'model': 'publisher', 'answers': {'move': {'type': 'choice', 'choice': 'place_A1'}}}
    server = create_server(Model(), 'local', 0, context_limit=32)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_address[1]}'
    request = {'model': 'local', 'state': {'tokens': 33}, 'questions': {'move': {
        'type': 'choice', 'instructions': 'Choose', 'criteria': {'place_A1': None, 'place_B1': None}}}}
    responses = []
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.post(url + '/v1/systemone', json=request).status_code == 400
            assert not entered.is_set()
            request['state']['tokens'] = 3
            worker = threading.Thread(target=lambda: responses.append(client.post(url + '/v1/systemone', json=request)))
            worker.start()
            assert entered.wait(2)
            assert client.get(url + '/v1/status').json()['busy'] is True
            assert client.post(url + '/v1/systemone', json=request).status_code == 409
            release.set()
            worker.join(5)
            result = responses[0].json()
            assert result['model'] == 'local' and result['runtime_model'] == 'publisher'
            assert result['answers']['move']['choice'] == 'place_A1'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
