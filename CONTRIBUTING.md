# Contributing

CIHive welcomes focused bug fixes, evaluation fixtures, security hardening, and
documentation improvements.

Before opening a pull request:

```bash
ruff check src tests
pytest
cihive eval --suite evals/cases.yml
```

Keep model-facing changes accompanied by a deterministic fixture. Do not add a
feature that lets model output publish, merge, or modify protected test/workflow
files without an explicit policy change and security review.
