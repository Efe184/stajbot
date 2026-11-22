# Staj Başvuru Otomasyonu — Mimari Sözleşme

Bu dosya projedeki tüm modüllerin uyması gereken sözleşmedir. Modül geliştiren herkes
(insan ya da ajan) önce bunu okur. Burada tanımlı modeller, enum'lar ve arayüzler
`stajbot/models.py`, `stajbot/enums.py`, `stajbot/collectors/base.py` içinde kodlanmıştır;
bu dosyalar **paylaşılan sözleşmedir, modül ajanları bunları değiştirmez**. Değişiklik
gerekiyorsa çıktı raporunda belirtilir, entegrasyon aşamasında birleştirilir.

## 1. Amaç

Bilgisayar mühendisliği 4. sınıf öğrencisi için İşyeri Mesleki Eğitim (uzun dönem, yüz yüze)
staj başvurusu otomasyonu:

1. Şirketleri birden çok kaynaktan toplar, tekilleştirir, zenginleştirir (e-posta, teknoloji, büyüklük).
2. Kullanıcı onay kuyruğunda şirketleri onaylar/reddeder.
3. Her şirket için şablon + LLM ile kişiselleştirilmiş başvuru maili üretir, kullanıcı önizler ve onaylar.
4. Günlük limit ve bekleme ile gönderir (veya Gmail taslaklarına koyar).
5. Gelen kutusunu okur, cevapları şirkete eşleştirir, durumu günceller.
6. Kullanıcı telefon/mülakat/sunum/teklif gibi aşamaları elle işaretler; her şey zaman çizelgesinde tutulur.

Hedef şehirler (zorunlu filtre): Konya, Ankara, İstanbul, İzmir, Eskişehir, Bursa, Sakarya,
Antalya, Mersin, Afyonkarahisar. Bu şehirlerin dışındaki şirketler havuza alınmaz
(`enums.TARGET_CITIES`).

## 2. Teknoloji

- Python 3.13, tek sanal ortam (`.venv`), bağımlılıklar `requirements.txt` içinde (ajanlar bu dosyayı
  değiştirmez; ek bağımlılık ihtiyacını raporlar).
- Veritabanı: SQLite, dosya `data/stajbot.db`, ORM: SQLAlchemy 2.0 (declarative, `Mapped` tipleri).
- Web: FastAPI + Jinja2 şablonları + HTMX (CDN) + Pico.css (CDN). JS framework yok.
- HTTP istemci: `httpx`; HTML ayrıştırma: `selectolax` (hızlı) veya `beautifulsoup4`.
- LLM: `anthropic` SDK. Varsayılan model `settings.llm_model`.
- E-posta: stdlib `smtplib` / `imaplib` / `email` (Gmail uygulama şifresi ile).
- CLI: `typer`. Loglama: stdlib `logging`, `stajbot.log.get_logger(name)`.
- Test: `pytest`; testler `tests/test_<modül>.py`. Ağa çıkan testler `@pytest.mark.network` ile işaretlenir ve
  varsayılan olarak çalışmaz (`pytest -m network` ile çalışır). Birim testleri yerel HTML/JSON fikstürleri kullanır
  (`tests/fixtures/`).
- Ayarlar: `pydantic-settings`, `.env` dosyasından (`stajbot/config.py`). Gizli bilgi koda yazılmaz.
- Dil: kod ve tanımlayıcılar İngilizce; kullanıcıya görünen metinler, arayüz ve docstring'ler Türkçe.
- Kodlama: tüm dosyalar UTF-8. Windows'ta çalışır; yol işlemlerinde `pathlib`.

## 3. Dizin yapısı

