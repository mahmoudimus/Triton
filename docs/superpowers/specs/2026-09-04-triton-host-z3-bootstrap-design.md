# Triton Host-Z3 Python Bootstrap Design

## Goal

Allow Python callers to direct Triton's existing host-Z3 extension to a specific
Z3 library by setting `builtins.Z3_LIB_DIRS` before `import triton`, matching the
configuration convention already used by `z3.z3core` and d810 speedups.

## Scope

This is the host-Z3 slice only. It does not add a bundled/private Z3 profile,
rename Z3 symbols, add a C++ `dlopen`/`dlsym` adapter, or change the no-Z3 and
Bitwuzla build paths.

## Architecture

The installed `triton` import becomes a Python package. Its `__init__.py` reads
`builtins.Z3_LIB_DIRS` before loading the native extension. When configured, it
searches those directories in order for the platform's Z3 shared-library name,
loads the first matching absolute path with `ctypes.CDLL(..., RTLD_GLOBAL)`, then
imports the host-Z3 extension. The extension retains its bare `libz3.dylib`
dependency, allowing dyld to bind it to the preloaded image.

The native extension is renamed `_triton` and exports `PyInit__triton`; the
Python package re-exports its public attributes so `import triton` remains the
public API. If `Z3_LIB_DIRS` is unset, the bootstrap imports `_triton` directly,
preserving the current IDA behavior where IDA already loaded Z3. If configured
directories contain no usable library, import raises an actionable `ImportError`
listing the attempted paths. Loader errors from `ctypes` are surfaced as an
actionable `ImportError` that identifies the selected library.

## Platform rules

The bootstrap searches `libz3.dylib` on macOS, `libz3.so` on Linux, and
`libz3.dll` on Windows. It uses `RTLD_GLOBAL` only where the platform exposes
it; Windows uses the normal `ctypes.CDLL` loader behavior. Paths must be absolute
after normalization; relative entries are rejected rather than interpreted
against IDA's current directory.

## Compatibility rules

`Z3_LIB_DIRS` is a host-selection input, not a compatibility promise. The
bootstrap does not accept or infer a broad version range. It only guarantees
that the configured library is selected before the extension loads; callers
remain responsible for supplying a Z3 ABI compatible with the Triton build.

## Verification

Unit tests cover the selection helper without importing the native extension:
unset configuration is a no-op; ordered absolute directories select the first
existing platform library; relative directories are rejected; missing libraries
report every attempted candidate; and the preload uses the chosen absolute path
and `RTLD_GLOBAL` where available. A macOS integration test builds the host-Z3
extension, configures `Z3_LIB_DIRS`, imports `triton`, and solves a constrained
model. The existing IDA console probe remains the final runtime gate.
