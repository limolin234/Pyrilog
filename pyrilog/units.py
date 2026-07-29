"""Pint-backed physical quantities with a stable modeling-language interface."""

from __future__ import annotations

from math import isclose
from typing import Any, Iterator, Mapping

import pint


Dimensions = tuple[tuple[str, float], ...]


_REGISTRY = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)


def _normalize(values: Mapping[str, float]) -> Dimensions:
    return tuple(sorted((name, float(power)) for name, power in values.items() if power != 0))


def _dimension_signature(value: pint.Quantity | pint.Unit) -> Dimensions:
    dimensionality = value.dimensionality
    return _normalize(
        {
            str(name).removeprefix("[").removesuffix("]"): float(power)
            for name, power in dimensionality.items()
        }
    )


class Unit:
    """A modeling unit backed by Pint's registry and conversion catalog."""

    __slots__ = ("_unit", "_name", "_algebraic_decibel")

    def __init__(self, unit: str | pint.Unit | Unit):
        if isinstance(unit, Unit):
            self._unit = unit._unit
            self._name = unit._name
            self._algebraic_decibel = unit._algebraic_decibel
            return
        if isinstance(unit, str) and unit in {"dB", "decibel"}:
            # In compact-model relations dB is an algebraic attenuation count,
            # e.g. 2*dB/cm enters 10**(-loss*length/20).  Letting Pint
            # linearize this compound unit would silently change that formula.
            self._unit = _REGISTRY.dimensionless
            self._name = "dB"
            self._algebraic_decibel = True
            return
        self._unit = _REGISTRY.Unit(unit)
        self._name = f"{self._unit:~}"
        self._algebraic_decibel = False

    @classmethod
    def _derived(cls, unit: pint.Unit, name: str, *, algebraic_decibel: bool = False) -> Unit:
        result = object.__new__(cls)
        result._unit = unit
        result._name = name
        result._algebraic_decibel = algebraic_decibel
        return result

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> Dimensions:
        return _dimension_signature(self._unit)

    def __rmul__(self, value: int | float | complex) -> Quantity:
        return Quantity(_REGISTRY.Quantity(value, self._unit), display_unit=self)

    def __mul__(self, other: Unit):
        if not isinstance(other, Unit):
            return NotImplemented
        return Unit._derived(
            self._unit * other._unit,
            f"{self.name} * {other.name}",
            algebraic_decibel=self._algebraic_decibel or other._algebraic_decibel,
        )

    def __truediv__(self, other: Unit):
        if not isinstance(other, Unit):
            return NotImplemented
        return Unit._derived(
            self._unit / other._unit,
            f"{self.name} / {other.name}",
            algebraic_decibel=self._algebraic_decibel or other._algebraic_decibel,
        )

    def __rtruediv__(self, value: int | float | complex) -> Quantity:
        inverse = Unit._derived(
            _REGISTRY.dimensionless / self._unit,
            f"1 / {self.name}",
            algebraic_decibel=self._algebraic_decibel,
        )
        return Quantity(_REGISTRY.Quantity(value, inverse._unit), display_unit=inverse)

    def __pow__(self, power: int | float) -> Unit:
        return Unit._derived(
            self._unit**power,
            f"{self.name} ** {power}",
            algebraic_decibel=self._algebraic_decibel,
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Unit)
            and self._unit == other._unit
            and self._algebraic_decibel == other._algebraic_decibel
        )

    def __hash__(self) -> int:
        return hash((self._unit, self._algebraic_decibel))

    def __repr__(self) -> str:
        return self.name


