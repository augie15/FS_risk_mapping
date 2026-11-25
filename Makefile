.PHONY: setup run assess
PYTHON ?= python3.12
setup:
	$(PYTHON) -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@test -f .env || cp .env.example .env
run:
	.venv/bin/uvicorn backend.app:app --reload --port 8800
# make assess LAT=54.69 LNG=-127.05
assess:
	.venv/bin/python -m pipeline $(LAT) $(LNG)
