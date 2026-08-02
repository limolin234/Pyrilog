"""Device reflection, typed ports, topology construction, and circuits."""

from __future__ import annotations

import contextvars
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from typing import Any, ClassVar, Iterable, get_origin

from .expressions import BinaryExpr, ConstantExpr, Expr, Relation, UnaryExpr, ddt, walk
from .units import J, K, W, Quantity, Unit, as_quantity, is_quantity_value, s


class ModelingError(Exception):
    pass


class TopologyError(ModelingError):
    pass


class ParameterError(ModelingError):
    pass


class BackendCapabilityError(ModelingError):
    pass


@dataclass(frozen=True)
class ParameterSpec:
    default: Any
    minimum: Any | None = None
    maximum: Any | None = None
    external: bool = False


def param(default: Any, *, min: Any | None = None, max: Any | None = None) -> ParameterSpec:
    return ParameterSpec(default, min, max)


def external(default: Any) -> ParameterSpec:
    return ParameterSpec(default, external=True)


@dataclass(frozen=True)
class LocalParameterSpec:
    default: Any


def localparam(default: Any) -> LocalParameterSpec:
    if not is_quantity_value(default):
        raise TypeError("localparam() expects a numeric or quantity value")
    return LocalParameterSpec(default)


@dataclass(frozen=True)
class InternalSpec:
    initial: Any


def val(initial: Any) -> InternalSpec:
    return InternalSpec(initial)


internal = val
state = val


@dataclass(frozen=True)
class ControllerOutputSpec:
    default: Quantity


def output(unit_or_default: Unit | Any) -> ControllerOutputSpec:
    if isinstance(unit_or_default, Unit):
        unit_or_default = 0 * unit_or_default
    if not is_quantity_value(unit_or_default):
        raise TypeError("output() expects a unit or numeric default value")
    return ControllerOutputSpec(as_quantity(unit_or_default))


class ParameterSymbol(Expr):
    def __init__(self, name: str, spec: ParameterSpec):
        self.name = name
        self.spec = spec

    def __get__(self, instance: Any | None, owner: type[Any]):
        if instance is None:
            return self
        return BoundParameter(instance, self)

    def __set__(self, instance: Any, value: Any) -> None:
        instance._set_parameter(self, value)

    def __repr__(self) -> str:
        return self.name


class BoundParameter(Expr):
    def __init__(self, owner: Any, symbol: ParameterSymbol):
        self.owner = owner
        self.symbol = symbol

    @property
    def value(self) -> Any:
        return self.owner._parameter_values[self.symbol.name]

    @property
    def external(self) -> bool:
        return self.symbol.spec.external

    def __repr__(self) -> str:
        return f"{self.owner.stable_id}.{self.symbol.name}"


class LocalParameterSymbol(Expr):
    def __init__(self, name: str, spec: LocalParameterSpec):
        self.name = name
        self.spec = spec

    def __get__(self, instance: Any | None, owner: type[Any]):
        if instance is None:
            return self
        return as_quantity(self.spec.default)

    def __set__(self, instance: Any, value: Any) -> None:
        raise ParameterError(f"localparam {self.name} is read-only")

    def __repr__(self) -> str:
        return self.name


class InternalSymbol(Expr):
    def __init__(self, name: str, spec: InternalSpec):
        self.name = name
        self.spec = spec

    def __get__(self, instance: Any | None, owner: type[Any]):
        if instance is None:
            return self
        return BoundInternal(instance, self)

    def __repr__(self) -> str:
        return self.name


class BoundInternal(Expr):
    def __init__(self, owner: Any, symbol: InternalSymbol):
        self.owner = owner
        self.symbol = symbol

    @property
    def initial(self) -> Any:
        value = self.owner._internal_initials[self.symbol.name]
        if value is _AMBIENT_INITIAL:
            if self.owner._builder is not None:
                return self.owner._builder.ambient_temperature
            return 300 * K
        return value

    def __repr__(self) -> str:
        return f"{self.owner.stable_id}.{self.symbol.name}"


class ControllerOutputSymbol:
    def __init__(self, name: str, spec: ControllerOutputSpec):
        self.name = name
        self.spec = spec

    def __get__(self, instance: Controller | None, owner: type[Controller]):
        if instance is None:
            return self
        return BoundControllerDeclaration(instance, self)


class BoundControllerDeclaration:
    def __init__(self, owner: Controller, symbol: ControllerOutputSymbol):
        self.owner = owner
        self.symbol = symbol

    def __repr__(self) -> str:
        return f"{self.owner.stable_id}.{self.symbol.name}"


class BoundControllerOutput(Expr):
    def __init__(self, call: ControlCall, symbol: ControllerOutputSymbol, index: int):
        self.call = call
        self.symbol = symbol
        self.index = index

    @property
    def dimensions(self):
        return self.symbol.spec.default.dimensions

    def bind(self, target: BoundParameter) -> None:
        self.call.bind(self, target)

    def __repr__(self) -> str:
        return f"{self.call.controller.stable_id}.{self.symbol.name}@call"


@dataclass(frozen=True)
class FeedbackBinding:
    controller: Controller
    inputs: tuple[Expr, ...]
    outputs: tuple[tuple[BoundControllerOutput, BoundParameter], ...]


class ControlCall:
    def __init__(self, controller: Controller, inputs: tuple[Expr, ...]):
        self.controller = controller
        self.inputs = inputs
        self.outputs = tuple(
            BoundControllerOutput(self, symbol, index)
            for index, symbol in enumerate(controller._output_symbols.values())
        )
        self._bindings: dict[int, BoundParameter] = {}
        self._committed = False
        self._aborted = False

    def __iter__(self):
        return iter(self.outputs)

    def __len__(self) -> int:
        return len(self.outputs)

    def __getattr__(self, name: str) -> BoundControllerOutput:
        for item in self.outputs:
            if item.symbol.name == name:
                return item
        raise AttributeError(name)

    def single_output(self) -> BoundControllerOutput:
        if len(self.outputs) != 1:
            raise ParameterError(
                f"{type(self.controller).__name__} has {len(self.outputs)} outputs; unpack them explicitly"
            )
        return self.outputs[0]

    def bind(self, output_ref: BoundControllerOutput, target: BoundParameter) -> None:
        if self._committed:
            raise TopologyError("controller call is already bound")
        if self._aborted:
            raise TopologyError("controller call was aborted by a failed output assignment")
        try:
            if output_ref.index in self._bindings:
                raise TopologyError(f"controller output {output_ref.symbol.name} is already bound")
            expected = output_ref.dimensions
            actual = as_quantity(target.symbol.spec.default).dimensions
            if expected != actual:
                raise ParameterError(
                    f"controller output {output_ref.symbol.name} has incompatible dimensions for {target}"
                )
            controller_builder = self.controller._builder
            if controller_builder is None or target.owner._builder is not controller_builder:
                raise TopologyError("controller and target parameter belong to different graphs")
            input_builders = _builders_in_expressions(self.inputs)
            if self.inputs and input_builders != {controller_builder}:
                raise TopologyError("controller inputs must belong to the same graph as the controller")
            candidate = {**self._bindings, output_ref.index: target}
            target_keys = [_parameter_key(bound) for bound in candidate.values()]
            if len(set(target_keys)) != len(target_keys):
                raise TopologyError("one feedback call cannot drive the same parameter twice")
            complete = len(candidate) == len(self.outputs)
            if complete:
                bindings = tuple((item, candidate[item.index]) for item in self.outputs)
                controller_builder.register_feedback(
                    FeedbackBinding(self.controller, self.inputs, bindings)
                )
        except ModelingError:
            self._abort()
            raise
        self._bindings = candidate
        self._committed = complete

    def _abort(self) -> None:
        self._bindings.clear()
        self._aborted = True
        self.controller._calls = [call for call in self.controller._calls if call is not self]


