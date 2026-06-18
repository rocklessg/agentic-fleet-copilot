import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")

    langchain_key = os.getenv("LANGCHAIN_API_KEY", "")
    if not langchain_key or langchain_key.startswith("your-"):
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
