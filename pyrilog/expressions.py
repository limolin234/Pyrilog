"""Backend-neutral symbolic expressions and local equality constraints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .units import Quantity, UNIT_REGISTRY, Unit, as_quantity


class Expr:
    __hash__ = object.__hash__

    def __add__(self, other: Any) -> Expr:
        return binary_expr("+", self, other)

    def __radd__(self, other: Any) -> Expr:
        return binary_expr("+", other, self)

    def __sub__(self, other: Any) -> Expr:
        return binary_expr("-", self, other)

    def __rsub__(self, other: Any) -> Expr:
        return binary_expr("-", other, self)

    def __mul__(self, other: Any) -> Expr:
        return binary_expr("*", self, other)

    def __rmul__(self, other: Any) -> Expr:
        return binary_expr("*", other, self)

    def __truediv__(self, other: Any) -> Expr:
        return binary_expr("/", self, other)

    def __rtruediv__(self, other: Any) -> Expr:
        return binary_expr("/", other, self)

    def __pow__(self, other: Any) -> Expr:
        return binary_expr("**", self, other)

    def __rpow__(self, other: Any) -> Expr:
        return binary_expr("**", other, self)

    def __neg__(self) -> Expr:
        return UnaryExpr("-", self)

    def __eq__(self, other: object) -> Relation:  # type: ignore[override]
        return Relation(self, ensure_expr(other))

    def __bool__(self) -> bool:
        raise TypeError("symbolic expressions cannot be used as Python booleans")

    def __getattr__(self, name: str) -> Expr:
        if name in UNIT_REGISTRY:
            return UnitViewExpr(self, UNIT_REGISTRY[name])
        if name in {"abs", "phase", "power"}:
            return FunctionExpr(name, (self,))
        raise AttributeError(name)


@dataclass(frozen=True, eq=False)
class ConstantExpr(Expr):
    value: Quantity


@dataclass(frozen=True, eq=False)
class BinaryExpr(Expr):
    operator: str
    left: Expr
    right: Expr


@dataclass(frozen=True, eq=False)
class UnaryExpr(Expr):
    operator: str
    operand: Expr


@dataclass(frozen=True, eq=False)
class FunctionExpr(Expr):
    name: str
    arguments: tuple[Expr, ...]
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, eq=False)
class UnitViewExpr(Expr):
    expression: Expr
    unit: Unit


@dataclass(frozen=True, eq=False)
class Relation:
    left: Expr
    right: Expr

    @property
    def residual(self) -> Expr:
        return self.left - self.right


def is_expr(value: object) -> bool:
    return isinstance(value, Expr)


def ensure_expr(value: Any) -> Expr:
    if isinstance(value, Expr):
        return value
    return ConstantExpr(as_quantity(value))


def binary_expr(operator: str, left: Any, right: Any) -> Expr:
    return BinaryExpr(operator, ensure_expr(left), ensure_expr(right))


def exp(value: Any) -> Expr:
    return FunctionExpr("exp", (ensure_expr(value),))


def ddt(value: Any) -> Expr:
    return FunctionExpr("ddt", (ensure_expr(value),))


def delay(value: Any, tau: Any, *, initial: Any) -> Expr:
    return FunctionExpr(
        "delay",
        (ensure_expr(value), ensure_expr(tau), ensure_expr(initial)),
    )


def piecewise(*branches: tuple[Any, Any], otherwise: Any) -> Expr:
    arguments: list[Expr] = []
    for condition, value in branches:
        arguments.extend((ensure_expr(condition), ensure_expr(value)))
    arguments.append(ensure_expr(otherwise))
    return FunctionExpr("piecewise", tuple(arguments))


def walk(expression: Expr) -> Iterable[Expr]:
    yield expression
    if isinstance(expression, BinaryExpr):
        yield from walk(expression.left)
        yield from walk(expression.right)
    elif isinstance(expression, UnaryExpr):
        yield from walk(expression.operand)
    elif isinstance(expression, FunctionExpr):
        for argument in expression.arguments:
            yield from walk(argument)
    elif isinstance(expression, UnitViewExpr):
        yield from walk(expression.expression)


pi = math.pi
