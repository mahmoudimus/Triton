#!/usr/bin/env python3
"""Turn a CMake install tree of Triton into a relocatable, distributable package.

Triton's generated tritonConfig.cmake hardcodes absolute build-machine paths into
include_directories()/link_directories() -- on a CI runner those become
/home/runner/work/... and are useless to whoever downloads the artifact.
tritonTargets.cmake is already relocatable (it derives _IMPORT_PREFIX from
CMAKE_CURRENT_LIST_FILE), so only the Config file has to be rewritten.

This script:
  1. optionally copies bundled dependencies (capstone, z3) into the install tree,
  2. rewrites lib/cmake/triton/tritonConfig.cmake to resolve everything relative to
     the package root, and to put the dependency link line on the imported `triton`
     target so consumers can just target_link_libraries(app PRIVATE triton).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

LLVM_PREFIXES = ("LLVM", "LTO", "Remarks", "Polly")

# Triton's public headers include its dependencies' headers directly
# (z3Solver.hpp does #include <z3++.h>), so consumers need these include dirs too.
HEADER_PROBES = {
    "z3": "z3++.h",
    "capstone": "capstone/capstone.h",
    "bitwuzla": "bitwuzla/cpp/bitwuzla.h",
}

FLAG_NAMES = (
    "TRITON_BITWUZLA_INTERFACE",
    "TRITON_BOOST_INTERFACE",
    "TRITON_BUILD_SHARED_LIBS",
    "TRITON_LLVM_INTERFACE",
    "TRITON_MSVC_STATIC",
    "TRITON_PYTHON_BINDINGS",
    "TRITON_VERSION",
    "TRITON_Z3_INTERFACE",
)


def read_flags(config: Path) -> dict[str, str]:
    """Recover the build-time feature flags from the original tritonConfig.cmake."""
    text = config.read_text(encoding="utf-8", errors="replace")
    flags: dict[str, str] = {}
    for name in FLAG_NAMES:
        m = re.search(rf'^set\({name}\s+"?([^")\n]*)"?\)', text, re.M)
        if m:
            flags[name] = m.group(1).strip()
    return flags


def copy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


# Bundling from a system prefix would copy the entire system include tree.
UNSAFE_BUNDLE_ROOTS = {Path("/"), Path("/usr"), Path("/usr/include"), Path("C:/"), Path("C:/Windows")}


def bundle(dep_root: Path, prefix: Path, keep: tuple[str, ...]) -> list[str]:
    """Copy a dependency's headers and libraries into the package. Returns lib filenames."""
    inc = dep_root / "include"
    if inc.is_dir():
        copy_tree(inc, prefix / "include")

    copied: list[str] = []
    for libdir in ("lib", "lib64", "bin"):
        d = dep_root / libdir
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.name.lower().endswith(keep):
                target = prefix / "lib" / f.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                copied.append(f.name)
    return copied



LIB_RE = re.compile(r'(?:[A-Za-z]:)?[/\\][^;"\'\s()]*\.(?:a|so|dylib|lib)(?:\.[0-9.]+)?')


def lib_kind(filename: str) -> str:
    """"static" for .a/.lib, "shared" for .so/.dylib."""
    m = re.search(r"\.(a|lib|so|dylib)(?:\.[0-9.]+)?$", filename)
    if not m:
        return "unknown"
    return "static" if m.group(1) in ("a", "lib") else "shared"


def lib_key(filename: str) -> str:
    """capstone from libcapstone.a, z3 from libz3.so.4.16, capstone from capstone.lib.

    The kind is part of the key: swapping a dylib for a static archive changes the
    linkage model of a library that was never linked that way, which segfaulted at
    runtime on macOS when libz3.dylib was replaced by libz3.a.
    """
    m = re.match(r"^(?:lib)?(.+?)\.(?:a|so|dylib|lib)(?:\.[0-9.]+)?$", filename)
    name = (m.group(1) if m else filename).lower()
    return f"{name}:{lib_kind(filename)}"


