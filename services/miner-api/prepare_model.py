import os
import re
import subprocess
from pathlib import Path

from huggingface_hub import snapshot_download


HF_MODEL = os.environ.get("MODEL_NAME", "Qwen/Qwen3-8B")
MODEL_COMMIT = os.environ.get("MODEL_COMMIT", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip() or None
SCRIPT_DIR = Path(__file__).resolve().parent
LLAMA_CPP_DIR = Path(os.environ.get("LLAMA_CPP_DIR", SCRIPT_DIR / "llama.cpp"))
CACHE_ROOT = Path("/models/hub") / f"models--{HF_MODEL.replace('/', '--')}"


def clean_filename(name: str) -> str:
    """Replace slashes and punctuation with a stable filesystem-safe name."""
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name)
    return name.strip("_")


def resolve_output_file() -> Path:
    explicit = os.environ.get("MODEL_FILE", "").strip()
    if explicit:
        return Path(explicit)
    return Path("/models") / f"{clean_filename(HF_MODEL)}.gguf"


def find_or_download() -> Path:
    snapshots_dir = CACHE_ROOT / "snapshots"
    if snapshots_dir.exists():
        snaps = sorted(path for path in snapshots_dir.iterdir() if path.is_dir())
        if MODEL_COMMIT:
            for snap in snaps:
                if snap.name.startswith(MODEL_COMMIT):
                    return snap
        if snaps:
            return snaps[-1]

    return Path(
        snapshot_download(
            repo_id=HF_MODEL,
            cache_dir="/models/hub",
            resume_download=True,
            revision=MODEL_COMMIT or None,
            token=HF_TOKEN,
        )
    )


def convert_model(model_dir: Path, output_file: Path) -> None:
    converter = LLAMA_CPP_DIR / "convert_hf_to_gguf.py"
    if not converter.exists():
        raise FileNotFoundError(f"converter not found at {converter}")

    subprocess.run(
        [
            "python3",
            str(converter),
            str(model_dir),
            "--outfile",
            str(output_file),
            "--outtype",
            os.environ.get("OUT_TYPE", "f32"),
        ],
        check=True,
        cwd=str(LLAMA_CPP_DIR),
    )


if __name__ == "__main__":
    output_file = resolve_output_file()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    base_name = output_file.stem
    model_marker = output_file.with_name(f"current_model_{base_name}.txt")
    marker_expected = HF_MODEL if not MODEL_COMMIT else f"{HF_MODEL}@{MODEL_COMMIT}"

    if output_file.exists() and model_marker.exists():
        if model_marker.read_text().strip() == marker_expected:
            print(f"Model {marker_expected} already converted at {output_file}, skipping.")
            raise SystemExit(0)

    model_dir = find_or_download()
    print(f"Using model directory: {model_dir}")
    print(f"Writing GGUF to: {output_file}")

    convert_model(model_dir, output_file)
    model_marker.write_text(marker_expected)
