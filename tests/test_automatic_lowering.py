from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from examples.automatic_lowering import build_example
from pyrilog import BackendCapabilityError, V, Circuit, Device, ddt, enode, eport, exp, localparam, u
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import CompilationError, OperatingPoint, Spice


class RelationResistor(Device):
    p = eport()
    n = eport()
    resistance = 2 * u.kohm
    relation = (p.i + n.i == 0, p.v - n.v == resistance * p.i)


class NamedRelationResistor(Device):
    left = eport()
    right = eport()
    resistance = 2 * u.kohm
    relation = (
        left.i + right.i == 0,
        left.v - right.v == resistance * left.i,
    )


class RelationCapacitor(Device):
    p = eport()
    n = eport()
    capacitance = 2 * u.pF
    relation = (
        p.i + n.i == 0,
        p.i == capacitance * ddt(p.v - n.v),
    )


class RelationInductor(Device):
    p = eport()
    n = eport()
    inductance = 3 * u.nH
    relation = (
        p.i + n.i == 0,
        p.v - n.v == inductance * ddt(p.i),
    )


class RelationVoltageSource(Device):
    p = eport()
    n = eport()
    dc = 1.5 * V
    relation = (p.i + n.i == 0, p.v - n.v == dc)


class RelationCurrentSource(Device):
    p = eport()
    n = eport()
    dc = 2 * u.mA
    relation = (p.i + n.i == 0, p.i == dc)


class RelationVCVS(Device):
    out_p = eport()
    out_n = eport()
    ctrl_p = eport()
    ctrl_n = eport()
    gain = 3.0
    relation = (
        out_p.i + out_n.i == 0,
        ctrl_p.i == 0,
        ctrl_n.i == 0,
        out_p.v - out_n.v == gain * (ctrl_p.v - ctrl_n.v),
    )


class RelationVCCS(Device):
    out_p = eport()
    out_n = eport()
    ctrl_p = eport()
    ctrl_n = eport()
    transconductance = 2 * u.mS
    relation = (
        out_p.i + out_n.i == 0,
        ctrl_p.i == 0,
        ctrl_n.i == 0,
        out_p.i == transconductance * (ctrl_p.v - ctrl_n.v),
    )


class NonlinearAmplifier(Device):
    out_p = eport()
    out_n = eport()
    in_p = eport()
    in_n = eport()
    gain = 2.0
    cubic = 10 / V**2
    relation = (
        out_p.i + out_n.i == 0,
        in_p.i == 0,
        in_n.i == 0,
        out_p.v - out_n.v
        == gain * (in_p.v - in_n.v) + cubic * (in_p.v - in_n.v) ** 3,
    )


class FunctionalVoltageSource(Device):
    p = eport()
    n = eport()
    scale = 2 * V
    exponent = 0.0
    relation = (p.i + n.i == 0, p.v - n.v == scale * exp(exponent))


class LocalParamVoltageSource(Device):
    p = eport()
    n = eport()
    scale = 2 * V
    offset = localparam(0.25 * V)
    relation = (p.i + n.i == 0, p.v - n.v == scale * exp(offset / scale))


def read_raw_point(path: Path) -> dict[str, float]:
    lines = path.read_text(encoding="ascii").splitlines()
    variables_at = lines.index("Variables:")
    values_at = lines.index("Values:")
    names = [line.split()[1] for line in lines[variables_at + 1 : values_at]]
    values = [
        float(line.split()[-1])
        for line in lines[values_at + 1 :]
        if line.split()
    ][: len(names)]
    return dict(zip(names, values, strict=True))


