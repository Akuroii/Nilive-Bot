"""Legacy launcher for the maintained script-level claim regression.

The authoritative test lives at ``scripts/test_slice2_level_claims.py``.
Run ``python test_slice2_level_claims.py`` only for backward compatibility.
"""
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    runpy.run_path(str(scripts_dir / "test_slice2_level_claims.py"),
                   run_name="__main__")
