"""Teknoloji ve sektör anahtar kelime sözlüğü (MIMARI.md bölüm 7).

Eşleştirme ASCII'ye indirgenmiş, küçük harfli metin üzerinde yapılır (``enums.ascii_fold``); desenler de
bu biçimde yazılır ("yapay zeka", "siber guvenlik"). Kanonik adlar kullanıcıya görünür (Türkçe).
"""
from __future__ import annotations

import re
from typing import Iterable

from ..enums import ascii_fold

# kanonik ad -> desenler (ASCII, küçük harf)
TECH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Python": (r"\bpython\b", r"\bdjango\b", r"\bflask\b", r"\bfastapi\b"),
    "Java": (r"\bjava\b(?!\s*script)", r"\bspring\s*boot\b", r"\bjakarta\s*ee\b", r"\bkotlin\b"),
    "C#": (r"\bc#", r"\bcsharp\b", r"\bc\s*sharp\b"),
    ".NET": (r"(?<![a-z])\.net\b", r"\bdotnet\b", r"\basp\.net\b", r"\bblazor\b"),
    "C++": (r"\bc\+\+", r"\bcpp\b"),
    "C": (r"(?<![a-z+#.])\bc\s*(?:/|ve|and|,)\s*c\+\+", r"\bembedded\s*c\b", r"\bansi\s*c\b"),
    "JavaScript": (r"\bjavascript\b", r"\btypescript\b", r"\bvue(?:\.js)?\b", r"\bnext\.js\b", r"\bsvelte\b"),
    "React": (r"\breact(?:\.js|js)?\b", r"\breact\s*native\b"),
    "Angular": (r"\bangular(?:js)?\b",),
    "Node.js": (r"\bnode(?:\.js|js)\b", r"\bexpress\.js\b", r"\bnestjs\b"),
    "Go": (r"\bgolang\b", r"\bgo\s*(?:lang|dili|programlama|language)\b"),
    "Rust": (r"\brust\b(?!\s*(?:proof|-?free|leme|lenme|pas))",),
    "PHP": (r"\bphp\b", r"\blaravel\b", r"\bsymfony\b"),
    "SQL": (r"\bsql\b", r"\bpostgres(?:ql)?\b", r"\bmysql\b", r"\bmssql\b", r"\bveri\s*taban"),
    "NoSQL": (r"\bmongodb\b", r"\bredis\b", r"\bcassandra\b", r"\belasticsearch\b", r"\bnosql\b"),
    "Gömülü sistemler": (r"\bembedded\b", r"\bgomulu\b", r"\bfirmware\b", r"\bmikrodenetleyici\b", r"\bmicrocontroller\b", r"\bstm32\b", r"\barm\s*cortex\b", r"\brtos\b"),
    "FPGA": (r"\bfpga\b", r"\bvhdl\b", r"\bverilog\b"),
    "Android": (r"\bandroid\b",),
    # "SWIFT: TRXX" banka kodu, "swift kodu" ve benzerleri iOS değildir
    "iOS": (r"\bios\b", r"\bswift\b(?!\s*(?::|#|code|kodu|kod|hesap|transfer|no\b|numara|bic))", r"\biphone\s*uygulama", r"\bswiftui\b"),
    "Flutter": (r"\bflutter\b", r"\bdart\b"),
    "Unity": (r"\bunity\s*(?:3d|engine|oyun|game)?\b",),
    "Unreal": (r"\bunreal(?:\s*engine)?\b",),
    "AWS": (r"\baws\b", r"\bamazon\s*web\s*services\b"),
    "Azure": (r"\bazure\b",),
    "Google Cloud": (r"\bgoogle\s*cloud\b", r"\bgcp\b"),
    "Kubernetes": (r"\bkubernetes\b", r"\bk8s\b", r"\bopenshift\b"),
    # "container"/"konteyner" tek başına lojistik anlamına da gelir; yalnızca yazılım bağlamında sayılır
    "Docker": (r"\bdocker\b", r"\bcontaineriz", r"\bcontainer\s*(?:orchestration|platform|image|registry)", r"\bkonteyner\s*(?:orkestrasyon|teknoloji|tabanli|mimari)", r"\bpodman\b"),
    "DevOps": (r"\bdevops\b", r"\bci/cd\b", r"\bjenkins\b", r"\bgitlab\s*ci\b", r"\bterraform\b", r"\bansible\b"),
    "SAP": (r"\bsap\b", r"\babap\b"),
    "Oracle": (r"\boracle\b",),
    "Siber güvenlik": (r"\bsiber\s*guvenli", r"\bcyber\s*security\b", r"\bbilgi\s*guvenli", r"\bsizma\s*test", r"\bpenetration\s*test", r"\bsoc\s*(?:analist|analyst|hizmet)", r"\bzafiyet\b"),
    "Yapay zeka": (r"\byapay\s*zeka\b", r"\bartificial\s*intelligence\b", r"(?<![a-z])\bai\b(?!\s*(?:r|rport|rline))", r"\bllm\b", r"\bgenerative\s*ai\b", r"\buretken\s*yapay\b", r"\bdoga?l\s*dil\s*isleme", r"\bnlp\b"),
    "Makine öğrenmesi": (r"\bmakine\s*ogrenme", r"\bmachine\s*learning\b", r"\bderin\s*ogrenme", r"\bdeep\s*learning\b", r"\btensorflow\b", r"\bpytorch\b", r"\bscikit", r"\bcomputer\s*vision\b", r"\bgoruntu\s*isleme", r"\bbilgisayarli\s*goru"),
    "Veri bilimi": (r"\bveri\s*bilim", r"\bdata\s*scien", r"\bbig\s*data\b", r"\bbuyuk\s*veri\b", r"\bveri\s*analiti", r"\bdata\s*analyt", r"\bveri\s*muhendis", r"\bdata\s*engineer", r"\bpower\s*bi\b", r"\bspark\b", r"\bhadoop\b"),
    "ERP": (r"\berp\b", r"\bkurumsal\s*kaynak\s*planlama", r"\bcrm\b", r"\bmrp\b", r"\blogo\s*(?:yazilim|tiger|netsis)", r"\bnetsis\b", r"\bmikro\s*yazilim\b"),
    "Fintech": (r"\bfintech\b", r"\bodeme\s*sistem", r"\bpayment\b", r"\bbankacilik\b", r"\bbanking\b", r"\bfinans(?:al)?\s*teknoloji", r"\bpos\b", r"\bsanal\s*pos\b", r"\be-?fatura\b"),
    "Savunma": (r"\bsavunma\b", r"\bdefen[cs]e\b", r"\bmilitary\b", r"\baskeri\b", r"\bhavacilik\b", r"\baerospace\b", r"\bradar\b", r"\binsansiz\s*hava\b", r"\biha\b", r"\bsiha\b", r"\bnato\b", r"\bssb\b", r"\bsavunma\s*sanayi"),
    "Oyun": (r"\boyun\s*(?:gelistir|studyo|motoru|sirketi|teknoloji|tasarim)", r"\bgame\s*(?:dev|studio|development|engine)", r"\bmobil\s*oyun\b", r"\bmobile\s*game", r"\bgaming\b", r"\bvideo\s*game"),
    "Sağlık": (r"\bsaglik\s*(?:bilisim|teknoloji|yazilim|sektor)", r"\bmedikal\b", r"\bmedical\b", r"\bhealth\s*(?:tech|care|it)", r"\bhastane\s*bilgi\s*(?:yonetim|sistem)", r"\bhbys\b", r"\btibbi\s*cihaz", r"\bbiyomedikal\b"),
    "E-ticaret": (r"\be-?\s*ticaret\b", r"\be-?\s*commerce\b", r"\bpazaryeri\b", r"\bmarketplace\b", r"\bonline\s*(?:satis|magaza|alisveris)", r"\bshopify\b", r"\bwoocommerce\b"),
    "IoT": (r"\biot\b", r"\binternet\s*of\s*things\b", r"\bnesnelerin\s*interneti\b", r"\bakilli\s*(?:sehir|ev|fabrika|tarim|sayac)", r"\bendustri\s*4\.0\b", r"\bindustry\s*4\.0\b", r"\bm2m\b"),
    "Blockchain": (r"\bblockchain\b", r"\bblok\s*zincir", r"\bweb3\b", r"\bsmart\s*contract", r"\bakilli\s*sozlesme", r"\bkripto\b", r"\bcrypto\b"),
    "Robotik": (r"\brobotik\b", r"\brobotics\b", r"\botonom\b", r"\bautonomous\b", r"\bros\s*2?\b(?=.*robot)", r"\bendustriyel\s*robot"),
    "Otomasyon": (r"\botomasyon\b", r"\bautomation\b", r"\bscada\b", r"\bplc\b", r"\bmes\s*(?:sistem|yazilim|cozum)"),
    "Mobil": (r"\bmobil\s*(?:uygulama|yazilim|gelistir)", r"\bmobile\s*(?:app|application|development)"),
    "Bulut": (r"\bbulut\s*(?:bilisim|teknoloji|cozum|hizmet)", r"\bcloud\s*(?:computing|native|solution|service)", r"\bsaas\b", r"\bpaas\b"),
    # "GSM: 0532 ..." iletişim sayfalarında telefon etiketi olarak geçer; tek başına Telekom sayılmaz
    "Telekom": (r"\btelekom(?:unikasyon)?\b", r"\btelecom(?:munications?)?\b", r"\b5g\b", r"\bvoip\b", r"\bgsm\s*(?:operator|sebeke|teknoloji|modul|modem|altyapi)"),
    "GIS": (r"\bgis\b", r"\bcografi\s*bilgi\s*sistem", r"\bharita\s*(?:yazilim|teknoloji)", r"\bgeospatial\b", r"\buzaktan\s*algilama"),
    "Web": (r"\bweb\s*(?:uygulama|yazilim|gelistir|tasarim|sitesi\s*tasarim|application|development)", r"\bfull\s*-?stack\b", r"\bfrontend\b", r"\bbackend\b", r"\bon\s*yuz\b", r"\barka\s*uc\b"),
    "Linux": (r"\blinux\b", r"\bunix\b", r"\bpardus\b"),
    "Simülasyon": (r"\bsimulasyon\b", r"\bsimulation\b", r"\bsimulator\b", r"\bdijital\s*ikiz\b", r"\bdigital\s*twin\b"),
    "AR/VR": (r"\bsanal\s*gerceklik\b", r"\bartirilmis\s*gerceklik\b", r"\bvirtual\s*reality\b", r"\baugmented\s*reality\b", r"\bar\s*/\s*vr\b", r"\bmetaverse\b", r"\bxr\b"),
}

