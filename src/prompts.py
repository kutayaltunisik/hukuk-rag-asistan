"""Sistem promptu, bağlam şablonu ve feragatname."""

from __future__ import annotations

from src import db

BILMIYORUM_ISARETI = "BAĞLAMDA YOK"

SISTEM_PROMPTU = """Sen Türk hukuku alanında çalışan bir mevzuat asistanısın. \
Elinde yalnızca aşağıda BAĞLAM olarak verilen kanun maddeleri var.

ÇIKTI BİÇİMİ — tam olarak iki satır yaz, başka hiçbir şey yazma. Sıra önemlidir, \
DAYANAK önce gelir:
DAYANAK: <soruyu cevaplayan madde etiketleri, örn. [TBK m. 27], [TBK m. 344]>
CEVAP: <soruya doğrudan cevap, en fazla 4 cümle>

KURALLAR:
1. Cevabını YALNIZCA bağlamdaki madde metinlerine dayandır. Bağlamda olmayan \
hiçbir bilgiyi, kendi genel hukuk bilgini veya başka ülkelerin hukukunu kullanma.
2. DAYANAK satırında yalnızca bağlamda GERÇEKTEN VERİLMİŞ madde numaralarını yaz. \
Bağlamda olmayan bir madde numarası yazmak en ağır hatadır.
3. Bağlamdaki maddeler soruyu cevaplamaya yetmiyorsa, uydurma. Şu ifadeyi kullan: \
"{bilmiyorum}" ve hangi konuda bilgi bulunmadığını bir cümleyle açıkla.
4. Soru bu korpusun kapsamı dışındaki bir mevzuatı ilgilendiriyorsa (örneğin \
İş Kanunu, Türk Ceza Kanunu, usul kanunları, vergi mevzuatı) bunu açıkça söyle.
5. Bağlam bloğunu, madde başlıklarını veya madde metnini OLDUĞU GİBİ KOPYALAMA. \
CEVAP satırında kendi cümlelerinle yaz.
6. Kesin süre, oran ve tutarları maddede yazdığı gibi ver; yuvarlama veya tahmin yapma.
7. Kısa yaz. Aynı cümleyi tekrarlama.
8. Somut olay tavsiyesi verme; yalnızca mevzuatın ne dediğini aktar."""

FERAGATNAME = (
    "Bu cevap yalnızca mevzuat metnine dayalı bilgilendirmedir, hukuki tavsiye "
    "değildir. Somut uyuşmazlığınız için bir avukata danışın."
)

KULLANICI_SABLONU = """BAĞLAM ({adet} madde):
{baglam}

SORU: {soru}"""

# Benzerlik eşiğinin altında kalan sorular için.
ALAKASIZ_SABLONU = """Aşağıdaki soru için korpusta (yalnızca Türk Borçlar Kanunu) \
YETERİNCE ALAKALI BİR HÜKÜM BULUNAMADI. Arama motorunun döndürdüğü en yakın \
maddelerin benzerlik puanı, alakalı sayılma eşiğinin altında kaldı.

SORU: {soru}

GÖREVİN: Bu soruyu CEVAPLAMA. Aşağıdaki maddeler soruyla ilgili DEĞİLDİR; \
onlardan cevap üretmeye çalışma ve onlara atıf yapma.
Şu iki şeyi yap:
1. "{bilmiyorum}" ifadesini kullanarak, bu sorunun cevabının Türk Borçlar \
Kanunu'nda bulunmadığını söyle.
2. Sorunun hangi mevzuat alanına girdiğini tahmin edebiliyorsan tek cümleyle \
belirt (örneğin Türk Ceza Kanunu, Türk Medenî Kanunu, İş Kanunu, idari yargı \
usulü, vergi mevzuatı). Emin değilsen bunu da söyle.

Alakasız olduğu tespit edilen maddeler (yalnızca bilgi için, cevap için değil):
{baglam}"""


def baglami_bicimle(parcalar: list[db.ParcaKaydi]) -> str:
    """Getirilen maddeleri modele verilecek biçime sokar."""
    bloklar: list[str] = []
    for p in parcalar:
        baslik = f"[{p.atif}]"
        if p.kenar_baslik:
            baslik += f" {p.kenar_baslik}"
        if p.not_etiketi:
            baslik += f" ({p.not_etiketi})"
        bloklar.append(f"{baslik}\n{p.icerik}")
    return "\n\n".join(bloklar)


# İstenen çıktı biçimini gösteren tek örnek.
ORNEK_SORU = """BAĞLAM (1 madde):
[TBK m. 232] I. Genel olarak
Satılan, alıcıya teslim edilmedikçe yarar ve hasar satıcıya aittir.

SORU: Satılan mal teslim edilmeden zarar görürse hasara kim katlanır?"""

ORNEK_CEVAP = (
    "DAYANAK: [TBK m. 232]\n"
    "CEVAP: Satılan mal alıcıya teslim edilmediği sürece hasara satıcı "
    "katlanır; yarar da satıcıya aittir."
)


def mesajlari_kur(
    soru: str, parcalar: list[db.ParcaKaydi], alakali: bool = True
) -> list[dict]:
    """Modele gönderilecek mesajları kurar.

    `alakali=False` ise (arama alaka eşiğinin altında kaldıysa) modele farklı bir
    görev verilir: cevap üretmek değil, sorunun korpus kapsamı dışında olduğunu
    bildirmek.
    """
    baglam = baglami_bicimle(parcalar) or "(ilgili madde bulunamadı)"
    if alakali:
        kullanici = KULLANICI_SABLONU.format(
            adet=len(parcalar), baglam=baglam, soru=soru.strip()
        )
    else:
        kullanici = ALAKASIZ_SABLONU.format(
            soru=soru.strip(), baglam=baglam, bilmiyorum=BILMIYORUM_ISARETI
        )
    mesajlar = [
        {
            "role": "system",
            "content": SISTEM_PROMPTU.format(bilmiyorum=BILMIYORUM_ISARETI),
        }
    ]
    # Örnek yalnızca normal (alakalı) sorularda verilir. Kapsam dışı durumda
    # görev "cevap yaz" değil "cevap verme" olduğu için cevaplı bir örnek
    # göstermek modeli ters yöne çeker.
    if alakali:
        mesajlar += [
            {"role": "user", "content": ORNEK_SORU},
            {"role": "assistant", "content": ORNEK_CEVAP},
        ]
    mesajlar.append({"role": "user", "content": kullanici})
    return mesajlar
