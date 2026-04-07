.PHONY: install dev test lint typecheck build clean release-check fmt

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install: $(VENV)
	$(PIP) install -e ".[dev]"
	@echo ""
	@echo "✓ vimgym installed in $(VENV)"
	@echo "  Activate it with: source $(VENV)/bin/activate"
	@echo "  Or use: direnv allow"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

dev:
	VIMGYM_WATCH_PATH=./data $(VENV)/bin/vg start

test:
	$(VENV)/bin/pytest -v --tb=short

test-quick:
	$(VENV)/bin/pytest -q --tb=line -x

lint:
	$(VENV)/bin/ruff check src/ tests/

fmt:
	$(VENV)/bin/ruff check --fix src/ tests/

typecheck:
	$(VENV)/bin/mypy src/vimgym/ --ignore-missing-imports

build: clean
	$(PY) -m build
	$(VENV)/bin/twine check dist/*

release-check: lint test build
	@echo ""
	@echo "✓ release artifacts ready in dist/"
	@ls -la dist/

clean:
	rm -rf dist build src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
