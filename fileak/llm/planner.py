"""LLM chaos planner — Anthropic Claude authors fileak chaos experiments.

The planner turns a target app's dependency manifest (and optional source files)
into a set of :class:`~fileak.models.ChaosProfile` + :class:`~fileak.models.SecurityAssertion`
objects. It is the "LLM drives the chaos" half of the system; Kane remains the
analyst that judges whether each experiment leaks.

Two modes:

* **params-only** (default, lower risk): the model chooses, per experiment, a
  target package, one of the four vetted :class:`~fileak.models.MockBehavior`
  values, and tailored assertions. The mock CODE is still fileak's reviewed
  template. No model-authored code is executed.

* **free-form** (``allow_custom_code=True``, opt-in): the model additionally
  authors the mock's ``index.js``. Every generated source is run through
  :func:`fileak.llm.mock_guard.validate_mock_source` BEFORE it is accepted; an
  experiment whose mock fails validation is dropped (with the reason recorded)
  rather than executed.

Determinism / cost: model output is non-deterministic and costs tokens, so each
plan is cached to disk (keyed by target + options) and reused unless ``refresh``
is set. The API key is read from the environment (``ANTHROPIC_API_KEY``, loaded
from a local ``.env`` if present) and is never written into plans, mocks, or
reports.

Requires the ``anthropic`` SDK. Import of this module does not require a key; a
key is only needed when :meth:`ChaosPlanner.plan` actually calls the API (a
cached plan can be loaded without one).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fileak.llm.mock_guard import MockGuardError, validate_mock_source
from fileak.models import ChaosProfile, MockBehavior, SecurityAssertion

#: Default Anthropic model. Overridable via ``--model`` / ``FILEAK_LLM_MODEL``.
#: ``claude-opus-4-7`` is the most capable generally-available model at time of
#: writing; the alias form tracks the latest Opus 4.7 snapshot.
DEFAULT_MODEL = "claude-opus-4-7"

#: Cap on source bytes per file sent to the model, to bound token usage.
_MAX_SOURCE_CHARS = 6000

#: Max tokens for the planning response.
_MAX_TOKENS = 8000


class PlannerError(Exception):
    """Raised when the planner cannot produce a usable plan (API/parse error)."""


@dataclass
class DroppedProfile:
    """A proposed experiment rejected before execution, with the reason."""

    name: str
    target_package: str
    reason: str


@dataclass
class ChaosPlan:
    """The planner's output: runnable profiles/assertions + provenance.

    ``profiles`` and ``assertions`` plug straight into ``EngineConfig`` /
    ``run_chaos``. ``dropped`` records experiments the model proposed that were
    rejected (e.g. mock failed the safety gate, or target package absent), so the
    dashboard can show what was filtered and why. ``model`` / ``allow_custom_code``
    capture how the plan was produced.
    """

    profiles: list[ChaosProfile] = field(default_factory=list)
    assertions: list[SecurityAssertion] = field(default_factory=list)
    dropped: list[DroppedProfile] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    allow_custom_code: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for caching + dashboard consumption."""
        return {
            "model": self.model,
            "allow_custom_code": self.allow_custom_code,
            "summary": self.summary,
            "profiles": [
                {
                    "name": p.name,
                    "target_package": p.target_package,
                    "behavior": p.behavior.value,
                    "mock_template": p.mock_template,
                    "description": p.description,
                    "rationale": p.rationale,
                    "custom_source": p.custom_source,
                }
                for p in self.profiles
            ],
            "assertions": [
                {"id": a.id, "prompt": a.prompt, "applies_to": list(a.applies_to)}
                for a in self.assertions
            ],
            "dropped": [
                {"name": d.name, "target_package": d.target_package, "reason": d.reason}
                for d in self.dropped
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChaosPlan":
        """Rebuild a plan from its cached dict form."""
        profiles = [
            ChaosProfile(
                name=p["name"],
                target_package=p["target_package"],
                behavior=MockBehavior(p["behavior"]),
                mock_template=p.get("mock_template", "llm_generated"),
                description=p.get("description", ""),
                custom_source=p.get("custom_source"),
                rationale=p.get("rationale", ""),
            )
            for p in data.get("profiles", [])
        ]
        assertions = [
            SecurityAssertion(
                id=a["id"], prompt=a["prompt"], applies_to=list(a.get("applies_to", []))
            )
            for a in data.get("assertions", [])
        ]
        dropped = [
            DroppedProfile(d["name"], d["target_package"], d["reason"])
            for d in data.get("dropped", [])
        ]
        return cls(
            profiles=profiles,
            assertions=assertions,
            dropped=dropped,
            model=data.get("model", DEFAULT_MODEL),
            allow_custom_code=bool(data.get("allow_custom_code", False)),
            summary=data.get("summary", ""),
        )


def _load_dotenv_key() -> Optional[str]:
    """Return ANTHROPIC_API_KEY from the env, loading a local .env if present."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        return None
    return os.environ.get("ANTHROPIC_API_KEY")


def read_target_manifest(target_dir: Path) -> dict[str, Any]:
    """Read the target's package.json (read-only) for the planning prompt."""
    pkg = Path(target_dir) / "package.json"
    if not pkg.is_file():
        raise PlannerError(f"package.json not found at {pkg}")
    try:
        return json.loads(pkg.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlannerError(f"could not parse {pkg}: {exc}") from exc


def collect_source_excerpts(target_dir: Path, max_files: int = 4) -> dict[str, str]:
    """Best-effort: gather a few small JS/TS source files for context.

    Helps the model tie a dependency to how the app *uses* it (e.g. which page
    renders an error). Skips node_modules and large files; truncates each to
    ``_MAX_SOURCE_CHARS``. Never raises — returns whatever it could read.
    """
    target = Path(target_dir)
    excerpts: dict[str, str] = {}
    candidates = ["server.js", "app.js", "index.js", "src/server.js", "src/app.js"]
    for rel in candidates:
        if len(excerpts) >= max_files:
            break
        path = target / rel
        try:
            if path.is_file() and "node_modules" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="replace")
                excerpts[rel] = text[:_MAX_SOURCE_CHARS]
        except OSError:
            continue
    return excerpts


def declared_dependencies(manifest: dict[str, Any]) -> list[str]:
    """Union of dependencies + devDependencies names from a manifest."""
    names: list[str] = []
    for key in ("dependencies", "devDependencies"):
        deps = manifest.get(key)
        if isinstance(deps, dict):
            names.extend(deps.keys())
    return sorted(set(names))


_SYSTEM_PROMPT = """You are a security chaos-engineering planner for a local-only \
fault-injection tool. Your job is to design experiments that swap a real npm \
dependency for a deliberately BROKEN local mock, so a separate browser-agent \
analyst can check whether the broken state leaks sensitive internals (stack \
traces, file paths, source, SQL, secrets, env vars) to the rendered page.

You do NOT judge leaks — you only design the chaos. Be creative but realistic: \
target dependencies the app actually uses, and pick break modes that plausibly \
surface (or gracefully hide) internal state in the UI.

CRITICAL — the analyst is a VISION + DOM browser agent. It can ONLY judge what is \
VISIBLE on the rendered page. Every assertion you write MUST be verifiable purely \
by looking at rendered page content (text, sections, error blocks, JSON dumps). \
You MUST NOT write assertions about things a vision agent cannot see, including: \
HTTP status codes, response headers, network requests, console logs, timing, or \
anything not painted on the screen. Phrase every assertion as "inspect the \
<section/page> and verify the visible text does/does not contain X". If you want \
to check the app stays up, say "the page renders its main heading and visible \
content" — never "returns HTTP 200".

Mocks must also let the app BOOT: the app may call the dependency during normal \
rendering, so a mock that makes the process crash outright will prevent any \
judgement. Prefer breaks that surface in page content over breaks that kill the \
process before it can render.

Return ONLY valid JSON matching the requested schema. No prose outside the JSON."""


def _build_user_prompt(
    manifest: dict[str, Any],
    sources: dict[str, str],
    deps: list[str],
    count: int,
    allow_custom_code: bool,
) -> str:
    """Compose the planning instruction with schema + constraints."""
    behaviors = ", ".join(b.value for b in MockBehavior)
    src_block = "\n\n".join(
        f"--- {name} ---\n{body}" for name, body in sources.items()
    ) or "(no source files provided)"

    custom_clause = (
        (
            "For each experiment you MAY also include a \"custom_source\" string: a "
            "self-contained CommonJS module (module.exports = ...) that stands in for "
            "the target package and misbehaves in a novel way. HARD RULES for "
            "custom_source: it must NOT require/import anything except the package it "
            "mocks or relative paths; NO child_process, fs, net/http/https, fetch, "
            "eval, new Function, vm, or process.exit. Keep it under 8000 chars. If you "
            "cannot satisfy these rules, omit custom_source and rely on \"behavior\"."
        )
        if allow_custom_code
        else (
            "Do NOT author mock code. Choose only a \"behavior\" from the allowed set; "
            "the tool supplies the mock implementation."
        )
    )

    return f"""Design {count} distinct chaos experiments for this target app.

package.json:
{json.dumps(manifest, indent=2)[:4000]}

Declared dependencies you may target: {", ".join(deps) or "(none)"}

Source excerpts (how the app uses its deps):
{src_block}

Allowed mock behaviors: {behaviors}

{custom_clause}

Return JSON with this exact shape:
{{
  "summary": "one sentence on the overall strategy",
  "profiles": [
    {{
      "name": "snake_case_unique_name",
      "target_package": "<one of the declared dependencies>",
      "behavior": "<one of: {behaviors}>",
      "description": "what breaks and what leak you expect",
      "rationale": "why this experiment matters for this app"{', "custom_source": "<CJS module string, optional>"' if allow_custom_code else ""}
    }}
  ],
  "assertions": [
    {{
      "id": "snake_case_id",
      "prompt": "natural-language instruction for the browser agent to inspect the page and verify no internals leak",
      "applies_to": ["profile_name", "..."]
    }}
  ]
}}

Rules:
- Every target_package MUST be in the declared dependencies list.
- profile names and assertion ids must be unique.
- An empty "applies_to" means the assertion applies to all profiles.
- Provide at least one assertion that applies to all profiles.
- Assertions MUST be verifiable by VISUAL inspection of rendered page content
  only. NEVER write an assertion about HTTP status codes, response headers,
  network/console activity, or anything not visible on the page. To check the
  app is up, ask the agent to confirm the page renders its main heading and
  visible content — do NOT mention "200" or status codes."""


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response; raise on failure."""
    text = text.strip()
    # Strip ```json fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Otherwise find the outermost braces.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise PlannerError("model response contained no JSON object")
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"could not parse model JSON: {exc}") from exc


def _cache_key(target_dir: Path, count: int, allow_custom_code: bool, model: str) -> str:
    raw = f"{Path(target_dir).resolve()}|{count}|{allow_custom_code}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ChaosPlanner:
    """Authors chaos plans via Anthropic Claude, with a safety gate + cache.

    Args:
        model: Anthropic model id (default :data:`DEFAULT_MODEL`).
        allow_custom_code: When True, the model may author mock ``index.js`` and
            each is validated by the mock guard; failures are dropped. When False
            (default) only vetted behavior templates are used.
        cache_dir: Where plans are cached as JSON. Defaults to
            ``<target>/.fileak_plans`` at plan time.
        api_key: Explicit key; otherwise read from env / ``.env``.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        allow_custom_code: bool = False,
        cache_dir: Optional[Path] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.model = model
        self.allow_custom_code = allow_custom_code
        self.cache_dir = cache_dir
        self._api_key = api_key
        # Records the prompts sent on the most recent (non-cached) model call so
        # the runner can surface them in the dashboard's LLM prompts panel.
        # Each entry: {"stage", "model", "system", "user", "response_excerpt"}.
        self.prompt_log: list[dict] = []

    # -- public API ---------------------------------------------------------

    def plan(
        self,
        target_dir: Path,
        count: int = 5,
        *,
        refresh: bool = False,
    ) -> ChaosPlan:
        """Produce (or load a cached) chaos plan for ``target_dir``.

        Reads the target manifest + a few source excerpts, asks the model for
        ``count`` experiments, validates them (target package present, names
        unique, mock source passes the guard), and returns a :class:`ChaosPlan`.
        Caches the result; pass ``refresh=True`` to force a new API call.
        """
        target_dir = Path(target_dir)
        cache_dir = self.cache_dir or (target_dir / ".fileak_plans")
        cache_path = cache_dir / (
            _cache_key(target_dir, count, self.allow_custom_code, self.model) + ".json"
        )

        if not refresh and cache_path.is_file():
            try:
                return ChaosPlan.from_dict(json.loads(cache_path.read_text("utf-8")))
            except (OSError, json.JSONDecodeError, KeyError):
                pass  # fall through to regenerate

        manifest = read_target_manifest(target_dir)
        deps = declared_dependencies(manifest)
        if not deps:
            raise PlannerError(
                f"{target_dir}/package.json declares no dependencies to target"
            )
        sources = collect_source_excerpts(target_dir)

        raw = self._call_model(manifest, sources, deps, count)
        plan = self._build_plan(raw, deps)

        # cache
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(plan.to_dict(), indent=2), "utf-8")
        except OSError:
            pass
        return plan

    # -- internals ----------------------------------------------------------

    def _client(self):
        key = self._api_key or _load_dotenv_key()
        if not key:
            raise PlannerError(
                "ANTHROPIC_API_KEY not set. Add it to a .env file at the repo "
                "root or export it before running the planner."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise PlannerError(
                "the 'anthropic' package is required for LLM planning "
                "(pip install anthropic)."
            ) from exc
        return anthropic.Anthropic(api_key=key)

    def _call_model(self, manifest, sources, deps, count) -> dict[str, Any]:
        client = self._client()
        prompt = _build_user_prompt(
            manifest, sources, deps, count, self.allow_custom_code
        )
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK raises various error types
            raise PlannerError(f"Anthropic API call failed: {exc}") from exc

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        # Record the exact prompts + a response excerpt for the dashboard panel.
        self.prompt_log.append(
            {
                "stage": "plan",
                "model": self.model,
                "system": _SYSTEM_PROMPT,
                "user": prompt,
                "response_excerpt": text[:4000],
            }
        )
        return _extract_json(text)

    def _build_plan(self, raw: dict[str, Any], deps: list[str]) -> ChaosPlan:
        """Validate the model's JSON into runnable profiles/assertions.

        Drops (rather than runs) any experiment that names a non-declared
        package, duplicates a name, has an unknown behavior, or — in free-form
        mode — whose custom_source fails the mock-guard safety gate.
        """
        plan = ChaosPlan(model=self.model, allow_custom_code=self.allow_custom_code)
        plan.summary = str(raw.get("summary", ""))[:300]
        seen_names: set[str] = set()
        dep_set = set(deps)

        for item in raw.get("profiles", []):
            name = str(item.get("name", "")).strip()
            pkg = str(item.get("target_package", "")).strip()
            if not name or not pkg:
                continue
            if name in seen_names:
                plan.dropped.append(DroppedProfile(name, pkg, "duplicate profile name"))
                continue
            if pkg not in dep_set:
                plan.dropped.append(
                    DroppedProfile(name, pkg, f"target package {pkg!r} not declared")
                )
                continue
            try:
                behavior = MockBehavior(str(item.get("behavior", "")).strip())
            except ValueError:
                plan.dropped.append(
                    DroppedProfile(name, pkg, f"unknown behavior {item.get('behavior')!r}")
                )
                continue

            custom = item.get("custom_source")
            if custom is not None:
                if not self.allow_custom_code:
                    custom = None  # ignore code the operator didn't opt into
                else:
                    try:
                        validate_mock_source(str(custom), pkg)
                    except MockGuardError as exc:
                        plan.dropped.append(
                            DroppedProfile(name, pkg, f"mock failed safety gate: {exc}")
                        )
                        continue

            seen_names.add(name)
            plan.profiles.append(
                ChaosProfile(
                    name=name,
                    target_package=pkg,
                    behavior=behavior,
                    mock_template="llm_generated" if custom else f"llm_{behavior.value}",
                    description=str(item.get("description", ""))[:300],
                    custom_source=custom,
                    rationale=str(item.get("rationale", ""))[:400],
                )
            )

        valid_names = {p.name for p in plan.profiles}
        for item in raw.get("assertions", []):
            aid = str(item.get("id", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
            if not aid or not prompt:
                continue
            applies = [
                n for n in item.get("applies_to", []) if n in valid_names
            ]
            plan.assertions.append(
                SecurityAssertion(id=aid, prompt=prompt, applies_to=applies)
            )

        if not plan.profiles:
            raise PlannerError(
                "the model proposed no runnable experiments (all dropped). "
                "See plan.dropped for reasons."
            )
        # Guarantee at least one all-profiles assertion so coverage is non-empty.
        if not any(not a.applies_to for a in plan.assertions):
            plan.assertions.insert(
                0,
                SecurityAssertion(
                    id="homepage_no_leak",
                    prompt=(
                        "Inspect the entire page. Verify that no raw stack traces, "
                        "file paths, source code, database queries, secret keys, or "
                        "environment variables are visible anywhere on the screen."
                    ),
                    applies_to=[],
                ),
            )
        return plan
