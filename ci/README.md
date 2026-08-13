# CI pipeline

`github-actions-ci.yml` is the CI definition for this project. It is parked
here rather than in `.github/workflows/` because the GitHub App used to push
this branch does not hold the `workflows` permission, so a commit containing
that path is rejected by the remote.

To enable it:

```bash
mkdir -p .github/workflows
git mv ci/github-actions-ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions"
git push
```

## What it runs

| Job | Purpose |
|---|---|
| `edge-agent` | ruff + the 162 Python tests |
| `native-core` | builds and runs the 618 C++ assertions, then rebuilds under ASan/UBSan |
| `visualizer` | syntax-checks every module and smoke-tests the server |
| `integration` | starts the agent and the visualiser together and verifies the full path: health, proxied state, glTF export, audit append and chain verification |

All four jobs run without any hardware — the sensor drivers fall back to
simulators, which is the reason the whole stack is CI-testable at all.