class PortQuantity(Expr):
    def __init__(self, port: PortTemplate | BoundPort, quantity: str):
        self.port = port
        self.quantity = quantity

    def __repr__(self) -> str:
        return f"{self.port}.{self.quantity}"


class FlowQuantity(PortQuantity):
    """An electrical current whose positive direction is into the device."""


class ThermalFlowQuantity(PortQuantity):
    """The existing thermal-power flow views, pending thermal API migration."""

    @property
    def i(self) -> Expr:
        return -self

    @property
    def o(self) -> Expr:
        return self


class PortTemplate:
    def __init__(self, domain: str, *, lazy: bool = False):
        self.domain = domain
        self.lazy = lazy
        self.name: str | None = None
        self.builder: GraphBuilder | None = None
        self.connection: Node | OpticalConnection | None = None

    def bind_name(self, name: str, builder: GraphBuilder) -> None:
        if self.name is None:
            self.name = name
        self.builder = builder

    def __get__(self, instance: Device | None, owner: type[Device]):
        if instance is None:
            return self
        return instance._ports[self.name]

    @property
    def v(self) -> PortQuantity:
        self._require("electrical", "v")
        return PortQuantity(self, "v")

    @property
    def i(self) -> PortQuantity:
        if self.domain == "electrical":
            return FlowQuantity(self, "i")
        if self.domain == "optical":
            return PortQuantity(self, "i")
        raise AttributeError("i")

    @property
    def o(self) -> Expr:
        if self.domain == "electrical":
            return -FlowQuantity(self, "i")
        if self.domain == "optical":
            return PortQuantity(self, "o")
        raise AttributeError("o")

    @property
    def t(self) -> PortQuantity:
        self._require("thermal", "t")
        return PortQuantity(self, "t")

    @property
    def p(self) -> PortQuantity:
        self._require("thermal", "p")
        return ThermalFlowQuantity(self, "p")

    def _require(self, domain: str, quantity: str) -> None:
        if self.domain != domain:
            raise AttributeError(quantity)

    def __or__(self, other: PortTemplate | BoundPort | Node):
        return connect_endpoints(self, other)

    def __repr__(self) -> str:
        return self.name or f"<{self.domain}-port>"


class BoundPort:
    def __init__(self, owner: Device, template: PortTemplate):
        self.owner = owner
        self.template = template
        self.domain = template.domain
        self.name = template.name
        self.connection: Node | OpticalConnection | None = None

    @property
    def v(self) -> PortQuantity:
        return self._quantity("electrical", "v")

    @property
    def i(self) -> PortQuantity:
        if self.domain == "electrical":
            return FlowQuantity(self, "i")
        if self.domain == "optical":
            return PortQuantity(self, "i")
        raise AttributeError("i")

    @property
    def o(self) -> Expr:
        if self.domain == "electrical":
            return -FlowQuantity(self, "i")
        return self._quantity("optical", "o")

    @property
    def t(self) -> PortQuantity:
        return self._quantity("thermal", "t")

    @property
    def p(self) -> PortQuantity:
        if self.domain != "thermal":
            raise AttributeError("p")
        return ThermalFlowQuantity(self, "p")

    def _quantity(self, domain: str, quantity: str) -> PortQuantity:
        if self.domain != domain:
            raise AttributeError(quantity)
        return PortQuantity(self, quantity)

    def __or__(self, other: PortTemplate | BoundPort | Node):
        return connect_endpoints(self, other)

    def __ior__(self, others: BoundPort | Node | Iterable[BoundPort | Node]):
        if self.domain == "optical":
            raise TopologyError("optical ports only support binary '|' connections")
        return _connect_conservative((self, *_endpoint_batch(others)))

    def __repr__(self) -> str:
        return f"{self.owner.stable_id}.{self.name}"


def eport() -> PortTemplate:
    return PortTemplate("electrical")


def oport() -> PortTemplate:
    return PortTemplate("optical")


def tport() -> PortTemplate:
    return PortTemplate("thermal")


class Node:
    def __init__(
        self,
        domain: str,
        *,
        reference: bool = False,
        fixed: bool = False,
        C: Any | None = None,
        T: Any | None = None,
        P: Any | None = None,
    ):
        if domain not in {"electrical", "thermal"}:
            raise ValueError(f"invalid node domain: {domain}")
        if domain == "thermal":
            if C is not None and _value_dimensions(C) != (J / K).dimensions:
                raise ParameterError("thermal node C must have heat-capacity dimensions")
            if T is not None and _value_dimensions(T) != K.dimensions:
                raise ParameterError("thermal node T must have temperature dimensions")
            if P is not None and _value_dimensions(P) != W.dimensions:
                raise ParameterError("thermal node P must have power dimensions")
        self.domain = domain
        self.reference = reference
        self.fixed = fixed
        self.C = C
        self.initial_T = T
        self.external_P = P
        self.ports: list[BoundPort | PortTemplate] = []
        self.builder: GraphBuilder | None = None
        self.stable_id = "unbound_node"
        self._parent: Node = self
        self._union_members: set[Node] = {self}

    def canonical(self) -> Node:
        if self._parent is not self:
            self._parent = self._parent.canonical()
        return self._parent

    @property
    def v(self) -> NodeQuantity:
        if self.domain != "electrical":
            raise AttributeError("v")
        return NodeQuantity(self, "v")

    @property
    def t(self) -> NodeQuantity:
        if self.domain != "thermal":
            raise AttributeError("t")
        return NodeQuantity(self, "t")

    @property
    def p(self) -> NodeQuantity:
        if self.domain != "thermal":
            raise AttributeError("p")
        return NodeQuantity(self, "p")

    @property
    def initial_temperature(self) -> Any | None:
        if self.domain != "thermal":
            return None
        if self.initial_T is not None:
            if isinstance(self.initial_T, Expr):
                return self.initial_T
            return as_quantity(self.initial_T)
        if self.builder is not None:
            return self.builder.ambient_temperature
        return 300 * K

    def __or__(self, other: BoundPort | PortTemplate | Node):
        return connect_endpoints(self, other)

    def __ior__(
        self,
        endpoints: BoundPort | PortTemplate | Node | Iterable[BoundPort | PortTemplate | Node],
    ):
        return _connect_conservative((self, *_endpoint_batch(endpoints)))

    def __repr__(self) -> str:
        return self.stable_id


