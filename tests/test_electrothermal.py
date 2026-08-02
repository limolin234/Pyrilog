from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pyrilog import Circuit, Device, K, V, W, eport, tnode, tport, u
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.model import TopologyError
from pyrilog.simulation import OperatingPoint, Spice

from tests.test_frontend import read_raw_point


class ThermalResistance(Device):
    a = tport()
    b = tport()
    resistance = 20 * K / W
    relation = (
        a.p.i + b.p.i == 0,
        a.p.i == (a.t - b.t) / resistance,
    )


class ElectroThermalHeater(Device):
    p = eport()
    n = eport()
    resistance = 100 * u.ohm
    junction_capacity = 6 * u.uJ / K
    case_capacity = 4 * u.uJ / K
    junction_case_resistance = 5 * K / W

    junction = tnode(C=junction_capacity)
    case = tnode(C=case_capacity)
    junction_to_case = ThermalResistance(resistance=junction_case_resistance)
    junction | junction_to_case.a
    case | junction_to_case.b

    relation = (
        p.i + n.i == 0,
        p.i == (p.v - n.v) / resistance,
        junction.p == (p.v - n.v) ** 2 / resistance,
    )


class ThermalLeaf(Device):
    capacity = 2 * u.uJ / K
    initial_temperature = 300 * K
    body = tnode(C=capacity, T=initial_temperature)


class ThermalSection(Device):
    boundary = tport()
    capacity = 2 * u.uJ / K
    initial_temperature = 300 * K
    leaf = ThermalLeaf(
        capacity=capacity,
        initial_temperature=initial_temperature,
    )
    leaf.body | boundary


class ParametricThermalPair(Device):
    first_temperature = 300 * K
    second_temperature = 300 * K
    first = tnode(C=1 * u.uJ / K, T=first_temperature)
    second = tnode(C=1 * u.uJ / K, T=second_temperature)
    first | second


class NativeElectricalSection(Device):
    p = eport()
    n = eport()
    load = Resistor(resistance=1 * u.kohm)
    p | load.p
    load.n | n


