"""Negative tests for the paper package's static guard. No OpenD needed.

These live outside ``sidecar/paper/`` on purpose: they must contain the literal
forbidden symbols, and any file inside that package is scanned for exactly
those. Keeping them here lets them be written plainly instead of obfuscated
into forms the guard would then miss.

Each case builds a throwaway package containing a copy of guard.py plus one
offending module, and asserts the import-time check refuses it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUARD = os.path.join(_SIDECAR, "paper", "guard.py")
_PY = os.path.join(_SIDECAR, ".venv", "bin", "python")

_RUNNER = """
import paper
try:
    paper.assert_simulated()
except paper.SimulationViolation as e:
    print("RAISED:", e)
    raise SystemExit(0)
print("NOT RAISED")
raise SystemExit(1)
"""


def _run_with(filename: str, source: str) -> tuple[int, str]:
    root = tempfile.mkdtemp()
    try:
        pkg = os.path.join(root, "paper")
        os.makedirs(pkg)
        shutil.copy(_GUARD, os.path.join(pkg, "guard.py"))
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write("from .guard import assert_simulated, SimulationViolation\n")
        with open(os.path.join(pkg, filename), "w") as fh:
            fh.write(source)
        env = dict(os.environ, PYTHONPATH=_SIDECAR)
        proc = subprocess.run(
            [_PY, "-c", _RUNNER], cwd=root, env=env,
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout + proc.stderr
    finally:
        shutil.rmtree(root, ignore_errors=True)


class GuardFailsClosed(unittest.TestCase):
    def _refused(self, filename, source, expect):
        code, out = _run_with(filename, source)
        self.assertEqual(code, 0, f"guard did not raise for {expect}: {out}")
        self.assertIn("RAISED", out)
        self.assertIn(expect, out)

    def test_unlock_trade_as_bare_string(self):
        self._refused("engine.py", 'X = "unlock_trade"\n', "unlock_trade")

    def test_unlock_trade_as_attribute(self):
        self._refused("engine.py", "def go(ctx):\n    return ctx.unlock_trade()\n",
                      "unlock_trade")

    def test_real_trading_env_attribute(self):
        self._refused("engine.py", "import moomoo as mm\nE = mm.TrdEnv.REAL\n", "REAL")

    def test_real_as_bare_string(self):
        self._refused("engine.py", 'ENV = "REAL"\n', "REAL")

    def test_order_method_outside_broker(self):
        self._refused(
            "engine.py",
            'def go(ctx):\n    return ctx.place_order(1, 1, "US.NVDA", "BUY")\n',
            "place_order",
        )

    def test_getattr_indirection(self):
        """The form moomoo_client's substring guard cannot see."""
        self._refused(
            "engine.py",
            'def go(ctx):\n    f = getattr(ctx, "place_order")\n    return f()\n',
            "place_order",
        )

    def test_combo_order_covered(self):
        """Omitted from moomoo_client._FORBIDDEN; covered here."""
        self._refused("engine.py", "def go(ctx):\n    return ctx.place_combo_order()\n",
                      "place_combo_order")

    def test_change_order_covered(self):
        self._refused("engine.py", "def go(ctx):\n    return ctx.change_order()\n",
                      "change_order")

    def test_cancel_all_order_covered(self):
        self._refused("engine.py", "def go(ctx):\n    return ctx.cancel_all_order()\n",
                      "cancel_all_order")


class GuardAllowsBroker(unittest.TestCase):
    def test_order_method_permitted_in_broker(self):
        code, out = _run_with(
            "broker.py",
            'def go(ctx):\n    return ctx.place_order(1, 1, "US.NVDA", "BUY")\n',
        )
        self.assertEqual(code, 1, f"expected no violation, got: {out}")
        self.assertIn("NOT RAISED", out)


class RealPackageIsClean(unittest.TestCase):
    def test_shipped_package_passes_its_own_guard(self):
        import paper
        paper.assert_simulated()
        paper.assert_read_only_modules()

    def test_original_read_only_guard_still_passes(self):
        """moomoo_client.py must remain importable and unmodified in behaviour."""
        proc = subprocess.run(
            [_PY, "-c", "import moomoo_client; print('ok')"],
            cwd=_SIDECAR, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
