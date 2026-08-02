"""Standard devices with explicit native SPICE lowering metadata."""

from __future__ import annotations

from dataclasses import dataclass

from .expressions import ddt
from .model import Device, eport, external, param
from .units import A, F, H, S, V, ohm, pF


@dataclass(frozen=True)
class SpicePrimitiveSpec:
    """Reserved compiler contract for an exact native SPICE primitive."""

    designator: str
    parameter: str | None = None
    ports: tuple[str, ...] = ("p", "n")
    model_type: str | None = None
    model_parameters: tuple[tuple[str, str], ...] = ()


class Resistor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("R", "resistance")

    p = eport()
    n = eport()
    resistance = param(1e3 * ohm, min=0 * ohm)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == resistance * p.i,
    )


class Capacitor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("C", "capacitance")

    p = eport()
    n = eport()
    capacitance = param(1 * pF, min=0 * F)
    relation = (
        p.i + n.i == 0,
        p.i == capacitance * ddt(p.v - n.v),
    )


class Inductor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("L", "inductance")

    p = eport()
    n = eport()
    inductance = param(1e-9 * H, min=0 * H)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == inductance * ddt(p.i),
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
        p.i == dc,
    )


class VoltageControlledVoltageSource(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec(
        "E", "gain", ports=("p", "n", "cp", "cn")
    )

    p = eport()
    n = eport()
    cp = eport()
    cn = eport()
    gain = param(1.0)
    relation = (
        p.i + n.i == 0,
        cp.i == 0,
        cn.i == 0,
        p.v - n.v == gain * (cp.v - cn.v),
    )


class VoltageControlledCurrentSource(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec(
        "G", "transconductance", ports=("p", "n", "cp", "cn")
    )

    p = eport()
    n = eport()
    cp = eport()
    cn = eport()
    transconductance = param(1 * S)
    relation = (
        p.i + n.i == 0,
        cp.i == 0,
        cn.i == 0,
        p.i == transconductance * (cp.v - cn.v),
    )


class NPN(Device):
    """Native SPICE NPN transistor using the backend's standard Q model."""

    __pyrilog_spice__ = SpicePrimitiveSpec(
        "Q",
        ports=("collector", "base", "emitter"),
        model_type="NPN",
        model_parameters=(
            ("IS", "saturation_current"),
            ("BF", "forward_beta"),
            ("BR", "reverse_beta"),
            ("VAF", "forward_early_voltage"),
        ),
    )

    collector = eport()
    base = eport()
    emitter = eport()
    saturation_current = param(1e-14 * A, min=0 * A)
    forward_beta = param(100.0, min=0.0)
    reverse_beta = param(1.0, min=0.0)
    forward_early_voltage = param(100 * V, min=0 * V)


class PNP(Device):
    """Native SPICE PNP transistor using the backend's standard Q model."""

    __pyrilog_spice__ = SpicePrimitiveSpec(
        "Q",
        ports=("collector", "base", "emitter"),
        model_type="PNP",
        model_parameters=(
            ("IS", "saturation_current"),
            ("BF", "forward_beta"),
            ("BR", "reverse_beta"),
            ("VAF", "forward_early_voltage"),
        ),
    )

    collector = eport()
    base = eport()
    emitter = eport()
    saturation_current = param(1e-14 * A, min=0 * A)
    forward_beta = param(100.0, min=0.0)
    reverse_beta = param(1.0, min=0.0)
    forward_early_voltage = param(100 * V, min=0 * V)


class Diode(Device):
    """Native SPICE diode with a local ``.model`` card."""

    __pyrilog_spice__ = SpicePrimitiveSpec(
        "D",
        ports=("p", "n"),
        model_type="D",
        model_parameters=(
            ("IS", "saturation_current"),
            ("N", "emission_coefficient"),
        ),
    )

    p = eport()
    n = eport()
    saturation_current = param(1e-14 * A, min=0 * A)
    emission_coefficient = param(1.0, min=0.0)


VCVS = VoltageControlledVoltageSource
VCCS = VoltageControlledCurrentSource


__all__ = [
    "Capacitor",
    "CurrentSource",
    "Diode",
    "Inductor",
    "NPN",
    "PNP",
    "Resistor",
    "VCCS",
    "VCVS",
    "VoltageControlledCurrentSource",
    "VoltageControlledVoltageSource",
    "VoltageSource",
]
