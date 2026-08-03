.PHONY: help install test smoke check-vendored sync

PY := .venv/bin/python
PIP := .venv/bin/pip

help:   ## 列出命令
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

install:  ## 建 venv + 装依赖
	python3.11 -m venv .venv
	$(PIP) install -r requirements.txt

test:  ## 单测
	$(PY) -m pytest tests/unit -q

smoke:  ## 冒烟: import + 场景唯一 + panel 解析 + 机械层
	$(PY) -m pytest tests/unit/test_smoke_strip.py -v

check-vendored:  ## 校验 vendored 引擎未被本地改动 (CI 门)
	$(PY) scripts/check_vendored.py

sync:  ## 从上游同步 vendored 引擎
	bash scripts/sync_from_upstream.sh