```
stajbot/
  config.py          ayarlar (.env)
  db.py              engine, SessionLocal, init_db(), get_session()
  enums.py           tüm enum'lar, TARGET_CITIES, şehir normalizasyonu
  models.py          SQLAlchemy modelleri
  utils.py           domain_of, normalize_url, slugify, normalize_name, now_utc
  log.py             get_logger
  collectors/
    base.py          RawCompany dataclass + Collector protokolü      [sözleşme]
    ingest.py        normalize + tekilleştir + kaydet (RawCompany -> Company)
    argem.py         Sanayi Bakanlığı Ar-Ge / Tasarım merkezleri listesi
    teknopark.py     teknopark firma listeleri (config: teknoparklar.yaml)
    bilisim500.py    Bilişim 500 listesi
    google_places.py Google Places API (New) text search
    csv_import.py    CSV/XLSX içe aktarma (okul protokol listesi, elle toplanan)
  enrich/
    website.py       şirket sitesini tara: e-postalar, teknolojiler, kariyer sayfası, büyüklük ipucu
    keywords.py      teknoloji / sektör anahtar kelime sözlüğü
    scoring.py       otomatik puan hesaplama
  mail/
    compose.py       şablon + LLM paragraf -> EmailMessage (draft)
    llm.py           Anthropic çağrısı (tek fonksiyon), profil.md okuma
    templates/       Jinja e-posta şablonları (sektöre göre)
    sender.py        SMTP gönderim, günlük limit, bekleme, taslak modu, tracking code
    inbox.py         IMAP okuma, gelen mesajı kaydetme
    matcher.py       gelen mesajı şirkete eşleştirme + olumlu/olumsuz tahmini
  web/
    app.py           FastAPI uygulaması, router kaydı, templates ayarı
    deps.py          get_db dependency
    routes/          companies.py, review.py, mail.py, inbox.py, stats.py, settings_page.py (/ayarlar)
    templates/       base.html + sayfalar
    static/
  services.py        modülleri birleştiren üst düzey işlemler (status değiştir + event yaz vb.)
  cli.py             typer komutları
data/
  stajbot.db, profil.md, cv.pdf (kullanıcı ekler), profil.example.md (örnek profil), imports/ (CSV'ler)
tests/
docs/
```

## 4. Veri modeli (models.py — sözleşme)

### Company
Bir şirket = bir başvuru. Ayrı Application tablosu yok.

| alan | tip | açıklama |
|---|---|---|
| id | int PK | |
| name | str | görünen ad |
| domain | str, unique, nullable | `sirket.com.tr` biçiminde normalize (www yok, küçük harf). Tekilleştirme anahtarı |
| website | str nullable | tam URL |
| city | str nullable | normalize edilmiş (`enums.normalize_city`) |
| district | str nullable | |
| address | text nullable | |
| phone | str nullable | |
| sector | str nullable | serbest metin (ör. "Savunma", "Fintech") |
| tags | JSON list[str] | serbest etiketler |
| tech_stack | JSON list[str] | tespit edilen teknolojiler |
| description | text nullable | |
| size_bucket | enum SizeBucket | unknown / s1_10 / s11_50 / s51_200 / s201_1000 / s1000_plus |
| employee_count | int nullable | biliniyorsa |
| founded_year | int nullable | |
| linkedin_url | str nullable | |
| career_url | str nullable | |
| has_internship_posting | bool | |
| internship_posting_url | str nullable | |
| is_arge_center | bool | |
| is_teknopark | bool | |
| teknopark_name | str nullable | |
| school_protocol | bool | okulla daha önce protokol imzalamış |
| priority | int 1-5 (default 3) | kullanıcı puanı |
| score | int 0-100 (default 0) | otomatik puan (scoring.py) |
| review_status | enum ReviewStatus | pending / approved / rejected / later |
| status | enum AppStatus | başvuru durumu (aşağıda) |
| blacklisted | bool | bir daha mail gitmez |
| notes | text nullable | |
| enriched_at | datetime nullable | website.py en son ne zaman çalıştı |
| last_contact_at | datetime nullable | son gönderilen/alınan mail |
| next_followup_at | datetime nullable | hatırlatma zamanı |
| created_at, updated_at | datetime | |

İlişkiler: `sources` (CompanySource), `contacts` (Contact), `events` (Event), `emails` (EmailMessage).

### CompanySource
`company_id, source (str: argem/teknopark/bilisim500/google_places/csv/school/manual/...), source_url, raw (JSON), collected_at`.
Unique `(company_id, source)`.

### Contact
`company_id, email (lowercase), name, role, kind (enum ContactKind: hr/info/personal/other), priority (int, düşük=iyi), is_primary (bool), source (str), created_at`. Unique `(company_id, email)`.

