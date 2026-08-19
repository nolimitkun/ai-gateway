SHELL := /bin/bash
.DEFAULT_GOAL := help
ENVOY_CLUSTER := ai-gw-envoy
AGENT_CLUSTER := ai-gw-agent

.PHONY: help up down clusters install mocks expose compare features status charts

help: ## show targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n",$$1,$$2}'

up: clusters install mocks expose ## build both clusters and deploy both gateways
	@echo
	@echo "Envoy AI Gateway -> http://localhost:8080"
	@echo "agentgateway     -> http://localhost:8081"
	@echo "run 'make compare' to test both"

clusters: ## create the two kind clusters
	kind create cluster --config clusters/kind-envoy-ai-gateway.yaml --wait 180s
	kind create cluster --config clusters/kind-agentgateway.yaml --wait 180s

install: ## install both gateway stacks from the vendored charts
	bash scripts/install-envoy-ai-gateway.sh
	bash scripts/install-agentgateway.sh

mocks: ## deploy the mock OpenAI upstreams into both clusters
	bash scripts/deploy-mock-llm.sh $(ENVOY_CLUSTER)
	bash scripts/deploy-mock-llm.sh $(AGENT_CLUSTER)

expose: ## apply routing manifests and pin gateways to nodePort 30080
	kubectl --context kind-$(ENVOY_CLUSTER) apply -f envoy-ai-gateway/manifests/
	kubectl --context kind-$(AGENT_CLUSTER) apply -f agentgateway/manifests/
	@sleep 20
	bash scripts/expose-gateway.sh $(ENVOY_CLUSTER) envoy-gateway-system ai-gateway
	bash scripts/expose-gateway.sh $(AGENT_CLUSTER) ai-demo ai-gateway

compare: ## run the functional suite against both gateways
	bash compare/run-comparison.sh

features: ## run the deeper feature probes (translation, auth, formats)
	bash compare/feature-matrix.sh

status: ## show pods in both clusters
	@echo "=== $(ENVOY_CLUSTER) ==="; kubectl --context kind-$(ENVOY_CLUSTER) get pods -A --no-headers | grep -vE 'kube-system|local-path'
	@echo "=== $(AGENT_CLUSTER) ==="; kubectl --context kind-$(AGENT_CLUSTER) get pods -A --no-headers | grep -vE 'kube-system|local-path'

charts: ## re-pull the helm charts and refresh the vendored default values
	bash scripts/pull-charts.sh

down: ## delete both clusters
	-kind delete cluster --name $(ENVOY_CLUSTER)
	-kind delete cluster --name $(AGENT_CLUSTER)
