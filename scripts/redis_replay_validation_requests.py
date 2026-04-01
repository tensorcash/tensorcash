#!/usr/bin/env python3
"""Submit ValidationRequest FlatBuffers directly to a GPU worker Redis stream."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import urllib.request
import uuid
from pathlib import Path


def current_worker_id(health_url: str) -> str:
    with urllib.request.urlopen(health_url, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    worker_id = data.get("worker_id")
    if not worker_id:
        raise RuntimeError(f"health response missing worker_id: {data!r}")
    return worker_id


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requests", nargs="*", help="ValidationRequest files")
    parser.add_argument("--worker-id")
    parser.add_argument("--health-url", default="http://127.0.0.1:8080/health")
    parser.add_argument("--job-type", default="full")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--poll-interval-sec", type=float, default=5.0)
    parser.add_argument("--poll-job-id", action="append", default=[])
    parser.add_argument("--redis-url-env", default="REDIS_URL")
    parser.add_argument("--prefix", default="manual-red-replay")
    args = parser.parse_args()

    try:
        import redis.asyncio as redis
    except Exception:
        import aioredis as redis  # type: ignore

    redis_url = os.environ.get(args.redis_url_env)
    if not redis_url:
        raise RuntimeError(f"{args.redis_url_env} is not set")

    worker_id = args.worker_id or current_worker_id(args.health_url)
    stream_key = f"verify:jobs:{worker_id}:{args.job_type}"

    client = redis.from_url(redis_url, decode_responses=True)
    async def poll_result(job_id: str) -> tuple[bool, dict]:
        result_key = f"verify:results:{job_id}"
        deadline = time.monotonic() + args.timeout_sec
        while time.monotonic() < deadline:
            values = await client.lrange(result_key, 0, 0)
            if values:
                try:
                    payload_json = json.loads(values[0])
                except Exception:
                    payload_json = {"raw": values[0]}
                print(
                    json.dumps(
                        {
                            "event": "result",
                            "job_id": job_id,
                            "result_key": result_key,
                            "result": payload_json,
                        }
                    ),
                    flush=True,
                )
                return True, payload_json
            await asyncio.sleep(args.poll_interval_sec)

        print(
            json.dumps(
                {
                    "event": "timeout",
                    "job_id": job_id,
                    "result_key": result_key,
                    "timeout_sec": args.timeout_sec,
                }
            ),
            flush=True,
        )
        return False, {}

    try:
        for job_id in args.poll_job_id:
            ok, _ = await poll_result(job_id)
            if not ok:
                return 2

        for request_path in args.requests:
            payload = Path(request_path).read_bytes()
            stem = Path(request_path).stem.replace(".", "-")
            job_id = f"{args.prefix}-{stem}-{uuid.uuid4().hex[:8]}"
            fields = {
                "job_id": job_id,
                "type": args.job_type,
                "payload": base64.b64encode(payload).decode("ascii"),
                "submitted_by": "codex-red-replay",
                "submitted_at": str(time.time()),
            }
            msg_id = await client.xadd(stream_key, fields)
            print(
                json.dumps(
                    {
                        "event": "sent",
                        "stream": stream_key,
                        "message_id": msg_id,
                        "job_id": job_id,
                        "file": request_path,
                        "bytes": len(payload),
                    }
                ),
                flush=True,
            )

            ok, _ = await poll_result(job_id)
            if not ok:
                return 2
    finally:
        await client.aclose()

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
