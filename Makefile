SHELL := /bin/bash
.DEFAULT_GOAL := help

KUADRANT_CLUSTER := ai-gw-kuadrant
ENVOY_CLUSTER := ai-gw-envoy
AGENT_CLUSTER := ai-gw-agent
CLUSTERS := $(KUADRANT_CLUSTER) $(ENVOY_CLUSTER) $(AGENT_CLUSTER)

.PHONY: help up down clusters install runtime gateways kserve pools pools-down policies policies-down semantic-router semantic-router-down keycloak compare status charts agent-ui test validate vllm-production vllm-production-down vllm-validate

help: ## show targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n",$$1,$$2}'

up: clusters install runtime gateways kserve ## build all three KServe gateway environments
	@echo
	@echo "Kuadrant         -> http://localhost:8082"
	@echo "Envoy AI Gateway -> http://localhost:8080"
	@echo "agentgateway     -> http://localhost:8081"
	@echo "run 'make compare'"

clusters: ## create one kind cluster per gateway stack
	kind create cluster --config clusters/kind-kuadrant.yaml --wait 180s
	kind create cluster --config clusters/kind-envoy-ai-gateway.yaml --wait 180s
	kind create cluster --config clusters/kind-agentgateway.yaml --wait 180s

install: ## install OpenShift-like Kuadrant, Envoy AI Gateway, agentgateway, and KServe
	bash scripts/install-kuadrant.sh
	bash scripts/install-envoy-ai-gateway.sh
	bash scripts/install-agentgateway.sh
	bash scripts/install-kserve.sh

runtime: ## publish the shared CPU mock runtime source in all clusters
	@for cluster in $(CLUSTERS); do bash scripts/deploy-runtime.sh "$$cluster"; done

gateways: ## create the three Gateway API entry points
	kubectl --context kind-$(KUADRANT_CLUSTER) apply -f kuadrant/manifests/gateway.yaml
	kubectl --context kind-$(KUADRANT_CLUSTER) apply -f kuadrant/manifests/policy.yaml
	kubectl --context kind-$(ENVOY_CLUSTER) apply -f envoy-ai-gateway/manifests/gateway.yaml
	kubectl --context kind-$(AGENT_CLUSTER) apply -f agentgateway/manifests/gateway.yaml
	@sleep 15
	bash scripts/expose-gateway.sh $(KUADRANT_CLUSTER) openshift-ingress openshift-ai-inference
	bash scripts/expose-gateway.sh $(AGENT_CLUSTER) ai-demo ai-gateway

kserve: ## deploy the same KServe LLMInferenceService into all three clusters
	bash scripts/deploy-kserve.sh

pools: ## add one KServe serving pool per accelerator class (B300/H200/H100/L40S)
	bash scripts/deploy-pools.sh

pools-down: ## remove the accelerator pools, keeping the shared KServe path
	bash scripts/deploy-pools.sh --delete

semantic-router: ## route "auto" requests by intent through each gateway's ext_proc API
	bash scripts/deploy-semantic-router.sh

semantic-router-down: ## remove the semantic router; the KServe path is unchanged
	bash scripts/deploy-semantic-router.sh --delete

policies: ## add Keycloak auth, authorization, rate limit, quota, and token policies
	bash scripts/deploy-policies.sh

policies-down: ## remove the gateway feature policies and Keycloak
	bash scripts/deploy-policies.sh --delete

keycloak: ## install only the shared Keycloak realm in all three clusters
	bash scripts/install-keycloak.sh

vllm-production: ## deploy pinned vLLM services (set VLLM_CONTEXT to a GPU cluster)
	@test -n "$(VLLM_CONTEXT)" || { echo 'set VLLM_CONTEXT to a kubectl context' >&2; exit 2; }
	bash scripts/deploy-vllm-production.sh "$(VLLM_CONTEXT)"

vllm-production-down: ## remove production vLLM services from VLLM_CONTEXT
	@test -n "$(VLLM_CONTEXT)" || { echo 'set VLLM_CONTEXT to a kubectl context' >&2; exit 2; }
	bash scripts/deploy-vllm-production.sh "$(VLLM_CONTEXT)" --delete

vllm-validate: ## validate production vLLM APIs (set VLLM_BASE_URL)
	@test -n "$(VLLM_BASE_URL)" || { echo 'set VLLM_BASE_URL to the Gateway URL' >&2; exit 2; }
	python3 scripts/validate-vllm-contract.py --base-url "$(VLLM_BASE_URL)" \
		--routing-header x-model-service --chat-model qwen3-8b \
		--embedding-model qwen3-embedding-8b \
		--rerank-model bge-reranker-v2-m3 \
		--transcription-model whisper-large-v3-turbo

compare: ## compare all gateways through the single KServe path
	bash compare/run-comparison.sh

test: ## test the multi-task mock runtime locally
	python3 -m unittest discover -s mock-llm -p 'test_*.py' -v

validate: ## check every manifest against the vendored CRD schemas, offline
	python3 scripts/validate-policies.py
	python3 scripts/validate-router-config.py

agent-ui: ## expose agentgateway UI at http://localhost:15000/ui
	@POD="$$(kubectl --context kind-$(AGENT_CLUSTER) -n ai-demo get pod \
		-l gateway.networking.k8s.io/gateway-name=ai-gateway \
		-o jsonpath='{.items[0].metadata.name}')"; \
		test -n "$$POD" || { echo "agentgateway proxy pod not found; run 'make up' first" >&2; exit 1; }; \
		echo "agentgateway UI -> http://localhost:15000/ui (Ctrl-C to stop)"; \
		kubectl --context kind-$(AGENT_CLUSTER) -n ai-demo port-forward \
			--address 127.0.0.1 "pod/$$POD" 15000:15000

status: ## show KServe and gateway readiness in all clusters
	@echo "=== $(KUADRANT_CLUSTER) ==="
	@kubectl --context kind-$(KUADRANT_CLUSTER) get gateway -n openshift-ingress
	@kubectl --context kind-$(KUADRANT_CLUSTER) get httproute,llminferenceservice,inferencepool -n ai-demo
	@for cluster in $(CLUSTERS); do \
		if [ "$$cluster" = "$(KUADRANT_CLUSTER)" ]; then continue; fi; \
		echo "=== $$cluster ==="; \
		kubectl --context "kind-$$cluster" get gateway,httproute,llminferenceservice,inferencepool -n ai-demo; \
	done

charts: ## refresh vendored gateway, Kuadrant, and KServe charts
	bash scripts/pull-charts.sh

down: ## delete all three kind clusters
	-kind delete cluster --name $(KUADRANT_CLUSTER)
	-kind delete cluster --name $(ENVOY_CLUSTER)
	-kind delete cluster --name $(AGENT_CLUSTER)
