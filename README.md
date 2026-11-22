# StajBot — Staj Başvuru Otomasyonu

Bilgisayar mühendisliği öğrencileri için **İşyeri Mesleki Eğitim (uzun dönem, yüz yüze) staj başvurusu**
otomasyonu. Hedef şehirlerdeki (Konya, Ankara, İstanbul, İzmir, Eskişehir, Bursa, Sakarya, Antalya,
Mersin, Afyonkarahisar) yazılım / bilişim şirketlerini bulur, e-posta adreslerini çıkarır, sizin
onayınızla kişiselleştirilmiş başvuru mailleri üretir, kontrollü biçimde gönderir ve gelen cevapları
takip eder.

**Ne yapar?**

1. Şirketleri birden çok kaynaktan toplar ve tekilleştirir: Sanayi Bakanlığı Ar-Ge / Tasarım Merkezleri
   listesi, teknopark firma rehberleri, YASAD / TÜBİSAD üye listeleri (Bilişim 500), Google Places,
   CSV (okul protokol listesi, elle toplanan).
2. Şirket web sitelerini tarayarak e-posta (İK / kariyer öncelikli), teknoloji yığını, kariyer sayfası,
   staj ilanı ve büyüklük ipuçlarını çıkarır (**zenginleştirme**).
3. Her şirkete 0-100 arası otomatik **puan** verir (staj ilanı, Ar-Ge merkezi, teknopark, okul protokolü,
   HR e-postası, profilinizle örtüşen teknolojiler...).
4. Web arayüzündeki **inceleme kuyruğunda** şirketleri onaylar / reddedersiniz.
5. Onaylı şirketler için şablon + LLM (Anthropic) ile **kişiselleştirilmiş başvuru maili taslağı** üretir;
   siz önizleyip onaylarsınız.
6. Günlük limit, mesai saati ve rastgele bekleme ile **gönderir** (ya da Gmail taslaklarına koyar).
7. Gelen kutusunu okur, cevapları şirkete eşleştirir (Message-ID, `BV-0042` takip kodu, domain),
   olumlu / olumsuz tahmini yapar; telefon, mülakat, teklif gibi aşamaları siz işaretlersiniz.
   Her şey şirket başına **zaman çizelgesinde** tutulur.

> Hiçbir mail sizin onayınız olmadan gitmez: taslaklar web arayüzünde onaylanır, gönderim ayrıca
> `send` komutuyla (ya da arayüzdeki butonla) başlatılır ve `MAIL_MODE` ile kontrol edilir.

---

## Kurulum

