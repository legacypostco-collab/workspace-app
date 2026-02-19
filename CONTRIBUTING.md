# Contributing

## Branching
- Work from `main`
- Keep commits small and focused

## Local checks before commit
```bash
make check
make test
```

If style tools are installed:
```bash
make check-style
```

## Optional pre-commit setup
```bash
source .venv/bin/activate
pip install pre-commit
pre-commit install
```

## Commit message style
- `feat: ...`
- `fix: ...`
- `refactor: ...`
- `chore: ...`
