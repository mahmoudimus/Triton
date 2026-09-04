#!/usr/bin/env python3
# coding: utf-8
"""Tester for examples."""
import glob
import itertools
import os
import platform
import subprocess
import sys
import unittest

EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "python")

#: Per-example wall-clock cap. Several CTF write-ups are legitimately slow --
#: google2016-unbreakable, hackcon-2016-angry-reverser and
#: NorthSec-2018-MarsAnalytica each take over a minute on a fast machine and
#: several times that on a CI runner -- so the default is generous. The point is
#: to turn an indefinite hang into a named failure, not to police runtime.
EXAMPLE_TIMEOUT = int(os.environ.get("TRITON_EXAMPLE_TIMEOUT", "900"))

ARGS = {
    "small_x86-64_symbolic_emulator.py":                [os.path.join(EXAMPLE_DIR, "samples", "sample_1"), "hello"],
}


class TestExample(unittest.TestCase):
    """Holder to run examples as tests."""

for i, example in enumerate(itertools.chain(glob.iglob(os.path.join(EXAMPLE_DIR, "*.py")),
                                            glob.iglob(os.path.join(EXAMPLE_DIR, "*", "*.py")),
                                            glob.iglob(os.path.join(EXAMPLE_DIR, "*", "*", "*.py")),
                                            glob.iglob(os.path.join(EXAMPLE_DIR, "*", "*", "*", "*.py")))):

    def _test_example(self, example_name=example):
        """Run example and show stdout in case of fail."""
        args = [v for k, v in list(ARGS.items()) if k in example_name]
        assert len(args) <= 1
        if len(args) == 1:
            args = args[0]

        if ('TRAVIS' in os.environ or 'APPVEYOR' in os.environ) and example_name.find('hackcon-2016-angry-reverser') >= 0:
            # FIXME: Doesn't work on Travis and Appveyor...
            return

        p = subprocess.Popen([sys.executable, example_name] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=EXAMPLE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Without a timeout a single wedged example hangs the whole suite
            # with no output, which is indistinguishable from CI being slow.
            # Fail loudly and name the example instead.
            p.kill()
            out, err = p.communicate()
            self.fail(
                f"{os.path.basename(example_name)} exceeded {EXAMPLE_TIMEOUT}s and was killed. "
                f"Set TRITON_EXAMPLE_TIMEOUT to raise the limit.\n"
                + "\n".join((str(out), str(err)))
            )
        self.assertEqual(p.returncode, 0, "\n".join((str(out), str(err), str(p.returncode))))

    # Define an arguments with a default value as default value is capture at
    # lambda creation so that example_name is not in the closure of the lambda
    # function.
    setattr(TestExample, "test_" + str(i) + "_" + os.path.basename(example), _test_example)