class AutomaticLoweringTests(unittest.TestCase):
    def test_localparam_is_emitted_as_verilog_a_localparam(self):
        with Circuit() as circuit:
            source = LocalParamVoltageSource()
            load = Resistor(resistance=1 * u.kohm)
            source.p | load.p
            circuit.GND |= (source.n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "localparam.sp", verilog_a_dir=root / "va")
            )
            source_text = compiled.models[0].source.read_text(encoding="ascii")
            self.assertIn("localparam real offset", source_text)
            self.assertNotIn("parameter real offset", source_text)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instance = next(item for item in manifest["instances"] if item["source"].endswith("LocalParamVoltageSource"))
            self.assertNotIn("offset", instance["parameters"])
    def test_automatic_native_lowering_preserves_user_port_names(self):
        with Circuit() as circuit:
            device = NamedRelationResistor()
            circuit.GND |= (device.left, device.right)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "named.sp", verilog_a_dir=root / "va")
            )
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            self.assertEqual(
                manifest["instances"][0]["ports"], {"left": "0", "right": "0"}
            )

    def test_inspectable_generated_sources_are_current_and_executable(self):
        checked_root = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "generated"
            / "automatic_lowering"
        )
        with tempfile.TemporaryDirectory() as directory:
            compiled = build_example(Path(directory))
            self.assertEqual(
                compiled.netlist.read_text(encoding="ascii"),
                (checked_root / "model.sp").read_text(encoding="ascii"),
            )
            generated_sources = {
                model.source.name: model.source.read_text(encoding="ascii")
                for model in compiled.models
            }
            checked_sources = {
                path.name: path.read_text(encoding="ascii")
                for path in (checked_root / "verilog_a").glob("*.va")
            }
            self.assertEqual(generated_sources, checked_sources)
            checked_manifest = json.loads(
                (checked_root / "model.manifest.json").read_text(encoding="ascii")
            )
            self.assertFalse(Path(checked_manifest["netlist"]).is_absolute())
            self.assertTrue(
                all(
                    not Path(model[path_key]).is_absolute()
                    for model in checked_manifest["models"]
                    for path_key in ("source", "osdi")
                )
            )
            selections = {
                item["stable_id"]: item["lowering"]["selection"]
                for item in checked_manifest["instances"]
            }
            self.assertEqual(selections["linear_amplifier_1"], "relation_match")
            self.assertEqual(selections["nonlinear_amplifier_1"], "relation_fallback")
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instances = {item["stable_id"]: item for item in manifest["instances"]}
            linear_node = instances["linear_amplifier_1"]["ports"]["out_p"]
            nonlinear_node = instances["nonlinear_amplifier_1"]["ports"]["out_p"]
            self.assertAlmostEqual(point[f"v({linear_node})"], 0.3)
            self.assertAlmostEqual(point[f"v({nonlinear_node})"], 0.21)

    def test_two_terminal_relations_select_native_spice(self):
        cases = (
            (RelationResistor, "R"),
            (RelationCapacitor, "C"),
            (RelationInductor, "L"),
            (RelationVoltageSource, "V"),
            (RelationCurrentSource, "I"),
        )
        for device_class, designator in cases:
            with self.subTest(device=device_class.__name__):
                with Circuit() as circuit:
                    device = device_class()
                    circuit.GND |= (device.p, device.n)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    compiled = circuit.compile(
                        Spice(netlist=root / "model.sp", verilog_a_dir=root / "va")
                    )
                    manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
                    self.assertEqual(
                        manifest["instances"][0]["lowering"],
                        {
                            "kind": "native",
                            "primitive": designator,
                            "selection": "relation_match",
                        },
                    )
                    self.assertEqual(compiled.models, ())

    def test_four_terminal_relations_select_native_e_and_g(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=0.2 * V)
            vcvs = RelationVCVS()
            vccs = RelationVCCS()
            voltage_load = Resistor(resistance=1 * u.kohm)
            current_load = Resistor(resistance=1 * u.kohm)
            control = enode()
            control |= (source.p, vcvs.ctrl_p, vccs.ctrl_p)
            voltage_output = enode()
            voltage_output |= (vcvs.out_p, voltage_load.p)
            current_output = enode()
            current_output |= (vccs.out_p, current_load.p)
            circuit.GND |= (
                source.n,
                vcvs.out_n,
                vcvs.ctrl_n,
                vccs.out_n,
                vccs.ctrl_n,
                voltage_load.n,
                current_load.n,
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "controlled.sp", verilog_a_dir=root / "va")
            )
            netlist = compiled.netlist.read_text(encoding="ascii")
            self.assertRegex(netlist, r"E1 n\d+ 0 n\d+ 0 3")
            self.assertRegex(netlist, r"G1 n\d+ 0 n\d+ 0 0\.002")
            self.assertEqual(compiled.models, ())
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instances = {item["backend_name"]: item for item in manifest["instances"]}
            self.assertAlmostEqual(point[f"v({instances['E1']['ports']['out_p']})"], 0.6)
            self.assertAlmostEqual(point[f"v({instances['G1']['ports']['out_p']})"], -0.4)

    def test_nonlinear_four_terminal_relation_generates_verilog_a(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=0.1 * V)
            amplifier = NonlinearAmplifier()
            load = Resistor(resistance=1 * u.kohm)
            control = enode()
            control |= (source.p, amplifier.in_p)
            output = enode()
            output |= (amplifier.out_p, load.p)
            circuit.GND |= (source.n, amplifier.in_n, amplifier.out_n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "nonlinear.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 1)
            source_text = compiled.models[0].source.read_text(encoding="ascii")
            self.assertIn("module nonlinearamplifier_", source_text)
            self.assertIn("V(out_p, out_n) <+", source_text)
            self.assertIn("pow((V(in_p) - V(in_n)), 3)", source_text)
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            instance = next(
                item for item in manifest["instances"] if item["stable_id"] == "nonlinear_amplifier_1"
            )
            self.assertAlmostEqual(point[f"v({instance['ports']['out_p']})"], 0.21)

    def test_parameter_function_source_falls_back_to_verilog_a(self):
        with Circuit() as circuit:
            source = FunctionalVoltageSource()
            load = Resistor(resistance=1 * u.kohm)
            output = enode()
            output |= (source.p, load.p)
            circuit.GND |= (source.n, load.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "functional.sp", verilog_a_dir=root / "va")
            )
            self.assertEqual(len(compiled.models), 1)
            source_text = compiled.models[0].source.read_text(encoding="ascii")
            self.assertIn("V(p, n) <+ (scale * exp(exponent));", source_text)
            point = read_raw_point(compiled.run(OperatingPoint()).raw_file)
            manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
            output_node = manifest["instances"][0]["ports"]["p"]
            self.assertAlmostEqual(point[f"v({output_node})"], 2.0)

    def test_native_source_evaluates_only_the_declared_arithmetic_operator(self):
        class OffsetSource(Device):
            p = eport()
            n = eport()
            dc = 1 * V
            zero = 0 * V
            relation = (p.i + n.i == 0, p.v - n.v == dc + zero)

        with Circuit() as circuit:
            source = OffsetSource()
            output = enode()
            output |= source.p
            circuit.GND |= source.n
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "offset.sp", verilog_a_dir=root / "va")
            )
            self.assertRegex(
                compiled.netlist.read_text(encoding="ascii"), r"V1 n\d+ 0 1\n"
            )

    def test_invalid_native_source_arithmetic_is_a_compilation_error(self):
        class InvalidSource(Device):
            p = eport()
            n = eport()
            dc = 1 * V
            divisor = 0.0
            relation = (p.i + n.i == 0, p.v - n.v == dc / divisor)

        with Circuit() as circuit:
            source = InvalidSource()
            circuit.GND |= (source.p, source.n)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netlist = root / "invalid.sp"
            with self.assertRaisesRegex(
                CompilationError, "cannot evaluate native source expression"
            ):
                circuit.compile(Spice(netlist=netlist, verilog_a_dir=root / "va"))
            self.assertFalse(netlist.exists())
            self.assertFalse(netlist.with_suffix(".manifest.json").exists())

    def test_local_relation_count_cannot_exceed_port_count(self):
        class Overconstrained(Device):
            p = eport()
            n = eport()
            relation = (
                p.i + n.i == 0,
                p.v - n.v == 1 * V,
                p.v - n.v == 2 * V,
            )

        with Circuit() as circuit:
            device = Overconstrained()
            circuit.GND |= (device.p, device.n)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CompilationError, "constraint budget"):
                circuit.compile(
                    Spice(
                        netlist=Path(directory) / "overconstrained.sp",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )

    def test_multiple_voltage_equations_cannot_drive_one_physical_branch(self):
        class DuplicateBranch(Device):
            p = eport()
            n = eport()
            relation = (
                p.v - n.v == 1 * V,
                n.v - p.v == -1 * V,
            )

        with Circuit() as circuit:
            device = DuplicateBranch()
            circuit.GND |= (device.p, device.n)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "same physical branch"):
                circuit.compile(
                    Spice(
                        netlist=Path(directory) / "duplicate.sp",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )

    def test_voltage_and_current_cannot_drive_one_physical_branch(self):
        class MixedBranchDrive(Device):
            p = eport()
            n = eport()
            unused = eport()
            relation = (
                p.i + n.i == 0,
                p.v - n.v == 1 * V,
                p.i == 1 * u.mA,
            )

        with Circuit() as circuit:
            device = MixedBranchDrive()
            circuit.GND |= (device.p, device.n, device.unused)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendCapabilityError, "same physical branch"):
                circuit.compile(
                    Spice(
                        netlist=Path(directory) / "mixed-drive.sp",
                        verilog_a_dir=Path(directory) / "va",
                    )
                )


if __name__ == "__main__":
    unittest.main()
