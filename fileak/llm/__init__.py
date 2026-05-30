"""LLM-driven chaos planning for fileak.

This subpackage lets an Anthropic Claude model *author* chaos experiments —
proposing which dependency to break, how, and what semantic security assertions
to hand Kane — instead of relying on a hand-written profile list.

Modules:

* :mod:`fileak.llm.mock_guard` — static safety validation for LLM-generated mock
  JavaScript (syntax gate + denylist). The single most security-critical piece,
  since generated code is ``npm install``-ed and executed inside the target app.
* :mod:`fileak.llm.planner` — the Anthropic client that turns a target's
  ``package.json`` (+ optional source) into ``ChaosPlan`` objects.
* :mod:`fileak.llm.advisor` — post-run remediation: feeds LEAK findings + evidence
  back to the model for concrete fix suggestions.

The LLM never judges leaks — Kane does. The model only proposes the chaos; the
existing orchestrator/reporter pipeline runs and judges it unchanged.
"""

from fileak.llm.mock_guard import MockGuardError, validate_mock_source

__all__ = ["MockGuardError", "validate_mock_source"]
