# Hukuk RAG Asistanı

Türk Borçlar Kanunu (6098) üzerinde çalışan yerel bir soru-cevap aracı.
Model cihazda (Microsoft Foundry Local), arama SQLite'ta (`db/hukuk.db`).
Cevaplar maddeye atıf verir; atıf bulunan maddede yoksa uyarır. Arayüz
Streamlit, bir de CLI var.

Korpus yalnızca TBK. İçtihat yok, hukuki tavsiye yok.

```
kanun  →  parçala (chunk)  →  embedding  →  SQLite
                                              │
soru   →  aynı embedding   →  vektör + kelime + madde no
                                              │
                         bulunan maddeler = BAĞLAM
                                              │
                         yerel LLM  →  atıflı cevap
```

## RAG 

Model kanunu ezberlemiyor. Soru geldiğinde önce ilgili maddeler bulunur,
sonra o maddeler prompt'a **bağlam** diye yazılır, model yalnızca onlara
bakarak cevap üretir. Buna RAG deniyor: retrieval (bul) + generation (yaz).

Asıl öğrenilecek kısım ikinci adım değil, birinci. Yanlış madde giderse
cevap da yanlış oluyor; model kendinden emin duruyor. Bu yüzden boru hattı
şöyle: metni parçala, her parçayı vektöre çevir, soruyu da aynı uzaya at,
yakın vektörleri getir, bunları bağlama koy, sonra konuş.

Hukukta bir de şunu gördüm: genel RAG tarifi yetmiyor. Madde ortadan
kesilince hüküm bozuluyor. İnsan "depozito" diyor, kanun "güvence" diyor
(m. 342). Model bağlamda olmayan madde numarası uydurabiliyor. Yani
chunking, embedding ve retrieval'ı anlamak yetmiyor; bunları alanın
diline göre ayarlamak gerekiyor.

## Parçalama (chunking)

Kanunun tamamını tek parça gömemezsin. Embedding modelinin penceresi
sınırlı, arama da "bu soruya en yakın *parça*" diye çalışıyor. O yüzden
metni parçalıyorsun. Bu iş `src/mevzuat/chunker.py`'de.

Genelde 500–1000 karakterlik pencereler kesiliyor. TBK'da bunu yapmadım:
anlam birimi madde. Madde 344'ü ortadan bölersen kira tavanının yarısı
bir parçada, yarısı öbüründe kalıyor; atıf da "hangi madde?" olmaktan
çıkıyor. Çok uzun maddeler (~2400 karakterden sonra) fıkra sınırından
bölünüyor, cümle ortasından değil. Arada 200 karakter örtüşme var ki
fıkra kopmasın.

