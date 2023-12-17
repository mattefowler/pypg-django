.PHONY: devenv
devenv:
	rm -rf venv; python -m venv venv && source venv/bin/activate && pip install -r requirements-dev.txt

.PHONY: test_migrations
test_migrations:
	DJANGO_SETTINGS_MODULE=pypg_django_test.test_project.settings
	python -m pypg_django_test.manage makemigrations

.PHONY: test_migrate
test_migrate:
	DJANGO_SETTINGS_MODULE=pypg_django_test.test_project.settings
	python -m pypg_django_test.manage migrate

