# CI for the PM Engine

`pm-engine-tests.yml` is a ready-to-use GitHub Actions workflow that runs all three
verification layers on Python 3.10–3.13:

1. upstream plugin validator (`pm-skills/validate_plugins.py`)
2. upstream consistency suite (`pm-skills/tests`)
3. engine validation + engine test-suite (`pm-engine validate`, `pytest tests`)

It lives here instead of `.github/workflows/` because the automation that opened this
branch is not allowed to create workflow files. To enable it:

```bash
mkdir -p .github/workflows
git mv AURA-PMSkills/ci/pm-engine-tests.yml .github/workflows/pm-engine-tests.yml
git commit -m "Enable PM Engine CI"
```

Locally, the same checks run with `pm-engine test`.
