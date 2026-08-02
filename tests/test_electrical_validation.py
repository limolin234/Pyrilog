import math
import shutil
import tempfile
import unittest
from pathlib import Path

from examples.electrical_validation import _finite_error, run_validation


NGSPICE = shutil.which("ngspice") or "/home/limolin/Myapps/ngspice/bin/ngspice"
XYCE = shutil.which("Xyce") or "/home/limolin/Myapps/xyce/bin/Xyce"


@unittest.skipUnless(
    Path(NGSPICE).is_file() and Path(XYCE).is_file(),
    "ngspice and Xyce are required for cross-simulator validation",
)
class ElectricalValidationTests(unittest.TestCase):
    def test_non_finite_backend_value_cannot_pass_comparison(self):
        for left, right in ((math.nan, 0.0), (0.0, math.nan), (math.inf, 0.0)):
            with self.subTest(left=left, right=right):
                with self.assertRaisesRegex(AssertionError, "non-finite"):
                    _finite_error(left, right, "probe")

    def test_native_benchmark_ladder_matches_ngspice_and_xyce(self):
        with tempfile.TemporaryDirectory() as directory:
            run_validation(Path(directory))


if __name__ == "__main__":
    unittest.main()
