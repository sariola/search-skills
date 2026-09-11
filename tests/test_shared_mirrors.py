"""Shared-plumbing mirror invariant (hard architectural constraint).

Each package ships as a **self-contained wheel** (``packages = ["src/<name>"]``),
and the SKILL.md runs it via ``uv run --with /abs/path``. There is NO third
shared distribution — the dual sync/async wrapper lives in a byte-identical
``_async.py`` mirrored into *both* packages. If the two copies drift apart, the
two skills silently diverge in behaviour. These tests pin that mirror.
"""
import hashlib
import os
import unittest

import brave
import exa

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXA_ASYNC = os.path.join(_ROOT, "exa", "src", "exa", "_async.py")
_BRAVE_ASYNC = os.path.join(_ROOT, "brave", "src", "brave", "_async.py")

# The exact set of names both modules must import from their own _async mirror.
_MIRROR_NAMES = [
    "_identity_await", "_wrap_result", "_make_async", "_apply_async_to",
    "_AsyncDict", "_AsyncStr", "_AsyncList", "_AsyncInt", "_AsyncFloat",
    "_AsyncBool", "_AsyncScalar",
]


class MirrorFileInvariant(unittest.TestCase):
    def test_both_mirror_files_exist(self):
        self.assertTrue(os.path.isfile(_EXA_ASYNC), _EXA_ASYNC)
        self.assertTrue(os.path.isfile(_BRAVE_ASYNC), _BRAVE_ASYNC)

    def test_mirror_files_are_byte_identical(self):
        with open(_EXA_ASYNC, "rb") as f:
            a = f.read()
        with open(_BRAVE_ASYNC, "rb") as f:
            b = f.read()
        self.assertEqual(hashlib.sha256(a).hexdigest(),
                         hashlib.sha256(b).hexdigest(),
                         "exa/_async.py and brave/_async.py have diverged")
        self.assertEqual(a, b, "mirror copies are not byte-identical")


class MirrorImportInvariant(unittest.TestCase):
    def test_exa_imports_all_mirror_names(self):
        for name in _MIRROR_NAMES:
            self.assertTrue(hasattr(exa, name), f"exa missing {name}")

    def test_brave_imports_all_mirror_names(self):
        for name in _MIRROR_NAMES:
            self.assertTrue(hasattr(brave, name), f"brave missing {name}")

    def test_mirrored_helpers_are_shared_objects(self):
        """The classes/functions the two modules expose from the mirror must be
        behavioural twins: same object type, and both coming from a module
        named ``_async`` (the mirrored plumbing lives in its own module, not
        inline in ``__init__``)."""
        for name in _MIRROR_NAMES:
            ea = getattr(exa, name)
            ba = getattr(brave, name)
            self.assertEqual(type(ea).__name__, type(ba).__name__)
            self.assertEqual(getattr(ea, "__module__", "").split(".")[-1],
                             "_async", f"{name} not defined in a _async module")
            self.assertEqual(getattr(ba, "__module__", "").split(".")[-1],
                             "_async", f"{name} not defined in a _async module")


class ApplyAsyncTailInvariant(unittest.TestCase):
    def test_apply_async_to_present_in_module_source(self):
        """Both modules must apply the async wrapper, scoped to the public API
        (``__all``), at load; without it the dual sync/async contract silently
        disappears, and without the ``__all__`` scope imported helpers (e.g.
        ``parsedate_to_datetime``) would be re-exported wrapped."""
        for mod, path in ((exa, os.path.join(_ROOT, "exa", "src", "exa",
                                            "__init__.py")),
                          (brave, os.path.join(_ROOT, "brave", "src", "brave",
                                              "__init__.py"))):
            with open(path, "r") as f:
                src = f.read()
            self.assertIn("_apply_async_to(globals(), public=__all__)", src,
                          f"{mod.__name__} never applies the async wrapper "
                          f"scoped to __all__")

    def test_imported_helpers_not_wrapped(self):
        """The stdlib helper imported at module top must keep its original,
        non-wrapped identity — a regression means the wrapper leaked onto
        names outside ``__all__``."""
        import email.utils
        self.assertIs(exa.parsedate_to_datetime,
                      email.utils.parsedate_to_datetime)
        self.assertIs(brave.parsedate_to_datetime,
                      email.utils.parsedate_to_datetime)


if __name__ == "__main__":
    unittest.main()
