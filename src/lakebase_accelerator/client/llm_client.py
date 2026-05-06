"""LLM client for Databricks Model Serving via direct HTTP calls.

Calls the Model Serving endpoint directly using httpx — no Databricks SDK dependency.
Handles OAuth token generation, structured output parsing, and retry logic.
"""

import asyncio
import json

import httpx
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from lakebase_accelerator.utils.exceptions import LLMClientError
from lakebase_accelerator.utils.logger import logger


class LLMClient:
    """Calls Databricks Model Serving endpoint via HTTP.

    Authentication: Uses SP OAuth (client_id + client_secret → workspace token).
    Retry: Up to max_retries on transient errors or malformed JSON.
    Structured output: Validates LLM responses against Pydantic models.
    """

    def __init__(
        self,
        databricks_host: str,
        client_id: str,
        client_secret: str,
        endpoint_name: str,
        max_retries: int = 2,
        timeout_seconds: int = 120,
    ) -> None:
        self._databricks_host = databricks_host.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._endpoint_name = endpoint_name
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self._cached_token: str | None = None

    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict | BaseModel:
        """Send a prompt to Model Serving and return parsed response.

        Args:
            system_prompt: System prompt for the LLM.
            user_prompt: User prompt for the LLM.
            response_model: Optional Pydantic model to validate response against.
            temperature: LLM temperature setting.
            max_tokens: Maximum tokens for the response.

        Returns:
            Parsed dict or validated Pydantic model instance.

        Raises:
            LLMClientError: If all retries are exhausted.
        """
        last_error: str | None = None
        adjusted_prompt = user_prompt

        for attempt in range(self._max_retries + 1):
            try:
                # Add validation error context on retries
                if last_error and attempt > 0:
                    adjusted_prompt = (
                        f"{user_prompt}\n\n"
                        f"Previous response had an error: {last_error}. "
                        f"Please fix and return valid JSON."
                    )

                raw_response = await self._invoke_endpoint(
                    system_prompt=system_prompt,
                    user_prompt=adjusted_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                # Parse JSON from response
                parsed = self._extract_json(raw_response)

                # Validate with Pydantic model if provided
                if response_model is not None:
                    return response_model.model_validate(parsed)
                return parsed

            except PydanticValidationError as e:
                last_error = str(e)
                logger.warning(
                    f"LLM response validation failed (attempt {attempt + 1}/{self._max_retries + 1})",
                    extra={"error": last_error[:200], "endpoint": self._endpoint_name},
                )

            except (json.JSONDecodeError, ValueError) as e:
                last_error = str(e)
                logger.warning(
                    f"LLM response parse error (attempt {attempt + 1}/{self._max_retries + 1})",
                    extra={"error": last_error[:200], "endpoint": self._endpoint_name},
                )

            except Exception as e:
                last_error = str(e)
                if attempt < self._max_retries:
                    backoff = 2**attempt
                    logger.warning(
                        f"LLM transient error (attempt {attempt + 1}), backing off {backoff}s",
                        extra={"error": last_error[:200], "endpoint": self._endpoint_name},
                    )
                    await asyncio.sleep(backoff)
                else:
                    break

        raise LLMClientError(
            f"LLM call failed after {self._max_retries + 1} attempts. "
            f"Endpoint: {self._endpoint_name}. Last error: {last_error}"
        )

    async def check_readiness(self) -> bool:
        """Check if the Model Serving endpoint is reachable."""
        try:
            token = await self._get_workspace_token()
            url = f"{self._databricks_host}/api/2.0/serving-endpoints/{self._endpoint_name}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                state = resp.json().get("state", {}).get("ready", "")
                return state == "READY"
            return False
        except Exception as e:
            logger.error(f"Failed to check endpoint readiness: {e}")
            return False

    async def _invoke_endpoint(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the Model Serving endpoint via HTTP and return raw response text."""
        token = await self._get_workspace_token()

        url = f"{self._databricks_host}/serving-endpoints/{self._endpoint_name}/invocations"

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Model Serving returned {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        # Standard OpenAI-compatible response format
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"Model Serving returned no choices: {data}")

        return choices[0].get("message", {}).get("content", "")

    async def _get_workspace_token(self) -> str:
        """Get workspace OAuth token (cached until we get a 401)."""
        if self._cached_token:
            return self._cached_token

        token_url = f"{self._databricks_host}/oidc/v1/token"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                auth=(self._client_id, self._client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise RuntimeError(f"Failed to get workspace token: {response.status_code} — {response.text[:200]}")

        self._cached_token = response.json()["access_token"]
        return self._cached_token

    def _extract_json(self, raw_response: str) -> dict:
        """Extract JSON from LLM response text.

        Handles markdown code block wrappers.
        """
        text = raw_response.strip()

        # Remove markdown code block wrappers if present
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        text = text.strip()
        return json.loads(text)
