"""Korpus bütünlüğü kanıtı: TBK'nın tüm maddeleri doğru ayrıştırıldı mı?

Ayrıştırıcının doğruluğu, "649 madde bulduk" demekle kanıtlanmaz. Bu betik
dört bağımsız kontrol yapar:

  1. SAYIM       — kaç normal madde, kaç geçici madde bulundu?
  2. SÜREKLİLİK  — 1'den son maddeye kadar EKSİK numara var mı? Tekrar var mı?
                   Bu, en kritik kontroldür: Word kaynaklı HTML'de "MADDE" ile
                   numarası ayrı satırlara düştüğünde madde sessizce kaybolur.
  3. İÇERİK      — boş, aşırı kısa ya da şüpheli uzun madde var mı? Madde
                   metnine yanlışlıkla başlık veya dipnot karışmış mı?
  4. ÖRNEKLEME   — bilinen maddelerin metni gerçekten doğru mu (elle doğrulanmış
                   çapa maddeler ile karşılaştırma).

Kullanım: ./.venv/bin/python -m tests.verify_corpus
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402

# Elle doğrulanmış çapa maddeler: metinlerinde MUTLAKA geçmesi gereken ifadeler.
# mevzuat.gov.tr'deki resmî metinle karşılaştırılarak seçildi.
CAPA_MADDELER: dict[str, str] = {
    "1": "karşılıklı ve birbirine uygun olarak açıklamalarıyla kurulur",
    "27": "Kanunun emredici hükümlerine, ahlaka, kamu düzenine",
    "146": "her alacak on yıllık zamanaşımına tabidir",
    "299": "Kira sözleşmesi, kiraya verenin bir şeyin kullanılmasını",
    "344": "tüketici fiyat endeksindeki oniki aylık ortalamalara göre değişim oranını",
    "438": "haklı sebep olmaksızın hizmet sözleşmesini derhâl feshederse",
    "649": "",   # son madde: yalnızca varlığı kontrol edilir
}


def main() -> int:
    conn = db.baglan()
    satirlar = conn.execute(
        "SELECT madde_no, madde_turu, atif, kenar_baslik, icerik, parca_no "
        "FROM parcalar ORDER BY sira"
    ).fetchall()

    if not satirlar:
        print("HATA: veritabanı boş. Önce: ./.venv/bin/python -m src.ingest")
        return 1

    # Aynı madde birden fazla parçaya bölünmüş olabilir; madde bazında topla.
    maddeler: dict[str, str] = {}
    for s in satirlar:
        maddeler.setdefault(s["madde_no"], "")
        maddeler[s["madde_no"]] += ("\n" if maddeler[s["madde_no"]] else "") + s["icerik"]

    normal = sorted(int(m) for m in maddeler if m.isdigit())
    gecici = sorted(m for m in maddeler if not m.isdigit())

    # Tekrar sayımı HAM SATIRLAR üzerinden yapılmalı. Madde sözlüğünün
    # anahtarları üzerinden sayılırsa tekrar bulunması yapısal olarak imkânsızdır
    # (sözlük anahtarları tekildir) ve kontrol sessizce hep "tekrar yok" der.
    # Bu tuzağa ilk sürümde düşüldü; gerçek bir tekrar bu yüzden gözden kaçtı.
    from collections import Counter

    atif_sayaci = Counter((s["madde_no"], s["atif"].split(" (")[0]) for s in satirlar)
    madde_sayaci = Counter(s["madde_no"] for s in satirlar)
    cok_parcali = {
        s["madde_no"] for s in satirlar if (s["parca_no"] or 1) > 1
    }
    # Aynı madde numarasının birden çok satırı varsa bu ya uzun maddenin
    # parçalanmasıdır (meşru) ya da iki farklı hükmün aynı numarayı taşımasıdır.
    ayni_no_farkli_hukum = sorted(
        no for no, adet in madde_sayaci.items()
        if adet > 1 and no not in cok_parcali
    )

    print("=" * 68)
    print("1) SAYIM")
    print("=" * 68)
    print(f"  parça (chunk) sayısı      : {len(satirlar)}")
    print(f"  ayrı madde sayısı         : {len(maddeler)}")
    print(f"  normal madde (sayılı)     : {len(normal)}  "
          f"(en küçük {normal[0]}, en büyük {normal[-1]})")
    print(f"  geçici/ek madde           : {len(gecici)}  -> {gecici}")

    print()
    print("=" * 68)
    print("2) SÜREKLİLİK  (1 .. %d aralığında eksik/tekrar var mı?)" % normal[-1])
    print("=" * 68)
    beklenen = set(range(1, normal[-1] + 1))
    eksik = sorted(beklenen - set(normal))
    cakisan_atif = sorted(
        {no for (no, _atif), adet in atif_sayaci.items() if adet > 1}
        - cok_parcali
    )
    print(f"  beklenen madde sayısı     : {len(beklenen)}")
    print(f"  bulunan madde sayısı      : {len(set(normal))}")
    print(f"  EKSİK MADDE               : {eksik or 'YOK'}")
    print(f"  aynı numarayı taşıyan     : {ayni_no_farkli_hukum or 'YOK'}")
    print(f"  ÇAKIŞAN ATIF (aynı atıf,  ")
    print(f"    farklı hüküm)           : {cakisan_atif or 'YOK'}")
    if ayni_no_farkli_hukum:
        print("    not: aynı numara farklı kaynaklardan gelebilir; belirleyici")
        print("         olan ATIF çakışmasıdır (bkz. işlenemeyen hükümler).")
    surekli = not eksik and not cakisan_atif

    print()
    print("=" * 68)
    print("3) İÇERİK SAĞLIĞI")
    print("=" * 68)
    bos = [m for m, t in maddeler.items() if not t.strip()]
    kisa = [(m, len(t)) for m, t in maddeler.items() if 0 < len(t.strip()) < 40]
    uzunluklar = sorted(((len(t), m) for m, t in maddeler.items()), reverse=True)
    # Madde metnine başlık karışmasının izi: "MADDE" kelimesinin metin içinde
    # tekrar geçmesi ya da dipnot işaretinin kalması.
    kirli = [
        m for m, t in maddeler.items()
        if re.search(r"(?i)\bMADDE\s+\d+\s*-", t) or "(1)" == t.strip()[:3]
    ]
    print(f"  boş madde                 : {bos or 'YOK'}")
    print(f"  40 karakterden kısa madde : {kisa or 'YOK'}")
    print(f"  metne 'MADDE n-' karışmış : {kirli or 'YOK'}")
    print(f"  en uzun 3 madde           : "
          f"{[(m, u) for u, m in uzunluklar[:3]]}")
    print(f"  en kısa 3 madde           : "
          f"{[(m, u) for u, m in uzunluklar[-3:]]}")
    print(f"  ortalama uzunluk          : "
          f"{sum(len(t) for t in maddeler.values()) // len(maddeler)} karakter")
    saglikli = not bos and not kirli

    print()
    print("=" * 68)
    print("4) ÇAPA MADDE DOĞRULAMASI (resmî metinle karşılaştırma)")
    print("=" * 68)
    capa_tamam = True
    for no, beklenen_ifade in CAPA_MADDELER.items():
        metin = maddeler.get(no)
        if metin is None:
            print(f"  m. {no:>3}  BULUNAMADI")
            capa_tamam = False
            continue
        if not beklenen_ifade:
            print(f"  m. {no:>3}  var ({len(metin)} karakter)")
            continue
        varmi = beklenen_ifade in metin
        capa_tamam &= varmi
        print(f"  m. {no:>3}  {'OK ' if varmi else 'HATA'}  "
              f"beklenen ifade {'bulundu' if varmi else 'BULUNAMADI'}")

    # Yapısal üstveri (kenar başlık / konu yolu) doldurulmuş mu?
    bassiz = conn.execute(
        "SELECT count(*) FROM parcalar WHERE kenar_baslik IS NULL OR kenar_baslik=''"
    ).fetchone()[0]
    yolsuz = conn.execute(
        "SELECT count(*) FROM parcalar WHERE konu_yolu IS NULL OR konu_yolu=''"
    ).fetchone()[0]
    print()
    print("=" * 68)
    print("5) YAPISAL ÜSTVERİ")
    print("=" * 68)
    print(f"  kenar başlığı olmayan parça : {bassiz} / {len(satirlar)}")
    print(f"  konu yolu olmayan parça     : {yolsuz} / {len(satirlar)}")

    print()
    print("=" * 68)
    basarili = surekli and saglikli and capa_tamam
    print("SONUÇ: " + ("KORPUS BÜTÜNLÜĞÜ DOĞRULANDI" if basarili
                       else "KORPUSTA SORUN VAR — yukarıya bakın"))
    print("=" * 68)
    return 0 if basarili else 1


if __name__ == "__main__":
    raise SystemExit(main())