class _AmbientNode(Node):
    def __init__(self, builder: GraphBuilder):
        super().__init__("thermal", fixed=True)
        self._ambient_builder = builder

    def _ensure_registered(self) -> None:
        if self.builder is None:
            self._ambient_builder.register_node(self, member_name="AMBIENT")

    @property
    def t(self) -> NodeQuantity:
        self._ensure_registered()
        return Node.t.fget(self)

    @t.setter
    def t(self, value: Any) -> None:
        quantity = self._ambient_builder.validate_ambient_temperature(value)
        self._ensure_registered()
        self._ambient_builder.ambient_temperature = quantity

    @property
    def initial_temperature(self) -> Quantity:
        return self._ambient_builder.ambient_temperature

    def __ior__(
        self,
        endpoints: BoundPort | PortTemplate | Node | Iterable[BoundPort | PortTemplate | Node],
    ):
        batch = _endpoint_batch(endpoints)
        if not batch:
            raise TopologyError("cannot connect an empty endpoint batch")
        if any(endpoint.domain != "thermal" for endpoint in batch):
            raise TopologyError("cannot connect endpoints from different physical domains")
        if _builder_for_endpoints((self, *batch)) is not self._ambient_builder:
            raise TopologyError("node and ports belong to different graphs")
        self._ensure_registered()
        return super().__ior__(batch)


class NodeQuantity(Expr):
    def __init__(self, node: Node, quantity: str):
        self.node = node
        self.quantity = quantity

    def __repr__(self) -> str:
        return f"{self.node.stable_id}.{self.quantity}"


class OpticalConnection:
    def __init__(self, left: BoundPort | PortTemplate, right: BoundPort | PortTemplate):
        self.left = left
        self.right = right
        self.builder: GraphBuilder | None = None
        self.stable_id = "unbound_optical_connection"

    def __repr__(self) -> str:
        return self.stable_id


class OpticalNode:
    """Named helper for one strict binary optical reference-plane link."""

    def __init__(self):
        self.builder: GraphBuilder | None = None
        self.connection: OpticalConnection | None = None

    def __ior__(self, ports: BoundPort | PortTemplate | Iterable[BoundPort | PortTemplate]):
        batch = _port_batch(ports)
        if len(batch) != 2:
            raise TopologyError("an optical node accepts exactly two ports")
        if batch[0].domain != "optical" or batch[1].domain != "optical":
            raise TopologyError("an optical node accepts only optical ports")
        builder = _builder_for_endpoints(batch)
        if self.connection is not None:
            raise TopologyError("optical node is already connected")
        if any(port.connection is not None for port in batch):
            raise TopologyError("an optical port is already connected")
        builder.begin_connections()
        connection = OpticalConnection(batch[0], batch[1])
        builder.register_optical(connection)
        batch[0].connection = connection
        batch[1].connection = connection
        self.builder = builder
        self.connection = connection
        return self


class GraphBuilder:
    def __init__(self, label: str):
        self.label = label
        self.ambient_temperature = 300 * K
        self.stage = "INSTANCE"
        self.devices: list[Device] = []
        self.controllers: list[Controller] = []
        self.feedbacks: list[FeedbackBinding] = []
        self.nodes: list[Node] = []
        self.optical_connections: list[OpticalConnection] = []
        self.members: OrderedDict[str, Any] = OrderedDict()
        self._device_counts: defaultdict[str, int] = defaultdict(int)
        self._controller_counts: defaultdict[str, int] = defaultdict(int)

    def validate_ambient_temperature(self, value: Any) -> Quantity:
        if self.stage == "COMPILED":
            raise TopologyError("compiled graphs are frozen")
        quantity = as_quantity(value)
        if quantity.dimensions != K.dimensions:
            raise ParameterError("ambient temperature must have temperature dimensions")
        return quantity

    def set_ambient_temperature(self, value: Any) -> None:
        self.ambient_temperature = self.validate_ambient_temperature(value)

    def register_device(self, device: Device, member_name: str | None = None) -> None:
        if self.stage != "INSTANCE":
            raise TopologyError("all devices must be instantiated before connections")
        if device._builder is not None and device._builder is not self:
            raise TopologyError("device already belongs to another graph")
        if device in self.devices:
            return
        base = _snake_case(type(device).__name__)
        self._device_counts[base] += 1
        local_id = member_name or f"{base}_{self._device_counts[base]}"
        device._attach(self, local_id)
        self.devices.append(device)
        if member_name is not None:
            self.members[member_name] = device

    def register_controller(self, controller: Controller, member_name: str | None = None) -> None:
        if self.stage == "COMPILED":
            raise TopologyError("compiled graphs are frozen")
        if controller._builder is not None and controller._builder is not self:
            raise TopologyError("controller already belongs to another graph")
        if controller in self.controllers:
            return
        base = _snake_case(type(controller).__name__)
        self._controller_counts[base] += 1
        local_id = member_name or f"{base}_{self._controller_counts[base]}"
        controller._builder = self
        controller.stable_id = local_id
        self.controllers.append(controller)
        if member_name is not None:
            self.members[member_name] = controller

    def register_feedback(self, feedback: FeedbackBinding) -> None:
        targets = [target for _, target in feedback.outputs]
        target_keys = [_parameter_key(target) for target in targets]
        if len(set(target_keys)) != len(target_keys):
            raise TopologyError("one feedback call cannot drive the same parameter twice")
        controlled = {
            _parameter_key(target)
            for existing in self.feedbacks
            for _, target in existing.outputs
        }
        conflict = next(
            (target for target in targets if _parameter_key(target) in controlled),
            None,
        )
        if conflict is not None:
            raise TopologyError(f"parameter {conflict} already has a feedback driver")
        self.begin_connections()
        self.feedbacks.append(feedback)

    def register_node(self, node: Node, member_name: str | None = None) -> None:
        if self.stage == "COMPILED":
            raise TopologyError("compiled graphs are frozen")
        if node.builder is not None and node.builder is not self:
            raise TopologyError("node already belongs to another graph")
        if node in self.nodes:
            if member_name is not None:
                if not (node.domain == "electrical" and node.reference):
                    node.stable_id = member_name
                self.members[member_name] = node
            return
        if node.domain == "electrical" and node.reference:
            if any(candidate.domain == "electrical" and candidate.reference for candidate in self.nodes):
                raise TopologyError("a graph can have only one electrical reference node")
        stable_id = (
            "0"
            if node.domain == "electrical" and node.reference
            else member_name or f"{node.domain}_node_{len(self.nodes) + 1}"
        )
        node.builder = self
        node.stable_id = stable_id
        self.nodes.append(node)
        if member_name is not None:
            self.members[member_name] = node

    def begin_connections(self) -> None:
        if self.stage == "COMPILED":
            raise TopologyError("compiled graphs are frozen")
        if self.stage == "INSTANCE":
            self.stage = "CONNECT"

    def register_optical(self, connection: OpticalConnection) -> None:
        self.begin_connections()
        connection.builder = self
        connection.stable_id = f"optical_connection_{len(self.optical_connections) + 1}"
        self.optical_connections.append(connection)


