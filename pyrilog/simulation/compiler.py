"""Validated lowering from the Python object graph to SPICE and Verilog-A."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analysis import OperatingPoint, Transient
from .backends import Spice
from ..devices import SpicePrimitiveSpec
from ..model import BackendCapabilityError, BoundPort, Circuit, Device
from ..model import InternalSymbol, Node, ParameterSymbol, PortQuantity, PortTemplate
from ..model import TopologyError
from ..expressions import BinaryExpr, ConstantExpr, Expr, FunctionExpr, Relation
from ..expressions import UnaryExpr, UnitViewExpr, walk
from ..units import A, F, H, K, Quantity, V, W, as_quantity, ohm, s


class CompilationError(Exception):
    pass


@dataclass(frozen=True)
class GeneratedModel:
    module: str
    source: Path
    osdi: Path


@dataclass(frozen=True)
class SimulationResult:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    raw_file: Path


@dataclass
class _FlatGraph:
    devices: list[Device]
    nodes: list[Node]
    all_nodes: list[Node]
    controllers: list[Any]
    feedbacks: list[Any]
    optical_connections: list[Any]
    aliases: dict[Node, Node]
    connections: dict[BoundPort, Node]
    node_ports: dict[Node, list[BoundPort]]


class CompiledModel:
    def __init__(
        self,
        target: Spice,
        netlist: Path,
        manifest: Path,
        models: tuple[GeneratedModel, ...],
    ):
        self.target = target
        self.netlist = netlist
        self.manifest = manifest
        self.models = models

    def build_models(self, openvaf: str = "openvaf-r") -> tuple[Path, ...]:
        executable = shutil.which(openvaf)
        if executable is None:
            raise FileNotFoundError(f"cannot find {openvaf}")
        outputs: list[Path] = []
        for model in self.models:
            model.osdi.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.run(
                [executable, str(model.source), "--output", str(model.osdi)],
                capture_output=True,
                text=True,
                check=False,
            )
            if process.returncode != 0:
                raise CompilationError(
                    f"OpenVAF failed for {model.source}:\n{process.stdout}{process.stderr}"
                )
            outputs.append(model.osdi)
        return tuple(outputs)

    def run(
        self,
        analysis: OperatingPoint | Transient,
        *,
        output=None,
        ngspice: str = "ngspice",
        openvaf: str = "openvaf-r",
    ) -> SimulationResult:
        if output is not None:
            raise BackendCapabilityError("observable CSV reconstruction is not implemented yet")
        self.build_models(openvaf)
        executable = shutil.which(ngspice)
        if executable is None:
            raise FileNotFoundError(f"cannot find {ngspice}")
        raw_file = self.netlist.with_suffix(".raw")
        run_file = self.netlist.with_name(f"{self.netlist.stem}.run.sp")
        raw_file.unlink(missing_ok=True)
        base = self.netlist.read_text(encoding="ascii").removesuffix(".end\n")
        if isinstance(analysis, OperatingPoint):
            command = "op"
        elif isinstance(analysis, Transient):
            command = f"tran {_number(analysis.step.si_value)} {_number(analysis.stop.si_value)}"
        else:
            raise TypeError(f"unsupported analysis: {type(analysis).__name__}")
        controls = [".control"]
        controls.extend(f"pre_osdi {_ngspice_path(model.osdi.resolve())}" for model in self.models)
        controls.extend(
            (
                command,
                "set filetype=ascii",
                f"write {_ngspice_path(raw_file.resolve())} all",
                "quit",
                ".endc",
                ".end",
            )
        )
        run_file.write_text(base + "\n" + "\n".join(controls) + "\n", encoding="utf-8")
        process = subprocess.run(
            [executable, "-b", str(run_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0 or not raw_file.exists():
            raise CompilationError(f"ngspice failed:\n{process.stdout}{process.stderr}")
        return SimulationResult(
            command=(executable, "-b", str(run_file)),
            stdout=process.stdout,
            stderr=process.stderr,
            raw_file=raw_file,
        )

    def session(self, analysis, *, output=None):
        raise BackendCapabilityError(
            f"interactive sessions are not implemented for {self.target.simulator}"
        )


def compile_circuit(circuit: Circuit, target: Spice) -> CompiledModel:
    if not isinstance(target, Spice):
        raise TypeError("the first compiler slice supports only Spice targets")
    if target.simulator.lower() != "ngspice":
        raise BackendCapabilityError(
            f"the executable compiler slice supports ngspice, not {target.simulator}"
        )
    source_graph = circuit.graph
    flat_graph = _flatten_graph(source_graph)
    _validate_graph(flat_graph)
    device_lowerings: list[tuple[Device, str, Any]] = []
    generated_classes: dict[str, type[Device]] = {}
    for device in flat_graph.devices:
        native = _match_native(device)
        if native is not None:
            device_lowerings.append((device, "native", native))
            continue
        module = _validate_verilog_a_device(device)
        generated_classes.setdefault(module, type(device))
        device_lowerings.append((device, "verilog_a", module))

    netlist_path = Path(target.netlist)
    va_directory = Path(target.verilog_a_dir)
    manifest_path = netlist_path.with_suffix(".manifest.json")
    output_root = netlist_path.parent
    output_root.mkdir(parents=True, exist_ok=True)
    va_directory.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pyrilog-compile-", dir=output_root) as temp_name:
        temp = Path(temp_name)
        temp_va = temp / "verilog_a"
        temp_va.mkdir()
        generated_models: list[GeneratedModel] = []
        for module, device_class in generated_classes.items():
            source = temp_va / f"{module}.va"
            source.write_text(_emit_verilog_a(device_class, module), encoding="ascii")
            generated_models.append(
                GeneratedModel(module, va_directory / source.name, va_directory / f"{module}.osdi")
            )

        canonical_node_names = _node_names(flat_graph.nodes)
        node_names = {
            node: canonical_node_names[_canonical_node(node, flat_graph)]
            for node in flat_graph.all_nodes
        }
        netlist_text, instance_manifest = _emit_netlist(
            device_lowerings, node_names, flat_graph.connections
        )
        generated_uses_ddt = any(
            isinstance(expression, FunctionExpr) and expression.name == "ddt"
            for device_class in generated_classes.values()
            for relation in device_class._relations
            for expression in walk(relation.residual)
        )
        temp_netlist = temp / netlist_path.name
        temp_netlist.write_text(netlist_text, encoding="ascii")
        manifest_data = {
            "format": "pyrilog-compile-manifest-v1",
            "simulator": target.simulator,
            "netlist": str(netlist_path),
            "models": [
                {
                    "module": model.module,
                    "source": str(model.source),
                    "osdi": str(model.osdi),
                    "source_sha256": hashlib.sha256(
                        (temp_va / model.source.name).read_bytes()
                    ).hexdigest(),
                }
                for model in generated_models
            ],
            "nodes": {node.stable_id: backend for node, backend in node_names.items()},
            "instances": instance_manifest,
            "required_capabilities": [
                "real_relations",
                *("verilog_a_osdi" for _ in generated_models[:1]),
                *(("verilog_a_ddt",) if generated_uses_ddt else ()),
            ],
        }
        temp_manifest = temp / manifest_path.name
        temp_manifest.write_text(
            json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="ascii"
        )

        for source in temp_va.iterdir():
            os.replace(source, va_directory / source.name)
        os.replace(temp_manifest, manifest_path)
        # The netlist is the publication marker and is replaced only after all
        # files it references are in place.
        os.replace(temp_netlist, netlist_path)

    models = tuple(
        GeneratedModel(module, va_directory / f"{module}.va", va_directory / f"{module}.osdi")
        for module in generated_classes
    )
    source_graph.stage = "COMPILED"
    return CompiledModel(target, netlist_path, manifest_path, models)


def _flatten_graph(graph) -> _FlatGraph:
    devices: list[Device] = []
    all_nodes: list[Node] = list(graph.nodes)
    optical_connections = list(graph.optical_connections)
    parents: dict[Node, Node] = {}
    top_level_order = {node: index for index, node in enumerate(graph.nodes)}

    def find(node: Node) -> Node:
        parent = parents.setdefault(node, node)
        if parent is not node:
            parents[node] = find(parent)
        return parents[node]

    def priority(node: Node) -> tuple[int, int, int]:
        return (
            int(node.domain == "electrical" and node.reference),
            int(node in top_level_order),
            -top_level_order.get(node, len(top_level_order)),
        )

    def union(left: Node, right: Node) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root is right_root:
            return
        if priority(left_root) >= priority(right_root):
            parents[right_root] = left_root
        else:
            parents[left_root] = right_root

    def collect(device: Device) -> None:
        if not device.is_composite:
            devices.append(device)
            return
        if device._relations:
            devices.append(device)
        all_nodes.extend(device._internal_nodes)
        optical_connections.extend(device._internal_optical_connections)
        for name, internal_node in device._boundary_nodes.items():
            external_node = device._ports[name].connection
            if not isinstance(external_node, Node):
                raise TopologyError(f"unconnected composite boundary port: {device._ports[name]}")
            union(internal_node, external_node)
        for child in device._children:
            collect(child)

    for device in graph.devices:
        collect(device)

    canonical_nodes: list[Node] = []
    for node in all_nodes:
        resolved = find(node)
        if resolved not in canonical_nodes:
            canonical_nodes.append(resolved)
    connections: dict[BoundPort, Node] = {}
    node_ports: dict[Node, list[BoundPort]] = {node: [] for node in canonical_nodes}
    for device in devices:
        for port in device._ports.values():
            if isinstance(port.connection, Node):
                resolved = find(port.connection)
                connections[port] = resolved
                if port not in node_ports[resolved]:
                    node_ports[resolved].append(port)

    aliases = {
        node: find(node)
        for node in dict.fromkeys(all_nodes)
        if find(node) is not node
    }

    flat = _FlatGraph(
        devices=devices,
        nodes=canonical_nodes,
        all_nodes=list(dict.fromkeys(all_nodes)),
        controllers=graph.controllers,
        feedbacks=graph.feedbacks,
        optical_connections=optical_connections,
        aliases=aliases,
        connections=connections,
        node_ports=node_ports,
    )
    return flat


def _canonical_node(node: Node, graph: _FlatGraph) -> Node:
    while node in graph.aliases:
        node = graph.aliases[node]
    return node


def _validate_graph(graph) -> None:
    if not graph.devices:
        raise TopologyError("cannot compile an empty circuit")
    unbound_calls = [
        call
        for controller in graph.controllers
        for call in controller._calls
        if not call._committed
    ]
    if unbound_calls:
        raise TopologyError("controller outputs must all be assigned before compilation")
    if graph.feedbacks:
        raise BackendCapabilityError("controller feedback scheduling is not implemented yet")
    if any(node.domain != "electrical" for node in graph.nodes):
        raise BackendCapabilityError("thermal node lowering is not implemented yet")
    if graph.optical_connections:
        raise BackendCapabilityError("optical complex-envelope lowering is not implemented yet")
    references = [node for node in graph.nodes if node.domain == "electrical" and node.reference]
    if len(references) != 1:
        raise TopologyError("the electrical graph requires exactly one reference node")
    isolated = [node.stable_id for node in graph.nodes if not graph.node_ports[node]]
    if isolated:
        raise TopologyError(f"unconnected electrical nodes: {', '.join(isolated)}")
    for device in graph.devices:
        for port in device._ports.values():
            if port.domain != "electrical":
                raise BackendCapabilityError(f"{port.domain} port lowering is not implemented yet")
            if port not in graph.connections:
                raise TopologyError(f"unconnected electrical port: {port}")
        for relation in device._relations:
            _validate_relation_dimensions(relation)
    _validate_electrical_connectivity(graph, references[0])


def _validate_electrical_connectivity(graph, reference: Node) -> None:
    reachable_nodes = {reference}
    reachable_devices: set[Device] = set()
    pending_nodes = [reference]
    while pending_nodes:
        node = pending_nodes.pop()
        for port in graph.node_ports[node]:
            device = port.owner
            if device in reachable_devices:
                continue
            reachable_devices.add(device)
            for peer in device._ports.values():
                peer_node = graph.connections.get(peer)
                if peer_node is not None and peer_node not in reachable_nodes:
                    reachable_nodes.add(peer_node)
                    pending_nodes.append(peer_node)
    floating = [
        node.stable_id
        for node in graph.nodes
        if node not in reachable_nodes and graph.node_ports[node]
    ]
    if floating:
        raise TopologyError(f"floating electrical component at nodes: {', '.join(floating)}")


def _match_native(device: Device):
    explicit = type(device).__dict__.get("__pyrilog_spice__")
    if explicit is not None:
        return _validate_spice_primitive(device, explicit)

    ports = list(device._ports.values())
    if len(ports) != 2 or any(port.domain != "electrical" for port in ports):
        return None
    p, n = ports
    template_p = p.template
    template_n = n.template
    if sum(
        _is_current_conservation(relation, template_p, template_n)
        for relation in device._relations
    ) != 1:
        return None
    constitutive = [
        relation
        for relation in device._relations
        if not _is_current_conservation(relation, template_p, template_n)
    ]
    if len(constitutive) != 1:
        return None
    relation = constitutive[0]
    if _is_voltage_difference(relation.right, template_p, template_n):
        relation = Relation(relation.right, relation.left)
    if not _is_voltage_difference(relation.left, template_p, template_n):
        return None
    resistor_parameter = _match_resistance(relation.right, template_p, template_n)
    if resistor_parameter is not None:
        return ("R", resistor_parameter)
    if not _contains_port_quantity(relation.right):
        return ("V", relation.right)
    return None


def _validate_spice_primitive(device: Device, spec: SpicePrimitiveSpec):
    if not isinstance(spec, SpicePrimitiveSpec):
        raise CompilationError(
            f"{type(device).__name__}.__pyrilog_spice__ must be a SpicePrimitiveSpec"
        )
    expected_dimensions = {
        "R": ohm.dimensions,
        "C": F.dimensions,
        "L": H.dimensions,
        "V": V.dimensions,
        "I": A.dimensions,
    }
    if spec.designator not in expected_dimensions:
        raise CompilationError(f"unsupported SPICE primitive designator: {spec.designator}")
    ports = list(device._ports.values())
    if [port.name for port in ports] != ["p", "n"] or any(
        port.domain != "electrical" for port in ports
    ):
        raise CompilationError(
            f"{type(device).__name__} SPICE primitive requires electrical ports p and n"
        )
    symbol = type(device)._parameter_symbols.get(spec.parameter)
    if symbol is None:
        raise CompilationError(
            f"{type(device).__name__} SPICE primitive parameter {spec.parameter!r} is not declared"
        )
    if as_quantity(symbol.spec.default).dimensions != expected_dimensions[spec.designator]:
        raise CompilationError(
            f"{type(device).__name__}.{spec.parameter} has incompatible dimensions for "
            f"SPICE {spec.designator}"
        )
    return spec.designator, symbol


def _match_resistance(
    expression: Expr, p: PortTemplate, n: PortTemplate
) -> ParameterSymbol | None:
    if not isinstance(expression, BinaryExpr) or expression.operator != "*":
        return None
    pairs = ((expression.left, expression.right), (expression.right, expression.left))
    for parameter, current in pairs:
        if (
            isinstance(parameter, ParameterSymbol)
            and _branch_current_coefficient(current, p, n) == 1
        ):
            return parameter
    return None


def _validate_verilog_a_device(device: Device) -> str:
    ports = list(device._ports.values())
    if len(ports) != 2 or any(port.domain != "electrical" for port in ports):
        raise BackendCapabilityError(
            f"{type(device).__name__} is neither a native primitive nor a supported two-terminal device"
        )
    p, n = ports
    if sum(
        _is_current_conservation(relation, p.template, n.template)
        for relation in device._relations
    ) != 1:
        raise BackendCapabilityError(
            f"{type(device).__name__} requires exactly one current-conservation relation"
        )
    _validate_backend_parameter_names(type(device))
    useful = [
        relation
        for relation in device._relations
        if not _is_current_conservation(relation, p.template, n.template)
    ]
    if len(useful) != 1:
        raise BackendCapabilityError(
            f"{type(device).__name__} requires exactly one two-terminal constitutive relation"
        )
    relation = useful[0]
    if _current_contribution(relation, p.template, n.template) is None and _voltage_contribution(
        relation, p.template, n.template
    ) is None:
        raise BackendCapabilityError(
            f"cannot lower {type(device).__name__} relation to Verilog-A contribution"
        )
    for expression in walk(relation.residual):
        if isinstance(expression, FunctionExpr) and expression.name in {"delay", "piecewise"}:
            raise BackendCapabilityError(f"Verilog-A lowering for {expression.name} is not implemented")
        if isinstance(expression, UnitViewExpr):
            raise BackendCapabilityError("unit views are output metadata, not device relations")
    _validate_verilog_a_parameters(device)
    return _module_name(type(device))


def _validate_verilog_a_parameters(device: Device) -> None:
    for name, symbol in type(device)._parameter_symbols.items():
        _validate_finite_real(
            _quantity_value(symbol.spec.default),
            f"{type(device).__name__}.{name} Verilog-A default",
        )
        for bound_name, bound in (
            ("minimum", symbol.spec.minimum),
            ("maximum", symbol.spec.maximum),
        ):
            if bound is not None:
                _validate_finite_real(
                    _quantity_value(bound),
                    f"{type(device).__name__}.{name} Verilog-A {bound_name}",
                )
        _validate_finite_real(
            _quantity_value(device._parameter_values[name]),
            f"{device.stable_id}.{name} Verilog-A parameter",
        )


def _validate_finite_real(value: float | complex, label: str) -> None:
    if isinstance(value, complex):
        if value.imag != 0:
            raise CompilationError(f"{label} must be a real scalar")
        value = value.real
    if not math.isfinite(value):
        raise CompilationError(f"{label} must be finite")


def _emit_verilog_a(device_class: type[Device], module: str) -> str:
    templates = list(device_class._port_templates.values())
    p, n = templates
    useful = [
        relation
        for relation in device_class._relations
        if not _is_current_conservation(relation, p, n)
    ]
    relation = useful[0]
    contribution = _current_contribution(relation, p, n)
    if contribution is not None:
        left = "I(p, n)"
        expression = _emit_va_expr(contribution, p, n)
    else:
        voltage = _voltage_contribution(relation, p, n)
        if voltage is None:
            raise AssertionError("validated relation has no contribution")
        left = "V(p, n)"
        expression = _emit_va_expr(voltage, p, n)
    parameters = []
    for name, symbol in device_class._parameter_symbols.items():
        value = _quantity_value(symbol.spec.default)
        bounds = ""
        if symbol.spec.minimum is not None or symbol.spec.maximum is not None:
            lower = _number(_quantity_value(symbol.spec.minimum)) if symbol.spec.minimum is not None else "-inf"
            upper = _number(_quantity_value(symbol.spec.maximum)) if symbol.spec.maximum is not None else "inf"
            bounds = f" from [{lower}:{upper}]"
        parameters.append(f"    parameter real {_safe_name(name)} = {_number(value)}{bounds};")
    return "\n".join(
        (
            '`include "constants.vams"',
            '`include "disciplines.vams"',
            "",
            f"module {module}(p, n);",
            "    inout p, n;",
            "    electrical p, n;",
            *parameters,
            "",
            "    analog begin",
            f"        {left} <+ {expression};",
            "    end",
            "endmodule",
            "",
        )
    )


def _emit_netlist(device_lowerings, node_names, connections):
    lines = ["* generated by Pyrilog 1.0.0"]
    manifest = []
    native_counts = {designator: 0 for designator in ("R", "C", "L", "V", "I")}
    for device, kind, lowering in device_lowerings:
        p, n = list(device._ports.values())
        p_name = node_names[connections[p]]
        n_name = node_names[connections[n]]
        parameter_values = {name: _quantity_value(value) for name, value in device._parameter_values.items()}
        if kind == "native":
            primitive, payload = lowering
            native_counts[primitive] += 1
            backend_name = f"{primitive}{native_counts[primitive]}"
            value = (
                parameter_values[payload.name]
                if isinstance(payload, ParameterSymbol)
                else _evaluate_constant_expression(payload, device)
            )
            _validate_native_value(device, primitive, value)
            lines.append(f"{backend_name} {p_name} {n_name} {_number(value)}")
            model = None
            lowering_manifest = {"kind": "native", "primitive": primitive}
        else:
            module = lowering
            backend_name = f"N{_safe_name(device.stable_id)}"
            model = f"m_{_safe_name(device.stable_id)}"
            assignments = " ".join(
                f"{_safe_name(name)}={_number(value)}" for name, value in parameter_values.items()
            )
            lines.append(f"{backend_name} {p_name} {n_name} {model}")
            lines.append(f".model {model} {module} {assignments}".rstrip())
            lowering_manifest = {"kind": "verilog_a", "module": module}
        manifest.append(
            {
                "stable_id": device.stable_id,
                "backend_name": backend_name,
                "lowering": lowering_manifest,
                "model": model,
                "source": f"{type(device).__module__}.{type(device).__qualname__}",
                "ports": {p.name: p_name, n.name: n_name},
                "parameters": {
                    name: {
                        "value_si": value,
                        "dimensions": list(device._parameter_values[name].dimensions),
                        "external": type(device)._parameter_symbols[name].spec.external,
                    }
                    for name, value in parameter_values.items()
                },
            }
        )
    lines.extend(("", ".end"))
    return "\n".join(lines) + "\n", manifest


def _validate_native_value(device: Device, primitive: str, value: float | complex) -> None:
    if isinstance(value, complex):
        if value.imag != 0:
            raise CompilationError(
                f"{device.stable_id} SPICE {primitive} value must be a real scalar"
            )
        value = value.real
    if not math.isfinite(value):
        raise CompilationError(f"{device.stable_id} SPICE {primitive} value must be finite")
    if primitive in {"R", "C", "L"} and value <= 0:
        raise CompilationError(
            f"{device.stable_id} SPICE {primitive} value must be positive and finite"
        )


def _node_names(nodes: list[Node]) -> dict[Node, str]:
    names: dict[Node, str] = {}
    index = 0
    for node in nodes:
        if node.reference:
            names[node] = "0"
        else:
            index += 1
            names[node] = f"n{index}"
    return names


def _current_contribution(relation: Relation, p: PortTemplate, n: PortTemplate) -> Expr | None:
    left_coefficient = _branch_current_coefficient(relation.left, p, n)
    if left_coefficient is not None and not _contains_flow(relation.right):
        return relation.right if left_coefficient == 1 else -relation.right
    right_coefficient = _branch_current_coefficient(relation.right, p, n)
    if right_coefficient is not None and not _contains_flow(relation.left):
        return relation.left if right_coefficient == 1 else -relation.left
    return None


def _branch_current_coefficient(
    expression: Expr, p: PortTemplate, n: PortTemplate
) -> int | None:
    """Return the expression coefficient relative to Verilog-A I(p,n)."""

    if _is_port_quantity(expression, p, "i"):
        return -1  # DSL current is positive leaving the device.
    if _is_port_quantity(expression, n, "i"):
        return 1
    if isinstance(expression, UnaryExpr) and expression.operator == "-":
        coefficient = _branch_current_coefficient(expression.operand, p, n)
        return -coefficient if coefficient is not None else None
    return None


def _voltage_contribution(relation: Relation, p: PortTemplate, n: PortTemplate) -> Expr | None:
    if _is_voltage_difference(relation.left, p, n) and not _contains_flow(relation.right):
        return relation.right
    if _is_voltage_difference(relation.right, p, n) and not _contains_flow(relation.left):
        return relation.left
    return None


def _is_current_conservation(relation: Relation, p: PortTemplate, n: PortTemplate) -> bool:
    sides = ((relation.left, relation.right), (relation.right, relation.left))
    for expression, zero in sides:
        if not _is_zero(zero) or not isinstance(expression, BinaryExpr) or expression.operator != "+":
            continue
        quantities = ((expression.left, expression.right), (expression.right, expression.left))
        if any(_is_port_quantity(a, p, "i") and _is_port_quantity(b, n, "i") for a, b in quantities):
            return True
    return False


def _is_voltage_difference(expression: Expr, p: PortTemplate, n: PortTemplate) -> bool:
    return (
        isinstance(expression, BinaryExpr)
        and expression.operator == "-"
        and _is_port_quantity(expression.left, p, "v")
        and _is_port_quantity(expression.right, n, "v")
    )


def _is_port_quantity(expression: Expr, port: PortTemplate, quantity: str) -> bool:
    return (
        isinstance(expression, PortQuantity)
        and expression.port is port
        and expression.quantity == quantity
    )


def _contains_port_quantity(expression: Expr) -> bool:
    return any(isinstance(item, PortQuantity) for item in walk(expression))


def _contains_flow(expression: Expr) -> bool:
    return any(
        isinstance(item, PortQuantity) and item.quantity in {"i", "p"} for item in walk(expression)
    )


def _is_zero(expression: Expr) -> bool:
    return isinstance(expression, ConstantExpr) and expression.value.si_value == 0


def _emit_va_expr(expression: Expr, p: PortTemplate, n: PortTemplate) -> str:
    if isinstance(expression, ConstantExpr):
        return _number(expression.value.si_value)
    if isinstance(expression, ParameterSymbol):
        return _safe_name(expression.name)
    if isinstance(expression, PortQuantity):
        if expression.quantity == "v":
            return "V(p)" if expression.port is p else "V(n)"
        if expression.quantity == "i":
            return "(-I(p, n))" if expression.port is p else "I(p, n)"
        raise BackendCapabilityError(f"unsupported Verilog-A port quantity: {expression.quantity}")
    if isinstance(expression, BinaryExpr):
        left = _emit_va_expr(expression.left, p, n)
        right = _emit_va_expr(expression.right, p, n)
        if expression.operator == "**":
            return f"pow({left}, {right})"
        return f"({left} {expression.operator} {right})"
    if isinstance(expression, UnaryExpr):
        return f"({expression.operator}{_emit_va_expr(expression.operand, p, n)})"
    if isinstance(expression, FunctionExpr):
        if expression.name not in {"exp", "abs", "ddt"}:
            raise BackendCapabilityError(f"unsupported Verilog-A function: {expression.name}")
        arguments = ", ".join(_emit_va_expr(item, p, n) for item in expression.arguments)
        return f"{expression.name}({arguments})"
    raise BackendCapabilityError(f"unsupported Verilog-A expression: {type(expression).__name__}")


def _evaluate_constant_expression(expression: Expr, device: Device) -> float | complex:
    if isinstance(expression, ConstantExpr):
        return expression.value.si_value
    if isinstance(expression, ParameterSymbol):
        return _quantity_value(device._parameter_values[expression.name])
    if isinstance(expression, UnaryExpr) and expression.operator == "-":
        return -_evaluate_constant_expression(expression.operand, device)
    if isinstance(expression, BinaryExpr):
        left = _evaluate_constant_expression(expression.left, device)
        right = _evaluate_constant_expression(expression.right, device)
        return {"+": left + right, "-": left - right, "*": left * right, "/": left / right, "**": left**right}[
            expression.operator
        ]
    raise BackendCapabilityError("native source expression is not parameter-only")


def _validate_relation_dimensions(relation: Relation) -> None:
    left = _dimensions(relation.left)
    right = _dimensions(relation.right)
    if _is_untyped_zero(relation.left) or _is_untyped_zero(relation.right):
        return
    if left != right:
        raise CompilationError(f"relation dimension mismatch: {left} != {right}")


def _dimensions(expression: Expr):
    if isinstance(expression, ConstantExpr):
        return expression.value.dimensions
    if isinstance(expression, ParameterSymbol):
        return as_quantity(expression.spec.default).dimensions
    if isinstance(expression, InternalSymbol):
        return as_quantity(expression.spec.initial).dimensions
    if isinstance(expression, PortQuantity):
        if expression.quantity == "v":
            return V.dimensions
        if expression.quantity == "i":
            if expression.port.domain == "electrical":
                return A.dimensions
            return tuple((name, power / 2) for name, power in W.dimensions)
        if expression.quantity == "o":
            return tuple((name, power / 2) for name, power in W.dimensions)
        if expression.quantity == "t":
            return K.dimensions
        if expression.quantity == "p":
            return W.dimensions
    if isinstance(expression, UnaryExpr):
        return _dimensions(expression.operand)
    if isinstance(expression, UnitViewExpr):
        return _dimensions(expression.expression)
    if isinstance(expression, BinaryExpr):
        left = _dimensions(expression.left)
        right = _dimensions(expression.right)
        if expression.operator in {"+", "-"}:
            if _is_untyped_zero(expression.left):
                return right
            if _is_untyped_zero(expression.right):
                return left
            if left != right:
                raise CompilationError(f"expression dimension mismatch: {left} != {right}")
            return left
        if expression.operator == "*":
            return _combine_dimensions(left, right)
        if expression.operator == "/":
            return _combine_dimensions(left, right, -1)
        if expression.operator == "**":
            if right:
                raise CompilationError("expression exponent must be dimensionless")
            if not isinstance(expression.right, ConstantExpr):
                raise CompilationError("dimensioned bases require a constant exponent")
            power = expression.right.value.si_value
            return tuple((name, exponent * power) for name, exponent in left)
    if isinstance(expression, FunctionExpr):
        if expression.name == "exp":
            if _dimensions(expression.arguments[0]):
                raise CompilationError("exp argument must be dimensionless")
            return ()
        if expression.name == "abs":
            return _dimensions(expression.arguments[0])
        if expression.name == "power":
            dimensions = _dimensions(expression.arguments[0])
            return tuple((name, exponent * 2) for name, exponent in dimensions)
        if expression.name == "phase":
            return ()
        if expression.name == "ddt":
            return _combine_dimensions(_dimensions(expression.arguments[0]), s.dimensions, -1)
        if expression.name == "delay":
            if _dimensions(expression.arguments[1]) != s.dimensions:
                raise CompilationError("delay tau must have time dimensions")
            initial = expression.arguments[2]
            if not _is_untyped_zero(initial) and _dimensions(initial) != _dimensions(expression.arguments[0]):
                raise CompilationError("delay initial value has incompatible dimensions")
            return _dimensions(expression.arguments[0])
    raise CompilationError(f"cannot infer dimensions for {type(expression).__name__}")


def _combine_dimensions(left, right, sign=1):
    values = dict(left)
    for name, power in right:
        values[name] = values.get(name, 0) + sign * power
    return tuple(sorted((name, power) for name, power in values.items() if power != 0))


def _quantity_value(value: Any) -> float | complex:
    if isinstance(value, Quantity):
        return value.si_value
    if isinstance(value, (int, float, complex)) and not isinstance(value, bool):
        return value
    raise BackendCapabilityError("symbolic composite parameter binding is not flattened yet")


def _number(value: Any) -> str:
    if isinstance(value, complex):
        if value.imag != 0:
            raise BackendCapabilityError("complex scalar SPICE parameters are not supported")
        value = value.real
    return format(float(value), ".17g")


def _safe_name(name: str) -> str:
    cleaned = "".join(
        character.lower() if character.isascii() and character.isalnum() else "_"
        for character in name
    )
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"m_{cleaned}"
    return cleaned


def _validate_backend_parameter_names(device_class: type[Device]) -> None:
    backend_names: dict[str, str] = {}
    for source_name in device_class._parameter_symbols:
        backend_name = _safe_name(source_name)
        previous = backend_names.get(backend_name)
        if previous is not None and previous != source_name:
            raise CompilationError(
                f"Verilog-A parameter names collide after normalization: "
                f"{previous!r} and {source_name!r} -> {backend_name!r}"
            )
        backend_names[backend_name] = source_name


def _ngspice_path(path: Path) -> str:
    value = str(path)
    if any(character.isspace() for character in value):
        raise BackendCapabilityError("ngspice control-command paths cannot contain whitespace yet")
    return value


def _is_untyped_zero(expression: Expr) -> bool:
    return _is_zero(expression) and expression.value.dimensions == ()


def _module_name(device_class: type[Device]) -> str:
    signature = {
        "parameters": [
            (
                name,
                _value_signature(symbol.spec.default),
                _value_signature(symbol.spec.minimum),
                _value_signature(symbol.spec.maximum),
                symbol.spec.external,
            )
            for name, symbol in device_class._parameter_symbols.items()
        ],
        "ports": [(name, port.domain) for name, port in device_class._port_templates.items()],
        "relations": [
            (_expression_signature(relation.left), _expression_signature(relation.right))
            for relation in device_class._relations
        ],
    }
    digest = hashlib.sha256(
        json.dumps(signature, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()[:12]
    return f"{_safe_name(device_class.__name__)}_{digest}"


def _value_signature(value: Any):
    if value is None:
        return None
    quantity = as_quantity(value)
    scalar = quantity.si_value
    if isinstance(scalar, complex):
        scalar = [scalar.real, scalar.imag]
    return [scalar, list(quantity.dimensions)]


def _expression_signature(expression: Expr):
    if isinstance(expression, ConstantExpr):
        return ["constant", _value_signature(expression.value)]
    if isinstance(expression, ParameterSymbol):
        return ["parameter", expression.name]
    if isinstance(expression, PortQuantity):
        return ["port", expression.port.name, expression.quantity]
    if isinstance(expression, BinaryExpr):
        return [
            "binary",
            expression.operator,
            _expression_signature(expression.left),
            _expression_signature(expression.right),
        ]
    if isinstance(expression, UnaryExpr):
        return ["unary", expression.operator, _expression_signature(expression.operand)]
    if isinstance(expression, FunctionExpr):
        return [expression.name, *(_expression_signature(item) for item in expression.arguments)]
    raise BackendCapabilityError(f"cannot create a stable signature for {type(expression).__name__}")
