SHELL := /bin/bash
.DEFAULT_GOAL := help

KUADRANT_CLUSTER := ai-gw-kuadrant
ENVOY_CLUSTER := ai-gw-envoy
AGENT_CLUSTER := ai-gw-agent
CLUSTERS := $(KUADRANT_CLUSTER) $(ENVOY_CLUSTER) $(AGENT_CLUSTER)
CONTEXT := kind-$(CLUSTER)
PYTHON ?= python3

.PHONY: help check-cluster up cluster install runtime gateway kserve pools pools-down \
	policies policies-down semantic-router semantic-router-down keycloak compare \
	comparison-summary status start-cluster stop-cluster stop-clusters down \
	up-all policies-all semantic-router-all features-all compare-all down-all \
	charts agent-ui test validate vllm-production vllm-production-down vllm-validate

help: ## show targets; comparison-cluster targets require CLUSTER=<name>
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-23s %s\n",$$1,$$2}'

check-cluster:
	@case "$(CLUSTER)" in $(KUADRANT_CLUSTER)|$(ENVOY_CLUSTER)|$(AGENT_CLUSTER)) ;; *) echo 'set CLUSTER to $(KUADRANT_CLUSTER), $(ENVOY_CLUSTER), or $(AGENT_CLUSTER)' >&2; exit 2 ;; esac

up: check-cluster ## create/start and reconcile one complete gateway + KServe cluster
	@$(MAKE) cluster CLUSTER="$(CLUSTER)"
	@$(MAKE) install CLUSTER="$(CLUSTER)"
	@$(MAKE) runtime CLUSTER="$(CLUSTER)"
	@$(MAKE) gateway CLUSTER="$(CLUSTER)"
	@$(MAKE) kserve CLUSTER="$(CLUSTER)"
	@echo
	@echo "$(CLUSTER) ready; run 'make policies CLUSTER=$(CLUSTER)' or 'make compare CLUSTER=$(CLUSTER)'"

cluster: check-cluster ## create one kind cluster, or start its retained node
	bash scripts/ensure-cluster.sh "$(CLUSTER)"

install: check-cluster ## install one gateway stack and KServe controller
	@case "$(CLUSTER)" in \
	  $(KUADRANT_CLUSTER)) bash scripts/install-kuadrant.sh "$(CONTEXT)" ;; \
	  $(ENVOY_CLUSTER)) bash scripts/install-envoy-ai-gateway.sh "$(CONTEXT)" ;; \
	  $(AGENT_CLUSTER)) bash scripts/install-agentgateway.sh "$(CONTEXT)" ;; \
	esac
	bash scripts/install-kserve.sh "$(CONTEXT)"

runtime: check-cluster ## publish the CPU mock runtime source in one cluster
	bash scripts/deploy-runtime.sh "$(CLUSTER)"

gateway: check-cluster ## reconcile one stack's Gateway API entry point
	bash scripts/deploy-gateway.sh "$(CLUSTER)"

kserve: check-cluster ## deploy the KServe LLMInferenceService path in one cluster
	bash scripts/deploy-kserve.sh "$(CONTEXT)"

pools: check-cluster ## add accelerator-class fixture pools to one cluster
	bash scripts/deploy-pools.sh --context "$(CONTEXT)"

pools-down: check-cluster ## remove fixture pools from one cluster
	bash scripts/deploy-pools.sh --delete --context "$(CONTEXT)"

policies: check-cluster ## add Keycloak and native gateway policies to one cluster
	bash scripts/deploy-policies.sh --context "$(CONTEXT)"

policies-down: check-cluster ## remove policies and Keycloak from one cluster
	bash scripts/deploy-policies.sh --delete --context "$(CONTEXT)"

semantic-router: check-cluster ## attach semantic routing to one cluster's chat rule
	bash scripts/deploy-semantic-router.sh --context "$(CONTEXT)"

semantic-router-down: check-cluster ## remove semantic routing from one cluster
	bash scripts/deploy-semantic-router.sh --delete --context "$(CONTEXT)"

keycloak: check-cluster ## install or refresh the Keycloak realm in one cluster
	bash scripts/install-keycloak.sh "$(CONTEXT)"

compare: check-cluster ## test one running cluster and write compare/results/<cluster>.json
	$(PYTHON) compare/run-gateway.py --cluster "$(CLUSTER)" --requests "$(or $(N),30)"

comparison-summary: ## merge saved per-cluster results into the README summary table
	$(PYTHON) compare/merge-results.py README.md

status: check-cluster ## show KServe, gateway, and policy state for one cluster
	bash scripts/cluster-status.sh "$(CLUSTER)"

start-cluster: check-cluster ## start one retained kind node and recover its data plane
	bash scripts/ensure-cluster.sh "$(CLUSTER)" --existing

stop-cluster: check-cluster ## stop one kind node without deleting cluster state
	docker stop -t 5 "$(CLUSTER)-control-plane"

stop-clusters: ## stop all retained comparison clusters without deleting them
	@for cluster in $(CLUSTERS); do docker stop -t 5 "$$cluster-control-plane" >/dev/null 2>&1 || true; done
	@echo "all comparison clusters stopped"

down: check-cluster ## delete one comparison cluster
	-kind delete cluster --name "$(CLUSTER)"

# Explicit all-cluster helpers are sequential and stop every node between
# stacks. They never require the three control planes to run concurrently.
up-all: ## build all clusters sequentially and leave all stopped
	@set -e; current=''; cleanup() { test -z "$$current" || $(MAKE) stop-cluster CLUSTER="$$current" >/dev/null 2>&1 || true; }; trap cleanup EXIT; \
	$(MAKE) stop-clusters; for cluster in $(CLUSTERS); do \
	  current="$$cluster"; \
	  $(MAKE) up CLUSTER="$$cluster"; \
	  $(MAKE) stop-cluster CLUSTER="$$cluster"; \
	  current=''; \
	done