class _DeviceNamespace(dict[str, Any]):
    def __init__(self, label: str, enabled: bool):
        super().__init__()
        self.builder = GraphBuilder(label)
        self.enabled = enabled

    def __setitem__(self, key: str, value: Any) -> None:
        if not self.enabled or key.startswith("__") or key == "relation":
            super().__setitem__(key, value)
            return
        if isinstance(value, LocalParameterSpec):
            value = LocalParameterSymbol(key, value)
        elif isinstance(value, ParameterSpec):
            value = ParameterSymbol(key, value)
        elif isinstance(value, InternalSpec):
            value = InternalSymbol(key, value)
        elif is_quantity_value(value):
            value = ParameterSymbol(key, ParameterSpec(value))
        if isinstance(value, PortTemplate):
            value.bind_name(key, self.builder)
        elif isinstance(value, Device):
            self.builder.register_device(value, member_name=key)
        elif isinstance(value, Node):
            self.builder.register_node(value, member_name=key)
        super().__setitem__(key, value)


class DeviceMeta(type):
    @classmethod
    def __prepare__(metaclass, name: str, bases: tuple[type, ...], **kwargs: Any):
        enabled = any(isinstance(base, DeviceMeta) for base in bases)
        return _DeviceNamespace(name, enabled)

    def __new__(metaclass, name: str, bases: tuple[type, ...], namespace: _DeviceNamespace, **kwargs: Any):
        inherited_local_parameters = {
            key
            for base in bases
            for key in getattr(base, "_local_parameter_symbols", {})
        }
        overridden_local_parameters = inherited_local_parameters & set(namespace)
        if overridden_local_parameters:
            raise ParameterError(
                f"inherited localparams cannot be overridden: {sorted(overridden_local_parameters)}"
            )
        annotations = namespace.get("__annotations__", {})
        for key, annotation in annotations.items():
            value = namespace.get(key)
            is_classvar = get_origin(annotation) is ClassVar or (
                isinstance(annotation, str)
                and annotation.replace("typing.", "").startswith("ClassVar[")
            )
            if is_classvar and isinstance(value, (ParameterSymbol, LocalParameterSymbol)):
                dict.__setitem__(namespace, key, value.spec.default)
        cls = super().__new__(metaclass, name, bases, dict(namespace))
        parameters: OrderedDict[str, ParameterSymbol] = OrderedDict()
        local_parameters: OrderedDict[str, LocalParameterSymbol] = OrderedDict()
        internals: OrderedDict[str, InternalSymbol] = OrderedDict()
        ports: OrderedDict[str, PortTemplate] = OrderedDict()
        for base in bases:
            parameters.update(getattr(base, "_parameter_symbols", {}))
            local_parameters.update(getattr(base, "_local_parameter_symbols", {}))
            internals.update(getattr(base, "_internal_symbols", {}))
            ports.update(getattr(base, "_port_templates", {}))
        for key, value in namespace.items():
            if isinstance(value, ParameterSymbol):
                parameters[key] = value
                local_parameters.pop(key, None)
            elif isinstance(value, LocalParameterSymbol):
                local_parameters[key] = value
                parameters.pop(key, None)
            elif isinstance(value, InternalSymbol):
                internals[key] = value
            elif isinstance(value, PortTemplate):
                ports[key] = value
        relation_value = namespace.get("relation", ())
        if isinstance(relation_value, Relation):
            relations = (relation_value,)
        else:
            relations = tuple(relation_value)
        if any(not isinstance(item, Relation) for item in relations):
            raise TypeError(f"{name}.relation must contain only equality constraints")
        cls._parameter_symbols = parameters
        cls._local_parameter_symbols = local_parameters
        cls._internal_symbols = internals
        cls._port_templates = ports
        cls._relations = relations
        cls._definition = namespace.builder if namespace.enabled else None
        return cls

    def __call__(cls, *args: Any, **kwargs: Any):
        instance = super().__call__(*args, **kwargs)
        builder = _ACTIVE_CIRCUIT.get()
        if builder is not None:
            builder.register_device(instance)
        return instance

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in getattr(cls, "_local_parameter_symbols", {}):
            raise ParameterError(f"localparam {name} is read-only")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in getattr(cls, "_local_parameter_symbols", {}):
            raise ParameterError(f"localparam {name} is read-only")
        super().__delattr__(name)


