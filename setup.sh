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

echo ""
echo "Setup complete."
echo "  Virtual environment: ${VENV_DIR}"
echo "  Python: $(python --version)"
echo "  Packages installed from requirements.txt"
echo ""
echo "To activate the environment manually:"
if [[ -f "${VENV_DIR}/Scripts/activate" ]]; then
    echo "  source ${VENV_DIR}/Scripts/activate"
else
    echo "  source ${VENV_DIR}/bin/activate"
fi
echo ""
echo "Copy .env.example to .env and fill in your API keys before running the agent."