Gereksinimler: Windows 11 (Linux/macOS'ta da çalışır), Python 3.13, bir Gmail hesabı.

```powershell
cd "C:\Users\...\staj otomasyon"
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m stajbot init-db
```

Komutları her zaman sanal ortamın yorumlayıcısı ile çalıştırın: `.venv\Scripts\python -m stajbot ...`
(Linux/macOS: `.venv/bin/python -m stajbot ...`).

### `.env` dosyası

`.env.example` dosyasını `.env` olarak kopyalayın ve doldurun. Bu dosya gizli bilgi içerir; `.gitignore`
içindedir, asla paylaşmayın.

| Alan | Açıklama |
|---|---|
| `USER_FULL_NAME`, `USER_PHONE`, `USER_SCHOOL`, `USER_DEPARTMENT`, `USER_CLASS` | Mail şablonlarında kullanılan kimlik bilgileri |
| `INTERNSHIP_TERM`, `INTERNSHIP_START`, `INTERNSHIP_END` | Staj dönemi (ör. `2026-2027 Bahar`, `2027-02-15`, `2027-06-15`) |
| `TARGET_CITIES` | Hedef şehirler (virgülle). Boşsa varsayılan 10 şehir |
| `GOOGLE_PLACES_API_KEY` | Google Places (New) anahtarı — isteğe bağlı, aşağıya bakın |
| `ANTHROPIC_API_KEY`, `LLM_MODEL` | Kişiselleştirilmiş paragraf için — isteğe bağlı |
| `MAIL_MODE` | `dry` (sadece logla) / `draft` (Gmail taslağına koy) / `send` (gerçekten gönder) |
| `MAIL_USER`, `MAIL_PASSWORD`, `MAIL_FROM_NAME` | Gmail adresi ve **uygulama şifresi** |
| `DAILY_SEND_LIMIT`, `SEND_DELAY_MIN_SEC`, `SEND_DELAY_MAX_SEC` | Günlük limit (varsayılan 15) ve gönderimler arası rastgele bekleme (sn) |
| `SEND_WINDOW`, `SEND_WEEKDAYS_ONLY` | Mesai penceresi (`09:00-17:30`) ve yalnızca hafta içi |
| `FOLLOWUP_DAYS`, `MIN_DAYS_BETWEEN_MAILS` | Hatırlatma süresi (10 gün) ve aynı şirkete iki mail arası en az gün (14) |
| `ATTACHMENTS` | Ek dosyalar, varsayılan `data/cv.pdf` (varsa eklenir) |
| `LOG_LEVEL`, `DB_PATH`, `DATA_DIR`, `PROFILE_PATH`, `HTTP_TIMEOUT_SEC`, `LLM_MAX_TOKENS` | İleri ayarlar; varsayılanlar genelde yeterlidir |

### Gmail uygulama şifresi (zorunlu: `draft` / `send` modu ve `inbox` için)

Gmail normal şifreyle SMTP/IMAP girişine izin vermez; **uygulama şifresi** gerekir.

1. Google hesabınızda **2 Adımlı Doğrulama**'yı açın: <https://myaccount.google.com/security>
2. **Uygulama şifreleri** sayfasına gidin: <https://myaccount.google.com/apppasswords>
3. Uygulama adı olarak `StajBot` yazın, oluşturulan 16 haneli şifreyi (boşluksuz) `.env` içine
   `MAIL_PASSWORD=` olarak yazın; `MAIL_USER=adresiniz@gmail.com`.
4. Gmail ayarlarında IMAP'ın açık olduğundan emin olun: *Ayarlar → Tüm ayarları gör → Yönlendirme ve
   POP/IMAP → IMAP'ı etkinleştir*.
5. Önce `MAIL_MODE=draft` ile deneyin: taslaklar Gmail'in **Taslaklar** klasörüne düşer, oradan
   kontrol edip elle gönderebilirsiniz. Her şey yerindeyse `MAIL_MODE=send` yapın.

Okul e-postanız (ör. `@ogr.universite.edu.tr`) Google Workspace üzerindeyse aynı adımlar geçerlidir;
Workspace yöneticisi uygulama şifrelerini kapatmışsa kişisel Gmail kullanın.

### Google Places API anahtarı (isteğe bağlı, aylık birkaç dolar)

Google Places, hedef şehirlerdeki yazılım şirketlerini web sitesiyle birlikte bulur. Diğer kaynaklar
olmadan da sistem çalışır; anahtar yoksa bu kaynak uyarı vererek atlanır.

1. <https://console.cloud.google.com> adresinde proje oluşturun ve **faturalandırma hesabı** bağlayın
   (ücretsiz kota için de gerekir).
2. *API'ler ve Hizmetler → Kitaplık* bölümünden **Places API (New)** servisini etkinleştirin
   (eski "Places API" değil).
3. *Kimlik bilgileri → Kimlik bilgisi oluştur → API anahtarı*. Anahtarı **yalnızca Places API (New)**
   ile kısıtlayın.
4. `.env` içine `GOOGLE_PLACES_API_KEY=AIza...` yazın.
5. *Faturalandırma → Bütçeler ve uyarılar* bölümünde 5 $ gibi bir bütçe uyarısı tanımlayın.

Maliyet: tam bir tarama (10 şehir × ~8 sorgu × 3 sayfa) yaklaşık 250 istektir; ayda ~1000 istek
ücretsizdir, sonrası tarama başına ~8-9 $. Ham yanıtlar `data/google_places/` altında 7 gün önbellekte
tutulur; tekrar çalıştırma ücretsizdir. Ayrıntı: `stajbot/collectors/google_places.py` başındaki açıklama.

### Anthropic API anahtarı (isteğe bağlı)

Her şirket için "neden bu şirket" paragrafını LLM yazar (2-4 cümle, şirketin teknolojileri / sektörü ve
sizin profilinizden yola çıkar, uydurma bilgi yok). <https://console.anthropic.com> adresinden anahtar
alın, `.env` içine `ANTHROPIC_API_KEY=` yazın. Anahtar yoksa ya da `compose --no-llm` derseniz şablonun
sabit paragrafı kullanılır. Maliyet mail başına birkaç kuruştur.

### `data/profil.md`

LLM'in ve şablonların sizi tanıtırken kullandığı kısa profil: okul, bölüm, sınıf, bildiğiniz
teknolojiler, 2-3 proje, ilgi alanları, staj dönemi. `data/profil.example.md` dosyasını `data/profil.md`
olarak kopyalayıp düzenleyin (web arayüzündeki **/ayarlar** sayfasından da düzenlenebilir). CV'nizi
`data/cv.pdf` olarak koyarsanız maillere eklenir.

---

## Günlük akış

```powershell
# 1) Şirketleri topla (tüm kaynaklar, .env'deki hedef şehirler)
.venv\Scripts\python -m stajbot collect
#    tek kaynak / tek şehir:
.venv\Scripts\python -m stajbot collect --source teknopark --city Konya --city Ankara

# 2) Okulun protokol listesi ya da elle hazırladığınız CSV
.venv\Scripts\python -m stajbot import-csv data\imports\okul_protokol.csv --source school

# 3) Web sitelerini tara: e-posta, teknoloji, kariyer sayfası (hiç taranmamışlar, 50 tane)
.venv\Scripts\python -m stajbot enrich --only-pending --limit 50

# 4) Puanla
.venv\Scripts\python -m stajbot score

# 5) Web arayüzünü aç ve /inceleme sayfasında şirketleri Onayla / Reddet / Sonra
.venv\Scripts\python -m stajbot serve          # http://127.0.0.1:8000

# 6) Onaylı ve e-postası olan (status: Hazır) şirketler için mail taslağı üret
.venv\Scripts\python -m stajbot compose --all-ready
#    LLM olmadan: --no-llm ; tek şirket: --company-id 42

# 7) /mailler sayfasında taslakları önizle, düzenle, Onayla

# 8) Gönder (önce prova)
.venv\Scripts\python -m stajbot send --dry-run
.venv\Scripts\python -m stajbot send

# 9) Gelen kutusunu oku, cevapları eşleştir
.venv\Scripts\python -m stajbot inbox

# 10) Cevap vermeyenler için hatırlatma taslakları (FOLLOWUP_DAYS sonra)
.venv\Scripts\python -m stajbot followups

# Hızlı özet
.venv\Scripts\python -m stajbot stats
```

Kısaca: **collect → enrich → score → inceleme (web) → compose → onay (web) → send → inbox**. Her gün
`inbox`, `send` ve haftada bir `followups` çalıştırmak yeterlidir; cevap gelen şirketlerde telefon /
mülakat / teklif aşamalarını web arayüzünde elle işaretleyin.

### Komutlar

| Komut | Açıklama |
|---|---|
| `init-db` | Veritabanı tablolarını oluşturur (`data/stajbot.db`) |
| `collect [--source argem\|teknopark\|bilisim500\|google_places\|all] [--city X]... [--allow-non-target]` | Kaynaklardan toplar, tekilleştirerek kaydeder; bir kaynak hata verirse diğerleriyle devam eder |
| `import-csv DOSYA [--source csv\|school] [--city VARSAYILAN] [--allow-non-target]` | CSV ya da XLSX içe aktarır (UTF-8 / cp1254; Türkçe başlıklar olur); `school` okul protokolü bayrağını açar. `--print-template` şablon başlığını yazdırır |
| `enrich [--limit N] [--only-pending] [--company-id N]` | Web sitelerini tarar (e-posta, teknoloji, kariyer, büyüklük) |
| `score` | Tüm şirketlerin puanını yeniden hesaplar |
| `compose (--company-id N \| --all-ready) [--no-llm] [--limit N]` | Başvuru maili taslağı üretir (gönderilmez) |
| `send [--dry-run] [--force] [--limit N]` | Onaylı taslakları gönderir; `--force` mesai saati kontrolünü aşar |
| `inbox` | IMAP'tan yeni mesajları alır ve şirketlere eşleştirir |
| `followups [--limit N]` | Süresi dolanları "Cevap yok" yapar, hatırlatma taslağı üretir (onay bekler; hatırlatma 14 gün kuralından muaftır) |
| `serve [--host 127.0.0.1] [--port 8000] [--reload]` | Web arayüzü |
| `stats` | Sayıların Türkçe özeti |

Her komut hata durumunda `HATA: ...` satırı yazar ve sıfır dışı çıkış koduyla biter. Ayrıntılı log
`.env` içinde `LOG_LEVEL=DEBUG` ile stderr'e yazılır.

### Web arayüzü sayfaları

| Sayfa | İçerik |
|---|---|
| `/` | Pano: durum sayıları, bekleyen onay, bugün gönderilen, sahipsiz mail, yaklaşan hatırlatmalar, son hareketler |
| `/sirketler` | Liste; şehir / durum / inceleme / büyüklük / kaynak / puan filtreleri, arama, sıralama; `/sirketler.csv` dışa aktarma |
| `/sirketler/{id}` | Şirket kartı, kişiler (birincil seç / ekle / sil), kaynaklar, zaman çizelgesi, mailler; durum değiştir, not, alan düzenleme, kara liste, zenginleştir, taslak üret |
| `/inceleme` | Onay kuyruğu (puana göre): Onayla / Reddet / Sonra |
| `/mailler` | Taslaklar: düzenle, paragrafı yeniden üret, onayla, sil; onaylananlar / gönderilenler / hatalı sekmeleri; "Gönderimi başlat" |
| `/gelen` | Gelen mailler: eşleşen / sahipsiz; sahipsizi şirkete bağla; olumlu-olumsuz tahminini onayla ve durumu güncelle |
| `/istatistik` | Kaynak / şehir / sektör bazında gönderim ve cevap oranları |
| `/ayarlar` | `.env` özeti (gizli değerler maskeli) ve `data/profil.md` düzenleme |

### Kaynaklarla ilgili notlar

- **argem**: sanayi.gov.tr otomatik indirmeyi engelleyebilir. Komut önce `data/argem/*.xlsx`
  dosyalarına bakar; tarayıcınızla *Ar-Ge Merkezleri* ve *Tasarım Merkezleri* XLSX dosyalarını indirip
  bu klasöre koyun (adresler komut uyarısında yazılır). Yoksa Wayback Machine'den indirmeyi dener.
- **teknopark**: Teknopark İstanbul gibi bot korumalı siteler otomatik taranamaz; firma listesini
  CSV ile içe aktarın. Yapılandırma: `stajbot/collectors/teknoparklar.yaml`.
- **bilisim500**: Bilişim 500 listesi ücretlidir. YASAD ve TÜBİSAD üye listeleri ücretsiz taranır;
  listeyi satın alırsanız `data/manual/bilisim500_<yil>.csv` olarak koyun.
- **google_places**: anahtar yoksa atlanır; istek bütçesi ve önbellek vardır.

---

## Güvenlik ve nezaket notları

- **`MAIL_MODE`**
  - `dry`: hiçbir şey gönderilmez, ne yapılacağı loglanır (varsayılan — kurulumu bu modda deneyin).
  - `draft`: mail Gmail **Taslaklar** klasörüne konur; siz Gmail'den okuyup gönderirsiniz (veritabanında mail
    "gönderildi" sayılır; göndermekten vazgeçerseniz şirket sayfasından durumu düzeltin). Gerçek
    gönderime geçmeden önce birkaç gün bu modu kullanmanız önerilir.
  - `send`: SMTP ile gerçekten gönderir. Yalnızca `MailStatus.approved` (sizin onayladığınız) taslaklar
    gider.