### Event (zaman çizelgesi)
`company_id, type (enum EventType), old_status, new_status, title, body, occurred_at, created_by (enum Actor: system/user), email_message_id (nullable FK)`.
Tüm durum değişiklikleri **yalnızca** `services.change_status()` üzerinden yapılır ve bir Event yazar.

### EmailMessage
`company_id (nullable), direction (enum Direction: outbound/inbound), message_id (RFC Message-ID header), in_reply_to, references_header, tracking_code (ör. BV-0042), from_addr, to_addr, cc, subject, body_text, body_html, status (enum MailStatus: draft/approved/queued/sent/failed/received), imap_uid, matched_by (enum MatchMethod: domain/message_id/tracking_code/manual/none), sentiment (enum Sentiment: positive/negative/info/unknown), llm_paragraph (text, nullable), template_name, error, created_at, sent_at, received_at`.

### AppSetting
`key (PK), value (str)` — çalışma zamanı ayarları (günlük sayaç, son IMAP UID vb.).

## 5. Enum'lar (enums.py)

```
ReviewStatus: pending, approved, rejected, later
AppStatus:
  found            Bulundu (toplandı, henüz onaylanmadı/mail yok)
  ready            Hazır (onaylandı, e-posta var, mail üretilebilir)
  drafted          Taslak üretildi, kullanıcı onayı bekliyor
  queued           Gönderim kuyruğunda (onaylandı)
  sent             Gönderildi
  no_reply         Cevap yok (N gün geçti)
  followup_sent    Hatırlatma gönderildi
  replied          Cevap geldi (sınıflandırılmadı)
  positive         Olumlu cevap
  negative         Olumsuz cevap
  info_requested   Bilgi/belge istediler
  phone_call       Telefon görüşmesi yapıldı
  interview        Mülakata çağrıldı / yapıldı
  presentation     Sunum / teknik görüşme
  offer            Teklif geldi
  accepted         Kabul ettim
  closed           Kapandı (iptal, kara liste, vazgeçtim)
SizeBucket, ContactKind, EventType(status_change, email_sent, email_received, note, call, interview, presentation, offer, enrich, collect, manual), Actor, Direction, MailStatus, MatchMethod, Sentiment
```
Otomatik geçişler (sistem): found→ready (onay + e-posta), ready→drafted, drafted→queued→sent, sent→replied, sent→no_reply, no_reply→followup_sent.
Elle geçişler (kullanıcı): her şey; arayüz her durumdan her duruma izin verir ama uyarı gösterir.

## 6. Collector arayüzü (collectors/base.py — sözleşme)

```python
@dataclass
class RawCompany:
    name: str
    source: str                   # 'argem', 'teknopark', ...
    source_url: str | None = None
    website: str | None = None
    city: str | None = None       # ham; ingest normalize eder
    district: str | None = None
    address: str | None = None
    phone: str | None = None
    sector: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    employee_count: int | None = None
    linkedin_url: str | None = None
    is_arge_center: bool = False
    is_teknopark: bool = False
    teknopark_name: str | None = None
    internship_posting_url: str | None = None
    school_protocol: bool = False
    raw: dict = field(default_factory=dict)

class Collector(Protocol):
    name: str
    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]: ...
```

Kurallar:
- Collector ağa çıkar ama veritabanına **yazmaz**. Yazma işi `ingest.ingest(raw_iter, session) -> IngestReport`.
- Her collector `httpx.Client` ile, `settings.http_user_agent`, zaman aşımı 20 sn, istekler arası `settings.crawl_delay_sec` bekleme, robots.txt saygısı (basit kontrol yeterli).
- Collector hata alırsa o kaynağı loglar ve **boş döner**, exception fırlatmaz (CLI diğer kaynaklarla devam eder).
- Şehir filtresi: collector mümkünse kaynakta filtreler, değilse hepsini üretir; **ingest** hedef şehir dışını atar (`IngestReport.skipped_city`).

