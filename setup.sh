#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"

echo "Creating Python virtual environment in ${VENV_DIR}..."
python3 -m venv "$VENV_DIR" 2>/dev/null || python -m venv "$VENV_DIR"

if [[ -f "${VENV_DIR}/Scripts/activate" ]]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/Scripts/activate"
elif [[ -f "${VENV_DIR}/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
else
    echo "Error: could not find virtual environment activation script." >&2
    exit 1
fi

echo "Upgrading pip..."
python -m pip install --upgrade pip

echo "Installing requirements..."
pip install -r requirements.txt

if [[ ! -f ".env" ]]; then
    echo "Creating .env from .env.example ..."
    cp .env.example .env
    echo "Update .env with your OPENAI_API_KEY and LANGCHAIN_API_KEY before running the agent."
fi

echo "Downloading dataset and building telemetry database..."
python scripts/bootstrap.py

echo ""
echo "Setup complete."
echo "  Virtual environment: ${VENV_DIR}"
echo "  Python: $(python --version)"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Start API:  uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000"
echo "  3. Start UI:   streamlit run app.py"
echo "  Or use Docker: docker compose up --build"
echo "  Run evals:     pytest evals/test_copilot.py -v"
