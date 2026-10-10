from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from app.core.game_policy import SessionPolicy


@dataclass(frozen=True)
class Stroke:
    points: tuple[tuple[float, float], ...]
    width: float = .012
    color: str = '#297a70'


@dataclass(frozen=True)
class Action:
    id: str
    description: str


@dataclass(frozen=True)
class Outcome:
    kind: str = 'ongoing'
    winner: str | None = None
    line: tuple[tuple[float, float], tuple[float, float]] | None = None


@dataclass(frozen=True)
class Stone:
    x: float
    y: float
    radius: float
    player: str
    colors: tuple[str, str, str, str]


@dataclass(frozen=True)
class Scene:
    lines: tuple[tuple[float, float, float, float], ...] = ()
    labels: tuple[tuple[str, float, float], ...] = ()
    circles: tuple[tuple[float, float, float], ...] = ()
    strokes: tuple[Stroke, ...] = ()
    stones: tuple[Stone, ...] = ()
    pockets: tuple[tuple[float, float, float], ...] = ()
    highlights: tuple[tuple[float, float, float, str], ...] = ()
    background: str = '#faf8f1'
    label_color: str = '#929b93'
    line_width: float = .004
    # Render labels last with a background patch so ink cannot obscure their IDs.
    protect_labels: bool = False


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    unknown_probability: float
    abstained: bool

    @property
    def effective_probability(self) -> float:
        return self.probabilities[self.choice] * (1 - self.unknown_probability)


@dataclass(frozen=True)
class Reply:
    model: str
    answers: Mapping[str, ChoiceAnswer]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class Ticket:
    session_id: str
    state_revision: int
    request_id: str
    purpose: str


class Game(Protocol):
    id: str
    name: str
    human_player: str
    computer_player: str
    instruction: str
    session_policy: SessionPolicy
    def instruction_for(self, state: Any) -> str: ...
    def initial_state(self) -> Any: ...
    def current_player(self, state: Any) -> str: ...
    def revision(self, state: Any) -> int: ...
    def legal_actions(self, state: Any) -> tuple[Action, ...]: ...
    def apply_action(self, state: Any, action: str, drawing: tuple[Stroke, ...] = ()) -> Any: ...
    def outcome(self, state: Any) -> Outcome: ...
    def render(self, state: Any, drawing: tuple[Stroke, ...], purpose: str) -> Scene: ...
    def recognition_request(self, state: Any, drawing: tuple[Stroke, ...]) -> dict: ...
    def decode_recognition(self, state: Any, drawing: tuple[Stroke, ...], reply: Reply, threshold: float) -> str: ...
    def decision_request(self, state: Any, actions: tuple[Action, ...]) -> dict: ...
    def decode_decision(self, state: Any, reply: Reply) -> str: ...
    def encode_state(self, state: Any) -> dict: ...
    def decode_state(self, record: dict) -> Any: ...