- Günlük limit (`DAILY_SEND_LIMIT=15`), gönderimler arası 2-6 dakika rastgele bekleme ve mesai saati
  penceresi spam izlenimini önler ve Gmail'in günlük sınırlarına takılmanızı engeller. `--force` yalnızca
  saat kontrolünü aşar; limiti aşmaz.
- Aynı şirkete 14 gün içinde ikinci mail gitmez; kara listedeki ya da *Kapandı* / *Kabul ettim* /
  *Olumsuz* durumundaki şirketlere asla mail gitmez.
- Her mailin konusunda `[BV-0042]` biçiminde takip kodu bulunur; cevaplar bununla eşleştirilir.
  Eşleşmeyen mailler arayüzde "Sahipsiz" listesinde elle bağlanır.
- Toplayıcılar robots.txt'e uyar, istekler arasında bekler ve kendini `HTTP_USER_AGENT` ile tanıtır.
- Olumlu / olumsuz tahmini yalnızca bir **tahmindir**; durum değişikliğini arayüzde siz onaylayın.
- `.env`, `data/stajbot.db`, `data/cv.pdf`, `data/profil.md` kişisel veri içerir; `.gitignore`'dadır.
  Yedeğini alın, paylaşmayın.
- LLM yalnızca "neden bu şirket" paragrafını yazar ve şirketin sitesinden çıkarılan bilgiyi kullanır;
  gönderimden önce her taslağı okuyun — sorumluluk sizdedir.

