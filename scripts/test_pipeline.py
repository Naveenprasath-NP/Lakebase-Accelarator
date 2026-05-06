"""Test the full pipeline end-to-end by calling the execute endpoint."""

import json
import time
import httpx

print("=" * 60)
print("  TESTING FULL PIPELINE (greenfield)")
print("=" * 60)

start_time = time.time()

try:
    with httpx.stream(
        "POST",
        "http://localhost:8000/api/v1/projects/execute",
        json={
            "type": "greenfield",
            "prompt": "Build an expense tracker app where employees can submit expenses with a category, amount, currency, receipt description, and approval status. Managers can approve or reject expenses.",
            "project_name": "expense-tracker",
        },
        timeout=600.0,
    ) as response:
        print(f"\nStatus: {response.status_code}")
        print(f"Streaming SSE events...\n")

        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                event_type = line.replace("event:", "").strip()
            elif line.startswith("data:"):
                data_str = line.replace("data:", "").strip()
                try:
                    data = json.loads(data_str)
                    step = data.get("step", "")
                    status = data.get("status", "")
                    message = data.get("message", "")
                    elapsed = time.time() - start_time

                    if event_type == "pipeline_complete":
                        print(f"\n{'='*60}")
                        print(f"  PIPELINE {status.upper()} ({elapsed:.1f}s)")
                        if data.get("data", {}).get("app_url"):
                            print(f"  APP URL: {data['data']['app_url']}")
                        if data.get("data", {}).get("error_code"):
                            print(f"  ERROR: {message}")
                        print(f"{'='*60}")
                    else:
                        icon = "✓" if status == "completed" else "✗" if status == "failed" else "?"
                        print(f"  [{elapsed:5.1f}s] {icon} {step}: {message[:100]}")
                except json.JSONDecodeError:
                    print(f"  RAW: {data_str[:100]}")

except httpx.ReadTimeout:
    elapsed = time.time() - start_time
    print(f"\n  TIMEOUT after {elapsed:.1f}s")
except Exception as e:
    print(f"\n  ERROR: {e}")

print(f"\nTotal time: {time.time() - start_time:.1f}s")
