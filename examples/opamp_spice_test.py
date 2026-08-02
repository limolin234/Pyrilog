"""Exercise relation lowering with a small transistor feedback amplifier."""

from __future__ import annotations

import csv
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pyrilog import A, Circuit, Device, V, enode, eport, exp, kohm, localparam, u
from pyrilog.devices import CurrentSource, Resistor, VoltageSource
from pyrilog.simulation import Spice


class NPNManual(Device):
    """Minimal Ebers-Moll-style NPN written entirely as local relations."""

    collector = eport()
    base = eport()
    emitter = eport()

    saturation_current = 1e-14 * A
    forward_beta = 100.0
    thermal_voltage = localparam(25.85 * u.mV)

    relation = (
        collector.i
        == saturation_current
        * (exp((base.v - emitter.v) / thermal_voltage) - 1),
        base.i == collector.i / forward_beta,
        emitter.i + collector.i + base.i == 0,
    )


def build_follower():
    """Three-NPN, two-stage amplifier closed as a voltage follower."""
    with Circuit() as circuit:
        positive_supply = VoltageSource(dc=5 * V)
        negative_supply = VoltageSource(dc=-5 * V)
        signal = VoltageSource(dc=0 * V)
        left_load = Resistor(resistance=10 * kohm)
        right_load = Resistor(resistance=10 * kohm)
        tail = CurrentSource(dc=0.2 * u.mA)
        input_left = NPNManual()
        input_right = NPNManual()
        gain_stage = NPNManual(forward_beta=1000.0)
        emitter_bias = Resistor(resistance=1 * kohm)
        collector_load = Resistor(resistance=1.272 * kohm)

        vcc = enode()
        vcc |= (positive_supply.p, collector_load.p)
        vee = enode()
        vee |= (negative_supply.p, tail.n, emitter_bias.n)
        gain_base = enode()
        gain_base |= (left_load.n, input_left.collector, gain_stage.base)
        right_collector = enode()
        right_collector |= (right_load.n, input_right.collector)
        input_emitters = enode()
        input_emitters |= (input_left.emitter, input_right.emitter, tail.p)
        gain_emitter = enode()
        gain_emitter |= (gain_stage.emitter, emitter_bias.p)
        output = enode()
        output |= (collector_load.n, gain_stage.collector, input_right.base)
        input_node = enode()
        input_node |= (signal.p, input_left.base)
        circuit.GND |= (
            positive_supply.n,
            negative_supply.n,
            signal.n,
            left_load.p,
            right_load.p,
        )
    return circuit, signal, input_node, output


def _read_ascii_raw(path: Path) -> dict[str, list[float]]:
    lines = path.read_text(encoding="ascii").splitlines()
    variables_at = lines.index("Variables:")
    values_at = lines.index("Values:")
    names = [line.split()[1].lower() for line in lines[variables_at + 1 : values_at]]
    values = {name: [] for name in names}
    cursor = values_at + 1
    while cursor < len(lines):
        first = lines[cursor].split()
        if not first:
            cursor += 1
            continue
        point = [float(first[-1])]
        cursor += 1
        while len(point) < len(names) and cursor < len(lines):
            tokens = lines[cursor].split()
            cursor += 1
            if tokens:
                point.append(float(tokens[-1]))
        if len(point) == len(names):
            for name, value in zip(names, point, strict=True):
                values[name].append(value)
    return values


def _linear_slope(x: list[float], y: list[float]) -> float:
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    denominator = sum((a - mean_x) ** 2 for a in x)
    return numerator / denominator


