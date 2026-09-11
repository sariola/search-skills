"""Test package bootstrap.

Making the skill source packages importable regardless of how the suite is
launched (``python -m unittest discover``, a bare ``python tests/...``, or an
IDE runner). Each skill is a self-contained wheel rooted at ``<skill>/src/<pkg>``
so we put both src roots on ``sys.path``. We insert at the front so the regular
packages (with ``__init__.py``) shadow any same-named namespace dirs.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

for _pkg_dir in ("exa/src", "brave/src"):
    _p = os.path.join(_ROOT, _pkg_dir)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
