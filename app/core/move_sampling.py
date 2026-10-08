"""Sample legal moves from Imajev preferences, without changing its raw reply."""
from dataclasses import replace
import math

from app.config import validate_move_temperature


def select_move(game, state, reply, temperature, rng):
    temperature = validate_move_temperature(temperature)
    # Validate the original answer first: sampling never bypasses abstention.
    proposed = game.decode_decision(state, reply)
    if temperature == 0:
        return proposed, proposed, {}
    answer = reply.answers['move']
    probabilities = {}
    for choice, probability in answer.probabilities.items():
        if not math.isfinite(probability) or probability < 0:
            raise ValueError('Cannot sample invalid move probabilities.')
        if probability == 0:
            continue
        candidate = replace(reply, answers={**reply.answers, 'move': replace(answer, choice=choice)})
        try:
            action = game.decode_decision(state, candidate)
        except ValueError:
            continue  # Authoritative game validation excludes illegal choices.
        probabilities[action] = probabilities.get(action, 0) + probability
    if not probabilities:
        raise ValueError('Imajev returned no positive probability for a legal move.')
    # Subtract before scaling to avoid overflow at very small temperatures.
    logs = {action: math.log(p) for action, p in probabilities.items()}
    maximum = max(logs.values())
    weights = {action: math.exp((value - maximum) / temperature) for action, value in logs.items()}
    total = sum(weights.values())
    distribution = {action: weight / total for action, weight in weights.items()}
    selected = rng.choices(list(distribution), weights=list(distribution.values()), k=1)[0]
    return proposed, selected, distribution