class Device(metaclass=DeviceMeta):
    _parameter_symbols: OrderedDict[str, ParameterSymbol]
    _local_parameter_symbols: OrderedDict[str, LocalParameterSymbol]
    _internal_symbols: OrderedDict[str, InternalSymbol]
    _port_templates: OrderedDict[str, PortTemplate]
    _relations: tuple[Relation, ...]
    _definition: GraphBuilder | None

    def __init__(self, **parameters: Any):
        immutable = set(parameters) & set(self._local_parameter_symbols)
        if immutable:
            raise ParameterError(
                f"localparams for {type(self).__name__} cannot be overridden: {sorted(immutable)}"
            )
        unknown = set(parameters) - set(self._parameter_symbols)
        if unknown:
            raise ParameterError(f"unknown parameters for {type(self).__name__}: {sorted(unknown)}")
        self._builder: GraphBuilder | None = None
        self.stable_id = f"unbound_{_snake_case(type(self).__name__)}"
        self._members: OrderedDict[str, Any] = OrderedDict()
        self._children: list[Device] = []
        self._internal_nodes: list[Node] = []
        self._internal_optical_connections: list[OpticalConnection] = []
        self._boundary_nodes: dict[str, Node] = {}
        self._node_bindings: dict[Node, Node] = {}
        self._node_metadata_bindings: dict[Node, dict[str, Expr]] = {}
        self._composite_parameter_bindings: dict[str, Expr | BoundParameter] = {}
        self._composite_parent: Device | None = None
        self._ports = {
            name: BoundPort(self, template) for name, template in self._port_templates.items()
        }
        self._parameter_values: dict[str, Any] = {}
        self._internal_initials = {
            name: symbol.spec.initial for name, symbol in self._internal_symbols.items()
        }
        for name, symbol in self._parameter_symbols.items():
            value = parameters.get(name, symbol.spec.default)
            self._set_parameter(symbol, value)
        if self._definition is not None and (
            self._definition.devices
            or self._definition.nodes
            or self._definition.optical_connections
        ):
            self._instantiate_definition()

    def _set_parameter(
        self,
        symbol: ParameterSymbol,
        value: Any,
        *,
        preserve_binding: bool = False,
    ) -> None:
        if isinstance(value, ControlCall):
            value = value.single_output()
        if isinstance(value, BoundControllerOutput):
            value.bind(BoundParameter(self, symbol))
            return
        if (
            symbol.name in self.__dict__.get("_parameter_values", {})
            and self._builder is not None
            and self._builder.stage == "COMPILED"
        ):
            raise ParameterError("compiled device parameters are frozen; rebuild the circuit")
        spec = symbol.spec
        if isinstance(value, BoundParameter):
            value = value.value
        if not isinstance(value, Expr) and not is_quantity_value(value):
            raise ParameterError(f"parameter {symbol.name} must be numeric or symbolic")
        if isinstance(value, Expr):
            dimensions = _declared_symbol_dimensions(value)
            if dimensions is not None and dimensions != as_quantity(spec.default).dimensions:
                raise ParameterError(f"parameter {symbol.name} has incompatible dimensions")
        else:
            candidate = as_quantity(value)
            default = as_quantity(spec.default)
            if candidate.dimensions != default.dimensions:
                raise ParameterError(f"parameter {symbol.name} has incompatible dimensions")
            if spec.minimum is not None and candidate < as_quantity(spec.minimum):
                raise ParameterError(f"parameter {symbol.name} is below its minimum")
            if spec.maximum is not None and not candidate <= as_quantity(spec.maximum):
                raise ParameterError(f"parameter {symbol.name} is above its maximum")
            value = candidate
        needs_snapshot = (
            self._children
            or self._node_metadata_bindings
            or symbol.name in self._composite_parameter_bindings
        )
        snapshot = self._parameter_tree_snapshot() if needs_snapshot else None
        if not preserve_binding:
            self._composite_parameter_bindings.pop(symbol.name, None)
        self._parameter_values[symbol.name] = value
        try:
            self._refresh_node_metadata()
            for child in self._children:
                child._refresh_composite_bindings()
        except Exception:
            if snapshot is not None:
                self._restore_parameter_tree(snapshot)
            raise

    def __setattr__(self, name: str, value: Any) -> None:
        ports = self.__dict__.get("_ports", {})
        if name in ports and isinstance(value, Node) and ports[name] in value.ports:
            return
        members = self.__dict__.get("_members", {})
        if (
            name in members
            and isinstance(members[name], Node)
            and isinstance(value, Node)
            and members[name].canonical() is value.canonical()
        ):
            return
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            members = object.__getattribute__(self, "__dict__").get("_members", {})
            if name in members:
                return members[name]
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    @property
    def is_composite(self) -> bool:
        return bool(self._children or self._internal_nodes or self._internal_optical_connections)

    def _instantiate_definition(self) -> None:
        definition = self._definition
        if definition is None:
            return
        child_map: dict[Device, Device] = {}
        descendant_node_map: dict[Node, Node] = {}

        def map_child_tree(template: Device, child: Device) -> None:
            child_map[template] = child
            if len(template._children) != len(child._children):
                raise TopologyError("composite clone changed its child-device structure")
            if len(template._internal_nodes) != len(child._internal_nodes):
                raise TopologyError("composite clone changed its internal-node structure")
            descendant_node_map.update(
                zip(template._internal_nodes, child._internal_nodes, strict=True)
            )
            for template_descendant, child_descendant in zip(
                template._children, child._children, strict=True
            ):
                map_child_tree(template_descendant, child_descendant)

        token = _ACTIVE_CIRCUIT.set(None)
        try:
            for template in definition.devices:
                resolved: dict[str, Any] = {}
                bindings: dict[str, Expr | BoundParameter] = {}
                for name, value in template._parameter_values.items():
                    if isinstance(value, (Expr, BoundParameter)):
                        bindings[name] = value
                    try:
                        resolved[name] = _resolve_composite_value(value, self)
                    except ParameterError:
                        if not isinstance(value, (Expr, BoundParameter)):
                            raise
                        resolved[name] = value
                child = type(template)(**resolved)
                child._composite_parent = self
                child._composite_parameter_bindings = bindings
                map_child_tree(template, child)
                self._children.append(child)
        finally:
            _ACTIVE_CIRCUIT.reset(token)

        node_map: dict[Node, Node] = {}
        for template in definition.nodes:
            descendant_node = descendant_node_map.get(template)
            if descendant_node is not None:
                node_map[template] = descendant_node
                self._internal_nodes.append(descendant_node)
                continue
            values = {}
            bindings: dict[str, Expr] = {}
            for field, value in (
                ("C", template.C),
                ("T", template.initial_T),
                ("P", template.external_P),
            ):
                if isinstance(value, Expr):
                    bindings[field] = value
                    try:
                        values[field] = _resolve_composite_value(value, self)
                    except ParameterError:
                        values[field] = value
                else:
                    values[field] = value
            node = Node(
                template.domain,
                reference=template.reference,
                fixed=template.fixed,
                C=values["C"],
                T=values["T"],
                P=values["P"],
            )
            node_map[template] = node
            self._node_metadata_bindings[node] = bindings
            self._internal_nodes.append(node)
        for template, node in node_map.items():
            template_root = template.canonical()
            if template_root is not template:
                _union_nodes(node_map[template_root], node)
        self._node_bindings = node_map

        boundary_templates = {template: name for name, template in self._port_templates.items()}

        def endpoint(template: PortTemplate | BoundPort) -> BoundPort:
            if isinstance(template, PortTemplate):
                return self._ports[boundary_templates[template]]
            return child_map[template.owner]._ports[template.name]

        for template_node, node in node_map.items():
            for template_port in template_node.ports:
                cloned_port = endpoint(template_port)
                if isinstance(template_port, PortTemplate):
                    self._boundary_nodes[boundary_templates[template_port]] = node
                    continue
                node.ports.append(cloned_port)
                cloned_port.connection = node

        for template_connection in definition.optical_connections:
            left = endpoint(template_connection.left)
            right = endpoint(template_connection.right)
            connection = OpticalConnection(left, right)
            self._internal_optical_connections.append(connection)
            if not isinstance(template_connection.left, PortTemplate):
                left.connection = connection
            if not isinstance(template_connection.right, PortTemplate):
                right.connection = connection

        for name, member in definition.members.items():
            if isinstance(member, Device):
                self._members[name] = child_map[member]
            elif isinstance(member, Node):
                self._members[name] = node_map[member]

    def _attach(self, builder: GraphBuilder, stable_id: str) -> None:
        self._builder = builder
        self.stable_id = stable_id
        child_names = {
            member: name for name, member in self._members.items() if isinstance(member, Device)
        }
        for index, child in enumerate(self._children, 1):
            child_name = child_names.get(child, f"device_{index}")
            child._attach(builder, f"{stable_id}.{child_name}")
        node_names = {
            member: name for name, member in self._members.items() if isinstance(member, Node)
        }
        for index, node in enumerate(self._internal_nodes, 1):
            node.builder = builder
            node_name = node_names.get(node, f"node_{index}")
            node.stable_id = f"{stable_id}.{node_name}"
        for index, connection in enumerate(self._internal_optical_connections, 1):
            connection.builder = builder
            connection.stable_id = f"{stable_id}.optical_connection_{index}"

    def _refresh_composite_bindings(self) -> None:
        if self._composite_parent is not None:
            for name, expression in self._composite_parameter_bindings.items():
                self._set_parameter(
                    self._parameter_symbols[name],
                    _resolve_composite_value(expression, self._composite_parent),
                    preserve_binding=True,
                )
            self._refresh_node_metadata()
            return
        for child in self._children:
            child._refresh_composite_bindings()

    def _refresh_node_metadata(self) -> None:
        for node, bindings in self._node_metadata_bindings.items():
            for field, expression in bindings.items():
                value = _resolve_composite_value(expression, self)
                if field == "C":
                    node.C = value
                elif field == "T":
                    node.initial_T = value
                else:
                    node.external_P = value
            _validate_node_union_group(node)

    def _parameter_tree_snapshot(self):
        snapshot = [
            (
                self,
                dict(self._parameter_values),
                dict(self._composite_parameter_bindings),
                {
                    node: (node.C, node.initial_T, node.external_P)
                    for node in self._node_metadata_bindings
                },
            )
        ]
        for child in self._children:
            snapshot.extend(child._parameter_tree_snapshot())
        return snapshot

    @staticmethod
    def _restore_parameter_tree(snapshot) -> None:
        for device, values, bindings, node_metadata in snapshot:
            device._parameter_values.clear()
            device._parameter_values.update(values)
            device._composite_parameter_bindings.clear()
            device._composite_parameter_bindings.update(bindings)
            for node, (capacity, temperature, power) in node_metadata.items():
                node.C = capacity
                node.initial_T = temperature
                node.external_P = power


