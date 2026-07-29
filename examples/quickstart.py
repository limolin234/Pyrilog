"""Build a native-SPICE voltage divider without invoking a simulator."""

from pathlib import Path

from pyrilog import Circuit, V, kohm
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import Spice


with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    upper = Resistor(resistance=1 * kohm)
    lower = Resistor(resistance=1 * kohm)

    source.p | upper.p
    upper.n | lower.p
    circuit.GND |= (source.n, lower.n)


compiled = circuit.compile(
    Spice(netlist=Path("build/quickstart/model.sp"), verilog_a_dir=Path("build/quickstart/va"))
)

print(compiled.netlist.read_text(encoding="utf-8"))
print(f"manifest: {compiled.manifest}")