### ingest.ingest(raws, session, *, allow_non_target=False) -> IngestReport
- domain çıkar (`utils.domain_of(url)`), yoksa `(normalize_name(name), city)` ile eşle.
- Varsa: boş alanları doldur (asla dolu alanı üzerine yazma), CompanySource ekle, Contact'ları ekle, bool bayrakları OR'la.
- Yoksa: Company oluştur (status=found, review_status=pending), Event(type=collect).
- `IngestReport(created, updated, skipped_city, skipped_invalid, errors: list[str])`.
- Uygulama notları (entegrasyon): domain anahtarı web sitesinden, yoksa genel olmayan (gmail vb. dışı) bir e-posta
  alan adından türetilir; linkedin/facebook gibi sosyal alan adları anahtar sayılmaz. Her kayıt bir SAVEPOINT içinde
  işlenir: biri patlarsa yalnızca o kayıt `errors`'a düşer, akış sürer. Hedef şehir listesi `settings.target_cities`.

### csv_import.read_file(path, *, source="csv", default_city=None) -> list[RawCompany]
CSV (virgül / noktalı virgül / sekme; UTF-8 ya da cp1254) ve XLSX (ilk sayfa, başlık ilk 10 satırda aranır) okur.
Başlıklar İngilizce ya da Türkçe olabilir; yalnızca ad zorunludur. `source="school"` ise `school_protocol=True` ve
`okul-protokol` etiketi eklenir. CLI `import-csv` bu fonksiyonu kullanır.

## 7. Zenginleştirme (enrich/)

`website.enrich_company(company, session, client=None) -> EnrichResult`
- Ana sayfa + bulduğu `iletisim|contact|kariyer|career|hakkimizda|about|insan-kaynaklari` sayfaları (en çok 6 sayfa).
- E-postalar: regex + `mailto:`; `data-cfemail` Cloudflare çözümü; obfuscation (`[at]`, `(at)`) çöz.
  ContactKind atama: `kariyer|career|ik|hr|insankaynaklari|staj|intern` → hr (priority 1), kişisel görünüm (ad.soyad@) → personal (2), `info|iletisim|contact` → info (3), diğer → other (4). En düşük priority `is_primary`.
