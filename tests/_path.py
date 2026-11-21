"""Shared test setup: put src/ on the path. No hardware, no network, no root needed."""
import os
import sys
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
