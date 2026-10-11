"""Portable, model-independent ordered rules; never include learning history."""
import json
from pathlib import Path

from app.storage.atomic import atomic_json
from app.storage.strategy import validate_strategy


def rules_document(game, strategy):
    if game not in ('tic_tac_toe', 'boku'):
        raise ValueError('Unsupported rules game.')
    return {'version': 1, 'game': game, 'strategy': validate_strategy(strategy)}


def load_rules(path, game):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or set(value) != {'version', 'game', 'strategy'}
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Unsupported rule file: expected version, game and strategy.')
    if value['game'] != game:
        raise ValueError(f'Rules are for {value["game"]}, not {game}.')
    return rules_document(game, value['strategy'])['strategy']


def save_rules(path, game, strategy):
    atomic_json(path, rules_document(game, strategy))


def rule_selection(game, rules_file=None, basic_rules=False):
    """Validate an explicit source before creating a learning run."""
    if rules_file is not None:
        return {'strategy': load_rules(rules_file, game.id), 'origin': str(Path(rules_file).resolve())}
    if basic_rules:
        return {'strategy': validate_strategy(game.session_policy.coaching.initial_strategy),
                'origin': 'Basic rules'}
    return None


def add_rule_arguments(parser):
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--rules-file', type=Path, help='Seed Coached quoted with portable rules in a new run')
    source.add_argument('--basic-rules', action='store_true', help='Seed Coached quoted with initial rules in a new run')


def validate_rule_arguments(args, mode):
    selected = args.rules_file is not None or args.basic_rules
    if selected and args.resume:
        raise ValueError('Changing rules requires a new run; rule-source flags cannot be used with --resume.')
    if (selected or getattr(args, 'save_rules', None)) and mode != 'coached_quoted':
        raise ValueError('Rule options require Coached quoted (coached_quoted); select that mode for a new run.')