# Sektör etiketi: teknoloji sözlüğündeki alan adları + yalnızca sektör anlamı olan birkaç desen
SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Savunma": TECH_KEYWORDS["Savunma"],
    "Fintech": TECH_KEYWORDS["Fintech"],
    "Oyun": TECH_KEYWORDS["Oyun"],
    "Sağlık": TECH_KEYWORDS["Sağlık"],
    "E-ticaret": TECH_KEYWORDS["E-ticaret"],
    "ERP": TECH_KEYWORDS["ERP"],
    "Siber güvenlik": TECH_KEYWORDS["Siber güvenlik"],
    "Yapay zeka": TECH_KEYWORDS["Yapay zeka"] + TECH_KEYWORDS["Makine öğrenmesi"],
    "Veri bilimi": TECH_KEYWORDS["Veri bilimi"],
    "IoT": TECH_KEYWORDS["IoT"],
    "Gömülü sistemler": TECH_KEYWORDS["Gömülü sistemler"] + TECH_KEYWORDS["FPGA"],
    "Robotik": TECH_KEYWORDS["Robotik"] + TECH_KEYWORDS["Otomasyon"],
    "Blockchain": TECH_KEYWORDS["Blockchain"],
    "Telekom": TECH_KEYWORDS["Telekom"],
    "Mobil": TECH_KEYWORDS["Mobil"],
    "Bulut": TECH_KEYWORDS["Bulut"],
    "Bilişim danışmanlığı": (r"\bdanismanli[kg]", r"\bconsulting\b", r"\bsistem\s*entegrat", r"\bsystem\s*integrat", r"\bit\s*hizmet", r"\bbilisim\s*hizmet"),
    "Yazılım": (r"\byazilim\b", r"\bsoftware\b", r"\bbilisim\b", r"\buygulama\s*gelistir", r"\bteknoloji\s*sirketi"),
}

