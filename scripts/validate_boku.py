"""Live Boku protocol/recognition probe. Uses synthetic ink, not a playing-strength test."""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import time

from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.contracts import Stroke
from app.games.boku.game import Boku
from app.games.boku.geometry import CENTERS, SPACING
from app.inference.imajev_client import ImajevClient
from app.inference.opponents import create_opponent
from app.inference.service_manager import ServiceManager
from app.ui.rendering import observation_png


def drawing(cell, symbol):
    x, y = CENTERS[cell]
    r = .26*SPACING
    if symbol == 'X':
        return (Stroke(((x-r,y-r),(x+r,y+r)),.007),Stroke(((x+r,y-r),(x-r,y+r)),.007))
    return (Stroke(tuple((x+r*math.cos(i*math.tau/40),y+r*math.sin(i*math.tau/40)) for i in range(41)),.007),)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--external-inference', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('/tmp/imajev-boku-validation.json'))
    args = parser.parse_args()
    app = QApplication([])
    config = replace(load_config(args.config),external_inference=args.external_inference)
    manager = ServiceManager(config)
    client = ImajevClient(config)
    client.manager = manager
    opponent = create_opponent(config, client, manager)
    game = Boku()
    results = []
    def recognize(state, cell, symbol):
        ink = drawing(cell,symbol)
        request = game.recognition_request(state,ink)
        started = time.monotonic()
        response = client.decide(request,observation_png(game.render(state,ink,'recognition')))
        row = {'purpose':'recognition','expected_cell':cell,'symbol':symbol,
               'seconds':time.monotonic()-started,'request_bytes':len(json.dumps(request)),
               'answers':{k:{'choice':v.choice,'effective_probability':v.effective_probability,
                             'probability':v.probabilities[v.choice], 'unknown_probability':v.unknown_probability,
                             'abstained':v.abstained} for k,v in response.answers.items()}}
        results.append(row)
        action = game.decode_recognition(state,ink,response,config.threshold)
        row['accepted_action'] = action
        return game.apply_action(state,action,ink)

    def decide(state):
        request = game.decision_request(state,game.legal_actions(state))
        started = time.monotonic()
        response = opponent.choose_move(request,observation_png(game.render(state,(),'decision')))
        row = {'purpose':'decision','phase':state.phase,'seconds':time.monotonic()-started,'request_bytes':len(json.dumps(request)),
               'choice':response.choice,'abstained':response.abstained}
        results.append(row)
        return game.apply_action(state,game.decode_decision(state,response))

    try:
        manager.start()
        empty = game.initial_state()
        client.warmup(game.recognition_request(empty,()),observation_png(game.render(empty,(),'recognition')))
        state = decide(recognize(empty,'F5','O'))
        state = decide(recognize(state,'K5','O'))
        capture = empty
        for cell in ('A1','A2','K1','A3','A4'):
            capture = game.apply_action(capture,'place_'+cell)
        capture = recognize(capture,'A2','X')
        assert capture.forbidden == 'A2' and capture.next_player == 'White'
        capture = decide(capture)
        assert capture.next_player == 'Black'
        computer_capture = empty
        for cell in ('A2','A1','A3','K1','K2','A4'):
            computer_capture = game.apply_action(computer_capture,'place_'+cell)
        assert computer_capture.phase == 'capture' and computer_capture.next_player == 'White'
        computer_capture = decide(computer_capture)
        assert computer_capture.next_player == 'Black' and computer_capture.forbidden
        # Verify both live states remain replayable.
        assert game.decode_state(game.encode_state(state)) == state
        assert game.decode_state(game.encode_state(capture)) == capture
        assert game.decode_state(game.encode_state(computer_capture)) == computer_capture
    except Exception as exc:
        results.append({'error':str(exc)})
        raise
    finally:
        args.output.write_text(json.dumps(results,indent=2))
        manager.shutdown()
        print(json.dumps(results,indent=2))
    return app


if __name__ == '__main__':
    main()
