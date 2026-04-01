#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest


def clean_filename(value: str) -> str:
    import re
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_")
    return cleaned or "model"


# Canonical Jinja chat templates the supervisor passes to llama-server via
# --chat-template-file when the loaded GGUF's embedded template is missing or
# isn't recognized by llama.cpp's differential autoparser. Without a parseable
# tool-call marker (e.g. `<tool_call>`), the autoparser falls through and the
# lazy PEG grammar that would constrain tool_call JSON output is NEVER built —
# the model emits free-form garbage inside the tag. Community Q4 quants
# routinely ship stripped or non-standard chat_template metadata and trigger
# exactly this failure mode (observed in prod with
# Hermes-3-Llama-3.1-8B.Q4_K_M.gguf).
#
# Pattern is a case-insensitive substring against the active MODEL_NAME (or
# the basename of the GGUF file when MODEL_NAME isn't a HF-style path).
# Template file resolves under LLAMA_CHAT_TEMPLATE_DIR (set in the Dockerfile
# to /app/chat-templates). Returning None means "trust the GGUF metadata".
_CHAT_TEMPLATE_OVERRIDES = (
    ("hermes", "hermes.jinja"),
)


def resolve_chat_template_file(model_name: str, model_file: str) -> str | None:
    """Pick a canonical Jinja template for known-broken GGUF families.

    Lookup is case-insensitive across MODEL_NAME and the GGUF basename so it
    works whether the user sets MODEL_NAME to a HF repo id, a friendly alias,
    or just the raw filename. Returns the absolute path inside the container
    (or None when no override applies).
    """
    template_dir = os.environ.get("LLAMA_CHAT_TEMPLATE_DIR", "").strip()
    if not template_dir:
        return None
    # Explicit override always wins.
    explicit = os.environ.get("LLAMA_CHAT_TEMPLATE_FILE", "").strip()
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(template_dir, explicit)
        return path if os.path.isfile(path) else None
    haystack = f"{model_name} {os.path.basename(model_file or '')}".lower()
    for needle, filename in _CHAT_TEMPLATE_OVERRIDES:
        if needle in haystack:
            path = os.path.join(template_dir, filename)
            if os.path.isfile(path):
                return path
    return None


