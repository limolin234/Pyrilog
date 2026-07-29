"""Compilation target declarations."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Spice:
    simulator: str = "ngspice"
    netlist: str | Path = "build/model.sp"
    verilog_a_dir: str | Path = "build/verilog_a"

    def __post_init__(self):
        object.__setattr__(self, "netlist", Path(self.netlist))
        object.__setattr__(self, "verilog_a_dir", Path(self.verilog_a_dir))
