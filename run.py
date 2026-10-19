"""Portable entry point; works from a source ZIP without Git or installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from bbp_demo.__main__ import main

if __name__ == "__main__":
    main()
