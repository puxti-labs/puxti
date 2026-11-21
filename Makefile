.DEFAULT_GOAL := help

# ── Neo4j ─────────────────────────────────────────────────────────────────────

.PHONY: neo4j
neo4j: ## Start Neo4j (required by Puxti for its Knowledge Graph)
	docker compose up -d

.PHONY: neo4j-down
neo4j-down: ## Stop Neo4j
	docker compose down

# ── Tests ─────────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run all tests
	uv run pytest

.PHONY: test-cli
test-cli: ## Run CLI tests only
	uv run pytest tests/cli -v

# ── Code quality ──────────────────────────────────────────────────────────────

.PHONY: lint
lint: ## Lint with ruff
	uv run ruff check src/ packages/

.PHONY: format
format: ## Format with ruff
	uv run ruff format src/ packages/

.PHONY: bi-schema
bi-schema: ## Regenerate the BI spec JSON Schema from the puxti-bi-core models
	uv run python -m puxti_bi_core.schema packages/puxti-bi-core/src/puxti_bi_core/schemas/v1alpha1.json

# ── Release ───────────────────────────────────────────────────────────────────

.PHONY: build
build: ## Build wheels and sdists for puxti and every workspace package
	uv build --all-packages

.PHONY: check-package
check-package: ## Build every sdist and abort if any holds unexpected files
	@rm -rf dist/check && uv build --all-packages --no-sources -q -o dist/check
	@scripts/check_sdist.sh dist/check

.PHONY: publish
publish: ## Run pre-release checks then publish to PyPI (usage: make publish VERSION=0.6.0)
	@[ -n "$(VERSION)" ] || (echo "Usage: make publish VERSION=x.y.z"; exit 1)
	@$(MAKE) test
	@$(MAKE) check-package
	@echo ""
	@echo "  Publishing puxti $(VERSION) and its workspace packages to PyPI..."
	@uv publish dist/check/*
	@echo "  Done."

# ── Setup ─────────────────────────────────────────────────────────────────────

.PHONY: install
install: ## Install all dependencies (including dev and workspace packages)
	uv sync --all-packages --extra dev

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
