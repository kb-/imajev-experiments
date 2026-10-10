"""Validation shared by ordered strategy lists."""
import json


def validate_strategy(strategy):
    if (not isinstance(strategy, list) or not 1 <= len(strategy) <= 12
            or any(not isinstance(rule, str) or not rule.strip() or len(rule) > 160 for rule in strategy)
            or len(json.dumps(strategy, ensure_ascii=False).encode()) > 2048):
        raise ValueError('Strategy must contain 1–12 nonempty rules, at most 160 characters each and 2048 bytes overall.')
    return [rule.strip() for rule in strategy]


