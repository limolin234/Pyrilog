"""Publish a native SPICE hierarchy into build/main.cir and subckt/."""

from pathlib import Path

from pyrilog import Circuit, Device, V, eport, enode, u
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import Spice


class DividerSection(Device):
    p = eport()
    n = eport()
    resistance = 2 * u.kohm

    load = Resistor(resistance=resistance)
    p | load.p
    load.n | n


with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    section = DividerSection()
    source.p | section.p
    circuit.GND |= (source.n, section.n)


compiled = circuit.compile(
    Spice(
        netlist=Path("build/main.cir"),
        verilog_a_dir=Path("build/verilog_a"),
    )
)
print(compiled.netlist)
print(compiled.dev_directory)
print(compiled.subckt_directory)
