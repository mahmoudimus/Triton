#!/usr/bin/env python3
"""Prove a packaged static Triton is actually consumable from somewhere else.

Builds a throwaway CMake project against the package via find_package(triton) in a
directory unrelated to the build tree, then runs it. This catches the failure mode
where tritonConfig.cmake still points at absolute paths that only existed on the
machine that built it.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAIN_CPP = r"""
#include <triton/context.hpp>
#include <iostream>

int main() {
  triton::Context ctx(triton::arch::ARCH_X86_64);
  ctx.symbolizeRegister(ctx.registers.x86_rax, "my_rax");
  triton::arch::Instruction i1("\x48\x35\x34\x12\x00\x00", 6);  // xor rax, 0x1234
  triton::arch::Instruction i2("\x48\x89\xc1", 3);              // mov rcx, rax
  ctx.processing(i1);
  ctx.processing(i2);
  auto rcx = ctx.getSymbolicRegister(ctx.registers.x86_rcx);
  auto ast = ctx.getAstContext();
  auto model = ctx.getModel(ast->equal(rcx->getAst(), ast->bv(0xdead, 64)));
  if (model.empty()) {
    std::cerr << "no model produced" << std::endl;
    return 1;
  }
  for (const auto& kv : model)
    std::cout << "model: " << kv.second << std::endl;
  std::cout << "smoke OK" << std::endl;
  return 0;
}
"""

CMAKELISTS = """cmake_minimum_required(VERSION 3.20)
project(triton_smoke C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(triton REQUIRED)
add_executable(smoke main.cpp)
target_link_libraries(smoke PRIVATE triton::triton)
"""


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True, type=Path, help="installed Triton package prefix")
    ap.add_argument("--workdir", required=True, type=Path, help="scratch dir for the test project")
    ap.add_argument("--llvm", default="", help="LLVM prefix (if Triton was built with LLVM_INTERFACE)")
    args = ap.parse_args()

    pkg = args.package.resolve()
    work = args.workdir.resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "src").mkdir(parents=True)
    (work / "src" / "main.cpp").write_text(MAIN_CPP)
    (work / "src" / "CMakeLists.txt").write_text(CMAKELISTS)

    prefix_path = [str(pkg)]
    if args.llvm:
        prefix_path.append(args.llvm)

    run(["cmake", "-S", str(work / "src"), "-B", str(work / "build"), "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_PREFIX_PATH=" + ";".join(prefix_path)])
    run(["cmake", "--build", str(work / "build"), "--config", "Release"])

    exe = work / "build" / ("smoke.exe" if os.name == "nt" else "smoke")
    if not exe.exists():
        found = list((work / "build").rglob("smoke*"))
        exe = next((f for f in found if f.is_file() and os.access(f, os.X_OK)), None)
        if exe is None:
            print("error: smoke binary not found", file=sys.stderr)
            return 1

    # Windows resolves DLLs from the executable's directory; z3 is still dynamic.
    if os.name == "nt":
        for var in ("Z3_BIN_DIR", "CAPSTONE_BIN_DIR"):
            d = os.environ.get(var)
            if d and Path(d).is_dir():
                for dll in Path(d).glob("*.dll"):
                    shutil.copy2(dll, exe.parent)

    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    print(out.stdout, end="")
    if "smoke OK" not in out.stdout:
        print("error: smoke binary did not report success", file=sys.stderr)
        return 1
    print("package is relocatable and links correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
