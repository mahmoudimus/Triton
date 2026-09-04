##
##  Copyright (C) - Triton
##
##  This program is under the terms of the Apache License 2.0.
##

import os
import platform
import re
import subprocess
import sys

from distutils.file_util import copy_file
from distutils.version import LooseVersion
from setuptools import Extension
from setuptools import setup
from setuptools.command.build_ext import build_ext


VERSION_MAJOR = 1
VERSION_MINOR = 0
VERSION_PATCH = 0
ON = "ON"
OFF = "OFF"

clang = "clang"
clang_cl = "clang_cl"
vs2022 = "vs2022"
Release = "Release"
Debug = "Debug"

RELEASE_CANDIDATE = 4
Z3_INTERFACE = ON
LLVM_INTERFACE = OFF
BITWUZLA_INTERFACE = OFF
BOOST_INTERFACE = OFF

BUILDTOOLS = vs2022 # clang or clang_cl or vs2022
CMAKE_BUILD_TYPE = Release # Release or Debug

# NOTE: the parentheses matter. Without them `a + b if c else d` binds as
# `(a + b) if c else d`, so VERSION collapsed to '' whenever RELEASE_CANDIDATE was 0.
VERSION = f'{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}' + \
            (f'rc{RELEASE_CANDIDATE}' if RELEASE_CANDIDATE else '')

# Optional PEP 440 local version segment, so variants of the same source (for
# example a build without the LLVM lifting interface) produce distinct wheel
# filenames and can sit side by side on a release.
_LOCAL_VERSION = os.getenv('TRITON_WHEEL_LOCAL_VERSION', '').strip()
if _LOCAL_VERSION:
    VERSION += '+' + re.sub(r'[^A-Za-z0-9.]', '.', _LOCAL_VERSION)

def is_cmake_true(value):
    """Check if CMake would parse the value as True or False. Might not be completely accurate.
    Based on https://cmake.org/cmake/help/latest/command/if.html#basic-expressions"""
    if(value in ['ON', 'YES', 'TRUE', 'Y']):
        return True
    try:
        float(value)
        if(int(value) == 0):
            return False
        return True
    except:
        return False

class CMakeExtension(Extension):
    def __init__(self, name, sourcedir=''):
        Extension.__init__(self, name, sources=[])
        self.sourcedir = os.path.abspath(sourcedir)


