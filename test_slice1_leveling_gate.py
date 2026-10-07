"""Legacy launcher for the maintained script-level Leveling regression.

The authoritative test lives at ``scripts/test_slice1_leveling_gate.py``.
Run ``python test_slice1_leveling_gate.py`` only for backward compatibility.
"""
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    runpy.run_path(str(scripts_dir / "test_slice1_leveling_gate.py"),
                   run_name="__main__")
