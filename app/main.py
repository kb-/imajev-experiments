import argparse
import logging
import os
from dataclasses import replace
import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QMessageBox
from app.config import load_config
from app.logging_setup import configure_logging
from app.core.registry import get_game
from app.core.session import SessionController
from app.inference.recognition import create_recognizer
from app.ui.window import Window
from app.inference.service_manager import ServiceManager

logger = logging.getLogger('app.main')


def main():
    parser = argparse.ArgumentParser(description='Draw a move and play against your local Imajev model.')
    parser.add_argument('--external-inference', action='store_true', help='Use a separately managed service; Ollama coaching needs managed inference')
    parser.add_argument('--config', type=Path, default=Path('configs/default.yaml'),
                        help='YAML profile (default: configs/default.yaml; see configs/README.md)')
    parser.add_argument('--resume', type=Path, help='Continue from a saved session JSON record')
    parser.add_argument('--debug-input', action='store_true', help='Log mouse press/move/release and stroke decisions locally')
    parser.add_argument('--log-file', type=Path, help='Write rotating diagnostics to this file')
    args = parser.parse_args()
    log_path = configure_logging(args.debug_input, args.log_file)
    logger.info('Application starting pid=%s config=%s external_inference=%s',
                os.getpid(), args.config, args.external_inference)
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
        controller = SessionController(game, create_recognizer(config), config)
    except (OSError, ValueError, TypeError) as exc:
        QMessageBox.critical(None, 'Configuration error', str(exc))
        return 1
    manager = ServiceManager(config)
    manager.progress = controller.progress.emit
    controller.service_manager = manager
    controller.recognizer.manager = manager
    if args.resume:
        try:
            controller.restore(args.resume)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.critical(None, 'Cannot resume game', str(exc))
            return 1
    controller.log_path = log_path
    window = Window(controller)
    # Cleanup hides the window before its final close, so Qt may not emit
    # lastWindowClosed. Explicitly quit once owned runtimes have stopped.
    window.closed.connect(application.quit)
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
        logger.info('Qt event loop exited pid=%s shutdown_done=%s busy=%s inflight=%s',
                    os.getpid(), controller.shutdown_done, controller.busy, controller.inflight)
        if not controller.shutdown_done:
            logger.info('Final service cleanup starting')
            manager.shutdown()
        logger.info('Application exit cleanup complete pid=%s', os.getpid())


if __name__ == '__main__':
    raise SystemExit(main())
