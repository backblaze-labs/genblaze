.PHONY: install install-dev test lint fmt typecheck coverage clean

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
	pip install -e cli

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
	pip install -e "cli[dev]"

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
	cd cli && pytest tests/ -v

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