_COMPILED_TECH: dict[str, re.Pattern[str]] = {k: re.compile("|".join(v)) for k, v in TECH_KEYWORDS.items()}
_COMPILED_SECTOR: dict[str, re.Pattern[str]] = {k: re.compile("|".join(v)) for k, v in SECTOR_KEYWORDS.items()}

# Genel sektör etiketleri yalnızca daha özel bir eşleşme yoksa kullanılır
_GENERIC_SECTORS: tuple[str, ...] = ("Yazılım", "Bilişim danışmanlığı")


def fold(text: str | None) -> str:
    """Metni eşleştirme için hazırlar: ASCII, küçük harf, tek boşluk."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", ascii_fold(str(text)))


def detect_technologies(text: str | None, *, extra: Iterable[str] | None = None) -> list[str]:
    """Metinde geçen teknolojileri (kanonik adlarla, sözlük sırasında) döner."""
    folded = fold(text)
    if not folded:
        return []
    found = [name for name, rx in _COMPILED_TECH.items() if rx.search(folded)]
    for e in extra or []:
        if e and e not in found:
            found.append(e)
    return found


def count_matches(text: str | None, patterns: dict[str, re.Pattern[str]] | None = None) -> dict[str, int]:
    folded = fold(text)
    out: dict[str, int] = {}
    for name, rx in (patterns or _COMPILED_SECTOR).items():
        n = len(rx.findall(folded)) if folded else 0
        if n:
            out[name] = n
    return out


def detect_sector(text: str | None) -> str | None:
    """En çok eşleşen sektör etiketi; genel etiketler ('Yazılım') yalnızca başka eşleşme yoksa."""
    counts = count_matches(text)
    if not counts:
        return None
    specific = {k: v for k, v in counts.items() if k not in _GENERIC_SECTORS}
    pool = specific or counts
    return max(pool.items(), key=lambda kv: (kv[1], -list(SECTOR_KEYWORDS).index(kv[0])))[0]


def profile_technologies(profile_text: str | None) -> list[str]:
    """Kullanıcının profil metninden teknolojileri çıkarır (puanlamada tech_stack kesişimi için)."""
    return detect_technologies(profile_text)
