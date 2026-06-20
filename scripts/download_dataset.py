import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
NDJSON_PATH = DATA_DIR / "device-telemetry-dataset.ndjson"
DRIVE_FILE_ID = "15HqoteWcUOAEy6aNr0JPjfdM-DMQPd2P"
DOWNLOAD_URL = f"https://drive.google.com/uc?export=download&id={DRIVE_FILE_ID}"


def download_dataset(force: bool = False) -> Path:
    if NDJSON_PATH.exists() and not force:
        print(f"Dataset already present: {NDJSON_PATH}")
        return NDJSON_PATH

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading dataset from Google Drive to {NDJSON_PATH} ...")

    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        response = client.get(DOWNLOAD_URL)
        response.raise_for_status()

    content = response.content
    if not content or content[:1] not in (b"{", b"["):
        raise RuntimeError(
            "Download did not return NDJSON content. "
            "Fetch manually from the assessment Google Drive link."
        )

    NDJSON_PATH.write_bytes(content)
    line_count = sum(1 for _ in NDJSON_PATH.open(encoding="utf-8"))
    print(f"Saved {len(content):,} bytes ({line_count} records) to {NDJSON_PATH}")
    return NDJSON_PATH


def main() -> None:
    force = "--force" in sys.argv
    try:
        download_dataset(force=force)
    except Exception as exc:
        print(f"Dataset download failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
