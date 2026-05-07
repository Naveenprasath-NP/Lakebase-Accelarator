"""Robust JSON parser that handles truncated or malformed LLM responses.

LLMs often return JSON that is:
- Wrapped in markdown code fences
- Truncated mid-string (hit max_tokens)
- Missing closing brackets/braces
- Has trailing commas

This module provides a best-effort repair before parsing.
"""

import json
import re

from lakebase_accelerator.utils.logger import logger


def parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response with repair for common issues.

    Handles:
    - Markdown code fences (```json ... ```)
    - Truncated strings (unterminated quotes)
    - Missing closing brackets/braces
    - Trailing commas before ] or }

    Args:
        text: Raw LLM response text.

    Returns:
        Parsed dict. Falls back to empty dict with entities=[] on total failure.

    Raises:
        ValueError: If JSON cannot be repaired and parsed.
    """
    text = _strip_markdown_fences(text)

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Attempt repair
    repaired = _repair_json(text)
    try:
        result = json.loads(repaired)
        if isinstance(result, dict):
            logger.debug("JSON parsed after repair")
            return result
    except json.JSONDecodeError:
        pass

    # Last resort: try to extract the largest valid JSON object
    result = _extract_partial_json(text)
    if result is not None:
        logger.warning("JSON parsed via partial extraction")
        return result

    raise ValueError(f"Cannot parse JSON even after repair. First 200 chars: {text[:200]}")


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from the response."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _repair_json(text: str) -> str:
    """Attempt to repair common JSON issues from truncated LLM output."""
    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Count open/close brackets to detect truncation
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    in_string = False
    escape_next = False

    # Check if we're inside an unterminated string
    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string

    # If we're inside a string, close it
    if in_string:
        text += '"'

    # Remove any trailing partial key-value after the last complete value
    # e.g., ..."field": "unterminated → close it
    if in_string:
        # Try to find a good truncation point
        # Look for the last complete JSON value
        last_complete = _find_last_complete_value(text)
        if last_complete > 0:
            text = text[:last_complete]
            # Recount after truncation
            open_braces = text.count("{") - text.count("}")
            open_brackets = text.count("[") - text.count("]")

    # Remove trailing commas again after potential truncation
    text = re.sub(r",\s*$", "", text)

    # Close open brackets and braces
    text += "]" * open_brackets
    text += "}" * open_braces

    return text


def _find_last_complete_value(text: str) -> int:
    """Find the position after the last complete JSON value (string, number, bool, null, }, ])."""
    # Look for the last occurrence of a complete value ending
    # These patterns indicate a complete value just ended
    patterns = [
        r'"[^"\\]*(?:\\.[^"\\]*)*"\s*',  # complete string
        r'\d+\.?\d*\s*',  # number
        r'true\s*',
        r'false\s*',
        r'null\s*',
        r'}\s*',
        r']\s*',
    ]

    last_pos = 0
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            end = match.end()
            if end > last_pos:
                last_pos = end

    return last_pos


def _extract_partial_json(text: str) -> dict | None:
    """Try to extract a valid JSON object from the beginning of the text.

    Progressively removes characters from the end until we get valid JSON.
    """
    # Find the first { to start
    start = text.find("{")
    if start == -1:
        return None

    text = text[start:]

    # Try progressively shorter substrings
    for end_offset in range(0, min(len(text), 500), 10):
        candidate = text[: len(text) - end_offset] if end_offset > 0 else text

        # Try to close it
        repaired = _repair_json(candidate)
        try:
            result = json.loads(repaired)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    return None
