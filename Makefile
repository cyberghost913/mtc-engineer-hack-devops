SHELL := /bin/bash
PYTHON ?= python3
PROMTOOL ?= promtool
VENV ?= .venv
INVENTORY ?= inventory.ini
EXTRA_ARGS ?=
export ANSIBLE_CONFIG := $(CURDIR)/ansible.cfg
export ANSIBLE_HOME := $(CURDIR)/.ansible

.PHONY: help setup doctor bootstrap deploy deploy-gateway verify verify-app verify-gateway verify-all deploy-monitoring verify-monitoring deploy-logging verify-logging lint-prometheus lint test
help:
	@echo 'setup      Install local Ansible tools in .venv'
	@echo 'doctor     Check the target VM without changing its configuration'
	@echo 'bootstrap  Prepare Ubuntu and create the single-node cluster'
	@echo 'deploy     Deploy Nginx, Gateway API, monitoring and logging; verify each stage'
	@echo 'deploy-gateway  Add Gateway API to an existing application'
	@echo 'deploy-monitoring Add Prometheus and exporters to an existing Gateway installation'
	@echo 'deploy-logging Add Fluentd and persistent application log archives'
	@echo 'verify-logging Confirm fresh Gateway access/error records in Fluentd output'
	@echo 'verify-monitoring Check scrape targets, metric samples, rules and persistent storage'
	@echo 'verify     Check API, node, Calico, DNS and Pod/Service networking'
	@echo 'verify-app Check the deployed Nginx application'
	@echo 'verify-gateway Check NodePort, HTTPRoute and correlated application logs'
	@echo 'verify-all Check foundation, application, Gateway, monitoring and logging'
	@echo 'lint/test  Validate source files locally; no VM required'
	@echo 'lint-prometheus Validate configuration and alert scenarios with promtool 3.13.3'
	@echo 'Use INVENTORY=inventory.local.ini when running inside Ubuntu VM'
setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -r requirements.lock
doctor:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/doctor.yml $(EXTRA_ARGS)
bootstrap:
	$(VENV)/bin/python scripts/check_vendor.py
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/bootstrap.yml $(EXTRA_ARGS)
verify:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/verify.yml $(EXTRA_ARGS)
deploy:
	$(VENV)/bin/python scripts/check_vendor.py
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/deploy.yml $(EXTRA_ARGS)
deploy-gateway:
	$(VENV)/bin/python scripts/check_vendor.py
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/gateway.yml $(EXTRA_ARGS)
verify-app:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/verify-app.yml $(EXTRA_ARGS)
verify-gateway:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/verify-gateway.yml $(EXTRA_ARGS)
deploy-monitoring:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/monitoring.yml $(EXTRA_ARGS)
verify-monitoring:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/verify-monitoring.yml $(EXTRA_ARGS)
deploy-logging:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/logging.yml $(EXTRA_ARGS)
verify-logging:
	$(VENV)/bin/ansible-playbook -i $(INVENTORY) ansible/verify-logging.yml $(EXTRA_ARGS)
verify-all:
	$(MAKE) verify
	$(MAKE) verify-app
	$(MAKE) verify-gateway
	$(MAKE) verify-monitoring
	$(MAKE) verify-logging
lint:
	$(VENV)/bin/yamllint ansible versions.yml config.example.yml tests/prometheus-rules.yml .yamllint.yml
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/bootstrap.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/doctor.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/verify.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/deploy.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/verify-app.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/gateway.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/verify-gateway.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/monitoring.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/verify-monitoring.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/logging.yml --syntax-check
	$(VENV)/bin/ansible-playbook -i inventory.local.ini ansible/verify-logging.yml --syntax-check
	$(VENV)/bin/python scripts/check_vendor.py
	bash -n scripts/verify-cluster.sh
test:
	$(VENV)/bin/python -m unittest discover -s tests -v

lint-prometheus:
	$(PROMTOOL) check config --syntax-only ansible/roles/monitoring/files/prometheus.yml
	$(PROMTOOL) check rules ansible/roles/monitoring/files/rules.yml
	$(PROMTOOL) test rules tests/prometheus-rules.yml