class Quantity:
    """A Pint quantity adapted for symbolic expressions and stable compilation."""

    __slots__ = ("_quantity", "_display_unit")

    def __init__(self, quantity: pint.Quantity | Quantity, *, display_unit: Unit | None = None):
        if isinstance(quantity, Quantity):
            self._quantity = quantity._quantity
            self._display_unit = display_unit or quantity._display_unit
        else:
            self._quantity = quantity
            self._display_unit = display_unit or Unit(quantity.units)

    @property
    def si_value(self) -> int | float | complex:
        return self._quantity.to_base_units().magnitude

    @property
    def dimensions(self) -> Dimensions:
        return _dimension_signature(self._quantity)

    @property
    def display_unit(self) -> Unit:
        return self._display_unit

    @property
    def value(self) -> int | float | complex:
        return self._quantity.to(self._display_unit._unit).magnitude

    def to(self, unit: Unit) -> Quantity:
        if not isinstance(unit, Unit):
            raise TypeError("Quantity.to() expects a Pyrilog Unit")
        return Quantity(self._quantity.to(unit._unit), display_unit=unit)

    def __add__(self, other: Any):
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(self._quantity + as_quantity(other)._quantity)

    __radd__ = __add__

    def __sub__(self, other: Any):
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(self._quantity - as_quantity(other)._quantity)

    def __rsub__(self, other: Any):
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(as_quantity(other)._quantity - self._quantity)

    def __mul__(self, other: Any):
        if isinstance(other, Unit):
            return Quantity(
                self._quantity * other._unit,
                display_unit=self.display_unit * other,
            )
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(self._quantity * as_quantity(other)._quantity)

    __rmul__ = __mul__

    def __truediv__(self, other: Any):
        if isinstance(other, Unit):
            return Quantity(
                self._quantity / other._unit,
                display_unit=self.display_unit / other,
            )
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(self._quantity / as_quantity(other)._quantity)

    def __rtruediv__(self, other: Any):
        if not is_quantity_value(other):
            return NotImplemented
        return Quantity(as_quantity(other)._quantity / self._quantity)

    def __pow__(self, power: int | float):
        return Quantity(self._quantity**power, display_unit=self.display_unit**power)

    def __neg__(self):
        return Quantity(-self._quantity, display_unit=self.display_unit)

    def __lt__(self, other: Any) -> bool:
        return bool(self._quantity < as_quantity(other)._quantity)

    def __le__(self, other: Any) -> bool:
        return bool(self._quantity <= as_quantity(other)._quantity)

    def __eq__(self, other: object) -> bool:
        if not is_quantity_value(other):
            return NotImplemented
        try:
            candidate = as_quantity(other)
            if self.dimensions != candidate.dimensions:
                return False
            return isclose(abs(self.si_value - candidate.si_value), 0.0, abs_tol=1e-15)
        except (pint.DimensionalityError, TypeError):
            return False

    def __repr__(self) -> str:
        return f"{self.value} {self.display_unit.name}".rstrip()


class UnitNamespace:
    """Attribute namespace for every unit known by Pint's registry."""

    def __getattr__(self, name: str) -> Unit:
        try:
            return Unit(name)
        except pint.UndefinedUnitError as error:
            raise AttributeError(name) from error

    def __getitem__(self, name: str) -> Unit:
        try:
            return Unit(name)
        except pint.UndefinedUnitError as error:
            raise KeyError(name) from error

    def Quantity(self, magnitude: int | float | complex, unit: Unit | str = "") -> Quantity:
        target = unit if isinstance(unit, Unit) else Unit(unit or "dimensionless")
        return Quantity(_REGISTRY.Quantity(magnitude, target._unit), display_unit=target)


class _UnitLookup(Mapping[str, Unit]):
    def __getitem__(self, name: str) -> Unit:
        return u[name]

    def __iter__(self) -> Iterator[str]:
        return iter(_REGISTRY)

    def __len__(self) -> int:
        return len(_REGISTRY)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            u[name]
            return True
        except KeyError:
            return False


def as_quantity(value: int | float | complex | Quantity | pint.Quantity) -> Quantity:
    if isinstance(value, Quantity):
        return value
    if isinstance(value, pint.Quantity):
        if str(value.units) in {"decibel", "dB"}:
            return value.magnitude * u.dB
        if value._REGISTRY is not _REGISTRY:
            value = _REGISTRY.Quantity(value.magnitude, str(value.units))
        return Quantity(value)
    if isinstance(value, (int, float, complex)) and not isinstance(value, bool):
        return Quantity(_REGISTRY.Quantity(value, _REGISTRY.dimensionless))
    raise TypeError(f"expected a number or Quantity, got {type(value).__name__}")


def is_quantity_value(value: object) -> bool:
    return isinstance(value, (Quantity, pint.Quantity, int, float, complex)) and not isinstance(value, bool)


u = UnitNamespace()
UNIT_REGISTRY: Mapping[str, Unit] = _UnitLookup()

# Compatibility aliases remain public while model files migrate to the compact u namespace.
dimensionless = u.dimensionless
s = u.s
ns = u.ns
ps = u.ps
m = u.m
cm = u.cm
um = u.um
nm = u.nm
A = u.A
mA = u.mA
V = u.V
mV = u.mV
ohm = u.ohm
kohm = u.kohm
W = u.W
mW = u.mW
K = u.K
degC = u.degC
J = u.J
uJ = u.uJ
rad = u.rad
deg = u.deg
dB = u.dB
dBm = u.dBm
Hz = u.Hz
GHz = u.GHz
F = u.F
pF = u.pF
H = u.H
S = u.S


__all__ = [
    "Dimensions", "Quantity", "Unit", "as_quantity", "is_quantity_value", "u",
    "dimensionless", "s", "ns", "ps", "m", "cm", "um", "nm", "A", "mA",
    "V", "mV", "ohm", "kohm", "W", "mW", "K", "degC", "J", "uJ", "rad",
    "deg", "dB", "dBm", "Hz", "GHz", "F", "pF", "H", "S",
]
