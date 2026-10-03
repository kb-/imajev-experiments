"""Explicit development fake, only used by this smoke check. No production fallback."""
import argparse
import time
from PyQt6.QtWidgets import QApplication
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.imajev_client import parse_reply
from app.ui.window import Window


class DevelopmentFake:
    def warmup(self, request, png):
        return self.decide(request, png)
    def decide(self, request, png):
        choices = {'symbol': 'invalid', 'cell': 'invalid'} if 'symbol' in request['questions'] else {'move': next(iter(request['questions']['move']['criteria']))}
        raw = {'model': Config.expected_model, 'answers': {name: {
            'type': 'choice', 'choice': choices[name], 'probabilities': {key: float(key == choices[name]) for key in question['criteria']},
            'unknown_probability': 0, 'abstained': False} for name, question in request['questions'].items()}}
        return parse_reply(raw, request, Config.expected_model)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshot', default='/tmp/imajev-window.png')
    args = parser.parse_args()
    app = QApplication([])
    controller = SessionController(TicTacToe(), DevelopmentFake(), Config())
    window = Window(controller)
    window.show()
    deadline = time.monotonic() + 5
    while not controller.ready:
        app.processEvents()
        if time.monotonic() > deadline:
            raise RuntimeError('UI startup failed')
        time.sleep(.01)
    app.processEvents()
    assert controller.phase == 'human'
    assert not window.submit_button.isEnabled()
    assert window.grab().save(args.screenshot)
    window.close()
    print('Qt window starts, warms up, and renders successfully.')


if __name__ == '__main__':
    main()
