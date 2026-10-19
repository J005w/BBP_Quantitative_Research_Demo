"""Run every selected test, including real local HTTP tests, without Git."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    raise SystemExit("Unexpected skips; investigate before release")
raise SystemExit(not result.wasSuccessful())
