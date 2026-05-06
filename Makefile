.PHONY: setup run test test-cov lint lint-fix format check clean help

## setup: Install development dependencies with uv
setup:
	@echo "Installing dependencies..."
	uv sync --all-extras

## run: Start the application
run:
	@echo "Starting Lakebase Accelerator..."
	uv run uvicorn lakebase_accelerator.main:app --host 0.0.0.0 --port 8000 --reload

## test: Run pytest test suite
test:
	@echo "Running tests..."
	uv run pytest -v

## test-cov: Run tests with coverage report
test-cov:
	@echo "Running tests with coverage..."
	uv run coverage run -m pytest
	uv run coverage report
	uv run coverage xml
	uv run diff-cover coverage.xml

## lint: Run Ruff linter
lint:
	@echo "Linting..."
	uv run ruff check src tests

## lint-fix: Auto-fix linting issues
lint-fix:
	@echo "Fixing lint issues..."
	uv run ruff check --fix src tests

## format: Format code with Ruff
format:
	@echo "Formatting..."
	uv run ruff format src tests

## check: Run lint and format checks together
check:
	@echo "Checking code quality..."
	uv run ruff check src tests
	uv run ruff format --check src tests

## clean: Remove cache files
clean:
	@echo "Cleaning..."
	rm -rf __pycache__ .pytest_cache .ruff_cache .mypy_cache htmlcov
	rm -f coverage.xml .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

## help: List all available commands
help:
	@echo "Available commands:"
	@grep -E '^## ' Makefile | sed 's/## /  /'