class LlamaSupervisor:
    def __init__(self):
        self.model_name = os.getenv("MODEL_NAME", "Qwen/Qwen3-8B").strip()
        self.model_commit = os.getenv("MODEL_COMMIT", "").strip()
        self.model_file = os.getenv("MODEL_FILE", "").strip()
        if not self.model_file:
            self.model_file = f"/models/{clean_filename(self.model_name)}.gguf"

        self.ctx = os.getenv("LLAMA_CTX_SIZE", "2048")
        self.parallel = os.getenv("LLAMA_PARALLEL", "2")
        self.port = int(os.getenv("LLAMA_PORT", "8000"))
        self.cache_ram = os.getenv("LLAMA_CACHE_RAM", "0")
        self.use_gpu = os.getenv("LLAMA_USE_GPU", "0").strip().lower() in ("1", "true", "yes")
        self.device = os.getenv("LLAMA_DEVICE", "").strip()
        self.n_gpu_layers = os.getenv("LLAMA_N_GPU_LAYERS", "").strip()
        self.api_key = (os.getenv("LLAMA_API_KEY", "").strip() or os.getenv("API_KEY", "").strip())

        self.control_host = os.getenv("LLAMA_CONTROL_HOST", "0.0.0.0")
        self.control_port = int(os.getenv("LLAMA_CONTROL_PORT", "8001"))
        self.admin_key = (os.getenv("LLAMA_ADMIN_KEY", "").strip() or self.api_key)
        self.switch_timeout_sec = int(os.getenv("LLAMA_SWITCH_TIMEOUT_SEC", "60"))

        self.proc = None
        self.lock = threading.RLock()
        self.stopping = False

    def _server_args(self, model_file: str) -> list[str]:
        args = [
            "/usr/local/bin/llama-server",
            "-m", model_file,
            "--host", "0.0.0.0",
            "--port", str(self.port),
            "--ctx-size", str(self.ctx),
            "--parallel", str(self.parallel),
            "--cache-ram", str(self.cache_ram),
            "--jinja",
        ]
        # When the GGUF's embedded chat_template isn't analyzable by the
        # autoparser (no detectable <tool_call> marker), tool_choice=auto
        # short-circuits the lazy PEG grammar and the model emits unconstrained
        # output. Override with a canonical template for known-broken families
        # so the grammar engages and tool_call JSON becomes grammatically
        # enforced. See `resolve_chat_template_file` above + the autoparser at
        # llama.cpp/common/chat-auto-parser-generator.cpp:55.
        template_file = resolve_chat_template_file(self.model_name, model_file)
        if template_file:
            args += ["--chat-template-file", template_file]
        if self.api_key:
            args += ["--api-key", self.api_key]
        if self.use_gpu:
            if self.device:
                args += ["--device", self.device]
            if self.n_gpu_layers:
                args += ["--n-gpu-layers", self.n_gpu_layers]
        else:
            args += ["--device", "none"]
        return args

    def _wait_backend_ready(self, timeout_sec: int) -> bool:
        deadline = time.time() + timeout_sec
        url = f"http://127.0.0.1:{self.port}/health"
        while time.time() < deadline:
            try:
                with urlrequest.urlopen(url, timeout=2) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def start_backend(self, model_file: str) -> None:
        if not Path(model_file).is_file():
            raise RuntimeError(f"GGUF model not found: {model_file}")
        args = self._server_args(model_file)
        self.proc = subprocess.Popen(args)
        if not self._wait_backend_ready(self.switch_timeout_sec):
            self.stop_backend(force=True)
            raise RuntimeError("llama-server did not become healthy in time")

    def stop_backend(self, force: bool = False) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
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
        model_file = f"/models/{clean_filename(model_name)}.gguf"

        with self.lock:
            old = {
                "model_name": self.model_name,
                "model_commit": self.model_commit,
                "model_file": self.model_file,
            }
            if old["model_name"] == model_name and old["model_commit"] == model_commit:
                return {"ok": True, "switched": False, "active_model": old}

            self.stop_backend(force=True)
            try:
                self.start_backend(model_file)
            except Exception as e:
                # rollback
                try:
                    self.start_backend(old["model_file"])
                    self.model_name = old["model_name"]
                    self.model_commit = old["model_commit"]
                    self.model_file = old["model_file"]
                except Exception:
                    pass
                raise RuntimeError(f"failed to switch backend model: {e}")

            self.model_name = model_name
            self.model_commit = model_commit
            self.model_file = model_file
            return {
                "ok": True,
                "switched": True,
                "active_model": {
                    "model_name": self.model_name,
                    "model_commit": self.model_commit,
                    "model_file": self.model_file,
                },
            }

    def health(self) -> dict:
        alive = self.proc is not None and self.proc.poll() is None
        return {
            "status": "healthy" if alive else "degraded",
            "backend_up": alive,
            "active_model": {
                "model_name": self.model_name,
                "model_commit": self.model_commit,
                "model_file": self.model_file,
            },
        }


SUP = LlamaSupervisor()


class Handler(BaseHTTPRequestHandler):
    server_version = "llama-supervisor/1.0"

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
            model_name = str(payload.get("model_name", "") or "")
            model_commit = str(payload.get("model_commit", "") or "")
            result = SUP.switch_model(model_name, model_commit)
            self._write_json(200, result)
        except Exception as e:
            self._write_json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        # Reduce noise in docker logs.
        pass


def _shutdown(*_):
    SUP.stopping = True
    SUP.stop_backend(force=True)
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    SUP.start_backend(SUP.model_file)
    httpd = ThreadingHTTPServer((SUP.control_host, SUP.control_port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

