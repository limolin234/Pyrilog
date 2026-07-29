"""Compile a hierarchical custom relation through OpenVAF-reloaded and ngspice."""

from pathlib import Path

from pyrilog import *
from pyrilog.devices import VoltageSource
from pyrilog.simulation import OperatingPoint, Spice


class Conductance(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (A / V)
    relation = (
        p.i + n.i == 0,
        p.i.i == conductance * (p.v - n.v),
    )


class ConductanceSection(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (A / V)

    load = Conductance(conductance=conductance)
    p | load.p
    load.n | n


class HierarchicalLoad(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (A / V)

    section = ConductanceSection(conductance=conductance)
    p | section.p
    section.n | n


with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    load = HierarchicalLoad()

    source.p | load.p
    circuit.GND |= (source.n, load.n)


root = Path("build/compiler_smoke")
compiled = circuit.compile(
    Spice(
        simulator="ngspice",
        netlist=root / "model.sp",
        verilog_a_dir=root / "verilog_a",
    )
)
result = compiled.run(OperatingPoint())

print(f"SPICE: {compiled.netlist}")
print(f"Verilog-A: {compiled.models[0].source}")
print(f"Manifest: {compiled.manifest}")
print(f"Hierarchy: {load.section.load.stable_id}")
print(f"ngspice raw: {result.raw_file}")