class _ControllerNamespace(dict[str, Any]):
    def __init__(self, enabled: bool):
        super().__init__()
        self.enabled = enabled

    def __setitem__(self, key: str, value: Any) -> None:
        if not self.enabled or key.startswith("__"):
            super().__setitem__(key, value)
            return
        if isinstance(value, ParameterSpec):
            value = ParameterSymbol(key, value)
        elif isinstance(value, InternalSpec):
            value = InternalSymbol(key, value)
        elif isinstance(value, ControllerOutputSpec):
            value = ControllerOutputSymbol(key, value)
        elif is_quantity_value(value):
            value = ParameterSymbol(key, ParameterSpec(value))
        super().__setitem__(key, value)


class ControllerMeta(type):
    @classmethod
    def __prepare__(metaclass, name: str, bases: tuple[type, ...], **kwargs: Any):
        return _ControllerNamespace(any(isinstance(base, ControllerMeta) for base in bases))

    def __new__(metaclass, name: str, bases: tuple[type, ...], namespace: _ControllerNamespace, **kwargs: Any):
        annotations = namespace.get("__annotations__", {})
        for key, annotation in annotations.items():
            value = namespace.get(key)
            is_classvar = get_origin(annotation) is ClassVar or (
                isinstance(annotation, str)
                and annotation.replace("typing.", "").startswith("ClassVar[")
            )
            if is_classvar and isinstance(value, ParameterSymbol):
                dict.__setitem__(namespace, key, value.spec.default)
        cls = super().__new__(metaclass, name, bases, dict(namespace))
        candidate_names: OrderedDict[str, None] = OrderedDict()
        for base in bases:
            for mapping_name in ("_parameter_symbols", "_internal_symbols", "_output_symbols"):
                for key in getattr(base, mapping_name, {}):
                    candidate_names.setdefault(key, None)
        for key, value in namespace.items():
            if isinstance(value, (ParameterSymbol, InternalSymbol, ControllerOutputSymbol)):
                candidate_names.setdefault(key, None)
        parameters: OrderedDict[str, ParameterSymbol] = OrderedDict()
        internals: OrderedDict[str, InternalSymbol] = OrderedDict()
        outputs: OrderedDict[str, ControllerOutputSymbol] = OrderedDict()
        for key in candidate_names:
            resolved = next(
                (base.__dict__[key] for base in cls.__mro__ if key in base.__dict__),
                None,
            )
            if isinstance(resolved, ParameterSymbol):
                parameters[key] = resolved
            elif isinstance(resolved, InternalSymbol):
                internals[key] = resolved
            elif isinstance(resolved, ControllerOutputSymbol):
                outputs[key] = resolved
        cls._parameter_symbols = parameters
        cls._internal_symbols = internals
        cls._output_symbols = outputs
        return cls

    def __call__(cls, *args: Any, **kwargs: Any):
        instance = super().__call__(*args, **kwargs)
        builder = _ACTIVE_CIRCUIT.get()
        if builder is not None:
            builder.register_controller(instance)
        return instance


class Controller(metaclass=ControllerMeta):
    _parameter_symbols: OrderedDict[str, ParameterSymbol]
    _internal_symbols: OrderedDict[str, InternalSymbol]
    _output_symbols: OrderedDict[str, ControllerOutputSymbol]
    delay = 0 * s
    hold = "zoh"

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "hold" and value not in {"zoh", "foh"}:
            raise ParameterError("controller hold must be 'zoh' or 'foh'")
        super().__setattr__(name, value)

    def __init__(self, **parameters: Any):
        hold_default = getattr(type(self), "hold", None)
        configuration = {"hold"} if isinstance(hold_default, str) else set()
        unknown = set(parameters) - set(self._parameter_symbols) - configuration
        if unknown:
            raise ParameterError(f"unknown parameters for {type(self).__name__}: {sorted(unknown)}")
        self._builder: GraphBuilder | None = None
        self.stable_id = f"unbound_{_snake_case(type(self).__name__)}"
        self._parameter_values: dict[str, Any] = {}
        self._calls: list[ControlCall] = []
        if configuration:
            hold_value = parameters.get("hold", hold_default)
            if hold_value not in {"zoh", "foh"}:
                raise ParameterError("controller hold must be 'zoh' or 'foh'")
            self.hold = hold_value
        self._internal_initials = {
            name: symbol.spec.initial for name, symbol in self._internal_symbols.items()
        }
        for name, symbol in self._parameter_symbols.items():
            self._set_parameter(symbol, parameters.get(name, symbol.spec.default))

    def _set_parameter(self, symbol: ParameterSymbol, value: Any) -> None:
        if isinstance(value, (ControlCall, BoundControllerOutput)):
            raise ParameterError("controller configuration parameters cannot be feedback targets")
        if isinstance(value, BoundParameter):
            value = value.value
        if not isinstance(value, Expr) and not is_quantity_value(value):
            raise ParameterError(f"parameter {symbol.name} must be numeric or symbolic")
        if isinstance(value, Expr):
            if symbol.name in {"sample", "delay"}:
                raise ParameterError(f"controller {symbol.name} must be a constant time value")
            dimensions = _declared_symbol_dimensions(value)
            if dimensions is not None and dimensions != as_quantity(symbol.spec.default).dimensions:
                raise ParameterError(f"parameter {symbol.name} has incompatible dimensions")
        else:
            candidate = as_quantity(value)
            default = as_quantity(symbol.spec.default)
            if candidate.dimensions != default.dimensions:
                raise ParameterError(f"parameter {symbol.name} has incompatible dimensions")
            if symbol.name in {"sample", "delay"}:
                if candidate.dimensions != s.dimensions:
                    raise ParameterError(f"controller {symbol.name} must have time dimensions")
                if symbol.name == "sample" and candidate.si_value <= 0:
                    raise ParameterError("controller sample must be positive")
                if symbol.name == "delay" and candidate.si_value < 0:
                    raise ParameterError("controller delay cannot be negative")
            if symbol.spec.minimum is not None and candidate < as_quantity(symbol.spec.minimum):
                raise ParameterError(f"parameter {symbol.name} is below its minimum")
            if symbol.spec.maximum is not None and not candidate <= as_quantity(symbol.spec.maximum):
                raise ParameterError(f"parameter {symbol.name} is above its maximum")
            value = candidate
        self._parameter_values[symbol.name] = value

    def __call__(self, *inputs: Expr) -> ControlCall:
        if not self._output_symbols:
            raise ParameterError(f"{type(self).__name__} declares no output() values")
        if "sample" not in self._parameter_symbols:
            raise ParameterError(f"{type(self).__name__} must declare a sample period")
        self._validate_schedule()
        if any(not isinstance(item, Expr) for item in inputs):
            raise ParameterError("controller inputs must be model expressions")
        step = getattr(self, "step", None)
        if not callable(step):
            raise ParameterError(f"{type(self).__name__} must define step()")
        try:
            signature(step).bind(*inputs)
        except TypeError as error:
            raise ParameterError(
                f"{type(self).__name__}.step() does not accept {len(inputs)} controller inputs"
            ) from error
        call = ControlCall(self, tuple(inputs))
        self._calls.append(call)
        return call

    def _validate_schedule(self) -> None:
        delay_value = self.delay.value if isinstance(self.delay, BoundParameter) else self.delay
        if not is_quantity_value(delay_value):
            raise ParameterError("controller delay must be a constant time value")
        delay_quantity = as_quantity(delay_value)
        if delay_quantity.dimensions != s.dimensions:
            raise ParameterError("controller delay must have time dimensions")
        if delay_quantity.si_value < 0:
            raise ParameterError("controller delay cannot be negative")
        if self.hold not in {"zoh", "foh"}:
            raise ParameterError("controller hold must be 'zoh' or 'foh'")


