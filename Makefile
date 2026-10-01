SHELL := /bin/bash
PYTHON ?= python3
PYTHONPATH := src

.PHONY: test test-v test-saf test-all race smoke run clean fmt help

help: ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test: ## run full suite (unit + integration + concurrency + chaos)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s . -p 'test_*.py'

test-v: ## verbose suite
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s . -p 'test_*.py' -v

test-saf: ## run the SAF subproject suite (needs pydantic; `pip install -e ".[test]"`)
	cd sovereign-agent-fabric-v20 && PYTHONPATH=. $(PYTHON) -m pytest -q

test-all: test test-saf ## kernel + SAF suites

race: ## stress: repeat the suite 5x (catches flaky interleavings)
	@for i in 1 2 3 4 5; do \
		echo "== iteration $$i =="; \
		PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s . -p 'test_*.py' -q || exit 1; \
	done

smoke: ## live end-to-end smoke against a temporary store
	@set -e; \
	PORT=18191; \
	rm -f /tmp/es-smoke.db*; \
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m effective_scale --store /tmp/es-smoke.db \
		--listen 127.0.0.1:$$PORT --admin-token smoke-admin --demo & \
	PID=$$!; \
	sleep 2; \
	echo "health:"; curl -fsS http://127.0.0.1:$$PORT/v1/health/ready; echo; \
	echo "status:"; curl -fsS http://127.0.0.1:$$PORT/v1/status; echo; \
	TOKEN=$$(curl -fsS -X POST http://127.0.0.1:$$PORT/v1/tokens \
		-H 'Content-Type: application/json' -H 'X-Admin-Token: smoke-admin' \
		-d '{"namespace":"demo","scopes":["read","write","admin"]}' \
		| $(PYTHON) -c "import json,sys; print(json.load(sys.stdin)['token'])"); \
	echo "workflow state:"; curl -fsS -H "Authorization: Bearer $$TOKEN" \
		http://127.0.0.1:$$PORT/v1/workflows | head -c 180; echo; \
	kill $$PID 2>/dev/null || true; \
	echo "SMOKE OK"

run: ## start the kernel (default :8080, local file store)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m effective_scale --demo

clean: ## remove runtime artifacts
	rm -rf data __pycache__ src/**/__pycache__ tests/__pycache__ *.db *.db-wal *.db-shm /tmp/es-smoke.db*