- Teknolojiler: `enrich/keywords.py` içindeki anahtar kelime sözlüğü (Python, Java, C#, .NET, React, Angular, Node, Go, Rust, C++, Embedded, FPGA, Android, iOS, Flutter, Unity, Unreal, AWS, Azure, Kubernetes, Docker, SAP, Oracle, Siber güvenlik, Yapay zeka, Makine öğrenmesi, Veri bilimi, ERP, Fintech, Savunma, Oyun, Sağlık, E-ticaret, IoT, Blockchain ...). Sektör etiketi de aynı sözlükten.
- `career_url`, `has_internship_posting` (staj/stajyer/intern kelimesi kariyer sayfasında).
- `linkedin_url` (sayfadaki linkedin.com/company linki).
- Büyüklük ipucu: "500+ çalışan", "100'den fazla mühendis" gibi kalıplar → employee_count.
- Robots.txt'e uy, sayfa başına 1-2 sn bekle, sadece aynı domain.
- Sonuç Company'ye yazılır (dolu alanlar üzerine yazılmaz), `enriched_at` güncellenir, Event(type=enrich) eklenir.

`scoring.compute_score(company, profile_techs: list[str] | None = None) -> int` (0-100), deterministik, `scoring.explain(company, profile_techs=None) -> list[str]`.
Ağırlıklar: hedef şehir zorunlu (değilse 0); kaynak sayısı ≥2 (+10); staj ilanı (+25); Ar-Ge merkezi (+15); teknopark (+10); okul protokolü (+25); HR e-postası var (+15), yalnızca info var (+5); tech_stack ∩ profil teknolojileri (her biri +5, en çok +20); büyüklük 11-1000 arası (+5). Sonuç 0-100'e sıkıştırılır.

## 8. Mail (mail/)

### Profil
`data/profil.md`: kullanıcının kısa profili (teknolojiler, projeler, ilgi alanları, okul, dönem). `llm.load_profile()` okur; `required=True` (varsayılan) ise yoksa `ProfileMissingError` ile `data/profil.example.md` dosyasının kopyalanması söylenir. `compose` dosyayı `required=False` ile okur: profil yoksa Türkçe uyarı loglanır, LLM paragrafı üretilmez ve şablonun sabit paragrafı kullanılır (taslak üretimi durmaz). Web arayüzünde `/ayarlar` sayfasından yazılabilir.

### compose.compose_for_company(company, session, *, use_llm=True) -> ComposeResult
- Şablon seçimi: `templates/` içinde `default.txt.j2`, `startup.txt.j2`, `corporate.txt.j2`, `defense.txt.j2`, `game.txt.j2`, `followup.txt.j2`; seçim `pick_template(company)` ile sektör/tags/size_bucket'a göre.
- Şablon değişkenleri: `company`, `contact_name`, `profile`, `why_paragraph`, `settings` (ad soyad, okul, bölüm, dönem tarihleri, telefon).
- `why_paragraph`: `llm.generate_why_paragraph(company, profile) -> str` (2-4 cümle, Türkçe, resmi-samimi, uydurma bilgi yok; şirketin tech_stack/sector/description'ından yola çıkar; bunlar boşsa genel ama şirket adını kullanan bir paragraf). `use_llm=False`, API anahtarı yoksa, profil boşsa ya da çağrı başarısız / reddedilirse (`LLMError`) şablonun sabit paragrafı kullanılır. Şablon bağlamına ayrıca `techs` (profilden çıkarılan teknolojiler) ve `tracking_code` verilir.
- Konu: `"Uzun Dönem Staj Başvurusu - {Ad Soyad} [{tracking_code}]"`. Tracking code: `BV-{company.id:04d}`.
- Alıcı: `is_primary` Contact; yoksa `ComposeResult(ok=False, error=...)` döner ve company `ready` olamaz.
- Mesaj `MailStatus.draft` olarak kaydedilir, company status → `drafted`. Aynı şirket için eski draft varsa üzerine yazılır (yeniden üret).
- Ekler: `settings.attachments` (varsayılan `data/cv.pdf` varsa).

### sender.send_approved(session, *, dry_run=False, force=False, limit=None) -> SendReport
- `MailStatus.approved` mesajları alır; günlük limit `settings.daily_send_limit` (varsayılan 15); gönderimler arası `random.uniform(settings.send_delay_min_sec, settings.send_delay_max_sec)`.
- Mesai saati kontrolü `settings.send_window` (ör. `09:00-17:30`, hafta içi) — dışındaysa atlar ve raporlar (`force=True` ile aşılır).
- `settings.mail_mode`: `send` (SMTP gönder) | `draft` (Gmail IMAP `[Gmail]/Drafts` klasörüne APPEND ile taslak koy; veritabanında mesaj yine `sent` sayılır ve Event gövdesi Gmail'den gönderilmesi gerektiğini söyler) | `dry` (sadece logla, veritabanı değişmez). `dry_run=True` de veritabanını değiştirmez; rapordaki `sent` "gönderilecek" sayısıdır.
- Message-ID üretir ve kaydeder; `sent_at`, company status → `sent`, `last_contact_at`, `next_followup_at = now + settings.followup_days`; Event(email_sent).
- Blacklisted veya status `closed/accepted` şirkete asla göndermez.
- Aynı şirkete `settings.min_days_between_mails` (14) içinde ikinci başvuru maili göndermez. Hatırlatma (`template_name == "followup"`) bu kuraldan muaftır (kullanıcı onaylamıştır ve `followup_days` < `min_days_between_mails` olabilir); gönderilince `no_reply -> followup_sent`.
- `send_approved(session, *, dry_run, force, limit, now=None, sleeper=time.sleep, smtp_factory=None, imap_factory=None)`; testler sahte SMTP/IMAP enjekte eder. Günlük sayaç veritabanından (bugün `sent` olan mesajlar) hesaplanır.

### inbox.fetch_new(session, *, imap_factory=None) -> list[EmailMessage]
- IMAP ile `INBOX`, son işlenen UID `AppSetting['imap_last_uid']` sonrası. Kendi adresimizden gelenler atlanır; bağlantı hatasında loglar ve boş liste döner.
- Her mesajı `EmailMessage(direction=inbound, status=received)` kaydeder (tekil: message_id).
- Sonra `matcher.match(message, session)`.

### matcher.match(msg, session) -> MatchResult
Sıra: (1) `In-Reply-To`/`References` → bizim Message-ID; (2) konu/gövdedeki `BV-\d{4}`; (3) gönderen domain → Company.domain veya Contact e-posta domain'i (gmail/hotmail/outlook/yandex gibi genel domainler hariç). Eşleşirse `company_id`, `matched_by`; company status `sent/no_reply/followup_sent` ise → `replied`, Event(email_received). Eşleşmezse `matched_by=none`, arayüzde "Sahipsiz" listesinde.
`matcher.guess_sentiment(text) -> Sentiment`: anahtar kelime sözlüğü (olumsuz: maalesef, uygun değil, kontenjan, ileriki dönem, değerlendirilmemiştir...; olumlu: görüşme, mülakat, müsait, telefon, davet, ne zaman, uygun olur...; info: belge, transkript, protokol, form, cv'nizi). Yalnızca tahmin; kullanıcı onaylar.

### Hatırlatma
`services.mark_no_reply(session)`: `sent` + `next_followup_at < now` → `no_reply`. Hatırlatma maili `compose.compose_followup(company, session)` ile `followup.txt.j2` şablonundan üretilir (draft, kullanıcı onaylar).

## 9. Servisler (services.py)

```
change_status(session, company, new_status, *, actor, title=None, body=None) -> Event
add_note(session, company, body, actor=Actor.user) -> Event
approve_company(session, company) / reject_company / defer_company
set_primary_contact(session, company, contact)
add_contact(session, company, email, *, name=None, role=None, kind=None, source="manual") -> Contact
mark_no_reply(session) -> int
assign_message_to_company(session, msg, company) (manuel eşleştirme)
set_blacklist(session, company, flag: bool)
```

## 10. Web arayüzü (web/)

Sayfalar (Türkçe, Pico.css + HTMX):
- `/` Pano: durum sayıları, bugün gönderilen, bekleyen onay, sahipsiz mail, yaklaşan hatırlatma.
- `/sirketler` Liste: şehir, durum, inceleme durumu, büyüklük, kaynak, puan filtreleri; arama; sıralama.
- `/sirketler/{id}` Detay: kart, kişiler (birincil seç, ekle), kaynaklar, teknolojiler, zaman çizelgesi, mailler; durum değiştir (select + not), not ekle, elle alan düzenleme (çalışan sayısı, LinkedIn, öncelik, protokol), "LinkedIn'de ara" ve "Google'da ara" linkleri, kara liste.
- `/inceleme` Onay kuyruğu: pending şirketler puana göre; kartta özet + Onayla / Reddet / Sonra (HTMX, sayfa yenilenmez).
- `/mailler` Taslaklar: önizleme, düzenle (konu/gövde textarea), "Paragrafı yeniden üret", Onayla, Sil; onaylananlar ve gönderilenler sekmeleri; "Gönderimi başlat" butonu (arka planda `sender.send_approved`).
- `/gelen` Gelen kutusu: eşleşen/sahipsiz; sahipsiz için şirket seçip bağla; sentiment onayı.
- `/istatistik`: kaynak/şehir/sektör bazında gönderim ve cevap oranları (basit tablo, grafik şart değil).
- `/ayarlar`: `.env` özetini salt okunur gösterir (gizli değerler maskeli) + profil.md düzenleme.
- HTMX parçaları `routes/*.py` içinde; JSON API zorunlu değil.

## 11. CLI (cli.py)

```
python -m stajbot init-db
python -m stajbot collect [--source argem|teknopark|bilisim500|google_places|all] [--city ...]
python -m stajbot import-csv path.csv [--source csv|school]
python -m stajbot enrich [--limit N] [--only-pending]
python -m stajbot score
python -m stajbot compose [--company-id N | --all-ready] [--no-llm]
python -m stajbot send [--dry-run] [--force]
python -m stajbot inbox
python -m stajbot followups
python -m stajbot serve [--port 8000]
```
Giriş noktası: `stajbot/__main__.py`.

## 12. Ayarlar (.env.example)

```
APP_NAME, DB_PATH=data/stajbot.db, DATA_DIR=data, LOG_LEVEL=INFO, PROFILE_PATH=data/profil.md
USER_FULL_NAME, USER_PHONE, USER_SCHOOL, USER_DEPARTMENT, USER_CLASS="4. sınıf", INTERNSHIP_TERM="2026-2027 Bahar", INTERNSHIP_START, INTERNSHIP_END
TARGET_CITIES (virgülle; boşsa enums varsayılanı)
HTTP_USER_AGENT, CRAWL_DELAY_SEC=1.5, HTTP_TIMEOUT_SEC=20
GOOGLE_PLACES_API_KEY
ANTHROPIC_API_KEY, LLM_MODEL=claude-sonnet-5-5, LLM_MAX_TOKENS=400
MAIL_MODE=dry|draft|send, SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, IMAP_HOST=imap.gmail.com, MAIL_USER, MAIL_PASSWORD (uygulama şifresi), MAIL_FROM_NAME
DAILY_SEND_LIMIT=15, SEND_DELAY_MIN_SEC=120, SEND_DELAY_MAX_SEC=360, SEND_WINDOW=09:00-17:30, SEND_WEEKDAYS_ONLY=true
FOLLOWUP_DAYS=10, MIN_DAYS_BETWEEN_MAILS=14
ATTACHMENTS=data/cv.pdf
```

## 13. Kalite kuralları

- Her modülün `tests/test_<modül>.py` dosyası vardır; ağ gerektirmeyen testler fikstürle çalışır.
- Hiçbir modül `print` kullanmaz (CLI hariç, orada `typer.echo`); `log.get_logger(__name__)`.
- Ağ hataları yakalanır, loglanır, akış durmaz.
- Veritabanına yazan her işlem bir `Session` alır; modüller kendi engine'ini oluşturmaz (`db.get_session()` kullanır). Testler `db.make_engine("sqlite:///:memory:")` + `db.init_db(engine)` kullanır.
- Kullanıcıya görünen tüm metin Türkçe.
- Paylaşılan dosyalar (models.py, enums.py, config.py, db.py, utils.py, log.py, collectors/base.py, requirements.txt) yalnızca entegrasyon aşamasında değiştirilir.

## 14. Entegrasyon notları (paylaşılan dosyalarda yapılan değişiklikler)

Entegrasyon aşamasında modül raporlarına göre uygulanan sözleşme değişiklikleri:

- `utils.normalize_name`: "anonim şirketi" / "limited şirketi" çok kelimeli ekleri de siler (`anonim`, `sirketi`,
  `limited` tek tek de ek listesinde); böylece "X A.Ş." ile "X Anonim Şirketi" aynı anahtara iner.
- `utils.normalize_url`: http(s) dışı şemalar (`mailto:`, `javascript:`, `tel:`, `ftp:`, `//host`) için None döner;
  `http://https://x` çift şema yazımı düzeltilir; çevreleyen tırnaklar temizlenir.
- `enums.is_target_city`: hedef listesindeki None/boş girdiler yok sayılır (önceden `ascii_fold(None)` patlıyordu);
  `targets=None` varsayılan `TARGET_CITIES`, açıkça verilen boş liste ise "hiçbir şehir hedef değil" demektir
  (artık varsayılana düşmez).
- `log.setup_logging`: seviye `.env` içindeki `LOG_LEVEL`'den okunur (`LOG_LEVEL=DEBUG` hata izlerini gösterir).
- `services.add_contact`: `primary_contact` özelliği birincil kişi yoksa ilk kişiye düştüğü için yeni kişi listeye
  eklendikten sonra bakıldığında "mevcut birincil" olarak kendisi görünüyor ve `is_primary` hiç yazılmıyordu;
  mevcut birincil kişi artık yeni kişi eklenmeden önce belirlenir.
- `routes/settings_page.py` (`/ayarlar`) ve `collectors/csv_import.py` eklendi; `enrich/website.py` HTML için
  `selectolax.lexbor` kullanır (Modest arka ucu selectolax 1.0'da kaldırıldı).
- Reddedilen istek: `enums._DISTRICT_TO_CITY` / `city_from_text`'in ortak modüle taşınması (üç toplayıcıda test
  edilmiş yerel kopyalar var; davranış riski, sonraya) — toplayıcılar kendi eşlemelerini kullanmaya devam eder.
