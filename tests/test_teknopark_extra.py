"""Teknopark collector için ek (saldırgan) testler: boş/bozuk girdiler, Türkçe karakterler,
yinelenenler, ağ hataları, yeniden çalıştırma ve Windows yolları."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from stajbot.collectors import teknopark as tp
from stajbot.collectors.base import RawCompany
from stajbot.models import Company

FIXTURES = Path(__file__).parent / "fixtures" / "teknopark"


def _entry(**over) -> dict:
    base = {
        "slug": "test-park", "name": "Test Park", "city": "Konya", "kind": "html",
        "list_url": "https://park.test/firmalar", "pagination": "none",
        "selectors": {"item": "div.f", "name": "h4", "website": "a@href"},
    }
    base.update(over)
    return base


def _cases(*names: str) -> str:
    return "".join(f"<div class='f'><h4>{n}</h4></div>" for n in names)


# ---------------------------------------------------------------------------
# Boş ve bozuk girdiler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("html", [b"", "", b"   ", "<html>", None, b"\xff\xfe\x00garbage", "<<<>>>", "<div class='f'><h4></h4></div>"])
def test_parse_list_html_empty_and_malformed(html):
    assert tp.parse_list_html(html, _entry()) == []


@pytest.mark.parametrize("data", [None, [], {}, "metin", 5, [None, 3, "x", []], {"companies": None}, {"companies": "x"}])
def test_parse_json_garbage_inputs(data):
    entry = {"slug": "j", "name": "J", "city": "Konya", "list_url": "https://j.test/api", "json_keys": {"items": "", "name": "name"}}
    assert tp.parse_json(data, entry) == []
    entry2 = dict(entry, json_keys={"items": "companies", "name": "name"})
    assert tp.parse_json(data, entry2) == []


def test_parse_json_items_as_dict_and_mixed_rows():
    entry = {"slug": "j", "name": "J", "city": "Konya", "list_url": "https://j.test/api", "json_keys": {"items": "data", "name": "ad", "website": "web"}}
    data = {"data": {"1": {"ad": "Alfa", "web": "alfa.com"}, "2": None, "3": {"ad": "  "}, "4": {"ad": 42}}}
    out = tp.parse_json(data, entry)
    assert [c.name for c in out] == ["Alfa", "42"]
    assert out[0].website == "https://alfa.com"


def test_extract_inline_json_variants():
    marker = {"inline_json_marker": "JSON.parse('"}
    # JS dizesinde kaçışsız çift tırnak (json_encode çıktısı tek tırnak içinde)
    js = "x = JSON.parse('[{\"name\": \"A\\'s Park\"}]');"
    assert tp.extract_inline_json(js, marker) == [{"name": "A's Park"}]
    # \\uXXXX kaçışları (Sakarya biçimi) + Türkçe karakter + surrogate çifti
    js2 = "JSON.parse('[{\\u0022name\\u0022: \\u0022\\u00d6rnek \\u015eirket \\ud83d\\ude80\\u0022}]')"
    assert tp.extract_inline_json(js2, marker) == [{"name": "Örnek Şirket " + chr(0x1F680)}]
    # kaçışlı ters bölü ve satır sonu
    js3 = "JSON.parse('{\"p\": \"a\\\\\\\\b\", \"n\": \"x\\\\ny\"}')"
    assert tp.extract_inline_json(js3, marker) == {"p": "a\\b", "n": "x\ny"}
    # bitmeyen dize ve bozuk JSON -> ValueError (collector bunu yakalar)
    with pytest.raises(ValueError):
        tp.extract_inline_json("JSON.parse('[{", marker)
    with pytest.raises(ValueError):
        tp.extract_inline_json("JSON.parse('[{bozuk')", marker)
    with pytest.raises(ValueError):
        tp.extract_inline_json("", marker)


def test_regex_with_unmatched_optional_group_keeps_item():
    """İsteğe bağlı grup eşleşmezse öğe düşmemeli (önceden 'NoneType has no strip' ile atlanıyordu)."""
    entry = _entry(selectors={"item": "div.f", "name": "h4", "sector": {"sel": "p", "regex": r"Sektör:\s*(\S+)?"}})
    html = "<div class='f'><h4>Alfa Yazılım</h4><p>Sektör:</p></div><div class='f'><h4>Beta</h4><p>Sektör: Gıda</p></div>"
    out = tp.parse_list_html(html, entry)
    assert [(c.name, c.sector) for c in out] == [("Alfa Yazılım", None), ("Beta", "Gıda")]


def test_invalid_regex_in_selector_does_not_drop_item():
    entry = _entry(selectors={"item": "div.f", "name": "h4", "sector": {"sel": "p", "regex": "("}})
    out = tp.parse_list_html("<div class='f'><h4>Alfa</h4><p>x</p></div>", entry)
    assert [c.name for c in out] == ["Alfa"] and out[0].sector is None


def test_extract_sub_links_bad_inputs(caplog):
    assert tp.extract_sub_links("", _entry(sub_links="a")) == []
    assert tp.extract_sub_links(b"", _entry(sub_links="a")) == []
    assert tp.extract_sub_links("<a href='/x'>x</a>", _entry()) == []  # sub_links tanımsız
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        assert tp.extract_sub_links("<a href='/x'>x</a>", _entry(sub_links="a", sub_links_pattern="(")) == []
    assert any("düzenli ifade" in r.getMessage() for r in caplog.records)
    # geçersiz CSS
    assert tp.extract_sub_links("<a href='/x'>x</a>", _entry(sub_links="a[[")) == []
    # kendine dönen, # ve javascript: bağlantılar süzülür; tekilleştirilir
    html = "<a href='https://park.test/firmalar/'>self</a><a href='#'>x</a><a href='javascript:void(0)'>y</a><a href='/menu/a/'>A</a><a href='/menu/a/'>A uzun etiket</a>"
    assert tp.extract_sub_links(html, _entry(sub_links="a")) == [("https://park.test/menu/a/", "A uzun etiket")]


def test_apply_detail_with_garbage_never_raises():
    c = RawCompany(name="X", source="teknopark")
    entry = _entry(detail_selectors={"website": "a@href", "phone": {"sel": "p", "regex": "("}})
    for html in (b"", "", "<<<", b"\x00\x01", "<a href='mailto:x@y.com'>m</a><a href='tel:123'>t</a>"):
        tp.apply_detail_html(c, html, entry)
    assert c.website is None and c.phone is None
    jentry = _entry(detail_json_keys={"website": "a.b.c", "email": "e", "sector": "s"})
    for data in (None, "x", 5, [], {"a": 5}, {"a": {"b": {"c": ["liste"]}}}, {"e": "INFO@Ornek.COM", "s": {"nested": 1}}):
        tp.apply_detail_json(c, data, jentry)
    assert c.website is None and c.sector is None
    assert c.emails == ["info@ornek.com"]


# ---------------------------------------------------------------------------
# Yapılandırma dosyası
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["- a\n- b\n", "just a string\n", "technoparks: 5\n", "defaults: 3\ntechnoparks: []\n", "technoparks:\n  - 3\n  - null\n", ""])
def test_load_config_tolerates_wrong_yaml_shapes(tmp_path, content):
    p = tmp_path / "t.yaml"
    p.write_text(content, encoding="utf-8")
    cfg = tp.load_config(p)
    assert cfg["technoparks"] == []
    assert cfg["defaults"]["max_detail_pages"] == 150


def test_load_config_windows_path_and_bom(tmp_path):
    p = tmp_path / "alt klasör" / "teknoparklar.yaml"
    p.parent.mkdir()
    p.write_text("technoparks:\n  - slug: ş\n    name: Şirin Park\n    city: İzmir\n    kind: html\n    list_url: https://s.test/\n    selectors: {item: div, name: h4}\n", encoding="utf-8-sig")
    win_path = str(p).replace("/", "\\")
    cfg = tp.load_config(win_path)
    assert [e["name"] for e in cfg["technoparks"]] == ["Şirin Park"]
    col = tp.TeknoparkCollector(config_path=win_path)
    assert [e["slug"] for e in col.entries] == ["ş"]


def test_collector_with_broken_config_yields_nothing(tmp_path, caplog):
    p = tmp_path / "bozuk.yaml"
    p.write_text("technoparks: [\n  - :::\n", encoding="utf-8")
    col = tp.TeknoparkCollector(config_path=p, client=httpx.Client(), delay=0)
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        assert list(col.collect()) == []
    assert any("okunamadı" in r.getMessage() or "bulunamadı" in r.getMessage() for r in caplog.records)
    missing = tp.TeknoparkCollector(config_path=tmp_path / "yok.yaml", client=httpx.Client(), delay=0)
    assert list(missing.collect(cities=["Konya"])) == []


def test_unknown_kind_is_skipped(tmp_path, caplog):
    p = tmp_path / "t.yaml"
    p.write_text("technoparks:\n  - slug: x\n    name: X Park\n    city: Konya\n    kind: rss\n    list_url: https://x.test/\n", encoding="utf-8")
    col = tp.TeknoparkCollector(config_path=p, client=httpx.Client(), delay=0)
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        assert list(col.collect()) == []
    assert any("bilinmeyen kaynak türü" in r.getMessage() for r in caplog.records)


def test_all_user_facing_log_messages_are_turkish():
    """Modüldeki log metinlerinde yaygın İngilizce sözcükler bulunmamalı."""
    src = Path(tp.__file__).read_text(encoding="utf-8")
    import re

    msgs = re.findall(r"log\.(?:info|warning|error)\(\s*\n?\s*\"([^\"]+)\"", src)
    assert msgs, "log çağrısı bulunamadı"
    english = re.compile(r"\b(failed|error|not found|skipped|missing|invalid|cannot|unable)\b", re.I)
    for m in msgs:
        assert not english.search(m), m


def test_module_has_no_print():
    src = Path(tp.__file__).read_text(encoding="utf-8")
    import re

    assert not re.search(r"^\s*print\(", src, re.M)


# ---------------------------------------------------------------------------
# Türkçe karakterler ve web sitesi temizliği
# ---------------------------------------------------------------------------


def test_turkish_characters_preserved_and_entities_decoded():
    html = "<div class='f'><h4>  Çağrı &amp; Işık Yazılım İnş. Ltd. Şti.  </h4><a href='https://çağrı.com.tr'>s</a></div>"
    out = tp.parse_list_html(html, _entry())
    assert out[0].name == "Çağrı & Işık Yazılım İnş. Ltd. Şti."
    assert out[0].website == "https://çağrı.com.tr"
    assert out[0].city == "Konya" and out[0].teknopark_name == "Test Park"


def test_website_rejects_technopark_own_domain_and_relative_links():
    entry = _entry(selectors={"item": "div.f", "name": "h4", "website": "a@href"})
    html = (
        "<div class='f'><h4>A</h4><a href='/firma/a'>detay</a></div>"
        "<div class='f'><h4>B</h4><a href='https://www.park.test/firma/b'>detay</a></div>"
        "<div class='f'><h4>C</h4><a href='http://www.'>boş</a></div>"
        "<div class='f'><h4>D</h4><a href='mailto:d@d.com'>m</a></div>"
        "<div class='f'><h4>E</h4><a href='WWW.E-Firma.COM.TR/'>s</a></div>"
        "<div class='f'><h4>F</h4><a href='https://f.com'>s</a></div>"
    )
    out = {c.name: c.website for c in tp.parse_list_html(html, entry)}
    assert out == {"A": None, "B": None, "C": None, "D": None, "E": "https://WWW.E-Firma.COM.TR/", "F": "https://f.com"}
    # detay sayfasından gelen göreli / kendi alanı da reddedilir
    c = RawCompany(name="A", source="teknopark")
    tp.apply_detail_html(c, "<a class='w' href='/iletisim'>x</a>", _entry(detail_selectors={"website": "a.w@href"}), base_url="https://park.test/firma/a")
    assert c.website is None
    tp.apply_detail_html(c, "<a class='w' href='https://gercek.com'>x</a>", _entry(detail_selectors={"website": "a.w@href"}), base_url="https://park.test/firma/a")
    assert c.website == "https://gercek.com"


def test_emails_lowercased_and_deduplicated():
    entry = _entry(selectors={"item": "div.f", "name": "h4", "email": "span"})
    html = "<div class='f'><h4>A</h4><span>E-posta: INFO@Ornek.COM.TR</span></div><div class='f'><h4>B</h4><span>info [at] ornek.com</span></div>"
    out = tp.parse_list_html(html, entry)
    assert out[0].emails == ["info@ornek.com.tr"]
    assert out[1].emails == []
    tp._fill_field(out[0], "email", "info@ornek.com.tr", None)
    assert out[0].emails == ["info@ornek.com.tr"]


# ---------------------------------------------------------------------------
# Tekilleştirme
# ---------------------------------------------------------------------------


def test_dedupe_does_not_merge_distinct_companies_with_shared_prefix():
    """'Alfa Yazılım' ile 'Alfa Teknoloji' farklı firmalardır; yalnızca hukuki ekler yok sayılır."""
    entry = _entry()
    cs = [
        RawCompany(name="Alfa Yazılım", source="teknopark"),
        RawCompany(name="Alfa Teknoloji", source="teknopark"),
        RawCompany(name="Alfa Bilişim Hizmetleri Ltd. Şti.", source="teknopark"),
        RawCompany(name="ALFA YAZILIM A.Ş.", source="teknopark"),
        RawCompany(name="Alfa Yazılım Anonim Şirketi", source="teknopark"),
        RawCompany(name="Alfa Yazilim San. ve Tic. Ltd. Şti.", source="teknopark"),
    ]
    out = tp.TeknoparkCollector._dedupe(cs, entry)
    assert sorted(c.name for c in out) == ["Alfa Bilişim Hizmetleri Ltd. Şti.", "Alfa Teknoloji", "Alfa Yazılım"]


def test_dedupe_key_edge_cases():
    assert tp._dedupe_key("A.Ş.") != ""
    assert tp._dedupe_key("???") != ""
    assert tp._dedupe_key("") == ""
    assert tp._dedupe_key("Örnek & Ortakları") == tp._dedupe_key("ÖRNEK VE ORTAKLARI")
    assert tp._dedupe_key("X-Ray Yazılım") == tp._dedupe_key("x ray yazilim")


def test_dedupe_keeps_first_and_merges_fields_once():
    a = RawCompany(name="Zeta", source="teknopark", tags=["Bina 1"], emails=["a@z.com"])
    b = RawCompany(name="ZETA", source="teknopark", tags=["Bina 1", "Bina 2"], emails=["A@z.com", "b@z.com"], phone="1", sector="S1")
    c = RawCompany(name="zeta", source="teknopark", sector="S2", phone="2")
    out = tp.TeknoparkCollector._dedupe([a, b, c], _entry())
    assert len(out) == 1 and out[0] is a
    assert out[0].phone == "1" and out[0].sector == "S1"
    assert out[0].tags == ["Bina 1", "Bina 2"]
    assert "b@z.com" in out[0].emails and len(out[0].emails) == 3  # harf farkı burada normalize edilmez (ingest lowercase yapar)
    assert out[0].raw["sectors"] == ["S1", "S2"]


# ---------------------------------------------------------------------------
# collect() — ağ hataları, sayfalama, yeniden çalıştırma
# ---------------------------------------------------------------------------


def _col(**kw) -> tp.TeknoparkCollector:
    return tp.TeknoparkCollector(client=httpx.Client(follow_redirects=True), delay=0, **kw)


def _write_cfg(tmp_path: Path, *entries: dict) -> Path:
    import yaml

    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({"technoparks": list(entries)}, allow_unicode=True), encoding="utf-8")
    return p


@pytest.mark.respx(assert_all_called=False)
def test_sub_pagination_continues_when_first_page_only_has_seen_names(respx_mock):
    """Bir sektörün 1. sayfası yalnızca başka sektörde görülmüş firmalardan oluşsa da 2. sayfası çekilir."""
    entry = _entry(
        sub_links="ul a", sub_link_field="sector", sub_pagination="page_param:p", page_start=2, page_size=2, max_pages=5,
        selectors={"item": "div.f", "name": "h4"},
    )
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text="<ul><a href='/s/a/'>A</a><a href='/s/b/'>B</a></ul>"))
    respx_mock.get("https://park.test/s/a/", params__contains={"p": "2"}).mock(return_value=httpx.Response(200, text=_cases("Gama")))
    respx_mock.get("https://park.test/s/a/").mock(return_value=httpx.Response(200, text=_cases("Alfa", "Beta")))
    b2 = respx_mock.get("https://park.test/s/b/", params__contains={"p": "2"}).mock(return_value=httpx.Response(200, text=_cases("Delta")))
    respx_mock.get("https://park.test/s/b/").mock(return_value=httpx.Response(200, text=_cases("Alfa", "Beta")))
    out = _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    assert b2.called
    assert sorted(c.name for c in out) == ["Alfa", "Beta", "Delta", "Gama"]
    by = {c.name: c for c in out}
    assert by["Delta"].sector == "B" and by["Alfa"].raw["sectors"] == ["A", "B"]


@pytest.mark.respx(assert_all_called=False)
def test_pagination_stops_on_network_error_and_keeps_partial(respx_mock, tmp_path):
    entry = _entry(pagination="page_param:page", page_size=2, max_pages=10)
    respx_mock.get("https://park.test/robots.txt").mock(side_effect=httpx.ReadTimeout("t"))
    respx_mock.get("https://park.test/firmalar", params__contains={"page": "3"}).mock(side_effect=httpx.ReadTimeout("zaman aşımı"))
    respx_mock.get("https://park.test/firmalar", params__contains={"page": "2"}).mock(return_value=httpx.Response(200, text=_cases("C", "D")))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text=_cases("A", "B")))
    col = tp.TeknoparkCollector(config_path=_write_cfg(tmp_path, entry), client=httpx.Client(), delay=0)
    out = list(col.collect())
    assert [c.name for c in out] == ["A", "B", "C", "D"]
    assert col.stats["test-park"] == 4


@pytest.mark.respx(assert_all_called=False)
def test_page_start_zero_is_respected(respx_mock):
    entry = _entry(pagination="page_param:page", page_start=0, page_step=1, page_size=1, max_pages=3, selectors={"item": "div.f", "name": "h4"})
    route = respx_mock.get(url__regex=r"https://park\.test/firmalar.*").mock(
        side_effect=[httpx.Response(200, text=_cases("A")), httpx.Response(200, text=_cases("B")), httpx.Response(200, text="")]
    )
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(404))
    col = _col(respect_robots=False)
    out = col._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    assert [c.name for c in out] == ["A", "B"]
    assert "page=0" in str(route.calls[1].request.url)


@pytest.mark.respx(assert_all_called=False)
def test_next_link_pagination_handles_relative_and_loops(respx_mock):
    entry = _entry(pagination="next_link:a.next", selectors={"item": "div.f", "name": "h4"}, max_pages=20)
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text=_cases("A") + "<a class='next' href='sayfa2'>›</a>"))
    p2 = respx_mock.get("https://park.test/sayfa2").mock(return_value=httpx.Response(200, text=_cases("B") + "<a class='next' href='/firmalar'>›</a>"))
    out = _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    assert [c.name for c in out] == ["A", "B"]
    assert p2.call_count == 1  # döngü: /firmalar tekrar çekilmez


@pytest.mark.respx(assert_all_called=False)
def test_server_error_on_sub_link_index_and_detail_pages(respx_mock, caplog):
    entry = _entry(sub_links="ul a", selectors={"item": "div.f", "name": "h4", "detail_link": "a@href"}, detail_page=True,
                   detail_kind="json", detail_json_keys={"website": "w"})
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(500))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        assert _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0)) == []
    # indeks tamam, detay JSON yerine HTML / 404 geliyor -> firma yine de listelenir
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text="<ul><a href='/s/'>S</a></ul>"))
    respx_mock.get("https://park.test/s/").mock(return_value=httpx.Response(200, text="<div class='f'><h4>A</h4><a href='/d/a'>d</a></div><div class='f'><h4>B</h4><a href='/d/b'>d</a></div>"))
    respx_mock.get("https://park.test/d/a").mock(return_value=httpx.Response(200, text="<html>hata sayfası</html>"))
    respx_mock.get("https://park.test/d/b").mock(return_value=httpx.Response(404))
    out = _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    assert sorted(c.name for c in out) == ["A", "B"] and all(c.website is None for c in out)


@pytest.mark.respx(assert_all_called=False)
def test_collect_continues_with_next_source_after_unexpected_exception(respx_mock, tmp_path, monkeypatch, caplog):
    e1 = _entry(slug="bozuk", name="Bozuk Park", list_url="https://bozuk.test/f")
    e2 = _entry(slug="iyi", name="İyi Park", list_url="https://iyi.test/f")
    respx_mock.get(url__regex=r"https://(bozuk|iyi)\.test/robots\.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://bozuk.test/f").mock(return_value=httpx.Response(200, text=_cases("X")))
    respx_mock.get("https://iyi.test/f").mock(return_value=httpx.Response(200, text=_cases("Y")))
    orig = tp.parse_list_html

    def boom(html, entry, *a, **kw):
        if entry["slug"] == "bozuk":
            raise RuntimeError("beklenmedik")
        return orig(html, entry, *a, **kw)

    monkeypatch.setattr(tp, "parse_list_html", boom)
    col = tp.TeknoparkCollector(config_path=_write_cfg(tmp_path, e1, e2), client=httpx.Client(), delay=0)
    with caplog.at_level(logging.ERROR, logger="stajbot"):
        out = list(col.collect())
    assert [c.name for c in out] == ["Y"]
    assert any("Bozuk Park" in r.getMessage() and "atlandı" in r.getMessage() for r in caplog.records)
    assert "bozuk" not in col.stats and col.stats["iyi"] == 1


@pytest.mark.respx(assert_all_called=False)
def test_robots_disallow_only_for_other_agent_still_allows(respx_mock):
    entry = _entry()
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text=_cases("A")))
    assert [c.name for c in _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0))] == ["A"]
    # robots.txt yerine HTML (yanlış yapılandırılmış sunucu) -> izin varsayılır
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(200, text="<html><body>404</body></html>"))
    col = _col()
    assert [c.name for c in col._collect_entry(entry, httpx.Client(), tp.Throttle(0))] == ["A"]
    # respect_robots=False tam engeli aşar
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    assert _col()._collect_entry(entry, httpx.Client(), tp.Throttle(0)) == []
    assert [c.name for c in _col(respect_robots=False)._collect_entry(entry, httpx.Client(), tp.Throttle(0))] == ["A"]


@pytest.mark.respx(assert_all_called=False)
def test_collect_twice_is_idempotent_through_ingest(respx_mock, tmp_path, session):
    """Aynı collector iki kez çalıştırılıp ingest edilirse satır sayısı değişmez."""
    ingest = pytest.importorskip("stajbot.collectors.ingest", reason="ingest modülü henüz yok").ingest
    entry = _entry(selectors={"item": "div.f", "name": "h4", "website": "a@href"})
    html = (
        "<div class='f'><h4>Alfa Yazılım A.Ş.</h4><a href='https://alfa.com.tr'>s</a></div>"
        "<div class='f'><h4>Beta Bilişim</h4></div>"
        "<div class='f'><h4>BETA BİLİŞİM LTD.</h4></div>"
    )
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text=html))
    col = tp.TeknoparkCollector(config_path=_write_cfg(tmp_path, entry), client=httpx.Client(), delay=0)
    first = list(col.collect(cities=["konya"]))
    assert [c.name for c in first] == ["Alfa Yazılım A.Ş.", "Beta Bilişim"]
    r1 = ingest(first, session)
    session.commit()
    assert r1.created == 2
    second = list(col.collect(cities=["KONYA"]))
    assert [c.name for c in second] == [c.name for c in first]
    r2 = ingest(second, session)
    session.commit()
    assert r2.created == 0
    rows = session.query(Company).all()
    assert len(rows) == 2
    assert all(r.is_teknopark and r.teknopark_name == "Test Park" and r.city == "Konya" for r in rows)
    assert {r.domain for r in rows} == {"alfa.com.tr", None}


@pytest.mark.respx(assert_all_called=False)
def test_collect_city_filter_outside_targets_yields_nothing_without_requests(respx_mock, tmp_path):
    entry = _entry()
    listing = respx_mock.get("https://park.test/firmalar").mock(return_value=httpx.Response(200, text=_cases("A")))
    col = tp.TeknoparkCollector(config_path=_write_cfg(tmp_path, entry), client=httpx.Client(), delay=0)
    assert list(col.collect(cities=["Trabzon"])) == []
    assert not listing.called
    # boş şehir adları süzgeç sayılmaz: kaynak normal taranır
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(404))
    assert [c.name for c in col.collect(cities=["", "  "])] == ["A"]


def test_select_entries_edge_inputs():
    entries = tp.list_technoparks()
    assert tp.select_entries([], cities=["Konya"]) == []
    assert tp.select_entries(entries, cities=None, slugs=None) == entries
    assert tp.select_entries(entries, cities=[], slugs=[]) == entries
    assert tp.select_entries(entries, slugs=["", "  ", None]) == entries  # boş slug'lar yok sayılır
    assert [e["city"] for e in tp.select_entries(entries, cities=["ISTANBUL", "izmır"])] and all(
        e["city"] in ("İstanbul", "İzmir") for e in tp.select_entries(entries, cities=["ISTANBUL", "izmır"])
    )
    assert all(e["city"] == "Afyonkarahisar" for e in tp.select_entries(entries, cities=["afyon"]))


def test_real_fixtures_have_no_own_domain_websites():
    entries = {e["slug"]: e for e in tp.list_technoparks()}
    for slug, fixture in [("aso-teknopark", "aso-teknopark.html"), ("yildiz-teknopark", "yildiz-teknopark.html"),
                          ("teknopark-izmir", "teknopark-izmir.html"), ("zafer-teknopark", "zafer-teknopark.html"),
                          ("atap-eskisehir", "atap-eskisehir.html"), ("izmir-bilimpark", "izmir-bilimpark.html")]:
        entry = entries[slug]
        own = tp.domain_of(entry["list_url"])
        companies = tp.parse_list_html((FIXTURES / fixture).read_bytes(), entry)
        assert companies
        assert all(tp.domain_of(c.website) != own for c in companies if c.website), slug
    konya = tp.parse_json(tp._loads_lenient((FIXTURES / "konya-teknokent.json").read_text(encoding="utf-8")), entries["konya-teknokent"])
    assert all(tp.domain_of(c.website) != "konyateknokent.com.tr" for c in konya if c.website)
    odtu = tp.parse_json(json.loads((FIXTURES / "odtu-teknokent.json").read_text(encoding="utf-8")), entries["odtu-teknokent"])
    assert len(odtu) == 392 and len({tp._dedupe_key(c.name) for c in odtu}) >= 390
