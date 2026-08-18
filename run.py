#!/usr/bin/env python3
"""Entry point for the H5 Viewer application.

Usage:
    python run.py [path/to/file.h5]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from h5tools_app.app import main

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
