.DEFAULT_GOAL := help
RUN := uv run --env-file .env
.PHONY: help up down reset db-check test lint eval baseline demo run vapi-config

.env:
	cp .env.example .env

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

up: .env  ## start Postgres and wait until it's healthy
	docker compose up -d --wait db

down:  ## stop containers (data kept)
	docker compose down

reset:  ## DELETE the local database volume, then start fresh (dev data only)
	docker compose down -v
	$(MAKE) up

db-check:  ## confirm btree_gist and the double-booking constraint exist
	@docker compose exec -T db psql -U app -d app -tAc \
	  "SELECT count(*) FROM pg_constraint WHERE conrelid = 'bookings'::regclass AND contype = 'x'" \
	  | grep -qx 1 && echo "db OK: bookings has its exclusion constraint" \
	  || (echo "db check FAILED"; exit 1)

test: .env  ## unit tests; DB tests run only while `make up` is running (they wipe the booking tables)
	$(RUN) pytest -q

lint:  ## ruff
	uv run ruff check .

eval: .env  ## call scenarios (-> evals/results.md) + concurrency baseline (-> evals/concurrency.md)
	$(RUN) python -m evals.run --start-server
	$(RUN) python -m evals.concurrency --runs 10

baseline: .env  ## concurrency test only: exclusion constraint vs check-then-insert
	$(RUN) python -m evals.concurrency --runs 10

demo: .env  ## start the app, replay every scenario as a call trace with PASS/FAIL, stop the app
	$(RUN) python -m evals.run --start-server --trace

run: .env  ## API on http://localhost:8004 with auto-reload
	$(RUN) uvicorn app.main:app --reload --port 8004

vapi-config:  ## regenerate vapi/tools.json and vapi/assistant.json from app/vapi.py
	uv run python -m app.vapi
