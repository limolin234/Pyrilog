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
from ..model import InternalSymbol, LocalParameterSymbol, Node, NodeQuantity
from ..model import ParameterSymbol, PortQuantity, PortTemplate
from ..model import TopologyError
from ..expressions import BinaryExpr, ConstantExpr, Expr, FunctionExpr, Relation
from ..expressions import UnaryExpr, UnitViewExpr, walk
from ..units import A, F, H, K, Quantity, S, V, W, as_quantity, ohm, s


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


@dataclass(frozen=True)
class _NativeLowering:
    designator: str
    ports: tuple[str, ...]
    value: ParameterSymbol | Expr | None = None
    model_type: str | None = None
    model_parameters: tuple[tuple[str, ParameterSymbol], ...] = ()
    selection: str = "relation_match"


@dataclass(frozen=True)
class _VerilogAContribution:
    kind: str
    p: PortTemplate | Node
    n: PortTemplate | Node | None
    expression: Expr


@dataclass(frozen=True)
class _VerilogALowering:
    module: str
    ports: tuple[str, ...]
    node_templates: tuple[Node, ...]
    contributions: tuple[_VerilogAContribution, ...]


class CompiledModel:
    def __init__(
        self,
        target: Spice,
        netlist: Path,
        manifest: Path,
        models: tuple[GeneratedModel, ...],
        *,
        dev_directory: Path | None = None,
        subckt_directory: Path | None = None,
    ):
        self.target = target
        self.netlist = netlist
        self.manifest = manifest
        self.models = models
        self.dev_directory = dev_directory
        self.subckt_directory = subckt_directory

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
        base = self.netlist.read_text(encoding="utf-8").removesuffix(".end\n")
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
    if _supports_hierarchical(source_graph):
        return _compile_hierarchical(circuit, target)
    flat_graph = _flatten_graph(source_graph)
    _validate_graph(flat_graph)
    device_lowerings: list[tuple[Device, str, Any]] = []
    generated_classes: dict[str, tuple[type[Device], _VerilogALowering]] = {}
    for device in flat_graph.devices:
        _validate_local_relation_budget(device)
        native = _match_native(device)
        if native is not None:
            device_lowerings.append((device, "native", native))
            continue
        verilog_a = _validate_verilog_a_device(device)
        generated_classes.setdefault(verilog_a.module, (type(device), verilog_a))
        device_lowerings.append((device, "verilog_a", verilog_a))

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
        for module, (device_class, lowering) in generated_classes.items():
            source = temp_va / f"{module}.va"
            source.write_text(
                _emit_verilog_a(device_class, lowering), encoding="ascii"
            )
            generated_models.append(
                GeneratedModel(module, va_directory / source.name, va_directory / f"{module}.osdi")
            )

        canonical_node_names = _node_names(flat_graph.nodes)
        node_names = {
            node: canonical_node_names[_canonical_node(node, flat_graph)]
            for node in flat_graph.all_nodes
        }
        netlist_text, instance_manifest, thermal_manifest = _emit_netlist(
            device_lowerings, node_names, flat_graph.connections, flat_graph
        )
        generated_uses_ddt = any(
            isinstance(expression, FunctionExpr) and expression.name == "ddt"
            for device_class, _ in generated_classes.values()
            for relation in device_class._relations
            for expression in walk(relation.residual)
        )
        temp_netlist = temp / netlist_path.name
        temp_netlist.write_text(netlist_text, encoding="ascii")
        manifest_data = {
            "format": "pyrilog-compile-manifest-v1",
            "simulator": target.simulator,
            "netlist": str(netlist_path),
            "dev_directory": None,
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
            "thermal_lumps": thermal_manifest,
            "required_capabilities": [
                "real_relations",
                *(("thermal_analog_mna",) if any(node.domain == "thermal" for node in flat_graph.nodes) else ()),
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


def _compile_hierarchical(circuit: Circuit, target: Spice) -> CompiledModel:
    """Publish a native-SPICE hierarchy without flattening composite devices."""
    graph = circuit.graph
    _validate_graph_hierarchy(graph)
    root = Path(target.netlist).parent
    subckt_directory = root / "subckt"
    subckt_directory.mkdir(parents=True, exist_ok=True)
    netlist_path = Path(target.netlist)
    manifest_path = netlist_path.with_suffix(".manifest.json")

    # Every composite instance gets a private subcircuit name.  This keeps
    # parameter bindings and local node names auditable even when two instances
    # share the same Python class but have different values.
    composites: list[Device] = []
    leaves: list[Device] = []

    def collect(device: Device) -> None:
        if device.is_composite:
            composites.append(device)
            for child in device._children:
                collect(child)
        else:
            leaves.append(device)

    for device in graph.devices:
        collect(device)

    def subckt_name(device: Device) -> str:
        return f"sc_{_safe_name(device.stable_id)}"

    def include_path(path: Path) -> str:
        resolved = str(path.resolve())
        if any(character.isspace() for character in resolved):
            raise BackendCapabilityError(
                "hierarchical SPICE include paths cannot contain whitespace yet"
            )
        return resolved

    def local_context(device: Device | None):
        nodes: list[Node] = []
        if device is None:
            nodes.extend(graph.nodes)
            for item in graph.devices:
                for port in item._ports.values():
                    if isinstance(port.connection, Node):
                        nodes.append(port.connection)
        else:
            nodes.extend(device._internal_nodes)
            for port in device._ports.values():
                if isinstance(port.connection, Node):
                    nodes.append(port.connection)
            for child in device._children:
                for port in child._ports.values():
                    if isinstance(port.connection, Node):
                        nodes.append(port.connection)
            for name, internal in device._boundary_nodes.items():
                external = device._ports[name].connection
                nodes.extend((internal, external) if isinstance(external, Node) else (internal,))
        nodes = list(dict.fromkeys(nodes))
        parent = {node: node for node in nodes}

        def find(node: Node) -> Node:
            root_node = parent.setdefault(node, node)
            if root_node is not node:
                parent[node] = find(root_node)
            return parent[node]

        def union(left: Node, right: Node) -> None:
            left_root, right_root = find(left), find(right)
            if left_root is not right_root:
                parent[right_root] = left_root

        if device is not None:
            for name, internal in device._boundary_nodes.items():
                external = device._ports[name].connection
                if isinstance(external, Node):
                    union(internal, external)
        else:
            # A composite can intentionally alias boundary pins (for example
            # a short-through helper).  Preserve that topology at the root so
            # an X instance receives the same SPICE node on both pins.
            for item in graph.devices:
                if not item.is_composite:
                    continue
                grouped: dict[Node, list[Node]] = {}
                for name, internal in item._boundary_nodes.items():
                    external = item._ports[name].connection
                    if isinstance(external, Node):
                        grouped.setdefault(internal, []).append(external)
                for endpoints in grouped.values():
                    for endpoint in endpoints[1:]:
                        union(endpoints[0], endpoint)
        boundary_roots = {
            name: find(device._boundary_nodes[name])
            for name in device._boundary_nodes
        } if device is not None else {}
        names: dict[Node, str] = {}
        root_counter = 0
        for node in nodes:
            root_node = find(node)
            if root_node in names:
                continue
            boundary = next((name for name, value in boundary_roots.items() if value is root_node), None)
            if boundary is not None:
                names[root_node] = boundary
            elif root_node.reference:
                names[root_node] = "0"
            else:
                if device is None:
                    root_counter += 1
                    names[root_node] = f"n{root_counter}"
                else:
                    names[root_node] = f"n_{_safe_name(root_node.stable_id)}"
        return lambda node: names[find(node)]

    def port_nodes(device: Device, node_name) -> dict[BoundPort, str]:
        result: dict[BoundPort, str] = {}
        for port in device._ports.values():
            if not isinstance(port.connection, Node):
                raise TopologyError(f"unconnected electrical port: {port}")
            result[port] = node_name(port.connection)
        return result

    def native_line(device: Device, node_name, backend_name: str) -> tuple[str, dict[str, Any]]:
        lowering = _match_native(device)
        if lowering is None:
            raise BackendCapabilityError(
                f"{type(device).__name__} in hierarchical SPICE lowering is not a native primitive"
            )
        connections = port_nodes(device, node_name)
        values = {name: _quantity_value(value) for name, value in device._parameter_values.items()}
        tokens = [backend_name, *[connections[device._ports[name]] for name in lowering.ports]]
        model = None
        if lowering.value is not None:
            value = values[lowering.value.name] if isinstance(lowering.value, ParameterSymbol) else _evaluate_constant_expression(lowering.value, device)
            _validate_native_value(device, lowering.designator, value)
            tokens.append(_number(value))
        if lowering.model_type is not None:
            model = f"m_{_safe_name(device.stable_id)}"
            assignments = []
            for backend_parameter, symbol in lowering.model_parameters:
                _validate_native_model_value(device, lowering.designator, symbol.name, values[symbol.name])
                assignments.append(f"{backend_parameter}={_number(values[symbol.name])}")
            tokens.extend((model,))
            model_line = f".model {model} {lowering.model_type} ({' '.join(assignments)})"
        else:
            model_line = None
        item = {
            "stable_id": device.stable_id,
            "backend_name": backend_name,
            "lowering": {"kind": "native", "primitive": lowering.designator, "selection": lowering.selection},
            "model": model,
            "source": f"{type(device).__module__}.{type(device).__qualname__}",
            "ports": {port.name: node for port, node in connections.items()},
            "parameters": {
                name: {"value_si": value, "dimensions": list(device._parameter_values[name].dimensions), "external": type(device)._parameter_symbols[name].spec.external}
                for name, value in values.items()
            },
        }
        return " ".join(tokens) + (f"\n{model_line}" if model_line else ""), item

    leaf_lines: dict[Device, str] = {}
    leaf_manifest: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for leaf in leaves:
        context_owner = leaf._composite_parent
        node_name = local_context(context_owner)
        primitive = _match_native(leaf)
        if primitive is None:
            raise BackendCapabilityError(
                f"{type(leaf).__name__} is not a native SPICE device; hierarchical lowering does not use Verilog-A"
            )
        counters[primitive.designator] = counters.get(primitive.designator, 0) + 1
        backend_name = f"{primitive.designator}{counters[primitive.designator]}"
        line, item = native_line(leaf, node_name, backend_name)
        leaf_lines[leaf] = line
        leaf_manifest.append(item)

    subckt_files: dict[Device, str] = {}
    subckt_manifest: list[dict[str, Any]] = []
    for composite in reversed(composites):
        node_name = local_context(composite)
        body = [f".subckt {subckt_name(composite)} {' '.join(port.name for port in composite._ports.values())}"]
        if composite._relations:
            lowering = _match_native(composite)
            if lowering is None:
                raise BackendCapabilityError(
                    f"{type(composite).__name__} local relations are not native SPICE"
                )
            line, item = native_line(composite, node_name, f"{lowering.designator}{_safe_name(composite.stable_id)}")
            body.append(line)
            item["lowering"] = {"kind": "subckt_self_native", "subckt": subckt_name(composite)}
            subckt_manifest.append(item)
        for child in composite._children:
            if child.is_composite:
                child_nodes = port_nodes(child, node_name)
                body.append("X{} {} {}".format(
                    _safe_name(child.stable_id),
                    " ".join(child_nodes[child._ports[name]] for name in child._ports),
                    subckt_name(child),
                ))
            else:
                body.append(leaf_lines[child])
        if len(body) == 1:
            body.append("* topology-only boundary alias")
        body.append(f".ends {subckt_name(composite)}")
        filename = f"{subckt_name(composite)}.cir"
        (subckt_directory / filename).write_text("\n".join(body) + "\n", encoding="utf-8")
        subckt_files[composite] = filename
        subckt_manifest.append({
            "stable_id": composite.stable_id,
            "backend_name": f"X{_safe_name(composite.stable_id)}",
            "lowering": {"kind": "subckt", "subckt": subckt_name(composite)},
            "model": None,
            "source": f"{type(composite).__module__}.{type(composite).__qualname__}",
            "ports": {
                name: node_name(composite._ports[name].connection)
                for name in composite._ports
                if isinstance(composite._ports[name].connection, Node)
            },
            "parameters": {},
        })

    root_node_name = local_context(None)
    main_lines = ["* generated by Pyrilog hierarchical SPICE lowering"]
    for composite in composites:
        main_lines.append(f".include {include_path(subckt_directory / subckt_files[composite])}")
    top_counter: dict[str, int] = {}
    instances: list[dict[str, Any]] = []
    for device in graph.devices:
        if device.is_composite:
            connections = port_nodes(device, root_node_name)
            main_lines.append("X{} {} {}".format(
                _safe_name(device.stable_id),
                " ".join(connections[device._ports[name]] for name in device._ports),
                subckt_name(device),
            ))
            instances.extend(item for item in subckt_manifest if item["stable_id"] == device.stable_id)
        else:
            primitive = _match_native(device)
            top_counter[primitive.designator] = top_counter.get(primitive.designator, 0) + 1
            main_lines.append(leaf_lines[device])
    main_lines.extend(("", ".end"))
    netlist_path.parent.mkdir(parents=True, exist_ok=True)
    netlist_path.write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    instances.extend(leaf_manifest)
    node_manifest = {node.stable_id: root_node_name(node) for node in graph.nodes}
    for composite in composites:
        mapper = local_context(composite)
        for node in composite._internal_nodes:
            node_manifest[node.stable_id] = mapper(node)
    manifest_data = {
        "format": "pyrilog-compile-manifest-v1",
        "simulator": target.simulator,
        "netlist": str(netlist_path),
        "dev_directory": None,
        "subckt_directory": str(subckt_directory),
        "models": [],
        "nodes": node_manifest,
        "instances": instances,
        "required_capabilities": ["real_relations", "hierarchical_subcircuits"],
    }
    manifest_path.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="ascii")
    graph.stage = "COMPILED"
    return CompiledModel(target, netlist_path, manifest_path, (), subckt_directory=subckt_directory)


def _validate_graph_hierarchy(graph) -> None:
    if not graph.devices:
        raise TopologyError("cannot compile an empty circuit")
    references = [node for node in graph.nodes if node.domain == "electrical" and node.reference]
    if len(references) != 1:
        raise TopologyError("the electrical graph requires exactly one reference node")
    if graph.controllers or graph.optical_connections:
        raise BackendCapabilityError("hierarchical SPICE lowering currently supports electrical devices only")
    for device in graph.devices:
        for port in device._ports.values():
            if port.domain != "electrical":
                raise BackendCapabilityError(f"{port.domain} port lowering is not implemented yet")
            if device.is_composite and port.connection is None:
                raise TopologyError(f"unconnected composite boundary port: {port}")
        if device._relations and _match_native(device) is None and not device._children:
            raise BackendCapabilityError(f"{type(device).__name__} is not a native SPICE device")


def _supports_hierarchical(graph) -> bool:
    """Return whether the native-only hierarchical publisher can be used."""
    if not any(device.is_composite for device in graph.devices):
        return False
    if sum(node.domain == "electrical" and node.reference for node in graph.nodes) != 1:
        return False
    if any(node.domain != "electrical" for node in graph.nodes):
        return False
    def visit(device: Device) -> bool:
        if any(port.domain != "electrical" for port in device._ports.values()):
            return False
        if any(node.domain != "electrical" for node in device._internal_nodes):
            return False
        if device._relations and _match_native(device) is None:
            return False
        if not device.is_composite:
            return _match_native(device) is not None
        return all(visit(child) for child in device._children)
    return all(visit(device) for device in graph.devices)


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

    def priority(node: Node) -> tuple[int, int, int, int]:
        return (
            int(node.domain == "electrical" and node.reference),
            int(node.fixed),
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

    for node in tuple(all_nodes):
        root = node.canonical()
        if root not in all_nodes:
            all_nodes.append(root)
        union(root, node)

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
    if graph.optical_connections:
        raise BackendCapabilityError("optical complex-envelope lowering is not implemented yet")
    _validate_thermal_initials(graph)
    references = [node for node in graph.nodes if node.domain == "electrical" and node.reference]
    has_electrical = any(node.domain == "electrical" for node in graph.nodes)
    if has_electrical and len(references) != 1:
        raise TopologyError("the electrical graph requires exactly one reference node")
    isolated = [
        node.stable_id
        for node in graph.nodes
        if node.domain == "electrical" and not graph.node_ports[node]
    ]
    if isolated:
        raise TopologyError(f"unconnected electrical nodes: {', '.join(isolated)}")
    for device in graph.devices:
        for port in device._ports.values():
            if port.domain not in {"electrical", "thermal"}:
                raise BackendCapabilityError(f"{port.domain} port lowering is not implemented yet")
            if port not in graph.connections:
                raise TopologyError(f"unconnected {port.domain} port: {port}")
        for relation in device._relations:
            _validate_relation_dimensions(relation)
    if references:
        _validate_electrical_connectivity(graph, references[0])


def _validate_thermal_initials(graph: _FlatGraph) -> None:
    explicit_by_root: dict[Node, Quantity] = {}
    for node in graph.all_nodes:
        if node.domain != "thermal" or (node.initial_T is None and not node.fixed):
            continue
        root = _canonical_node(node, graph)
        temperature = as_quantity(node.initial_temperature)
        previous = explicit_by_root.setdefault(root, temperature)
        if previous != temperature:
            raise TopologyError(
                "cannot ideally merge thermal nodes with different explicit T"
            )


def _validate_electrical_connectivity(graph, reference: Node) -> None:
    reachable_nodes = {reference}
    reachable_devices: set[Device] = set()
    pending_nodes = [reference]
    while pending_nodes:
        node = pending_nodes.pop()
        for port in graph.node_ports[node]:
            if port.domain != "electrical":
                continue
            device = port.owner
            if device in reachable_devices:
                continue
            reachable_devices.add(device)
            for peer in device._ports.values():
                if peer.domain != "electrical":
                    continue
                peer_node = graph.connections.get(peer)
                if peer_node is not None and peer_node not in reachable_nodes:
                    reachable_nodes.add(peer_node)
                    pending_nodes.append(peer_node)
    floating = [
        node.stable_id
        for node in graph.nodes
        if node.domain == "electrical"
        and node not in reachable_nodes
        and graph.node_ports[node]
    ]
    if floating:
        raise TopologyError(f"floating electrical component at nodes: {', '.join(floating)}")


def _match_native(device: Device):
    explicit = type(device).__dict__.get("__pyrilog_spice__")
    if explicit is not None:
        return _validate_spice_primitive(device, explicit)

    ports = list(device._ports.values())
    if any(port.domain != "electrical" for port in ports):
        return None
    controlled = _match_controlled_source(device)
    if controlled is not None:
        return controlled
    if len(ports) != 2:
        return None
    p, n = ports
    port_names = (p.name, n.name)
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
    current = _current_contribution(relation, template_p, template_n)
    if current is not None:
        capacitance = _match_capacitance(current, template_p, template_n)
        if capacitance is not None:
            return _NativeLowering("C", port_names, capacitance)
        if _is_native_constant_expression(current):
            return _NativeLowering("I", port_names, current)
    if _is_voltage_difference(relation.right, template_p, template_n):
        relation = Relation(relation.right, relation.left)
    if not _is_voltage_difference(relation.left, template_p, template_n):
        return None
    resistor_parameter = _match_resistance(relation.right, template_p, template_n)
    if resistor_parameter is not None:
        return _NativeLowering("R", port_names, resistor_parameter)
    inductance = _match_inductance(relation.right, template_p, template_n)
    if inductance is not None:
        return _NativeLowering("L", port_names, inductance)
    if _is_native_constant_expression(relation.right):
        return _NativeLowering("V", port_names, relation.right)
    return None


def _validate_local_relation_budget(device: Device) -> None:
    conservative_ports = sum(
        port.domain in {"electrical", "thermal"} for port in device._ports.values()
    )
    local_nodes = {
        item.node
        for relation in device._relations
        for item in walk(relation.residual)
        if isinstance(item, NodeQuantity)
    }
    relation_budget = conservative_ports + len(local_nodes)
    if relation_budget and len(device._relations) > relation_budget:
        raise CompilationError(
            f"{type(device).__name__} has {len(device._relations)} local relations for "
            f"{relation_budget} conservative terminals; local relation count exceeds the "
            "available port/node constraint budget"
        )


def _match_controlled_source(device: Device) -> _NativeLowering | None:
    templates = tuple(port.template for port in device._ports.values())
    if len(templates) != 4:
        return None
    conservation_pairs = tuple(
        pair
        for relation in device._relations
        if (pair := _current_conservation_pair(relation)) is not None
    )
    if len(conservation_pairs) != 1:
        return None
    output_p, output_n = conservation_pairs[0]
    zero_flow_ports = {
        port
        for relation in device._relations
        if (port := _zero_flow_port(relation)) is not None
    }
    constitutive = [
        relation
        for relation in device._relations
        if _current_conservation_pair(relation) is None
        and _zero_flow_port(relation) is None
    ]
    if len(constitutive) != 1:
        return None
    control_ports = set(templates) - {output_p, output_n}
    if zero_flow_ports != control_ports:
        return None
    relation = constitutive[0]
    voltage = _voltage_contribution(relation, output_p, output_n)
    if voltage is not None:
        scaled = _match_scaled_voltage_difference(voltage, control_ports)
        if scaled is not None:
            cp, cn, gain = scaled
            return _NativeLowering(
                "E",
                (output_p.name, output_n.name, cp.name, cn.name),
                gain,
            )
    current = _current_contribution(relation, output_p, output_n)
    if current is not None:
        scaled = _match_scaled_voltage_difference(current, control_ports)
        if scaled is not None:
            cp, cn, transconductance = scaled
            return _NativeLowering(
                "G",
                (output_p.name, output_n.name, cp.name, cn.name),
                transconductance,
            )
    return None


def _match_scaled_voltage_difference(
    expression: Expr, allowed_ports: set[PortTemplate]
) -> tuple[PortTemplate, PortTemplate, Expr] | None:
    if not isinstance(expression, BinaryExpr) or expression.operator != "*":
        return None
    for scale, voltage in (
        (expression.left, expression.right),
        (expression.right, expression.left),
    ):
        pair = _voltage_difference_pair(voltage)
        if (
            pair is not None
            and set(pair) == allowed_ports
            and _is_native_constant_expression(scale)
        ):
            return pair[0], pair[1], scale
    return None


def _validate_spice_primitive(device: Device, spec: SpicePrimitiveSpec):
    if not isinstance(spec, SpicePrimitiveSpec):
        raise CompilationError(
            f"{type(device).__name__}.__pyrilog_spice__ must be a SpicePrimitiveSpec"
        )
    value_dimensions = {
        "R": ohm.dimensions,
        "C": F.dimensions,
        "L": H.dimensions,
        "V": V.dimensions,
        "I": A.dimensions,
        "E": (),
        "G": S.dimensions,
    }
    expected_ports = {
        "R": ("p", "n"),
        "C": ("p", "n"),
        "L": ("p", "n"),
        "V": ("p", "n"),
        "I": ("p", "n"),
        "E": ("p", "n", "cp", "cn"),
        "G": ("p", "n", "cp", "cn"),
        "D": ("p", "n"),
        "Q": ("collector", "base", "emitter"),
    }
    if spec.designator not in expected_ports:
        raise CompilationError(f"unsupported SPICE primitive designator: {spec.designator}")
    ports = list(device._ports.values())
    if spec.ports != expected_ports[spec.designator]:
        raise CompilationError(
            f"SPICE {spec.designator} metadata requires ports {expected_ports[spec.designator]}"
        )
    if tuple(port.name for port in ports) != spec.ports or any(
        port.domain != "electrical" for port in ports
    ):
        raise CompilationError(
            f"{type(device).__name__} SPICE {spec.designator} requires electrical ports "
            + ", ".join(spec.ports)
        )
    value = None
    if spec.designator in value_dimensions:
        if spec.parameter is None:
            raise CompilationError(
                f"{type(device).__name__} SPICE {spec.designator} requires a value parameter"
            )
        value = _require_spice_parameter(device, spec.parameter)
        if as_quantity(value.spec.default).dimensions != value_dimensions[spec.designator]:
            raise CompilationError(
                f"{type(device).__name__}.{spec.parameter} has incompatible dimensions for "
                f"SPICE {spec.designator}"
            )
        if spec.model_type is not None or spec.model_parameters:
            raise CompilationError(f"SPICE {spec.designator} does not accept model metadata")
    elif spec.designator == "Q":
        if spec.parameter is not None:
            raise CompilationError("SPICE Q does not accept a scalar value parameter")
        if spec.model_type not in {"NPN", "PNP"}:
            raise CompilationError("SPICE Q model_type must be NPN or PNP")
    elif spec.designator == "D":
        if spec.parameter is not None or spec.model_type != "D":
            raise CompilationError("SPICE D requires a D model card and no scalar value")
    model_parameters = tuple(
        (backend_name, _require_spice_parameter(device, parameter_name))
        for backend_name, parameter_name in spec.model_parameters
    )
    if spec.designator in {"Q", "D"}:
        model_dimensions = {
            "IS": A.dimensions,
            "BF": (),
            "BR": (),
            "VAF": V.dimensions,
            "N": (),
        }
        for backend_name, symbol in model_parameters:
            if backend_name not in model_dimensions:
                raise CompilationError(f"unsupported SPICE {spec.designator} model parameter: {backend_name}")
            if as_quantity(symbol.spec.default).dimensions != model_dimensions[backend_name]:
                raise CompilationError(
                    f"{type(device).__name__}.{symbol.name} has incompatible dimensions for "
                    f"SPICE Q {backend_name}"
                )
    return _NativeLowering(
        spec.designator,
        spec.ports,
        value,
        spec.model_type,
        model_parameters,
        "explicit_metadata",
    )


def _require_spice_parameter(device: Device, name: str) -> ParameterSymbol:
    symbol = type(device)._parameter_symbols.get(name)
    if symbol is None:
        raise CompilationError(
            f"{type(device).__name__} SPICE primitive parameter {name!r} is not declared"
        )
    return symbol


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


def _match_capacitance(
    expression: Expr, p: PortTemplate, n: PortTemplate
) -> ParameterSymbol | None:
    if not isinstance(expression, BinaryExpr) or expression.operator != "*":
        return None
    for parameter, derivative in (
        (expression.left, expression.right),
        (expression.right, expression.left),
    ):
        if (
            isinstance(parameter, ParameterSymbol)
            and isinstance(derivative, FunctionExpr)
            and derivative.name == "ddt"
            and len(derivative.arguments) == 1
            and _is_voltage_difference(derivative.arguments[0], p, n)
        ):
            return parameter
    return None


def _match_inductance(
    expression: Expr, p: PortTemplate, n: PortTemplate
) -> ParameterSymbol | None:
    if not isinstance(expression, BinaryExpr) or expression.operator != "*":
        return None
    for parameter, derivative in (
        (expression.left, expression.right),
        (expression.right, expression.left),
    ):
        if (
            isinstance(parameter, ParameterSymbol)
            and isinstance(derivative, FunctionExpr)
            and derivative.name == "ddt"
            and len(derivative.arguments) == 1
            and _branch_current_coefficient(derivative.arguments[0], p, n) == 1
        ):
            return parameter
    return None


def _validate_verilog_a_device(device: Device) -> _VerilogALowering:
    ports = list(device._ports.values())
    if any(port.domain not in {"electrical", "thermal"} for port in ports):
        raise BackendCapabilityError(
            f"{type(device).__name__} contains a domain not supported by real MNA lowering"
        )
    _validate_backend_parameter_names(type(device))
    templates = tuple(port.template for port in ports)
    node_templates = tuple(
        dict.fromkeys(
            item.node
            for relation in device._relations
            for item in walk(relation.residual)
            if isinstance(item, NodeQuantity)
        )
    )
    multiport = _multiport_current_contributions(device._relations)
    if multiport is not None:
        _validate_verilog_a_relation_expressions(device)
        _validate_verilog_a_parameters(device)
        return _VerilogALowering(
            _module_name(type(device)),
            tuple(port.name for port in templates),
            node_templates,
            multiport,
        )
    conservation_pairs = tuple(
        pair
        for relation in device._relations
        if (pair := _current_conservation_pair(relation)) is not None
    )
    if not conservation_pairs and any(
        _contains_flow(relation.residual) and _zero_flow_port(relation) is None
        for relation in device._relations
    ):
        raise BackendCapabilityError(
            f"{type(device).__name__} requires an explicit current-conservation relation "
            "for each driven current branch"
        )
    contributions: list[_VerilogAContribution] = []
    for relation in device._relations:
        if _current_conservation_pair(relation) is not None:
            continue
        if _zero_flow_port(relation) is not None:
            continue
        node_contribution = _node_power_contribution(relation)
        if node_contribution is not None:
            contributions.append(node_contribution)
            continue
        contribution = _relation_contribution(relation, conservation_pairs)
        if contribution is None:
            raise BackendCapabilityError(
                f"cannot lower {type(device).__name__} implicit or incomplete relation "
                "to an explicit Verilog-A contribution"
            )
        contributions.append(contribution)
    _validate_verilog_a_relation_expressions(device)
    if not contributions:
        raise BackendCapabilityError(
            f"{type(device).__name__} has no explicit constitutive contribution; "
            "the underconstrained relation set must be completed before compilation"
        )
    branch_keys = [frozenset((item.p, item.n)) for item in contributions]
    if len(set(branch_keys)) != len(branch_keys):
        raise BackendCapabilityError(
            f"{type(device).__name__} has multiple equations driving the same physical branch"
        )
    _validate_verilog_a_parameters(device)
    return _VerilogALowering(
        _module_name(type(device)),
        tuple(port.name for port in templates),
        node_templates,
        tuple(contributions),
    )


def _node_power_contribution(relation: Relation) -> _VerilogAContribution | None:
    for target, value in ((relation.left, relation.right), (relation.right, relation.left)):
        if (
            isinstance(target, NodeQuantity)
            and target.quantity == "p"
            and target.node.domain == "thermal"
            and not _contains_flow(value)
        ):
            # Positive node.p injects heat into the node; positive Verilog-A
            # I(node, 0) leaves it, hence the sign inversion.
            return _VerilogAContribution("I", target.node, None, -value)
    return None


def _validate_verilog_a_relation_expressions(device: Device) -> None:
    for relation in device._relations:
        for expression in walk(relation.residual):
            if isinstance(expression, FunctionExpr) and expression.name in {
                "delay",
                "piecewise",
            }:
                raise BackendCapabilityError(
                    f"Verilog-A lowering for {expression.name} is not implemented"
                )
            if isinstance(expression, UnitViewExpr):
                raise BackendCapabilityError(
                    "unit views are output metadata, not device relations"
                )


def _multiport_current_contributions(
    relations: tuple[Relation, ...],
) -> tuple[_VerilogAContribution, ...] | None:
    conservation = [
        (relation, terms)
        for relation in relations
        if (terms := _current_conservation_terms(relation)) is not None and len(terms) > 2
    ]
    if not conservation:
        return None
    if len(conservation) != 1:
        raise BackendCapabilityError(
            "multiport current lowering requires exactly one N-port conservation relation"
        )
    conservation_relation, terms = conservation[0]
    if len(set(terms)) != len(terms):
        raise BackendCapabilityError("multiport current conservation repeats a port")
    definitions: dict[PortTemplate, Expr] = {}
    for relation in relations:
        if relation is conservation_relation:
            continue
        definition = _port_current_definition(relation)
        if definition is None:
            raise BackendCapabilityError(
                "multiport current lowering requires one explicit flow definition per driven port"
            )
        port, expression = definition
        if port not in terms:
            raise BackendCapabilityError(
                f"flow definition for {port.name} is outside the N-port conservation relation"
            )
        if port in definitions:
            raise BackendCapabilityError(f"flow for {port.name} is defined more than once")
        definitions[port] = expression
    references = [port for port in terms if port not in definitions]
    if len(references) != 1 or len(definitions) != len(terms) - 1:
        raise BackendCapabilityError(
            "N-port current lowering requires exactly N-1 driven flows and one reference port"
        )
    reference = references[0]
    resolved: dict[PortTemplate, Expr] = {}

    def resolve(port: PortTemplate, active: set[PortTemplate]) -> Expr:
        if port in resolved:
            return resolved[port]
        if port in active:
            raise BackendCapabilityError("cyclic right-hand-side flow dependency")
        active.add(port)
        expression = _substitute_port_currents(definitions[port], definitions, resolve, active)
        active.remove(port)
        if _contains_flow(expression):
            raise BackendCapabilityError(
                f"flow definition for {port.name} contains an unresolved flow reference"
            )
        resolved[port] = expression
        return expression

    return tuple(
        _VerilogAContribution("I", port, reference, resolve(port, set()))
        for port in terms
        if port is not reference
    )


def _current_conservation_terms(relation: Relation) -> tuple[PortTemplate, ...] | None:
    for expression, zero in ((relation.left, relation.right), (relation.right, relation.left)):
        if not _is_zero(zero):
            continue
        terms = _flow_linear_terms(expression)
        if terms is None or len(terms) < 2:
            continue
        coefficients = set(terms.values())
        if coefficients in ({1}, {-1}):
            return tuple(terms)
    return None


def _flow_linear_terms(expression: Expr) -> dict[PortTemplate, int] | None:
    term = _port_flow_term(expression)
    if term is not None:
        return {term[0]: term[1]}
    if isinstance(expression, UnaryExpr) and expression.operator == "-":
        nested = _flow_linear_terms(expression.operand)
        return None if nested is None else {port: -value for port, value in nested.items()}
    if isinstance(expression, BinaryExpr) and expression.operator in {"+", "-"}:
        left = _flow_linear_terms(expression.left)
        right = _flow_linear_terms(expression.right)
        if left is None or right is None:
            return None
        sign = 1 if expression.operator == "+" else -1
        result = dict(left)
        for port, coefficient in right.items():
            result[port] = result.get(port, 0) + sign * coefficient
            if result[port] == 0:
                result.pop(port)
        return result
    return None


def _port_current_definition(relation: Relation) -> tuple[PortTemplate, Expr] | None:
    for flow, value in ((relation.left, relation.right), (relation.right, relation.left)):
        term = _port_flow_term(flow)
        if term is not None:
            return term[0], value if term[1] == 1 else -value
    return None


def _substitute_port_currents(expression, definitions, resolve, active):
    term = _port_flow_term(expression)
    if term is not None:
        port, coefficient = term
        if port not in definitions:
            return expression
        value = resolve(port, active)
        return value if coefficient == 1 else -value
    if isinstance(expression, BinaryExpr):
        return BinaryExpr(
            expression.operator,
            _substitute_port_currents(expression.left, definitions, resolve, active),
            _substitute_port_currents(expression.right, definitions, resolve, active),
        )
    if isinstance(expression, UnaryExpr):
        return UnaryExpr(
            expression.operator,
            _substitute_port_currents(expression.operand, definitions, resolve, active),
        )
    if isinstance(expression, FunctionExpr):
        return FunctionExpr(
            expression.name,
            tuple(
                _substitute_port_currents(item, definitions, resolve, active)
                for item in expression.arguments
            ),
            expression.metadata,
        )
    return expression


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
    for name, symbol in type(device)._local_parameter_symbols.items():
        _validate_finite_real(
            _quantity_value(symbol.spec.default),
            f"{type(device).__name__}.{name} Verilog-A localparam",
        )


def _validate_finite_real(value: float | complex, label: str) -> None:
    if isinstance(value, complex):
        if value.imag != 0:
            raise CompilationError(f"{label} must be a real scalar")
        value = value.real
    if not math.isfinite(value):
        raise CompilationError(f"{label} must be finite")


def _emit_verilog_a(
    device_class: type[Device], lowering: _VerilogALowering
) -> str:
    module = lowering.module
    node_names = {node: _thermal_terminal_name(node) for node in lowering.node_templates}
    terminals = (*lowering.ports, *(node_names[node] for node in lowering.node_templates))
    port_list = ", ".join(terminals)
    statements = []
    for contribution in lowering.contributions:
        p = _contribution_terminal(contribution.p, node_names)
        n = (
            _contribution_terminal(contribution.n, node_names)
            if contribution.n is not None
            else None
        )
        left = (
            f"{contribution.kind}({p}, {n})"
            if n is not None
            else f"{contribution.kind}({p})"
        )
        expression = _emit_va_expr(contribution.expression, node_names)
        statements.append(f"        {left} <+ {expression};")
    parameters = []
    for name, symbol in device_class._parameter_symbols.items():
        value = _quantity_value(symbol.spec.default)
        bounds = ""
        if symbol.spec.minimum is not None or symbol.spec.maximum is not None:
            lower = _number(_quantity_value(symbol.spec.minimum)) if symbol.spec.minimum is not None else "-inf"
            upper = _number(_quantity_value(symbol.spec.maximum)) if symbol.spec.maximum is not None else "inf"
            bounds = f" from [{lower}:{upper}]"
        parameters.append(f"    parameter real {_safe_name(name)} = {_number(value)}{bounds};")
    local_parameters = [
        f"    localparam real {_safe_name(name)} = {_number(_quantity_value(symbol.spec.default))};"
        for name, symbol in device_class._local_parameter_symbols.items()
    ]
    return "\n".join(
        (
            '`include "constants.vams"',
            '`include "disciplines.vams"',
            "",
            f"module {module}({port_list});",
            f"    inout {port_list};",
            f"    electrical {port_list};",
            *parameters,
            *local_parameters,
            "",
            "    analog begin",
            *statements,
            "    end",
            "endmodule",
            "",
        )
    )


def _thermal_terminal_name(node: Node) -> str:
    return f"th_{_safe_name(node.stable_id)}"


def _contribution_terminal(endpoint, node_names: dict[Node, str]) -> str:
    if isinstance(endpoint, Node):
        return node_names[endpoint]
    return endpoint.name


def _emit_netlist(device_lowerings, node_names, connections, graph: _FlatGraph):
    lines = ["* generated by Pyrilog 1.1.0"]
    thermal_lines, thermal_manifest = _emit_thermal_lumps(graph, node_names)
    lines.extend(thermal_lines)
    manifest = []
    native_counts: dict[str, int] = {}
    for device, kind, lowering in device_lowerings:
        node_port_manifest: dict[str, str] = {}
        parameter_values = {
            name: _quantity_value(value)
            for name, value in device._parameter_values.items()
        }
        if kind == "native":
            primitive = lowering.designator
            native_counts[primitive] = native_counts.get(primitive, 0) + 1
            backend_name = f"{primitive}{native_counts[primitive]}"
            ports = [device._ports[name] for name in lowering.ports]
            backend_nodes = [node_names[connections[port]] for port in ports]
            model = None
            tokens = [backend_name, *backend_nodes]
            if lowering.value is not None:
                value = (
                    parameter_values[lowering.value.name]
                    if isinstance(lowering.value, ParameterSymbol)
                    else _evaluate_constant_expression(lowering.value, device)
                )
                _validate_native_value(device, primitive, value)
                tokens.append(_number(value))
            if lowering.model_type is not None:
                model = f"m_{_safe_name(device.stable_id)}"
                tokens.append(model)
                for _, symbol in lowering.model_parameters:
                    _validate_native_model_value(
                        device, primitive, symbol.name, parameter_values[symbol.name]
                    )
                assignments = " ".join(
                    f"{backend_name}={_number(parameter_values[symbol.name])}"
                    for backend_name, symbol in lowering.model_parameters
                )
                lines.append(" ".join(tokens))
                lines.append(
                    f".model {model} {lowering.model_type} ({assignments})".rstrip()
                )
            else:
                lines.append(" ".join(tokens))
            lowering_manifest = {
                "kind": "native",
                "primitive": primitive,
                "selection": lowering.selection,
            }
            if lowering.model_type is not None:
                lowering_manifest["model_type"] = lowering.model_type
        else:
            module = lowering.module
            ports = [device._ports[name] for name in lowering.ports]
            backend_nodes = [node_names[connections[port]] for port in ports]
            bound_nodes = [
                device._node_bindings[template]
                for template in lowering.node_templates
            ]
            thermal_backend_nodes = [node_names[node] for node in bound_nodes]
            node_port_manifest = {
                f"thermal:{template.stable_id}": backend_node
                for template, backend_node in zip(
                    lowering.node_templates,
                    thermal_backend_nodes,
                    strict=True,
                )
            }
            backend_name = f"N{_safe_name(device.stable_id)}"
            model = f"m_{_safe_name(device.stable_id)}"
            assignments = " ".join(
                f"{_safe_name(name)}={_number(value)}" for name, value in parameter_values.items()
            )
            lines.append(
                " ".join(
                    (backend_name, *backend_nodes, *thermal_backend_nodes, model)
                )
            )
            lines.append(f".model {model} {module} {assignments}".rstrip())
            lowering_manifest = {
                "kind": "verilog_a",
                "module": module,
                "selection": "relation_fallback",
            }
        manifest.append(
            {
                "stable_id": device.stable_id,
                "backend_name": backend_name,
                "lowering": lowering_manifest,
                "model": model,
                "source": f"{type(device).__module__}.{type(device).__qualname__}",
                "ports": {
                    port.name: backend_node
                    for port, backend_node in zip(ports, backend_nodes, strict=True)
                }
                | node_port_manifest,
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
    return "\n".join(lines) + "\n", manifest, thermal_manifest


def _emit_thermal_lumps(
    graph: _FlatGraph, node_names: dict[Node, str]
) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    manifest: list[dict[str, Any]] = []
    fixed_roots: set[Node] = set()
    capacitor_index = 0
    source_index = 0
    initial_conditions: dict[Node, float] = {}
    explicit_initials: dict[Node, float] = {}
    for node in graph.all_nodes:
        if node.domain == "thermal" and node.initial_T is not None:
            explicit_initials[_canonical_node(node, graph)] = _quantity_value(node.initial_T)
    for node in graph.all_nodes:
        if node.domain != "thermal":
            continue
        root = _canonical_node(node, graph)
        backend = node_names[node]
        if root.fixed and root not in fixed_roots:
            fixed_roots.add(root)
            source_index += 1
            temperature = _quantity_value(root.initial_temperature)
            lines.append(f"VTH{source_index} {backend} 0 {_number(temperature)}")
        if node.C is not None:
            capacity = _quantity_value(node.C)
            _validate_finite_real(capacity, f"{node.stable_id} thermal C")
            if capacity < 0:
                raise CompilationError(f"{node.stable_id} thermal C must be non-negative")
            if capacity > 0 and not root.fixed:
                capacitor_index += 1
                lines.append(f"CTH{capacitor_index} {backend} 0 {_number(capacity)}")
                initial_conditions.setdefault(
                    root,
                    explicit_initials.get(root, _quantity_value(node.initial_temperature)),
                )
        if node.external_P is not None:
            power = _quantity_value(node.external_P)
            _validate_finite_real(power, f"{node.stable_id} thermal P")
            source_index += 1
            lines.append(f"ITH{source_index} 0 {backend} {_number(power)}")
        manifest.append(
            {
                "stable_id": node.stable_id,
                "canonical_node": root.stable_id,
                "backend_node": backend,
                "capacity_si": _quantity_value(node.C) if node.C is not None else 0.0,
                "initial_temperature_si": _quantity_value(node.initial_temperature),
                "fixed": root.fixed,
            }
        )
    lines.extend(
        f".ic V({node_names[root]})={_number(value)}"
        for root, value in initial_conditions.items()
    )
    return lines, manifest


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


def _validate_native_model_value(
    device: Device, primitive: str, parameter: str, value: float | complex
) -> None:
    _validate_finite_real(value, f"{device.stable_id} SPICE {primitive} {parameter}")
    real_value = value.real if isinstance(value, complex) else value
    if primitive in {"Q", "D"} and real_value <= 0:
        raise CompilationError(
            f"{device.stable_id} SPICE {primitive} {parameter} must be positive and finite"
        )


def _node_names(nodes: list[Node]) -> dict[Node, str]:
    names: dict[Node, str] = {}
    electrical_index = 0
    thermal_index = 0
    for node in nodes:
        if node.reference:
            names[node] = "0"
        elif node.domain == "thermal":
            thermal_index += 1
            names[node] = f"th{thermal_index}"
        else:
            electrical_index += 1
            names[node] = f"n{electrical_index}"
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

    term = _port_flow_term(expression)
    if term is not None and term[0] is p:
        return term[1]
    if term is not None and term[0] is n:
        return -term[1]
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


def _relation_contribution(
    relation: Relation,
    conservation_pairs: tuple[tuple[PortTemplate, PortTemplate], ...],
) -> _VerilogAContribution | None:
    for expression, value in (
        (relation.left, relation.right),
        (relation.right, relation.left),
    ):
        pair = _voltage_difference_pair(expression)
        if pair is not None and not _contains_flow(value):
            return _VerilogAContribution("V", pair[0], pair[1], value)
    for p, n in conservation_pairs:
        current = _current_contribution(relation, p, n)
        if current is not None:
            return _VerilogAContribution("I", p, n, current)
    return None


def _is_current_conservation(relation: Relation, p: PortTemplate, n: PortTemplate) -> bool:
    pair = _current_conservation_pair(relation)
    return pair is not None and set(pair) == {p, n}


def _current_conservation_pair(
    relation: Relation,
) -> tuple[PortTemplate, PortTemplate] | None:
    sides = ((relation.left, relation.right), (relation.right, relation.left))
    for expression, zero in sides:
        if not _is_zero(zero) or not isinstance(expression, BinaryExpr) or expression.operator != "+":
            continue
        quantities = ((expression.left, expression.right), (expression.right, expression.left))
        for a, b in quantities:
            left = _port_flow_term(a)
            right = _port_flow_term(b)
            if (
                left is not None
                and right is not None
                and left[0] is not right[0]
                and left[1] == right[1]
            ):
                return left[0], right[0]
    return None


def _zero_flow_port(relation: Relation) -> PortTemplate | None:
    for expression, zero in (
        (relation.left, relation.right),
        (relation.right, relation.left),
    ):
        flow = _port_flow_term(expression)
        if _is_zero(zero) and flow is not None:
            return flow[0]
    return None


def _port_flow_term(expression: Expr) -> tuple[PortTemplate, int] | None:
    if (
        isinstance(expression, PortQuantity)
        and expression.quantity in {"i", "p"}
        and isinstance(expression.port, PortTemplate)
    ):
        # Electrical .i is incoming. Thermal bare .p is the outgoing view;
        # .p.i is represented by UnaryExpr('-', p) and becomes incoming here.
        return expression.port, 1 if expression.quantity == "i" else -1
    if isinstance(expression, UnaryExpr) and expression.operator == "-":
        term = _port_flow_term(expression.operand)
        if term is not None:
            return term[0], -term[1]
    return None


def _is_voltage_difference(expression: Expr, p: PortTemplate, n: PortTemplate) -> bool:
    pair = _voltage_difference_pair(expression)
    return pair is not None and pair == (p, n)


def _voltage_difference_pair(
    expression: Expr,
) -> tuple[PortTemplate, PortTemplate] | None:
    if not (
        isinstance(expression, BinaryExpr)
        and expression.operator == "-"
        and isinstance(expression.left, PortQuantity)
        and isinstance(expression.right, PortQuantity)
        and expression.left.quantity in {"v", "t"}
        and expression.right.quantity == expression.left.quantity
        and isinstance(expression.left.port, PortTemplate)
        and isinstance(expression.right.port, PortTemplate)
    ):
        return None
    return expression.left.port, expression.right.port


def _is_port_quantity(expression: Expr, port: PortTemplate, quantity: str) -> bool:
    return (
        isinstance(expression, PortQuantity)
        and expression.port is port
        and expression.quantity == quantity
    )


def _contains_port_quantity(expression: Expr) -> bool:
    return any(isinstance(item, PortQuantity) for item in walk(expression))


def _is_native_constant_expression(expression: Expr) -> bool:
    if isinstance(expression, (ConstantExpr, ParameterSymbol, LocalParameterSymbol)):
        return True
    if isinstance(expression, UnaryExpr):
        return expression.operator == "-" and _is_native_constant_expression(
            expression.operand
        )
    if isinstance(expression, BinaryExpr):
        return expression.operator in {"+", "-", "*", "/", "**"} and all(
            _is_native_constant_expression(item)
            for item in (expression.left, expression.right)
        )
    return False


def _contains_flow(expression: Expr) -> bool:
    return any(
        isinstance(item, PortQuantity) and item.quantity in {"i", "p"} for item in walk(expression)
    )


def _is_zero(expression: Expr) -> bool:
    return isinstance(expression, ConstantExpr) and expression.value.si_value == 0


def _emit_va_expr(expression: Expr, node_names: dict[Node, str] | None = None) -> str:
    node_names = node_names or {}
    if isinstance(expression, ConstantExpr):
        return _number(expression.value.si_value)
    if isinstance(expression, ParameterSymbol):
        return _safe_name(expression.name)
    if isinstance(expression, LocalParameterSymbol):
        return _safe_name(expression.name)
    if isinstance(expression, PortQuantity):
        if expression.quantity in {"v", "t"}:
            return f"V({expression.port.name})"
        if expression.quantity in {"i", "p"}:
            raise BackendCapabilityError(
                "Verilog-A right-hand-side flow references require an explicit branch"
            )
        raise BackendCapabilityError(f"unsupported Verilog-A port quantity: {expression.quantity}")
    if isinstance(expression, NodeQuantity):
        if expression.quantity == "t":
            try:
                return f"V({node_names[expression.node]})"
            except KeyError as error:
                raise BackendCapabilityError(
                    f"thermal node {expression.node} is not a terminal of this relation model"
                ) from error
        raise BackendCapabilityError(
            "Verilog-A right-hand-side node power references are not supported"
        )
    if isinstance(expression, BinaryExpr):
        left = _emit_va_expr(expression.left, node_names)
        right = _emit_va_expr(expression.right, node_names)
        if expression.operator == "**":
            return f"pow({left}, {right})"
        return f"({left} {expression.operator} {right})"
    if isinstance(expression, UnaryExpr):
        return f"({expression.operator}{_emit_va_expr(expression.operand, node_names)})"
    if isinstance(expression, FunctionExpr):
        if expression.name not in {"exp", "abs", "ddt"}:
            raise BackendCapabilityError(f"unsupported Verilog-A function: {expression.name}")
        arguments = ", ".join(
            _emit_va_expr(item, node_names) for item in expression.arguments
        )
        return f"{expression.name}({arguments})"
    raise BackendCapabilityError(f"unsupported Verilog-A expression: {type(expression).__name__}")


def _evaluate_constant_expression(expression: Expr, device: Device) -> float | complex:
    if isinstance(expression, ConstantExpr):
        return expression.value.si_value
    if isinstance(expression, ParameterSymbol):
        return _quantity_value(device._parameter_values[expression.name])
    if isinstance(expression, LocalParameterSymbol):
        return _quantity_value(expression.spec.default)
    if isinstance(expression, UnaryExpr) and expression.operator == "-":
        return -_evaluate_constant_expression(expression.operand, device)
    if isinstance(expression, BinaryExpr):
        left = _evaluate_constant_expression(expression.left, device)
        right = _evaluate_constant_expression(expression.right, device)
        try:
            if expression.operator == "+":
                return left + right
            if expression.operator == "-":
                return left - right
            if expression.operator == "*":
                return left * right
            if expression.operator == "/":
                return left / right
            if expression.operator == "**":
                return left**right
        except (ArithmeticError, ValueError) as error:
            raise CompilationError(
                f"cannot evaluate native source expression: {error}"
            ) from error
        raise BackendCapabilityError(
            f"unsupported native source operator: {expression.operator}"
        )
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
    if isinstance(expression, LocalParameterSymbol):
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
    if isinstance(expression, NodeQuantity):
        if expression.quantity == "v":
            return V.dimensions
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
    for source_name in (
        *device_class._parameter_symbols,
        *device_class._local_parameter_symbols,
    ):
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
    if isinstance(expression, LocalParameterSymbol):
        return ["localparam", expression.name, _value_signature(expression.spec.default)]
    if isinstance(expression, PortQuantity):
        return ["port", expression.port.name, expression.quantity]
    if isinstance(expression, NodeQuantity):
        return ["node", expression.node.stable_id, expression.quantity]
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