Kaynak [mevzuat.gov.tr](https://www.mevzuat.gov.tr) (`1.5.6098.htm`).
HTML Word'den üretilmiş, kitap / kısım / bölüm / kenar başlık / madde
diye ayrışıyor. Kanunun sonundaki işlenemeyen hükümler ayrı etiketli —
numaralar TBK ile çakışabiliyor (hem TBK'da hem 6217'de "Geçici 2" var).
Sonuç: 649 madde, 652 parça.

## Embedding ve vektörler

Her parça `qwen3-embedding-0.6b` ile 1024 boyutlu bir vektöre çevriliyor.
Anlamca yakın metinler uzayda da yakın duruyor. "kira artışı sınırı" ile
m. 344 aynı kelimeleri paylaşmasa da vektörleri birbirine yaklaşıyor.
Kelime araması bunu yapamaz.

Soru geldiğinde **aynı model** soruyu da vektöre çeviriyor. Arama, soru
vektörü ile madde vektörleri arasında kosinüs benzerliği: açı küçükse
metinler yakın. En yakınlar aday madde oluyor.

Vektörler SQLite'ta blob; Chroma / FAISS yok, küçük korpus için gerekmedi.
Ingest bir kez çalışıyor (`python -m src.ingest`): indir → parçala → göm
→ kaydet. İlk sefer ~5 dakika, internet yalnızca kanun indirme. Sonrası
offline.

Öğrendiğim sınır: gömme genel amaçlı ve çok dilli. "depozito = güvence",
"ihtiyaç = gereksinim" (m. 350, konuttan çıkarma) gibi eşleşmeleri
kaçırabiliyor. `src/esanlam.py` kelime aramasını genişletiyor; gömme
sorgusuna ekleyince vektör asıl sorudan kayıyordu.

## Arama: vektör yetmeyince

Sadece vektörle gittim, yetmedi (hit@5 ~%93). "TBK 344 ne diyor?" gibi
soruda "344" vektörde neredeyse sinyal taşımıyor; konu olarak yakın
başka maddeler önde çıkıyor. Tam terimler ("zamanaşımı") kelime
indeksinde daha net.

Üç kanal, `src/retrieve.py`:

1. vektör (anlam)
2. FTS5 / BM25 (kelime)
3. madde numarası (doğrudan)

Her kanal ~12 aday üretiyor, modele en fazla 5 madde gidiyor. Üç
sıralama Reciprocal Rank Fusion ile birleşiyor. Skorları aynı ölçeğe
çekmeye gerek yok; sıra numarasına bakıyor. Madde numarası yazıldıysa o
kanal ağır basıyor.

Kelime tarafında "sözleşme", "zarar" neredeyse her maddede geçiyor,
listeyi kirletiyor. Belge frekansına bakıp sık geçenleri atıyorum.
Sabit durak kelime listesi hukukta işe yaramıyor; hangi kelimenin boş
olduğu korpusa bağlı.

## Bağlam ve cevap

Bulunan maddelerin metni prompt'a `BAĞLAM:` diye yazılıyor
(`src/prompts.py`). Model (`qwen3-4b`, Foundry Local) iki satır yazıyor:
DAYANAK (hangi maddeler) ve CEVAP. Kendi eğitiminden hukuk "hatırlamasın"
diye talimat var. Streamlit'te kaynak maddeler cevaptan önce açılıyor —
okunması gereken şey zaten madde.

Her getirilen madde bağlama girmiyor. Benzerlik çok düşükse soru TBK
dışında sayılıyor (işçi çıkarımı, ceza, idare…). O zaman maddeler "cevap
adayı" değil "alakasız bulundu" diye veriliyor; yoksa model kira
maddesindeki 30 günü idari dava süresi sanıyor (m. 353). Zayıf maddeler
de ayrılıyor: kira tavanı sorusuna rekabet yasağındaki "iki yılı aşamaz"
(m. 445) karışmıştı.

Cevap bitince atıflar taranıyor (`src/answer.py`). Bağlamda yoksa kırmızı
uyarı. Prompt "uydurma" demek yetmiyor; kontrol kodda.

## Ortam

M2, 8 GB RAM. Gömme ve sohbet aynı makinede, `foundry-local-sdk`. Qwen3
düşünme açıkken soru ~2 dakikada kilitleniyordu; `/no_think` ile ilk
token ~3 s, cevap ~30 s. SDK modelleri `~/.foundry/cache/models` verilmezse
bulamıyor, sistem mesajı da kullanıcı turuna katlanıyor.

34 soru ile baktım (`tests/eval_set.yaml`, 28 cevaplanabilir + 6 tuzak):
arama hit@5 %100, atıflı doğru cevap ~%75, kapsam dışı ret %100. Cevap,
aramadan zor; doğru madde önündeyken bile kayabiliyor.

## Kurulum

Python 3.12+, Foundry Local, ~4 GB disk (modeller).

```bash
bash scripts/install_foundry_macos.sh
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m src.ingest
```

Elle: [Foundry Local](https://github.com/microsoft/Foundry-Local/releases)
`osx-arm64.pkg`, sonra `qwen3-embedding-0.6b` ve `qwen3-4b`.
`foundry model list` takılabiliyor; `python -m src.cli teshis` daha iyi.

## Çalıştırma

```bash
./.venv/bin/streamlit run src/app_streamlit.py
```

```bash
./.venv/bin/python -m src.cli sor "Kira artışının üst sınırı nedir?"
./.venv/bin/python -m src.cli ara "zamanaşımı" --ayrintili
./.venv/bin/python -m src.cli madde 344
./.venv/bin/python -m src.cli kontrol
```

```bash
./.venv/bin/python -m tests.verify_corpus
./.venv/bin/python -m tests.evaluate --sadece-arama
```

Ayarlar `src/config.py` (`HUKUK_CHAT_MODEL`, `HUKUK_TOP_K`, `HUKUK_PROVIDER`).

## Kod

```
src/ingest.py            indir → parçala → göm → SQLite
src/mevzuat/chunker.py   HTML → madde
src/db.py                FTS5 + vektör blob
src/retrieve.py          vektör + FTS + madde no
src/esanlam.py           günlük dil ↔ kanun dili
src/prompts.py           bağlam bloğu
src/answer.py            orkestrasyon + atıf kontrolü
src/providers.py         Foundry
src/cli.py / app_streamlit.py
tests/eval_set.yaml      34 soru
```

## Sınırlar

Yalnızca TBK 6098. Yargıtay yok, yürürlük tarihi yok. 
Hukuki tavsiye değil; kanun metnine bakmayı kolaylaştırıyor.
