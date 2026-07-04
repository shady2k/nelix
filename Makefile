# Nelix — common dev commands. Run `make` (or `make help`) for the list.
# Python project: a virtualenv plus requirements.txt. (The out-of-process daemon installs its own
# hash-pinned deps from requirements-daemon.lock at runtime; this venv is for tests + tooling.)

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

# Nelix runs inside the Hermes daemon venv, which is Python 3.11. Pin dev + tests to the
# same interpreter so a stray newer Python can't ship a 3.12+-only API green and break the
# daemon (nelix-cb0: os.waitid — 3.13+ — passed on a 3.14 .venv, died on the 3.11 daemon).
PYTHON ?= python3.11

# Installed-plugin checkout used by `deploy` / `reinstall-plugin`. Override for another profile:
#   make reinstall-plugin PLUGIN_DIR=/path/to/plugins/nelix
PLUGIN_DIR ?= $(HOME)/.hermes/profiles/local/plugins/nelix

.DEFAULT_GOAL := help

.PHONY: help
help:  ## List the available commands
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv:  ## Create the Python 3.11 virtualenv if it is missing
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)

.PHONY: install
install: venv  ## Create the venv and install dependencies
	$(PIP) install -r requirements.txt

.PHONY: check-python
check-python:  ## Fail unless the venv is Python 3.11 (the daemon's floor)
	@$(PY) -c "import sys; v=sys.version_info; sys.exit(0 if v[:2]==(3,11) else 'nelix targets Python 3.11 (the Hermes daemon venv); this $(VENV) is %d.%d — recreate it: rm -rf $(VENV) && make install' % v[:2])"

.PHONY: test
test: check-python  ## Run the test suite
	$(PY) -m pytest -q

.PHONY: clean
clean:  ## Remove Python caches (keeps the venv)
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache

.PHONY: capture
capture:  ## Replay a session raw into golden frame(s): make capture ARGS="<session-dir> --all"
	$(PY) bin/nelix-capture $(ARGS)

.PHONY: vendor-shim
vendor-shim: ## Copy the built libghostty-vt shim.wasm into the daemon package (run after vt-spike-build)
	@test -f spikes/vt-ghostty/.build/shim.wasm || { echo "missing shim.wasm — run 'make vt-spike-build' first"; exit 1; }
	cp spikes/vt-ghostty/.build/shim.wasm daemon/renderer/shim.wasm
	@echo ">> vendored daemon/renderer/shim.wasm"

.PHONY: vt-spike-build
vt-spike-build:  ## Build the libghostty-vt wasm renderer for the VT-render spike (downloads pinned Zig+ghostty)
	spikes/vt-ghostty/build.sh

.PHONY: vt-spike-run
vt-spike-run: venv  ## Render a captured raw via libghostty-vt: make vt-spike-run RAW=<session-raw>
	@test -n "$(RAW)" || { echo "usage: make vt-spike-run RAW=<path-to-session-raw>"; exit 1; }
	@test -f spikes/vt-ghostty/.build/shim.wasm || { echo "missing shim.wasm — run 'make vt-spike-build' first"; exit 1; }
	@$(PY) -c "import wasmtime" 2>/dev/null || $(PIP) install 'wasmtime==45.0.0'
	$(PY) spikes/vt-ghostty/compare.py "$(RAW)"

.PHONY: reinstall-plugin
reinstall-plugin:  ## Reset the installed plugin checkout to origin/main (override PLUGIN_DIR)
	@test -d "$(PLUGIN_DIR)/.git" || { echo "no plugin checkout at $(PLUGIN_DIR)"; exit 1; }
	git -C "$(PLUGIN_DIR)" fetch origin
	git -C "$(PLUGIN_DIR)" reset --hard origin/main
	git -C "$(PLUGIN_DIR)" log --oneline -1

.PHONY: deploy
deploy:  ## Push main, then reset the installed plugin to it
	git push origin main
	$(MAKE) reinstall-plugin
	@echo "Deployed. Restart the Hermes 'local' session (/new) so the daemon reloads the new code."