def relocate_targets(prefix: Path, bundled: list[str]) -> tuple[list[str], list[str]]:
    """Rewrite absolute out-of-package library paths inside tritonTargets*.cmake.

    install(EXPORT) records the *build machine's* dependency paths in the imported
    target's link interface. They are meaningless to whoever downloads the package
    and they shadow the copies bundled beside it. Point them at the bundled files
    where possible, otherwise leave a bare library name for the consumer's linker.

    A match preceded by `}` belongs to `${_IMPORT_PREFIX}/lib/libtriton.a` and is
    already relocatable, so it must be left alone.
    """
    cmake_dir = prefix / "lib" / "cmake" / "triton"
    by_key = {lib_key(n): n for n in bundled}
    rewritten: list[str] = []
    fell_back: list[str] = []

    for f in sorted(cmake_dir.glob("tritonTargets*.cmake")):
        text = f.read_text(encoding="utf-8")

        def repl(m: "re.Match[str]", _text: str = text) -> str:
            path = m.group(0)
            if m.start() > 0 and _text[m.start() - 1] == "}":
                return path  # part of ${_IMPORT_PREFIX}/...
            if Path(path).as_posix().startswith(prefix.as_posix()):
                return path  # already inside the package
            key = lib_key(Path(path).name)
            if key in by_key:
                rewritten.append(f"{Path(path).name} -> {by_key[key]}")
                return "${_IMPORT_PREFIX}/lib/" + by_key[key]
            fell_back.append(Path(path).name)
            return key.split(":", 1)[0]

        new_text = LIB_RE.sub(repl, text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
    return rewritten, fell_back


def audit_prefix(prefix: Path) -> list[str]:
    """Any absolute build-machine path left in the CMake files is a bug.

    Must not fire on `${CMAKE_CURRENT_LIST_DIR}/tritonTargets.cmake`, so a match has
    to start right after a quote or whitespace (never after `}`) and carry at least
    two path segments.
    """
    abs_path = re.compile(r"""(?<=["'\s])((?:[A-Za-z]:)?/(?:[^;"'\s()]+/)+[^;"'\s()]+)""")
    system_ok = ("/usr/", "/lib/", "/lib64/", "/opt/_internal/", "/System/", "/Library/")
    bad: list[str] = []
    for f in sorted((prefix / "lib" / "cmake" / "triton").glob("*.cmake")):
        for m in abs_path.finditer(f.read_text(encoding="utf-8")):
            path = m.group(1)
            if Path(path).as_posix().startswith(prefix.as_posix()):
                continue
            if path.startswith(system_ok):
                continue
            bad.append(f"{f.name}: {path}")
    return bad


CONFIG_TEMPLATE = """\
# Relocatable Triton package config.
# Generated by .github/scripts/package_triton.py -- do not edit by hand.
#
# Usage:
#     find_package(triton REQUIRED)
#     target_link_libraries(my_target PRIVATE triton::triton)
#
# NOTE: if this package was built with LLVM_INTERFACE=ON, your project() call must
# enable C as well as CXX -- LLVMConfig.cmake runs check_include_file(), which is a
# C compile test and hard-errors in a CXX-only project.
#
# The unnamespaced name `triton` is also provided as an alias.
#
# Everything resolves relative to this file, so the package can be unpacked anywhere.

get_filename_component(TRITON_INSTALL_PREFIX "${{CMAKE_CURRENT_LIST_DIR}}/../../.." ABSOLUTE)

include("${{CMAKE_CURRENT_LIST_DIR}}/tritonTargets.cmake")

# tritonTargets.cmake exports the namespaced target; provide the short name too.
if(NOT TARGET triton)
    add_library(triton INTERFACE IMPORTED)
    target_link_libraries(triton INTERFACE triton::triton)
endif()

{flags}
set(TRITON_INCLUDE_DIRS "${{TRITON_INSTALL_PREFIX}}/include")
set(TRITON_LIBRARY_DIRS "${{TRITON_INSTALL_PREFIX}}/lib")

set(_triton_deps "")
set(_triton_incs "")

# --- Triton's own dependencies ------------------------------------------------
# A find_library() below that resolves inside this package is a bundled copy;
# anything else has to be present on the consuming machine.
{deps}

# --- dependencies that must be provided by the consuming project --------------
include(CMakeFindDependencyMacro)

if(TRITON_LLVM_INTERFACE)
    # Triton was built with LLVM lifting; the consumer needs the same LLVM.
    # LLVMConfig.cmake performs a C compile test, so the consuming project must
    # have enabled the C language: project(foo C CXX).
    get_property(_triton_langs GLOBAL PROPERTY ENABLED_LANGUAGES)
    if(NOT "C" IN_LIST _triton_langs)
        message(WARNING "triton: this package needs LLVM, whose CMake config requires "
                        "the C language. Add C to your project() call: project(foo C CXX)")
    endif()
    find_dependency(LLVM CONFIG)
    if(LLVM_LINK_LLVM_DYLIB)
        list(APPEND _triton_deps LLVM)
    else()
        list(APPEND _triton_deps ${{LLVM_AVAILABLE_LIBS}})
    endif()
    target_include_directories(triton::triton INTERFACE ${{LLVM_INCLUDE_DIRS}})

    # LLVM 19+ Linux prebuilts ship static archives containing thin-LTO bitcode
    # rather than ELF objects. GNU ld rejects them with
    #   "libLLVMPasses.a: error adding symbols: file format not recognized"
    # while lld reads them transparently. Triton's own build applies this to
    # itself; consumers of the static package need it too.
    if(CMAKE_SYSTEM_NAME MATCHES "Linux"
            AND NOT LLVM_LINK_LLVM_DYLIB
            AND LLVM_VERSION_MAJOR GREATER_EQUAL 19)
        set_property(TARGET triton::triton APPEND PROPERTY INTERFACE_LINK_OPTIONS
                     "-B${{LLVM_TOOLS_BINARY_DIR}}" "-fuse-ld=lld")
    endif()
endif()

if(TRITON_BOOST_INTERFACE)
    find_dependency(Boost)
endif()

set_property(TARGET triton::triton APPEND PROPERTY INTERFACE_LINK_LIBRARIES ${{_triton_deps}})
if(_triton_incs)
    list(REMOVE_DUPLICATES _triton_incs)
    set_property(TARGET triton::triton APPEND PROPERTY INTERFACE_INCLUDE_DIRECTORIES ${{_triton_incs}})
endif()
list(APPEND TRITON_INCLUDE_DIRS ${{_triton_incs}})

# Backwards-compatible variables (upstream tritonConfig.cmake exposed these).
set(TRITON_LIBRARIES triton::triton ${{_triton_deps}})
get_target_property(_triton_location triton::triton IMPORTED_LOCATION_RELEASE)
if(TRITON_BUILD_SHARED_LIBS)
    set(TRITON_LIBRARY "${{_triton_location}}")
else()
    set(TRITON_ARCHIVE "${{_triton_location}}")
endif()

message(STATUS "Found Triton ${{TRITON_VERSION}}: ${{TRITON_INSTALL_PREFIX}}")
"""


def read_dep_names(config: Path) -> list[str]:
    """Recover the non-LLVM dependency library names from the original TRITON_LIBRARIES."""
    text = config.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^set\(TRITON_LIBRARIES\s+"([^"]*)"\)', text, re.M)
    if not m:
        return []
    names = []
    for raw in m.group(1).split(";"):
        name = raw.strip()
        if not name or name == "triton":
            continue
        if any(name.startswith(p) for p in LLVM_PREFIXES):
            continue  # LLVM is resolved through find_dependency(LLVM CONFIG)
        if name.lower().startswith("python"):
            continue  # only the shared build links libpython, and it does so itself
        names.append(name)
    # de-duplicate, preserve order
    return list(dict.fromkeys(names))


