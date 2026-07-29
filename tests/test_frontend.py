from __future__ import annotations

import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import pint
import pyrilog

from pyrilog import BackendCapabilityError, Circuit, Device
from pyrilog import ddt, enode, eport, external, internal, oport, param
from pyrilog import tnode, tport, u, val
from pyrilog.control import Controller, output
from pyrilog.devices import Capacitor as SpiceCapacitor
from pyrilog.devices import CurrentSource as SpiceCurrentSource
from pyrilog.devices import Inductor as SpiceInductor
from pyrilog.devices import Resistor as SpiceResistor
from pyrilog.devices import VoltageSource as SpiceVoltageSource
from pyrilog.simulation import CompilationError, OperatingPoint, Output, Spice, Transient
from pyrilog.model import FlowQuantity, InternalSymbol, Node, NodeQuantity
from pyrilog.model import ParameterError, ParameterSymbol, TopologyError
from pyrilog.expressions import FunctionExpr, UnaryExpr, walk
from pyrilog.units import A, V, kohm, ns, ohm
from pyrilog.units import as_quantity


class VoltageSource(Device):
    p = eport()
    n = eport()
    dc = external(0 * V)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == dc,
    )


class Resistor(Device):
    p = eport()
    n = eport()
    resistance = param(1 * kohm, min=0 * ohm)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == resistance * p.i.i,
    )


class Conductance(Device):
    p = eport()
    n = eport()
    conductance = param(1e-3 * (A / V), min=0 * (A / V))
    relation = (
        p.i + n.i == 0,
        p.i.i == conductance * (p.v - n.v),
    )


class OutwardConductance(Device):
    p = eport()
    n = eport()
    conductance = param(1e-3 * (A / V), min=0 * (A / V))
    relation = (
        p.i + n.i == 0,
        p.i.o == -conductance * (p.v - n.v),
    )


class ResistorSection(Device):
    p = eport()
    n = eport()
    resistance = 1 * kohm

    resistor = Resistor(resistance=resistance)
    p | resistor.p
    resistor.n | n


class NestedResistorSection(Device):
    p = eport()
    n = eport()
    resistance = 1 * kohm

    section = ResistorSection(resistance=resistance)
    p | section.p
    section.n | n


