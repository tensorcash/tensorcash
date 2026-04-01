#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest


class VllmSupervisor:
    def __init__(self):
        self.model_name = os.getenv("MODEL_NAME", "gpt2-large").strip()
        self.model_commit = os.getenv("MODEL_COMMIT", "").strip()
        self.max_model_len = os.getenv("MAX_MODEL_LEN", "10000").strip()
        self.device = os.getenv("DEVICE", "auto").strip()
        self.gpu_mem_util = os.getenv("GPU_MEM_UTIL", "0.8").strip()
        self.api_key = (os.getenv("API_KEY", "dev-secret") or "dev-secret").strip()
        self.download_dir = os.getenv("HF_HUB_CACHE", "/models/hub").strip() or "/models/hub"

        self.vllm_host = os.getenv("VLLM_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.vllm_port = int(os.getenv("VLLM_PORT", "8000"))

        self.control_host = os.getenv("VLLM_CONTROL_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.control_port = int(os.getenv("VLLM_CONTROL_PORT", "8001"))
        self.admin_key = (os.getenv("VLLM_ADMIN_KEY", "").strip() or self.api_key)
        self.switch_timeout_sec = int(os.getenv("VLLM_SWITCH_TIMEOUT_SEC", "120"))
        self.runtime_model_state_path = (
            os.getenv("MINER_RUNTIME_MODEL_STATE_PATH", "/data/miner_runtime_model_state.json").strip()
            or "/data/miner_runtime_model_state.json"
        )
        self.model_source = "env"

        self.proc = None
        self.lock = threading.RLock()
        self.stopping = False
        self._load_runtime_model_state()

    def _load_runtime_model_state(self) -> None:
        path = self.runtime_model_state_path
        try:
            if not path or not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return
            model_name = (state.get("model_name") or "").strip()
            model_commit = (state.get("model_commit") or "").strip()
            # Accept only fully pinned state to avoid partial corruption.
            if not model_name or not model_commit:
                return
            self.model_name = model_name
            self.model_commit = model_commit
            self.model_source = "persisted"
        except Exception:
            # Keep startup resilient; proxy will perform startup sync as fallback.
            return

    def _save_runtime_model_state(self) -> None:
        path = self.runtime_model_state_path
        if not path:
            return
        payload = {
            "model_name": self.model_name,
            "model_commit": self.model_commit,
            "source": "runtime",
            "updated_at": int(time.time()),
        }
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
            os.replace(tmp, path)
        except Exception:
            # Non-fatal persistence failure.
            return

    def _server_args(self, model_name: str, model_commit: str) -> list[str]:
        args = [
            "vllm",
            "serve",
            model_name,
            "--trust-remote-code",
            "--tensor-parallel-size",
            "1",
            "--max-num-seqs",
            "32",
            "--host",
            self.vllm_host,
            "--port",
            str(self.vllm_port),
            "--api-key",
            self.api_key,
            "--download-dir",
            self.download_dir,
            "--load-format",
            "safetensors",
            "--max-model-len",
            self.max_model_len,
            "--enable-prompt-tokens-details",
            "--generation-config",
            "vllm",
        ]
        if model_commit:
            args += ["--revision", model_commit]
        if self.device and self.device != "cpu":
            args += ["--gpu-memory-utilization", self.gpu_mem_util]
        return args

    def _wait_backend_ready(self, timeout_sec: int) -> bool:
        deadline = time.time() + timeout_sec
        url = f"http://127.0.0.1:{self.vllm_port}/health"
        while time.time() < deadline:
            try:
                req = urlrequest.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
                with urlrequest.urlopen(req, timeout=2) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def start_backend(self, model_name: str, model_commit: str) -> None:
        args = self._server_args(model_name, model_commit)
        self.proc = subprocess.Popen(args)
        if not self._wait_backend_ready(self.switch_timeout_sec):
            self.stop_backend(force=True)
            raise RuntimeError("vllm backend did not become healthy in time")

    def stop_backend(self, force: bool = False) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=20)
        except Exception:
            if force:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        finally:
            self.proc = None

    def switch_model(self, model_name: str, model_commit: str) -> dict:
        model_name = (model_name or "").strip()
        model_commit = (model_commit or "").strip()
        if not model_name:
            raise RuntimeError("model_name is required")

        with self.lock:
            old_name = self.model_name
            old_commit = self.model_commit
            if old_name == model_name and old_commit == model_commit:
                return {"ok": True, "switched": False, "active_model": self.active_model()}

            self.stop_backend(force=True)
            try:
                self.start_backend(model_name, model_commit)
            except Exception as e:
                try:
                    self.start_backend(old_name, old_commit)
                    self.model_name = old_name
                    self.model_commit = old_commit
                except Exception:
                    pass
                raise RuntimeError(f"failed to switch backend model: {e}")

            self.model_name = model_name
            self.model_commit = model_commit
            self.model_source = "runtime"
            self._save_runtime_model_state()
            return {"ok": True, "switched": True, "active_model": self.active_model()}

    def active_model(self) -> dict:
        return {
            "model_name": self.model_name,
            "model_commit": self.model_commit,
            "backend": "vllm",
            "device": self.device,
            "source": self.model_source,
        }

    def health(self) -> dict:
        alive = self.proc is not None and self.proc.poll() is None
        return {
            "status": "healthy" if alive else "degraded",
            "backend_up": alive,
            "active_model": self.active_model(),
        }


SUP = VllmSupervisor()


class Handler(BaseHTTPRequestHandler):
    server_version = "vllm-supervisor/1.0"

    def _auth_ok(self) -> bool:
        if not SUP.admin_key:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {SUP.admin_key}"

    def _write_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, SUP.health())
            return
        if self.path == "/admin/active-model":
            if not self._auth_ok():
                self._write_json(401, {"error": "unauthorized"})
                return
            self._write_json(200, SUP.health())
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/admin/switch-model":
            self._write_json(404, {"error": "not_found"})
            return
        if not self._auth_ok():
            self._write_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            result = SUP.switch_model(
                str(payload.get("model_name", "") or ""),
                str(payload.get("model_commit", "") or ""),
            )
            self._write_json(200, result)
        except Exception as e:
            self._write_json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        pass


def _shutdown(*_):
    SUP.stopping = True
    SUP.stop_backend(force=True)
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    SUP.start_backend(SUP.model_name, SUP.model_commit)
    httpd = ThreadingHTTPServer((SUP.control_host, SUP.control_port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
