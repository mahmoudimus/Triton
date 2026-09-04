import os as _os
import sys as _sys

# Windows resolves a .pyd's dependent DLLs from a small, explicit set of
# directories -- the .pyd's own directory is NOT among them for extension
# modules. Add it so libz3.dll / capstone.dll sitting beside _triton.pyd are
# found. Wheels repaired by delvewheel vendor their own copies and patch the
# .pyd, so this is a no-op there.
if _sys.platform == "win32" and hasattr(_os, "add_dll_directory"):
    try:
        _os.add_dll_directory(_os.path.dirname(_os.path.abspath(__file__)))
    except OSError:
        pass

from ._z3_loader import preload_configured_z3


preload_configured_z3()

from ._triton import *