policies-all: ## install feature policies sequentially and leave all stopped
	@set -e; current=''; cleanup() { test -z "$$current" || $(MAKE) stop-cluster CLUSTER="$$current" >/dev/null 2>&1 || true; }; trap cleanup EXIT; \
	$(MAKE) stop-clusters; for cluster in $(CLUSTERS); do \
	  current="$$cluster"; \
	  $(MAKE) start-cluster CLUSTER="$$cluster"; \
	  $(MAKE) policies CLUSTER="$$cluster"; \
	  $(MAKE) stop-cluster CLUSTER="$$cluster"; \
	  current=''; \
	done

semantic-router-all: ## install semantic routers sequentially and leave all stopped
	@set -e; current=''; cleanup() { test -z "$$current" || $(MAKE) stop-cluster CLUSTER="$$current" >/dev/null 2>&1 || true; }; trap cleanup EXIT; \
	$(MAKE) stop-clusters; for cluster in $(CLUSTERS); do \
	  current="$$cluster"; \
	  $(MAKE) start-cluster CLUSTER="$$cluster"; \
	  $(MAKE) semantic-router CLUSTER="$$cluster"; \
	  $(MAKE) stop-cluster CLUSTER="$$cluster"; \
	  current=''; \
	done

features-all: ## install policies and router per cluster in one sequential pass
	@set -e; current=''; cleanup() { test -z "$$current" || $(MAKE) stop-cluster CLUSTER="$$current" >/dev/null 2>&1 || true; }; trap cleanup EXIT; \
	$(MAKE) stop-clusters; for cluster in $(CLUSTERS); do \
	  current="$$cluster"; \
	  $(MAKE) start-cluster CLUSTER="$$cluster"; \
	  $(MAKE) policies CLUSTER="$$cluster"; \
	  $(MAKE) semantic-router CLUSTER="$$cluster"; \
	  $(MAKE) stop-cluster CLUSTER="$$cluster"; \
	  current=''; \
	done

compare-all: ## test clusters sequentially, merge README results, leave all stopped
	@set -e; current=''; cleanup() { test -z "$$current" || $(MAKE) stop-cluster CLUSTER="$$current" >/dev/null 2>&1 || true; }; trap cleanup EXIT; \
	$(MAKE) stop-clusters; for cluster in $(CLUSTERS); do \
	  current="$$cluster"; \
	  $(MAKE) start-cluster CLUSTER="$$cluster"; \
	  $(MAKE) compare CLUSTER="$$cluster" N="$(or $(N),30)"; \
	  $(MAKE) stop-cluster CLUSTER="$$cluster"; \
	  current=''; \
	done
	@$(MAKE) comparison-summary

down-all: ## delete all three comparison clusters
	@for cluster in $(CLUSTERS); do kind delete cluster --name "$$cluster" || true; done

agent-ui: ## expose the retained agentgateway UI at http://localhost:15000/ui
	@POD="$$(kubectl --context kind-$(AGENT_CLUSTER) -n ai-demo get pod \
	  -l gateway.networking.k8s.io/gateway-name=ai-gateway \
	  -o jsonpath='{.items[0].metadata.name}')"; \
	test -n "$$POD" || { echo "agentgateway proxy pod not found; start ai-gw-agent first" >&2; exit 1; }; \
	echo "agentgateway UI -> http://localhost:15000/ui (Ctrl-C to stop)"; \
	kubectl --context kind-$(AGENT_CLUSTER) -n ai-demo port-forward \
	  --address 127.0.0.1 "pod/$$POD" 15000:15000

charts: ## refresh all vendored dependency charts
	bash scripts/pull-charts.sh

test: ## test the multi-task mock runtime locally
	$(PYTHON) -m unittest discover -s mock-llm -p 'test_*.py' -v

validate: ## check manifests and router config offline
	$(PYTHON) scripts/validate-policies.py
	$(PYTHON) scripts/validate-router-config.py
	$(PYTHON) scripts/validate-tenant-model.py
	$(PYTHON) scripts/validate-tier-contract.py

vllm-production: ## deploy pinned vLLM services (set VLLM_CONTEXT)
	@test -n "$(VLLM_CONTEXT)" || { echo 'set VLLM_CONTEXT to a kubectl context' >&2; exit 2; }
	bash scripts/deploy-vllm-production.sh "$(VLLM_CONTEXT)"

vllm-production-down: ## remove production vLLM services from VLLM_CONTEXT
	@test -n "$(VLLM_CONTEXT)" || { echo 'set VLLM_CONTEXT to a kubectl context' >&2; exit 2; }
	bash scripts/deploy-vllm-production.sh "$(VLLM_CONTEXT)" --delete

vllm-validate: ## validate production vLLM APIs (set VLLM_BASE_URL)
	@test -n "$(VLLM_BASE_URL)" || { echo 'set VLLM_BASE_URL to the Gateway URL' >&2; exit 2; }
	$(PYTHON) scripts/validate-vllm-contract.py --base-url "$(VLLM_BASE_URL)" \
	  --check-auto $(if $(VLLM_TOKEN),--token "$(VLLM_TOKEN)",) \
	  --chat-model qwen3.8-27b \
	  --embedding-model qwen3-embedding-8b \
	  --rerank-model bge-reranker-v2-m3 \
	  --transcription-model whisper-large-v3