def deps_block(dep_names: list[str], bundled: list[str]) -> str:
    """Emit find_library()/find_path() calls preferring bundled copies, then the system."""
    if not dep_names:
        return "# (no non-LLVM dependencies recorded)"
    bundled_stems = {re.sub(r"^lib", "", Path(b).stem).lower() for b in bundled}
    lines = []
    for name in dep_names:
        key = name.lower()
        var = "_triton_dep_" + re.sub(r"[^A-Za-z0-9]", "_", key)
        note = "  # bundled in this package" if key in bundled_stems else ""
        lines.append(
            f'find_library({var} NAMES "{name}" "lib{name}"{note}\n'
            f'    HINTS "${{TRITON_INSTALL_PREFIX}}/lib")\n'
            f'if({var})\n'
            f'    list(APPEND _triton_deps "${{{var}}}")\n'
            f'else()\n'
            f'    message(WARNING "triton: dependency \'{name}\' not found; '
            f'set CMAKE_PREFIX_PATH to point at it")\n'
            f'endif()'
        )
        probe = HEADER_PROBES.get(key)
        if probe:
            ivar = var + "_inc"
            lines.append(
                f'find_path({ivar} NAMES "{probe}"\n'
                f'    HINTS "${{TRITON_INSTALL_PREFIX}}/include")\n'
                f'if({ivar})\n'
                f'    list(APPEND _triton_incs "${{{ivar}}}")\n'
                f'else()\n'
                f'    message(WARNING "triton: header \'{probe}\' not found; '
                f'Triton public headers include it directly")\n'
                f'endif()'
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", required=True, type=Path,
                    help="CMake install prefix to make relocatable (modified in place)")
    ap.add_argument("--bundle-capstone", type=Path, default=None,
                    help="Capstone install root to copy into the package")
    ap.add_argument("--bundle-z3", type=Path, default=None,
                    help="Z3 install root to copy into the package")
    args = ap.parse_args()

    prefix: Path = args.prefix.resolve()
    config = prefix / "lib" / "cmake" / "triton" / "tritonConfig.cmake"
    if not config.is_file():
        print(f"error: {config} not found -- did `cmake --install` run?", file=sys.stderr)
        return 1

    flags = read_flags(config)
    dep_names = read_dep_names(config)
    # A static package must never claim Python bindings; they are forced off by
    # cmake_dependent_option when BUILD_SHARED_LIBS=OFF.
    if flags.get("TRITON_BUILD_SHARED_LIBS", "OFF").upper() in ("OFF", "0", "FALSE"):
        flags["TRITON_PYTHON_BINDINGS"] = "OFF"

    static_exts = (".a", ".lib")
    shared_exts = (".so", ".dylib", ".dll", ".lib")
    want_static = flags.get("TRITON_BUILD_SHARED_LIBS", "OFF").upper() in ("OFF", "0", "FALSE")
    exts = static_exts if want_static else shared_exts

    bundled: list[str] = []
    for root in (args.bundle_capstone, args.bundle_z3):
        if root is None:
            continue
        if not root.is_dir():
            print(f"warning: bundle source {root} does not exist, skipping", file=sys.stderr)
            continue
        if root.resolve() in UNSAFE_BUNDLE_ROOTS:
            print(f"warning: refusing to bundle from system prefix {root} "
                  f"(it would copy the whole include tree); skipping", file=sys.stderr)
            continue
        got = bundle(root.resolve(), prefix, exts)
        bundled.extend(got)
        print(f"bundled from {root}: {', '.join(got) if got else '(headers only)'}")

    # Anything already sitting in the package's lib/ counts as bundled, so a
    # re-run (or a package assembled by other means) still relocates correctly.
    in_package = [f.name for f in sorted((prefix / "lib").glob("*"))
                  if f.is_file() and f.suffix.lower() in (".a", ".lib", ".so", ".dylib")]
    rewritten, fell_back = relocate_targets(prefix, list(dict.fromkeys(bundled + in_package)))
    for line in rewritten:
        print(f"relocated target link path: {line}")
    for name in sorted(set(fell_back)):
        print(f"warning: {name} was not bundled; the imported target now asks the "
              f"consumer's linker for it by name", file=sys.stderr)

    flag_lines = "\n".join(f'set({k} {v})' for k, v in sorted(flags.items()))
    config.write_text(
        CONFIG_TEMPLATE.format(flags=flag_lines, deps=deps_block(dep_names, bundled)),
        encoding="utf-8",
    )
    print(f"rewrote {config} as relocatable")

    leftovers = audit_prefix(prefix)
    if leftovers:
        print("error: build-machine paths still referenced by the package:", file=sys.stderr)
        for line in leftovers:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("audit: no build-machine paths remain in the package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