def run(output_root: Path = Path("examples/generated/opamp_spice_test")) -> dict[str, Any]:
    ngspice = shutil.which("ngspice") or "/home/limolin/Myapps/ngspice/bin/ngspice"
    if not Path(ngspice).is_file():
        raise FileNotFoundError(ngspice)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    circuit, signal, input_node, output_node = build_follower()
    compiled = circuit.compile(
        Spice(
            netlist=output_root / "compiled.cir",
            verilog_a_dir=output_root / "verilog_a",
        )
    )
    compiled.build_models()
    manifest = json.loads(compiled.manifest.read_text(encoding="ascii"))
    instances = {item["stable_id"]: item for item in manifest["instances"]}
    source_name = instances[signal.stable_id]["backend_name"]
    input_name = manifest["nodes"][input_node.stable_id]
    output_name = manifest["nodes"][output_node.stable_id]
    raw_path = output_root / "dc.raw"
    base = compiled.netlist.read_text(encoding="utf-8").removesuffix(".end\n")
    manual_deck = output_root / "manual_dc.cir"
    manual_deck.write_text(
        base
        + "\n\n* Manual stimulus and result export; not emitted by the compiler.\n"
        + ".control\n"
        + "".join(
            f"pre_osdi {model.osdi.resolve()}\n" for model in compiled.models
        )
        + f"dc {source_name} -0.2 0.2 0.002\n"
        + "set filetype=ascii\n"
        + f"write {raw_path.name} v({input_name}) v({output_name})\n"
        + "quit\n"
        + ".endc\n.end\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [ngspice, "-b", manual_deck.name],
        cwd=output_root,
        capture_output=True,
        text=True,
        check=False,
    )
    (output_root / "ngspice.stdout.txt").write_text(process.stdout, encoding="utf-8")
    (output_root / "ngspice.stderr.txt").write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0 or not raw_path.exists():
        raise RuntimeError(f"ngspice failed:\n{process.stdout}{process.stderr}")

    raw = _read_ascii_raw(raw_path)
    input_values = raw[f"v({input_name})"]
    output_values = raw[f"v({output_name})"]
    if len(input_values) != 201 or len(output_values) != 201:
        raise AssertionError("DC sweep did not return the expected 201 points")
    if not math.isclose(input_values[0], -0.2, abs_tol=1e-12) or not math.isclose(
        input_values[-1], 0.2, abs_tol=1e-12
    ):
        raise AssertionError("DC sweep endpoints do not match -0.2 V to 0.2 V")
    errors = [output - input_ for input_, output in zip(input_values, output_values, strict=True)]
    endpoint_tolerance = 1e-12
    window = [
        index
        for index, value in enumerate(input_values)
        if 0.09 - endpoint_tolerance <= value <= 0.11 + endpoint_tolerance
    ]
    window_input = [input_values[index] for index in window]
    window_output = [output_values[index] for index in window]
    window_errors = [errors[index] for index in window]
    window_gain = _linear_slope(window_input, window_output)
    maximum_window_error = max(abs(value) for value in window_errors)
    window_monotonic = all(
        right > left for left, right in zip(window_output, window_output[1:])
    )
    operating_index = min(range(len(input_values)), key=lambda index: abs(input_values[index] - 0.1))
    operating_error = abs(errors[operating_index])
    if operating_error > 4e-2:
        raise AssertionError(f"0.1 V closed-loop error is {operating_error} V")
    if maximum_window_error > 4e-2:
        raise AssertionError(f"local tracking error is {maximum_window_error} V")
    if not window_monotonic:
        raise AssertionError("local DC transfer is not monotonic around the operating point")
    if not all(math.isfinite(value) for value in (*input_values, *output_values)):
        raise AssertionError("ngspice returned a non-finite voltage")

    csv_path = output_root / "dc_transfer.csv"
    with csv_path.open("w", newline="", encoding="ascii") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("input_V", "output_V", "tracking_error_V"))
        writer.writerows(zip(input_values, output_values, errors, strict=True))

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(7.2, 7.0), sharex=True)
    axes[0].plot(input_values, input_values, "--", color="0.45", label="ideal follower")
    axes[0].plot(input_values, output_values, color="#1769aa", label="Pyrilog -> SPICE")
    axes[0].set_ylabel("Output voltage (V)")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()
    axes[1].plot(input_values, [value * 1e3 for value in errors], color="#b23a48")
    axes[1].axhline(0, color="0.45", linewidth=0.8)
    axes[1].set_xlabel("Input voltage (V)")
    axes[1].set_ylabel("Tracking error (mV)")
    axes[1].grid(True, alpha=0.3)
    figure.tight_layout()
    figure.savefig(output_root / "dc_transfer.png", dpi=160)
    plt.close(figure)

    summary = {
        "validation_scope": "relation-lowering DC smoke test; not a precision follower model",
        "points": len(input_values),
        "local_window_V": [0.09, 0.11],
        "local_closed_loop_gain": window_gain,
        "maximum_local_tracking_error_V": maximum_window_error,
        "local_transfer_monotonic": window_monotonic,
        "operating_point_input_V": input_values[operating_index],
        "operating_point_output_V": output_values[operating_index],
        "operating_point_error_V": errors[operating_index],
        "input_start_V": input_values[0],
        "input_stop_V": input_values[-1],
        "output_start_V": output_values[0],
        "output_stop_V": output_values[-1],
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
