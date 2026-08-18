"""Streamlit arayüzü.

    ./.venv/bin/streamlit run src/app_streamlit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# `streamlit run src/app_streamlit.py` çağrısında proje kökü sys.path'te olmuyor.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, db  # noqa: E402
from src.answer import HukukAsistani  # noqa: E402
from src.prompts import FERAGATNAME  # noqa: E402

st.set_page_config(page_title="Hukuk RAG Asistanı", page_icon="⚖️", layout="wide")

ORNEK_SORULAR = [
    "Kira bedelindeki artış oranının üst sınırı nedir?",
    "Genel zamanaşımı süresi kaç yıldır?",
    "Ahlaka aykırı bir sözleşme geçerli midir?",
    "Kiracı kira bedelini ödemezse kiraya veren ne yapabilir?",
    "İşveren haklı sebep olmadan işçiyi çıkarırsa işçi ne isteyebilir?",
    "Evli bir kişinin kefil olması için eşinin rızası gerekir mi?",
    "TBK 344 ne diyor?",
]


@st.cache_resource(show_spinner="Model ve veritabanı yükleniyor (ilk açılışta ~30 saniye)...")
def asistani_yukle():
    """Ağır kaynakları süreç ömrü boyunca tek örnek olarak tutar."""
    return HukukAsistani()


@st.cache_data(show_spinner=False)
def korpus_bilgisi() -> dict:
    return db.istatistik(db.baglan())


def kaynaklari_goster(parcalar, baglam=None) -> None:
    """Bulunan maddeleri gösterir; modele gönderilmeyenleri ayrıca işaretler."""
    baglam_idleri = {p.id for p in baglam} if baglam is not None else None
    if baglam_idleri is not None:
        st.caption(
            f"{len(parcalar)} madde bulundu · {len(baglam_idleri)} tanesi modele "
            f"bağlam olarak verildi (zayıf eşleşmeler bağlama alınmaz)"
        )
    else:
        st.caption(f"{len(parcalar)} madde bulundu")

    for i, p in enumerate(parcalar, 1):
        baglamda = baglam_idleri is None or p.id in baglam_idleri
        baslik = f"{'✓' if baglamda else '·'} {i}. {p.atif}"
        if p.kenar_baslik:
            baslik += f" — {p.kenar_baslik}"
        if not baglamda:
            baslik += "   (bağlama alınmadı)"
        with st.expander(baslik, expanded=(i == 1)):
            if p.konu_yolu:
                st.caption(p.konu_yolu)
            st.write(p.icerik)
            etiketler = (
                f"skor {p.skor:.5f} · benzerlik {p.benzerlik:.4f} · "
                f"bulan: {', '.join(p.kaynaklar)}"
            )
            if p.not_etiketi:
                etiketler += f" · {p.not_etiketi}"
            st.caption(etiketler)


# --------------------------------------------------------------------------
# Kenar çubuğu
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("⚖️ Hukuk RAG Asistanı")
    st.caption("Tamamen yerel çalışır — internet ve bulut kullanılmaz.")

    try:
        bilgi = korpus_bilgisi()
        st.subheader("Korpus")
        st.metric("Madde sayısı", bilgi.get("madde_sayisi", "?"))
        st.caption(
            f"{bilgi.get('parca_sayisi')} parça · "
            f"{bilgi.get('gomme_boyutu')} boyutlu gömme"
        )
        st.caption(f"Gömme modeli: `{bilgi.get('gomme_modeli')}`")
    except Exception as exc:
        st.error(f"Veritabanı okunamadı: {exc}")
        st.code("./.venv/bin/python -m src.ingest")
        st.stop()

    st.divider()
    top_k = st.slider(
        "Getirilecek madde sayısı", 3, 10, config.TOP_K,
        help="Modele bağlam olarak verilecek madde sayısı. "
             "Ölçümde 5 madde ile 28 sorunun tamamında doğru madde bağlama girdi.",
    )
    ayrintili = st.toggle("Arama teşhisini göster", value=False)

    st.divider()
    st.subheader("Örnek sorular")
    for ornek in ORNEK_SORULAR:
        if st.button(ornek, use_container_width=True, key=f"ornek_{ornek}"):
            st.session_state["secili_soru"] = ornek

    st.divider()
    st.caption(FERAGATNAME)


# --------------------------------------------------------------------------
# Ana bölüm
# --------------------------------------------------------------------------
st.title("Türk Borçlar Hukuku Asistanı")

try:
    asistan = asistani_yukle()
except Exception as exc:
    st.error("Yerel model sağlayıcısı başlatılamadı.")
    st.exception(exc)
    st.info("Kontrol: `foundry --version` ve `./.venv/bin/python -m src.cli teshis`")
    st.stop()

st.caption(
    f"Sağlayıcı: `{type(asistan.saglayici).__name__}` · "
    f"Model: `{asistan.saglayici.chat_model}` · "
    f"Gömme: `{asistan.saglayici.embed_model}`"
)

if "gecmis" not in st.session_state:
    st.session_state["gecmis"] = []

# Önceki turlar
for tur in st.session_state["gecmis"]:
    with st.chat_message("user"):
        st.write(tur["soru"])
    with st.chat_message("assistant"):
        st.markdown(tur["cevap"])
        if tur["uyari"]:
            st.error(tur["uyari"])
        with st.expander("Dayanak maddeler"):
            for satir in tur["kaynaklar"]:
                st.write(f"- {satir}")

soru = st.chat_input("Borçlar hukuku hakkında bir soru sorun...")
if secili := st.session_state.pop("secili_soru", None):
    soru = secili

if soru:
    with st.chat_message("user"):
        st.write(soru)

    with st.chat_message("assistant"):
        # Kaynaklar cevaptan önce gösterilir: kullanıcı model yazarken okumaya
        # başlayabiliyor ve bekleme süresi boşa geçmiyor.
        with st.status("Mevzuat aranıyor...", expanded=False) as durum:
            arama, akis = asistan.cevapla_akisli(soru, top_k=top_k)
            baglam = arama.baglam_parcalari()
            durum.update(
                label=f"{len(arama.parcalar)} madde bulundu — cevap yazılıyor...",
                state="running",
            )
            kaynaklari_goster(arama.parcalar, baglam)
            if ayrintili:
                st.caption(
                    f"vektör: {arama.vektor_adet} aday · "
                    f"kelime: {arama.fts_adet} aday · "
                    f"madde-no: {arama.madde_adet} aday · "
                    f"en yüksek benzerlik: {arama.en_yuksek_benzerlik:.4f}"
                )
                if arama.fts_sorgusu:
                    st.code(arama.fts_sorgusu, language="text")
            durum.update(label="Kaynak maddeler", state="complete")

        if not arama.alakali:
            st.warning(
                f"Bu soru korpusun (yalnızca TBK 6098) kapsamı dışında görünüyor: "
                f"en yakın maddenin benzerliği {arama.en_yuksek_benzerlik:.3f}, "
                f"alaka eşiği {config.RELEVANCE_MIN}. Asistanın cevap üretmemesi "
                f"beklenir."
            )

        yazi = st.write_stream(akis)

        cevap = asistan.cevabi_sonlandir(soru, arama, yazi or "")
        if cevap.metin.strip() != (asistan.saglayici._temizle(yazi or "")).strip():
            st.markdown(cevap.metin)

        uyari = ""
        if cevap.dayanaksiz_atiflar:
            uyari = (
                "Bu cevap, getirilen maddelerde bulunmayan atıf(lar) içeriyor: "
                + ", ".join(cevap.dayanaksiz_atiflar)
                + ". Cevaba güvenmeyin; madde metinlerini kendiniz kontrol edin."
            )
            st.error(uyari)
        elif cevap.atiflar:
            st.success("Atıflar doğrulandı: " + ", ".join(cevap.atiflar))
        elif cevap.bilmiyorum:
            st.info("Asistan, sorunun cevabının bu korpusta bulunmadığını bildirdi.")

        st.caption(FERAGATNAME)

    st.session_state["gecmis"].append(
        {
            "soru": soru,
            "cevap": cevap.metin,
            "uyari": uyari,
            "kaynaklar": cevap.kaynak_listesi(),
        }
    )
