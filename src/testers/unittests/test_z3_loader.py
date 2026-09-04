import builtins
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
LOADER_PATH = ROOT / "src" / "libtriton" / "bindings" / "python" / "package" / "_z3_loader.py"


def load_loader():
    spec = importlib.util.spec_from_file_location("triton_z3_loader_test", LOADER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConfiguredZ3Preload(unittest.TestCase):
    def setUp(self):
        self.loader = load_loader()
        self.old_dirs = getattr(builtins, "Z3_LIB_DIRS", None)
        self.had_dirs = hasattr(builtins, "Z3_LIB_DIRS")

    def tearDown(self):
        if self.had_dirs:
            builtins.Z3_LIB_DIRS = self.old_dirs
        elif hasattr(builtins, "Z3_LIB_DIRS"):
            del builtins.Z3_LIB_DIRS

    def test_unset_configuration_does_not_load_a_library(self):
        if hasattr(builtins, "Z3_LIB_DIRS"):
            del builtins.Z3_LIB_DIRS

        with mock.patch.object(self.loader.ctypes, "CDLL") as cdll:
            self.loader.preload_configured_z3()

        cdll.assert_not_called()

    def test_first_existing_absolute_candidate_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first.mkdir()
            second.mkdir()
            candidate = second / self.loader.platform_library_name()
            candidate.touch()
            builtins.Z3_LIB_DIRS = [str(first), str(second)]

            with mock.patch.object(self.loader.ctypes, "CDLL") as cdll:
                self.loader.preload_configured_z3()

        self.assertEqual(cdll.call_args.args, (str(candidate),))
        if self.loader.rtld_global_mode() is None:
            self.assertEqual(cdll.call_args.kwargs, {})
        else:
            self.assertEqual(cdll.call_args.kwargs, {"mode": self.loader.rtld_global_mode()})

    def test_relative_directory_is_rejected(self):
        builtins.Z3_LIB_DIRS = ["relative/z3"]

        with self.assertRaisesRegex(ImportError, "absolute"):
            self.loader.preload_configured_z3()

    def test_missing_libraries_list_every_attempted_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first.mkdir()
            second.mkdir()
            builtins.Z3_LIB_DIRS = [str(first), str(second)]

            with self.assertRaisesRegex(ImportError, str(first / self.loader.platform_library_name())) as error:
                self.loader.preload_configured_z3()

        self.assertIn(str(second / self.loader.platform_library_name()), str(error.exception))

    def test_dynamic_loader_error_identifies_selected_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / self.loader.platform_library_name()
            candidate.touch()
            builtins.Z3_LIB_DIRS = [tmp]

            with mock.patch.object(self.loader.ctypes, "CDLL", side_effect=OSError("bad ABI")):
                with self.assertRaisesRegex(ImportError, str(candidate)):
                    self.loader.preload_configured_z3()


if __name__ == "__main__":
    unittest.main()
