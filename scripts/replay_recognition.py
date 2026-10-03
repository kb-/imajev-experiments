"""Replay a saved recognition image against the current or original prompt."""
import argparse
import base64
import json
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.contracts import Stroke
from app.core.registry import get_game
from app.inference.imajev_client import ImajevClient, InferenceError
from app.ui.rendering import observation_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path, help='Exported JSON or sessions/<id>/session.json')
    parser.add_argument('--config', type=Path, default=Path('config.yaml'))
    parser.add_argument('--request-id', help='Default: last recognition request')
    parser.add_argument('--original-prompt', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    app = QApplication([])
    config = load_config(args.config)
    record = json.loads(args.session.read_text())
    events = [event for event in record['events'] if event.get('ticket', {}).get('purpose') == 'recognition'
              and (not args.request_id or event['ticket']['request_id'] == args.request_id)]
    if not events:
        parser.exit(1, 'No matching recognition request in this session.\n')
    event = events[-1]
    game = get_game(event['state']['game'])
    state = game.decode_state(event['state'])
    drawing = tuple(Stroke(tuple(tuple(p) for p in s['points']), s['width'], s['color']) for s in event['drawing'])
    if event.get('image_png_base64'):
        image = base64.b64decode(event['image_png_base64'], validate=True)
    elif event.get('image'):
        image = (args.session.parent / event['image']).read_bytes()
    else:
        image = observation_png(game.render(state, drawing, 'recognition'), config.observation_size)
    request = event['request'] if args.original_prompt else game.recognition_request(state, drawing)
    try:
        reply = ImajevClient(config).decide(request, image)
    except InferenceError as exc:
        parser.exit(1, f'Inference failed: {exc}\n')
    proposed, rejection = None, None
    try:
        proposed = game.decode_recognition(state, drawing, reply, config.threshold)
    except ValueError as exc:
        rejection = str(exc)
    result = {'version': 1, 'source_request_id': event['ticket']['request_id'], 'original_prompt': args.original_prompt,
              'threshold': config.threshold, 'request': request, 'reply': dict(reply.raw),
              'proposed_action': proposed, 'rejection': rejection}
    text = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(json.dumps({'answers': dict(reply.raw['answers']), 'proposed_action': proposed, 'rejection': rejection}, indent=2))


if __name__ == '__main__':
    main()