_ACTIVE_CIRCUIT: contextvars.ContextVar[GraphBuilder | None] = contextvars.ContextVar(
    "pyrilog_active_circuit", default=None
)

_AMBIENT_INITIAL = object()


class Circuit:
    def __init__(self):
        self.graph = GraphBuilder("circuit")
        self.graph.ambient_temperature = 300 * K
        self._ground: Node | None = None
        self._ambient: Node | None = None
        self._tokens: contextvars.ContextVar[tuple[contextvars.Token, ...]] = (
            contextvars.ContextVar("pyrilog_circuit_tokens", default=())
        )

    def __enter__(self) -> Circuit:
        token = _ACTIVE_CIRCUIT.set(self.graph)
        self._tokens.set((*self._tokens.get(), token))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        tokens = self._tokens.get()
        if not tokens:
            raise RuntimeError("Circuit context exited without a matching enter")
        _ACTIVE_CIRCUIT.reset(tokens[-1])
        self._tokens.set(tokens[:-1])

    def compile(self, target):
        from .simulation.compiler import compile_circuit

        return compile_circuit(self, target)

    @property
    def GND(self) -> Node:
        if self._ground is None:
            ground = Node("electrical", reference=True)
            self.graph.register_node(ground, member_name="GND")
            self._ground = ground
        return self._ground

    @GND.setter
    def GND(self, value: Node) -> None:
        if value is not self._ground:
            raise TopologyError("Circuit.GND is a fixed built-in node")

    @property
    def AMBIENT(self) -> Node:
        if self._ambient is None:
            self._ambient = _AmbientNode(self.graph)
        return self._ambient

    @AMBIENT.setter
    def AMBIENT(self, value: Node) -> None:
        if value is not self._ambient:
            raise TopologyError("Circuit.AMBIENT is a fixed built-in node")

    @property
    def ambient_temperature(self) -> Quantity:
        return self.graph.ambient_temperature

    @ambient_temperature.setter
    def ambient_temperature(self, value: Any) -> None:
        self.graph.set_ambient_temperature(value)


class Output:
    def __init__(self, *observables: Expr, file: str | Path | None = None):
        self.observables = tuple(observables)
        self.file = Path(file) if file is not None else None


def enode(*, reference: bool = False) -> Node:
    node = Node("electrical", reference=reference)
    builder = _ACTIVE_CIRCUIT.get()
    if builder is not None:
        builder.register_node(node)
    return node


def tnode(*, C: Any, T: Any | None = None, P: Any | None = None, fixed: bool = False) -> Node:
    node = Node("thermal", fixed=fixed, C=C, T=T, P=P)
    builder = _ACTIVE_CIRCUIT.get()
    if builder is not None:
        builder.register_node(node)
    return node


def onode() -> OpticalNode:
    return OpticalNode()


def connect_endpoints(
    left: PortTemplate | BoundPort | Node,
    right: PortTemplate | BoundPort | Node,
):
    allowed = (PortTemplate, BoundPort, Node)
    if not isinstance(left, allowed) or not isinstance(right, allowed):
        raise TopologyError("'|' connects ports or nodes")
    if left is right:
        raise TopologyError("an endpoint cannot connect to itself")
    if left.domain != right.domain:
        raise TopologyError(f"cannot connect {left.domain} and {right.domain} endpoints")
    if left.domain == "optical":
        if not isinstance(left, (PortTemplate, BoundPort)) or not isinstance(
            right, (PortTemplate, BoundPort)
        ):
            raise TopologyError("optical links connect exactly two optical ports")
        if left.connection is not None or right.connection is not None:
            raise TopologyError("an optical port is already connected")
        builder = _builder_for_endpoints((left, right))
        builder.begin_connections()
        connection = OpticalConnection(left, right)
        builder.register_optical(connection)
        left.connection = connection
        right.connection = connection
        return connection
    return _connect_conservative((left, right))


def _port_batch(ports: Any) -> tuple[Any, ...]:
    if isinstance(ports, (BoundPort, PortTemplate)):
        return (ports,)
    try:
        return tuple(ports)
    except TypeError as error:
        raise TopologyError("expected a port or iterable of ports") from error


def _endpoint_batch(endpoints: Any) -> tuple[Any, ...]:
    if isinstance(endpoints, (BoundPort, PortTemplate, Node)):
        return (endpoints,)
    try:
        return tuple(endpoints)
    except TypeError as error:
        raise TopologyError("expected a node, port, or iterable of endpoints") from error


