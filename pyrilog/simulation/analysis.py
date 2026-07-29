"""Analysis declarations."""

from dataclasses import dataclass

from ..units import Quantity, s


@dataclass(frozen=True)
class OperatingPoint:
    pass


@dataclass(frozen=True)
class Transient:
    stop: Quantity
    step: Quantity

    def __post_init__(self):
        if self.stop.dimensions != s.dimensions or self.step.dimensions != s.dimensions:
            raise ValueError("transient stop and step must have time dimensions")
        if self.stop.si_value <= 0 or self.step.si_value <= 0:
            raise ValueError("transient stop and step must be positive")
        if self.step.si_value > self.stop.si_value:
            raise ValueError("transient step cannot exceed stop time")
