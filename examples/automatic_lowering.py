"""Generate inspectable SPICE and Verilog-A from relation-only devices."""

from pathlib import Path

from pyrilog import Circuit, Device, V, ddt, enode, eport, u
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import Spice


class LinearAmplifier(Device):
    out_p = eport()
    out_n = eport()
    in_p = eport()
    in_n = eport()
    gain = 3.0

    relation = (
        out_p.i + out_n.i == 0,
        in_p.i == 0,
        in_n.i == 0,
        out_p.v - out_n.v == gain * (in_p.v - in_n.v),
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


class NonlinearCharge(Device):
    p = eport()
    n = eport()
    capacitance = 1 * u.pF
    nonlinear_capacitance = 0.1 * u.pF / V

    relation = (
        p.i + n.i == 0,
        p.i
        == ddt(
            capacitance * (p.v - n.v)
            + nonlinear_capacitance * (p.v - n.v) ** 2
        ),
    )


def build_example(output_root: Path):
    with Circuit() as circuit:
        source = VoltageSource(dc=0.1 * V)
        linear = LinearAmplifier()
        nonlinear = NonlinearAmplifier()
        charge = NonlinearCharge()
        linear_load = Resistor(resistance=1 * u.kohm)
        nonlinear_load = Resistor(resistance=1 * u.kohm)

        input_node = enode()
        input_node |= (source.p, linear.in_p, nonlinear.in_p, charge.p)

        linear_output = enode()
        linear_output |= (linear.out_p, linear_load.p)

        nonlinear_output = enode()
        nonlinear_output |= (nonlinear.out_p, nonlinear_load.p)

        circuit.GND |= (
            source.n,
            linear.in_n,
            linear.out_n,
            nonlinear.in_n,
            nonlinear.out_n,
            charge.n,
            linear_load.n,
            nonlinear_load.n,
        )

    return circuit.compile(
        Spice(
            netlist=output_root / "model.sp",
            verilog_a_dir=output_root / "verilog_a",
        )
    )


if __name__ == "__main__":
    output_root = Path("examples/generated/automatic_lowering")
    compiled = build_example(output_root)
    print(f"SPICE: {compiled.netlist}")
    print(f"Manifest: {compiled.manifest}")
    for model in compiled.models:
        print(f"Verilog-A: {model.source}")
