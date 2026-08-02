import tempfile
import unittest
from pathlib import Path

from pyrilog import Circuit, Device, V, eport, u
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import OperatingPoint, Spice


class ResistorSection(Device):
    p = eport()
    n = eport()
    load = Resistor(resistance=2 * u.kohm)
    p | load.p
    load.n | n


class HierarchicalSpiceTests(unittest.TestCase):
    def test_native_hierarchy_publishes_auditable_files(self):
        with Circuit() as circuit:
            source = VoltageSource(dc=1 * V)
            section = ResistorSection()
            source.p | section.p
            circuit.GND |= (source.n, section.n)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiled = circuit.compile(
                Spice(netlist=root / "main.cir", verilog_a_dir=root / "verilog_a")
            )
            self.assertIsNone(compiled.dev_directory)
            self.assertEqual(compiled.subckt_directory, root / "subckt")
            self.assertFalse((root / "dev").exists())
            subckt = root / "subckt" / "sc_resistor_section_1.cir"
            subckt_text = subckt.read_text()
            self.assertIn(".subckt sc_resistor_section_1 p n", subckt_text)
            self.assertIn("R1 p n 2000", subckt_text)
            self.assertNotIn(".include", subckt_text)
            self.assertIn("Xresistor_section_1", compiled.netlist.read_text())
            result = compiled.run(OperatingPoint())
            self.assertTrue(result.raw_file.exists())


if __name__ == "__main__":
    unittest.main()