class ElectrothermalTests(unittest.TestCase):
    def test_conservative_connections_chain_extend_and_merge_nodes(self):
        with Circuit() as circuit:
            first = ThermalResistance()
            second = ThermalResistance()
            third = ThermalResistance()
            node_1 = tnode(C=1 * u.uJ / K, T=300 * K)
            node_1 |= first.a | second.a
            chained = node_1 | third.a

            node_2 = tnode(C=2 * u.uJ / K, T=300 * K)
            merged = chained | node_2

        root = merged.canonical()
        self.assertIs(node_1.canonical(), root)
        self.assertIs(node_2.canonical(), root)
        self.assertEqual(root.ports, [first.a, second.a, third.a])
        self.assertTrue(all(port.connection is root for port in root.ports))
        self.assertEqual(circuit.graph.stage, "CONNECT")

    def test_conflicting_explicit_temperatures_reject_ideal_merge_atomically(self):
        with Circuit() as circuit:
            first = tnode(C=1 * u.uJ / K, T=300 * K)
            second = tnode(C=1 * u.uJ / K, T=301 * K)
            with self.assertRaisesRegex(TopologyError, "different explicit T"):
                first | second

        self.assertEqual(circuit.graph.stage, "INSTANCE")
        self.assertIs(first.canonical(), first)
        self.assertIs(second.canonical(), second)

    def test_chain_and_parameterized_temperature_conflicts_are_rejected(self):
        with Circuit() as circuit:
            unspecified = tnode(C=1 * u.uJ / K)
            first = tnode(C=1 * u.uJ / K, T=300 * K)
            second = tnode(C=1 * u.uJ / K, T=301 * K)
            merged = unspecified | first
            with self.assertRaisesRegex(TopologyError, "different explicit T"):
                merged | second
        self.assertEqual(circuit.graph.stage, "CONNECT")
        self.assertIsNot(merged.canonical(), second.canonical())

        with Circuit() as batch_circuit:
            unspecified = tnode(C=1 * u.uJ / K)
            first = tnode(C=1 * u.uJ / K, T=300 * K)
            second = tnode(C=1 * u.uJ / K, T=301 * K)
            with self.assertRaisesRegex(TopologyError, "different explicit T"):
                unspecified |= (first, second)
        self.assertEqual(batch_circuit.graph.stage, "INSTANCE")
        self.assertIs(unspecified.canonical(), unspecified)
        self.assertIs(first.canonical(), first)
        self.assertIs(second.canonical(), second)

        with self.assertRaisesRegex(TopologyError, "different explicit T"):
            ParametricThermalPair(second_temperature=301 * K)

        pair = ParametricThermalPair()
        with self.assertRaisesRegex(TopologyError, "different explicit T"):
            pair.second_temperature = 301 * K
        self.assertEqual(pair.second_temperature.value, 300 * K)
        self.assertEqual(pair.second.initial_temperature, 300 * K)

    def test_parameter_updates_refresh_internal_thermal_metadata(self):
        with Circuit():
            heater = ElectroThermalHeater(junction_capacity=7 * u.uJ / K)
        self.assertEqual(heater.junction.C, 7 * u.uJ / K)
        heater.junction_capacity = 8 * u.uJ / K
        self.assertEqual(heater.junction.C, 8 * u.uJ / K)

    def test_nested_thermal_node_is_reused_across_anonymous_boundary_connection(self):
        with Circuit() as circuit:
            section = ThermalSection(capacity=3 * u.uJ / K)
            circuit.AMBIENT | section.boundary

        boundary_node = section._boundary_nodes["boundary"]
        self.assertIs(boundary_node, section.leaf.body)
        self.assertIs(section.boundary.connection, circuit.AMBIENT)
        section.capacity = 4 * u.uJ / K
        self.assertEqual(section.leaf.body.C, 4 * u.uJ / K)

    def test_thermal_composite_selects_flat_lowering_and_checks_boundary_temperature(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            load = Resistor(resistance=1 * u.kohm)
            section = ThermalSection()
            source.p | load.p
            circuit.GND |= source.n | load.n
            circuit.AMBIENT | section.boundary

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "thermal_section.cir", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertIn("thermal_analog_mna", manifest["required_capabilities"])

        with Circuit() as conflicting:
            source = VoltageSource(dc=1 * V)
            load = Resistor(resistance=1 * u.kohm)
            section = ThermalSection(initial_temperature=301 * K)
            source.p | load.p
            conflicting.GND |= source.n | load.n
            conflicting.AMBIENT | section.boundary
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TopologyError, "different explicit T"):
                conflicting.compile(
                    Spice(
                        netlist=Path(directory) / "conflict.cir",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )

    def test_top_level_thermal_lump_prevents_pure_electrical_hierarchical_lowering(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = NativeElectricalSection()
            source.p | section.p
            circuit.GND |= source.n | section.n
            thermal = tnode(C=1 * u.uJ / K, P=1 * u.mW)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "top_level_thermal.cir", verilog_a_dir=root / "va")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertIn("thermal_analog_mna", manifest["required_capabilities"])
            self.assertIn("CTH1", netlist)
            self.assertIn("ITH1", netlist)
            self.assertEqual(manifest["nodes"][thermal.stable_id], "th1")

    def test_flatten_preserves_fixed_thermal_root(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            load = Resistor(resistance=1 * u.kohm)
            source.p | load.p
            circuit.GND |= source.n | load.n
            ordinary = tnode(C=1 * u.uJ / K)
            circuit.ambient_temperature = 310 * K
            merged = ordinary | circuit.AMBIENT

        self.assertIs(merged.canonical(), circuit.AMBIENT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "fixed_thermal.cir", verilog_a_dir=root / "va")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            merged_lumps = [
                item
                for item in manifest["thermal_lumps"]
                if item["backend_node"] == manifest["nodes"][ordinary.stable_id]
            ]
            self.assertTrue(all(item["fixed"] for item in merged_lumps))
            self.assertIn("VTH1 th1 0 310", netlist)

    def test_electrothermal_network_compiles_and_runs(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            heater = ElectroThermalHeater()
            sink = ThermalResistance(resistance=100 * K / W)

            source.p | heater.p
            circuit.GND |= source.n | heater.n
            extra_case_capacity = tnode(C=1 * u.uJ / K)
            case_temperature = extra_case_capacity | heater.case | sink.a
            ambient_temperature = circuit.AMBIENT | sink.b

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "electrothermal.cir", verilog_a_dir=root / "verilog_a")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            sources = [model.source.read_text(encoding="ascii") for model in compiled.models]

            self.assertIn("thermal_analog_mna", manifest["required_capabilities"])
            self.assertIn("VTH1", netlist)
            self.assertEqual(netlist.count("CTH"), 3)
            self.assertTrue(any("I(th_" in source for source in sources))
            self.assertTrue(any("I(a, b)" in source for source in sources))

            lumps = {item["stable_id"]: item for item in manifest["thermal_lumps"]}
            junction = lumps["electro_thermal_heater_1.junction"]
            case = lumps["electro_thermal_heater_1.case"]
            self.assertAlmostEqual(junction["capacity_si"], 6e-6)
            self.assertAlmostEqual(case["capacity_si"], 4e-6)
            self.assertEqual(case["backend_node"], manifest["nodes"][case_temperature.stable_id])
            merged_case_lumps = [
                item
                for item in manifest["thermal_lumps"]
                if item["backend_node"] == case["backend_node"]
            ]
            self.assertEqual(
                sorted(item["capacity_si"] for item in merged_case_lumps),
                [1e-6, 4e-6],
            )
            self.assertEqual(
                manifest["nodes"][ambient_temperature.stable_id],
                next(item["backend_node"] for item in manifest["thermal_lumps"] if item["fixed"]),
            )

            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            self.assertAlmostEqual(point["i(v1)"], -0.01, places=8)
            self.assertAlmostEqual(point[f"v({junction['backend_node']})"], 301.05, places=5)
            self.assertAlmostEqual(point[f"v({case['backend_node']})"], 301.0, places=5)
            self.assertAlmostEqual(
                point[f"v({manifest['nodes'][ambient_temperature.stable_id]})"],
                300.0,
                places=8,
            )


if __name__ == "__main__":
    unittest.main()