class ConductanceSection(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (A / V)

    load = Conductance(conductance=conductance)
    p | load.p
    load.n | n


class DividerSection(Device):
    p = eport()
    n = eport()
    resistance = 2 * kohm

    first = Resistor(resistance=resistance / 2)
    second = Resistor(resistance=resistance / 2)
    p | first.p
    midpoint = first.n | second.p
    second.n | n


class ShortThrough(Device):
    p = eport()
    n = eport()

    p | n


class GroundedSection(Device):
    p = eport()
    n = eport()
    resistance = 1 * kohm

    load = Resistor(resistance=resistance)
    p | load.p
    ground = enode(reference=True)
    ground |= (load.n, n)


class ParallelSection(Device):
    p = eport()
    n = eport()
    resistance = 1 * kohm
    conductance = 1e-3 * (A / V)

    load = Resistor(resistance=resistance)
    p | load.p
    load.n | n
    relation = (
        p.i + n.i == 0,
        p.i.i == conductance * (p.v - n.v),
    )


class Integrator(Device):
    p = eport()
    n = eport()
    state = internal(0 * V)
    relation = ddt(state) == p.v - n.v


class MetadataOnly(Device):
    revision: ClassVar[int] = 3


class OpticalEndpoint(Device):
    optical = oport()


class ThermalBody(Device):
    electrical = eport()
    C = 10 * u.uJ / u.K
    relation = P == 0 * u.W


class ElectricalCapacitanceName(Device):
    p = eport()
    n = eport()
    C = 5 * u.pF


class ThermalLink(Device):
    a = tport()
    b = tport()
    resistance = 20 * u.K / u.W
    relation = (
        a.p + b.p == 0,
        a.p.i == (a.t - b.t) / resistance,
    )


class DualController(Controller):
    sample = 10 * ns
    delay = 2 * ns
    hold = "zoh"
    out1 = output(V)
    out2 = output(V)
    state = val(0 * V)

    def step(self, first, second):
        return first, second


def voltage_divider(load_class=Resistor, **load_parameters):
    with Circuit() as circuit:
        source = VoltageSource(dc=1 * V)
        load = load_class(**load_parameters)
        output = source.p | load.p
        circuit.GND |= (source.n, load.n)
    return circuit, source, load, output


def read_raw_point(path: Path) -> dict[str, float]:
    lines = path.read_text(encoding="ascii").splitlines()
    variables_at = lines.index("Variables:")
    values_at = lines.index("Values:")
    names = [line.split()[1] for line in lines[variables_at + 1 : values_at]]
    values: list[float] = []
    for line in lines[values_at + 1 :]:
        parts = line.split()
        if parts:
            values.append(float(parts[-1]))
        if len(values) == len(names):
            break
    return dict(zip(names, values, strict=True))


class FrontendTests(unittest.TestCase):
    def test_pyrilog_public_package_version(self):
        self.assertEqual(pyrilog.__version__, "1.0.0")
        self.assertNotIn("Controller", pyrilog.__all__)
        self.assertNotIn("Spice", pyrilog.__all__)
        self.assertFalse(hasattr(pyrilog, "Controller"))
        self.assertFalse(hasattr(pyrilog, "Spice"))

    def test_circuit_builtin_reference_nodes_are_lazy_and_stable(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            load = Resistor()
            source.p | load.p
            self.assertFalse(any(node.domain == "thermal" for node in circuit.graph.nodes))
            circuit.GND |= (source.n, load.n)
            self.assertIs(circuit.GND, circuit.GND)
            self.assertEqual(circuit.GND.stable_id, "0")
            self.assertNotIn("AMBIENT", circuit.graph.members)

            circuit.AMBIENT.t = 315 * u.K
            self.assertEqual(circuit.AMBIENT.initial_temperature, 315 * u.K)
            self.assertEqual(circuit.ambient_temperature, 315 * u.K)
            self.assertTrue(circuit.AMBIENT.fixed)
            self.assertIs(circuit.AMBIENT, circuit.AMBIENT)
            self.assertIsInstance(circuit.AMBIENT.t, NodeQuantity)

            with self.assertRaisesRegex(ParameterError, "temperature dimensions"):
                circuit.AMBIENT.t = 1 * V

            circuit.ambient_temperature = 316 * u.K
            self.assertEqual(circuit.AMBIENT.initial_temperature, 316 * u.K)

    def test_circuit_builtin_reference_nodes_cannot_be_replaced(self):
        circuit = Circuit()
        with self.assertRaisesRegex(TopologyError, "fixed built-in node"):
            circuit.GND = Node("electrical", reference=True)
        with self.assertRaisesRegex(TopologyError, "fixed built-in node"):
            circuit.AMBIENT = Node("thermal", fixed=True)

        ambient = circuit.AMBIENT
        circuit.graph.stage = "COMPILED"
        with self.assertRaisesRegex(TopologyError, "frozen"):
            ambient.t = 315 * u.K
        with self.assertRaisesRegex(TopologyError, "frozen"):
            circuit.ambient_temperature = 315 * u.K

    def test_ambient_reference_registration_is_atomic(self):
        circuit = Circuit()
        with self.assertRaisesRegex(ParameterError, "temperature dimensions"):
            circuit.AMBIENT.t = 1 * V
        self.assertNotIn("AMBIENT", circuit.graph.members)
        self.assertFalse(circuit.graph.nodes)

        with Circuit() as wrong_domain:
            source = VoltageSource()
            with self.assertRaisesRegex(TopologyError, "electrical port to thermal node"):
                wrong_domain.AMBIENT |= source.p
        self.assertNotIn("AMBIENT", wrong_domain.graph.members)
        self.assertFalse(wrong_domain.graph.nodes)

        with Circuit() as occupied_port:
            link = ThermalLink()
            thermal = tnode()
            thermal |= link.a
            with self.assertRaisesRegex(TopologyError, "already connected"):
                occupied_port.AMBIENT |= link.a
        self.assertNotIn("AMBIENT", occupied_port.graph.members)
        self.assertNotIn(occupied_port.AMBIENT, occupied_port.graph.nodes)

        with Circuit() as compiled:
            source = VoltageSource(dc=1 * V)
            load = Resistor()
            source.p | load.p
            compiled.GND |= (source.n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            compiled.compile(
                Spice(netlist=Path(directory) / "ambient.sp", verilog_a_dir=Path(directory) / "va")
            )
        ambient = compiled.AMBIENT
        for _ in range(2):
            with self.assertRaisesRegex(TopologyError, "frozen"):
                _ = ambient.t
        self.assertIsNone(ambient.builder)
        self.assertNotIn("AMBIENT", compiled.graph.members)

    def test_ordinary_thermal_node_temperature_is_read_only(self):
        node = tnode()
        with self.assertRaises(AttributeError):
            node.t = 315 * u.K

    def test_class_namespace_promotes_parameters_before_relations(self):
        self.assertIsInstance(Resistor.__dict__["resistance"], ParameterSymbol)
        self.assertEqual(tuple(Resistor._parameter_symbols), ("resistance",))
        self.assertEqual(len(Resistor._relations), 2)

    def test_composite_instances_own_independent_hierarchies(self):
        with Circuit() as circuit:
            first = NestedResistorSection(resistance=2 * kohm)
            second = NestedResistorSection(resistance=3 * kohm)
        self.assertIsNot(first.section, second.section)
        self.assertIsNot(first.section.resistor, second.section.resistor)
        self.assertIsNot(
            first.section._internal_nodes[0],
            second.section._internal_nodes[0],
        )
        self.assertEqual(first.section.resistor.resistance.value, 2 * kohm)
        self.assertEqual(second.section.resistor.resistance.value, 3 * kohm)
        self.assertEqual(
            first.section.resistor.stable_id,
            "nested_resistor_section_1.section.resistor",
        )
        self.assertIs(first.section._builder, circuit.graph)

    def test_composite_parent_parameter_updates_child_binding(self):
        with Circuit():
            section = NestedResistorSection(resistance=2 * kohm)
        section.resistance = 1.5 * kohm
        self.assertEqual(section.section.resistor.resistance.value, 1.5 * kohm)
        with self.assertRaisesRegex(ParameterError, "below its minimum"):
            section.resistance = -0.5 * kohm
        self.assertEqual(section.resistance.value, 1.5 * kohm)
        self.assertEqual(section.section.resistor.resistance.value, 1.5 * kohm)

    def test_composite_internal_node_is_hierarchically_accessible(self):
        with Circuit() as circuit:
            first = DividerSection()
            second = DividerSection()
        self.assertIsInstance(first.midpoint, Node)
        self.assertIsNot(first.midpoint, second.midpoint)
        self.assertEqual(first.midpoint.stable_id, "divider_section_1.midpoint")
        self.assertIs(first.midpoint.builder, circuit.graph)
        self.assertEqual(first.first.resistance.value, 1 * kohm)

    def test_internal_ddt_is_preserved_in_relation_ir(self):
        self.assertIsInstance(Integrator.__dict__["state"], InternalSymbol)
        functions = [
            expression
            for expression in walk(Integrator._relations[0].residual)
            if isinstance(expression, FunctionExpr)
        ]
        self.assertEqual([function.name for function in functions], ["ddt"])

    def test_classvar_is_not_promoted_to_parameter(self):
        self.assertEqual(MetadataOnly.revision, 3)
        self.assertNotIn("revision", MetadataOnly._parameter_symbols)

    def test_flow_direction_views_are_signed_aliases(self):
        current = Resistor.__dict__["p"].i
        self.assertIsInstance(current, FlowQuantity)
        self.assertIs(current.o, current)
        self.assertIsInstance(current.i, UnaryExpr)
        self.assertIs(current.i.operand, current)

    def test_optical_input_and_output_remain_independent_quantities(self):
        optical = OpticalEndpoint.__dict__["optical"]
        incoming = optical.i
        outgoing = optical.o
        self.assertEqual(incoming.quantity, "i")
        self.assertEqual(outgoing.quantity, "o")
        self.assertIsNot(incoming, outgoing)
        with self.assertRaises(AttributeError):
            _ = incoming.o

    def test_thermal_capacity_injects_internal_state_and_port(self):
        self.assertEqual(tuple(ThermalBody._parameter_symbols), ("C",))
        self.assertEqual(tuple(ThermalBody._internal_symbols), ("T", "P"))
        self.assertEqual(tuple(ThermalBody._port_templates), ("electrical", "TP"))
        self.assertEqual(ThermalBody._internal_symbols["T"].spec.initial, 300 * u.K)
        body = ThermalBody(T=25 * u.degC)
        self.assertEqual(body.T.initial, 298.15 * u.K)
        self.assertEqual(body.TP.t.quantity, "t")
        self.assertEqual(body.TP.p.quantity, "p")

    def test_electrical_capacitance_named_C_does_not_enable_thermal_state(self):
        self.assertEqual(tuple(ElectricalCapacitanceName._internal_symbols), ())
        self.assertNotIn("TP", ElectricalCapacitanceName._port_templates)

    def test_final_C_schema_controls_inherited_thermal_elements(self):
        class ElectricalOverride(ThermalBody):
            C = 5 * u.pF

        self.assertNotIn("T", ElectricalOverride._internal_symbols)
        self.assertNotIn("P", ElectricalOverride._internal_symbols)
        self.assertNotIn("TP", ElectricalOverride._port_templates)
        self.assertEqual(len(ElectricalOverride._relations), 0)

    def test_automatic_thermal_names_are_reserved(self):
        with self.assertRaisesRegex(TypeError, "TP is reserved"):
            class ExplicitPortBeforeCapacity(Device):
                TP = tport()
                C = 1 * u.J / u.K

        with self.assertRaisesRegex(TypeError, "T is reserved"):
            class ExplicitTemperatureAfterCapacity(Device):
                C = 1 * u.J / u.K
                T = internal(300 * u.K)

    def test_symbolic_parameter_binding_checks_declared_dimensions(self):
        class SourceParameters(Device):
            voltage = 1 * u.V
            heat_capacity = 2 * u.J / u.K

        with self.assertRaisesRegex(ParameterError, "incompatible dimensions"):
            ThermalBody(C=SourceParameters.voltage)
        body = ThermalBody(C=SourceParameters.heat_capacity)
        self.assertIs(body._parameter_values["C"], SourceParameters.heat_capacity)

    def test_explicit_thermal_link_does_not_need_C(self):
        self.assertEqual(tuple(ThermalLink._port_templates), ("a", "b"))
        self.assertEqual(tuple(ThermalLink._parameter_symbols), ("resistance",))

    def test_thermal_node_validates_and_defaults_initial_temperature(self):
        with Circuit() as circuit:
            thermal = tnode(C=1 * u.uJ / u.K)
        circuit.ambient_temperature = 305 * u.K
        self.assertEqual(thermal.initial_temperature, 305 * u.K)
        with self.assertRaisesRegex(ParameterError, "heat-capacity"):
            tnode(C=1 * u.pF)
        with self.assertRaisesRegex(ParameterError, "temperature"):
            tnode(T=1 * u.V)
        with self.assertRaisesRegex(ParameterError, "power"):
            tnode(P=1 * u.V)

    def test_val_is_the_short_form_for_internal(self):
        self.assertIs(val, internal)
        self.assertEqual(tuple(Integrator._internal_symbols), ("state",))

    def test_controller_outputs_bind_to_parameters_with_assignment_syntax(self):
        with Circuit() as circuit:
            first = VoltageSource()
            second = VoltageSource()
            controller = DualController()
            first.dc, second.dc = controller(first.p.v, second.p.v)
        self.assertEqual(len(circuit.graph.controllers), 1)
        self.assertEqual(len(circuit.graph.feedbacks), 1)
        feedback = circuit.graph.feedbacks[0]
        self.assertIs(feedback.controller, controller)
        self.assertEqual(tuple(item.symbol.name for item, _ in feedback.outputs), ("out1", "out2"))
        self.assertEqual(controller.sample.value, 10 * ns)
        self.assertEqual(controller.delay.value, 2 * ns)

    def test_single_controller_output_can_bind_without_unpacking(self):
        class SingleController(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self, measured):
                return measured

        with Circuit() as circuit:
            source = VoltageSource()
            controller = SingleController()
            source.dc = controller(source.p.v)
        self.assertEqual(len(circuit.graph.feedbacks[0].outputs), 1)

    def test_zero_input_controller_can_drive_a_parameter(self):
        class ClockedSource(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self):
                return 0 * V

        with Circuit() as circuit:
            source = VoltageSource()
            controller = ClockedSource()
            source.dc = controller()
        self.assertEqual(circuit.graph.feedbacks[0].inputs, ())

    def test_controller_requires_declared_outputs(self):
        class MissingOutput(Controller):
            pass

        with Circuit():
            controller = MissingOutput()
            with self.assertRaisesRegex(ParameterError, "declares no output"):
                controller()

    def test_controller_requires_sample_period(self):
        class MissingSample(Controller):
            command = output(V)

            def step(self):
                return 0 * V

        with Circuit():
            controller = MissingSample()
            with self.assertRaisesRegex(ParameterError, "must declare a sample period"):
                controller()

    def test_controller_schedule_parameters_are_validated(self):
        with self.assertRaisesRegex(ParameterError, "sample must be positive"):
            DualController(sample=0 * ns)
        with self.assertRaisesRegex(ParameterError, "delay cannot be negative"):
            DualController(delay=-1 * ns)
        with self.assertRaisesRegex(ParameterError, "hold"):
            DualController(hold="invalid")
        controller = DualController(hold="foh")
        self.assertEqual(controller.hold, "foh")

    def test_cross_circuit_feedback_references_are_rejected(self):
        with Circuit() as first_circuit:
            first_source = VoltageSource()
        with Circuit() as second_circuit:
            second_source = VoltageSource()
            controller = DualController()
            call = controller(first_source.p.v, second_source.p.v)
            with self.assertRaisesRegex(TopologyError, "same graph"):
                second_source.dc = call.out1
        self.assertEqual(first_circuit.graph.feedbacks, [])
        self.assertEqual(second_circuit.graph.feedbacks, [])

    def test_duplicate_feedback_targets_are_rejected_atomically(self):
        with Circuit() as circuit:
            source = VoltageSource()
            controller = DualController()
            call = controller(source.p.v, source.n.v)
            source.dc = call.out1
            with self.assertRaisesRegex(TopologyError, "same parameter twice"):
                source.dc = call.out2
        self.assertEqual(circuit.graph.feedbacks, [])

    def test_failed_feedback_binding_rolls_back_call(self):
        with Circuit() as circuit:
            first = VoltageSource()
            second = VoltageSource()
            load = Resistor()
            controller = DualController()
            call = controller(first.p.v, second.p.v)
            first.dc = call.out1
            with self.assertRaisesRegex(ParameterError, "incompatible dimensions"):
                load.resistance = call.out2
            self.assertEqual(circuit.graph.feedbacks, [])
            replacement = DualController()
            replacement_call = replacement(first.p.v, second.p.v)
            first.dc, second.dc = replacement_call
        self.assertEqual(len(circuit.graph.feedbacks), 1)

    def test_existing_feedback_driver_rejects_new_call_atomically(self):
        class SingleController(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self, measured):
                return measured

        with Circuit() as circuit:
            first = VoltageSource()
            second = VoltageSource()
            existing = SingleController()
            candidate = DualController()
            first.dc = existing(first.p.v)
            call = candidate(first.p.v, second.p.v)
            second.dc = call.out1
            with self.assertRaisesRegex(TopologyError, "already has a feedback driver"):
                first.dc = call.out2
        self.assertEqual(len(circuit.graph.feedbacks), 1)
        self.assertEqual(candidate._calls, [])

    def test_controller_rejects_unbound_inputs_and_bad_step_arity(self):
        class BadController(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self, measured):
                return measured

        with Circuit() as circuit:
            source = VoltageSource()
            controller = BadController()
            with self.assertRaisesRegex(TopologyError, "same graph"):
                source.dc = controller(VoltageSource.dc)
            with self.assertRaisesRegex(ParameterError, "does not accept 0"):
                source.dc = controller()

    def test_controller_step_is_required(self):
        class NoStep(Controller):
            sample = 1 * ns
            command = output(V)

        with Circuit() as circuit:
            source = VoltageSource()
            controller = NoStep()
            with self.assertRaisesRegex(ParameterError, "must define step"):
                source.dc = controller(source.p.v)

    def test_controller_schema_follows_python_override_and_mro(self):
        class Base(Controller):
            sample = 1 * ns
            command = output(V)
            state = val(0 * V)

            def step(self, measured):
                return measured

        class Child(Base):
            command = None
            state = None

        self.assertNotIn("command", Child._output_symbols)
        self.assertNotIn("state", Child._internal_symbols)

        class First(Controller):
            sample = 1 * ns
            command = output(V)

        class Second(Controller):
            sample = 2 * ns
            command = output(A)

        class Combined(First, Second):
            def step(self, measured):
                return measured

        self.assertEqual(Combined._parameter_symbols["sample"].spec.default, 1 * ns)
        self.assertEqual(Combined._output_symbols["command"].spec.default, 0 * V)

    def test_incomplete_controller_output_binding_is_rejected_at_compile(self):
        with Circuit() as circuit:
            first = VoltageSource()
            second = VoltageSource()
            controller = DualController()
            call = controller(first.p.v, second.p.v)
            first.dc = call.out1
            ground = enode(reference=True)
            ground |= (first.n, second.n)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TopologyError, "outputs must all be assigned"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "incomplete.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_ambient_temperature_is_used_for_implicit_initial_state(self):
        with Circuit() as circuit:
            body = ThermalBody()
        circuit.ambient_temperature = 310 * u.K
        self.assertEqual(body.T.initial, 310 * u.K)

    def test_explicit_initial_temperature_must_have_temperature_dimensions(self):
        with self.assertRaisesRegex(ParameterError, "initial T"):
            ThermalBody(T=1 * V)

    def test_pint_namespace_supports_offset_log_complex_and_fractional_units(self):
        self.assertEqual((25 * u.degC).to(u.K), 298.15 * u.K)
        self.assertEqual((25 * u.degC) - (20 * u.degC), 5 * u.delta_degC)
        self.assertEqual((3 * u.dB).si_value, 3)
        self.assertEqual((2 * u.dB / u.cm).si_value, 200)
        self.assertAlmostEqual((0 * u.dBm).si_value, 1e-3)
        complex_power = (2 + 3j) * (u.W**0.5)
        self.assertEqual(complex_power.dimensions, (u.W**0.5).dimensions)

    def test_external_pint_quantities_and_symbolic_arithmetic_are_adapted(self):
        external_registry = pint.UnitRegistry()
        load = Resistor(resistance=2 * external_registry.kohm)
        self.assertEqual(load.resistance.value, 2 * u.kohm)
        left = (1 * u.V) + Resistor.__dict__["p"].v
        right = Resistor.__dict__["p"].v + (1 * u.V)
        self.assertEqual(left.operator, "+")
        self.assertEqual(right.operator, "+")
        left_relation = (1 * u.V) == Resistor.__dict__["p"].v
        right_relation = Resistor.__dict__["p"].v == (1 * u.V)
        self.assertEqual(left_relation.left.quantity, "v")
        self.assertEqual(right_relation.left.quantity, "v")

    def test_decibel_construction_paths_keep_algebraic_loss_semantics(self):
        self.assertEqual(u.Quantity(3, u.dB).si_value, 3)
        external_registry = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
        self.assertEqual(as_quantity(3 * external_registry.dB).si_value, 3)

    def test_root_facade_exports_frontend_and_backend_objects(self):
        from pyrilog.control import Controller as ControlController
        from pyrilog.control import output as control_output
        from pyrilog import eport as RootEport
        from pyrilog.simulation import Output as SimulationOutput

        self.assertIs(ControlController, Controller)
        self.assertIs(control_output, output)
        self.assertIs(RootEport, eport)
        self.assertIs(SimulationOutput, Output)

    def test_root_star_import_exposes_common_units(self):
        namespace: dict[str, object] = {}
        exec("from pyrilog import *", namespace)
        for name in ("V", "A", "W", "K", "ns", "um", "kohm", "pF", "dB"):
            self.assertIn(name, namespace)
        for name in ("Controller", "output", "Output", "Spice", "Transient"):
            self.assertNotIn(name, namespace)

    def test_controller_can_be_added_after_physical_topology(self):
        class SingleController(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self, measured):
                return measured

        with Circuit() as circuit:
            source = VoltageSource()
            load = Resistor()
            measured = source.p | load.p
        with circuit:
            controller = SingleController()
            source.dc = controller(measured.v)
            with self.assertRaisesRegex(TopologyError, "before connections"):
                Resistor()
        self.assertEqual(circuit.graph.controllers, [controller])
        self.assertEqual(len(circuit.graph.feedbacks), 1)

    def test_cross_circuit_node_extension_is_atomic(self):
        with Circuit() as first:
            local = Resistor()
            node = local.p | local.n
        with Circuit() as second:
            foreign = Resistor()
        with self.assertRaisesRegex(TopologyError, "different graphs"):
            node |= foreign.p
        self.assertIsNone(foreign.p.connection)
        self.assertEqual(second.graph.stage, "INSTANCE")

    def test_failed_node_batch_does_not_change_stage(self):
        with Circuit() as circuit:
            electrical = Resistor()
            optical = OpticalEndpoint()
            node = enode()
            with self.assertRaisesRegex(TopologyError, "cannot connect optical"):
                node |= (electrical.p, optical.optical)
        self.assertEqual(circuit.graph.stage, "INSTANCE")
        self.assertIsNone(electrical.p.connection)
        self.assertIsNone(optical.optical.connection)

    def test_duplicate_reference_does_not_bind_rejected_node(self):
        circuit = Circuit()
        first = Node("electrical", reference=True)
        second = Node("electrical", reference=True)
        circuit.graph.register_node(first)
        with self.assertRaisesRegex(TopologyError, "only one electrical reference"):
            circuit.graph.register_node(second)
        self.assertIsNone(second.builder)

    def test_connections_are_typed_and_device_order_is_enforced(self):
        circuit, _, _, output = voltage_divider()
        self.assertEqual(output.domain, "electrical")
        self.assertEqual(len(output.ports), 2)
        with self.assertRaises(TopologyError):
            with circuit:
                Resistor()

    def test_same_circuit_nested_context_restores_active_builder(self):
        circuit = Circuit()
        with circuit:
            first = Resistor()
            with circuit:
                second = Resistor()
            third = Resistor()
        outside = Resistor()
        self.assertEqual(circuit.graph.devices, [first, second, third])
        self.assertIsNone(outside._builder)

    def test_full_example_builds_through_root_topology(self):
        source = Path("examples/modeling_language_v1.py").read_text(encoding="utf-8")
        prefix = source.split("# Environment, compilation, analysis, and output")[0]
        namespace = {"__name__": "modeling_language_frontend_test"}
        exec(compile(prefix, "examples/modeling_language_v1.py", "exec"), namespace)
        system = namespace["system"]
        self.assertGreaterEqual(len(system.graph.devices), 5)
        self.assertGreaterEqual(len(system.graph.nodes), 3)


class CompilerTests(unittest.TestCase):
    def test_transient_analysis_validates_time_values(self):
        with self.assertRaisesRegex(ValueError, "time dimensions"):
            Transient(stop=1 * V, step=1 * ns)
        with self.assertRaisesRegex(ValueError, "positive"):
            Transient(stop=1 * ns, step=0 * ns)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            Transient(stop=1 * ns, step=2 * ns)

    def test_native_spice_lowering(self):
        circuit, _, _, _ = voltage_divider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "divider.sp", verilog_a_dir=root / "verilog_a")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            self.assertIn("V1", netlist)
            self.assertIn("R1", netlist)
            self.assertEqual(compiled.models, ())
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(manifest["required_capabilities"], ["real_relations"])
            self.assertEqual(manifest["instances"][0]["ports"]["n"], "0")
            self.assertTrue(manifest["instances"][0]["parameters"]["dc"]["external"])
            result = compiled.run(OperatingPoint())
            point = read_raw_point(result.raw_file)
            self.assertAlmostEqual(point["v(n1)"], 1.0)
            self.assertAlmostEqual(point["i(v1)"], -1e-3)

    def test_standard_device_library_emits_explicit_native_primitives(self):
        with Circuit() as circuit:
            voltage = SpiceVoltageSource(dc=1 * V)
            resistor = SpiceResistor(resistance=2 * kohm)
            capacitor = SpiceCapacitor(capacitance=2 * u.pF)
            inductor = SpiceInductor(inductance=3 * u.nH)
            current = SpiceCurrentSource(dc=4 * u.mA)
            node = enode()
            node |= (voltage.p, resistor.p, capacitor.p, inductor.p, current.p)
            ground = enode(reference=True)
            ground |= (voltage.n, resistor.n, capacitor.n, inductor.n, current.n)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "primitives.sp", verilog_a_dir=root / "va")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            self.assertIn("R1 n1 0 2000", netlist)
            self.assertIn("C1 n1 0 2e-12", netlist)
            self.assertIn("L1 n1 0 3.0000000000000004e-09", netlist)
            self.assertIn("V1 n1 0 1", netlist)
            self.assertIn("I1 n1 0 0.0040000000000000001", netlist)
            self.assertEqual(compiled.models, ())
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(
                [item["backend_name"][0] for item in manifest["instances"]],
                ["V", "R", "C", "L", "I"],
            )

    def test_standard_current_source_uses_spice_p_to_n_direction(self):
        with Circuit() as circuit:
            source = SpiceCurrentSource(dc=1 * u.mA)
            load = SpiceResistor(resistance=1 * kohm)
            output = enode()
            output |= (source.p, load.p)
            ground = enode(reference=True)
            ground |= (source.n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "current.sp", verilog_a_dir=root / "va")
            )
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["v(n1)"], -1.0)

    def test_standard_primitive_metadata_is_not_inherited_silently(self):
        class NonlinearResistor(SpiceResistor):
            nonlinear_coefficient = 1e-3 / V**2
            relation = (
                SpiceResistor.p.i + SpiceResistor.n.i == 0,
                SpiceResistor.p.i.i
                == (SpiceResistor.p.v - SpiceResistor.n.v) / SpiceResistor.resistance
                + nonlinear_coefficient
                * (SpiceResistor.p.v - SpiceResistor.n.v) ** 3
                / SpiceResistor.resistance,
            )

        circuit, _, _, _ = voltage_divider(NonlinearResistor)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "nonlinear.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 1)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            load = manifest["instances"][1]
            self.assertEqual(load["lowering"]["kind"], "verilog_a")
            self.assertTrue(load["backend_name"].startswith("N"))

    def test_nested_composite_flattens_to_native_spice(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = NestedResistorSection(resistance=2 * kohm)
            source.p | section.p
            ground = enode(reference=True)
            ground |= (source.n, section.n)
        section.resistance = 4 * kohm
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "nested.sp", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instances = {item["stable_id"]: item for item in manifest["instances"]}
            child_id = "nested_resistor_section_1.section.resistor"
            self.assertIn(child_id, instances)
            self.assertEqual(instances[child_id]["parameters"]["resistance"]["value_si"], 4000)
            self.assertIn("nested_resistor_section_1.section.node_1", manifest["nodes"])
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -0.25e-3)

    def test_shared_composite_boundary_node_unions_external_nodes(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            short = ShortThrough()
            load = Resistor()
            source.p | short.p
            short.n | load.p
            ground = enode(reference=True)
            ground |= (source.n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "short.sp", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instances = {item["stable_id"]: item for item in manifest["instances"]}
            self.assertEqual(instances["voltage_source_1"]["ports"]["p"], "n1")
            self.assertEqual(instances["resistor_1"]["ports"]["p"], "n1")
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -1e-3)

    def test_composite_internal_reference_survives_boundary_union(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = GroundedSection()
            source.p | section.p
            source.n | section.n
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "grounded.sp", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(manifest["nodes"]["grounded_section_1.ground"], "0")
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -1e-3)

    def test_composite_local_relations_and_children_both_lower(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = ParallelSection()
            source.p | section.p
            ground = enode(reference=True)
            ground |= (source.n, section.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "parallel.sp", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            stable_ids = {item["stable_id"] for item in manifest["instances"]}
            self.assertIn("parallel_section_1", stable_ids)
            self.assertIn("parallel_section_1.load", stable_ids)
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -2e-3)

    def test_flatten_does_not_mutate_source_hierarchy_on_success_or_failure(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = NestedResistorSection()
            source.p | section.p
            ground = enode(reference=True)
            ground |= (source.n, section.n)
        child_connection = section.section.resistor.p.connection
        node_ports = {node: tuple(node.ports) for node in circuit.graph.nodes}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            circuit.compile(Spice(netlist=root / "ok.sp", verilog_a_dir=root / "va"))
        self.assertIs(section.section.resistor.p.connection, child_connection)
        self.assertEqual(
            {node: tuple(node.ports) for node in circuit.graph.nodes}, node_ports
        )

        with Circuit() as invalid:
            source = VoltageSource(dc=1 * V)
            section = NestedResistorSection()
            source.p | section.p
            source.n | section.n
        child_connection = section.section.resistor.p.connection
        node_ports = {node: tuple(node.ports) for node in invalid.graph.nodes}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(TopologyError, "exactly one reference"):
                invalid.compile(Spice(netlist=root / "bad.sp", verilog_a_dir=root / "va"))
        self.assertIs(section.section.resistor.p.connection, child_connection)
        self.assertEqual(
            {node: tuple(node.ports) for node in invalid.graph.nodes}, node_ports
        )

    def test_composite_flattens_to_generated_verilog_a(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = ConductanceSection(conductance=2e-3 * (A / V))
            source.p | section.p
            ground = enode(reference=True)
            ground |= (source.n, section.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "nested_va.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 1)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(manifest["instances"][1]["stable_id"], "conductance_section_1.load")
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -2e-3)

    def test_generated_verilog_a_runs_in_openvaf_and_ngspice(self):
        circuit, _, _, _ = voltage_divider(Conductance)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "conductance.sp", verilog_a_dir=root / "verilog_a")
            )
            self.assertEqual(len(compiled.models), 1)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(len(manifest["models"][0]["source_sha256"]), 64)
            result = compiled.run(OperatingPoint())
            self.assertTrue(result.raw_file.exists())
            self.assertIn("No. of Data Rows : 1", result.stdout)
            point = read_raw_point(result.raw_file)
            self.assertAlmostEqual(point["v(n1)"], 1.0)
            self.assertAlmostEqual(point["i(v1)"], -1e-3)

    def test_outward_current_view_lowers_to_the_same_branch_direction(self):
        circuit, _, _, _ = voltage_divider(OutwardConductance)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "outward.sp", verilog_a_dir=root / "verilog_a")
            )
            result = compiled.run(OperatingPoint())
            point = read_raw_point(result.raw_file)
            self.assertAlmostEqual(point["v(n1)"], 1.0)
            self.assertAlmostEqual(point["i(v1)"], -1e-3)
            transient = compiled.run(Transient(stop=2 * ns, step=1 * ns))
            self.assertIn("Transient Analysis", transient.raw_file.read_text(encoding="ascii"))

    def test_same_named_device_classes_get_distinct_models(self):
        def load_class(multiplier):
            class Load(Device):
                p = eport()
                n = eport()
                conductance = 1e-3 * (A / V)
                relation = (
                    p.i + n.i == 0,
                    p.i.i == multiplier * conductance * (p.v - n.v),
                )

            return Load

        first_load = load_class(1)
        second_load = load_class(2)
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            first = first_load()
            second = second_load()
            output = source.p | first.p
            output |= second.p
            ground = enode(reference=True)
            ground |= (source.n, first.n, second.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "same_name.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 2)
            self.assertEqual(len({model.module for model in compiled.models}), 2)
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -3e-3)

    def test_unsupported_interactive_session_fails_explicitly(self):
        circuit, _, _, _ = voltage_divider()
        with tempfile.TemporaryDirectory() as directory:
            compiled = circuit.compile(
                Spice(netlist=Path(directory) / "divider.sp", verilog_a_dir=Path(directory) / "va")
            )
            with self.assertRaises(BackendCapabilityError):
                compiled.session(OperatingPoint())

    def test_controller_feedback_backend_gap_fails_explicitly(self):
        class SingleController(Controller):
            sample = 1 * ns
            command = output(V)

            def step(self, measured):
                return measured

        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            load = Resistor()
            controller = SingleController()
            measured = source.p | load.p
            ground = enode(reference=True)
            ground |= (source.n, load.n)
            source.dc = controller(measured.v)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "feedback scheduling"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "feedback.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_unsupported_output_reconstruction_fails_explicitly(self):
        circuit, _, _, output_node = voltage_divider()
        with tempfile.TemporaryDirectory() as directory:
            compiled = circuit.compile(
                Spice(netlist=Path(directory) / "divider.sp", verilog_a_dir=Path(directory) / "va")
            )
            with self.assertRaisesRegex(BackendCapabilityError, "CSV reconstruction"):
                compiled.run(OperatingPoint(), output=Output(output_node.v))

    def test_unsupported_optical_and_thermal_lowering_fail_explicitly(self):
        with Circuit() as optical_circuit:
            left = OpticalEndpoint()
            right = OpticalEndpoint()
            left.optical | right.optical
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "optical complex-envelope"):
                optical_circuit.compile(
                    Spice(netlist=Path(directory) / "optical.sp", verilog_a_dir=Path(directory) / "va")
                )

        with Circuit() as thermal_circuit:
            link = ThermalLink()
            hot = tnode()
            cold = tnode(fixed=True)
            hot |= link.a
            cold |= link.b
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "thermal node"):
                thermal_circuit.compile(
                    Spice(netlist=Path(directory) / "thermal.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_nonlinear_charge_ddt_lowers_to_verilog_a_and_runs_transient(self):
        class NonlinearCharge(Device):
            p = eport()
            n = eport()
            capacitance = 1 * u.pF
            nonlinear_capacitance = 0.1 * u.pF / V
            relation = (
                p.i + n.i == 0,
                p.i.i
                == ddt(
                    capacitance * (p.v - n.v)
                    + nonlinear_capacitance * (p.v - n.v) ** 2
                ),
            )

        circuit, _, _, _ = voltage_divider(NonlinearCharge)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "charge.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 1)
            source = compiled.models[0].source.read_text(encoding="ascii")
            self.assertIn("I(p, n) <+ ddt(", source)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(
                manifest["required_capabilities"],
                ["real_relations", "verilog_a_osdi", "verilog_a_ddt"],
            )
            result = compiled.run(Transient(stop=5 * ns, step=1 * ns))
            self.assertTrue(result.raw_file.exists())
            self.assertIn("No. of Data Rows", result.stdout)

    def test_generated_ddt_matches_rc_natural_response(self):
        class LinearCharge(Device):
            p = eport()
            n = eport()
            capacitance = 1 * u.nF
            relation = (
                p.i + n.i == 0,
                p.i.i == ddt(capacitance * (p.v - n.v)),
            )

        with Circuit() as circuit:
            load = SpiceResistor(resistance=1 * kohm)
            charge = LinearCharge()
            node = enode()
            node |= (load.p, charge.p)
            ground = enode(reference=True)
            ground |= (load.n, charge.n)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "decay.sp", verilog_a_dir=root / "va")
            )
            compiled.build_models()
            data = root / "decay.dat"
            run_file = root / "decay.run.sp"
            base = compiled.netlist.read_text(encoding="ascii").removesuffix(".end\n")
            run_file.write_text(
                base
                + "\n.ic v(n1)=1\n.control\n"
                + f"pre_osdi {compiled.models[0].osdi.resolve()}\n"
                + "set wr_singlescale\n"
                + "tran 5e-8 5e-6 uic\n"
                + f"wrdata {data.resolve()} v(n1)\n"
                + "quit\n.endc\n.end\n",
                encoding="ascii",
            )
            process = subprocess.run(
                ["ngspice", "-b", str(run_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            samples = [
                tuple(float(value) for value in line.split())
                for line in data.read_text(encoding="ascii").splitlines()
                if line.strip()
            ]
            final_time = samples[-1][0]
            final_voltage = samples[-1][-1]
            expected = math.exp(-final_time / 1e-6)
            self.assertAlmostEqual(final_voltage, expected, delta=2e-4)

    def test_non_ngspice_target_is_rejected(self):
        circuit, _, _, _ = voltage_divider()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "not xyce"):
                circuit.compile(
                    Spice(
                        simulator="xyce",
                        netlist=Path(directory) / "divider.sp",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )

    def test_ngspice_control_path_with_whitespace_is_rejected(self):
        circuit, _, _, _ = voltage_divider()
        with tempfile.TemporaryDirectory(prefix="pyrilog path ") as directory:
            compiled = circuit.compile(
                Spice(netlist=Path(directory) / "divider.sp", verilog_a_dir=Path(directory) / "va")
            )
            with self.assertRaisesRegex(BackendCapabilityError, "cannot contain whitespace"):
                compiled.run(OperatingPoint())

    def test_floating_component_is_rejected(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            load = Resistor()
            floating = Resistor()
            source.p | load.p
            ground = enode(reference=True)
            ground |= (source.n, load.n)
            floating.p | floating.n
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TopologyError, "floating electrical component"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "floating.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_unconnected_explicit_node_is_rejected(self):
        circuit, _, _, _ = voltage_divider()
        with circuit:
            spare = enode()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TopologyError, "unconnected electrical nodes"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "isolated.sp", verilog_a_dir=Path(directory) / "va")
                )
        self.assertEqual(spare.ports, [])

    def test_zero_native_resistance_is_rejected(self):
        circuit, _, _, _ = voltage_divider(resistance=0 * ohm)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CompilationError, "SPICE R value must be positive"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "short.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_standard_rlc_values_must_be_positive_and_finite(self):
        cases = (
            (SpiceResistor, "resistance", 0 * ohm, "SPICE R"),
            (SpiceCapacitor, "capacitance", 0 * u.F, "SPICE C"),
            (SpiceInductor, "inductance", float("nan") * u.H, "SPICE L"),
        )
        for device_class, parameter, value, message in cases:
            with self.subTest(device=device_class.__name__):
                with Circuit() as circuit:
                    device = device_class(**{parameter: value})
                    ground = enode(reference=True)
                    ground |= (device.p, device.n)
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(CompilationError, message):
                        circuit.compile(
                            Spice(
                                netlist=Path(directory) / "invalid.sp",
                                verilog_a_dir=Path(directory) / "va",
                            )
                        )

    def test_two_terminal_lowering_requires_explicit_current_conservation(self):
        class MissingConservation(Device):
            p = eport()
            n = eport()
            conductance = 1e-3 * (A / V)
            relation = p.i.i == conductance * (p.v - n.v)

        circuit, _, _, _ = voltage_divider(MissingConservation)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "current-conservation"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "missing_kcl.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_verilog_a_parameter_name_collisions_are_rejected(self):
        class CollidingParameters(Device):
            p = eport()
            n = eport()
            gain_A = 1e-3 * (A / V)
            gain_a = 2e-3 * (A / V)
            relation = (
                p.i + n.i == 0,
                p.i.i == (gain_A + gain_a) * (p.v - n.v),
            )

        circuit, _, _, _ = voltage_divider(CollidingParameters)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CompilationError, "collide after normalization"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "collision.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_verilog_a_parameters_must_be_finite_real_scalars(self):
        class UnboundedConductance(Device):
            p = eport()
            n = eport()
            conductance = 1e-3 * (A / V)
            relation = (
                p.i + n.i == 0,
                p.i.i == conductance * (p.v - n.v),
            )

        for value, message in (
            (float("nan") * (A / V), "must be finite"),
            ((1 + 1j) * (A / V), "must be a real scalar"),
        ):
            with self.subTest(value=value):
                circuit, _, _, _ = voltage_divider(
                    UnboundedConductance, conductance=value
                )
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(CompilationError, message):
                        circuit.compile(
                            Spice(
                                netlist=Path(directory) / "invalid_va.sp",
                                verilog_a_dir=Path(directory) / "va",
                            )
                        )

        class InvalidDefault(Device):
            p = eport()
            n = eport()
            conductance = float("nan") * (A / V)
            relation = (
                p.i + n.i == 0,
                p.i.i == conductance * (p.v - n.v),
            )

        circuit, _, _, _ = voltage_divider(
            InvalidDefault, conductance=1e-3 * (A / V)
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CompilationError, "default must be finite"):
                circuit.compile(
                    Spice(
                        netlist=Path(directory) / "invalid_default.sp",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )

    def test_dimensioned_zero_does_not_bypass_unit_check(self):
        class BadZero(Device):
            p = eport()
            n = eport()
            relation = (
                p.i + n.i == 0,
                p.i.i == 0 * V,
            )

        circuit, _, _, _ = voltage_divider(BadZero)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CompilationError, "dimension mismatch"):
                circuit.compile(
                    Spice(netlist=Path(directory) / "bad.sp", verilog_a_dir=Path(directory) / "va")
                )

    def test_compiled_graph_is_frozen(self):
        circuit, _, load, _ = voltage_divider()
        with tempfile.TemporaryDirectory() as directory:
            circuit.compile(
                Spice(netlist=Path(directory) / "divider.sp", verilog_a_dir=Path(directory) / "va")
            )
            count = len(circuit.graph.nodes)
            with self.assertRaisesRegex(TopologyError, "frozen"):
                with circuit:
                    enode()
            self.assertEqual(len(circuit.graph.nodes), count)
            with self.assertRaisesRegex(ParameterError, "parameters are frozen"):
                load.resistance = 2 * kohm


if __name__ == "__main__":
    unittest.main()
