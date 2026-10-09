from __future__ import annotations

from dataclasses import dataclass


# Forward Gray-code sequence: 00 -> 01 -> 11 -> 10 -> 00.
_TRANSITIONS = {
    (0b00, 0b01): 1,
    (0b01, 0b11): 1,
    (0b11, 0b10): 1,
    (0b10, 0b00): 1,
    (0b00, 0b10): -1,
    (0b10, 0b11): -1,
    (0b11, 0b01): -1,
    (0b01, 0b00): -1,
}


@dataclass(slots=True)
class QuadratureDiagnostics:
    transitions: int = 0
    accepted_counts: int = 0
    illegal_transitions: int = 0
    duplicate_events: int = 0


class QuadratureDecoder:
    """Pure, unbounded Gray-code decoder with true x1/x2/x4 semantics."""

    def __init__(self, multiplier: int = 4, initial_a: int = 0, initial_b: int = 0) -> None:
        if multiplier not in (1, 2, 4):
            raise ValueError("Quadrature multiplier must be 1, 2 or 4")
        self.multiplier = multiplier
        self.state = self._state(initial_a, initial_b)
        self.count = 0
        self.diagnostics = QuadratureDiagnostics()

    @staticmethod
    def _state(a: int, b: int) -> int:
        return (int(bool(a)) << 1) | int(bool(b))

    def update(self, a: int, b: int) -> int:
        new_state = self._state(a, b)
        if new_state == self.state:
            self.diagnostics.duplicate_events += 1
            return 0
        previous = self.state
        self.state = new_state
        delta = _TRANSITIONS.get((previous, new_state))
        self.diagnostics.transitions += 1
        if delta is None:
            self.diagnostics.illegal_transitions += 1
            return 0
        a_changed = bool((previous ^ new_state) & 0b10)
        a_rising = not bool(previous & 0b10) and bool(new_state & 0b10)
        accepted = self.multiplier == 4 or (self.multiplier == 2 and a_changed) or (self.multiplier == 1 and a_rising)
        if not accepted:
            return 0
        self.count += delta
        self.diagnostics.accepted_counts += 1
        return delta

