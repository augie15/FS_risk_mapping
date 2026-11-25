.PHONY: setup run
setup:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@test -f .env || cp .env.example .env
run:
	.venv/bin/uvicorn backend.app:app --reload --port 8800
