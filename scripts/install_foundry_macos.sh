#!/usr/bin/env bash
# Foundry Local kurulum yardımcısı (macOS, Apple Silicon).
#
# Foundry Local bir pip paketi değildir; imzalı bir .pkg olarak dağıtılır ve
# kurulumu yönetici hakkı ister. Bu betik indirmeyi ve kurulumu otomatikleştirir,
# ardından projenin ihtiyaç duyduğu iki modeli indirir.
#
# Kullanım:  bash scripts/install_foundry_macos.sh

set -euo pipefail

CHAT_MODEL="qwen3-4b"
EMBED_MODEL="qwen3-embedding-0.6b"
REPO="microsoft/Foundry-Local"

log() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
uyari() { printf '\033[33m[uyarı]\033[0m %s\n' "$1"; }

# ---------------------------------------------------------------- ön kontroller
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Bu betik yalnızca macOS içindir. Windows'ta: winget install Microsoft.FoundryLocal"
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  uyari "Apple Silicon değil ($(uname -m)). Intel Mac'te donanım hızlandırma çalışmaz."
fi

# ---------------------------------------------------------------- kurulum
if command -v foundry >/dev/null 2>&1; then
  log "Foundry Local zaten kurulu: $(foundry --version)"
else
  log "Son sürüm bilgisi alınıyor"
  PKG_URL=$(
    curl -fsSL "https://api.github.com/repos/${REPO}/releases?per_page=20" |
      python3 -c "
import json, sys
for r in json.load(sys.stdin):
    for a in r.get('assets') or []:
        if str(a.get('name','')).endswith('osx-arm64.pkg'):
            print(a['browser_download_url'])
            raise SystemExit
"
  )
  if [[ -z "${PKG_URL}" ]]; then
    echo "İndirme bağlantısı bulunamadı. Elle indirin:"
    echo "  https://github.com/${REPO}/releases"
    echo "  (latest etiketi bazen CLI paketi içermez; cli-preview-*.pkg arayın)"
    exit 1
  fi

  PKG_PATH="${TMPDIR:-/tmp}/$(basename "${PKG_URL}")"
  log "İndiriliyor: $(basename "${PKG_URL}")"
  curl -fL --progress-bar -o "${PKG_PATH}" "${PKG_URL}"

  log "Kuruluyor (yönetici şifresi istenecek)"
  sudo installer -pkg "${PKG_PATH}" -target /
  rm -f "${PKG_PATH}"

  hash -r
  command -v foundry >/dev/null 2>&1 || {
    echo "Kurulum bitti ama 'foundry' komutu PATH'te yok. Yeni bir terminal açın."
    exit 1
  }
  log "Kuruldu: $(foundry --version)"
fi

# ---------------------------------------------------------------- servis
log "Servis başlatılıyor"
foundry server start || uyari "Servis başlatılamadı; SDK yolu yine çalışabilir."

# ---------------------------------------------------------------- modeller
# NOT: `foundry model list` komutu asılabildiği için (bir denemede 6 dakika
# yanıt vermedi) burada hiç çağrılmıyor. `model download` zaten indirilmiş
# modelde hızlı dönüyor, bu yüzden koşulsuz çağrılabilir.
log "Gömme modeli indiriliyor: ${EMBED_MODEL} (~515 MB)"
foundry model download "${EMBED_MODEL}"

log "Sohbet modeli indiriliyor: ${CHAT_MODEL} (~2,9 GB)"
foundry model download "${CHAT_MODEL}"

log "Tamamlandı"
cat <<'SONRAKI'
Sonraki adımlar:

  python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
  ./.venv/bin/python -m src.cli teshis      # kurulumu doğrula
  ./.venv/bin/python -m src.ingest          # korpusu kur (~5 dk)
SONRAKI
