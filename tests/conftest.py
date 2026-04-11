"""
Pytest configuration for BOTTEU test suite.

Sets up the Python path so both `app` and `collector` packages are importable
without installing the project.
"""
import sys
import os

# Project root (one level up from tests/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
