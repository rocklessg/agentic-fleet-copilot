import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
NDJSON_PATH = DATA_DIR / "device-telemetry-dataset.ndjson"
DB_PATH = DATA_DIR / "telemetry.db"
DOWNLOAD_SCRIPT = PROJECT_ROOT / "scripts" / "download_dataset.py"


def _needs_ingest() -> bool:
    if not DB_PATH.exists():
        return True
    if not NDJSON_PATH.exists():
        return False
    return NDJSON_PATH.stat().st_mtime > DB_PATH.stat().st_mtime


def bootstrap(force_download: bool = False) -> None:
    download_args = [sys.executable, str(DOWNLOAD_SCRIPT)]
    if force_download:
        download_args.append("--force")
    subprocess.run(download_args, cwd=PROJECT_ROOT, check=True)

    if _needs_ingest():
        print("Building SQLite telemetry database ...")
        subprocess.run(
            [sys.executable, "-m", "src.database.ingest"],
            cwd=PROJECT_ROOT,
            check=True,
        )
        print(f"Telemetry database ready: {DB_PATH}")
    else:
        print(f"Telemetry database up to date: {DB_PATH}")


def main() -> None:
    force = "--force-download" in sys.argv
    bootstrap(force_download=force)


if __name__ == "__main__":
    main()
