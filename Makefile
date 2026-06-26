.PHONY: install install-dev test lint deptry fmt typecheck coverage clean ts-types ts-types-check pypi-metadata-check pypi-pin-parity release-smoke pre-release post-release

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
	pip install -e libs/connectors/hume
	pip install -e libs/connectors/gmicloud
	pip install -e libs/connectors/langsmith
	pip install -e libs/connectors/nvidia
	pip install -e libs/connectors/assemblyai
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
	pip install -e "libs/connectors/hume[dev]"
	pip install -e "libs/connectors/gmicloud[dev]"
	pip install -e "libs/connectors/langsmith[dev]"
	pip install -e "libs/connectors/nvidia[dev]"
	pip install -e "libs/connectors/assemblyai[dev]"
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
	cd libs/connectors/hume && pytest -v
	cd libs/connectors/gmicloud && pytest -v
	cd libs/connectors/langsmith && pytest -v
	cd libs/connectors/nvidia && pytest -v
	cd libs/connectors/assemblyai && pytest -v
	cd cli && pytest tests/ -v
	cd libs/meta && pytest tests/ -v
	pytest tools/tests/ -v

lint:
	ruff check libs/ cli/ examples/
	ruff format --check libs/ cli/ examples/
	$(MAKE) deptry

# Dependency hygiene gate. Per-package because each carries its own
# [project.dependencies] + [tool.deptry] config. Fails on undeclared imports
# (DEP001) — the replicate/httpx clean-install crash class (#37/#106) — plus
# misclassified transitive deps (DEP003) and unused declared deps (DEP002).
# Requires the workspace installed (run after `make install-dev`); deptry maps
# imports to packages via installed metadata. The connector glob auto-includes
# newly scaffolded packages (matching the CI `build` job) so the gate can't
# silently skip one. libs/meta is intentionally excluded: it's an umbrella
# metapackage whose deps are install-time bundles, not imports, so deptry's
# import-vs-declaration model does not apply.
deptry:
	@for pkg in libs/core cli libs/connectors/*/; do \
		pkg="$${pkg%/}"; \
		echo "=== deptry: $$pkg ==="; \
		(cd "$$pkg" && deptry .) || exit 1; \
	done

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

# CI gate: every published Python package has consistent PyPI metadata.
# A release-prep PR that adds a new package whose pyproject.toml is
# missing classifiers / authors / project_urls / keywords / etc. fails
# loudly here rather than rendering empty on PyPI later.
pypi-metadata-check:
	@python tools/check_pypi_metadata.py --strict

# Pre-release drift guard. For every package whose source version is
# already on PyPI, compare [project.dependencies] against the wheel's
# Requires-Dist. A mismatch means ``skip-existing`` would silently
# no-op a divergent wheel — the trap that shipped twice (s3 in 0.3.0,
# langsmith + cli in 0.3.2). See tools/check_pin_parity.py.
pypi-pin-parity:
	@python tools/check_pin_parity.py

# Pre-release wheel install smoke test. Builds every package to a
# local wheelhouse, then installs the local genblaze wheels into a
# fresh venv while PyPI remains enabled for transitive dependencies.
# Catches version-constraint breakage that the editable installs in
# ``install-dev`` bypass. Run this before tagging a release.
release-smoke:
	@tools/release_smoke.sh

# Local pre-flight before tagging a release. Runs the same gates the
# release workflow runs (validate-version excepted — that needs the
# tag and the CHANGELOG cut). A clean ``make pre-release`` on ``main``
# is a strong signal the publish pipeline will be green. Ordered for
# quick-fail: cheap checks first, then the full test suite and the
# wheelhouse smoke. See RELEASING.md.
pre-release: lint typecheck ts-types-check pypi-metadata-check pypi-pin-parity test release-smoke
	@echo ""
	@echo "pre-release gates passed."
	@echo "Next: bump pyproject.toml versions, cut CHANGELOG, tag, gh release create."
	@echo "See RELEASING.md for the full flow."

# Post-publish verification. Installs the umbrella from public PyPI
# into a throwaway venv and imports the umbrella, core, s3, and every
# genblaze[all] connector, mirroring what real users see — this is what
# catches the same-version-different-content trap that bit the 0.3.0
# wave. Pass the umbrella version via VERSION=<X.Y.Z>:
#
#   make post-release VERSION=0.4.0
#
# VERSION is the umbrella version (libs/meta/pyproject.toml), which
# may differ from the wave name (e.g. wave 0.3.0 shipped umbrella
# 0.4.0). On failure the venv is left in place for inspection.
post-release:
	@if [ -z "$(VERSION)" ]; then \
		echo "ERROR: VERSION is required. Usage: make post-release VERSION=0.4.0"; \
		exit 1; \
	fi
	@set -e; \
		VENV=$$(mktemp -d)/verify-$(VERSION); \
		echo "Creating fresh venv at $$VENV"; \
		python -m venv $$VENV; \
		$$VENV/bin/pip install --quiet --upgrade pip; \
		echo "Installing genblaze[all]==$(VERSION) from public PyPI..."; \
		$$VENV/bin/pip install "genblaze[all]==$(VERSION)"; \
		echo ""; \
		echo "Smoke-importing genblaze[all] modules..."; \
		$$VENV/bin/python tools/release_import_smoke.py; \
		echo ""; \
		echo "Installed versions:"; \
		$$VENV/bin/pip show genblaze genblaze-core genblaze-s3 | grep -E '^(Name|Version)'; \
		rm -rf $$(dirname $$VENV); \
		echo ""; \
		echo "genblaze==$(VERSION) verified on PyPI."
