"""Local GGUF readout bridge; optional decider-ai dependencies live outside the GUI."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import threading
import time


def create_server(model, identity, port, context_limit=4096, provenance=None):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value):
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/health':
                self.reply(200, {'status': 'ok'})
            elif self.path == '/v1/status':
                self.reply(200, {'busy': lock.locked(), 'model': identity})
            elif self.path == '/v1/models':
                self.reply(200, {'data': [{'id': identity, 'architecture': {
                    'input_modalities': ['text'], 'output_modalities': ['decisions']},
                    'provenance': provenance or {}}]})
            else:
                self.reply(404, {'error': 'Unknown endpoint.'})

        def do_POST(self):
            if self.path != '/v1/systemone':
                self.reply(404, {'error': 'Unknown endpoint.'})
                return
            if not lock.acquire(blocking=False):
                self.reply(409, {'error': 'The previous GPU request is still running.'})
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 65536:
                    self.reply(413, {'error': 'Request must fit within 64 KiB.'})
                    return
                request = json.loads(self.rfile.read(length))
                if request.get('model', identity) != identity:
                    raise ValueError('Requested model does not match this service.')
                if request.get('images') or request.get('files'):
                    raise ValueError('Mapika Decider profiles support text only.')
                questions = request['questions']
                if not isinstance(questions, dict) or not questions:
                    raise ValueError('Nonempty questions mapping required.')
                for q in questions.values():
                    if q.get('type') != 'choice' or not 2 <= len(q.get('criteria', {})) <= 255:
                        raise ValueError('This bridge requires choice questions with 2–255 options.')
                # Use the publisher's exact prompt builder without state truncation.
                # This private boundary is isolated behind the pinned service dependency.
                _, _, items = model._system_one_items(request['state'], questions, True, 1_000_000, 'state_first', False)
                if any(len(item['ids']) > context_limit for item in items):
                    raise ValueError('Complete decision prompt exceeds context_limit; no state was truncated.')
                started = time.monotonic()
                result = model.system_one(request['state'], questions, max_state_tokens=1_000_000,
                                          max_fwd_tokens=context_limit, layout='state_first', isolated=False)
                self.reply(200, {**result, 'runtime_model': result.get('model'), 'model': identity,
                                 'inference_seconds': time.monotonic() - started,
                                 'runtime_provenance': provenance or {}})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
            except Exception as exc:
                self.reply(500, {'error': str(exc)})
            finally:
                lock.release()

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--context-limit', type=int, default=4096)
    parser.add_argument('--gpu-layers', type=int, default=-1)
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from app.inference.provenance import file_identity
    for name in ('tokenizer_config.json', 'tokenizer.json', 'decider_config.json'):
        if not (args.model.parent / name).is_file():
            parser.error('Missing local Decider tokenizer/calibration asset: ' + name)
    from decider.infer import Decider
    model = Decider(str(args.model.resolve()), gguf_options={
        'n_gpu_layers': args.gpu_layers, 'n_ctx': args.context_limit, 'n_seq_max': 1})
    provenance = {'decider_ai': version('decider-ai'), 'llama_cpp_python': version('llama-cpp-python'),
                  'assets': [file_identity(args.model), *[file_identity(args.model.parent / name)
                             for name in ('tokenizer_config.json', 'tokenizer.json', 'decider_config.json')]]}
    server = create_server(model, args.identity, args.port, args.context_limit, provenance)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
