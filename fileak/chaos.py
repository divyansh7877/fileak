"""ChaosMutator — dependency fault injection and mock rendering.

The ``ChaosMutator`` swaps a real npm dependency for a deliberately broken local
mock so the engine can observe how a target application behaves when one of its
dependencies misbehaves. This module currently provides the mock *rendering*
primitive used by injection: :func:`render_mock_module`.

A rendered mock is a self-contained local npm package living entirely inside its
destination folder (``dest/package.json`` + ``dest/index.js``). The
``package.json`` declares the package being mocked (``dest.name``) and points
``main`` at ``index.js``; the ``index.js`` is valid CommonJS whose runtime
behavior matches the requested :class:`~fileak.models.MockBehavior`:

* ``THROW_UNHANDLED`` — raises an unhandled exception (with a stack trace) on use.
* ``RETURN_EMPTY``    — returns an empty object for every call/member.
* ``HTTP_500``        — responds with HTTP 500 Internal Server Error.
* ``LEAK_DEBUG_STATE``— echoes raw internal/debug state (env, config, secrets).

Each generated module exports a ``Proxy`` over a function so that *both* direct
calls (``require('x')()``) and member calls (``require('x').verify()``) trigger
the misbehavior — real dependencies are used in many shapes, and the mock has to
trip regardless of how the target app consumes it.

Stdlib only: ``json``, ``pathlib``.

Formal specification (from design.md):

``render_mock_module(dest, behavior, template)``
    Preconditions: ``dest`` parent is writable; ``behavior`` is a valid enum;
    ``template`` is a known template id.
    Postconditions: ``dest/package.json`` and ``dest/index.js`` exist;
    ``index.js`` behavior matches ``behavior`` (throws / returns ``{}`` / serves
    500 / echoes debug state). No files outside ``dest`` are modified.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import ChaosProfile, MockBehavior, MutationRecord

#: Version stamped onto every generated mock ``package.json``. The unusual
#: pre-release suffix makes it obvious in logs/lockfiles that the package is a
#: fileak chaos mock and not a real dependency.
MOCK_PACKAGE_VERSION = "0.0.0-fileak-mock"


def render_mock_module(
    dest: Path,
    behavior: MockBehavior,
    template: str,
    custom_source: str | None = None,
) -> None:
    """Materialize a deliberately broken local npm package at ``dest``.

    Writes ``dest/package.json`` and ``dest/index.js`` (creating ``dest`` and any
    missing parents) such that the module's runtime behavior matches
    ``behavior``. The ``package.json`` uses ``dest.name`` as the package name so
    the mock can stand in for the real dependency being replaced.

    Args:
        dest: Destination folder for the mock package. ``dest.name`` is used as
            the npm package name. Created with ``parents=True, exist_ok=True``.
        behavior: The :class:`~fileak.models.MockBehavior` the generated module
            must exhibit (or, when ``custom_source`` is given, the label/category
            the custom mock falls under).
        template: A known template-id hint (e.g. ``"auth_verify_throws"``)
            recorded in the generated files for traceability. ``behavior`` drives
            the actual content unless ``custom_source`` overrides it.
        custom_source: Optional LLM-authored CommonJS ``index.js`` source. When
            provided, it is written verbatim as ``dest/index.js`` instead of a
            built-in behavior template (free-form planner mode). The caller MUST
            have already passed it through
            :func:`fileak.llm.mock_guard.validate_mock_source`; this function
            does not re-validate. A banner is prepended for traceability.

    Raises:
        ValueError: If ``behavior`` is not a :class:`MockBehavior`, ``template``
            is not a non-empty string, or ``custom_source`` is given but empty.

    Side effects:
        Creates/overwrites only ``dest/package.json`` and ``dest/index.js``. No
        files outside ``dest`` are modified.
    """
    if not isinstance(behavior, MockBehavior):
        raise ValueError(f"behavior must be a MockBehavior, got {behavior!r}")
    if not isinstance(template, str) or not template:
        raise ValueError(f"template must be a non-empty string, got {template!r}")
    if custom_source is not None and not custom_source.strip():
        raise ValueError("custom_source, when provided, must be non-empty")

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    package_name = dest.name
    (dest / "package.json").write_text(
        _render_package_json(package_name, behavior, template),
        encoding="utf-8",
    )
    if custom_source is not None:
        index_js = _header(behavior, template) + (
            "// SOURCE: LLM-authored mock (validated by fileak.llm.mock_guard).\n\n"
            + custom_source.rstrip()
            + "\n"
        )
    else:
        index_js = _render_index_js(behavior, template)
    (dest / "index.js").write_text(index_js, encoding="utf-8")


def _render_package_json(name: str, behavior: MockBehavior, template: str) -> str:
    """Render the mock package manifest as a formatted JSON string.

    The manifest is marked ``private`` and uses an ``UNLICENSED`` license so the
    mock can never be accidentally published, and carries a ``fileak`` metadata
    block recording the behavior and originating template id.
    """
    manifest = {
        "name": name,
        "version": MOCK_PACKAGE_VERSION,
        "description": (
            f"fileak chaos mock ({behavior.value}) for '{name}'. "
            "Deliberately broken local dependency — DO NOT PUBLISH."
        ),
        "main": "index.js",
        "private": True,
        "license": "UNLICENSED",
        "fileak": {
            "mock": True,
            "behavior": behavior.value,
            "template": template,
        },
    }
    # Trailing newline keeps the file POSIX-friendly and diff-clean.
    return json.dumps(manifest, indent=2) + "\n"


def _render_index_js(behavior: MockBehavior, template: str) -> str:
    """Render the CommonJS ``index.js`` body for ``behavior``."""
    renderer = _INDEX_RENDERERS.get(behavior)
    if renderer is None:  # Defensive: every MockBehavior must be handled.
        raise ValueError(f"no mock renderer for behavior {behavior!r}")
    return _header(behavior, template) + renderer()


def _header(behavior: MockBehavior, template: str) -> str:
    """Build the shared banner comment for a generated ``index.js``."""
    return (
        "'use strict';\n"
        f"// fileak chaos mock — behavior: {behavior.value}; template: {template}\n"
        "// Auto-generated by fileak ChaosMutator. DO NOT EDIT, COMMIT, OR PUBLISH.\n"
        "// Deliberately broken local dependency used for fault injection.\n\n"
    )


# --- Per-behavior CommonJS bodies -----------------------------------------
#
# Each body exports a Proxy wrapping a function so that direct invocation
# (`require('x')()`), construction (`new (require('x'))()`), and member
# invocation (`require('x').anyMethod()`) all route to the misbehavior. The
# `get` trap returns undefined for interop/promise/symbol probes so that
# `require()` itself succeeds and the failure surfaces at *use* time.


def _render_throw_unhandled() -> str:
    return '''\
// THROW_UNHANDLED: raise an unhandled exception whenever the module is used.

function boom() {
  throw new Error(
    'fileak chaos: simulated unhandled dependency failure (THROW_UNHANDLED)'
  );
}

const handler = {
  get: function (_target, prop) {
    if (prop === '__esModule') return true;
    if (prop === 'then') return undefined; // not a thenable
    if (typeof prop === 'symbol') return undefined;
    // Any member resolves to a function that throws when called.
    return boom;
  },
  apply: function () {
    return boom();
  },
  construct: function () {
    return boom();
  },
};

module.exports = new Proxy(boom, handler);
'''


def _render_return_empty() -> str:
    return '''\
// RETURN_EMPTY: return an empty object for every call and every member.

function empty() {
  return {};
}

const handler = {
  get: function (_target, prop) {
    if (prop === '__esModule') return true;
    if (prop === 'then') return undefined;
    if (typeof prop === 'symbol') return undefined;
    return empty;
  },
  apply: function () {
    return {};
  },
  construct: function () {
    return {};
  },
};

module.exports = new Proxy(empty, handler);
'''


def _render_http_500() -> str:
    return '''\
// HTTP_500: respond with 500 Internal Server Error in an HTTP context, and
// throw a 500-shaped error generically when used outside one.

function respond500(req, res, next) {
  // Express-style response.
  if (res && typeof res.status === 'function') {
    res.status(500);
    if (typeof res.json === 'function') {
      return res.json({ statusCode: 500, error: 'Internal Server Error' });
    }
    if (typeof res.send === 'function') {
      return res.send('Internal Server Error');
    }
    res.statusCode = 500;
    if (typeof res.end === 'function') return res.end('Internal Server Error');
    return undefined;
  }
  // Node http-style response.
  if (res && typeof res.writeHead === 'function') {
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    return res.end('Internal Server Error');
  }
  // No HTTP context: surface a 500-shaped failure.
  const err = new Error('fileak chaos: Internal Server Error (HTTP_500)');
  err.status = 500;
  err.statusCode = 500;
  throw err;
}

const handler = {
  get: function (_target, prop) {
    if (prop === '__esModule') return true;
    if (prop === 'then') return undefined;
    if (typeof prop === 'symbol') return undefined;
    if (prop === 'status' || prop === 'statusCode') return 500;
    return respond500;
  },
  apply: function (_target, _thisArg, args) {
    return respond500.apply(null, args);
  },
  construct: function () {
    return { statusCode: 500, error: 'Internal Server Error' };
  },
};

module.exports = new Proxy(respond500, handler);
'''


def _render_leak_debug_state() -> str:
    return '''\
// LEAK_DEBUG_STATE: echo raw internal/debug state — environment variables,
// internal config, connection strings, and secrets — for every call.

function debugState() {
  return {
    __fileak_chaos__: 'LEAK_DEBUG_STATE',
    env: Object.assign({}, process.env),
    internalConfig: {
      DATABASE_URL: 'postgres://admin:s3cr3t@127.0.0.1:5432/app',
      API_KEY: 'FAKE-DEMO-api-key-NOT-A-REAL-SECRET-000000',
      SECRET_TOKEN: 'fake.debug.session.token.do.not.use',
      password: 'hunter2',
      debugQuery: "SELECT * FROM users WHERE email = 'admin@example.com'",
    },
    process: {
      pid: process.pid,
      cwd: process.cwd(),
      argv: process.argv,
      execPath: process.execPath,
      versions: process.versions,
    },
  };
}

const handler = {
  get: function (_target, prop) {
    if (prop === '__esModule') return true;
    if (prop === 'then') return undefined;
    if (typeof prop === 'symbol') return undefined;
    return debugState;
  },
  apply: function () {
    return debugState();
  },
  construct: function () {
    return debugState();
  },
};

module.exports = new Proxy(debugState, handler);
'''


#: Dispatch table from behavior to its ``index.js`` body renderer. Kept module
#: level so :func:`_render_index_js` can detect (and loudly reject) any
#: ``MockBehavior`` that lacks a renderer.
_INDEX_RENDERERS = {
    MockBehavior.THROW_UNHANDLED: _render_throw_unhandled,
    MockBehavior.RETURN_EMPTY: _render_return_empty,
    MockBehavior.HTTP_500: _render_http_500,
    MockBehavior.LEAK_DEBUG_STATE: _render_leak_debug_state,
}


#: Subfolder under the target directory where chaos mocks are materialized. The
#: mutated dependency spec points at ``file:./.fileak_mocks/<pkg>`` so npm links
#: the local broken mock instead of the real published package.
MOCKS_DIRNAME = ".fileak_mocks"

#: The two dependency maps in ``package.json`` that may hold a target package,
#: searched in this order. ``original_spec`` and the rewrite both target
#: whichever map first contains the package, so revert can round-trip exactly.
_DEPENDENCY_MAPS = ("dependencies", "devDependencies")


def _mock_spec(package: str) -> str:
    """Return the ``file:`` dependency spec pointing at a package's local mock.

    Always a local relative ``file:`` path (never a published package) so the
    mutation cannot cause supply-chain confusion.
    """
    return f"file:./{MOCKS_DIRNAME}/{package}"


class ChaosMutator:
    """Inject a single chaos profile by swapping a real dependency for a
    deliberately broken local mock, and revert it precisely.

    Injection rewrites the target dependency spec in ``package.json`` to point at
    a local ``file:./.fileak_mocks/<pkg>`` folder and materializes the broken
    mock module there via :func:`render_mock_module`. Every change is captured in
    a :class:`~fileak.models.MutationRecord` so :meth:`revert` can restore the
    prior state losslessly.

    Only two things on disk are ever touched by an injection: the mock
    destination folder (``<target_dir>/.fileak_mocks/<pkg>``) and
    ``package.json``. If the target package is not a current dependency, the
    injection is rejected *before* anything is written or rendered.

    Args:
        target_dir: The target application directory. ``package.json`` is read
            from / written to ``target_dir / "package.json"`` and mocks are
            materialized under ``target_dir / ".fileak_mocks"``.
        profiles: Mapping of profile name to its :class:`ChaosProfile`.
    """

    def __init__(self, target_dir: Path, profiles: dict[str, ChaosProfile]) -> None:
        self.target_dir = Path(target_dir)
        self.profiles = dict(profiles)
        self.package_json = self.target_dir / "package.json"

    def list_profiles(self) -> list[str]:
        """Return the configured chaos profile names."""
        return list(self.profiles.keys())

    def inject(self, profile_name: str) -> MutationRecord:
        """Swap a profile's target dependency for a local broken mock.

        Reads ``package.json``, locates ``profile.target_package`` in
        ``dependencies`` (then ``devDependencies``), captures the original spec
        into a :class:`MutationRecord` *before* any write, rewrites the spec to
        ``file:./.fileak_mocks/<pkg>``, and materializes the mock module on disk.

        The dependency-presence check happens before any file is written or any
        mock is rendered, so a rejection leaves all files unchanged.

        Args:
            profile_name: Name of a configured :class:`ChaosProfile`.

        Returns:
            A :class:`MutationRecord` capturing exactly what changed, for a
            precise :meth:`revert`.

        Raises:
            KeyError: If ``profile_name`` is not a configured profile.
            ValueError: If the profile's ``target_package`` is not a current
                dependency in ``package.json`` (the message names the missing
                package). No files are written or rendered in this case.
        """
        if profile_name not in self.profiles:
            raise KeyError(f"unknown chaos profile: {profile_name!r}")
        profile = self.profiles[profile_name]

        # Read the manifest and validate the dependency BEFORE any mutation so a
        # rejection leaves all files (and the mock folder) untouched.
        with self.package_json.open(encoding="utf-8") as fp:
            manifest = json.load(fp)

        original_spec = self._find_spec(manifest, profile.target_package)
        if original_spec is None:
            raise ValueError(
                f"cannot inject profile {profile_name!r}: target package "
                f"{profile.target_package!r} is not a current dependency in "
                f"{self.package_json}"
            )

        # --- From here on we mutate disk. ---
        mock_path = self.target_dir / MOCKS_DIRNAME / profile.target_package
        render_mock_module(
            mock_path,
            profile.behavior,
            profile.mock_template,
            custom_source=getattr(profile, "custom_source", None),
        )

        # Rewrite the spec in whichever map currently holds the package.
        self._set_spec(manifest, profile.target_package, _mock_spec(profile.target_package))
        self._write_manifest(manifest)

        return MutationRecord(
            profile_name=profile_name,
            target_package=profile.target_package,
            original_spec=original_spec,
            mock_path=mock_path,
            package_json=self.package_json,
        )

    def revert(self, record: MutationRecord) -> None:
        """Undo a single injection, restoring the prior state losslessly.

        Reverses exactly what :meth:`inject` changed for ``record``:

        1. Restores ``record.target_package``'s dependency spec in
           ``package.json`` back to ``record.original_spec`` (into whichever
           dependency map currently holds the ``file:`` mock spec), writing with
           the same :meth:`_write_manifest` formatting used by ``inject`` so the
           inject→revert round-trip reproduces the dependency-spec value exactly.
        2. Removes the mock folder created for that mutation
           (``record.mock_path``).

        The operation is best-effort idempotent: a missing mock folder is not an
        error, so calling ``revert`` twice (or after an external cleanup) is
        safe.

        Args:
            record: The :class:`MutationRecord` returned by the matching
                :meth:`inject` call.
        """
        # 1. Restore the dependency spec in package.json.
        with self.package_json.open(encoding="utf-8") as fp:
            manifest = json.load(fp)

        if self._find_spec(manifest, record.target_package) is not None:
            # Package still present in a dependency map: restore in place so the
            # spec round-trips through the same map inject rewrote.
            self._set_spec(manifest, record.target_package, record.original_spec)
        else:
            # Edge case: the package was dropped from every dependency map after
            # injection. Reinsert it into "dependencies" so the spec is restored.
            manifest.setdefault("dependencies", {})[record.target_package] = (
                record.original_spec
            )
        self._write_manifest(manifest)

        # 2. Remove the mock folder created for this mutation. Safe if already
        # gone (e.g. BaselineGuard cleared .fileak_mocks/, or double-revert).
        mock_path = Path(record.mock_path)
        if mock_path.exists():
            shutil.rmtree(mock_path)

    def _find_spec(self, manifest: dict, package: str) -> str | None:
        """Return the current dependency spec for ``package``, or ``None``.

        Searches ``dependencies`` first, then ``devDependencies`` (the design's
        ``dependencies(pkg)`` lookup, extended to devDependencies for
        robustness). Returns the first match so revert targets the same map.
        """
        for map_name in _DEPENDENCY_MAPS:
            deps = manifest.get(map_name)
            if isinstance(deps, dict) and package in deps:
                return deps[package]
        return None

    def _set_spec(self, manifest: dict, package: str, spec: str) -> None:
        """Set ``package``'s spec in whichever dependency map currently holds it.

        Writes into the first of ``dependencies``/``devDependencies`` that
        already contains ``package`` so the rewrite (and later revert) round-trip
        through the same map.
        """
        for map_name in _DEPENDENCY_MAPS:
            deps = manifest.get(map_name)
            if isinstance(deps, dict) and package in deps:
                deps[package] = spec
                return
        # Unreachable: callers validate presence first.
        raise ValueError(f"{package!r} not found in any dependency map")

    def _write_manifest(self, manifest: dict) -> None:
        """Write ``package.json`` with stable 2-space indent + trailing newline.

        Keeps the file diff-clean; matches the formatting used when reading back
        so an inject→revert round-trip reproduces the dependency-spec value
        exactly.
        """
        with self.package_json.open("w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)
            fp.write("\n")
