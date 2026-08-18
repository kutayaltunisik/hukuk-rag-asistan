#!/usr/bin/env bash
# Hızlı sağlık kontrolü. LLM çağırmaz.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"

echo "==> Foundry CLI"
command -v foundry >/dev/null
foundry --version

echo "==> Python birim testleri"
"$PY" -m tests.test_cevap_temizleme

echo "==> Korpus bütünlüğü"
"$PY" -m tests.verify_corpus

echo "==> CLI kontrol (teshis + arama + madde 344)"
"$PY" -m src.cli kontrol

echo
echo "Tamam. Arayüz:  ./.venv/bin/streamlit run src/app_streamlit.py"
