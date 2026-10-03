"""Local application diagnostics; detailed pointer traces are explicitly enabled."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(debug_input=False, log_file=None):
    logger = logging.getLogger('app')
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG if debug_input else logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if debug_input or log_file:
        path = Path(log_file or 'logs/input-debug.log').resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding='utf-8')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.info('Local diagnostic log: %s; detailed mouse logging=%s', path, debug_input)
        return path
    return None
