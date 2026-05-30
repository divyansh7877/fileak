# fileak

Autonomous Dependency Fault-Injection & Leak Detection Engine.

`fileak` is a local, developer-facing Python CLI that hunts for security and
information-leak regressions which only surface when an application's
dependencies misbehave. It injects controlled "chaos" into a target sandbox
app (swapping a real npm dependency for a deliberately broken local mock),
boots the mutated app, and drives [Kane CLI](https://www.testmuai.com/kane-cli/)
against it with semantic, natural-language security assertions.

See the design and requirements under
`.kiro/specs/fault-injection-leak-engine/`.

## Requirements

- Python 3.10+
- Node.js 18+ and a local Google Chrome (for live Kane runs)
- `kane-cli` authenticated (`kane-cli whoami` succeeds)

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