def _connect_conservative(
    endpoints: Iterable[BoundPort | PortTemplate | Node],
) -> Node:
    batch = tuple(endpoints)
    if len(batch) < 2:
        raise TopologyError("a conservative connection requires at least two endpoints")
    if any(not isinstance(item, (BoundPort, PortTemplate, Node)) for item in batch):
        raise TopologyError("a conservative connection accepts only ports or nodes")
    domains = {item.domain for item in batch}
    if len(domains) != 1:
        raise TopologyError("cannot connect endpoints from different physical domains")
    domain = next(iter(domains))
    if domain == "optical":
        raise TopologyError("optical ports only support strict binary links")

    nodes: list[Node] = []
    ports: list[BoundPort | PortTemplate] = []
    for endpoint in batch:
        if isinstance(endpoint, Node):
            node = endpoint.canonical()
            if node not in nodes:
                nodes.append(node)
        else:
            ports.append(endpoint)
            if isinstance(endpoint.connection, Node):
                node = endpoint.connection.canonical()
                if node not in nodes:
                    nodes.append(node)
            elif endpoint.connection is not None:
                raise TopologyError(f"port {endpoint} already belongs to a non-node connection")

    builder = _builder_for_endpoints(batch)
    if not nodes:
        root = Node(domain)
        nodes.append(root)
    root = _choose_union_root(nodes)
    for node in nodes:
        if node is not root:
            _validate_node_union(root, node)
    if domain == "thermal":
        _validate_thermal_member_temperatures(
            member
            for node in nodes
            for member in node._union_members
        )
    builder.begin_connections()
    if root not in builder.nodes:
        builder.register_node(root)
    for node in nodes:
        if node is not root:
            _union_nodes(root, node)
    root = root.canonical()
    for port in ports:
        if port not in root.ports:
            root.ports.append(port)
        port.connection = root
    return root


def _choose_union_root(nodes: list[Node]) -> Node:
    return max(
        nodes,
        key=lambda node: (
            int(node.reference or node.fixed),
            int(node.builder is not None),
            -nodes.index(node),
        ),
    )


def _union_nodes(preferred: Node, other: Node) -> Node:
    left = preferred.canonical()
    right = other.canonical()
    if left is right:
        return left
    _validate_node_union(left, right)
    if right.reference or right.fixed:
        left, right = right, left
    right._parent = left
    left._union_members.update(right._union_members)
    right._union_members.clear()
    if left.builder is None:
        left.builder = right.builder
    for port in tuple(right.ports):
        if port not in left.ports:
            left.ports.append(port)
        port.connection = left
    right.ports.clear()
    return left


def _validate_node_union(left: Node, right: Node) -> None:
    left = left.canonical()
    right = right.canonical()
    if left is right:
        return
    if left.domain != right.domain:
        raise TopologyError("cannot merge nodes from different physical domains")
    if left.builder is not None and right.builder is not None and left.builder is not right.builder:
        raise TopologyError("cannot merge nodes from different graphs")
    if left.domain == "thermal":
        _validate_thermal_member_temperatures(
            (*left._union_members, *right._union_members)
        )


def _validate_node_union_group(node: Node) -> None:
    root = node.canonical()
    if root.domain == "thermal":
        _validate_thermal_member_temperatures(root._union_members)


def _validate_thermal_member_temperatures(nodes: Iterable[Node]) -> None:
    explicit: Quantity | None = None
    for node in nodes:
        if node.initial_T is None and not node.fixed:
            continue
        temperature = node.initial_temperature
        if isinstance(temperature, Expr):
            continue
        temperature = as_quantity(temperature)
        if explicit is None:
            explicit = temperature
        elif explicit != temperature:
            raise TopologyError("cannot ideally merge thermal nodes with different explicit T")


def _builder_for_endpoints(
    endpoints: Iterable[PortTemplate | BoundPort | Node],
) -> GraphBuilder:
    builders: list[GraphBuilder] = []
    for endpoint in endpoints:
        if isinstance(endpoint, PortTemplate):
            builder = endpoint.builder
        elif isinstance(endpoint, BoundPort):
            builder = endpoint.owner._builder
        else:
            builder = endpoint.builder
        if builder is not None:
            builders.append(builder)
    if not builders or any(builder is not builders[0] for builder in builders[1:]):
        raise TopologyError("endpoints belong to different or unregistered graphs")
    return builders[0]


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _is_thermal_capacity(value: Any) -> bool:
    candidate = value.default if isinstance(value, (ParameterSpec, LocalParameterSpec)) else value
    if not is_quantity_value(candidate):
        return False
    return as_quantity(candidate).dimensions == (J / K).dimensions


def _value_dimensions(value: Any):
    if isinstance(value, Expr):
        dimensions = _declared_symbol_dimensions(value)
        if dimensions is None:
            raise ParameterError("node metadata must have statically known dimensions")
        return dimensions
    return as_quantity(value).dimensions


def _resolve_composite_value(value: Any, parent: Device) -> Quantity:
    if isinstance(value, BoundParameter):
        return _resolve_composite_value(value.value, parent)
    if isinstance(value, ParameterSymbol):
        parent_symbol = parent._parameter_symbols.get(value.name)
        if parent_symbol is not value:
            if parent._composite_parent is not None:
                return _resolve_composite_value(value, parent._composite_parent)
            raise ParameterError(
                f"composite parameter {value.name} does not belong to {type(parent).__name__}"
            )
        return _resolve_composite_value(parent._parameter_values[value.name], parent)
    if isinstance(value, LocalParameterSymbol):
        parent_symbol = parent._local_parameter_symbols.get(value.name)
        if parent_symbol is not value:
            raise ParameterError(
                f"composite localparam {value.name} does not belong to {type(parent).__name__}"
            )
        return as_quantity(value.spec.default)
    if isinstance(value, ConstantExpr):
        return value.value
    if isinstance(value, UnaryExpr) and value.operator == "-":
        return -_resolve_composite_value(value.operand, parent)
    if isinstance(value, BinaryExpr):
        left = _resolve_composite_value(value.left, parent)
        right = _resolve_composite_value(value.right, parent)
        operations = {
            "+": lambda: left + right,
            "-": lambda: left - right,
            "*": lambda: left * right,
            "/": lambda: left / right,
            "**": lambda: left**right.si_value,
        }
        try:
            return as_quantity(operations[value.operator]())
        except KeyError as error:
            raise ParameterError(
                f"unsupported composite parameter operator: {value.operator}"
            ) from error
    if is_quantity_value(value):
        return as_quantity(value)
    raise ParameterError(
        f"unsupported composite parameter binding: {type(value).__name__}"
    )


def _declared_symbol_dimensions(value: Expr):
    if isinstance(value, BoundParameter):
        return as_quantity(value.symbol.spec.default).dimensions
    if isinstance(value, ParameterSymbol):
        return as_quantity(value.spec.default).dimensions
    if isinstance(value, LocalParameterSymbol):
        return as_quantity(value.spec.default).dimensions
    return None


def _builders_in_expressions(expressions: Iterable[Expr]) -> set[GraphBuilder | None]:
    builders: set[GraphBuilder | None] = set()
    for expression in expressions:
        for item in walk(expression):
            if isinstance(item, BoundControllerOutput):
                raise TopologyError("controller outputs cannot be used as feedback inputs")
            if isinstance(item, (ParameterSymbol, LocalParameterSymbol, InternalSymbol)):
                builders.add(None)
            elif isinstance(item, (BoundParameter, BoundInternal)):
                builders.add(item.owner._builder)
            elif isinstance(item, PortQuantity):
                if isinstance(item.port, BoundPort):
                    builders.add(item.port.owner._builder)
                else:
                    builders.add(item.port.builder)
            elif isinstance(item, NodeQuantity):
                builders.add(item.node.builder)
    return builders


def _parameter_key(parameter: BoundParameter) -> tuple[Any, ParameterSymbol]:
    return parameter.owner, parameter.symbol
