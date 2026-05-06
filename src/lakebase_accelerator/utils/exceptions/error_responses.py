"""Pydantic models for standardized error responses.

These models provide a consistent structure for API error payloads
across all endpoints in the Lakebase Accelerator.
"""

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Individual error detail containing code, message, and optional context.

    Attributes:
        code: Machine-readable error code from error_codes module.
        message: Human-readable error description.
        details: Optional dictionary with additional context about the error.
    """

    code: str
    """Machine-readable error code from error_codes module."""

    message: str
    """Human-readable error description."""

    details: dict | None = None
    """Optional dictionary with additional context about the error."""


class ErrorResponse(BaseModel):
    """Standardized error response envelope for all API errors.

    Attributes:
        status: Always "error" for error responses.
        error: Detailed error information.
    """

    status: str = "error"
    """Always 'error' for error responses."""

    error: ErrorDetail
    """Detailed error information."""
