"""Load an explicitly configured host Z3 before importing ``_triton``."""

import builtins
import ctypes
from pathlib import Path
import sys


def platform_library_name():
    if sys.platform == "darwin":
        return "libz3.dylib"
    if sys.platform == "win32":
        return "libz3.dll"
    return "libz3.so"


def rtld_global_mode():
    if sys.platform == "win32":
        return None
    return ctypes.RTLD_GLOBAL


def _configured_candidates(directories):
    candidates = []
    for directory in directories:
        path = Path(directory)
        if not path.is_absolute():
            raise ImportError("Triton Z3_LIB_DIRS entries must be absolute: " + str(path))
        candidates.append(path / platform_library_name())
    return candidates


def preload_configured_z3():
    directories = getattr(builtins, "Z3_LIB_DIRS", None)
    if directories is None:
        return
    if isinstance(directories, (str, bytes)):
        raise ImportError("Triton Z3_LIB_DIRS must be a sequence of directories")

    candidates = _configured_candidates(directories)
    library = next((path for path in candidates if path.is_file()), None)
    if library is None:
        raise ImportError("Triton could not find Z3; tried: " + ", ".join(map(str, candidates)))

    kwargs = {}
    mode = rtld_global_mode()
    if mode is not None:
        kwargs["mode"] = mode
    try:
        ctypes.CDLL(str(library), **kwargs)
    except OSError as error:
        raise ImportError(f"Triton could not load configured Z3 {library}: {error}") from error
