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
	uv run pytest tests/test_cli.py -v

# ── Code quality ──────────────────────────────────────────────────────────────

.PHONY: lint
lint: ## Lint with ruff
	uv run ruff check src/

.PHONY: format
format: ## Format with ruff
	uv run ruff format src/

# ── Release ───────────────────────────────────────────────────────────────────

# Files allowed in the sdist outside src/puxti/ — anything else triggers a prompt.
SDIST_ALLOWED = pyproject.toml LICENSE NOTICE.md README.md SECURITY.md .env.example .gitignore

.PHONY: build
build: ## Build wheel and sdist
	uv build

.PHONY: check-package
check-package: ## Build sdist and flag any unexpected files (must confirm to continue)
	@uv build --no-sources -q
	@SDIST=$$(ls -t dist/*.tar.gz | head -1); \
	echo "Checking $$SDIST ..."; \
	UNEXPECTED=$$(tar -tzf $$SDIST \
	  | sed 's|^[^/]*/||' \
	  | grep -v '^src/puxti/' \
	  | grep -v '^$$' \
	  | grep -vFf <(printf '%s\n' $(SDIST_ALLOWED))); \
	if [ -z "$$UNEXPECTED" ]; then \
	  echo "  Package looks clean — only src/puxti/ and approved root files."; \
	else \
	  echo ""; \
	  echo "  \033[33mUnexpected files in sdist:\033[0m"; \
	  echo "$$UNEXPECTED" | sed 's/^/    /'; \
	  echo ""; \
	  printf "  Continue anyway? [y/N] "; read ans; \
	  case "$$ans" in y|Y) echo "  Proceeding.";; *) echo "  Aborted."; exit 1;; esac; \
	fi

.PHONY: publish
publish: ## Run pre-release checks then publish to PyPI (usage: make publish VERSION=0.6.0)
	@[ -n "$(VERSION)" ] || (echo "Usage: make publish VERSION=x.y.z"; exit 1)
	@$(MAKE) test
	@$(MAKE) check-package
	@echo ""
	@echo "  Publishing puxti $(VERSION) to PyPI..."
	@uv publish
	@echo "  Done."

# ── Setup ─────────────────────────────────────────────────────────────────────

.PHONY: install
install: ## Install all dependencies (including dev)
	uv sync --extra dev

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
