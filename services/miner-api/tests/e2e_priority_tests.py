"""E2E tests for priority mode (dummy abortion under load)"""
import os
import time
import json
import requests
import pytest
import concurrent.futures

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8081")


def _get_status():
    r = requests.get(f"{PROXY_URL}/status", timeout=5)
    r.raise_for_status()
    return r.json()


class TestPriorityModeE2E:
    @pytest.fixture(autouse=True)
    def verify_priority_enabled(self):
        # Wait until status endpoint is up
        deadline = time.time() + 30
        last_exc = None
        while time.time() < deadline:
            try:
                data = _get_status()
                # Must include priority block when enabled
                if "priority" in data.get("proxy", {}) or "priority" in data:
                    return
                # Some implementations attach priority under top-level or proxy
            except Exception as e:
                last_exc = e
            time.sleep(1)
        raise RuntimeError(f"Priority mode not detected in /status: {last_exc}")

    def test_dummy_abortion_under_burst(self):
        # Snapshot initial aborted count
        data = _get_status()
        priority = data.get("priority") or data.get("proxy", {}).get("priority") or {}
        start_aborted = int(priority.get("total_aborted", 0))

        # Fire a burst of concurrent external requests to exceed capacity
        def make_request(i):
            req = {
                "model": "Qwen/Qwen3-8B",
                "prompt": f"Priority burst {i}",
                "max_tokens": 10
            }
            try:
                resp = requests.post(f"{PROXY_URL}/v1/completions", json=req, timeout=15)
                return resp.status_code
            except Exception:
                return 0

        # Use a burst larger than (max - min + 1) to trigger abortion
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            futures = [ex.submit(make_request, i) for i in range(12)]
            _ = [f.result(timeout=20) for f in futures]

        # Poll status for a short window to observe abortion count increase
        new_aborted = start_aborted
        deadline = time.time() + 10
        while time.time() < deadline:
            s = _get_status()
            pr = s.get("priority") or s.get("proxy", {}).get("priority") or {}
            new_aborted = int(pr.get("total_aborted", 0))
            if new_aborted > start_aborted:
                break
            time.sleep(0.5)

        assert new_aborted > start_aborted, (
            f"Expected total_aborted to increase (start={start_aborted}, got={new_aborted})"
        )

