# SLVD — Secure Loan Verification Demo. Single entry point for build/test/deploy.
# Lives in source_code/; build/test/deploy targets used by the autonomous loop.
#
# Path model (robust to where make is invoked from):
#   SRC  = this directory (source_code/)
#   ROOT = repo root (parent of source_code/)
# The Docker build CONTEXT is always $(ROOT) so the Dockerfile's
# `COPY source_code/...` instructions resolve correctly.

SRC := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ROOT := $(abspath $(SRC)/..)

PY := $(ROOT)/venv/bin/python
# Target registry for build-images (override: make REG=<your-registry>/<repo> ...)
IMG ?= $(REG)/slvd
VER := $(shell awk '/^version:/{print $$2}' $(ROOT)/charts/slvd/Chart.yaml)
NS := slvd
KUBE ?= $(shell echo $$KUBECONFIG)   # target-cluster kubeconfig (env KUBECONFIG)
K := kubectl --kubeconfig=$(KUBE)

.PHONY: help venv test-unit test-e2e test-security build build-images chart lint \
       prepull deploy verify clean demo-run

help:
	@echo "SLVD make targets"
	@echo "  venv            create the python venv + install deps"
	@echo "  test-unit       run engine + mcp unit tests"
	@echo "  test-e2e        run the 8 local simulation scenarios (S1-S8)"
	@echo "  test-security   gitleaks + rendered-chart secret scan + memo parity"
	@echo "  build           build the portal (npm) — must be green before images"
	@echo "  build-images    build + push engine/mcp and portal images to $(REG)"
	@echo "  chart           helm package + lint + push to chartmuseum"
	@echo "  prepull         pre-pull images in ns $(NS) via probe pods"
	@echo "  deploy          helm install/upgrade + EzAppConfig + VS + verify"
	@echo "  verify          external URL + agent + LLM health checks"
	@echo "  clean           uninstall + delete EzAppConfig + ns (BYOA teardown)"
	@echo "  demo-run        one fresh end-to-end run via the portal API (curl)"

venv:
	cd $(ROOT) && python3 -m venv venv
	$(ROOT)/venv/bin/pip install -q -r $(SRC)/requirements.txt
	cd $(SRC)/portal && npm install --no-audit --no-fund

test-unit:
	$(PY) -m pytest $(SRC)/engine/tests/ $(SRC)/mcp-server/tests/ -q

test-e2e:
	$(PY) $(SRC)/e2e/run_e2e.py

test-security:
	$(PY) $(SRC)/e2e/security_scan.py

build:
	cd $(SRC)/portal && npm run build

build-images:
	docker build -t $(IMG):$(VER) -f $(SRC)/Dockerfile $(ROOT)
	docker build -t $(IMG)-portal:$(VER) -f $(SRC)/Dockerfile.portal $(ROOT)
	docker push $(IMG):$(VER)
	docker push $(IMG)-portal:$(VER)

chart:
	helm lint $(ROOT)/charts/slvd
	helm package $(ROOT)/charts/slvd -d $(ROOT)
	helm push $(ROOT)/slvd-$(VER).tgz http://127.0.0.1:18080

lint:
	$(PY) -m ruff check $(SRC)/engine $(SRC)/mcp-server $(SRC)/e2e 2>/dev/null || true
	helm lint $(ROOT)/charts/slvd
	helm template slvd $(ROOT)/charts/slvd > /dev/null

prepull:
	$(K) create ns $(NS) --dry-run=client -o yaml | $(K) apply -f -
	$(K) run slvd-prepull --rm -it --namespace=$(NS) --image=$(IMG):$(VER) --restart=Never -- image=$(IMG):$(VER) || true
	$(K) run slvd-prepull2 --rm -it --namespace=$(NS) --image=$(IMG)-portal:$(VER) --restart=Never -- echo pulled || true
	$(K) delete pod slvd-prepull slvd-prepull2 -n $(NS) --ignore-not-found

deploy:
	$(K) create ns $(NS) --dry-run=client -o yaml | $(K) apply -f -
	$(K) apply -f $(ROOT)/charts/slvd/ezappconfig.generated.yaml
	$(K) wait --for=condition=ready pod -l app.kubernetes.io/part-of=slvd -n $(NS) --timeout=600s

verify:
	@$(SRC)/e2e/verify_cluster.sh

clean:
	$(K) delete ezappconfig -n ui -l app.kubernetes.io/part-of=slvd --ignore-not-found
	$(K) delete vs -n istio-system -l app.kubernetes.io/part-of=slvd --ignore-not-found 2>/dev/null || true
	$(K) delete ns $(NS) --ignore-not-found
	@echo "cleaned ns $(NS) + slvd EzAppConfig"

demo-run:
	$(SRC)/e2e/demo_run.sh