class CMakeBuild(build_ext):

    def run(self):
        ext = self.extensions[0]
        self.build_extension(ext)
        self.copy_extension_to_source(ext)
        self.copy_autocomplete()


    def build_extension(self, ext):
        # Set platform-agnostric arguments.
        cmake_args = [
            # General arguments.
            '-DPYTHON_EXECUTABLE=' + sys.executable,
            '-DCMAKE_BUILD_TYPE='+ CMAKE_BUILD_TYPE,
        ]

        # Interfaces can be defined using environment variables.
        # Interfaces by default:
        #
        #   Z3_INTERFACE=On
        #   LLVM_INTERFACE=Off
        #   BITWUZLA_INTERFACE=Off
        #   BOOST_INTERFACE=Off
        #
        for arg, value in [('Z3_INTERFACE', Z3_INTERFACE), ('LLVM_INTERFACE', LLVM_INTERFACE), ('BITWUZLA_INTERFACE', BITWUZLA_INTERFACE), ('BOOST_INTERFACE', BOOST_INTERFACE)]:
            if os.getenv(arg):
                cmake_args += [f'-D{arg}=' + os.getenv(arg)]
            else:
                cmake_args += [f'-D{arg}={value}']

        build_args = []

        env = os.environ.copy()
        env['CXXFLAGS'] = '{} -DVERSION_INFO=\\"{}\\"'.format(env.get('CXXFLAGS', ''), self.distribution.get_version())

        # Set platform-specific arguments.
        if platform.system() == "Linux":
            build_args += ['--', '-j4']

        elif platform.system() == "Darwin":
            build_args += ['--', '-j4']

        elif platform.system() == "Windows":
            if BUILDTOOLS == vs2022:
                cmake_args += []
                build_args += ["--", "/m:4"]
            else:
                assert os.getenv('COMPILER_DIR'), "COMPILER_DIR(clang or clang_cl) env not found"
                if BUILDTOOLS == clang:
                    cmake_args += [
                        "-DCMAKE_C_COMPILER=" +  os.getenv('COMPILER_DIR') + "/clang.exe",
                        "-DCMAKE_CXX_COMPILER=" +  os.getenv('COMPILER_DIR') + "/clang++.exe",
                        "-DCMAKE_RC_COMPILER=" +  os.getenv('COMPILER_DIR') + "/llvm-rc.exe",
                        "-G Ninja",
                    ]
                if BUILDTOOLS == clang_cl:
                    cmake_args += [
                        "-DCMAKE_C_COMPILER= " +  os.getenv('COMPILER_DIR') + "/clang-cl.exe",
                        "-DCMAKE_CXX_COMPILER=" +  os.getenv('COMPILER_DIR') + "/clang-cl.exe",
                        "-DCMAKE_RC_COMPILER=" +  os.getenv('COMPILER_DIR') + "/llvm-rc.exe",
                        "-G Ninja",
                    ]
        else:
            raise Exception(f'Platform not supported: {platform.system()}')

        # Custom Python paths.
        # A wheel is redistributable by definition; on macOS that means the
        # extension must not bake in this machine's libpython path.
        cmake_args += ['-DPYTHON_EXTENSION_ONLY=ON']

        if os.getenv('PYTHON_LIBRARIES'):
            cmake_args += ['-DPYTHON_LIBRARIES=' + os.getenv('PYTHON_LIBRARIES')]

        if os.getenv('PYTHON_INCLUDE_DIRS'):
            cmake_args += ['-DPYTHON_INCLUDE_DIRS=' + os.getenv('PYTHON_INCLUDE_DIRS')]

        if os.getenv('PYTHON_LIBRARY'):
            cmake_args += ['-DPYTHON_LIBRARY=' + os.getenv('PYTHON_LIBRARY')]

        if os.getenv('PYTHON_VERSION'):
            cmake_args += ['-DPYTHON_VERSION=' + os.getenv('PYTHON_VERSION')]

        # Custom Z3 paths.
        if os.getenv('Z3_LIBRARIES'):
            cmake_args += ['-DZ3_LIBRARIES=' + os.getenv('Z3_LIBRARIES')]

        if os.getenv('Z3_INCLUDE_DIRS'):
            cmake_args += ['-DZ3_INCLUDE_DIRS=' + os.getenv('Z3_INCLUDE_DIRS')]

        # Custom Bitwuzla paths.
        if os.getenv('BITWUZLA_LIBRARIES'):
            cmake_args += ['-DBITWUZLA_LIBRARIES=' + os.getenv('BITWUZLA_LIBRARIES')]

        if os.getenv('BITWUZLA_INCLUDE_DIRS'):
            cmake_args += ['-DBITWUZLA_INCLUDE_DIRS=' + os.getenv('BITWUZLA_INCLUDE_DIRS')]
        # Custom Capstone paths.
        if os.getenv('CAPSTONE_LIBRARIES'):
            cmake_args += ['-DCAPSTONE_LIBRARIES=' + os.getenv('CAPSTONE_LIBRARIES')]

        if os.getenv('CAPSTONE_INCLUDE_DIRS'):
            cmake_args += ['-DCAPSTONE_INCLUDE_DIRS=' + os.getenv('CAPSTONE_INCLUDE_DIRS')]

        # Custom LLVM paths.
        if os.getenv('LLVM_LIBRARIES'):
            cmake_args += ['-DLLVM_LIBRARIES=' + os.getenv('LLVM_LIBRARIES')]

        if os.getenv('LLVM_INCLUDE_DIRS'):
            cmake_args += ['-DLLVM_INCLUDE_DIRS=' + os.getenv('LLVM_INCLUDE_DIRS')]

        # Custom CMake prefix path.
        if os.getenv('CMAKE_PREFIX_PATH'):
            cmake_args += ['-DCMAKE_PREFIX_PATH=' + os.getenv('CMAKE_PREFIX_PATH')]

        # Autocomplete stub generation. Enabled by default.
        python_autocomplete_value = os.getenv('PYTHON_BINDINGS_AUTOCOMPLETE', default='ON').upper()
        if python_autocomplete_value:
            cmake_args += ['-DPYTHON_BINDINGS_AUTOCOMPLETE=' + python_autocomplete_value]

        # Create temp and lib folders.
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)

        if not os.path.exists(self.build_lib):
            os.makedirs(self.build_lib)

        subprocess.check_call(['cmake', ext.sourcedir] + cmake_args, cwd=self.build_temp, env=env)
        subprocess.check_call(['cmake', '--build', '.', '--config', CMAKE_BUILD_TYPE, '--target', 'python-triton'] + build_args, cwd=self.build_temp)

        # The autocomplete file has to be built separately.
        if (is_cmake_true(python_autocomplete_value)):
            subprocess.check_call(['cmake', '--build', '.', '--config', CMAKE_BUILD_TYPE, '--target', 'python_autocomplete'], cwd=self.build_temp)

    def _cmake_package_dir(self):
        """Directory CMake wrote the `triton` package into.

        The `python-triton` target emits a package -- `_triton<suffix>` plus
        `__init__.py` and `_z3_loader.py` -- rather than a flat module, because
        the Z3 preload has to run before `_triton` is imported. Multi-config
        generators put it under the configuration directory.
        """
        base = os.path.join(self.build_temp, 'src', 'libtriton')
        if platform.system() == "Windows" and BUILDTOOLS == vs2022:
            base = os.path.join(base, CMAKE_BUILD_TYPE)
        return os.path.join(base, 'triton')

    def copy_extension_to_source(self, ext):
        if platform.system() not in ("Linux", "Darwin", "Windows"):
            raise Exception(f'Platform not supported: {platform.system()}')

        suffix = '.pyd' if platform.system() == "Windows" else '.so'
        pkg_dir = self._cmake_package_dir()
        src_filename = os.path.join(pkg_dir, '_triton' + suffix)
        if not os.path.isfile(src_filename):
            raise Exception(
                f'CMake did not produce {src_filename}. The python-triton target '
                f'emits a package directory; check src/libtriton/CMakeLists.txt.'
            )

        # get_ext_filename('triton._triton') -> 'triton/_triton.<abi>.<ext>'
        filename = self.get_ext_filename(self.get_ext_fullname(ext.name))
        dst_filename = os.path.join(self.build_lib, filename)
        os.makedirs(os.path.dirname(dst_filename), exist_ok=True)
        copy_file(src_filename, dst_filename, verbose=self.verbose)

        # __init__.py / _z3_loader.py also come from the build tree, so a stale
        # source copy can never diverge from what CMake actually installed.
        for pyfile in ('__init__.py', '_z3_loader.py'):
            src_py = os.path.join(pkg_dir, pyfile)
            if os.path.isfile(src_py):
                copy_file(src_py, os.path.join(self.build_lib, 'triton', pyfile),
                          verbose=self.verbose)

    def copy_autocomplete(self):
        src_filename = os.path.join(self.build_temp + '/doc/triton_autocomplete', 'triton.pyi')
        if(os.path.exists(src_filename)):
            # The stub describes the package's public surface, so it belongs at
            # triton/__init__.pyi now that `triton` is a package.
            dst_dir = os.path.join(self.build_lib, 'triton')
            os.makedirs(dst_dir, exist_ok=True)
            copy_file(src_filename, os.path.join(dst_dir, '__init__.pyi'),
                      verbose=self.verbose)

with open("README.md", "r") as f:
    long_description = f.read()


setup(
    name="triton-library",
    version=VERSION,
    author="The Triton's community",
    author_email="tritonlibrary@gmail.com",
    description="Triton is a dynamic binary analysis library",
    long_description=long_description,
    long_description_content_type="text/markdown",
    license = "Apache License Version 2.0",
    license_files = ('LICENSE.txt',),
    classifiers=[
        "Programming Language :: C++",
        "Programming Language :: Python :: 3",
        "Topic :: Security",
        "Topic :: Software Development :: Libraries",
    ],
    python_requires='>=3.6',
    project_urls={
        'Homepage': 'https://triton-library.github.io/',
        'Source': 'https://github.com/jonathansalwan/Triton',
    },
    ext_modules=[
        CMakeExtension('triton._triton', sourcedir='.')
    ],
    packages=['triton'],
    package_dir={'triton': 'src/libtriton/bindings/python/package'},
    package_data={'triton': ['*.pyi']},
    cmdclass=dict(build_ext=CMakeBuild),
    zip_safe=False,
    install_requires=[]
)