---

## CSV içe aktarma şablonu

`python -m stajbot import-csv --print-template` komutu şablonu yazdırır. Dosya **UTF-8** (Excel'den
kaydederken "CSV UTF-8") olmalı; ayraç virgül, noktalı virgül ya da sekme olabilir. Yalnızca `name`
zorunludur; başlıklar İngilizce ya da Türkçe (`Şirket Adı`, `Web Sitesi`, `E-posta`, `Şehir`, `İlçe`,
`Adres`, `Telefon`, `Sektör`, `Açıklama`, `Yetkili`, `Etiketler`, `Not`) yazılabilir.

```csv
name,website,email,city,district,address,phone,sector,description,contact_name,tags,notes
Örnek Yazılım A.Ş.,https://www.ornekyazilim.com,kariyer@ornekyazilim.com,İstanbul,Maslak,,0212 000 00 00,Fintech,Ödeme sistemleri geliştirir,,"fintech;odeme",Okul listesinden
Konya Bilişim Ltd.,konyabilisim.com.tr,info@konyabilisim.com.tr,Konya,Selçuklu,Teknokent,,Yazılım,ERP çözümleri,Ayşe Yılmaz,erp,
```

| Sütun | Açıklama |
|---|---|
| `name` | Şirket adı (**zorunlu**) |
| `website` | Web sitesi; `https://` yazmasanız da olur. Tekilleştirme anahtarıdır |
| `email` | Bir ya da birden çok e-posta (`;` ile). `kariyer@`, `ik@`, `staj@` gibi adresler İK olarak önceliklenir |
| `city` | Şehir; boşsa `--city` ile verilen varsayılan kullanılır. Hedef şehir dışındakiler atlanır (`--allow-non-target` ile alınır) |
| `district`, `address`, `phone`, `sector`, `description` | İsteğe bağlı |
| `contact_name` | Yetkili kişi adı (şablonda hitap için) |
| `tags` | Serbest etiketler, `;` ile |
| `notes` | Kendi notunuz |

Örnek dosya: `tests/fixtures/cli/sablon.csv`. Okul protokol listesini `--source school` ile içe
aktarın; bu şirketlerde `school_protocol=True` olur ve puana +25 eklenir. Aynı dosyayı tekrar içe
aktarmak kayıt çoğaltmaz; yalnızca boş alanlar doldurulur.

---

## Geliştirme

```powershell
.venv\Scripts\python -m pytest -q              # ağ gerektirmeyen testler
.venv\Scripts\python -m pytest -q -m network   # canlı siteye çıkan testler
```

Mimari sözleşme ve modül sınırları için `MIMARI.md` dosyasına bakın.
