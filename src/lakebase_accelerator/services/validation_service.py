"""Validation Service — Step 8 of both pipelines.

Validates schema, tables, seed data, and generated file completeness
before deployment. Catches issues early to avoid deployment failures.
"""

from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import REQUIRED_APP_FILES
from lakebase_accelerator.utils.exceptions.exceptions import ValidationError
from lakebase_accelerator.utils.logger import logger


class ValidationCheck:
    """Result of a single validation check."""

    def __init__(self, name: str, passed: bool, message: str) -> None:
        self.name = name
        self.passed = passed
        self.message = message


class ValidationService:
    """Validates generated app before deployment."""

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    async def execute(
        self,
        schema_name: str,
        table_names: list[str],
        all_files: dict[str, str],
    ) -> list[ValidationCheck]:
        """Run all validation checks.

        Args:
            schema_name: Lakebase schema to validate.
            table_names: Expected table names.
            all_files: Combined dict of all generated files.

        Returns:
            List of ValidationCheck results.

        Raises:
            ValidationError: If any check fails.
        """
        logger.info("Starting validation", extra={"step": "validation", "schema_name": schema_name})

        checks: list[ValidationCheck] = []

        # Check 1: Schema exists
        checks.append(self._check_schema_exists(schema_name))

        # Check 2: All tables exist
        for table_name in table_names:
            checks.append(self._check_table_exists(schema_name, table_name))

        # Check 3: Tables have seed data
        row_counts = self._repo.get_table_row_counts(schema_name, table_names)
        for table_name in table_names:
            count = row_counts.get(table_name, 0)
            checks.append(
                ValidationCheck(
                    name=f"seed_data_{table_name}",
                    passed=count > 0,
                    message=f"Table '{table_name}' has {count} rows"
                    if count > 0
                    else f"Table '{table_name}' has no seed data",
                )
            )

        # Check 4: Required files present
        for required_file in REQUIRED_APP_FILES:
            checks.append(
                ValidationCheck(
                    name=f"file_{required_file}",
                    passed=required_file in all_files,
                    message=f"File '{required_file}' present"
                    if required_file in all_files
                    else f"Missing file: {required_file}",
                )
            )

        # Report results
        failed = [c for c in checks if not c.passed]
        if failed:
            failed_names = [c.name for c in failed]
            logger.warning(
                f"Validation failed: {len(failed)} checks failed",
                extra={"step": "validation", "failed_checks": failed_names},
            )
            raise ValidationError(
                message=f"{len(failed)} validation checks failed: {', '.join(failed_names)}",
                failed_checks=failed_names,
            )

        logger.info(f"Validation passed: {len(checks)} checks", extra={"step": "validation"})
        return checks

    def _check_schema_exists(self, schema_name: str) -> ValidationCheck:
        """Check if the schema exists in Lakebase."""
        exists = self._repo.schema_exists(schema_name)
        return ValidationCheck(
            name="schema_exists",
            passed=exists,
            message=f"Schema '{schema_name}' exists" if exists else f"Schema '{schema_name}' not found",
        )

    def _check_table_exists(self, schema_name: str, table_name: str) -> ValidationCheck:
        """Check if a table exists in the schema."""
        exists = self._repo.table_exists(schema_name, table_name)
        return ValidationCheck(
            name=f"table_{table_name}",
            passed=exists,
            message=f"Table '{table_name}' exists" if exists else f"Table '{table_name}' not found",
        )
