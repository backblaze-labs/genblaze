.PHONY: install install-dev test lint fmt typecheck coverage clean ts-types ts-types-check

install:
	pip install -e libs/core
	pip install -e libs/connectors/replicate
	pip install -e libs/connectors/s3
	pip install -e libs/connectors/openai
	pip install -e libs/connectors/google
	pip install -e libs/connectors/runway
	pip install -e libs/connectors/luma
	pip install -e libs/connectors/decart
	pip install -e libs/connectors/elevenlabs
	pip install -e libs/connectors/stability-audio
	pip install -e libs/connectors/lmnt
	pip install -e libs/connectors/gmicloud
	pip install -e libs/connectors/langsmith
	pip install -e libs/connectors/nvidia
	pip install -e cli
	pip install -e libs/meta

install-dev:
	pip install -e "libs/core[dev]"
	pip install -e "libs/connectors/replicate[dev]"
	pip install -e "libs/connectors/s3[dev]"
	pip install -e "libs/connectors/openai[dev]"
	pip install -e "libs/connectors/google[dev]"
	pip install -e "libs/connectors/runway[dev]"
	pip install -e "libs/connectors/luma[dev]"
	pip install -e "libs/connectors/decart[dev]"
	pip install -e "libs/connectors/elevenlabs[dev]"
	pip install -e "libs/connectors/stability-audio[dev]"
	pip install -e "libs/connectors/lmnt[dev]"
	pip install -e "libs/connectors/gmicloud[dev]"
	pip install -e "libs/connectors/langsmith[dev]"
	pip install -e "libs/connectors/nvidia[dev]"
	pip install -e "cli[dev]"
	pip install -e "libs/meta[dev]"

test:
	cd libs/core && pytest tests/ -v
	cd libs/connectors/replicate && pytest -v
	cd libs/connectors/s3 && pytest -v
	cd libs/connectors/openai && pytest -v
	cd libs/connectors/google && pytest -v
	cd libs/connectors/runway && pytest -v
	cd libs/connectors/luma && pytest -v
	cd libs/connectors/decart && pytest -v
	cd libs/connectors/elevenlabs && pytest -v
	cd libs/connectors/stability-audio && pytest -v
	cd libs/connectors/lmnt && pytest -v
	cd libs/connectors/gmicloud && pytest -v
	cd libs/connectors/langsmith && pytest -v
	cd libs/connectors/nvidia && pytest -v
	cd cli && pytest tests/ -v
	cd libs/meta && pytest tests/ -v

lint:
	ruff check libs/ cli/ examples/
	ruff format --check libs/ cli/ examples/

fmt:
	ruff format libs/ cli/ examples/
	ruff check --fix libs/ cli/ examples/

typecheck:
	mypy libs/core/genblaze_core/ --ignore-missing-imports

coverage:
	cd libs/core && pytest tests/ --cov=genblaze_core --cov-report=term-missing --cov-report=html --cov-fail-under=70

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true

# Regenerate libs/spec/ts/genblaze.d.ts from the JSON Schemas.
# Requires Node. See libs/spec/README.md for details.
ts-types:
	libs/spec/scripts/generate-types.sh

# CI drift guard: fails if regenerated types would differ from committed.
# Run this in CI after ts-types; any diff means someone changed a schema
# without running `make ts-types`.
ts-types-check: ts-types
	@git diff --exit-code libs/spec/ts/ \
		|| (echo "ERROR: libs/spec/ts/ is out of date. Run 'make ts-types' and commit." && exit 1)
