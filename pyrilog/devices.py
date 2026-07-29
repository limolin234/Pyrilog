"""Standard devices with explicit native SPICE lowering metadata."""

from __future__ import annotations

from dataclasses import dataclass

from .expressions import ddt
from .model import Device, eport, external, param
from .units import A, F, H, V, ohm, pF


@dataclass(frozen=True)
class SpicePrimitiveSpec:
    """Reserved compiler contract for an exact two-terminal SPICE primitive."""

    designator: str
    parameter: str


class Resistor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("R", "resistance")

    p = eport()
    n = eport()
    resistance = param(1e3 * ohm, min=0 * ohm)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == resistance * p.i.i,
    )


class Capacitor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("C", "capacitance")

    p = eport()
    n = eport()
    capacitance = param(1 * pF, min=0 * F)
    relation = (
        p.i + n.i == 0,
        p.i.i == capacitance * ddt(p.v - n.v),
    )


class Inductor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("L", "inductance")

    p = eport()
    n = eport()
    inductance = param(1e-9 * H, min=0 * H)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == inductance * ddt(p.i.i),
    )


class VoltageSource(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("V", "dc")

    p = eport()
    n = eport()
    dc = external(0 * V)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == dc,
    )


class CurrentSource(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("I", "dc")

    p = eport()
    n = eport()
    dc = external(0 * A)
    relation = (
        p.i + n.i == 0,
        p.i.i == dc,
    )


__all__ = [
    "Capacitor",
    "CurrentSource",
    "Inductor",
    "Resistor",
    "VoltageSource",
]
