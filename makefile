.PHONY: venv
venv:
	python -m venv venv && source venv/bin/activate && pip install -r requirements-dev.txt

.PHONY: test_migrations
test_migrations:
	python -m pypg_django_test.manage makemigrations

.PHONY: test_migrate
test_migrate:
	python -m pypg_django_test.manage migrate

.PHONY: schema
schema: test_migrations test_migrate

.PHONY: clean
clean:
	git clean -xfd

.PHONY: devenv
devenv: clean venv schema


