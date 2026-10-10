import argparse
from dataclasses import replace
import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QMessageBox
from app.config import load_config
from app.logging_setup import configure_logging
from app.core.registry import get_game
from app.core.session import SessionController
from app.inference.imajev_client import ImajevClient
from app.ui.window import Window
from app.inference.service_manager import ServiceManager


def main():
    parser = argparse.ArgumentParser(description='Draw a move and play against your local Imajev model.')
    parser.add_argument('--external-inference', action='store_true', help='Use a separately managed service; Ollama coaching needs managed inference')
    parser.add_argument('--config', type=Path, default=Path('config.yaml'))
    parser.add_argument('--resume', type=Path, help='Continue from a saved session JSON record')
    parser.add_argument('--debug-input', action='store_true', help='Log mouse press/move/release and stroke decisions locally')
    parser.add_argument('--log-file', type=Path, help='Write rotating diagnostics to this file')
    args = parser.parse_args()
    log_path = configure_logging(args.debug_input, args.log_file)
    application = QApplication(sys.argv[:1])
    application.setApplicationName('Imajev Drawing Game')
    try:
        config = load_config(args.config)
        config = replace(config, external_inference=config.external_inference or args.external_inference)
        if config.coach_backend == 'ollama' and config.external_inference:
            raise ValueError('Ollama coaching requires managed inference.')
        if args.debug_input:
            config = replace(config, diagnostics=True, save_sessions=True)
        game = get_game(config.game)
        controller = SessionController(game, ImajevClient(config), config)
    except (OSError, ValueError, TypeError) as exc:
        QMessageBox.critical(None, 'Configuration error', str(exc))
        return 1
    manager = ServiceManager(config)
    manager.progress = controller.progress.emit
    controller.service_manager = manager
    controller.client.manager = manager
    if args.resume:
        try:
            controller.restore(args.resume)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.critical(None, 'Cannot resume game', str(exc))
            return 1
    controller.log_path = log_path
    window = Window(controller)
    labels = ['Resumed' if args.resume else 'Debug' if args.debug_input else '']
    if controller.policy.tactics_caption(config):
        labels.append('Tactics')
    labels = [label for label in labels if label]
    if labels:
        window.setWindowTitle(f'Imajev · Drawing game [{" · ".join(labels)}]')
    window.show()
    try:
        return application.exec()
    finally:
        if not controller.shutdown_done:
            manager.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
