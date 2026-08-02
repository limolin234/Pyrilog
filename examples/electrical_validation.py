"""Cross-check progressively larger native electrical circuits in ngspice and Xyce."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyrilog import Circuit, Device, V, enode, eport, kohm, u
from pyrilog.devices import CurrentSource, NPN, Resistor, VoltageControlledVoltageSource
from pyrilog.devices import VoltageSource
from pyrilog.model import Node
from pyrilog.simulation import OperatingPoint, Spice


@dataclass(frozen=True)
class Benchmark:
    name: str
    circuit: Circuit
    observables: dict[str, Node]
    comparison_tolerances: dict[str, float]
    expected_ranges: dict[str, tuple[float, float]]


class VCVSOpAmp(Device):
    noninverting = eport()
    inverting = eport()
    output = eport()
    reference = eport()
    gain = 1e5

    core = VoltageControlledVoltageSource(gain=gain)
    noninverting | core.cp
    inverting | core.cn
    output | core.p
    reference | core.n


def bjt_bias() -> Benchmark:
    with Circuit() as circuit:
        supply = VoltageSource(dc=5 * V)
        bias = VoltageSource(dc=0.65 * V)
        load = Resistor(resistance=1 * kohm)
        transistor = NPN(forward_beta=120.0)
        supply_node = enode()
        supply_node |= (supply.p, load.p)
        collector = enode()
        collector |= (load.n, transistor.collector)
        base = enode()
        base |= (bias.p, transistor.base)
        circuit.GND |= (supply.n, bias.n, transistor.emitter)
    return Benchmark(
        "bjt_bias",
        circuit,
        {"collector": collector, "base": base},
        {"collector": 1e-3, "base": 1e-9},
        {"collector": (3.0, 5.0), "base": (0.649999, 0.650001)},
    )


def differential_pair() -> Benchmark:
    with Circuit() as circuit:
        positive_supply = VoltageSource(dc=5 * V)
        negative_supply = VoltageSource(dc=-5 * V)
        left_input = VoltageSource(dc=10 * u.mV)
        right_input = VoltageSource(dc=-10 * u.mV)
        left_load = Resistor(resistance=10 * kohm)
        right_load = Resistor(resistance=10 * kohm)
        tail = CurrentSource(dc=0.5 * u.mA)
        left = NPN()
        right = NPN()
        vcc = enode()
        vcc |= (positive_supply.p, left_load.p, right_load.p)
        vee = enode()
        vee |= (negative_supply.p, tail.n)
        left_collector = enode()
        left_collector |= (left_load.n, left.collector)
        right_collector = enode()
        right_collector |= (right_load.n, right.collector)
        emitters = enode()
        emitters |= (left.emitter, right.emitter, tail.p)
        left_base = enode()
        left_base |= (left_input.p, left.base)
        right_base = enode()
        right_base |= (right_input.p, right.base)
        circuit.GND |= (
            positive_supply.n,
            negative_supply.n,
            left_input.n,
            right_input.n,
        )
    return Benchmark(
        "differential_pair",
        circuit,
        {
            "left_collector": left_collector,
            "right_collector": right_collector,
            "emitters": emitters,
        },
        {"left_collector": 5e-5, "right_collector": 5e-5, "emitters": 5e-5},
        {
            "left_collector": (1.0, 2.5),
            "right_collector": (2.5, 4.5),
            "emitters": (-0.7, -0.5),
        },
    )


def transistor_opamp_follower() -> Benchmark:
    """A compact two-stage discrete amplifier closed as a voltage follower."""

    with Circuit() as circuit:
        positive_supply = VoltageSource(dc=5 * V)
        negative_supply = VoltageSource(dc=-5 * V)
        signal = VoltageSource(dc=0.1 * V)
        left_load = Resistor(resistance=10 * kohm)
        right_load = Resistor(resistance=10 * kohm)
        tail = CurrentSource(dc=0.2 * u.mA)
        input_left = NPN()
        input_right = NPN()
        gain_stage = NPN(forward_beta=1000.0)
        emitter_bias = Resistor(resistance=1 * kohm)
        collector_load = Resistor(resistance=1.263 * kohm)

        vcc = enode()
        vcc |= (positive_supply.p, collector_load.p)
        vee = enode()
        vee |= (negative_supply.p, tail.n, emitter_bias.n)
        left_collector = enode()
        left_collector |= (left_load.n, input_left.collector, gain_stage.base)
        right_collector = enode()
        right_collector |= (right_load.n, input_right.collector)
        input_emitters = enode()
        input_emitters |= (input_left.emitter, input_right.emitter, tail.p)
        gain_emitter = enode()
        gain_emitter |= (gain_stage.emitter, emitter_bias.p)
        output = enode()
        output |= (collector_load.n, gain_stage.collector, input_right.base)
        noninverting = enode()
        noninverting |= (signal.p, input_left.base)
        circuit.GND |= (
            positive_supply.n,
            negative_supply.n,
            signal.n,
            left_load.p,
            right_load.p,
        )
    return Benchmark(
        "transistor_opamp_follower",
        circuit,
        {"input": noninverting, "output": output, "gain_base": left_collector},
        {"input": 1e-9, "output": 5e-5, "gain_base": 5e-5},
        {
            "input": (0.099999, 0.100001),
            "output": (0.099, 0.101),
            "gain_base": (-0.55, -0.4),
        },
    )


def vcvs_inverting_amplifier() -> Benchmark:
    with Circuit() as circuit:
        signal = VoltageSource(dc=0.2 * V)
        amplifier = VCVSOpAmp()
        input_resistor = Resistor(resistance=10 * kohm)
        feedback_resistor = Resistor(resistance=20 * kohm)
        load = Resistor(resistance=5 * kohm)
        source_node = enode()
        source_node |= (signal.p, input_resistor.p)
        summing_node = enode()
        summing_node |= (input_resistor.n, feedback_resistor.n, amplifier.inverting)
        output = enode()
        output |= (amplifier.output, feedback_resistor.p, load.p)
        circuit.GND |= (
            signal.n,
            amplifier.reference,
            amplifier.noninverting,
            load.n,
        )
    return Benchmark(
        "vcvs_inverting_amplifier",
        circuit,
        {"summing_node": summing_node, "output": output},
        {"summing_node": 1e-8, "output": 1e-8},
        {"summing_node": (0.0, 1e-5), "output": (-0.4001, -0.3998)},
    )


def vcvs_summing_amplifier() -> Benchmark:
    """A hierarchical op-amp reused in a two-input weighted summing circuit."""

    with Circuit() as circuit:
        first_source = VoltageSource(dc=0.1 * V)
        second_source = VoltageSource(dc=0.2 * V)
        amplifier = VCVSOpAmp()
        first_input = Resistor(resistance=10 * kohm)
        second_input = Resistor(resistance=10 * kohm)
        feedback = Resistor(resistance=20 * kohm)
        load = Resistor(resistance=5 * kohm)
        first_node = enode()
        first_node |= (first_source.p, first_input.p)
        second_node = enode()
        second_node |= (second_source.p, second_input.p)
        summing_node = enode()
        summing_node |= (
            first_input.n,
            second_input.n,
            feedback.n,
            amplifier.inverting,
        )
        output = enode()
        output |= (amplifier.output, feedback.p, load.p)
        circuit.GND |= (
            first_source.n,
            second_source.n,
            amplifier.noninverting,
            amplifier.reference,
            load.n,
        )
    return Benchmark(
        "vcvs_summing_amplifier",
        circuit,
        {"summing_node": summing_node, "output": output},
        {"summing_node": 1e-8, "output": 1e-8},
        {"summing_node": (0.0, 1e-5), "output": (-0.6002, -0.5997)},
    )


def _read_ascii_raw(path: Path) -> dict[str, float]:
    lines = path.read_text(encoding="ascii").splitlines()
    variables_at = lines.index("Variables:")
    values_at = lines.index("Values:")
    names = [line.split()[1].lower() for line in lines[variables_at + 1 : values_at]]
    values: list[float] = []
    for line in lines[values_at + 1 :]:
        parts = line.split()
        if parts:
            values.append(float(parts[-1]))
        if len(values) == len(names):
            break
    return dict(zip(names, values, strict=True))


def _node_voltage(point: dict[str, float], backend_node: str) -> float:
    for name in (f"v({backend_node})", backend_node):
        if name.lower() in point:
            return point[name.lower()]
    raise KeyError(f"raw output does not contain voltage for {backend_node}")


def _finite_error(left: float, right: float, label: str) -> float:
    if not math.isfinite(left) or not math.isfinite(right):
        raise AssertionError(f"{label}: backend returned a non-finite voltage")
    error = abs(left - right)
    if not math.isfinite(error):
        raise AssertionError(f"{label}: voltage error is not finite")
    return error


def _xyce_operating_point(netlist: Path, raw_file: Path, executable: str) -> dict[str, float]:
    deck = netlist.with_name(f"{netlist.stem}.xyce.cir")
    base = netlist.read_text(encoding="ascii").removesuffix(".end\n")
    deck.write_text(base + "\n.OP\n.end\n", encoding="ascii")
    process = subprocess.run(
        [executable, "-a", "-r", str(raw_file), str(deck)],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0 or not raw_file.exists():
        raise RuntimeError(f"Xyce failed for {netlist}:\n{process.stdout}{process.stderr}")
    return _read_ascii_raw(raw_file)


def run_validation(build_root: Path = Path("build/electrical_validation")) -> None:
    ngspice = shutil.which("ngspice") or "/home/limolin/Myapps/ngspice/bin/ngspice"
    xyce = shutil.which("Xyce") or "/home/limolin/Myapps/xyce/bin/Xyce"
    for executable in (ngspice, xyce):
        if not Path(executable).is_file():
            raise FileNotFoundError(executable)

    benchmarks = (
        bjt_bias(),
        differential_pair(),
        transistor_opamp_follower(),
        vcvs_inverting_amplifier(),
        vcvs_summing_amplifier(),
    )
    for benchmark in benchmarks:
        root = build_root / benchmark.name
        compiled = benchmark.circuit.compile(
            Spice(netlist=root / "model.sp", verilog_a_dir=root / "verilog_a")
        )
        manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
        ngspice_point = _read_ascii_raw(
            compiled.run(OperatingPoint(), ngspice=ngspice).raw_file
        )
        xyce_point = _xyce_operating_point(compiled.netlist, root / "xyce.raw", xyce)

        maximum_error = 0.0
        values = []
        for label, node in benchmark.observables.items():
            backend_node = manifest["nodes"][node.stable_id]
            ngspice_value = _node_voltage(ngspice_point, backend_node)
            xyce_value = _node_voltage(xyce_point, backend_node)
            error = _finite_error(
                ngspice_value, xyce_value, f"{benchmark.name}.{label}"
            )
            maximum_error = max(maximum_error, error)
            values.append(f"{label}={ngspice_value:.9g}/{xyce_value:.9g} V")
            tolerance = benchmark.comparison_tolerances[label]
            if error > tolerance:
                raise AssertionError(
                    f"{benchmark.name}.{label}: ngspice/Xyce error {error:.3g} V "
                    f"exceeds {tolerance:.3g} V"
                )
            lower, upper = benchmark.expected_ranges[label]
            if not (
                lower <= ngspice_value <= upper
                and lower <= xyce_value <= upper
            ):
                raise AssertionError(
                    f"{benchmark.name}.{label}: expected [{lower}, {upper}] V; "
                    f"ngspice={ngspice_value:.9g} V, Xyce={xyce_value:.9g} V"
                )
        print(f"{benchmark.name}: max_error={maximum_error:.3g} V; " + ", ".join(values))


if __name__ == "__main__":
    run_validation()
