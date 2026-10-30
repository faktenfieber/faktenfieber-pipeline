#!/usr/bin/env python3
"""
FAKTENFIEBER – Pipeline v8.0
Wikipedia-Recherche -> Skript -> Faktencheck gegen Quelltext -> Voiceover -> Clips -> Schnitt -> Telegram

KI-Anbieter (alle gratis, mindestens einer noetig – werden der Reihe nach probiert):
  MISTRAL_API_KEY, GROQ_API_KEY, GEMINI_API_KEY
"""
import difflib
import json
import math
import os
import random
import re
import string
import subprocess
import sys
import time
import wave
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------- Einstellungen
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
WORK = ROOT / "work"
USED_FILE = ROOT / "data" / "used_topics.json"

W, H, FPS = 1080, 1920, 30
MAX_DAUER = 58.0                       # Sekunden, Shorts bleiben unter 60
MAX_WOERTER = 125
MAX_SEGMENT = float(os.environ.get("SCHNITT_SEKUNDEN", "3.0"))   # spaetestens alle X s ein neues Bild
VOICE_NAME = os.environ.get("PIPER_VOICE", "de_DE-thorsten-high")
LENGTH_SCALE = float(os.environ.get("VOICE_SPEED", "0.92"))   # <1 = schneller
FONT = "DejaVu Sans"
MAX_VERSUCHE = 4                       # neue Themen, falls Faktencheck durchfaellt

MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

WIKI_API = "https://de.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "FaktenfieberBot/0.5 (faktenfieber.official@gmail.com)"}
# Exzellente + lesenswerte Artikel = von der Community gepruefte Qualitaet, tausende Themen
WIKI_KATEGORIEN = ["Kategorie:Wikipedia:Exzellent", "Kategorie:Wikipedia:Lesenswert"]


def log(msg):
    print(f"[Faktenfieber] {msg}", flush=True)


# ================================================================ KI-Anbieter
GESPERRT = {}        # (anbieter, modell) -> gesperrt bis Zeitpunkt


def _openai_kompatibel(url, key, modell, prompt, temperatur):
    body = {
        "model": modell,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperatur,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in modell:
        body["reasoning_effort"] = "low"         # sonst "denkt" es so lange, dass die Antwort leer bleibt
    senden = lambda: requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=120, json=body)
    r = senden()
    if r.status_code == 400 and "reasoning_effort" in r.text:
        body.pop("reasoning_effort", None)
        r = senden()
    if r.status_code == 400 and "json_validate" in r.text:
        body.pop("response_format")          # manche Modelle mögen den JSON-Modus nicht
        r = senden()
    if r.status_code != 200:
        return r.status_code, r.text[:700], None
    inhalt = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not inhalt and "response_format" in body:   # leere Antwort -> ohne JSON-Modus nochmal
        body.pop("response_format")
        r = senden()
        if r.status_code == 200:
            inhalt = (r.json()["choices"][0]["message"].get("content") or "").strip()
    return (200, "", inhalt) if inhalt else (0, "leere Antwort", None)


def _gemini(modell, prompt, temperatur):
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modell}:generateContent",
                      params={"key": GEMINI_KEY}, timeout=45, json={
                          "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": temperatur,
                                               "responseMimeType": "application/json"}})
    if r.status_code != 200:
        return r.status_code, r.text[:700], None
    kand = (r.json().get("candidates") or [{}])[0]
    return 200, "", "".join(p.get("text", "") for p in kand.get("content", {}).get("parts", []))


def _groq_modelle():
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=30)
        ids = [m["id"] for m in r.json().get("data", [])
               if (m.get("context_window") or 32000) >= 16000 and m.get("active", True)]
    except Exception:
        return ["llama-3.3-70b-versatile"]
    ids = [i for i in ids if not any(x in i for x in ("whisper", "guard", "tts", "prompt", "playai", "compound", "orpheus", "canopylabs", "speech", "audio", "allam"))]
    vorlieben = ["gpt-oss-120b", "kimi", "llama-4-maverick", "llama-3.3-70b", "qwen", "llama-4", "gpt-oss-20b"]
    ids.sort(key=lambda i: next((n for n, v in enumerate(vorlieben) if v in i), 99))
    return ids[:4]


def _gemini_modelle():
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": GEMINI_KEY, "pageSize": 1000}, timeout=30)
        namen = []
        for m in r.json().get("models", []):
            n = m["name"].split("/")[-1]
            if ("generateContent" in m.get("supportedGenerationMethods", []) and "flash" in n
                    and not any(x in n for x in ("image", "tts", "audio", "live", "embed", "omni"))):
                v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
                namen.append((("preview" not in n, "lite" not in n, float(v.group(1)) if v else 0), n))
        return [n for _, n in sorted(namen, reverse=True)][:3] or ["gemini-flash-latest"]
    except Exception:
        return ["gemini-flash-latest"]


ANBIETER = []


def anbieter_laden():
    """Reihenfolge: Groq (schnell) -> Gemini -> Mistral."""
    liste = []
    if GROQ_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        for m in _groq_modelle():
            liste.append(("Groq", m, lambda m_, p, t, u=url: _openai_kompatibel(u, GROQ_KEY, m_, p, t)))
    if GEMINI_KEY:
        for m in _gemini_modelle():
            liste.append(("Gemini", m, _gemini))
    if MISTRAL_KEY:
        url = "https://api.mistral.ai/v1/chat/completions"
        for m in ("mistral-medium-latest", "mistral-small-latest"):
            liste.append(("Mistral", m, lambda m_, p, t, u=url: _openai_kompatibel(u, MISTRAL_KEY, m_, p, t)))
    if not liste:
        sys.exit("Kein KI-Key gefunden! Mindestens MISTRAL_API_KEY, GROQ_API_KEY oder GEMINI_API_KEY setzen.")
    log("KI-Anbieter: " + ", ".join(f"{a}/{m}" for a, m, _ in liste))
    return liste


def wartezeit(info):
    """Liest 'try again in 13.3s' (Groq) bzw. 'retryDelay': '13s' (Gemini) aus der Fehlermeldung."""
    t = info or ""
    h = re.search(r"retry in (\d+)h", t, re.I)
    if h:
        return int(h.group(1)) * 3600.0
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", t) or \
        re.search(r'retryDelay"?\s*:\s*"?([\d.]+)s', t)
    if not m:
        return None
    if m.lastindex == 2:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return float(m.group(1))


def ki(prompt, temperatur=0.8, nicht=None):
    """Fragt die KI und gibt (json_dict, 'Anbieter/Modell') zurueck.
    nicht = Anbieter, der NICHT genommen werden soll (z. B. damit der Faktencheck unabhaengig ist)."""
    global ANBIETER
    if not ANBIETER:
        ANBIETER = anbieter_laden()
    # nicht = "Anbieter/Modell" des Autors -> fuer den Check zuerst ein ANDERES Modell nehmen
    reihenfolge = ([a for a in ANBIETER if f"{a[0]}/{a[1]}" != nicht] +
                   [a for a in ANBIETER if f"{a[0]}/{a[1]}" == nicht])
    fehler = ""
    for name, modell, funk in reihenfolge:
        if GESPERRT.get((name, modell), 0) > time.time():
            continue
        for versuch in range(3):
            try:
                code, info, text = funk(modell, prompt, temperatur)
            except requests.RequestException as e:
                code, info, text = 503, e.__class__.__name__, None
            if code == 200 and text:
                try:
                    daten = json_aus(text)
                    log(f"KI ok: {name}/{modell}")
                    return daten, f"{name}/{modell}"
                except ValueError as e:
                    code, info = 0, f"kaputtes JSON: {e}"
            fehler = f"{name}/{modell}: {code} {info}"
            kurz = re.sub(r"\s+", " ", fehler)[:260]
            log(f"KI-Problem -> {kurz}")
            warten = wartezeit(info)
            text_info = info or ""
            if code == 429 and ("Request too large" in text_info or "exceed the enforced limit" in text_info):
                break                            # diese Anfrage ist fuer das Modell zu gross -> naechstes Modell
            if code == 429 and ("per day" in text_info.lower() or "PerDay" in text_info
                                or (warten and warten > 3600)):
                GESPERRT[(name, modell)] = time.time() + 10**9   # Tageslimit -> fuer diesen Lauf raus
                break
            if code == 429 and not warten and name == "Mistral":
                GESPERRT[(name, modell)] = time.time() + 900     # Mistral ohne Hinweis -> 15 Min. Pause
                break
            if code == 429 and warten and warten <= 65 and versuch < 2:
                log(f"   Limit pro Minute erreicht -> warte {warten:.0f} s wie verlangt")
                time.sleep(warten + 1)
                continue
            if code in (500, 502, 503, 0) and versuch == 0:
                time.sleep(8)
                continue
            # 429/503 = nur voruebergehend (45 s), alles andere (403, 404 ...) = fuer diesen Lauf raus
            GESPERRT[(name, modell)] = time.time() + (45 if code in (429, 500, 502, 503, 0) else 10**9)
            break
    if not getattr(ki, "_nochmal", False):
        ki._nochmal = True
        log("Alle KIs gerade busy – warte 60 s und probiere nochmal ...")
        time.sleep(60)
        try:
            return ki(prompt, temperatur, nicht)
        finally:
            ki._nochmal = False
    raise RuntimeError(f"Keine KI erreichbar. Letzter Fehler: {fehler}")


def json_aus(text):
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("Kein JSON in der KI-Antwort")
    return json.loads(text[a:b + 1])


# ================================================================ Wikipedia-Recherche
def wiki(**params):
    params.update(action="query", format="json", formatversion=2)
    r = requests.get(WIKI_API, params=params, headers=WIKI_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


# Schwerpunkt: Geschichte, wie Menschen frueher lebten, kuriose Ereignisse, unnuetzes Wissen
THEMEN_SUCHE = [
    # Geschichte zum Anfassen
    "Mittelalter Alltag", "Ritter", "Burg", "Gladiator", "Antike Rom", "Altes Ägypten", "Mumie", "Pharao",
    "Wikinger", "Piraterie", "Steinzeit", "Pest", "Hinrichtung", "Folter Mittelalter", "Gefängnis Geschichte",
    "Aberglaube", "Hexenverfolgung", "Brauch", "Beruf Geschichte", "Arbeit Mittelalter", "Kinder Geschichte",
    "Medizin Geschichte", "Hygiene", "Essen Geschichte", "Süßigkeit", "Mode Geschichte",
    # Krimi, Abenteuer, Katastrophen
    "Hochstapler", "Fälschung", "Schwindel", "Raub", "Kunstraub", "Kriminalfall", "Schmuggel", "Spionage",
    "Schiffbruch", "Unglück", "Katastrophe", "Explosion", "Vulkanausbruch", "Erdbeben", "Expedition",
    "Verschollen", "Schatz", "Entdeckung",
    # Verrueckte Erfindungen & Rekorde
    "Erfindung", "Erfindung Zufall", "Rekord", "Zirkus", "Jahrmarkt", "Ballon", "Zeppelin", "Achterbahn",
    "Olympische Spiele", "Sport Kuriosität",
    # Natur & Tiere
    "Tier Rekord", "giftiges Tier", "Hai", "Dinosaurier", "Naturphänomen", "Höhle", "Meteorit",
    # Heute: Technik, Internet, Alltag
    "Smartphone", "Internet", "Soziales Netzwerk", "Videospiel", "Spielkonsole", "Emoji", "Computervirus",
    "Hacker", "Künstliche Intelligenz", "Roboter", "Drohne", "Elektroauto", "Raumfahrt", "Mars", "Satellit",
    "Fast Food", "Getränk Marke", "Süßigkeit Marke", "Sneaker", "Streaming", "Musikvideo", "Fußball Rekord",
    "Freizeitpark", "Flughafen", "Wolkenkratzer", "Menschlicher Körper", "Schlaf", "Gehirn", "Traum",
    "Optische Täuschung", "Rekord Mensch",
]
KATEGORIE_SUCHE = "incategory:Wikipedia:Exzellent|Wikipedia:Lesenswert"


def schon_benutzt(titel, benutzt):
    """Auch aehnliche Titel zaehlen als benutzt (z. B. 'Gasometer Neunkirchen' ~ 'Gasometerexplosion in Neunkirchen')."""
    n = re.sub(r"[^a-z0-9äöüß]", "", titel.lower())
    for b in benutzt:
        m = re.sub(r"[^a-z0-9äöüß]", "", str(b).lower())
        if n == m or (len(n) > 6 and (n in m or m in n)) or difflib.SequenceMatcher(None, n, m).ratio() > 0.85:
            return True
    return False


def wiki_kandidaten(benutzt, anzahl=14):
    titel = set()
    # ~3/4 gezielt aus Geschichte & unnuetzem Wissen, Rest zufaellig quer durch alles
    for begriff in random.sample(THEMEN_SUCHE, 5):
        try:
            daten = wiki(list="search", srsearch=f"{KATEGORIE_SUCHE} {begriff}", srlimit=20, srnamespace=0)
            treffer = [t["title"] for t in daten.get("query", {}).get("search", [])
                       if not schon_benutzt(t["title"], benutzt)]
        except Exception as e:
            log(f"Wiki-Suche '{begriff}' fehlgeschlagen: {e}")
            treffer = []
        random.shuffle(treffer)
        titel.update(treffer[:2])
    for _ in range(4):
        if len(titel) >= anzahl:
            break
        kat = random.choice(WIKI_KATEGORIEN)
        daten = wiki(list="categorymembers", cmtitle=kat, cmlimit=50, cmnamespace=0,
                     cmstartsortkeyprefix=random.choice(string.ascii_uppercase))
        mitglieder = [m["title"] for m in daten.get("query", {}).get("categorymembers", [])
                      if not schon_benutzt(m["title"], benutzt)]
        random.shuffle(mitglieder)
        titel.update(mitglieder[:2])
    if not titel:   # Notfall: zufaellige Artikel
        daten = wiki(list="random", rnnamespace=0, rnlimit=anzahl)
        titel = {m["title"] for m in daten["query"]["random"]}
    titel = list(titel)[:anzahl]
    random.shuffle(titel)
    intros = wiki(prop="extracts", exintro=1, explaintext=1, exlimit=20, titles="|".join(titel))
    return {p["title"]: p.get("extract", "")[:300] for p in intros["query"]["pages"] if p.get("extract")}


def wiki_artikel(titel):
    daten = wiki(prop="extracts|info", explaintext=1, inprop="url", titles=titel, redirects=1)
    seite = daten["query"]["pages"][0]
    text = re.sub(r"\n{2,}", "\n", seite.get("extract", ""))
    # Literatur/Weblinks/Einzelnachweise am Ende abschneiden
    text = re.split(r"\n=+ (Literatur|Weblinks|Einzelnachweise|Anmerkungen) =+", text)[0]
    return seite["title"], text[:7000], seite.get("fullurl", f"https://de.wikipedia.org/wiki/{titel}")


EN_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Wikidata-Eigenschaften, die fuer Shorts interessant sind (exakte Zahlen + Daten)
WD_EIGENSCHAFTEN = {
    "P571": "Gegründet/entstanden", "P580": "Beginn", "P582": "Ende", "P585": "Zeitpunkt", "P569": "Geboren",
    "P570": "Gestorben", "P575": "Entdeckt/erfunden", "P577": "Veröffentlicht", "P1619": "Eröffnet",
    "P17": "Land", "P131": "Ort", "P276": "Ort", "P61": "Entdecker/Erfinder", "P170": "Urheber", "P112": "Gründer",
    "P2048": "Höhe", "P2043": "Länge", "P2049": "Breite", "P2046": "Fläche", "P2067": "Masse", "P2044": "Höhe über Meer",
    "P1082": "Einwohner", "P1128": "Mitarbeiter", "P1120": "Todesopfer", "P1339": "Verletzte", "P2052": "Geschwindigkeit",
    "P2130": "Kosten", "P2142": "Einspielergebnis", "P1174": "Besucher pro Jahr", "P2047": "Dauer",
}
TEXT_BUDGET = 9500                    # Zeichen fuer alle Quellen zusammen (sonst werden KI-Anfragen zu gross)


def _wd_wert(dv, labels):
    typ, w = dv.get("type"), dv.get("value")
    if typ == "time":
        t = w["time"].lstrip("+")
        jahr = t[:t.index("-", 1)] if t.startswith("-") else t[:4]
        if t.startswith("-"):
            return f"{jahr[1:].lstrip('0')} v. Chr."
        return jahr.lstrip("0") if w.get("precision", 11) <= 9 else f"{t[8:10]}.{t[5:7]}.{jahr}"
    if typ == "quantity":
        zahl = w["amount"].lstrip("+")
        einheit = w.get("unit", "1").rsplit("/", 1)[-1]
        return f"{zahl} {labels.get(einheit, '')}".strip()
    if typ == "wikibase-entityid":
        return labels.get(w.get("id"), "")
    if typ in ("string", "monolingualtext"):
        return w if isinstance(w, str) else w.get("text", "")
    return ""


def wikidata_fakten(qid):
    """Exakte Eckdaten (Jahreszahlen, Groessen, Opferzahlen ...) aus Wikidata als kurze Liste."""
    try:
        e = requests.get(WIKIDATA_API, headers=WIKI_HEADERS, timeout=30, params={
            "action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"}).json()["entities"][qid]
        werte, brauche = [], set()
        for prop, name in WD_EIGENSCHAFTEN.items():
            for c in (e.get("claims", {}).get(prop) or [])[:2]:
                dv = (c.get("mainsnak") or {}).get("datavalue")
                if not dv:
                    continue
                werte.append((name, dv))
                if dv["type"] == "wikibase-entityid":
                    brauche.add(dv["value"]["id"])
                elif dv["type"] == "quantity" and "/entity/" in dv["value"].get("unit", ""):
                    brauche.add(dv["value"]["unit"].rsplit("/", 1)[-1])
        labels = {}
        if brauche:
            l = requests.get(WIKIDATA_API, headers=WIKI_HEADERS, timeout=30, params={
                "action": "wbgetentities", "ids": "|".join(list(brauche)[:50]), "props": "labels",
                "languages": "de|en", "format": "json"}).json().get("entities", {})
            labels = {k: (v.get("labels", {}).get("de") or v.get("labels", {}).get("en") or {}).get("value", "")
                      for k, v in l.items()}
        zeilen = [f"- {name}: {_wd_wert(dv, labels)}" for name, dv in werte]
        return "\n".join(z for z in zeilen if not z.endswith(": "))[:900]
    except Exception as ex:
        log(f"Wikidata nicht ladbar: {ex}")
        return ""


def englischer_artikel(titel_de):
    """Der passende englische Artikel (oft 3-5x ausfuehrlicher) + Wikidata-ID."""
    try:
        d = wiki(prop="langlinks|pageprops", titles=titel_de, lllang="en", ppprop="wikibase_item", redirects=1)
        seite = d["query"]["pages"][0]
        qid = (seite.get("pageprops") or {}).get("wikibase_item", "")
        en = (seite.get("langlinks") or [{}])[0].get("title")
        if not en:
            return "", "", "", qid
        r = requests.get(EN_API, headers=WIKI_HEADERS, timeout=30, params={
            "action": "query", "format": "json", "formatversion": 2, "prop": "extracts|info", "explaintext": 1,
            "inprop": "url", "titles": en, "redirects": 1}).json()
        s_en = r["query"]["pages"][0]
        text = re.sub(r"\n{2,}", "\n", s_en.get("extract", ""))
        text = re.split(r"\n=+ (See also|References|Notes|Further reading|External links|Bibliography) =+", text)[0]
        return s_en.get("title", en), text, s_en.get("fullurl", f"https://en.wikipedia.org/wiki/{en}"), qid
    except Exception as ex:
        log(f"Englischer Artikel nicht ladbar: {ex}")
        return "", "", "", ""


def quellen_sammeln(titel):
    """Deutsche Wikipedia + englische Wikipedia + Wikidata zu einem Recherche-Text zusammenfuehren."""
    titel, de, url = wiki_artikel(titel)
    en_titel, en, en_url, qid = englischer_artikel(titel)
    wd = wikidata_fakten(qid) if qid else ""
    de = de[:6500]
    en = en[:max(2500, TEXT_BUDGET - len(de) - len(wd))]
    teile = [f"=== DEUTSCHER WIKIPEDIA-ARTIKEL ===\n{de}"]
    if en:
        teile.append(f"=== ENGLISCHER WIKIPEDIA-ARTIKEL \"{en_titel}\" ===\n{en}")
    if wd:
        teile.append(f"=== WIKIDATA (exakte Eckdaten) ===\n{wd}")
    quellen = [f"Wikipedia – {titel}: {url}"] + ([f"Wikipedia (en) – {en_titel}: {en_url}"] if en else [])
    log(f"Quellen: DE {len(de)} Zeichen, EN {len(en)} Zeichen, Wikidata {len(wd.splitlines()) if wd else 0} Eckdaten")
    return titel, "\n\n".join(teile), quellen


FREIE_LIZENZEN = ("cc0", "cc by", "cc-by", "public domain", "pd", "gemeinfrei", "no restrictions")
BILD_AUSSCHLUSS = re.compile(r"(icon|logo|flag|flagge|wappen|coat.of.arms|karte|map|locator|symbol|signatur|"
                             r"signature|unterschrift|commons-logo|wiktionary|wikidata|disambig|stub|pictogram|"
                             r"diagram|diagramm|grafik|chart|\.svg$|\.gif$|\.tif)", re.I)


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def _ohne_html(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip()


def _bildinfos(api, dateien):
    """Holt Lizenz, Urheber, Groesse fuer eine Liste von Dateinamen und filtert auf freie, brauchbare Fotos."""
    out = []
    for i in range(0, len(dateien), 40):
        try:
            r = requests.get(api, headers=WIKI_HEADERS, timeout=40, params={
                "action": "query", "format": "json", "formatversion": 2, "titles": "|".join(dateien[i:i + 40]),
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
                "iiextmetadatalanguage": "de"})
            seiten = r.json().get("query", {}).get("pages", [])
        except Exception as e:
            log(f"Bildinfos nicht ladbar: {e.__class__.__name__}")
            continue
        for seite in seiten:
            ii = (seite.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata", {})
            lizenz = meta.get("LicenseShortName", {}).get("value", "")
            if ii.get("mime") not in ("image/jpeg", "image/png") or min(ii.get("width", 0), ii.get("height", 0)) < 450:
                continue
            if not any(l in lizenz.lower() for l in FREIE_LIZENZEN):
                continue
            out.append({
                "datei": seite["title"],
                "url": ii.get("thumburl") or ii.get("url"),
                "beschreibung": _ohne_html(meta.get("ImageDescription", {}).get("value", ""))[:160]
                                or seite["title"].split(":", 1)[-1].rsplit(".", 1)[0],
                "urheber": _ohne_html(meta.get("Artist", {}).get("value", ""))[:80] or "unbekannt",
                "lizenz": lizenz,
            })
    return out


def wiki_bilder(titel, maximal=20):
    """Echte Bilder zum Thema: aus dem deutschen + englischen Artikel und passende Fotos auf Wikimedia Commons."""
    dateien, englisch = [], None
    try:
        daten = wiki(prop="images|langlinks", titles=titel, imlimit=60, lllang="en", redirects=1)
        seite = daten["query"]["pages"][0]
        dateien += [i["title"] for i in seite.get("images", [])]
        englisch = (seite.get("langlinks") or [{}])[0].get("title")
    except Exception as e:
        log(f"Artikel-Bilder nicht ladbar: {e.__class__.__name__}")
    if englisch:
        try:
            r = requests.get("https://en.wikipedia.org/w/api.php", headers=WIKI_HEADERS, timeout=30, params={
                "action": "query", "format": "json", "formatversion": 2, "prop": "images",
                "titles": englisch, "imlimit": 60, "redirects": 1})
            dateien += [i["title"].replace("File:", "Datei:") for i in
                        r.json()["query"]["pages"][0].get("images", [])]
        except Exception:
            pass
    # Commons-Suche nach dem Thema (deutscher + englischer Titel)
    for q in dict.fromkeys([titel, englisch] if englisch else [titel]):
        try:
            r = requests.get(COMMONS_API, headers=WIKI_HEADERS, timeout=30, params={
                "action": "query", "format": "json", "formatversion": 2, "list": "search",
                "srsearch": f"{q} filetype:bitmap", "srnamespace": 6, "srlimit": 15})
            dateien += [t["title"].replace("File:", "Datei:") for t in r.json().get("query", {}).get("search", [])]
        except Exception:
            pass
    dateien = [d for d in dict.fromkeys(dateien) if not BILD_AUSSCHLUSS.search(d)]
    if not dateien:
        log("Keine Bilder zum Thema gefunden")
        return []
    bilder = _bildinfos(COMMONS_API, [d.replace("Datei:", "File:") for d in dateien])
    # Dubletten (gleiches Bild in anderer Groesse) raus
    gesehen, eindeutig = set(), []
    for b in bilder:
        schluessel = re.sub(r"[^a-z]", "", b["datei"].lower())[:30]
        if schluessel not in gesehen:
            gesehen.add(schluessel)
            eindeutig.append(b)
    log(f"{len(eindeutig[:maximal])} echte Bilder zum Thema gefunden (Artikel + Commons)")
    return eindeutig[:maximal]


# ================================================================ Thema + Skript
def lade_benutzte():
    if USED_FILE.exists():
        return json.loads(USED_FILE.read_text(encoding="utf-8"))
    return []


def speichere_thema(thema):
    benutzt = lade_benutzte()
    benutzt.append(thema)
    USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    USED_FILE.write_text(json.dumps(benutzt, ensure_ascii=False, indent=1), encoding="utf-8")


WUNSCH_THEMA = os.environ.get("WUNSCH_THEMA", "").strip()
TEIL2 = WUNSCH_THEMA.endswith(" TEIL2")
SERIE = None                         # heutiger Themen-Tag (wird in themen_pool gesetzt)
ANDERS_ALS = []                      # Titel/Themen verworfener Videos fuer denselben Termin (-> neuer Blickwinkel)
if TEIL2:
    WUNSCH_THEMA = WUNSCH_THEMA[:-6].strip()
WUNSCH_HINWEIS = ""
HOOK_STILE_TEXT = ""
HOOK_ZULETZT = ""
_VERSUCHT = set()                    # in diesem Lauf schon probierte Artikel
_KANDIDATEN = None                   # Themen-Pool fuer diesen Lauf (wird einmal gebaut)


# ================================================================ Themenfindung (viral statt 0815)
VIRAL_KRITERIEN = """WAS VIRAL GEHT (fuer ein breites Publikum, Jugendliche UND Erwachsene, ohne Vorwissen):
- Geheimnisse, Mysterien, ungeklaerte Faelle, Verschwoerungen, die WIRKLICH passiert sind
- Krasse Geschichten: Katastrophen, Ueberlebende, Verbrechen, Heists, Fluchten, Betrueger, Spione
- "Das glaubt mir keiner"-Fakten: verrueckte Zufaelle, absurde Regeln, irre Rekorde, gefaehrliche Experimente
- Sachen, die JEDER kennt, aber keiner die Hintergruende: Marken, Apps, Handy, Internet, Games, Essen, Sport
- Gruseliges, Ekliges, Lustiges, Gefaehrliches; Tiere und Natur, die unglaublich sind
NICHT viral (0815): unbekannte Personen, Fernsehsendungen, Buerokratie, Politik, Religion, Kunst, Architektur,
Adelsfamilien, Vereine, Doerfer, Firmen ohne Story, trockene Wissenschaft, alles mit vielen Namen und Jahreszahlen."""


def artikel_finden(titel):
    """Gibt den echten Wikipedia-Titel zurueck (auch ueber Umleitungen) oder None."""
    try:
        seite = wiki(prop="info|pageprops", titles=titel, redirects=1)["query"]["pages"][0]
        if not seite.get("missing") and "disambiguation" not in (seite.get("pageprops") or {}):
            return seite["title"]
        r = requests.get(WIKI_API, headers=WIKI_HEADERS, timeout=30, params={
            "action": "opensearch", "search": titel, "limit": 1, "namespace": 0, "redirects": "resolve"})
        vorschlag = (r.json()[1] or [None])[0]
        return vorschlag if vorschlag and "(Begriffsklärung)" not in vorschlag else None
    except Exception:
        return None


def ideen_brainstorm(benutzt, wunsch="", anzahl=14):
    """Die KI schlaegt konkrete, wirklich spannende Geschichten vor (mit echtem Wikipedia-Artikel)."""
    zuletzt = ", ".join(list(benutzt)[-60:]) or "(keine)"
    if wunsch:
        auftrag = (f'Der Kanalbetreiber will ein Video zu: "{wunsch}".\n'
                   f"Interpretiere das wie ein Mensch, nicht woertlich. Schlage {anzahl} KONKRETE, ECHTE Geschichten, "
                   f"Ereignisse, Orte oder Dinge vor, die genau dazu passen und die Leute WIRKLICH fesseln. "
                   f'Beispiel: "Russland groesstes Geheimnis" -> Djatlow-Pass-Vorfall, Kola-Bohrung, Tunguska-Ereignis, '
                   f"Bernsteinzimmer, geheime Atomstaedte ... (nicht: irgendeine unbekannte Person).")
    else:
        auftrag = (f"Schlage {anzahl} KONKRETE, ECHTE Themen fuer die naechsten Videos vor, bunt gemischt: "
                   f"ca. ein Drittel Geschichte und Mysterien, ein Drittel Dinge von HEUTE (Handy, Internet, Apps, "
                   f"Games, Marken, Essen, Sport, Technik, Raumfahrt, menschlicher Koerper), ein Drittel Natur, Tiere, "
                   f"Rekorde und krasse Ereignisse. Nichts, was jeder schon tausendmal gehoert hat.")
    prompt = f"""Du bist Ideen-Chef eines deutschen Shorts-Kanals mit dem Ziel, viral zu gehen.
{auftrag}

{VIRAL_KRITERIEN}

Jedes Thema muss einen eigenen DEUTSCHEN Wikipedia-Artikel haben; gib dessen exakten Titel an.
Schon gemacht (nicht nochmal): {zuletzt}

Antworte NUR mit JSON:
{{"ideen": [{{"wikipedia_titel": "...", "hook": "der krasseste Fakt in einem Satz"}}]}}"""
    try:
        antwort, _ = ki(prompt, temperatur=0.9)
    except Exception as e:
        log(f"Ideen-Brainstorm nicht moeglich: {e}")
        return {}
    ideen = {}
    for idee in antwort.get("ideen", [])[:anzahl + 4]:
        titel = artikel_finden(str(idee.get("wikipedia_titel", "")).strip())
        if titel and not schon_benutzt(titel, benutzt) and titel not in ideen:
            ideen[titel] = str(idee.get("hook", ""))[:200]
    log(f"Ideen ({'Wunsch: ' + wunsch if wunsch else 'frei'}): {len(ideen)} mit Wikipedia-Artikel")
    return ideen


TREND_AUSSCHLUSS = re.compile(r"^(Hauptseite|Spezial:|Wikipedia:|Datei:|Liste |Nekrolog|Deutschland$|Österreich$)")


def trend_kandidaten(benutzt, anzahl=8):
    """Was die Leute gerade WIRKLICH lesen: die meistgelesenen deutschen Wikipedia-Artikel von gestern."""
    tag = (datetime.utcnow() - timedelta(days=1)).strftime("%Y/%m/%d")
    try:
        r = requests.get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/de.wikipedia/all-access/{tag}",
                         headers=WIKI_HEADERS, timeout=30)
        artikel = r.json()["items"][0]["articles"]
    except Exception as e:
        log(f"Trends nicht abrufbar: {e.__class__.__name__}")
        return []
    titel = [a["article"].replace("_", " ") for a in artikel[:80]]
    titel = [t for t in titel if not TREND_AUSSCHLUSS.search(t) and not schon_benutzt(t, benutzt)]
    log(f"Trends von gestern: {', '.join(titel[:10])} …")
    return titel[:anzahl]


IDEEN_DATEI = ROOT / "data" / "ideen.json"


def ideen_laden():
    try:
        d = json.loads(IDEEN_DATEI.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("warteschlange", [])          # deine Wunsch-Themen fuer die naechsten automatischen Videos
    d.setdefault("backlog", {})                # von der KI vorgedachte Ideen (Titel -> Hook)
    return d


def ideen_speichern(d):
    IDEEN_DATEI.parent.mkdir(parents=True, exist_ok=True)
    d["backlog"] = dict(list(d["backlog"].items())[-60:])
    IDEEN_DATEI.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def ideen_verbraucht(titel):
    d = ideen_laden()
    if d["backlog"].pop(titel, None) is not None:
        ideen_speichern(d)


MIN_LESER = int(os.environ.get("MIN_LESER", "40"))          # Leser pro Tag (Median), darunter = Nischenthema
MIN_LESER_PERSON = int(os.environ.get("MIN_LESER_PERSON", "400"))   # Personen nur, wenn wirklich bekannt


def beliebtheit(titel_liste, tage=60):
    """Wie viele Leute lesen den Artikel normalerweise? Median der Tagesaufrufe (Ausreisser durch News zaehlen nicht)."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote
    ende = jetzt_utc().date() - timedelta(days=1)
    start = ende - timedelta(days=tage)

    def holen(t):
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/de.wikipedia/all-access/user/"
               f"{quote(t.replace(' ', '_'), safe='')}/daily/{start:%Y%m%d}/{ende:%Y%m%d}")
        try:
            r = requests.get(url, headers=WIKI_HEADERS, timeout=20)
            if r.status_code == 404:
                return t, 0
            werte = sorted(x["views"] for x in r.json().get("items", []))
            werte += [0] * max(0, tage - len(werte))       # Tage ohne Eintrag = 0 Aufrufe
            return t, sorted(werte)[len(werte) // 2]
        except Exception:
            return t, None
    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(holen, titel_liste))


def sind_personen(titel_liste):
    """Welche Artikel handeln von einem einzelnen Menschen? (Wikidata: 'ist ein' = Mensch)"""
    personen = set()
    try:
        for i in range(0, len(titel_liste), 40):
            d = wiki(prop="pageprops", ppprop="wikibase_item", titles="|".join(titel_liste[i:i + 40]), redirects=1)
            qids = {p["pageprops"]["wikibase_item"]: p["title"] for p in d["query"]["pages"] if p.get("pageprops")}
            if not qids:
                continue
            e = requests.get(WIKIDATA_API, headers=WIKI_HEADERS, timeout=30, params={
                "action": "wbgetentities", "ids": "|".join(qids), "props": "claims", "format": "json"}).json()
            for q, ent in e.get("entities", {}).items():
                for c in ent.get("claims", {}).get("P31", []):
                    if ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id") == "Q5":
                        personen.add(qids.get(q))
    except Exception as ex:
        log(f"Personen-Check nicht moeglich: {ex}")
    return personen


def jetzt_utc():
    return datetime.now().astimezone()


def themen_pool(benutzt):
    """Kandidaten aus 3 Quellen: KI-Ideen (konkret + spannend), aktuelle Trends, Zufall aus Wikipedia."""
    global _KANDIDATEN, SERIE
    if _KANDIDATEN is not None:
        return _KANDIDATEN
    SERIE = None if WUNSCH_THEMA else themen_tag()
    pool = {}
    if WUNSCH_THEMA:
        pool.update({t: ("ideen", h) for t, h in ideen_brainstorm(set() if TEIL2 else benutzt, WUNSCH_THEMA, 10).items()})
        direkt = artikel_finden(WUNSCH_THEMA)
        if direkt and direkt not in pool:
            pool[direkt] = ("direkt", "")
    elif SERIE:
        log(f"Themen-Tag: {SERIE[0]}")
        pool.update({t: ("serie", h) for t, h in ideen_brainstorm(benutzt, SERIE[1], 12).items()})
        if len(pool) < 4:                         # Serie liefert zu wenig -> normal weiter
            for t in trend_kandidaten(benutzt):
                pool.setdefault(t, ("trend", ""))
    if not WUNSCH_THEMA and (not SERIE or len(pool) < 4):
        # Ideen-Speicher nutzen (spart KI-Anfragen), bei Bedarf auffuellen
        d = ideen_laden()
        backlog = {t: h for t, h in d["backlog"].items() if not schon_benutzt(t, benutzt)}
        if len(backlog) < 10:
            neu = ideen_brainstorm(set(benutzt) | set(backlog), anzahl=18)
            backlog.update(neu)
            d["backlog"] = backlog
            ideen_speichern(d)
        auswahl = list(backlog.items())
        random.shuffle(auswahl)
        pool.update({t: ("ideen", h) for t, h in auswahl[:14]})
        for t in trend_kandidaten(benutzt):
            pool.setdefault(t, ("trend", ""))
        if len(pool) < 8:                         # Reserve, falls die KI-Ideen ausfallen
            for t in wiki_kandidaten(benutzt, anzahl=8):
                pool.setdefault(t, ("zufall", ""))
    titel = list(pool)[:30]
    try:
        intros = {}
        for i in range(0, len(titel), 20):
            daten = wiki(prop="extracts", exintro=1, explaintext=1, exlimit=20, titles="|".join(titel[i:i + 20]),
                         redirects=1)
            intros.update({p["title"]: p.get("extract", "")[:260] for p in daten["query"]["pages"] if p.get("extract")})
    except Exception as e:
        log(f"Intros nicht ladbar: {e}")
        intros = {t: "" for t in titel}
    kand = {t: {"quelle": pool[t][0], "hook": pool[t][1], "intro": intros.get(t, "")} for t in titel if t in intros}
    # Beliebtheits- und Personen-Check: keine Nischenthemen, keine Portraets unbekannter Leute
    try:
        leser = beliebtheit(list(kand))
        personen = sind_personen(list(kand))
        raus = []
        for t, v in kand.items():
            v["leser"] = leser.get(t)
            v["person"] = t in personen
            if WUNSCH_THEMA or v["leser"] is None:
                continue
            if v["leser"] < MIN_LESER or (v["person"] and v["leser"] < MIN_LESER_PERSON):
                raus.append(t)
        if len(kand) - len(raus) >= 3:
            for t in raus:
                kand.pop(t)
        else:                                      # zu wenig uebrig -> die beliebtesten Nicht-Personen behalten
            gut = sorted((t for t in kand if not kand[t]["person"]), key=lambda t: -(kand[t]["leser"] or 0))[:6]
            kand = {t: kand[t] for t in gut} or kand
        log(f"Beliebtheits-Check: {len(raus)} Nischenthemen/unbekannte Personen aussortiert, {len(kand)} bleiben")
    except Exception as e:
        log(f"Beliebtheits-Check uebersprungen: {e}")
    _KANDIDATEN = kand
    return _KANDIDATEN


def thema_waehlen(benutzt):
    kandidaten = {t: v for t, v in themen_pool(benutzt).items() if t not in _VERSUCHT}
    if not kandidaten:
        raise RuntimeError("Keine Themen-Kandidaten mehr")
    liste = "\n\n".join(f"### {t}  [{v['quelle']}]"
                        + (f" (~{v['leser']} Leser/Tag)" if v.get("leser") else "")
                        + (" [PERSON]" if v.get("person") else "")
                        + f"\n{v['hook'] + ' | ' if v['hook'] else ''}{v['intro']}"
                        for t, v in kandidaten.items())
    wunsch = (f'\nDER KANALBETREIBER WUENSCHT SICH DAS THEMA: "{WUNSCH_THEMA}". Nimm das Thema, das diesen Wunsch '
              f"am besten trifft UND am spannendsten ist.\n") if WUNSCH_THEMA else ""
    if ANDERS_ALS:
        wunsch += ("\nVERWORFEN wurde bereits: " + "; ".join(ANDERS_ALS[-3:]) + ". Nimm moeglichst einen ANDEREN Artikel "
                   "zum selben Wunsch (oder denselben nur, wenn es keinen passenden anderen gibt).\n")
    if SERIE and not WUNSCH_THEMA:
        wunsch += (f'\nHEUTE IST THEMEN-TAG "{SERIE[0]}" ({SERIE[1]}). Nimm bevorzugt ein [serie]-Thema, '
                   f"das wirklich dazu passt.\n")
    prompt = f"""Du bist Chefredakteur eines deutschen Shorts-Kanals, der in den naechsten Tagen viral gehen soll.
Waehle aus diesen Wikipedia-Artikeln den EINEN, aus dem das fesselndste 50-Sekunden-Video wird.
[ideen] = Vorschlag der Redaktion, [trend] = wird gerade von vielen gelesen, [zufall]/[direkt] = sonstige.
{wunsch}
{VIRAL_KRITERIEN}

{erfolgs_profil()}

Frag dich ehrlich: Wuerde ein normaler Mensch beim Scrollen STOPPEN und das danach Freunden erzaehlen?
Nutze die Lerndaten oben (falls vorhanden): mehr von dem, was bei uns gut lief, weniger von den Flops.
Bewerte (1-10): "viral" (Stopp- und Weitererzaehl-Faktor), "verstaendlich" (ohne Vorwissen sofort klar).
Nur ein Thema mit viral >= 8 und verstaendlich >= 8. Trends nur, wenn sich daraus eine echte Story machen laesst
(keine reinen Promi- oder Nachrichtenmeldungen, keine Todesfaelle von Personen).
"Leser/Tag" zeigt, wie viele Menschen sich normalerweise dafuer interessieren - mehr ist meist besser.
KEINE Portraets von Personen [PERSON], die die breite Masse nicht kennt (Journalisten, Lokalpolitiker, Sportler
aus Randsportarten, Wissenschaftler ...). Eine Person geht nur, wenn ihre Geschichte fuer sich allein krass ist
(z. B. ein beruehmter Hochstapler) - dann geht es um die GESCHICHTE, nicht um den Lebenslauf.
WEITERERZAEHL-TEST: Formuliere in "weitererzaehlen" den EINEN Satz, mit dem man das morgen einem Freund erzaehlt
("Alter, wusstest du, dass ..."). Er muss sofort ein "Was?! Echt?!" ausloesen. Wenn dir kein solcher Satz einfaellt,
ist das Thema nicht gut genug - nimm ein anderes.

{liste}

Antworte NUR mit JSON: {{"titel": "exakter Titel aus der Liste", "story": "die Story in einem kurzen Satz",
"weitererzaehlen": "Alter, wusstest du, dass ...", "unbekannte_person": true/false,
"viral": 1-10, "verstaendlich": 1-10}}"""
    bester = None
    for runde in range(3):                         # bis zu 3 Anlaeufe, bis ein Thema den Test besteht
        wahl, _ = ki(prompt, temperatur=0.4 + 0.15 * runde)
        titel = wahl.get("titel", "")
        if titel not in kandidaten:
            titel = (difflib.get_close_matches(titel, list(kandidaten), n=1, cutoff=0) or list(kandidaten))[0]
        try:
            punkte = int(wahl.get("viral", 0)) + int(wahl.get("verstaendlich", 0))
        except (TypeError, ValueError):
            punkte = 0
        satz = str(wahl.get("weitererzaehlen", "")).strip()
        besteht = (punkte >= 16 and len(satz) > 25 and not wahl.get("unbekannte_person")
                   and not (kandidaten[titel].get("person") and (kandidaten[titel].get("leser") or 0) < MIN_LESER_PERSON
                            and not WUNSCH_THEMA))
        if bester is None or (besteht, punkte) > (bester[2], bester[1]):
            bester = (titel, punkte, besteht, wahl)
        if besteht:
            break
        log(f"Thema „{titel}“ faellt durch den Weitererzaehl-Test (viral {wahl.get('viral')}, "
            f"Person: {wahl.get('unbekannte_person')}) – suche ein besseres")
        _VERSUCHT.add(titel)
        kandidaten.pop(titel, None)
        if not kandidaten:
            break
        liste_neu = "\n\n".join(b for b in liste.split("\n\n") if not b.startswith(f"### {titel} "))
        prompt = prompt.replace(liste, liste_neu)
        liste = liste_neu
    titel, _, _, wahl = bester
    _VERSUCHT.add(titel)
    log(f"Gewaehlt: {titel} [{(_KANDIDATEN or {}).get(titel, {}).get('quelle', '?')}] (viral {wahl.get('viral')}, "
        f"verstaendlich {wahl.get('verstaendlich')}) – „{wahl.get('weitererzaehlen', wahl.get('story', ''))}“")
    return titel


HOOK_STILE = {
    "Schock-Fakt": 'den krassesten Fakt direkt als Aussage ("Dieses Schiff galt als unsinkbar. Es sank nach vier Tagen.")',
    "Frage": 'eine Frage, auf die man die Antwort wissen will ("Warum durften Koenige nicht einfach verreisen?")',
    "Stell dir vor": 'den Zuschauer mitten in die Situation werfen ("Stell dir vor, dein Arzt verschreibt dir Quecksilber.")',
    "Widerspruch": 'etwas, das falsch klingt, aber stimmt ("Das erste Handy wog so viel wie eine Packung Milch.")',
    "Zeitsprung": 'mitten in der Szene starten ("Es ist Nacht, 1912. Und keiner auf dem Schiff ahnt etwas.")',
    "Mythos-Check": 'einen bekannten Irrtum aufgreifen ("Alle glauben, Wikinger hatten Hoerner am Helm. Falsch.")',
    "Wusstest du": 'klassisch, aber nur ab und zu ("Wusstest du, dass ...")',
}
HOOK_DATEI = ROOT / "data" / "hooks.json"


def hook_zuletzt():
    try:
        return json.loads(HOOK_DATEI.read_text(encoding="utf-8"))[-3:]
    except Exception:
        return []


def hook_merken(stil):
    liste = hook_zuletzt() + [stil]
    HOOK_DATEI.parent.mkdir(parents=True, exist_ok=True)
    HOOK_DATEI.write_text(json.dumps(liste[-10:], ensure_ascii=False), encoding="utf-8")


def skript_erstellen(titel):
    global WUNSCH_HINWEIS, HOOK_STILE_TEXT, HOOK_ZULETZT
    HOOK_ZULETZT = ", ".join(hook_zuletzt()) or "(noch keine)"
    profil = erfolgs_profil()
    if profil:
        HOOK_ZULETZT += "\n  " + profil.replace("\n", "\n  ")
    HOOK_STILE_TEXT = "\n".join(f"  * {k}: {v}" for k, v in HOOK_STILE.items())
    titel, artikel, quellen = quellen_sammeln(titel)
    WUNSCH_HINWEIS = (f"\n- WUNSCH DES KANALBETREIBERS: Das Video soll um \"{WUNSCH_THEMA}\" gehen. Bleib genau bei "
                      f"diesem Thema, auch wenn der Artikel breiter ist.") if WUNSCH_THEMA else ""
    if ANDERS_ALS:
        WUNSCH_HINWEIS += ("\n- NEU GEMACHT: Diese Version(en) wurden vom Kanalbetreiber verworfen: "
                           + "; ".join(ANDERS_ALS[-3:]) + ". Mach es deutlich ANDERS: anderer Einstieg, anderer "
                           "Blickwinkel, andere Fakten in den Vordergrund, anderer Titel.")
    if TEIL2:
        WUNSCH_HINWEIS += ("\n- DAS IST TEIL 2: Zu diesem Thema gab es schon ein Video. Nimm ANDERE Fakten und einen anderen "
                           "Blickwinkel als die offensichtlichsten. Der Titel enthaelt \"Teil 2\". Der Einstieg darf "
                           "kurz an Teil 1 anknuepfen (\"Im letzten Video ...\"), aber nur einen Satz lang.")
    bilder = wiki_bilder(titel)
    bildliste = "\n".join(f"{i}: {b.get('beschreibung') or b.get('datei', '')}" for i, b in enumerate(bilder)) or "(keine Bilder vorhanden)"
    prompt = f"""Du bist Autor des deutschen Faceless-Kanals FAKTENFIEBER (YouTube Shorts + Instagram Reels).
Schreibe ein Short ueber "{titel}". Du hast drei Quellen: den deutschen und (falls vorhanden) den englischen
Wikipedia-Artikel sowie exakte Eckdaten aus Wikidata.

ABSOLUTE REGEL: Verwende NUR Fakten, die so in diesen Quellen stehen. Nichts dazuerfinden,
nichts aus deinem eigenen Wissen ergaenzen, nicht uebertreiben, keine Zahlen runden oder aendern.
- Der englische Artikel ist oft ausfuehrlicher: Nutze ihn fuer spannende Details, die im deutschen fehlen,
  und uebersetze sie sauber ins Deutsche.
- Widersprechen sich die Quellen bei einer Zahl, einem Datum oder Namen, lass sie weg oder formuliere vorsichtig
  ("etwa", "ueber"), aber nur, wenn das durch BEIDE Quellen gedeckt ist.

QUELLEN:
\"\"\"{artikel}\"\"\"

ECHTE BILDER ZUM THEMA (Nummer: Beschreibung):
{bildliste}

STIL:
- 90 bis 110 Woerter gesprochener Text (ca. 45-52 Sekunden). NIEMALS mehr als 120 Woerter! Jedes Wort muss sitzen.
- EINSTIEG (die ersten 2 Sekunden entscheiden, ob jemand weiterwischt!): Du entscheidest selbst, wie du anfaengst.
  Er muss sofort neugierig machen und darf NICHT langweilig oder erklaerend beginnen. Moegliche Hook-Stile:
{HOOK_STILE_TEXT}
  Zuletzt benutzt (bitte NICHT schon wieder): {HOOK_ZULETZT}
  Gib den gewaehlten Stil in "hook_stil" an. Nicht immer mit "Wusstest du" anfangen.
- GUT ERKLAEREN: Der Zuschauer weiss NICHTS ueber das Thema. Mach klar, WAS es ist, WO und WANN,
  und WARUM es so besonders oder verrueckt ist. Konkrete Zahlen und Details aus dem Artikel nennen.
  ROTER FADEN: Erzaehl EINE Geschichte, keine Faktenliste. Jeder Satz knuepft an den vorherigen an
  (deshalb, aber, dann, das Problem:, und genau da ...). Erst einfuehren, dann darueber reden.
- SO ERKLAEREN, ALS WUERDEST DU ES EINEM KUMPEL BEIM BIER ODER IN DER PAUSE ERZAEHLEN:
  Fuer Jugendliche UND Erwachsene: nicht kindisch, nicht belehrend, aber ohne Vorwissen sofort verstaendlich.
  * Einfache Alltagssprache, kurze Saetze (max. 12 Woerter), keine Schachtelsaetze, kein Behoerdendeutsch.
  * Hoechstens 2-3 Zahlen und hoechstens 1-2 Namen im ganzen Video. Jahreszahlen lieber umschreiben
    ("vor ueber 600 Jahren" statt "1347"), wenn das aus dem Artikel klar hervorgeht.
  * "Stell dir vor ..." nutzen, damit man sich reinversetzen kann. Zeig, was das fuer einen ganz normalen
    Menschen damals bedeutet hat.
  * Vergleiche mit der Welt von heute: Handy, Job, Fussballfeld, Supermarkt, Wohnung, Auto usw.
  * Gefuehl rein: Was ist daran gruselig, eklig, lustig, unfair oder genial?
- BEISPIEL, wie es klingen soll:
  SCHLECHT: "Die fruehneuzeitliche Inkognito-Praxis des Hochadels stellte ein zeremonielles Regelwerk dar."
  GUT: "Koenige durften frueher nicht einfach so verreisen. Also haben sie sich verkleidet. Wie ein Promi mit Kappe und Sonnenbrille."
  Kein Satz darf klingen wie aus Wikipedia abgeschrieben. Ein automatischer Test misst die Verstaendlichkeit,
  zu komplizierte Texte werden abgelehnt.
- KOMPLIZIERTES EINFACH MACHEN: Erklaere so, dass es jeder ohne Vorwissen sofort versteht.
  Jeder Fachbegriff, jedes Fremdwort, jeder unbekannte Ort und jede Epoche bekommt sofort eine kurze,
  einfache Erklaerung ("ein Gasometer, also ein riesiger Speicher fuer Gas"). Lieber ein Detail weglassen
  als etwas unerklaert lassen. Bildhafte Vergleiche aus dem Alltag sind erlaubt ("so hoch wie ein
  20-stoeckiges Haus"), aber nur, wenn die Zahlen dahinter aus dem Artikel stammen.
  Lieber ein Detail weglassen als die Laenge zu sprengen.
- Erklaere den Zusammenhang: Nicht nur WAS passiert ist, sondern auch WARUM und WIE. Der Zuschauer soll
  am Ende das Gefuehl haben, wirklich etwas verstanden zu haben.
- Aufbau: Hook -> Einordnung (was/wo/wann) -> Spannung aufbauen -> ueberraschendes Detail -> Pointe/Twist am Ende.
- Bei historischen Themen: Lass den Zuschauer spueren, wie anders das Leben damals war (Vergleich mit heute,
  aber nur mit Fakten aus dem Artikel). Ton: "unnuetzes Wissen", das man beim naechsten Treffen erzaehlen will.
- Bei modernen Themen (Technik, Internet, Apps, Marken, Alltag heute): Such die ueberraschendsten Fakten raus,
  die kaum einer kennt (Ursprung, verrueckte Anfaenge, krasse Zahlen, kuriose Details, "Was, echt?!").
  Nichts, was jeder schon weiss. Keine Werbung fuer Firmen, neutral bleiben.{WUNSCH_HINWEIS}
- Kurze, lebendige Saetze, Du-Ansprache, locker aber nicht cringe (keine Jugendwoerter erzwingen). Keine Emojis, keine Klammern,
  keine Abkuerzungen, keine Sonderzeichen. Der Text wird von einer Computerstimme vorgelesen.
- 8 bis 12 Szenen, jede Szene genau EIN kurzer Satz (fuer schnelle Schnitte).
- VIRAL-REGELN:
  * Der erste Satz enthaelt SOFORT den krassesten Fakt. Kein Anlauf, keine Einleitung, max. 12 Woerter.
    Verboten als Einstieg: "Heute", "In diesem Video", "Hast du schon mal", "Lass uns", "Es war einmal".
  * Offene Schleife: Im 2. oder 3. Satz andeuten, dass noch etwas Verrueckteres kommt
    (z. B. "Aber das ist noch nicht mal das Verrueckteste."), und es am Ende wirklich liefern.
  * Ende: Entweder ein ECHTER Loop (der letzte Satz bricht so ab, dass der erste Satz ihn inhaltlich und
    grammatikalisch vollendet) ODER eine starke Pointe. Floskeln wie "und deshalb fragen sich heute noch viele"
    sind VERBOTEN, wenn der erste Satz nicht wirklich die Antwort darauf ist.
    KEIN "Folg mir", kein "Abonnieren", keine Verabschiedung im gesprochenen Text.
- BILDER: Es duerfen NUR Bilder gezeigt werden, die wirklich das Thema zeigen. Nichts Beliebiges!
  "bilder": Liste mit 1-2 Nummern ECHTER Bilder aus der Liste oben, die zu diesem Satz passen.
            Die echten Bilder sind IMMER die erste Wahl. Du darfst ein Bild in mehreren Szenen verwenden.
  "suchbegriff_en": englischer Stock-Video-Suchbegriff (2-4 Woerter), NUR wenn ein Stock-Video GENAU das
            zeigen kann, was im Satz passiert, inkl. Epoche/Ort (z. B. "medieval town street", "1930s steam train",
            "octopus swimming"). Wenn kein Stock-Video wirklich passt: null.
- Jede Faktenbehauptung einzeln auflisten, mit "beleg" = exaktes, woertliches Zitat (5-25 Woerter)
  aus einer der Quellen, das die Behauptung belegt. Copy-Paste in der ORIGINALSPRACHE (englische Zitate bleiben
  englisch, Wikidata-Zeilen genau so wie oben), kein Umformulieren, nicht uebersetzen!
- Titel, Beschreibung und Hashtags fuer YouTube/Instagram selbst erstellen.

Antworte NUR mit JSON:
{{
  "thema": "{titel}",
  "titel": "max. 45 Zeichen, KEIN Doppelpunkt, eine klare Neugier-Luecke, ohne Kontext verstaendlich, keine Luege",
  "szenen": [{{"text": "...", "bilder": [0], "suchbegriff_en": "... oder null"}}],
  "behauptungen": [{{"aussage": "...", "beleg": "woertliches Zitat aus dem Artikel"}}],
  "beschreibung": "2-3 Saetze, erzaehlt neugierig weiter, endet mit Aufforderung zum Folgen",
  "hashtags": ["#fakten", "#unnützeswissen", "..."],
  "hook_stil": "Name des gewaehlten Hook-Stils",
  "musik_stimmung": "eins von: spannend, mysterioes, dramatisch, froehlich, episch, historisch, kurios, ruhig",
  "suchbegriff_thema": "englischer Stock-Video-Suchbegriff fuer das Thema allgemein (2-3 Woerter)",
  "hook_text": "Texteinblendung fuer die ersten Sekunden: max. 6 Woerter, der krasseste Fakt, wahr laut Artikel",
  "kommentar_frage": "kurze Frage an die Zuschauer fuer die Kommentare, passend zum Thema"
}}"""
    daten, modell = ki(prompt, temperatur=0.8)
    daten["thema"] = titel
    daten["_modell"] = modell
    felder_auffuellen(daten)
    daten["_artikel"] = artikel
    daten["quellen"] = quellen
    daten["_bilder"] = bilder
    daten["_quelle"] = ((_KANDIDATEN or {}).get(titel) or {}).get("quelle", "wunsch" if WUNSCH_THEMA else "")
    if SERIE and daten["_quelle"] == "serie":
        tag = "#" + re.sub(r"[^a-zäöüß]", "", SERIE[0].lower())
        daten["hashtags"] = [tag] + [h for h in daten.get("hashtags", []) if h.lower() != tag][:6]
        daten["_serie"] = SERIE[0]
    return daten


def felder_auffuellen(daten):
    """Falls die KI ein Feld vergisst: sinnvolle Standardwerte statt Absturz."""
    daten.setdefault("titel", daten.get("thema", "Faktenfieber"))
    if not daten.get("titel"):
        daten["titel"] = daten.get("thema", "Faktenfieber")
    if not daten.get("beschreibung"):
        daten["beschreibung"] = (f"{daten.get('hook_text') or daten['titel']}. Mehr unnuetzes Wissen, das du "
                                 f"sofort weitererzaehlen willst: Folg Faktenfieber!")
    if not daten.get("hashtags"):
        daten["hashtags"] = ["#fakten", "#wusstestdu", "#unnützeswissen", "#geschichte", "#faktenfieber"]
    daten.setdefault("behauptungen", [])
    daten.setdefault("musik_stimmung", "spannend")
    return daten


FLOSKELN = ["fragen sich heute noch viele", "fragt man sich bis heute", "und deshalb fragen sich",
            "in diesem video", "lass uns", "heute schauen wir", "hast du schon mal"]


def hook_und_loop(daten):
    """Feilt nur am ersten und letzten Satz: maximaler Catch am Anfang + Ende, das wirklich zum Anfang passt."""
    szenen = daten.get("szenen") or []
    if len(szenen) < 3:
        return daten
    text = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(szenen))
    bilder = [b for b in daten.get("_bilder", []) if not b.get("ki")]
    bilder_teil = ""
    if bilder:
        bilder_teil = ("\n4) ERSTES BILD: Die ersten 0,5 Sekunden entscheiden. Welches dieser echten Bilder ist das "
                       "spektakulaerste, auffaelligste, das zum ersten Satz passt? (Menschen, Action, Zerstoerung, "
                       "Ungewoehnliches schlagen Landkarten, Gebaeude von aussen und Portraetfotos.)\n"
                       + "\n".join(f"{i}: {(b.get('beschreibung') or b.get('datei', ''))[:110]}"
                                    for i, b in enumerate(daten.get("_bilder", [])) if not b.get("ki")) + "\n")
    prompt = f"""Du bist Experte fuer virale Shorts (YouTube/Instagram). Hier ist ein fertiges Skript, ein Satz pro Zeile.
Verbessere NUR den ERSTEN und den LETZTEN Satz und den TITEL. Benutze nur Fakten, die schon im Skript stehen.

SKRIPT:
{text}

1) ERSTER SATZ = DER CATCH. Er entscheidet in 1-2 Sekunden, ob jemand weiterwischt.
   - max. 12 Woerter, sofort der krasseste/ueberraschendste Fakt aus dem Skript, konkret statt allgemein
   - erzeugt eine Frage im Kopf ("Wie bitte? Warum?"), die erst spaeter beantwortet wird
   - verboten: "Heute", "In diesem Video", "Hast du schon mal", "Lass uns", "Es war einmal", langes Ausholen
   Schreib 3 verschiedene Varianten, bewerte jede ehrlich von 1-10 (Wuerde ICH stoppen?) und nimm die beste.

2) LETZTER SATZ = DER LOOP. Wenn das Video endet, startet es sofort neu mit dem ersten Satz.
   WICHTIG: Der erste Satz bleibt ein VOLLSTAENDIGER Satz, der allein als Catch funktioniert (wer neu reinscrollt,
   hoert zuerst ihn!). Der LETZTE Satz fuehrt so darauf hin, dass beide zusammen wie EIN Gedanke klingen.
   Beispiel:  letzter: "Und angefangen hat das ganze Drama so:"  ->  erster: "Ein einziger Arbeiter hat eine Schraube vergessen."
   Beispiel:  letzter: "Deshalb gilt bis heute die verrueckte Regel:"  ->  erster: "Koenige durften nicht einfach verreisen."
   Beispiel:  letzter: "Und jetzt weisst du auch, warum"  ->  erster: "Dieses Schiff nie wieder gebaut wurde."  <- FALSCH,
              weil der erste Satz allein keinen Sinn ergibt. Das ist verboten.
   Der letzte Satz muss sich direkt auf das beziehen, was der erste Satz sagt. Geht das nicht natuerlich, dann
   schreib stattdessen eine starke Pointe als letzten Satz und setze "loop": false.
   Keine Floskeln wie "und deshalb fragen sich heute noch viele".

3) TITEL fuer YouTube/Instagram (aktuell: "{daten.get('titel', '')}"): Schreib 3 Varianten.
   REGELN: max. 45 Zeichen. KEIN Doppelpunkt, kein "&", keine Aufzaehlung, kein "Von ... bis ...".
   EINE klare Neugier-Luecke (man will sofort wissen, was dahintersteckt), ohne Kontext verstaendlich,
   einfache Woerter, gerne mit Zahl oder Widerspruch. NICHT luegen (das Video muss es einloesen).
   GUT: "Dieses Schiff sank nach nur 20 Minuten" · "Koenige durften nicht verreisen" · "Das erste Handy wog 1 Kilo"
   SCHLECHT: "Spotify: 11 Mrd. Dollar & dein Bier-Fehler" (verwirrend) · "Katja Gloger: Von Moskau bis Washington" (Lebenslauf)
   Bewerte jede ehrlich (Wuerde ICH klicken?) und nimm die beste.
{bilder_teil}
Antworte NUR mit JSON:
{{"hook_varianten": [{{"text": "...", "punkte": 1-10}}], "erster_satz": "...", "letzter_satz": "...",
  "loop": true/false, "loop_test": "letzter Satz + erster Satz am Stueck gelesen",
  "titel_varianten": [{{"text": "...", "punkte": 1-10}}], "titel": "...", "bestes_bild": Nummer oder null}}"""
    try:
        neu, modell = ki(prompt, temperatur=0.7)
    except Exception as e:
        log(f"Hook/Loop-Feinschliff uebersprungen: {e}")
        return daten
    erster, letzter = str(neu.get("erster_satz", "")).strip(), str(neu.get("letzter_satz", "")).strip()
    ok_erster = (erster and erster[0].isupper() and erster.rstrip()[-1:] in ".!?" and len(erster.split()) <= 16
                 and not any(f in erster.lower() for f in FLOSKELN))
    ok_letzter = letzter and len(letzter.split()) <= 20 and not (
        any(f in letzter.lower() for f in FLOSKELN) and not neu.get("loop"))
    if neu.get("loop") and not ok_erster:
        ok_letzter = False                         # Loop-Ende ohne passenden Anfang waere sinnlos
    titel = titel_waehlen(neu, daten)
    if titel:
        daten["_titel_alt"] = daten.get("titel")
        daten["titel"] = titel
    try:
        nr = int(neu.get("bestes_bild"))
        if 0 <= nr < len(daten.get("_bilder", [])) and not daten["_bilder"][nr].get("ki"):
            alt = [n for n in (szenen[0].get("bilder") or []) if str(n) != str(nr)]
            szenen[0]["bilder"] = [nr] + alt[:1]
            log(f"Erstes Bild: {(daten['_bilder'][nr].get('beschreibung') or daten['_bilder'][nr].get('datei', ''))[:60]}")
    except (TypeError, ValueError):
        pass
    if ok_erster:
        szenen[0]["text"] = erster
    if ok_letzter:
        szenen[-1]["text"] = letzter
    daten["_loop"] = bool(neu.get("loop")) and ok_erster and ok_letzter
    daten["_loop_test"] = neu.get("loop_test", "")
    beste = max((v.get("punkte", 0) for v in neu.get("hook_varianten", []) if isinstance(v, dict)), default="?")
    daten["_hook_punkte"] = beste
    log(f"Hook/Loop ({modell}): Catch {beste}/10, Loop {'ja' if daten['_loop'] else 'nein (Pointe)'}")
    return daten


def roter_faden(daten):
    """Prueft, ob die Geschichte logisch durchlaeuft (jeder Satz baut auf dem vorigen auf) und glaettet Spruenge.
    Erster und letzter Satz (Catch + Loop) bleiben unangetastet, Fakten werden nicht veraendert."""
    szenen = daten.get("szenen") or []
    if len(szenen) < 5:
        return daten
    text = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(szenen))
    prompt = f"""Du bist Story-Editor fuer kurze Erklaervideos. Lies das Skript (ein Satz pro Zeile) wie ein Zuschauer,
der NICHTS ueber das Thema weiss. Thema: "{daten.get('thema')}".

{text}

Pruefe den ROTEN FADEN:
- Versteht man nach Satz 2-3, WORUM es geht (was, wo, wann)?
- Baut jeder Satz logisch auf dem vorherigen auf? Gibt es Spruenge, bei denen man denkt "Hae, wie kommt der jetzt darauf?"
- Werden Personen/Dinge eingefuehrt, BEVOR sie vorkommen? Gibt es Saetze ohne Bezug?
- Wird die offene Frage vom Anfang am Ende wirklich beantwortet?
Bewerte den roten Faden ehrlich von 1-10.
Wenn unter 8: Schreib die MITTLEREN Saetze (nicht Satz 1, nicht den letzten) so um, dass die Geschichte fluessig
durchlaeuft: Bruecken wie "Das Problem:", "Und dann", "Deshalb", "Aber", Reihenfolge aendern, Bezugswoerter klarstellen,
unnoetige Saetze streichen. KEINE neuen Fakten, keine Zahlen aendern, kurze Saetze (max. 12 Woerter) behalten,
Gesamtzahl der Saetze darf um hoechstens 2 sinken. Jeder Satz behaelt seine Nummer ("nr") aus dem Original,
damit die Bilder passen; neue Bruecken-Saetze bekommen die Nummer des Satzes, an den sie anschliessen.

Antworte NUR mit JSON: {{"punkte": 1-10, "probleme": ["..."],
"saetze": [{{"nr": 1, "text": "..."}}, ...]  (nur wenn punkte < 8, sonst leere Liste)}}"""
    try:
        urteil, modell = ki(prompt, temperatur=0.3)
    except Exception as e:
        log(f"Roter-Faden-Check uebersprungen: {e}")
        return daten
    try:
        punkte = int(urteil.get("punkte", 10))
    except (TypeError, ValueError):
        punkte = 10
    daten["_roter_faden"] = punkte
    neu = [x for x in urteil.get("saetze") or [] if isinstance(x, dict) and str(x.get("text", "")).strip()]
    if punkte >= 8 or len(neu) < len(szenen) - 2:
        log(f"Roter Faden ({modell}): {punkte}/10 ✅")
        return daten
    erster, letzter = szenen[0], szenen[-1]
    mitte = []
    for x in neu:
        try:
            nr = max(1, min(len(szenen), int(x.get("nr", 0)))) - 1
        except (TypeError, ValueError):
            continue
        if nr in (0, len(szenen) - 1):
            continue                               # Catch + Loop bleiben, wie sie sind
        vorlage = dict(szenen[nr])
        vorlage["text"] = str(x["text"]).strip()
        mitte.append(vorlage)
    if len(mitte) < len(szenen) - 4:
        log(f"Roter Faden: Umbau unbrauchbar, behalte Original ({punkte}/10)")
        return daten
    daten["szenen"] = [erster] + mitte + [letzter]
    log(f"Roter Faden ({modell}): {punkte}/10 -> geglaettet ({'; '.join(map(str, urteil.get('probleme', [])))[:150]})")
    return daten


TITEL_MAX = 45


def titel_gueltig(t):
    return 8 <= len(t) <= TITEL_MAX and ":" not in t and "&" not in t and " bis " not in t.lower()


def titel_waehlen(neu, daten):
    """Bester Titel, der die Regeln erfuellt (max. 45 Zeichen, kein Doppelpunkt ...)."""
    kandidaten = []
    for v in neu.get("titel_varianten", []) if isinstance(neu.get("titel_varianten"), list) else []:
        if isinstance(v, dict):
            try:
                kandidaten.append((float(v.get("punkte", 0)), str(v.get("text", "")).strip().strip('"')))
            except (TypeError, ValueError):
                pass
    gewaehlt = str(neu.get("titel", "")).strip().strip('"')
    kandidaten.append((99, gewaehlt))                   # die KI-Wahl zuerst, wenn sie gueltig ist
    for _, t in sorted(kandidaten, key=lambda x: -x[0]):
        if titel_gueltig(t):
            return t
    for t in (daten.get("titel", ""), daten.get("hook_text", "")):
        if titel_gueltig(str(t)):
            return str(t)
    # letzter Ausweg: kuerzester Kandidat, sauber an einer Wortgrenze gekuerzt
    t = min((t for _, t in kandidaten if t), key=len, default="").split(":")[-1].replace("&", "und").strip()
    while len(t) > TITEL_MAX and " " in t:
        t = t.rsplit(" ", 1)[0]
    return t if len(t) >= 8 else ""


def _norm(t):
    t = t.lower().replace("„", '"').replace("“", '"').replace("”", '"').replace("–", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%-]", " ", t)).strip()


def beleg_gefunden(beleg, artikel):
    """Programmatischer Check: steht das Zitat wirklich im Artikel? (kleine Abweichungen ok)"""
    b, a = _norm(beleg), _norm(artikel)
    if len(b) >= 10 and b in a:              # auch kurze, exakte Wikidata-Zeilen ("Todesopfer: 9")
        return True
    if len(b) < 15:
        return False
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size >= 0.85 * len(b)


MAX_SCHWIERIGKEIT = float(os.environ.get("MAX_SCHWIERIGKEIT", "7"))   # Standard, per Telegram aenderbar (/schwierigkeit)
SCHWIERIGKEIT_MIN, SCHWIERIGKEIT_MAX = 4, 10
SKALA = ("4 = sehr leicht, wie ein Gespräch unter Freunden\n"
         "5 = leicht, einfache Alltagssprache\n"
         "6 = leicht mit etwas mehr Details\n"
         "7 = mittel (Standard) – wie ein lockerer Zeitungsartikel\n"
         "8 = anspruchsvoller, längere Sätze\n"
         "9–10 = schon recht schwer, eher wie ein Sachbuch")


def aktuelle_grenze():
    """Grenze aus dem gespeicherten Status (per /schwierigkeit gesetzt), sonst Standard."""
    try:
        wert = float(json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8")).get("max_schwierigkeit"))
        return max(SCHWIERIGKEIT_MIN, min(SCHWIERIGKEIT_MAX, wert))
    except Exception:
        return MAX_SCHWIERIGKEIT


def _silben(wort):
    return max(1, len(re.findall(r"[aeiouyäöü]+", wort.lower())))


def verstaendlichkeit(text, ausnahmen=""):
    """Misst, wie schwer ein Text ist (Wiener Sachtextformel) und findet die Problemstellen."""
    saetze = [x.strip() for x in re.split(r"[.!?]+", text) if x.strip()]
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß]+", text)
    if not woerter or not saetze:
        return 0.0, [], []
    n = len(woerter)
    ms = 100 * sum(_silben(w) >= 3 for w in woerter) / n
    sl = n / len(saetze)
    iw = 100 * sum(len(w) > 6 for w in woerter) / n
    es = 100 * sum(_silben(w) == 1 for w in woerter) / n
    wert = round(0.1935 * ms + 0.1672 * sl + 0.1297 * iw - 0.0327 * es - 0.875, 1)
    frei = set(re.findall(r"[A-Za-zÄÖÜäöüß]+", ausnahmen.lower()))
    lange_woerter = sorted({w for w in woerter if (len(w) >= 14 or _silben(w) >= 5) and w.lower() not in frei})
    lange_saetze = [x for x in saetze if len(x.split()) > 14]
    return wert, lange_woerter, lange_saetze


def faktencheck(daten):
    artikel = daten["_artikel"]
    # Stufe 0: Laenge (zu lange Texte werden sonst beschleunigt und klingen gehetzt)
    anzahl = len(" ".join(s["text"] for s in daten.get("szenen", [])).split())
    if anzahl > MAX_WOERTER:
        log(f"Stufe 0: Text zu lang ({anzahl} Woerter)")
        return False, {"pruefung": [], "gesamt": "nicht_ok",
                       "reparieren": [f"Der Text ist mit {anzahl} Woertern zu lang. Kuerze ihn auf hoechstens 100 Woerter, "
                                      f"indem du unwichtige Saetze streichst. Hook und Pointe bleiben."]}
    # Stufe 0b: Ist das ohne Vorwissen verstaendlich?
    sprech = " ".join(s["text"] for s in daten.get("szenen", []))
    wert, lange_woerter, lange_saetze = verstaendlichkeit(sprech, daten.get("thema", ""))
    daten["_schwierigkeit"] = wert
    grenze = aktuelle_grenze()
    daten["_grenze"] = grenze
    erlaubt_woerter = 1 if grenze <= 5 else 3 if grenze <= 7 else 5
    erlaubt_saetze = 0 if grenze <= 5 else 1 if grenze <= 7 else 2
    if wert > grenze or len(lange_woerter) > erlaubt_woerter or len(lange_saetze) > erlaubt_saetze:
        log(f"Stufe 0b: zu kompliziert (Schwierigkeit {wert}, Ziel max. {grenze}), "
            f"{len(lange_woerter)} schwere Woerter, {len(lange_saetze)} lange Saetze")
        aufgaben = [f"Der Text ist zu kompliziert (Schwierigkeit {wert}, erlaubt max. {grenze}). "
                    f"Schreib ihn so um, dass ihn wirklich JEDER ohne Vorwissen sofort versteht: "
                    f"kurze Saetze mit max. 10-12 Woertern, kurze Alltagswoerter, keine Fachbegriffe."]
        if lange_woerter:
            aufgaben.append("Ersetze diese schweren Woerter durch einfache Alltagswoerter oder erklaere sie: "
                            + ", ".join(lange_woerter[:12]))
        for satz in lange_saetze[:4]:
            aufgaben.append(f"Teile diesen langen Satz in zwei kurze: „{satz}“")
        return False, {"pruefung": [], "gesamt": "nicht_ok", "reparieren": aufgaben}
    log(f"Stufe 0b: verstaendlich ✅ (Schwierigkeit {wert}, Grenze {grenze})")
    # Stufe 1: Stehen die Zitate wirklich im Artikel?
    pruefung, alle_ok = [], True
    for b in daten.get("behauptungen", []):
        gefunden = beleg_gefunden(b.get("beleg", ""), artikel)
        pruefung.append({"behauptung": b.get("aussage", ""), "beleg_im_text": gefunden})
        alle_ok &= gefunden
    if not daten.get("behauptungen"):
        alle_ok = False
    if not alle_ok:
        fehlend = [f"Zitat steht nicht im Artikel (Aussage: '{b.get('aussage', '')}', angebliches Zitat: '{b.get('beleg', '')}')"
                   for b in daten.get("behauptungen", []) if not beleg_gefunden(b.get("beleg", ""), artikel)]
        log(f"Stufe 1: {len(fehlend)} Zitat(e) stehen NICHT in den Quellen.")
        return False, {"pruefung": pruefung, "gesamt": "nicht_ok", "probleme": fehlend or ["Keine Behauptungen angegeben"]}

    # Stufe 2: Eine (moeglichst andere) KI vergleicht den Sprechtext mit dem Artikel
    sprechtext = " ".join(s["text"] for s in daten["szenen"])
    if daten.get("hook_text"):
        sprechtext = f"[Texteinblendung: {daten['hook_text']}] " + sprechtext
    autor = daten["_modell"]
    prompt = f"""Du bist strenger Faktenpruefer. Vergleiche den SPRECHTEXT eines Videos mit den QUELLEN
(deutscher + ggf. englischer Wikipedia-Artikel + Wikidata-Eckdaten).
Jede FAKTENAUSSAGE im Sprechtext muss durch mindestens eine Quelle gedeckt sein.
DOPPEL-CHECK: Widersprechen sich die Quellen bei einer Zahl, einem Datum oder Namen und der Sprechtext nennt
eine davon als sicher -> "faktisch" mit dem Hinweis "Quellen widersprechen sich: weglassen oder vorsichtig formulieren".
schwere "faktisch" = erfundene oder falsche Fakten, falsche Zahlen/Daten/Namen, verdrehte Zusammenhaenge.
schwere "stil" = harmlose Wertungen, Meinungen, Uebergaenge, rhetorische Fragen, Hook-Saetze, Spannungsaufbau,
einfache Erklaerungen von Begriffen und veranschaulichende Alltagsvergleiche (solange die Zahlen stimmen),
sowie harmlose Vereinfachungen, die nichts Falsches behaupten (z. B. nur das Startjahr nennen, "Koenige" statt
"Monarchen", eine Zeitspanne grob zusammenfassen).
"faktisch" NUR, wenn eine Aussage den Quellen widerspricht oder etwas behauptet, das dort nicht steht.
Sauber uebersetzte Fakten aus dem englischen Artikel sind korrekt.
Melde nur echte Probleme, keine Kleinigkeiten.

QUELLEN:
\"\"\"{artikel}\"\"\"

SPRECHTEXT:
{sprechtext}

Antworte NUR mit JSON:
{{"probleme": [{{"satz": "...", "problem": "...", "schwere": "faktisch|stil"}}]}}"""
    urteil, pruefer = ki(prompt, temperatur=0.1, nicht=autor)
    probleme = [p for p in urteil.get("probleme", []) if isinstance(p, dict)]
    faktisch = [p for p in probleme if p.get("schwere") == "faktisch"]
    ok = not faktisch
    urteil["probleme"] = [f"{p.get('problem')} ({p.get('schwere')})" for p in probleme]
    for p in pruefung:
        p["urteil"] = "korrekt" if ok else "unsicher"
    log(f"Stufe 2 ({pruefer}): {'ok' if ok else urteil.get('probleme')}")
    return ok, {"pruefung": pruefung, "probleme": urteil.get("probleme", []),
                "reparieren": [f"Satz: '{p.get('satz')}' -> {p.get('problem')}" for p in faktisch],
                "gesamt": "ok" if ok else "nicht_ok", "pruefer": pruefer}


def reparieren(daten, ergebnis):
    """Laesst die KI nur die beanstandeten Stellen korrigieren, statt das Thema wegzuwerfen."""
    probleme = ergebnis.get("reparieren") or ergebnis.get("probleme") or []
    entwurf = {k: daten[k] for k in ("titel", "szenen", "behauptungen", "beschreibung", "hashtags") if k in daten}
    prompt = f"""Ein Faktenpruefer hat Fehler in diesem Video-Skript gefunden. Korrigiere NUR diese Stellen.
Fehlerhafte Saetze entweder streng nach dem Artikel umschreiben oder ganz streichen. Nichts Neues erfinden.
Jede Behauptung braucht als "beleg" ein EXAKTES, woertliches Zitat aus den Quellen (Copy-Paste in Originalsprache).
Erster und letzter Satz nur aendern, wenn sie selbst fehlerhaft sind (sie wurden extra fuer Catch und Loop optimiert). Gesamtlaenge 90 bis 110 Woerter, NIEMALS mehr als 120. Felder "bilder", "suchbegriff_en", "hook_text" und "kommentar_frage" beibehalten.

GEFUNDENE FEHLER:
{chr(10).join('- ' + str(p) for p in probleme)}

QUELLEN:
\"\"\"{daten['_artikel']}\"\"\"

AKTUELLES SKRIPT (JSON):
{json.dumps(entwurf, ensure_ascii=False)}

Antworte NUR mit dem korrigierten JSON im selben Format."""
    neu, modell = ki(prompt, temperatur=0.3, nicht=ergebnis.get("pruefer"))
    if not neu.get("szenen"):
        raise ValueError("Reparatur lieferte kein Skript")
    for k in ("titel", "szenen", "behauptungen", "beschreibung", "hashtags"):
        if neu.get(k):
            daten[k] = neu[k]
    daten["_modell"] = modell
    return felder_auffuellen(daten)


# ---------------------------------------------------------------- Voiceover
# 1. Wahl: Gemini-TTS (natuerliche, betonte Stimme, gratis) – 2. Wahl: Piper (offline, robotischer)
TTS_STIMME = os.environ.get("TTS_STIMME", "Charon")      # Gemini-Stimmen: Fenrir, Puck, Charon, Orus, Kore, Zephyr ...
TTS_MODELLE = [m for m in (os.environ.get("TTS_MODEL", ""), "gemini-3.1-flash-tts-preview",
                           "gemini-2.5-flash-preview-tts") if m]
TTS_STIL = os.environ.get("TTS_STIL", (
    "Lies den folgenden deutschen Text wie ein packender Erzaehler eines erfolgreichen YouTube-Shorts-Kanals vor: "
    "angenehmes, normales Erzaehltempo: deutlich und nicht gehetzt, aber auch nicht schleppend. Lebendig und neugierig, natuerliche Betonung, "
    "kurze Pausen nach jedem Satz und eine kleine Spannungspause vor der Pointe. "
    "Lies nur den Text vor, sonst nichts:"))
STIMME_GENUTZT = "?"


# Die besten Erzaehler-Stimmen (Gemini-TTS, gratis, sprechen sehr gut Deutsch). Name -> Beschreibung
STIMMEN = {
    "Charon":       "♂ ruhig, informativ – klassischer Doku-Erzähler",
    "Orus":         "♂ tief, bestimmt – sehr seriös",
    "Fenrir":       "♂ aufgeregt, energisch – viel Drive",
    "Sadaltager":   "♂ wissend, souverän – wie ein Experte",
    "Algenib":      "♂ rau, markant – Gänsehaut-Stimme",
    "Puck":         "♂ locker, gut gelaunt – jung und frisch",
    "Iapetus":      "♂ klar, deutlich – neutral",
    "Achird":       "♂ freundlich, nahbar",
    "Kore":         "♀ bestimmt, klar – stark für Fakten",
    "Sulafat":      "♀ warm, angenehm – zum Zuhören",
    "Aoede":        "♀ leicht, luftig – entspannt",
    "Leda":         "♀ jugendlich, frisch",
    "Zephyr":       "♀ hell, lebendig",
    "Gacrux":       "♀ reif, erzählend – Hörbuch-Stil",
    "Vindemiatrix": "♀ sanft, ruhig",
}
EMPFOHLEN = ["Charon", "Algenib", "Fenrir", "Kore"]      # fuer "/voice test" (Vergleich)
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
HOERPROBE = ("Neun Wanderer flohen mitten in der Nacht aus ihrem eigenen Zelt. Barfuß. Bei minus dreißig Grad. "
             "Und bis heute weiß niemand so genau, warum.")


def modus_hoerprobe():
    """/voice test: schickt kurze Hoerproben als Sprachnachricht (gleicher Beispielsatz fuer fairen Vergleich)."""
    global TTS_STIMME
    WORK.mkdir(parents=True, exist_ok=True)
    roh = [n.strip() for n in os.environ.get("STIMME_TEST", "").replace(";", ",").split(",") if n.strip()]
    namen = []
    for n in roh or EMPFOHLEN:
        if n.lower().startswith("eleven:"):
            namen.append(n)
            continue
        treffer = next((k for k in STIMMEN if k.lower() == n.lower()), None)
        if treffer:
            namen.append(treffer)
        else:
            telegram_text(f"🤔 Die Stimme „{n}“ kenne ich nicht – alle Stimmen: /voice")
    for i, name in enumerate(namen[:6]):
        wav, ogg = WORK / f"probe{i}.wav", WORK / f"probe{i}.ogg"
        try:
            if name.lower().startswith("eleven:"):
                elevenlabs_tts(HOERPROBE, wav, name.split(":", 1)[1])
            else:
                TTS_STIMME = name
                gemini_tts(HOERPROBE, wav)
            ffmpeg("-i", wav, "-c:a", "libopus", "-b:a", "48k", ogg)
            beschreibung = STIMMEN.get(name, "ElevenLabs")
            with open(ogg, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice", timeout=60,
                              data={"chat_id": TG_CHAT, "caption": f"🎙 {name} – {beschreibung}\nNehmen: /voice {name}"},
                              files={"voice": ("probe.ogg", f, "audio/ogg")})
        except Exception as e:
            telegram_text(f"⚠️ Hörprobe {name} ging nicht: {str(e)[:200]}")
        if i < len(namen) - 1:
            time.sleep(4)                         # Gemini-Limit schonen


def stimme_aus_status():
    try:
        return json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8")).get("stimme") or TTS_STIMME
    except Exception:
        return TTS_STIMME


def elevenlabs_tts(text, ziel, voice_id):
    """Optional (kostenpflichtig, aber noch natuerlicher): ElevenLabs. Braucht Secret ELEVENLABS_API_KEY."""
    if not ELEVEN_KEY:
        raise RuntimeError("ElevenLabs gewaehlt, aber kein ELEVENLABS_API_KEY")
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", timeout=180,
                      headers={"xi-api-key": ELEVEN_KEY, "Accept": "audio/mpeg"},
                      json={"text": text, "model_id": "eleven_multilingual_v2",
                            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.35}})
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs: {r.status_code} {r.text[:200]}")
    mp3 = Path(str(ziel) + ".mp3")
    mp3.write_bytes(r.content)
    ffmpeg("-i", mp3, "-ar", "24000", "-ac", "1", ziel)
    return f"ElevenLabs/{voice_id}"


def sprechen(text, ziel, stimme=None):
    """Erzeugt die Sprachaufnahme mit der gewaehlten Stimme (mit Ersatzstimmen, falls etwas ausfaellt)."""
    global TTS_STIMME
    stimme = stimme or stimme_aus_status()
    if stimme.startswith("eleven:"):
        try:
            return elevenlabs_tts(text, ziel, stimme.split(":", 1)[1])
        except Exception as e:
            log(f"⚠️ {e} -> nehme Gemini-Stimme")
            stimme = "Charon"
    TTS_STIMME = stimme
    try:
        return gemini_tts(text, ziel)
    except Exception as e:
        log(f"⚠️ {e} -> nehme Ersatzstimme Piper")
        return piper_tts(text, ziel)


def gemini_tts(text, ziel):
    if not GEMINI_KEY:
        raise RuntimeError("kein GEMINI_API_KEY")
    import base64
    body = {
        "contents": [{"parts": [{"text": f"{TTS_STIL}\n\n{text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_STIMME}}},
        },
    }
    fehler = ""
    for modell in TTS_MODELLE:
        for versuch in range(3):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modell}:generateContent",
                                  params={"key": GEMINI_KEY}, json=body, timeout=180)
            except requests.RequestException as e:
                fehler = f"{modell}: {e.__class__.__name__}"
                time.sleep(10)
                continue
            if r.status_code == 200:
                teile = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                audio = next((p["inlineData"] for p in teile if "inlineData" in p), None)
                if audio:
                    rate = int((re.search(r"rate=(\d+)", audio.get("mimeType", "")) or [0, 24000])[1])
                    with wave.open(str(ziel), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(rate)
                        wf.writeframes(base64.b64decode(audio["data"]))
                    return f"Gemini-TTS/{modell}/{TTS_STIMME}"
                fehler = f"{modell}: Antwort ohne Audio"
                break
            fehler = f"{modell}: {r.status_code} {r.text[:300]}"
            log(f"TTS-Problem -> {fehler}")
            warten = wartezeit(r.text)
            if r.status_code == 429 and warten and warten <= 65:
                time.sleep(warten + 1)
                continue
            if r.status_code in (500, 503):
                time.sleep(10)
                continue
            break
    raise RuntimeError(f"Gemini-TTS nicht verfuegbar ({fehler})")


def piper_tts(text, ziel):
    from piper import PiperVoice
    sprache, sprecher, qualitaet = VOICE_NAME.split("-")
    basis = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
             f"{sprache.split('_')[0]}/{sprache}/{sprecher}/{qualitaet}/{VOICE_NAME}")
    for endung in (".onnx", ".onnx.json"):
        datei = WORK / f"{VOICE_NAME}{endung}"
        if not datei.exists():
            r = requests.get(basis + endung, timeout=300)
            r.raise_for_status()
            datei.write_bytes(r.content)
    stimme = PiperVoice.load(str(WORK / f"{VOICE_NAME}.onnx"))
    with wave.open(str(ziel), "wb") as wf:
        if hasattr(stimme, "synthesize_wav"):
            try:
                from piper import SynthesisConfig
                stimme.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=LENGTH_SCALE))
            except ImportError:
                stimme.synthesize_wav(text, wf)
        else:
            stimme.synthesize(text, wf, length_scale=LENGTH_SCALE)
    return f"Piper/{VOICE_NAME}"


def wav_dauer(pfad):
    with wave.open(str(pfad), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


# ---------------------------------------------------------------- Wort-Zeitstempel (fuer synchrone Untertitel)
def _wnorm(w):
    return re.sub(r"[^\w]", "", w.lower())


def wort_zeiten(wav, woerter):
    """Gibt fuer jedes Skript-Wort (start, ende) in Sekunden zurueck.
    Nutzt faster-whisper zum Hoeren; faellt sonst auf eine Schaetzung nach Zeichenlaenge zurueck."""
    gesamt = wav_dauer(wav)
    zeiten = [None] * len(woerter)
    try:
        from faster_whisper import WhisperModel
        modell = WhisperModel(os.environ.get("WHISPER_MODELL", "base"), device="cpu", compute_type="int8")
        segmente, _ = modell.transcribe(str(wav), language="de", word_timestamps=True, vad_filter=False)
        gehoert = [(_wnorm(w.word), w.start, w.end) for s in segmente for w in (s.words or [])]
        sm = difflib.SequenceMatcher(None, [_wnorm(w) for w in woerter], [g[0] for g in gehoert], autojunk=False)
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                zeiten[a + k] = (gehoert[b + k][1], gehoert[b + k][2])
        log(f"Untertitel-Sync: {sum(z is not None for z in zeiten)}/{len(woerter)} Woerter exakt gehoert")
    except Exception as e:
        log(f"Whisper nicht verfuegbar ({e.__class__.__name__}), schaetze Zeiten.")

    # Luecken fuellen: zwischen bekannten Nachbarn nach Zeichenlaenge verteilen
    gewicht = [len(w) + 1 for w in woerter]
    i = 0
    while i < len(woerter):
        if zeiten[i] is not None:
            i += 1
            continue
        j = i
        while j < len(woerter) and zeiten[j] is None:
            j += 1
        t0 = zeiten[i - 1][1] if i > 0 else 0.0
        t1 = zeiten[j][0] if j < len(woerter) else gesamt
        summe = sum(gewicht[i:j]) or 1
        t = t0
        for k in range(i, j):
            d = (t1 - t0) * gewicht[k] / summe
            zeiten[k] = (t, t + d)
            t += d
        i = j
    return zeiten


# ---------------------------------------------------------------- Untertitel
def chunk_grenzen(woerter, max_woerter=3, max_zeichen=16):
    """Teilt eine Wortliste in Untertitel-Haeppchen, gibt Listen von Indizes zurueck."""
    out, akt = [], []
    for i, w in enumerate(woerter):
        if akt and (len(akt) >= max_woerter or len(" ".join(woerter[k] for k in akt + [i])) > max_zeichen):
            out.append(akt)
            akt = []
        akt.append(i)
        if re.search(r"[.!?,:;]$", w):
            out.append(akt)
            akt = []
    if akt:
        out.append(akt)
    return out


def ass_zeit(t):
    h, rest = divmod(max(t, 0), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


FONT_DATEI = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MAX_TEXTBREITE = W - 140          # Rand links/rechts inkl. Umrandung


def schriftgroesse(text, basis):
    """Verkleinert die Schrift, bis der Text sicher in eine Zeile passt (lange deutsche Woerter!)."""
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(FONT_DATEI, basis)
        breite = font.getlength(text) + 16          # + Umrandung
    except Exception:
        breite = len(text) * basis * 0.72           # Schaetzung ohne Pillow
    if breite <= MAX_TEXTBREITE:
        return basis
    return max(44, int(basis * MAX_TEXTBREITE / breite))


GELB = "&H0000E1FF&"
WASSERZEICHEN = os.environ.get("WASSERZEICHEN", "@faktenfieber.official")
WEISS = "&H00FFFFFF&"


def hook_umbruch(text, max_zeichen=16):
    """Hook-Text auf max. 2-3 Zeilen verteilen."""
    zeilen, akt = [], ""
    for w in text.split():
        if akt and len(akt) + 1 + len(w) > max_zeichen:
            zeilen.append(akt)
            akt = w
        else:
            akt = f"{akt} {w}".strip()
    if akt:
        zeilen.append(akt)
    return zeilen[:3]


HOOK_DAUER = float(os.environ.get("HOOK_SEKUNDEN", "5.5"))


def untertitel_schreiben(eintraege, ziel, hook_text="", hook_dauer=HOOK_DAUER):
    """eintraege = [(woerter=[(wort, start, ende)], start, ende)]
    Karaoke-Stil: das gerade gesprochene Wort leuchtet gelb. Oben zusaetzlich der Hook-Text."""
    kopf = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1
Style: Hook,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    zeilen = []
    # Hook-Text oben (weiss auf schwarzem Balken, krasser Fakt sofort sichtbar)
    if hook_text:
        hz = [ass_escape(z.upper()) for z in hook_umbruch(hook_text)]
        groesse = min(schriftgroesse(z, 84) for z in hz)
        txt = r"\N".join(hz)
        zeilen.append(f"Dialogue: 1,{ass_zeit(0)},{ass_zeit(hook_dauer)},Hook,,0,0,0,,"
                      f"{{\\an8\\pos(540,250)\\fs{groesse}\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)"
                      f"\\fad(0,250)}}{txt}")
    for woerter, s_chunk, e_chunk in eintraege:
        roh = [ass_escape(w.upper()) for w, _, _ in woerter]
        groesse = schriftgroesse(" ".join(roh), 92)
        for k, (w, ws, we) in enumerate(woerter):
            start = s_chunk if k == 0 else ws
            ende = woerter[k + 1][1] if k + 1 < len(woerter) else e_chunk
            if ende <= start:
                continue
            text = " ".join((f"{{\\c{GELB}}}{x}{{\\c{WEISS}}}" if j == k else x) for j, x in enumerate(roh))
            anim = r"\fscx82\fscy82\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            zeilen.append(f"Dialogue: 0,{ass_zeit(start)},{ass_zeit(ende)},Main,,0,0,0,,"
                          f"{{\\an5\\pos(540,1180)\\fs{groesse}{anim}}}{text}")
    if eintraege:                                 # dezentes Wasserzeichen (hilft, wenn Videos weitergeteilt werden)
        ende = ass_zeit(max(e[2] for e in eintraege) + 1)
        zeilen.append(f"Dialogue: 2,0:00:00.00,{ende},Main,,0,0,0,,"
                      f"{{\\an2\\pos(540,1835)\\fs34\\bord2\\shad0\\alpha&H60&}}{WASSERZEICHEN}")
    Path(ziel).write_text(kopf + "\n".join(zeilen) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Clips (Pixabay)
FUELLWOERTER = {"old", "the", "a", "an", "of", "in", "on", "with", "and", "closeup", "close", "up", "view", "shot",
                "background", "footage", "video", "people", "man", "woman", "scene", "aerial", "slow", "motion"}


def _relevant(begriff, tags):
    """Passt der Clip wirklich? Mindestens ein inhaltliches Wort des Suchbegriffs muss in den Tags vorkommen."""
    woerter = [w for w in re.findall(r"[a-z0-9]+", begriff.lower()) if w not in FUELLWOERTER and len(w) > 2]
    tags = tags.lower()
    treffer = sum(1 for w in woerter if w[:5] in tags)
    return bool(woerter) and treffer >= max(1, (len(woerter) + 1) // 2)


def clips_suchen(begriff, dauer, schon_benutzt, anzahl=1):
    """Sucht bis zu `anzahl` Pixabay-Videos, die WIRKLICH zum Begriff passen. Lieber keins als ein falsches."""
    if not begriff or str(begriff).lower() in ("null", "none"):
        return []
    woerter = begriff.split()
    versuche = [begriff, " ".join(woerter[:2]), " ".join(woerter[-2:])]
    gefunden = []
    for q in dict.fromkeys(versuche):
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": PIXABAY_KEY, "q": q, "per_page": 30, "safesearch": "true"}, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            log(f"Pixabay {r.status_code}: {r.text[:200]}")
            continue
        treffer = [h for h in r.json().get("hits", [])
                   if h["id"] not in schon_benutzt and h["id"] not in {g[0] for g in gefunden}
                   and _relevant(begriff, h.get("tags", ""))]
        treffer.sort(key=lambda h: (h.get("duration", 0) >= dauer,
                                    h["videos"]["large"].get("height", 0) >= h["videos"]["large"].get("width", 1),
                                    h.get("likes", 0)), reverse=True)
        for h in treffer:
            url = next((h["videos"][g]["url"] for g in ("large", "medium", "small")
                        if h["videos"].get(g, {}).get("url")), None)
            if url:
                gefunden.append((h["id"], url))
            if len(gefunden) >= anzahl:
                return gefunden
    return gefunden


def ffmpeg(*args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    subprocess.run(cmd, check=True)


def szene_rendern(clip, dauer, ziel, nummer=0):
    """Stock-Clip -> 9:16 mit leichtem Zoom (abwechselnd rein/raus), damit nie etwas stillsteht."""
    W2, H2 = int(W * 1.15) // 2 * 2, int(H * 1.15) // 2 * 2
    frames = max(1, int(dauer * FPS))
    z = f"1+0.08*on/{frames}" if nummer % 2 == 0 else f"1.08-0.08*on/{frames}"
    vf = (f"scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},fps={FPS},"
          f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
          f"setsar=1,eq=brightness=-0.06:saturation=1.1")
    ffmpeg("-stream_loop", "-1", "-i", clip, "-t", f"{dauer:.3f}", "-vf", vf, "-an", "-frames:v", frames,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", ziel)


def bild_rendern(bild, dauer, ziel, nummer):
    """Standbild -> bewegtes Segment: unscharfer Hintergrund + ganzes Bild im Vordergrund + langsamer Zoom."""
    komp = WORK / f"komp_{nummer:02d}.png"
    W2, H2 = W * 2, H * 2
    ffmpeg("-i", bild, "-filter_complex",
           f"[0:v]scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},boxblur=40:3,"
           f"eq=brightness=-0.18[bg];[0:v]scale={W2}:{H2}:force_original_aspect_ratio=decrease[fg];"
           f"[bg][fg]overlay=(W-w)/2:(H-h)/2", "-frames:v", "1", komp)
    frames = max(1, int(dauer * FPS))
    art = nummer % 4                 # abwechselnd: rein, raus, Schwenk links->rechts, Schwenk oben->unten
    if art == 0:
        z, x, y = f"min(1+0.14*on/{frames},1.14)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif art == 1:
        z, x, y = f"max(1.14-0.14*on/{frames},1)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif art == 2:
        z, x, y = "1.15", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.15", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames}"
    ffmpeg("-i", komp, "-vf",
           f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
           f"setsar=1,format=yuv420p", "-frames:v", frames,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", ziel)


# ---------------------------------------------------------------- Hintergrundmusik (die KI sucht aus)
MUSIK_CACHE = ROOT / "data" / "musik"            # erfolgreich geladene Tracks werden hier gesammelt
MUSIK_CACHE_MAX = 25
USED_MUSIK = ROOT / "data" / "used_music.json"
MUSIK_LAUTSTAERKE = float(os.environ.get("MUSIK_LAUTSTAERKE", "0.09"))   # bewusst leise
# Nur zwei Stimmungen, beide leise und ruhig:
#  "sanft"    = normale Themen: weiches Piano, sanftes Ambient
#  "mystisch" = Mord, Raetsel, Katastrophen, Gruseliges: etwas geheimnisvoller, aber NIE aggressiv
SANFT = ["soft piano", "calm piano", "gentle ambient", "peaceful piano", "relaxing piano ambient",
         "soft ambient", "calm background music", "gentle instrumental"]
MYSTISCH = ["ethereal ambient", "mysterious ambient calm", "atmospheric piano", "soft mysterious",
            "calm ambient pad", "dreamy ambient", "melancholic piano"]
STIMMUNG_SUCHE = {"sanft": SANFT, "mystisch": MYSTISCH}
ENTSPANNT = ["calm ambient", "soft piano ambient", "relaxing ambient", "peaceful ambient"]
DUNKLE_THEMEN = re.compile(r"(mord|moerder|mörder|kill|serienm|verbrech|krimin|\btot(e|en)?\b|\btod\b|leiche|gift|geist|spuk|"
                           r"fluch|raetsel|rätsel|mysteri|verschwund|verschollen|katastroph|unglueck|unglück|"
                           r"absturz|explosion|krieg|schlacht|\bpest\b|seuche|hinricht|folter|hexe|vampir|\bgrab\b|"
                           r"untergang|sank|ungeklaert|ungeklärt|horror|grusel|unheimlich)", re.I)


def musik_stimmung(daten):
    """Normale Themen -> sanft, dunkle Themen (Mord, Raetsel, Katastrophe ...) -> mystisch (aber ruhig)."""
    text = " ".join(str(daten.get(k, "")) for k in ("titel", "thema", "hook_text", "musik_stimmung"))
    text += " " + " ".join(s.get("text", "") for s in daten.get("szenen", [])[:4])
    if daten.get("musik_stimmung") in ("mysterioes", "dramatisch") or len(DUNKLE_THEMEN.findall(text)) >= 2:
        return "mystisch"
    return "sanft"


MUSIKNACHWEIS = ""


def _lade_json(pfad):
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except Exception:
        return [] if pfad.suffix == ".json" and "used" in pfad.name else {}


def _merke_musik(kennung):
    liste = _lade_json(USED_MUSIK) or []
    liste.append(kennung)
    USED_MUSIK.parent.mkdir(parents=True, exist_ok=True)
    USED_MUSIK.write_text(json.dumps(liste[-50:], ensure_ascii=False), encoding="utf-8")


def _openverse(q, nur_musik=True):
    params = {"q": q, "license_type": "commercial,modification", "page_size": 20}
    if nur_musik:
        params["category"] = "music"
    try:
        r = requests.get("https://api.openverse.org/v1/audio/", headers=WIKI_HEADERS, timeout=30, params=params)
        if r.status_code != 200:
            log(f"Openverse antwortet {r.status_code}: {r.text[:150]}")
            return []
        alle = r.json().get("results", [])
    except Exception as e:
        log(f"Openverse nicht erreichbar: {e.__class__.__name__}")
        return []
    return [{"id": t["id"], "url": t["url"], "title": t.get("title") or "Ohne Titel",
             "creator": t.get("creator") or "unbekannt", "license": t["license"],
             "license_version": t.get("license_version") or "", "landing": t.get("foreign_landing_url") or "",
             "tags": ", ".join(x.get("name", "") for x in (t.get("tags") or [])[:6]),
             "dauer": int((t.get("duration") or 0) / 1000)}
            for t in alle if t.get("license") in ("cc0", "by", "by-sa", "pdm") and t.get("url")
            and ((t.get("duration") or 60000) >= 40000)
            and not _verboten(t.get("title", ""), " ".join(x.get("name", "") for x in (t.get("tags") or [])))]


def _nachweis(t):
    lizenz = "gemeinfrei (CC0)" if t["license"] in ("cc0", "pdm") else f"CC {t['license'].upper()} {t['license_version']}".strip()
    return f"„{t['title']}“ von {t['creator']} – {lizenz} – {t['landing']}".strip(" –")


ARCHIV_THEMEN = {"sanft": ["piano", "relaxing", "calm", "meditation", "peaceful", "soft"],
                 "mystisch": ["ethereal", "atmospheric", "ambient", "meditation", "dreamy"]}
ARCHIV_ENTSPANNT = ["ambient", "relaxing", "meditation", "calm", "piano"]
ARCHIV_LIZENZ = ('(licenseurl:*publicdomain/zero* OR licenseurl:*publicdomain/mark* OR '
                 'licenseurl:*licenses/by/* OR licenseurl:*licenses/by-sa/*)')


# Alles, was nach Sprache/Gesang klingt, fliegt raus
VERBOTEN = ["speech", "spoken", "vocal", "vocals", "voice", "singer", "song", "lyrics", "rap", "hip hop", "hiphop",
            "lecture", "audiobook", "podcast", "radio", "interview", "poetry", "poem", "propaganda", "rede",
            "hörspiel", "hoerspiel", "sermon", "preaching", "news", "broadcast", "ww2", "wwii", "war speech",
            "hitler", "nazi", "politic", "choir", "chant", "a cappella", "acapella", "opera", "karaoke",
            # zu wild fuer leise Hintergrundmusik:
            "drum", "drums", "beat", "beats", "techno", "dubstep", "rock", "metal", "punk", "edm", "dance",
            "trap", "breakbeat", "hardcore", "house", "trance", "dnb", "jungle", "industrial", "noise",
            "glitch", "electro", "funk", "party", "club", "aggressive", "hard", "rave",
            # zu duester/unruhig:
            "drone", "dark ambient", "horror", "scary", "harsh", "experimental", "doom", "black", "ritual",
            "distort", "psychedelic", "epic", "action", "battle", "war", "tension", "suspense", "thriller",
            "power", "bass", "dub", "chiptune", "8-bit", "8bit", "field recording", "soundscape", "abstract"]
VERBOTEN_SUCHE = " OR ".join(f'"{v}"' for v in VERBOTEN)
INSTRUMENTAL_BONUS = ["instrumental", "ambient", "piano", "orchestral", "cinematic", "soundtrack", "lofi", "chill"]


def _verboten(*texte):
    t = " ".join(str(x) for x in texte).lower()
    # kurze Woerter nur als ganzes Wort ("war" soll nicht "warm" treffen), lange auch als Wortanfang
    return any(re.search(rf"\b{re.escape(v)}(s|es)?\b" if len(v) <= 5 else rf"\b{re.escape(v)}", t) for v in VERBOTEN)


def hat_stimme(pfad):
    """Hoert mit der Spracherkennung in den Track rein. Erkennt sie Woerter -> Gesang/Sprache -> raus."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return False                               # ohne Spracherkennung koennen wir es nicht pruefen
    probe = WORK / "musik_probe.wav"
    try:
        laenge = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                       "csv=p=0", str(pfad)], capture_output=True, text=True).stdout or 0)
        woerter = 0
        for start in (min(15, laenge * 0.1), laenge * 0.45, max(0, laenge * 0.75)):
            ffmpeg("-ss", f"{start:.1f}", "-t", "25", "-i", pfad, "-ar", "16000", "-ac", "1", probe)
            global _WHISPER
            if "_WHISPER" not in globals() or _WHISPER is None:
                _WHISPER = WhisperModel(os.environ.get("WHISPER_MODELL", "base"), device="cpu", compute_type="int8")
            segmente, _ = _WHISPER.transcribe(str(probe), vad_filter=True, beam_size=1)
            for seg in segmente:
                if seg.no_speech_prob < 0.5 and seg.avg_logprob > -1.0:
                    woerter += len(seg.text.split())
            if woerter >= 5:
                return True
        return False
    except Exception as e:
        log(f"   Stimmen-Check nicht moeglich ({e.__class__.__name__})")
        return False


_WHISPER = None


def klingt_ruhig(pfad):
    """Hoert objektiv rein: keine Schlagzeug-/Beat-Impulse, nicht grell, kein Rauschen, keine wilden Sprunge.
    Gibt (ok, grund) zurueck. Ohne numpy wird nicht geprueft."""
    try:
        import numpy as np
    except ImportError:
        return True, ""
    try:
        laenge = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                       "csv=p=0", str(pfad)], capture_output=True, text=True).stdout or 0)
        sr, fen = 22050, 1024
        werte = {"impulse": [], "hell": [], "rauschen": [], "spruenge": []}
        for start in (min(20, laenge * 0.15), laenge * 0.45, max(0, laenge * 0.7)):
            roh = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.1f}", "-t", "20", "-i", str(pfad),
                                  "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True).stdout
            x = np.frombuffer(roh, dtype=np.int16).astype(np.float32) / 32768
            if len(x) < sr * 5:
                continue
            n = len(x) // fen
            frames = x[:n * fen].reshape(n, fen)
            rms_db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)
            laut = rms_db > rms_db.max() - 40                       # Stille ignorieren
            if laut.sum() < 20:
                continue
            spek = np.abs(np.fft.rfft(frames * np.hanning(fen), axis=1)) ** 2
            freqs = np.fft.rfftfreq(fen, 1 / sr)
            # 1) Schlagzeug/Beats: viele ploetzliche Energie-Anstiege (spektraler Fluss) pro Sekunde
            # (nur Frequenzen ueber 2,5 kHz: dort sitzen Snare/Hi-Hat; weiche Klaviertoene loesen das nicht aus)
            fluss = np.maximum(np.diff(np.sqrt(spek[:, freqs > 2500]), axis=0), 0).sum(axis=1)
            schwelle = np.median(fluss) * 4 + 1e-9
            hf_anteil = spek[:, freqs > 2500].sum(axis=1) / (spek.sum(axis=1) + 1e-12)
            spitzen = ((fluss[1:-1] > schwelle) & (fluss[1:-1] > fluss[:-2]) & (fluss[1:-1] >= fluss[2:])
                       & (hf_anteil[2:-1] > 0.04))      # nur echte Schlaege mit spuerbarem Hoehen-Anteil
            werte["impulse"].append(spitzen.sum() / (n * fen / sr))
            # 2) Grell/aggressiv: viel Energie ueber 4 kHz
            werte["hell"].append(spek[laut][:, freqs > 4000].sum() / (spek[laut].sum() + 1e-9))
            # 3) Rauschen: spektrale Flachheit
            s_l = spek[laut][:, (freqs > 100) & (freqs < 8000)] + 1e-12
            werte["rauschen"].append(float(np.median(np.exp(np.log(s_l).mean(axis=1)) / s_l.mean(axis=1))))
            # 4) wilde Lautstaerke-Spruenge
            glatt = np.convolve(rms_db, np.ones(5) / 5, mode="valid")
            werte["spruenge"].append(float(np.percentile(np.abs(np.diff(glatt)), 98)))
        if not werte["impulse"]:
            return True, ""
        imp, hell = max(werte["impulse"]), max(werte["hell"])
        rau, spr = max(werte["rauschen"]), max(werte["spruenge"])
        if imp > 1.6:
            return False, f"zu viele Beats/Schlaege ({imp:.1f}/s)"
        if hell > 0.10:
            return False, f"zu grell ({hell * 100:.0f} % Hoehen)"
        if rau > 0.35:
            return False, f"zu rauschig ({rau:.2f})"
        if spr > 4.5:
            return False, f"zu unruhig ({spr:.1f} dB Spruenge)"
        return True, f"Beats {imp:.1f}/s, Hoehen {hell * 100:.0f} %, Rauschen {rau:.2f}"
    except Exception as e:
        log(f"   Ruhe-Check nicht moeglich ({e.__class__.__name__})")
        return True, ""


def _archive(themen):
    """Internet Archive (archive.org): riesige, zuverlaessige Sammlung freier Musik (Netlabels), ohne Key."""
    teil = " OR ".join(f'"{t}"' for t in themen)
    try:
        r = requests.get("https://archive.org/advancedsearch.php", headers=WIKI_HEADERS, timeout=40, params={
            "q": (f"mediatype:audio AND collection:netlabels AND subject:({teil}) AND NOT subject:({VERBOTEN_SUCHE}) "
                  f"AND NOT title:({VERBOTEN_SUCHE})"),
            "fl[]": ["identifier", "title", "creator", "licenseurl", "subject"],
            "rows": 100, "sort[]": "downloads desc", "output": "json"})
        if r.status_code != 200:
            log(f"Archive.org antwortet {r.status_code}")
            return []
        docs = r.json().get("response", {}).get("docs", [])
    except Exception as e:
        log(f"Archive.org nicht erreichbar: {e.__class__.__name__}")
        return []
    out = []
    for d in docs:
        lic = str(d.get("licenseurl", "")).lower()
        if "publicdomain" in lic:
            art = "cc0"
        elif re.search(r"licenses/by-sa/", lic):
            art = "by-sa"
        elif re.search(r"licenses/by/", lic):
            art = "by"
        else:
            continue                          # NC, ND oder ohne Lizenz -> nicht erlaubt
        version = (re.search(r"/(\d\.\d)", lic) or [None, ""])[1]
        subj = d.get("subject", "")
        creator = d.get("creator") or "unbekannt"
        if _verboten(d.get("title", ""), subj):
            continue
        out.append({"id": f"ia_{d['identifier']}", "archive_id": d["identifier"], "url": None,
                    "title": d.get("title") or d["identifier"],
                    "creator": creator if isinstance(creator, str) else ", ".join(creator[:2]),
                    "license": art, "license_version": version,
                    "landing": f"https://archive.org/details/{d['identifier']}",
                    "tags": ", ".join(subj[:6]) if isinstance(subj, list) else str(subj)[:80], "dauer": 0})
    log(f"Archive.org ({', '.join(themen[:2])}…): {len(docs)} Alben, {len(out)} mit passender Lizenz")
    return out


def _archive_datei(t):
    """Waehlt im Archive-Album einen passenden MP3-Track (1-6 Minuten)."""
    try:
        meta = requests.get(f"https://archive.org/metadata/{t['archive_id']}", headers=WIKI_HEADERS, timeout=40).json()
    except Exception:
        return None
    def sekunden(x):
        try:
            if ":" in str(x):
                teile = [float(p) for p in str(x).split(":")]
                return sum(v * 60 ** i for i, v in enumerate(reversed(teile)))
            return float(x)
        except ValueError:
            return 0
    mp3 = [f for f in meta.get("files", []) if str(f.get("name", "")).lower().endswith(".mp3")
           and 60 <= sekunden(f.get("length", 0)) <= 420]
    if not mp3:
        mp3 = [f for f in meta.get("files", []) if str(f.get("name", "")).lower().endswith(".mp3")
               and 100_000 < int(f.get("size", 0) or 0) < 15_000_000]
    if not mp3:
        return None
    f = random.choice(mp3[:12])
    if f.get("title"):
        t["title"] = f"{f['title']} ({t['title']})"
    return f"https://archive.org/download/{t['archive_id']}/{requests.utils.quote(f['name'])}"


def _laden(t):
    url = t.get("url") or (_archive_datei(t) if t.get("archive_id") else None)
    if not url:
        log(f"   ✗ {t['title'][:50]}: keine MP3-Datei gefunden")
        return None
    try:
        r = requests.get(url, headers=WIKI_HEADERS, timeout=90)
    except requests.RequestException as e:
        log(f"   ✗ {t['title'][:50]}: Download-Fehler {e.__class__.__name__}")
        return None
    daten = r.content
    if r.status_code != 200 or len(daten) < 150_000 or daten[:15].lstrip().startswith(b"<"):
        log(f"   ✗ {t['title'][:50]}: HTTP {r.status_code}, {len(daten)} Bytes – kein brauchbares Audio")
        return None
    MUSIK_CACHE.mkdir(parents=True, exist_ok=True)
    ziel = MUSIK_CACHE / f"{re.sub(r'[^A-Za-z0-9]', '', t['id'])[:30]}.mp3"
    ziel.write_bytes(daten)
    # Probe: kann ffmpeg das lesen?
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(ziel)],
                           capture_output=True, text=True)
    try:
        if float(probe.stdout.strip() or 0) < 20:
            raise ValueError
    except ValueError:
        log(f"   ✗ {t['title'][:50]}: Datei nicht abspielbar")
        ziel.unlink(missing_ok=True)
        return None
    if hat_stimme(ziel):
        log(f"   ✗ {t['title'][:50]}: enthaelt Stimme/Gesang -> aussortiert")
        ziel.unlink(missing_ok=True)
        _merke_musik(t["id"])                      # nie wieder vorschlagen
        return None
    ruhig, grund = klingt_ruhig(ziel)
    if not ruhig:
        log(f"   ✗ {t['title'][:50]}: {grund} -> aussortiert")
        ziel.unlink(missing_ok=True)
        _merke_musik(t["id"])
        return None
    log(f"   ✓ ruhig genug ({grund})")
    index = _lade_json(MUSIK_CACHE / "index.json") or {}
    index[ziel.name] = {"nachweis": _nachweis(t), "stimmung": t.get("_stimmung", ""), "tags": t.get("tags", ""),
                        "ohne_stimme": True, "ruhig_geprueft": True}
    for alt in list(index)[:-MUSIK_CACHE_MAX]:
        (MUSIK_CACHE / alt).unlink(missing_ok=True)
        index.pop(alt, None)
    (MUSIK_CACHE / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return ziel


def musik_waehlen(daten):
    """Die KI waehlt aus frei lizenzierten Tracks den passendsten. Sonst entspannte Musik,
    sonst ein Track aus dem selbst gesammelten Archiv, und als allerletztes ein selbst erzeugter Ambient-Teppich."""
    global MUSIKNACHWEIS
    stimmung = musik_stimmung(daten)
    daten["_musik_stimmung"] = stimmung
    log(f"🎵 Musik-Stimmung: {stimmung}")
    zuletzt = set((_lade_json(USED_MUSIK) or [])[-12:])
    runden = [(STIMMUNG_SUCHE[stimmung], ARCHIV_THEMEN[stimmung], stimmung),
              (ENTSPANNT, ARCHIV_ENTSPANNT, "sanft")]
    for runde, (openverse_q, archiv_q, label) in enumerate(runden):
        kandidaten = {}
        for t in _archive(archiv_q):
            kandidaten[t["id"]] = t
        for q in random.sample(openverse_q, min(2, len(openverse_q))):
            for t in _openverse(q):
                kandidaten[t["id"]] = t
        for t in kandidaten.values():
            t["_stimmung"] = label
        liste = [t for t in kandidaten.values() if t["id"] not in zuletzt] or list(kandidaten.values())
        log(f"Musik-Kandidaten ({label}): {len(liste)}")
        if not liste:
            continue
        random.shuffle(liste)
        liste = liste[:18]
        reihenfolge = liste
        try:
            auswahl = "\n".join(f"{i}: {t['title'][:70]} | {t['tags'][:60]}" for i, t in enumerate(liste))
            prompt = f"""Du bist Musik-Supervisor fuer einen YouTube-Short ueber: "{daten.get('titel')}" ({daten.get('thema')}).
Gewuenschte Stimmung: {"sanft, warm, entspannt (z. B. ruhiges Klavier, weiches Ambient)" if stimmung == "sanft" else
"leicht geheimnisvoll, aber sanft und ruhig (z. B. schwebendes Ambient, melancholisches Klavier) - NICHT gruselig, NICHT bedrohlich"}.
Die Musik laeuft SEHR LEISE unter einer Erzaehlstimme und darf NIE stoeren oder ablenken.
Keine Beats, kein Schlagzeug, nichts Hektisches, nichts Duesteres, keine Spannungs- oder Horror-Musik.
Im Zweifel IMMER die ruhigere Wahl.
Waehle die 4 passendsten Tracks/Alben. NUR reine Instrumentalmusik!
Absolut verboten: Gesang, Sprache, Reden, Rap, Hoerspiele, Nachrichten, historische Tonaufnahmen,
Podcasts, Lesungen, Weihnachtslieder, Noise und Field-Recordings.

{auswahl}

Antworte NUR mit JSON: {{"auswahl": [Nummer1, Nummer2, Nummer3, Nummer4], "warum": "1 Satz"}}"""
            wahl, _ = ki(prompt, temperatur=0.3)
            nummern = [int(n) for n in wahl.get("auswahl", []) if str(n).isdigit() and int(n) < len(liste)]
            reihenfolge = [liste[n] for n in nummern] + [t for i, t in enumerate(liste) if i not in nummern]
            log(f"KI-Musikwahl: {wahl.get('warum', '')}")
        except Exception as e:
            log(f"KI-Musikwahl nicht moeglich ({e.__class__.__name__}), nehme Zufall")
        for t in reihenfolge[:8]:
            pfad = _laden(t)
            if pfad:
                _merke_musik(t["id"])
                MUSIKNACHWEIS = _nachweis(t)
                log(f"🎵 Musik: {MUSIKNACHWEIS}")
                return pfad

    index = _lade_json(MUSIK_CACHE / "index.json") or {}
    for n, i in list(index.items()):
        if not i.get("ohne_stimme") or not i.get("ruhig_geprueft"):   # alte, ungepruefte Tracks raus
            (MUSIK_CACHE / n).unlink(missing_ok=True)
            index.pop(n)
    if MUSIK_CACHE.exists():
        (MUSIK_CACHE / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    archiv = [(n, i) for n, i in index.items() if (MUSIK_CACHE / n).exists() and i.get("ruhig_geprueft")]
    ruhig = ([a for a in archiv if a[1].get("stimmung") == stimmung]
             or [a for a in archiv if a[1].get("stimmung") in ("sanft", "mystisch")])
    if ruhig:
        name, info = random.choice(ruhig)
        MUSIKNACHWEIS = info.get("nachweis", "")
        log(f"🎵 Musik aus dem eigenen Archiv: {name}")
        return MUSIK_CACHE / name

    log("🎵 Keine Musik online gefunden -> erzeuge ruhigen Ambient-Teppich")
    MUSIKNACHWEIS = ""
    return ambient_erzeugen(WORK / "ambient.wav")


def ambient_erzeugen(ziel, dauer=90):
    """Notfall-Musik, selbst erzeugt: weicher Akkord-Teppich mit langsamem Schweben (lizenzfrei)."""
    akkorde = random.choice([(220.0, 261.63, 329.63, 392.0), (196.0, 246.94, 293.66, 369.99),
                             (174.61, 220.0, 261.63, 329.63), (164.81, 207.65, 246.94, 329.63)])
    toene = "+".join(f"0.18*sin(2*PI*{f}*t)*(0.6+0.4*sin(2*PI*{0.05 + i * 0.03:.2f}*t))"
                     for i, f in enumerate(akkorde))
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{toene}':s=44100:d={dauer}",
           "-af", "lowpass=f=1800,aecho=0.8:0.7:120|260:0.35|0.25,volume=1.4", "-ac", "2", ziel)
    return ziel


def musik_lautstaerke_aktuell():
    try:
        return float(json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8")).get("musik_lautstaerke",
                                                                                                MUSIK_LAUTSTAERKE))
    except Exception:
        return MUSIK_LAUTSTAERKE


def audio_mischen(stimme, musik, gesamt, ziel):
    """Stimme + leise Musik, die unter der Stimme automatisch noch leiser wird (Ducking)."""
    if musik_lautstaerke_aktuell() <= 0:
        musik = None
    if not musik:
        ffmpeg("-i", stimme, "-af", f"apad=whole_dur={gesamt:.2f},loudnorm=I=-14:TP=-1.5:LRA=11",
               "-t", f"{gesamt:.2f}", "-ar", "44100", "-ac", "2", ziel)
        return
    start = 0.0
    try:
        laenge = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "csv=p=0", str(musik)], capture_output=True, text=True).stdout or 0)
        if laenge > gesamt + 30:
            start = random.uniform(0, min(40, laenge - gesamt - 5))   # nicht immer beim selben Intro starten
    except ValueError:
        pass
    filt = (f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad=whole_dur={gesamt:.2f},asplit=2[stimme][sc];"
            f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{gesamt:.2f},asetpts=PTS-STARTPTS,"
            f"acompressor=threshold=0.08:ratio=4:attack=80:release=800,highpass=f=70,lowpass=f=4200,"   # weich + unaufdringlich
            f"volume={musik_lautstaerke_aktuell()},afade=t=in:d=0.2,afade=t=out:st={max(0, gesamt - 0.4):.2f}:d=0.4[m];"
            f"[m][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=500[musik];"
            f"[stimme][musik]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
            f"alimiter=limit=0.95[out]")
    ffmpeg("-i", stimme, "-ss", f"{start:.1f}", "-stream_loop", "-1", "-i", musik,
           "-filter_complex", filt, "-map", "[out]", "-t", f"{gesamt:.2f}", "-ar", "44100", ziel)


# ---------------------------------------------------------------- KI-Illustrationen (optional)
KI_BILDER_MAX = 4
_KI_BILD_MODELL = None
_KI_BILD_AUS = False


def ki_bilder_erlaubt():
    try:
        return json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8")).get("ki_bilder", True)
    except Exception:
        return True


def ki_illustration(szene, thema, ziel):
    """Erzeugt ein Bild mit Gemini (nur mit Gemini-Abrechnung moeglich) und kennzeichnet es sichtbar als KI-Illustration.
    Klappt es nicht, wird das fuer den Rest des Laufs abgeschaltet – das Video entsteht dann ganz normal ohne."""
    global _KI_BILD_MODELL, _KI_BILD_AUS
    if _KI_BILD_AUS or not GEMINI_KEY or not ki_bilder_erlaubt():
        return None
    import base64
    if _KI_BILD_MODELL is None:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                             params={"key": GEMINI_KEY, "pageSize": 1000}, timeout=30).json()
            namen = [m["name"].split("/")[-1] for m in r.get("models", []) if "image" in m["name"]
                     and "generateContent" in m.get("supportedGenerationMethods", [])]
            _KI_BILD_MODELL = sorted(namen, reverse=True)[0] if namen else ""
        except Exception:
            _KI_BILD_MODELL = ""
    if not _KI_BILD_MODELL:
        _KI_BILD_AUS = True
        return None
    auftrag = (f"Cinematic, realistic documentary-style illustration in vertical 9:16 format showing: {szene} "
               f"(topic: {thema}). Dramatic lighting, high detail. No text, no letters, no logos, no watermarks, "
               f"no real famous people's faces.")
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{_KI_BILD_MODELL}:generateContent",
                          params={"key": GEMINI_KEY}, timeout=120, json={
                              "contents": [{"parts": [{"text": auftrag}]}],
                              "generationConfig": {"responseModalities": ["IMAGE"]}})
        teile = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        daten = next((p["inlineData"]["data"] for p in teile if "inlineData" in p), None)
        if r.status_code != 200 or not daten:
            raise RuntimeError(f"{r.status_code} {r.text[:150]}")
    except Exception as e:
        log(f"KI-Illustrationen nicht verfuegbar ({e}) – fuer diesen Lauf aus.")
        _KI_BILD_AUS = True
        return None
    Path(ziel).write_bytes(base64.b64decode(daten))
    try:                                           # sichtbare Kennzeichnung
        from PIL import Image, ImageDraw, ImageFont
        bild = Image.open(ziel).convert("RGB")
        z = ImageDraw.Draw(bild)
        font = ImageFont.truetype(FONT_DATEI, max(18, bild.width // 40))
        z.text((bild.width * 0.03, bild.height * 0.02), "KI-Illustration", font=font, fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        bild.save(ziel, quality=92)
    except Exception:
        pass
    log(f"KI-Illustration erstellt ({_KI_BILD_MODELL})")
    return ziel


# ---------------------------------------------------------------- Titelbild (Cover)
def cover_erstellen(video, titel, ziel, zeitpunkt=0.6):
    """Titelbild im Faktenfieber-Look: Standbild aus dem Video, abgedunkelt, grosser Titel, Marke oben."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    roh = WORK / "cover_roh.jpg"
    ffmpeg("-ss", f"{zeitpunkt:.2f}", "-i", video, "-frames:v", "1", "-q:v", "2", roh)
    bild = ImageEnhance.Brightness(Image.open(roh).convert("RGB")).enhance(0.55)
    zeichnen = ImageDraw.Draw(bild)
    breite = bild.width - 140
    woerter, zeilen, groesse = titel.upper().split(), [], 120
    while groesse >= 60:                           # so gross wie moeglich, max. 4 Zeilen
        font = ImageFont.truetype(FONT_DATEI, groesse)
        zeilen, akt = [], ""
        for w in woerter:
            test = f"{akt} {w}".strip()
            if font.getlength(test) <= breite:
                akt = test
            else:
                zeilen.append(akt)
                akt = w
        zeilen.append(akt)
        if len(zeilen) <= 4 and all(font.getlength(z) <= breite for z in zeilen):
            break
        groesse -= 8
    hoehe = len(zeilen) * int(groesse * 1.15)
    y = (bild.height - hoehe) // 2
    for i, z in enumerate(zeilen):
        farbe = (255, 225, 0) if i == 0 else (255, 255, 255)      # erste Zeile gelb wie der Hook
        x = (bild.width - font.getlength(z)) / 2
        zeichnen.text((x, y), z, font=font, fill=farbe, stroke_width=8, stroke_fill=(0, 0, 0))
        y += int(groesse * 1.15)
    marke = ImageFont.truetype(FONT_DATEI, 44)
    text = "FAKTENFIEBER"
    zeichnen.rounded_rectangle([(bild.width - marke.getlength(text)) / 2 - 24, 150,
                                (bild.width + marke.getlength(text)) / 2 + 24, 222], radius=18, fill=(0, 0, 0))
    zeichnen.text(((bild.width - marke.getlength(text)) / 2, 158), text, font=marke, fill=(255, 225, 0))
    bild.save(ziel, quality=90)
    return ziel


# ---------------------------------------------------------------- Video bauen
def video_bauen(daten):
    global STIMME_GENUTZT
    szenen = daten["szenen"]
    woerter, szene_von = [], []
    for i, s in enumerate(szenen):
        for w in s["text"].split():
            woerter.append(w)
            szene_von.append(i)
    volltext = " ".join(s["text"] for s in szenen)

    # 1) Voiceover (ein Stueck, damit die Betonung natuerlich durchlaeuft)
    roh = WORK / "voice_roh.wav"
    STIMME_GENUTZT = sprechen(volltext, roh)
    daten["_stimme"] = STIMME_GENUTZT.split("/")[-1]
    gesamt = wav_dauer(roh)
    log(f"Voiceover ({STIMME_GENUTZT}): {gesamt:.1f} s")

    tempo = max(1.0, gesamt / MAX_DAUER)
    if tempo > 1.0:
        log(f"Etwas zu lang, beschleunige um Faktor {tempo:.2f}")
        ffmpeg("-i", roh, "-filter:a", f"atempo={tempo:.4f}", WORK / "voice.wav")
    else:
        (WORK / "voice.wav").write_bytes(roh.read_bytes())

    # 2) Wort-Zeiten -> Untertitel + Szenengrenzen
    # Achtung: voice.wav ist schon die (evtl. beschleunigte) End-Fassung -> Zeiten 1:1 uebernehmen
    versatz = float(os.environ.get("UNTERTITEL_VERSATZ", "0.05"))
    zeiten = [(a + versatz, b + versatz) for a, b in wort_zeiten(WORK / "voice.wav", woerter)]
    ende_gesamt = wav_dauer(WORK / "voice.wav") + 0.15     # fast keine Pause am Ende -> sauberer Loop
    eintraege = []
    for i in range(len(szenen)):
        idx = [k for k, sz in enumerate(szene_von) if sz == i]
        for gruppe in chunk_grenzen([woerter[k] for k in idx]):
            ks = [idx[g] for g in gruppe]
            eintraege.append([[(woerter[k], zeiten[k][0], zeiten[k][1]) for k in ks],
                              zeiten[ks[0]][0], zeiten[ks[-1]][1]])
    for n in range(len(eintraege) - 1):          # Untertitel lueckenlos bis zum naechsten
        eintraege[n][2] = max(eintraege[n][2], eintraege[n + 1][1])
    eintraege[-1][2] = ende_gesamt
    untertitel_schreiben(eintraege, WORK / "subs.ass", daten.get("hook_text", ""))

    starts = [0.0] + [zeiten[szene_von.index(i)][0] for i in range(1, len(szenen))]
    grenzen = starts + [ende_gesamt]
    dauern = [max(0.8, grenzen[i + 1] - grenzen[i]) for i in range(len(szenen))]

    # 3) Visuals: NUR Themen-Material. Echte Bilder zuerst, Stock-Clips nur wenn sie wirklich passen.
    bilder = daten.get("_bilder", [])
    nutzung = {i: 0 for i in range(len(bilder))}          # wie oft jedes echte Bild schon dran war
    benutzt, segmente, bild_dateien = set(), [], {}

    def bild_nummern(sz):
        roh = sz.get("bilder", sz.get("bild"))
        roh = roh if isinstance(roh, list) else [roh]
        return [int(n) for n in roh if str(n).strip().isdigit() and int(n) < len(bilder)]

    def naechstes_bild(bevorzugt):
        # erst die zur Szene passenden, dann das am wenigsten genutzte Themenbild (max. 3x pro Bild)
        for n in bevorzugt:
            if nutzung[n] < 3:
                return n
        frei = sorted((n for n in nutzung if nutzung[n] < 3), key=lambda n: (nutzung[n], n))
        return frei[0] if frei else None

    def notfall_bild():
        return min(nutzung, key=lambda n: nutzung[n]) if nutzung else None

    for i, (sz, seg_dauer) in enumerate(zip(szenen, dauern)):
        teile = max(1, round(seg_dauer / MAX_SEGMENT))
        teil_dauer = seg_dauer / teile
        bevorzugt = bild_nummern(sz)
        clips = clips_suchen(sz.get("suchbegriff_en"), teil_dauer, benutzt, anzahl=teile)
        visuals = []
        for j in range(teile):
            # Reihenfolge pro Szene: passendes Bild -> passender Clip -> naechstes Themenbild -> Clip
            if j == 0 and bevorzugt and nutzung[bevorzugt[0]] < 3:
                visuals.append(("bild", naechstes_bild(bevorzugt)))
            elif clips:
                cid, url = clips.pop(0)
                benutzt.add(cid)
                visuals.append(("clip", url))
            else:
                n = naechstes_bild(bevorzugt[1:] if j else bevorzugt)
                if n is not None:
                    visuals.append(("bild", n))
                elif visuals:
                    visuals.append(visuals[-1])
            if visuals and visuals[-1][0] == "bild":
                nutzung[visuals[-1][1]] += 1
        if not visuals:
            # wirklich nichts Passendes: allgemeiner Suchbegriff zum Thema
            for cid, url in clips_suchen(daten.get("suchbegriff_thema") or daten["thema"], teil_dauer, benutzt, teile):
                benutzt.add(cid)
                visuals.append(("clip", url))
        if not visuals or (not bevorzugt and len([v for v in visuals if v[0] == "clip"]) == 0 and not bilder):
            ki_anzahl = sum(1 for b in bilder if b.get("ki"))
            if ki_anzahl < KI_BILDER_MAX:
                pfad = ki_illustration(sz["text"], daten["thema"], WORK / f"ki_{i:02d}.png")
                if pfad:
                    bilder.append({"datei": "KI-Illustration", "url": str(pfad), "lokal": True, "ki": True,
                                   "urheber": "KI (Gemini)", "lizenz": "KI-generiert"})
                    nutzung[len(bilder) - 1] = 1
                    visuals = [("bild", len(bilder) - 1)] + visuals
        if not visuals and notfall_bild() is not None:
            visuals.append(("bild", notfall_bild()))
            nutzung[visuals[-1][1]] += 1
        if not visuals:
            raise RuntimeError(f"Kein passendes Bildmaterial fuer Szene {i+1}")
        while len(visuals) < teile:
            visuals.append(visuals[-1])
        for j, (art, quelle) in enumerate(visuals[:teile]):
            seg = WORK / f"seg_{i:02d}_{j}.mp4"
            if art == "bild":
                if quelle not in bild_dateien:
                    datei = WORK / f"bild_{quelle:02d}.jpg"
                    if bilder[quelle].get("lokal"):
                        datei = Path(bilder[quelle]["url"])
                    else:
                        datei.write_bytes(requests.get(bilder[quelle]["url"], headers=WIKI_HEADERS, timeout=120).content)
                    bild_dateien[quelle] = datei
                bild_rendern(bild_dateien[quelle], teil_dauer, seg, len(segmente))
            else:
                clip = WORK / f"clip_{i:02d}_{j}.mp4"
                clip.write_bytes(requests.get(quelle, timeout=300).content)
                szene_rendern(clip, teil_dauer, seg, len(segmente))
            segmente.append(seg)
        arten = [a for a, _ in visuals[:teile]]
        log(f"Szene {i+1}/{len(szenen)}: {arten.count('bild')} echte(s) Bild(er), {arten.count('clip')} Clip(s)")
    genutzte_bilder = [bilder[n] for n in bild_dateien]
    daten["bildnachweise"] = [f"{b['datei'].split(':', 1)[-1]} – {b['urheber']} – {b['lizenz']} (Wikimedia Commons)"
                              for b in genutzte_bilder if not b.get("ki")]
    daten["_ki_bilder"] = sum(1 for b in genutzte_bilder if b.get("ki"))
    if daten["_ki_bilder"]:
        daten["bildnachweise"].append(f"{daten['_ki_bilder']} Bild(er) sind KI-Illustrationen (gekennzeichnet)")
    log(f"{len(segmente)} Schnitte, {len(genutzte_bilder)} verschiedene echte Bilder zum Thema")

    liste = WORK / "liste.txt"
    liste.write_text("".join(f"file '{p.resolve()}'\n" for p in segmente))
    ffmpeg("-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", WORK / "bild.mp4")

    # 4) Ton: Stimme + Hintergrundmusik
    try:
        musik = musik_waehlen(daten)
        audio_mischen(WORK / "voice.wav", musik, ende_gesamt, WORK / "ton.wav")
        ton = WORK / "ton.wav"
        daten["musiknachweis"] = MUSIKNACHWEIS
    except Exception as e:
        log(f"⚠️ Musik/Sounds fehlgeschlagen ({e}), nehme nur die Stimme.")
        ton = WORK / "voice.wav"

    # 5) Alles zusammen + Untertitel einbrennen (hochkant 1080x1920, 9:16)
    final = OUT / "faktenfieber.mp4"
    subs = str((WORK / "subs.ass").resolve()).replace(":", r"\:")
    # Fortschrittsbalken unten (zeigt, wie viel noch kommt -> Leute schauen eher bis zum Ende)
    gesamt_d = max(ende_gesamt, 1)
    filt = (f"[0:v]ass='{subs}'[v];"
            f"[v][2:v]overlay=x='-w+w*t/{gesamt_d:.2f}':y=H-12:shortest=1,setsar=1,setdar=9/16[out]")
    ffmpeg("-i", WORK / "bild.mp4", "-i", ton, "-f", "lavfi", "-i", f"color=c=0xFFE100:s={W}x8:r={FPS}",
           "-filter_complex", filt, "-map", "[out]", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{ende_gesamt:.2f}",
           "-movflags", "+faststart", final)
    try:
        cover_erstellen(WORK / "bild.mp4", daten.get("titel", ""), OUT / "cover.jpg")   # Bild ohne Untertitel
    except Exception as e:
        log(f"Titelbild nicht moeglich: {e}")
    return final, ende_gesamt


# ---------------------------------------------------------------- Telegram
def telegram_senden(video, daten, check, dauer, knoepfe=None, kopfzeile=""):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram nicht konfiguriert, ueberspringe.")
        return None
    api = f"https://api.telegram.org/bot{TG_TOKEN}"
    caption = f"🎬 {daten['titel']}\n⏱ {dauer:.0f} s | {daten['thema']}"
    with open(video, "rb") as f:
        r = requests.post(f"{api}/sendVideo",
                          data={"chat_id": TG_CHAT, "caption": caption[:1000], "supports_streaming": "true",
                                "width": W, "height": H, "duration": int(dauer)},
                          files={"video": ("faktenfieber.mp4", f, "video/mp4")}, timeout=300)
    if r.status_code != 200:
        log(f"Telegram-Video-Fehler: {r.text[:300]}")

    pruef = "\n".join(f"{'✅' if p.get('beleg_im_text') else '⚠️'} {p.get('behauptung')}"
                      for p in check.get("pruefung", []))
    text = (kopfzeile + f"📌 TITEL\n{daten['titel']}\n\n"
            f"🪝 EINSTIEG ({daten.get('hook_stil', '?')}, Catch {daten.get('_hook_punkte', '?')}/10)\n„{daten['szenen'][0]['text']}“\n"
            f"Einblendung: {daten.get('hook_text', '–')}\n\n"
            f"🔚 ENDE ({'Loop ♾️' if daten.get('_loop') else 'Pointe'})\n„{daten['szenen'][-1]['text']}“\n"
            + (f"↪️ Loop gelesen: „{daten.get('_loop_test')}“\n" if daten.get('_loop') and daten.get('_loop_test') else "")
            + "\n"
            f"📝 BESCHREIBUNG\n{daten['beschreibung']}\n\n"
            f"{quellen_zeile(daten)}\n"
            + f"\n{' '.join(daten.get('hashtags', []))}\n\n"
            f"🔎 FAKTENCHECK (Prüfer: {check.get('pruefer', '?')})\n{pruef}\n\n"
            f"🧠 Schwierigkeit: {daten.get('_schwierigkeit', '?')} (deine Grenze: {daten.get('_grenze', '?')} · ändern mit /schwierigkeit)\n"
            f"🤖 Geschrieben von: {daten.get('_modell')}\n🎙 Stimme: {STIMME_GENUTZT}")
    daten_tg = {"chat_id": TG_CHAT, "text": text[:4000], "disable_web_page_preview": "true"}
    if knoepfe:
        daten_tg["reply_markup"] = json.dumps({"inline_keyboard": knoepfe})
    r = requests.post(f"{api}/sendMessage", data=daten_tg, timeout=60)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


# ---------------------------------------------------------------- YouTube-Upload
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")


def youtube_beschreibung(daten):
    teile = [daten.get("beschreibung", "")]
    if daten.get("kommentar_frage"):
        teile += ["", f"💬 {daten['kommentar_frage']} Schreib's in die Kommentare!"]
    teile += ["", quellen_zeile(daten)]
    teile += ["", " ".join(daten.get("hashtags", [])) + ("" if daten.get("_lang") else " #shorts")]
    return "\n".join(teile).replace("<", "").replace(">", "")[:4900]


def youtube_hochladen(video, daten, sichtbarkeit=None):
    sichtbarkeit = sichtbarkeit or os.environ.get("YT_SICHTBARKEIT", "private")
    if not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
        raise RuntimeError("YouTube-Secrets fehlen (YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)")
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise RuntimeError(f"YouTube-Login fehlgeschlagen: {r.text[:300]}")
    token = r.json()["access_token"]

    tags = [h.lstrip("#") for h in daten.get("hashtags", []) if h.strip("#")][:15]
    meta = {
        "snippet": {
            "title": daten["titel"].replace("<", "").replace(">", "")[:100],
            "description": youtube_beschreibung(daten),
            "tags": tags,
            "categoryId": "27",                  # Bildung
            "defaultLanguage": "de",
            "defaultAudioLanguage": "de",
        },
        "status": {"privacyStatus": sichtbarkeit, "selfDeclaredMadeForKids": False},
    }
    groesse = Path(video).stat().st_size
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                      params={"uploadType": "resumable", "part": "snippet,status"},
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(groesse)},
                      data=json.dumps(meta), timeout=60)
    if r.status_code != 200 or "Location" not in r.headers:
        raise RuntimeError(f"YouTube-Upload-Start fehlgeschlagen: {r.status_code} {r.text[:400]}")
    with open(video, "rb") as f:
        r2 = requests.put(r.headers["Location"], data=f, timeout=600,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"YouTube-Upload fehlgeschlagen: {r2.status_code} {r2.text[:400]}")
    vid = r2.json()["id"]
    log(f"YouTube-Upload ok: https://youtube.com/shorts/{vid} ({sichtbarkeit})")
    cover = Path(video).with_name("cover.jpg")
    if cover.exists():
        try:
            t = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", params={"videoId": vid},
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                              data=cover.read_bytes(), timeout=60)
            log("YouTube-Titelbild gesetzt" if t.status_code == 200 else f"YouTube-Titelbild nicht moeglich: {t.text[:120]}")
        except requests.RequestException:
            pass
    return vid


# ---------------------------------------------------------------- Instagram-Upload (Reels)
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER = os.environ.get("IG_USER_ID", "")
IG_API = "https://graph.instagram.com"


def instagram_caption(daten):
    hashtags = [h if h.startswith("#") else f"#{h}" for h in daten.get("hashtags", [])][:25]
    teile = [daten["titel"], "", daten.get("beschreibung", "")]
    if daten.get("kommentar_frage"):
        teile += ["", f"💬 {daten['kommentar_frage']}"]
    teile += ["", quellen_zeile(daten)]
    teile += ["", " ".join(hashtags)]
    return "\n".join(teile)[:2150]


GH_PAT = os.environ.get("GH_PAT", "")
GH_OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "faktenfieber")
NACHWEIS_DATEI = ROOT / "data" / "nachweise.json"


def quellen_zeile(daten):
    """Eine kurze Zeile statt langer Listen. Die vollstaendigen Bild-/Musiknachweise (Pflicht bei CC-Lizenzen)
    stehen gesammelt auf faktenfieber.github.io/quellen."""
    zeile = "📚 Quelle: Wikipedia"
    if daten.get("bildnachweise") or daten.get("musiknachweis"):
        zeile += f" · Bild- & Musiknachweise: {GH_OWNER}.github.io/quellen"
    return zeile


def nachweise_merken(daten):
    """Merkt sich die Nachweise jedes geposteten Videos fuer die Quellen-Seite."""
    if daten.get("_lang"):
        return
    liste = _lade_json(NACHWEIS_DATEI) or []
    liste = [e for e in liste if e.get("titel") != daten.get("titel")]
    liste.append({"datum": jetzt().strftime("%d.%m.%Y"), "titel": daten.get("titel", ""),
                  "artikel": (daten.get("quellen") or [""])[0].rsplit(": ", 1)[-1],
                  "weitere": [q.rsplit(": ", 1)[-1] for q in (daten.get("quellen") or [])[1:]],
                  "bilder": daten.get("bildnachweise", []),
                  "musik": daten.get("musiknachweis", "")})
    NACHWEIS_DATEI.parent.mkdir(parents=True, exist_ok=True)
    NACHWEIS_DATEI.write_text(json.dumps(liste[-300:], ensure_ascii=False, indent=1), encoding="utf-8")


def quellen_html():
    import html as _h
    eintraege = ""
    for e in reversed(_lade_json(NACHWEIS_DATEI) or []):
        bilder = "".join(f"<li>{_h.escape(b)}</li>" for b in e.get("bilder", []))
        eintraege += (f"<section><h2>{_h.escape(e['titel'])}</h2><p class=m>{_h.escape(e['datum'])} · "
                      f"Text: <a href=\"{_h.escape(e['artikel'])}\">Wikipedia</a>"
                      + "".join(f" · <a href=\"{_h.escape(w)}\">Wikipedia (en)</a>" for w in e.get("weitere", []))
                      + " (CC BY-SA 4.0) · Eckdaten: Wikidata (CC0)</p>"
                      + (f"<ul>{bilder}</ul>" if bilder else "")
                      + (f"<p>🎵 {_h.escape(e['musik'])}</p>" if e.get("musik") else "") + "</section>")
    return f"""<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Faktenfieber – Quellen</title><style>
body{{margin:0;background:#0f0f12;color:#eee;font:15px/1.5 system-ui,sans-serif;padding:16px}}main{{max-width:800px;margin:auto}}
h1{{color:#ffd400}}h2{{font-size:17px;margin:0 0 4px}}section{{background:#1a1a20;border-radius:12px;padding:14px;margin:10px 0}}
.m{{color:#9a9aa5;font-size:13px;margin:0 0 6px}}a{{color:#ffd400}}ul{{margin:4px 0;padding-left:18px;font-size:14px}}
</style></head><body><main><h1>🔥 Faktenfieber – Quellen & Bildnachweise</h1>
<p class=m>Alle Fakten stammen aus Wikipedia (deutsch + englisch) und Wikidata. Bilder: Wikimedia Commons (freie Lizenzen) und Pixabay. Musik: freie Lizenzen.</p>
{eintraege or '<p>Noch keine Videos.</p>'}</main></body></html>"""



def _github_pages(video):
    """Legt das Video auf deiner eigenen Website (faktenfieber.github.io) ab, mit sauberem video/mp4.
    Es liegt dort immer nur das jeweils aktuelle Video (die Historie wird jedes Mal ersetzt)."""
    if not GH_PAT:
        log("   GitHub-Pages-Hosting uebersprungen (Secret GH_PAT fehlt)")
        return None
    import shutil
    import tempfile
    import uuid
    repo = f"https://x-access-token:{GH_PAT}@github.com/{GH_OWNER}/{GH_OWNER}.github.io.git"
    ordner = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", *a], cwd=ordner, check=True, capture_output=True, text=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", repo, str(ordner)], check=True, capture_output=True, text=True)
        shutil.rmtree(ordner / "v", ignore_errors=True)
        for alt in ordner.glob("d-*.html"):          # alte statische Dashboards entfernen (jetzt live im Tuersteher)
            alt.unlink()
        (ordner / "v").mkdir()
        name = f"{uuid.uuid4().hex[:12]}.mp4"
        shutil.copy(video, ordner / "v" / name)
        cover = Path(video).with_name("cover.jpg")
        if cover.exists():
            shutil.copy(cover, ordner / "v" / name.replace(".mp4", ".jpg"))
        (ordner / "quellen.html").write_text(quellen_html(), encoding="utf-8")
        git("config", "user.name", "faktenfieber-bot")
        git("config", "user.email", "actions@users.noreply.github.com")
        git("checkout", "--orphan", "neu")
        git("add", "-A")
        git("commit", "-m", "Website + aktuelles Video")
        git("push", "-f", "origin", "neu:main")
    except subprocess.CalledProcessError as e:
        log(f"   GitHub-Pages-Upload fehlgeschlagen: {(e.stderr or '')[-300:].replace(GH_PAT, '***')}")
        return None
    url = f"https://{GH_OWNER}.github.io/v/{name}"
    for _ in range(24):                          # warten bis die Seite neu gebaut ist (max. ~4 Min.)
        time.sleep(10)
        try:
            r = requests.head(url, timeout=20)
            if r.status_code == 200 and "video" in r.headers.get("Content-Type", ""):
                return url
        except requests.RequestException:
            pass
    log("   GitHub Pages hat das Video nicht rechtzeitig bereitgestellt")
    return None


def _temp_uguu(video):
    with open(video, "rb") as f:
        r = requests.post("https://uguu.se/upload", timeout=300, files={"files[]": ("faktenfieber.mp4", f, "video/mp4")})
    try:
        return r.json()["files"][0]["url"]
    except Exception:
        log(f"   uguu: {r.status_code} {r.text[:150]}")
        return None


def _temp_litterbox(video):
    with open(video, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php", timeout=300,
                          data={"reqtype": "fileupload", "time": "12h"},
                          files={"fileToUpload": ("faktenfieber.mp4", f, "video/mp4")})
    url = r.text.strip()
    if r.status_code == 200 and url.startswith("https://"):
        return url
    log(f"   litterbox: {r.status_code} {r.text[:150]}")
    return None


def _temp_tmpfiles(video):
    with open(video, "rb") as f:
        r = requests.post("https://tmpfiles.org/api/v1/upload", timeout=300,
                          files={"file": ("faktenfieber.mp4", f, "video/mp4")})
    url = (r.json().get("data") or {}).get("url", "") if r.status_code == 200 else ""
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1).replace("http://", "https://") or None


def instagram_hochladen(video, daten):
    """Reel hochladen. Instagram holt sich das Video selbst von einer oeffentlichen URL ab,
    deshalb liegt es kurz (max. 12 h) bei einem Temp-Datei-Dienst."""
    if not (IG_TOKEN and IG_USER):
        raise RuntimeError("Instagram-Secrets fehlen (IG_ACCESS_TOKEN, IG_USER_ID)")
    fehler = []
    for name, hoster in (("GitHub Pages", _github_pages), ("uguu", _temp_uguu),
                         ("litterbox", _temp_litterbox), ("tmpfiles", _temp_tmpfiles)):
        try:
            video_url = hoster(video)
        except Exception as e:
            fehler.append(f"{name}: {e.__class__.__name__}")
            continue
        if not video_url:
            fehler.append(f"{name}: keine URL")
            continue
        log(f"Video kurz bereitgestellt ({name}) – Instagram holt es ab ...")
        felder = {"media_type": "REELS", "video_url": video_url, "caption": instagram_caption(daten),
                  "share_to_feed": "true", "thumb_offset": "1500", "access_token": IG_TOKEN}
        if "github.io" in video_url and Path(video).with_name("cover.jpg").exists():
            felder["cover_url"] = video_url.replace(".mp4", ".jpg")
        r = requests.post(f"{IG_API}/{IG_USER}/media", timeout=60, data=felder)
        if r.status_code != 200 or "id" not in r.json():
            raise RuntimeError(f"Instagram-Container fehlgeschlagen: {r.status_code} {r.text[:400]}")
        container = r.json()["id"]
        status = {}
        for _ in range(40):                      # bis zu ~10 Minuten auf die Verarbeitung warten
            time.sleep(15)
            status = requests.get(f"{IG_API}/{container}", timeout=30,
                                  params={"fields": "status_code,status", "access_token": IG_TOKEN}).json()
            if status.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                break
        if status.get("status_code") != "FINISHED":
            fehler.append(f"{name}: Instagram-Verarbeitung {status}")
            log(f"Instagram konnte das Video von {name} nicht verarbeiten: {status}")
            continue
        r = requests.post(f"{IG_API}/{IG_USER}/media_publish", timeout=60,
                          data={"creation_id": container, "access_token": IG_TOKEN})
        if r.status_code != 200:
            raise RuntimeError(f"Instagram-Veroeffentlichung fehlgeschlagen: {r.status_code} {r.text[:400]}")
        media = r.json()["id"]
        if daten.get("kommentar_frage"):
            try:                                   # erster Kommentar = Frage -> mehr Kommentare
                k = requests.post(f"{IG_API}/{media}/comments", timeout=30,
                                  data={"message": f"💬 {daten['kommentar_frage']}", "access_token": IG_TOKEN})
                log("Instagram: Kommentar-Frage gepostet" if k.status_code == 200 else f"Kommentar nicht moeglich: {k.text[:150]}")
            except requests.RequestException:
                pass
        link = requests.get(f"{IG_API}/{media}", timeout=30,
                            params={"fields": "permalink", "access_token": IG_TOKEN}).json().get("permalink", "")
        if story_an() and not daten.get("_lang"):
            daten["_ig_story"] = instagram_story(video_url)
        log(f"Instagram-Upload ok: {link or media}")
        daten["_ig_media_id"] = media
        return link or media
    raise RuntimeError("Instagram-Upload fehlgeschlagen: " + " | ".join(fehler))


STORY_AN = None                        # wird in modus_pruefen aus dem aktuellen Status gesetzt


def story_an():
    if STORY_AN is not None:
        return STORY_AN
    try:
        return json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8")).get("story", True)
    except Exception:
        return True


def instagram_story(video_url):
    """Teilt das Video zusaetzlich als Story (Instagram erlaubt per API kein Liken/Reposten eigener Reels -
    die Story ist der offizielle Weg, das Reel nochmal allen Followern zu zeigen)."""
    try:
        r = requests.post(f"{IG_API}/{IG_USER}/media", timeout=60, data={
            "media_type": "STORIES", "video_url": video_url, "access_token": IG_TOKEN})
        if r.status_code != 200 or "id" not in r.json():
            log(f"Story nicht moeglich: {r.text[:200]}")
            return False
        container = r.json()["id"]
        for _ in range(24):
            time.sleep(10)
            st = requests.get(f"{IG_API}/{container}", timeout=30,
                              params={"fields": "status_code", "access_token": IG_TOKEN}).json()
            if st.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                break
        if st.get("status_code") != "FINISHED":
            log(f"Story-Verarbeitung: {st}")
            return False
        r = requests.post(f"{IG_API}/{IG_USER}/media_publish", timeout=60,
                          data={"creation_id": container, "access_token": IG_TOKEN})
        log("Instagram: Story gepostet" if r.status_code == 200 else f"Story-Veroeffentlichung: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Story-Fehler: {e}")
        return False


# ---------------------------------------------------------------- Video erstellen (gemeinsamer Kern)
def erstelle_video():
    """Recherche -> Skript -> Faktencheck -> Video. Gibt (video, daten, check, dauer) zurueck."""
    for d in (OUT, WORK):
        d.mkdir(parents=True, exist_ok=True)
    if not PIXABAY_KEY:
        sys.exit("PIXABAY_API_KEY fehlt (GitHub Secrets pruefen).")

    benutzt = set(lade_benutzte())
    daten = check = None
    for versuch in range(1, MAX_VERSUCHE + 1):
        log(f"Versuch {versuch}: suche Thema ...")
        try:
            titel = thema_waehlen(benutzt)
            benutzt.add(titel)
            kandidat = skript_erstellen(titel)
            kandidat = hook_und_loop(kandidat)
            kandidat = roter_faden(kandidat)
            log(f"Skript fertig: {kandidat['titel']} – pruefe Fakten ...")
            ok, ergebnis = faktencheck(kandidat)
            for runde in range(1, 4):
                if ok:
                    break
                log(f"Reparatur-Runde {runde}: korrigiere beanstandete Stellen ...")
                kandidat = reparieren(kandidat, ergebnis)
                ok, ergebnis = faktencheck(kandidat)
        except Exception as e:
            log(f"Fehler: {e}")
            continue
        if ok:
            daten, check = kandidat, ergebnis
            log("Faktencheck bestanden ✅")
            break
        log("Faktencheck NICHT bestanden, nehme neues Thema.")

    if not daten:
        sys.exit("Kein Thema hat den Faktencheck bestanden. Bitte nochmal starten.")

    felder_auffuellen(daten)
    video, dauer = video_bauen(daten)
    meta = {k: v for k, v in daten.items() if k not in ("_artikel", "_bilder")}
    meta.update(faktencheck=check, dauer=round(dauer, 1))
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    speichere_thema(daten["thema"])
    ideen_verbraucht(daten["thema"])
    if daten.get("hook_stil"):
        hook_merken(str(daten["hook_stil"])[:30])
    return video, daten, check, dauer


def veroeffentlichen(video, daten, einzeln=True):
    """Laedt auf YouTube + Instagram hoch. einzeln=True: jede Plattform meldet sich sofort in Telegram.
    Fehler werden immer gemeldet. Gibt Ergebnis-Dict zurueck."""
    ergebnis = {}
    try:
        nachweise_merken(daten)
    except Exception as e:
        log(f"Nachweise: {e}")
    try:
        vid = youtube_hochladen(video, daten)
        ergebnis["youtube"] = f"https://youtube.com/shorts/{vid}"
        hinweis = ("" if os.environ.get("YT_SICHTBARKEIT") == "public" else
                   "\n(noch privat: bitte in der YouTube-Studio-App auf „Öffentlich“ stellen, bis das Audit durch ist)")
        if einzeln:
            telegram_text(f"▶️ YouTube: {ergebnis['youtube']}{hinweis}")
    except Exception as e:
        log(f"YouTube-Fehler: {e}")
        ergebnis["youtube_fehler"] = str(e)[:300]
        if einzeln:
            telegram_text(f"❌ YouTube-Upload fehlgeschlagen:\n{e}")
    try:
        ergebnis["instagram"] = instagram_hochladen(video, daten)
        if einzeln:
            telegram_text(f"📸 Instagram: {ergebnis['instagram']}")
    except Exception as e:
        log(f"Instagram-Fehler: {e}")
        ergebnis["instagram_fehler"] = str(e)[:300]
        if einzeln:
            telegram_text(f"❌ Instagram-Upload fehlgeschlagen:\n{e}")
    return ergebnis


# ---------------------------------------------------------------- Modus "test": sofort alles (wie bisher)
def main():
    video, daten, check, dauer = erstelle_video()
    telegram_senden(video, daten, check, dauer)
    if os.environ.get("YT_UPLOAD") == "1" or os.environ.get("IG_UPLOAD") == "1":
        veroeffentlichen(video, daten)
    log(f"FERTIG: {video} ({dauer:.1f} s)")


# ---------------------------------------------------------------- Zeitplan + Freigabe
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZEITZONE = ZoneInfo("Europe/Vienna")
SLOTS = [(8, 0), (12, 0), (18, 0)]               # Posting-Zeiten (bei Aenderung auch PLAN im Tuersteher anpassen!)
VORLAUF = timedelta(hours=2, minutes=30)          # so frueh vor dem Termin wird das Video gebaut
VERFALL_VOR_NAECHSTEM = timedelta(minutes=60)     # Freigabe moeglich bis 1 h vor dem naechsten Termin
MAX_NEU = 3                                      # so oft darf pro Slot "Neu machen" gedrueckt werden
STATUS_DATEI = ROOT / "data" / "status.json"
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def jetzt():
    return datetime.now(ZEITZONE)


def verfall_zeit(slot):
    """Ein Video kann bis 1 Stunde vor dem naechsten Termin freigegeben werden:
    8-Uhr-Video bis 11:00, 12-Uhr-Video bis 17:00, 18-Uhr-Video bis 7:00 am naechsten Morgen."""
    return slot_zeit(naechster_slot(ab=slot_zeit(slot))) - VERFALL_VOR_NAECHSTEM


def slot_zeit(slot):
    return datetime.strptime(slot, "%Y-%m-%d %H:%M").replace(tzinfo=ZEITZONE)


def naechster_slot(ab=None):
    ab = ab or jetzt()
    for tage in range(0, 3):
        tag = (ab + timedelta(days=tage)).date()
        for h, m in SLOTS:
            kandidat = datetime(tag.year, tag.month, tag.day, h, m, tzinfo=ZEITZONE)
            if kandidat > ab + timedelta(minutes=20):
                return kandidat.strftime("%Y-%m-%d %H:%M")


def status_laden():
    try:
        return json.loads(STATUS_DATEI.read_text(encoding="utf-8"))
    except Exception:
        return {"tg_offset": 0, "slots": {}}


def status_speichern(st):
    # alte Eintraege (aelter als 7 Tage) aufraeumen
    grenze = jetzt() - timedelta(days=7)
    st["slots"] = {k: v for k, v in st.get("slots", {}).items() if slot_zeit(k) > grenze}
    STATUS_DATEI.parent.mkdir(parents=True, exist_ok=True)
    STATUS_DATEI.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def tg(methode, **daten):
    if not (TG_TOKEN and TG_CHAT):
        return {}
    try:
        return requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{methode}", data=daten, timeout=40).json()
    except Exception:
        return {}


def _ziel_slot(st):
    """Welcher Termin soll gebaut werden? SLOT='sofort' = Extra-Video, das man direkt posten kann."""
    wunsch = os.environ.get("SLOT", "").strip()
    if wunsch.lower() in ("sofort", "extra", "jetzt"):
        t = jetzt().replace(second=0, microsecond=0)
        while t.strftime("%Y-%m-%d %H:%M") in st["slots"]:      # nie einen bestehenden Termin ueberschreiben
            t += timedelta(minutes=1)
        return t.strftime("%Y-%m-%d %H:%M"), True
    return (wunsch or naechster_slot()), st["slots"].get(wunsch, {}).get("extra", False)


def video_noetig():
    st = status_laden()
    slot, _ = _ziel_slot(st)
    status = st["slots"].get(slot, {}).get("status")
    if st.get("pause") and not os.environ.get("SLOT", "").strip():   # Pause gilt nur fuer den Automatik-Betrieb
        return False, slot, "Pause"
    return status not in ("wartet", "freigegeben", "gepostet"), slot, status


def modus_noetig():
    """Schneller Vorab-Check fuer GitHub: muss ueberhaupt ein Video gebaut werden? (spart Minuten)"""
    noetig, slot, status = video_noetig()
    log(f"Termin {slot}: {'Video wird gebaut' if noetig else f'schon erledigt ({status})'}")
    if not noetig and os.environ.get("SLOT", "").strip():      # gezielt angefordert -> Bescheid geben
        telegram_text(f"ℹ️ Für {slot} gibt es schon ein Video (Status: {status}), deshalb wurde nichts neu gebaut.")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"noetig={'1' if noetig else '0'}\n")


def knoepfe_fuer(slot, extra=False, auto=False):
    if auto and not extra:
        return [[{"text": "⛔ Nicht posten", "callback_data": f"stopp|{slot}"},
                 {"text": "❌ Neu machen", "callback_data": f"neu|{slot}"}],
                [{"text": "🚀 Jetzt sofort posten", "callback_data": f"jetzt|{slot}"}]]
    if extra:
        return [[{"text": "🚀 Jetzt posten", "callback_data": f"jetzt|{slot}"},
                 {"text": "❌ Neu machen", "callback_data": f"neu|{slot}"}]]
    return [[{"text": "✅ Freigeben", "callback_data": f"ok|{slot}"},
             {"text": "❌ Neu machen", "callback_data": f"neu|{slot}"}],
            [{"text": "🚀 Jetzt sofort posten", "callback_data": f"jetzt|{slot}"}]]


def modus_erstellen():
    """Baut das Video fuer den naechsten Slot (oder ein Extra-Video) und schickt es mit Knoepfen an Telegram."""
    st = status_laden()
    slot, extra = _ziel_slot(st)
    eintrag = st["slots"].get(slot, {})
    if st.get("pause") and not os.environ.get("SLOT", "").strip():
        log("Pause aktiv – kein automatisches Video.")
        return
    if eintrag.get("status") in ("wartet", "freigegeben", "gepostet"):
        log(f"Fuer {slot} gibt es schon ein Video ({eintrag['status']}) – nichts zu tun.")
        return
    global WUNSCH_THEMA, TEIL2, ANDERS_ALS
    if not WUNSCH_THEMA and eintrag.get("wunsch"):          # "Neu machen" nach /thema: beim Wunschthema bleiben
        WUNSCH_THEMA = eintrag["wunsch"]
        TEIL2 = WUNSCH_THEMA.endswith(" TEIL2")
        log(f"Neu machen – bleibe beim Wunschthema: {WUNSCH_THEMA}")
    ANDERS_ALS = list(eintrag.get("verworfen", []))
    aus_warteschlange = None
    if not extra and not WUNSCH_THEMA:
        schlange = ideen_laden()["warteschlange"]
        if schlange:
            aus_warteschlange = schlange[0]
            WUNSCH_THEMA = aus_warteschlange
            log(f"Thema aus deiner Warteschlange: {WUNSCH_THEMA}")
    log(f"Erstelle {'Extra-Video' if extra else 'Video'} fuer {slot} ...")
    video, daten, check, dauer = erstelle_video()
    if aus_warteschlange:
        d = ideen_laden()
        if d["warteschlange"] and d["warteschlange"][0] == aus_warteschlange:
            d["warteschlange"].pop(0)
            ideen_speichern(d)
    uhrzeit = slot.split(" ")[1]
    tag = "heute" if slot_zeit(slot).date() == jetzt().date() else slot_zeit(slot).strftime("%d.%m.")
    if aus_warteschlange:
        kopf_extra = f"💡 Aus deiner Ideen-Liste: „{aus_warteschlange}“\n"
    else:
        kopf_extra = ""
    auto = bool(status_laden().get("auto")) and not extra
    if auto:
        kopf_extra += (f"🤖 AUTO-MODUS: geht automatisch um {uhrzeit} Uhr raus.\n"
                       f"⛔ = nicht posten · ❌ = neues Video · 🚀 = sofort\n\n")
    if auto:
        kopf = kopf_extra
    elif extra:
        kopf = kopf_extra + (f"⚡ Wunsch-Video zu „{WUNSCH_THEMA}“ – geht raus, sobald du auf 🚀 drückst\n\n"
                             if WUNSCH_THEMA else "⚡ Extra-Video – geht raus, sobald du auf 🚀 drückst\n\n")
    else:
        kopf = kopf_extra + (f"🗓 Geplant für {tag} {uhrzeit} Uhr\n"
                             f"✅ = pünktlich posten · 🚀 = sofort posten · ❌ = neues Video\n\n")
    msg = telegram_senden(video, daten, check, dauer, knoepfe=knoepfe_fuer(slot, extra, auto), kopfzeile=kopf)
    st = status_laden()                           # neu laden (koennte sich geaendert haben)
    st["slots"][slot] = {"status": "freigegeben" if auto else "wartet", "auto": auto,
                         "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                         "message_id": msg, "titel": daten["titel"], "thema": daten.get("thema", ""), "extra": extra,
                         "neu_gemacht": eintrag.get("neu_gemacht", 0), "erstellt": jetzt().isoformat(),
                         "wunsch": WUNSCH_THEMA, "verworfen": ANDERS_ALS}
    status_speichern(st)
    log(f"FERTIG: Video fuer {slot} {'automatisch freigegeben (Auto-Modus)' if auto else 'wartet auf Freigabe'}")


def _artefakt_laden(run_id):
    """Holt das fertige Video (Artefakt) aus dem Erstell-Lauf."""
    import io
    import zipfile
    kopf = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"https://api.github.com/repos/{GH_REPO}/actions/runs/{run_id}/artifacts", headers=kopf, timeout=30)
    artefakte = [a for a in r.json().get("artifacts", []) if not a.get("expired")]
    if not artefakte:
        raise RuntimeError(f"Kein Video-Artefakt im Lauf {run_id} gefunden")
    z = requests.get(artefakte[0]["archive_download_url"], headers=kopf, timeout=300)
    ziel = OUT / f"posten_{run_id}"
    ziel.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(z.content)).extractall(ziel)
    return ziel / "faktenfieber.mp4", json.loads((ziel / "meta.json").read_text(encoding="utf-8"))


def _neu_starten(slot, thema=""):
    """Startet den Erstell-Workflow (nach 'Neu machen' fuer denselben Termin, oder mit Thema fuer Teil 2)."""
    r = requests.post(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/erstellen.yml/dispatches",
                      headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main", "inputs": {"slot": slot, "thema": thema}}, timeout=30)
    return r.status_code == 204


def modus_pruefen():
    """Laeuft regelmaessig: liest die Knopfdruecke aus Telegram und postet freigegebene Videos zur Slot-Zeit."""
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    st = status_laden()

    # 0) Befehle vom Tuersteher
    befehl = os.environ.get("AKTION", "")
    if befehl in ("pause", "weiter", "status", "schwierigkeit", "stats", "wochenbericht", "idee", "ideen", "ideen_leeren",
                  "auto_an", "auto_aus", "notstopp", "voice", "musik", "einstellungen", "kommentare", "kibilder",
                  "dashboard", "serie", "ziel", "story", "admin_pin", "admin_reset"):
        befehl_ausfuehren(st, befehl)
        status_speichern(st)
        if befehl == "status":
            return
    # 1) Knopfdruecke: sofort vom Telegram-Tuersteher (Cloudflare) weitergereicht, sonst selbst abholen
    if befehl in ("ok", "neu", "jetzt", "teil2", "stopp", "antw", "ign") and os.environ.get("AKTION_SLOT"):
        aktion_verarbeiten(st, os.environ["AKTION"], os.environ["AKTION_SLOT"], vom_tuersteher=True)
    upd = tg("getUpdates", offset=st.get("tg_offset", 0), timeout=0, allowed_updates=json.dumps(["callback_query"]))
    for u in upd.get("result", []) if upd.get("ok") else []:
        st["tg_offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if not cb or str(cb.get("message", {}).get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        aktion, _, slot = cb.get("data", "").partition("|")
        aktion_verarbeiten(st, aktion, slot, cb_id=cb["id"])

    # 2) Faellige Videos posten, verfallene melden, Erinnerungen
    global STORY_AN
    STORY_AN = bool(st.get("story", True))       # immer der allerneueste Schalter-Stand
    for slot, eintrag in sorted(st["slots"].items()):
        zeit = slot_zeit(slot)
        if eintrag.get("status") == "freigegeben" and (eintrag.get("sofort") or jetzt() >= zeit - timedelta(minutes=5)):
            try:
                video, meta = _artefakt_laden(eintrag["run_id"])
                ergebnis = veroeffentlichen(video, meta, einzeln=False)
                eintrag["status"] = "gepostet"
                eintrag["links"] = ergebnis
                eintrag["ig_media_id"] = meta.get("_ig_media_id")
                eintrag["gepostet_um"] = jetzt().isoformat()
                eintrag["_slot"] = slot
                archiv_eintragen(slot, eintrag, meta, ergebnis)
                posting_melden(eintrag, meta, ergebnis)
                eintrag.pop("_slot", None)
                log(f"{slot}: gepostet {ergebnis}")
            except Exception as e:
                eintrag["fehler"] = eintrag.get("fehler", 0) + 1
                log(f"{slot}: Posten fehlgeschlagen: {e}")
                if eintrag["fehler"] >= 3:
                    eintrag["status"] = "fehler"
                    tg("sendMessage", chat_id=TG_CHAT, text=f"❌ „{eintrag.get('titel', slot)}“ konnte nach 3 Versuchen "
                       f"nicht gepostet werden:\n{e}\n\nDrück unten, um es nochmal zu probieren.",
                       reply_markup=json.dumps({"inline_keyboard": [[{"text": "🔁 Nochmal posten",
                                                                       "callback_data": f"jetzt|{slot}"}]]}))
                else:
                    telegram_text(f"⚠️ Posten von „{eintrag.get('titel', slot)}“ hat nicht geklappt "
                                  f"(Versuch {eintrag['fehler']}/3). Ich probiere es gleich nochmal.\n\n{e}")
            status_speichern(st)                  # sofort sichern, damit nie doppelt gepostet wird
        elif eintrag.get("status") == "wartet":
            if eintrag.get("extra"):
                if jetzt() > zeit + timedelta(hours=12):
                    eintrag["status"] = "verfallen"
                continue
            if jetzt() > verfall_zeit(slot):
                eintrag["status"] = "verfallen"
                tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
                   reply_markup=json.dumps({"inline_keyboard": [[{"text": "⌛ Verfallen", "callback_data": "x"}]]}))
                telegram_text(f"⌛ Das Video für {slot} wurde nicht freigegeben und verfällt. Nichts wurde gepostet.")
            elif jetzt() > zeit - timedelta(minutes=35) and not eintrag.get("erinnert"):
                eintrag["erinnert"] = True
                telegram_text(f"⏰ Erinnerung: Das Video für {slot.split(' ')[1]} Uhr wartet noch auf deine Freigabe "
                              f"(siehe oben ✅ / ❌).")
            elif jetzt() > zeit + timedelta(hours=1) and not eintrag.get("erinnert2"):
                eintrag["erinnert2"] = True
                bis = verfall_zeit(slot).strftime("%H:%M")
                telegram_text(f"👀 Das {slot.split(' ')[1]}-Uhr-Video ist noch nicht freigegeben. Wenn du jetzt ✅ drückst, "
                              f"geht es sofort raus (möglich bis {bis} Uhr).")

    # 3) Selbstheilung + Fruehwarnungen
    try:
        selbstheilung(st)
    except Exception as e:
        log(f"Selbstheilung: {e}")
    try:
        token_warnung(st)
    except Exception as e:
        log(f"Token-Check: {e}")

    try:
        stats_aktualisieren()
    except Exception as e:
        log(f"Stats: {e}")
    try:
        kommentare_pruefen(st)
    except Exception as e:
        log(f"Kommentare: {e}")
    try:
        verlauf_merken()                          # Dashboard ist jetzt live (Cloudflare-Tuersteher /dashboard)
    except Exception as e:
        log(f"Verlauf/Dashboard: {e}")

    # 4) Instagram-Token rechtzeitig verlaengern (laeuft sonst nach 60 Tagen ab)
    try:
        instagram_token_pflegen(st)
    except Exception as e:
        log(f"Instagram-Token-Verlaengerung: {e}")
    try:
        cloudflare_token_sync(st)
    except Exception as e:
        log(f"Cloudflare-Sync: {e}")
    status_speichern(st)


def posting_melden(eintrag, meta, ergebnis):
    """Eine klare Bestaetigung nach jedem Posting, als Antwort direkt auf das Video in Telegram."""
    um = jetzt().strftime("%H:%M")
    zeilen = []
    if ergebnis.get("youtube"):
        privat = os.environ.get("YT_SICHTBARKEIT", "private") != "public"
        zeilen.append(f"▶️ YouTube{' (privat – bitte in der Studio-App auf Öffentlich stellen)' if privat else ''}:\n"
                      f"{ergebnis['youtube']}")
    else:
        zeilen.append(f"❌ YouTube hat nicht geklappt:\n{ergebnis.get('youtube_fehler', 'unbekannter Fehler')}")
    if ergebnis.get("instagram"):
        zeilen.append(f"📸 Instagram:\n{ergebnis['instagram']}" + ("\n📲 + als Story geteilt" if meta.get("_ig_story") else ""))
    else:
        zeilen.append(f"❌ Instagram hat nicht geklappt:\n{ergebnis.get('instagram_fehler', 'unbekannter Fehler')}")
    alles_ok = ergebnis.get("youtube") and ergebnis.get("instagram")
    kopf = (f"🎉 Gepostet um {um} Uhr!" if alles_ok else
            f"⚠️ Teilweise gepostet um {um} Uhr" if (ergebnis.get("youtube") or ergebnis.get("instagram")) else
            f"❌ Posten um {um} Uhr fehlgeschlagen")
    tg("sendMessage", chat_id=TG_CHAT, text=f"{kopf}\n„{meta.get('titel', '')}“\n\n" + "\n\n".join(zeilen),
       reply_to_message_id=eintrag.get("message_id") or "", disable_web_page_preview="true")
    knopf = f"🎉 Gepostet um {um} Uhr" if alles_ok else f"⚠️ Gepostet um {um} Uhr (mit Fehlern)"
    reihen = [[{"text": knopf, "callback_data": "x"}]]
    slot = next((k for k, v in status_laden().get("slots", {}).items() if v is eintrag), None) or eintrag.get("_slot")
    if slot:
        reihen.append([{"text": "🔁 Teil 2 zu diesem Thema bauen", "callback_data": f"teil2|{slot}"}])
    tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
       reply_markup=json.dumps({"inline_keyboard": reihen}))


# ================================================================ Lernen aus den Zahlen
ARCHIV_DATEI = ROOT / "data" / "archiv.json"


def archiv_laden():
    try:
        return json.loads(ARCHIV_DATEI.read_text(encoding="utf-8"))
    except Exception:
        return []


def archiv_speichern(archiv):
    ARCHIV_DATEI.parent.mkdir(parents=True, exist_ok=True)
    ARCHIV_DATEI.write_text(json.dumps(archiv[-400:], ensure_ascii=False, indent=1), encoding="utf-8")


def archiv_eintragen(slot, eintrag, meta, ergebnis):
    """Merkt sich jedes gepostete Video dauerhaft (fuer Statistik, Lernen, Wochen-Video)."""
    archiv = archiv_laden()
    yt = (ergebnis.get("youtube") or "").rstrip("/").split("/")[-1] or None
    archiv.append({
        "slot": slot, "gepostet": jetzt().isoformat(), "titel": meta.get("titel", ""), "thema": meta.get("thema", ""),
        "hook_stil": meta.get("hook_stil", ""), "stimme": meta.get("_stimme", ""), "quelle": meta.get("_quelle", ""),
        "stunde": slot[11:13], "ig": meta.get("_ig_media_id") or eintrag.get("ig_media_id"), "yt": yt,
        "ig_link": ergebnis.get("instagram"), "run_id": eintrag.get("run_id"), "dauer": meta.get("dauer"),
        "stats": {},
    })
    archiv_speichern(archiv)


def _ig_zahlen(media_id):
    d = {}
    try:
        d = requests.get(f"{IG_API}/{media_id}", timeout=30, params={
            "fields": "like_count,comments_count", "access_token": IG_TOKEN}).json()
    except requests.RequestException:
        pass
    aufrufe = None
    try:
        ins = requests.get(f"{IG_API}/{media_id}/insights", timeout=30,
                           params={"metric": "views", "access_token": IG_TOKEN}).json()
        aufrufe = ins["data"][0]["values"][0]["value"]
    except Exception:
        pass
    return {"likes": d.get("like_count", 0) or 0, "kommentare": d.get("comments_count", 0) or 0, "aufrufe": aufrufe}


def _yt_aufrufe(ids):
    """Aufrufe/Likes von YouTube (braucht das Secret YT_API_KEY, sonst leer)."""
    key = os.environ.get("YT_API_KEY", "")
    if not key or not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 50):
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30, params={
                "part": "statistics", "id": ",".join(ids[i:i + 50]), "key": key}).json()
            for v in r.get("items", []):
                st = v.get("statistics", {})
                out[v["id"]] = {"yt_aufrufe": int(st.get("viewCount", 0)), "yt_likes": int(st.get("likeCount", 0))}
        except Exception:
            pass
    return out


def punkte(stats):
    """Ein Erfolgswert pro Video (Aufrufe zaehlen am meisten, dann Kommentare, dann Likes)."""
    aufrufe = (stats.get("aufrufe") or 0) + (stats.get("yt_aufrufe") or 0)
    return round(aufrufe / 20 + (stats.get("likes", 0) + stats.get("yt_likes", 0)) + 3 * stats.get("kommentare", 0), 1)


def stats_aktualisieren(erzwingen=False):
    """Holt frische Zahlen fuer alle Videos der letzten 30 Tage (hoechstens stuendlich, ausser erzwungen)."""
    archiv = archiv_laden()
    grenze = jetzt() - timedelta(days=30)
    aktiv = [v for v in archiv if datetime.fromisoformat(v["gepostet"]) > grenze]
    faellig = [v for v in aktiv if erzwingen or not v.get("stats_am")
               or jetzt() - datetime.fromisoformat(v["stats_am"]) > timedelta(minutes=50)]
    geaendert = False
    for v in archiv:                              # YouTube-Richtlinie: API-Daten nicht laenger als 30 Tage aufheben
        if datetime.fromisoformat(v["gepostet"]) <= grenze and any(k.startswith("yt_") for k in v.get("stats", {})):
            v["stats"] = {k: w for k, w in v["stats"].items() if not k.startswith("yt_")}
            v["punkte"] = punkte(v["stats"])
            geaendert = True
    if geaendert:
        archiv_speichern(archiv)
    if not faellig:
        return archiv
    yt = _yt_aufrufe([v["yt"] for v in faellig if v.get("yt")])
    for v in faellig:
        neu = _ig_zahlen(v["ig"]) if v.get("ig") and IG_TOKEN else {}
        neu.update(yt.get(v.get("yt"), {}))
        if neu:
            v["stats"] = neu
            v["punkte"] = punkte(neu)
            v["stats_am"] = jetzt().isoformat()
    archiv_speichern(archiv)
    return archiv


def erfolgs_profil(archiv=None, mindestens=6):
    """Was laeuft bei UNS gut? Kurzer Text fuer die KI (Themenwahl + Hook)."""
    archiv = archiv if archiv is not None else archiv_laden()
    mit = [v for v in archiv if v.get("punkte") is not None]
    if len(mit) < mindestens:
        return ""
    mittel = sum(v["punkte"] for v in mit) / len(mit)

    def gruppe(feld):
        g = {}
        for v in mit:
            if v.get(feld):
                g.setdefault(v[feld], []).append(v["punkte"])
        return sorted(((k, sum(p) / len(p), len(p)) for k, p in g.items() if len(p) >= 2), key=lambda x: -x[1])

    teile = []
    top = sorted(mit, key=lambda v: -v["punkte"])[:5]
    flop = sorted(mit, key=lambda v: v["punkte"])[:3]
    teile.append("Top-Videos: " + "; ".join(f"{v['thema']} ({v['punkte']:.0f})" for v in top))
    teile.append("Flops: " + "; ".join(f"{v['thema']} ({v['punkte']:.0f})" for v in flop))
    for feld, name in (("hook_stil", "Hook-Stile"), ("quelle", "Themen-Quellen"), ("stunde", "Uhrzeiten")):
        g = gruppe(feld)
        if g:
            teile.append(f"{name}: " + ", ".join(f"{k} {p / mittel * 100:.0f}%" for k, p, n in g[:4]))
    return "LERNDATEN UNSERES KANALS (100% = Durchschnitt):\n" + "\n".join(teile)


# ================================================================ Kommentare beantworten
def kommentare_pruefen(st):
    """Holt neue Instagram-Kommentare, laesst die KI eine Antwort vorschlagen und schickt sie dir mit ✅ / 🙈."""
    if not (IG_TOKEN and IG_USER) or not st.get("kommentare_an", True):
        return
    letzte = st.get("kommentare_check")
    if letzte and jetzt() - datetime.fromisoformat(letzte) < timedelta(hours=2):
        return
    st["kommentare_check"] = jetzt().isoformat()
    gesehen = set(st.get("kommentare_gesehen", []))
    if "ig_name" not in st:
        try:
            st["ig_name"] = requests.get(f"{IG_API}/me", timeout=30,
                                         params={"fields": "username", "access_token": IG_TOKEN}).json().get("username", "")
        except requests.RequestException:
            st["ig_name"] = ""
    neue = []
    for v in [v for v in archiv_laden() if v.get("ig")][-10:]:
        try:
            r = requests.get(f"{IG_API}/{v['ig']}/comments", timeout=30, params={
                "fields": "id,text,username,timestamp,replies{username}", "access_token": IG_TOKEN}).json()
        except requests.RequestException:
            continue
        if "error" in r:
            if not st.get("kommentare_fehler_gemeldet"):
                st["kommentare_fehler_gemeldet"] = True
                telegram_text("ℹ️ Kommentare können nicht gelesen werden – der Instagram-Token braucht die Berechtigung "
                              "instagram_business_manage_comments (siehe Handbuch). Bis dahin überspringe ich das.")
            return
        for k in r.get("data", []):
            schon_beantwortet = any(a.get("username") == st.get("ig_name") for a in (k.get("replies") or {}).get("data", []))
            if k["id"] in gesehen or k.get("username") == st.get("ig_name") or schon_beantwortet:
                gesehen.add(k["id"])
                continue
            neue.append((v, k))
    spam = re.compile(r"(https?://|www\.|\.(com|xyz|ru|io)\b|follower|dm me|check my|promo|gewinnspiel)", re.I)
    for v, k in neue:
        if spam.search(k.get("text", "")):
            gesehen.add(k["id"])                   # Links/Spam gar nicht erst vorschlagen
    neue = [(v, k) for v, k in neue if k["id"] not in gesehen]
    for v, k in neue[:5]:                         # nicht mehr als 5 auf einmal
        gesehen.add(k["id"])
        try:
            vorschlag, _ = ki(f"""Du betreust den deutschen Fakten-Kanal FAKTENFIEBER auf Instagram.
Video: "{v.get('titel')}" (Thema: {v.get('thema')})
Kommentar von @{k.get('username')}: "{k.get('text', '')[:400]}"

Schreib eine kurze, sympathische Antwort (max. 2 Saetze, locker, Du-Form, 0-1 Emoji). Bei Fragen: ehrlich antworten,
nur mit dem, was zum Thema sicher stimmt; im Zweifel neugierig zurueckfragen. Keine Werbung, keine Diskussionen.
Spam, Beleidigungen, Hass oder Links -> "ignorieren": true.

Antworte NUR mit JSON: {{"antwort": "...", "ignorieren": true/false}}""", temperatur=0.7)
        except Exception as e:
            log(f"Kommentar-Vorschlag nicht moeglich: {e}")
            continue
        if vorschlag.get("ignorieren") or not vorschlag.get("antwort"):
            continue
        st.setdefault("antworten", {})[k["id"]] = {"text": vorschlag["antwort"][:300], "titel": v.get("titel", "")}
        tg("sendMessage", chat_id=TG_CHAT,
           text=f"💬 Neuer Kommentar zu „{v.get('titel', '')[:40]}“\n@{k.get('username')}: {k.get('text', '')[:300]}\n\n"
                f"🤖 Vorschlag:\n{vorschlag['antwort'][:300]}",
           reply_markup=json.dumps({"inline_keyboard": [[{"text": "✅ So antworten", "callback_data": f"antw|{k['id']}"},
                                                         {"text": "🙈 Ignorieren", "callback_data": f"ign|{k['id']}"}]]}))
    st["kommentare_gesehen"] = list(gesehen)[-500:]
    st["antworten"] = dict(list(st.get("antworten", {}).items())[-50:])


def kommentar_antworten(st, kommentar_id, senden=True):
    vorschlag = st.get("antworten", {}).pop(kommentar_id, None)
    if not vorschlag:
        return telegram_text("ℹ️ Diesen Kommentar habe ich schon erledigt.")
    if not senden:
        return
    r = requests.post(f"{IG_API}/{kommentar_id}/replies", timeout=30,
                      data={"message": vorschlag["text"], "access_token": IG_TOKEN})
    telegram_text("✅ Antwort gepostet." if r.status_code == 200 else f"⚠️ Antwort nicht möglich: {r.text[:200]}")


def statistik_senden(st, wochenbericht=False):
    """Zahlen der letzten Videos (Instagram + YouTube), bestes Video, und beim Wochenbericht KI-Tipps."""
    archiv = stats_aktualisieren(erzwingen=True)
    tage = 7 if wochenbericht else 30
    neu = [v for v in archiv if datetime.fromisoformat(v["gepostet"]) > jetzt() - timedelta(days=tage)]
    if not neu:
        telegram_text("📈 Noch keine geposteten Videos mit Statistik (zählt ab jetzt mit).")
        return
    zeilen = []
    for v in sorted(neu, key=lambda v: v["gepostet"])[-14:]:
        z = v.get("stats", {})
        aufr = (z.get("aufrufe") or 0) + (z.get("yt_aufrufe") or 0)
        zeilen.append(f"{v['slot'][5:]}  👀 {aufr if aufr else '–'}  ❤️ {z.get('likes', 0) + z.get('yt_likes', 0)}  "
                      f"💬 {z.get('kommentare', 0)}\n   „{v['titel'][:45]}“")
    bestes = max(neu, key=lambda v: v.get("punkte") or 0)
    summe = sum((v.get("stats", {}).get("aufrufe") or 0) + (v.get("stats", {}).get("yt_aufrufe") or 0) for v in neu)
    text = (("📈 WOCHENBERICHT\n\n" if wochenbericht else f"📈 STATISTIK (letzte {tage} Tage)\n\n")
            + "\n\n".join(zeilen)
            + f"\n\n🎬 Videos: {len(neu)} · 👀 Aufrufe gesamt: {summe if summe else '–'}"
            + f"\n🏆 Bestes Video: „{bestes['titel'][:50]}“")
    if not os.environ.get("YT_API_KEY"):
        text += "\n\nℹ️ YouTube-Zahlen fehlen noch: dafür das Secret YT_API_KEY anlegen (siehe Handbuch)."
    telegram_text(text)
    if wochenbericht:
        ki_tipps(archiv)
        try:
            ziel_senden()
        except Exception as e:
            log(f"Ziel im Wochenbericht: {e}")


def ki_tipps(archiv):
    """Die KI analysiert die Zahlen und gibt 3 konkrete Tipps fuer die naechste Woche."""
    profil = erfolgs_profil(archiv, mindestens=4)
    if not profil:
        telegram_text("🧠 Für KI-Tipps brauche ich noch etwas mehr Daten (mind. 4 Videos mit Zahlen).")
        return
    try:
        antwort, _ = ki(f"""Du bist Social-Media-Stratege fuer einen deutschen Fakten-Shorts-Kanal (YouTube + Instagram).
Hier sind die echten Zahlen des Kanals:

{profil}

Gib 3 konkrete, umsetzbare Tipps fuer die naechste Woche (Themen, Einstieg, Uhrzeit, Stil). Kurz, klar, auf Deutsch,
locker formuliert. Nur Tipps, die sich wirklich aus den Zahlen ableiten lassen. Wenn die Daten zu duenn sind, sag das ehrlich.

Antworte NUR mit JSON: {{"tipps": ["...", "...", "..."], "themen_vorschlaege": ["konkretes Thema 1", "konkretes Thema 2", "konkretes Thema 3"]}}""",
                        temperatur=0.4)
    except Exception as e:
        log(f"KI-Tipps nicht moeglich: {e}")
        return
    tipps = "\n".join(f"{i+1}. {t}" for i, t in enumerate(antwort.get("tipps", [])[:3]))
    themen = "\n".join(f"• /idee {t}" for t in antwort.get("themen_vorschlaege", [])[:3])
    telegram_text(f"🧠 KI-TIPPS FÜR NÄCHSTE WOCHE\n\n{tipps}\n\n💡 Themen, die gut passen würden (Zeile kopieren & an mich schicken):\n{themen}")


# ================================================================ Monetarisierungs-Ziel + Dashboard
YT_HANDLE = os.environ.get("YT_HANDLE", "@faktenfieber.official")
VERLAUF_DATEI = ROOT / "data" / "verlauf.json"
# YouTube-Partnerprogramm (Shorts-Weg): Stufe 1 = Fan-Funktionen, Stufe 2 = Werbeeinnahmen
YPP_STUFEN = [("Stufe 1 (Super Thanks, Kanalmitgliedschaft)", 500, 3_000_000),
              ("Stufe 2 (Werbegeld aus Shorts)", 1000, 10_000_000)]


def kanal_zahlen():
    """Abos + Aufrufe auf YouTube (YT_API_KEY) und Follower auf Instagram. Fehlendes bleibt None."""
    z = {"yt_abos": None, "yt_aufrufe_gesamt": None, "yt_shorts_90": None, "ig_follower": None, "ig_beitraege": None}
    key = os.environ.get("YT_API_KEY", "")
    if key:
        try:
            kanal = {"forHandle": YT_HANDLE}
            eigene = [v["yt"] for v in archiv_laden() if v.get("yt")]
            if eigene:                            # sicherster Weg: Kanal ueber ein eigenes Video finden
                info = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30, params={
                    "part": "snippet", "id": eigene[-1], "key": key}).json()
                cid = ((info.get("items") or [{}])[0].get("snippet") or {}).get("channelId")
                if cid:
                    kanal = {"id": cid}
            r = requests.get("https://www.googleapis.com/youtube/v3/channels", timeout=30, params={
                "part": "statistics", **kanal, "key": key}).json()
            item = (r.get("items") or [{}])[0].get("statistics", {})
            if item:
                z["yt_abos"] = int(item.get("subscriberCount", 0))
                z["yt_aufrufe_gesamt"] = int(item.get("viewCount", 0))
        except Exception as e:
            log(f"YouTube-Kanalzahlen: {e}")
        grenze = jetzt() - timedelta(days=90)
        ids = [v["yt"] for v in archiv_laden() if v.get("yt") and datetime.fromisoformat(v["gepostet"]) > grenze]
        if ids:
            z["yt_shorts_90"] = sum(x["yt_aufrufe"] for x in _yt_aufrufe(ids).values())
        elif z["yt_abos"] is not None:
            z["yt_shorts_90"] = 0
    if IG_TOKEN:
        try:
            r = requests.get(f"{IG_API}/me", timeout=30, params={
                "fields": "followers_count,media_count", "access_token": IG_TOKEN}).json()
            z["ig_follower"] = r.get("followers_count")
            z["ig_beitraege"] = r.get("media_count")
        except requests.RequestException:
            pass
    return z


def verlauf_laden():
    return _lade_json(VERLAUF_DATEI) or []


def verlauf_merken(erzwingen=False):
    """Einmal am Tag die Kanalzahlen notieren (daraus entstehen Prognose und Dashboard-Kurve)."""
    verlauf = verlauf_laden()
    heute = jetzt().date().isoformat()
    if verlauf and verlauf[-1].get("tag") == heute and not erzwingen:
        return verlauf, False
    z = kanal_zahlen()
    if all(v is None for v in z.values()):
        return verlauf, False
    verlauf = [v for v in verlauf if v.get("tag") != heute] + [{"tag": heute, **z}]
    alt = (jetzt() - timedelta(days=30)).date().isoformat()
    verlauf = [v if v["tag"] >= alt else {k: w for k, w in v.items() if not k.startswith("yt_")} for v in verlauf]
    VERLAUF_DATEI.parent.mkdir(parents=True, exist_ok=True)
    VERLAUF_DATEI.write_text(json.dumps(verlauf[-400:], ensure_ascii=False, indent=1), encoding="utf-8")
    return verlauf, True


def _pro_tag(verlauf, feld, tage=14):
    """Durchschnittlicher Zuwachs pro Tag der letzten ~2 Wochen (None, wenn zu wenig Daten)."""
    punkte_ = [(date.fromisoformat(v["tag"]), v[feld]) for v in verlauf if v.get(feld) is not None]
    if len(punkte_) < 2:
        return None
    ende = punkte_[-1]
    start = next((p for p in punkte_ if (ende[0] - p[0]).days <= tage), punkte_[0])
    spanne = (ende[0] - start[0]).days
    return None if spanne < 3 else (ende[1] - start[1]) / spanne


def _balken(anteil, breite=10):
    voll = max(0, min(breite, round(anteil * breite)))
    return "▰" * voll + "▱" * (breite - voll)


def _zahl(n):
    if n is None:
        return "–"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",") + " Mio."
    return f"{n:,}".replace(",", ".")


def ziel_senden():
    """/ziel: Wie weit ist der Kanal von der Monetarisierung entfernt – mit Prognose."""
    verlauf = verlauf_laden()
    z = kanal_zahlen()
    if all(v is None for v in z.values()):
        telegram_text("🎯 Ich komme noch an keine Kanalzahlen. Dafür braucht GitHub das Secret YT_API_KEY "
                      "(Anleitung im Handbuch, dauert 3 Minuten).")
        return
    text = "🎯 ZIEL: MONETARISIERUNG\n\n"
    if z["yt_abos"] is None:
        text += "▶️ YouTube: noch keine Zahlen (Secret YT_API_KEY fehlt)\n"
    else:
        abo_rate = _pro_tag(verlauf, "yt_abos")
        aufr_rate = _pro_tag(verlauf, "yt_shorts_90")
        text += (f"▶️ YouTube: {_zahl(z['yt_abos'])} Abos · {_zahl(z['yt_shorts_90'])} Shorts-Aufrufe (90 Tage, ca.)\n")
        for name, abos, aufrufe in YPP_STUFEN:
            a, b = z["yt_abos"] / abos, (z["yt_shorts_90"] or 0) / aufrufe
            text += (f"\n{'✅' if a >= 1 and b >= 1 else '🔒'} {name}\n"
                     f"   Abos     {_balken(a)} {min(a, 1) * 100:.0f} %  ({_zahl(z['yt_abos'])}/{_zahl(abos)})\n"
                     f"   Aufrufe  {_balken(b)} {min(b, 1) * 100:.0f} %  ({_zahl(z['yt_shorts_90'])}/{_zahl(aufrufe)})\n")
            if not (a >= 1 and b >= 1):
                prognose = []
                if a < 1:
                    prognose.append(f"Abos in ca. {math.ceil((abos - z['yt_abos']) / abo_rate)} Tagen"
                                    if abo_rate and abo_rate > 0 else "Abos: noch kein Trend")
                if b < 1:
                    prognose.append(f"Aufrufe in ca. {math.ceil((aufrufe - (z['yt_shorts_90'] or 0)) / aufr_rate)} Tagen"
                                    if aufr_rate and aufr_rate > 0 else "Aufrufe: noch kein Trend")
                text += "   📅 " + " · ".join(prognose) + "\n"
        if abo_rate is not None:
            text += f"\n📈 Tempo: {abo_rate:+.1f} Abos pro Tag".replace(".", ",")
            if aufr_rate is not None:
                text += f" · {aufr_rate:+,.0f} Aufrufe pro Tag".replace(",", ".")
            text += "\n"
    if z["ig_follower"] is not None:
        ig_rate = _pro_tag(verlauf, "ig_follower")
        text += (f"\n📸 Instagram: {_zahl(z['ig_follower'])} Follower · {z.get('ig_beitraege') or 0} Beiträge"
                 + (f" ({ig_rate:+.1f}/Tag)".replace(".", ",") if ig_rate is not None else "")
                 + "\n   (Instagram zahlt über Bonus-Programme/Kooperationen – ab ca. 10.000 Followern werden Marken aufmerksam)\n")
    if len(verlauf) < 4:
        text += "\nℹ️ Die Prognose wird ab ca. 4 Tagen Daten genau – ich notiere die Zahlen jeden Tag automatisch."
    text += "\n\nℹ️ Die 90-Tage-Aufrufe sind eine Näherung; die offizielle Zahl steht in YouTube Studio → Verdienen."
    telegram_text(text)


def dashboard_html(verlauf, archiv):
    """Eine einfache, selbststaendige Statistik-Seite (keine Tokens, nur oeffentliche Zahlen)."""
    import html as _h
    letzte = verlauf[-1] if verlauf else {}
    reihe = [v for v in verlauf if v.get("yt_abos") is not None or v.get("ig_follower") is not None][-60:]

    def linie(feld, farbe):
        werte = [(i, v[feld]) for i, v in enumerate(reihe) if v.get(feld) is not None]
        if len(werte) < 2:
            return ""
        tief, hoch = min(w for _, w in werte), max(w for _, w in werte)
        spanne = (hoch - tief) or 1
        breite = max(len(reihe) - 1, 1)
        pts = " ".join(f"{i / breite * 600:.1f},{175 - (w - tief) / spanne * 160:.1f}" for i, w in werte)
        return f'<polyline points="{pts}" fill="none" stroke="{farbe}" stroke-width="3" stroke-linejoin="round"/>'

    kurve = linie("yt_abos", "var(--yt)") + linie("ig_follower", "var(--ig)")
    grenze = jetzt() - timedelta(days=30)
    neu = sorted([v for v in archiv if datetime.fromisoformat(v["gepostet"]) > grenze],
                 key=lambda v: -(v.get("punkte") or 0))
    zeilen = "".join(
        f"<tr><td>{_h.escape(v['slot'][5:16])}</td><td>{_h.escape(v.get('titel', '')[:70])}</td>"
        f"<td class=n>{_zahl((v.get('stats', {}).get('aufrufe') or 0) + (v.get('stats', {}).get('yt_aufrufe') or 0))}</td>"
        f"<td class=n>{v.get('stats', {}).get('likes', 0) + v.get('stats', {}).get('yt_likes', 0)}</td>"
        f"<td class=n>{v.get('stats', {}).get('kommentare', 0)}</td></tr>" for v in neu[:30])
    stufen = ""
    for name, abos, aufrufe in YPP_STUFEN:
        a = min((letzte.get("yt_abos") or 0) / abos, 1)
        b = min((letzte.get("yt_shorts_90") or 0) / aufrufe, 1)
        stufen += (f'<div class=ziel><b>{_h.escape(name)}</b>'
                   f'<label>Abos {a * 100:.0f} %</label><div class=bar><i style="width:{a * 100:.1f}%"></i></div>'
                   f'<label>Shorts-Aufrufe 90 Tage {b * 100:.0f} %</label><div class=bar><i style="width:{b * 100:.1f}%"></i></div></div>')
    stand = jetzt().strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow">
<title>Faktenfieber Dashboard</title><style>
:root{{--bg:#0f0f12;--card:#1a1a20;--text:#f2f2f2;--muted:#9a9aa5;--akzent:#ffd400;--yt:#ff4d4d;--ig:#c86bff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif;padding:16px}}
main{{max-width:900px;margin:auto}}h1{{font-size:24px;margin:8px 0}}h1 span{{color:var(--akzent)}}
.kacheln{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}
.k,.ziel,.box{{background:var(--card);border-radius:12px;padding:14px}}.k b{{display:block;font-size:26px}}
.k small,label,.m{{color:var(--muted);font-size:13px}}.bar{{height:10px;background:#2c2c35;border-radius:5px;overflow:hidden;margin:2px 0 8px}}
.bar i{{display:block;height:100%;background:var(--akzent)}}.ziele{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}}
.ziel b{{display:block;margin-bottom:6px}}svg{{width:100%;height:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{padding:6px 4px;border-bottom:1px solid #2c2c35;text-align:left}}.n{{text-align:right;white-space:nowrap}}
.box{{margin-top:12px;overflow-x:auto}}.leg span{{margin-right:14px}}</style></head><body><main>
<h1>🔥 Faktenfieber <span>Dashboard</span></h1><div class=m>Stand: {stand} · aktualisiert sich täglich</div>
<div class=kacheln>
<div class=k><small>YouTube-Abos</small><b>{_zahl(letzte.get('yt_abos'))}</b></div>
<div class=k><small>Shorts-Aufrufe (90 T.)</small><b>{_zahl(letzte.get('yt_shorts_90'))}</b></div>
<div class=k><small>Instagram-Follower</small><b>{_zahl(letzte.get('ig_follower'))}</b></div>
<div class=k><small>Videos (30 T.)</small><b>{len(neu)}</b></div></div>
<h2>Ziel: Monetarisierung</h2><div class=ziele>{stufen}</div>
<div class=box><div class="m leg"><span style="color:var(--yt)">━ YouTube-Abos</span><span style="color:var(--ig)">━ Instagram-Follower</span></div>
<svg viewBox="0 0 600 190">{kurve or '<text x=10 y=100 fill="#9a9aa5">Kurve erscheint ab 2 Tagen Daten</text>'}</svg></div>
<div class=box><h2>Videos der letzten 30 Tage</h2><table><tr><th>Termin</th><th>Titel</th><th class=n>👀</th><th class=n>❤️</th><th class=n>💬</th></tr>
{zeilen or '<tr><td colspan=5 class=m>Noch keine Videos mit Zahlen</td></tr>'}</table></div>
</main></body></html>"""


def dashboard_hochladen(st):
    """Legt die Seite auf faktenfieber.github.io/<geheimer-name>.html (Rest der Website bleibt unveraendert)."""
    if not GH_PAT or not st.get("dashboard"):
        return None
    import tempfile
    seite = dashboard_html(verlauf_laden(), archiv_laden())
    repo = f"https://x-access-token:{GH_PAT}@github.com/{GH_OWNER}/{GH_OWNER}.github.io.git"
    ordner = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", *a], cwd=ordner, check=True, capture_output=True, text=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", repo, str(ordner)],
                       check=True, capture_output=True, text=True)
        (ordner / f"{st['dashboard']}.html").write_text(seite, encoding="utf-8")
        git("config", "user.name", "faktenfieber-bot")
        git("config", "user.email", "actions@users.noreply.github.com")
        git("checkout", "--orphan", "neu")          # ohne Historie, damit das Repo klein bleibt
        git("add", "-A")
        git("commit", "-m", "Dashboard")
        git("push", "-f", "origin", "neu:main")
    except subprocess.CalledProcessError as e:
        log(f"Dashboard-Upload fehlgeschlagen: {(e.stderr or '')[-300:].replace(GH_PAT, '***')}")
        return None
    st["dashboard_am"] = jetzt().isoformat()
    return f"https://{GH_OWNER}.github.io/{st['dashboard']}.html"


def dashboard_befehl(st):
    wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
    if wahl == "aus":
        st.pop("dashboard", None)
        telegram_text("📊 Dashboard aus. Die Seite wird nicht mehr aktualisiert (sie verschwindet beim nächsten Video-Upload).")
        return
    if not GH_PAT:
        telegram_text("📊 Für das Dashboard braucht GitHub das Secret GH_PAT (hast du schon, wenn Instagram läuft).")
        return
    if wahl == "neu" or not st.get("dashboard"):
        import uuid
        st["dashboard"] = "d-" + uuid.uuid4().hex[:10]   # schwer zu erraten, nicht in Google
    verlauf_merken(erzwingen=True)
    stats_aktualisieren()
    url = dashboard_hochladen(st)
    if url:
        telegram_text(f"📊 Dein Dashboard (aktualisiert sich jeden Tag):\n{url}\n\nDauert beim ersten Mal 1–2 Min., bis "
                      f"die Seite da ist. Sie ist nicht in Google, aber wer den Link hat, kann sie sehen – also nur "
                      f"teilen, mit wem du willst. Neuer Link: /dashboard neu · Abschalten: /dashboard aus")
    else:
        telegram_text("⚠️ Dashboard konnte nicht hochgeladen werden (Details im Actions-Log).")


# ================================================================ Themen-Tage (Serien)
THEMEN_TAGE = {
    0: ("Mystery-Montag", "ungeloeste Raetsel, mysterioese Orte, unerklaerte Ereignisse"),
    2: ("Was-zur-Hölle-Mittwoch", "absurde, kaum zu glaubende aber wahre Geschichten"),
    4: ("Fail-Freitag", "legendaere Fehlschlaege, Pannen und Fehlentscheidungen der Geschichte"),
    6: ("Sonntags-Erfinder", "Erfindungen und Entdeckungen mit ueberraschender Entstehungsgeschichte"),
}


def themen_tag():
    """(Name, Beschreibung) wenn heute Serien-Tag ist und /serie an ist, sonst None."""
    try:
        st = status_laden()
    except Exception:
        return None
    if not st.get("serie"):
        return None
    return THEMEN_TAGE.get(jetzt().weekday())


def serie_befehl(st):
    wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
    if wahl in ("an", "aus"):
        st["serie"] = wahl == "an"
    tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    plan = "\n".join(f"• {tage[t]}: {n} – {b.replace('ae', 'ä').replace('oe', 'ö').replace('ue', 'ü')}"
                     for t, (n, b) in THEMEN_TAGE.items())
    telegram_text(f"📺 THEMEN-TAGE sind {'AN' if st.get('serie') else 'AUS'}\n\n{plan}\n\n"
                  "An diesen Tagen passen alle automatischen Videos zur Serie (mit eigenem Hashtag) – das baut "
                  "Wiedererkennung auf, Leute kommen gezielt wieder. Die anderen Tage bleiben bunt gemischt.\n"
                  "/serie an · /serie aus")


# ================================================================ Wochen-Video (Top 5)
def _karte(text_oben, text_gross, ziel, dauer=2.5):
    """Zwischentitel im Faktenfieber-Look (schwarz, gelb/weiss) als kurzer Clip mit Stille."""
    from PIL import Image, ImageDraw, ImageFont
    bild = Image.new("RGB", (W, H), (12, 12, 16))
    z = ImageDraw.Draw(bild)
    klein, gross = ImageFont.truetype(FONT_DATEI, 70), ImageFont.truetype(FONT_DATEI, 96)
    z.text(((W - klein.getlength(text_oben)) / 2, 700), text_oben, font=klein, fill=(255, 225, 0))
    zeilen, akt = [], ""
    for w in text_gross.upper().split():
        if gross.getlength(f"{akt} {w}".strip()) > W - 140:
            zeilen.append(akt)
            akt = w
        else:
            akt = f"{akt} {w}".strip()
    zeilen.append(akt)
    y = 860
    for zeile in zeilen[:4]:
        z.text(((W - gross.getlength(zeile)) / 2, y), zeile, font=gross, fill=(255, 255, 255),
               stroke_width=4, stroke_fill=(0, 0, 0))
        y += 115
    png = Path(str(ziel) + ".png")
    bild.save(png)
    ffmpeg("-loop", "1", "-i", png, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dauer}",
           "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", "-shortest", ziel)
    return ziel


def modus_wochenvideo():
    """Baut aus den 5 besten Videos der Woche ein laengeres Video (gut fuer Wiedergabezeit/Monetarisierung)."""
    for d in (OUT, WORK):
        d.mkdir(parents=True, exist_ok=True)
    archiv = stats_aktualisieren(erzwingen=True)
    woche = [v for v in archiv if v.get("run_id") and datetime.fromisoformat(v["gepostet"]) > jetzt() - timedelta(days=7)]
    woche.sort(key=lambda v: -(v.get("punkte") or 0))
    top = []
    for v in woche:
        if len(top) >= 5:
            break
        try:
            video, meta = _artefakt_laden(v["run_id"])
            top.append((v, video))
        except Exception as e:
            log(f"Video '{v['titel']}' nicht mehr verfuegbar: {e}")
    if len(top) < 3:
        telegram_text(f"🎬 Wochen-Video: Diese Woche gibt es erst {len(top)} passende Videos (mind. 3 nötig). Nächste Woche!")
        return
    top = list(reversed(top))                      # Countdown: Platz 5 zuerst, Platz 1 zum Schluss
    kw = jetzt().isocalendar()[1]
    teile, kapitel, zeit = [], [], 0.0

    def dauer(pfad):
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(pfad)], capture_output=True, text=True).stdout or 0)

    intro = _karte("FAKTENFIEBER", f"Die {len(top)} krassesten Fakten der Woche", WORK / "w_intro.mp4", 3)
    teile.append(intro)
    kapitel.append((0.0, "Intro"))
    zeit += dauer(intro)
    for platz, (v, video) in zip(range(len(top), 0, -1), top):
        karte = _karte(f"PLATZ {platz}", v["titel"], WORK / f"w_karte_{platz}.mp4")
        kapitel.append((zeit, f"Platz {platz}: {v['titel']}"))
        teile += [karte, video]
        zeit += dauer(karte) + dauer(video)
    outro = _karte("DAS WAR'S!", "Folg Faktenfieber für mehr unnützes Wissen", WORK / "w_outro.mp4", 3)
    teile.append(outro)
    # alles auf ein einheitliches Format bringen und zusammenfuegen
    einheitlich = []
    for i, t in enumerate(teile):
        z = WORK / f"w_teil_{i:02d}.mp4"
        ffmpeg("-i", t, "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p,setsar=1", "-ar", "44100", "-ac", "2",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "192k", z)
        einheitlich.append(z)
    liste = WORK / "w_liste.txt"
    liste.write_text("".join(f"file '{p.resolve()}'\n" for p in einheitlich))
    final = OUT / "wochenvideo.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", "-movflags", "+faststart", final)
    try:
        cover_erstellen(einheitlich[2], f"Top {len(top)} Fakten der Woche", OUT / "cover.jpg", zeitpunkt=1.0)
    except Exception as e:
        log(f"Cover: {e}")

    kap = "\n".join(f"{int(t // 60)}:{int(t % 60):02d} {name}" for t, name in kapitel)
    daten = {"titel": f"Die {len(top)} krassesten Fakten der Woche (KW {kw}) | Faktenfieber",
             "beschreibung": f"Die besten Fakten und Geschichten dieser Woche in einem Video!\n\n{kap}",
             "quellen": ["Quellen in den einzelnen Shorts auf unserem Kanal"],
             "hashtags": ["#fakten", "#unnützeswissen", "#faktenfieber", "#top5", "#geschichte"], "_lang": True}
    try:
        vid = youtube_hochladen(final, daten)
        privat = os.environ.get("YT_SICHTBARKEIT", "private") != "public"
        telegram_text(f"🎬 WOCHEN-VIDEO ist auf YouTube{' (privat – in der Studio-App öffentlich stellen)' if privat else ''}:\n"
                      f"https://youtube.com/watch?v={vid}\n\n{kap}\n\n"
                      f"⏱ Länge: {int(zeit // 60)}:{int(zeit % 60):02d} Min. – "
                      + ("zählt als normales Video (Wiedergabezeit für die Monetarisierung)" if zeit >= 185 else
                         "unter 3 Min., zählt deshalb noch als Short"))
    except Exception as e:
        telegram_text(f"❌ Wochen-Video konnte nicht hochgeladen werden: {e}")


def befehl_ausfuehren(st, befehl):
    if befehl == "voice":
        wahl = os.environ.get("AKTION_SLOT", "").strip()
        aktuell = st.get("stimme") or TTS_STIMME
        if not wahl or wahl == "-":
            liste = "\n".join(f"{'👉 ' if k == aktuell else '• '}{k} – {v}" for k, v in STIMMEN.items())
            telegram_text(f"🎙 STIMMEN (aktuell: {aktuell})\n\n{liste}\n\n"
                          f"Probe hören: /voice test  (vergleicht {', '.join(EMPFOHLEN)})\n"
                          f"oder: /voice test Orus\nWechseln: /voice Orus")
            return
        if wahl.lower().startswith("eleven:"):
            st["stimme"] = wahl
            telegram_text(f"🎙 Stimme auf ElevenLabs ({wahl.split(':', 1)[1]}) gestellt. Gilt ab dem nächsten Video.\n"
                          + ("" if ELEVEN_KEY else "⚠️ Dafür braucht GitHub das Secret ELEVENLABS_API_KEY (Starter-Abo wegen kommerzieller Lizenz)."))
            return
        name = next((k for k in STIMMEN if k.lower() == wahl.lower()), None)
        if not name:
            telegram_text(f"🤔 Die Stimme „{wahl}“ kenne ich nicht. Alle Stimmen: /voice")
            return
        st["stimme"] = name
        telegram_text(f"🎙 Stimme gewechselt: {aktuell} → {name} ({STIMMEN[name]}). Gilt ab dem nächsten Video.")
        return
    if befehl == "musik":
        wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
        alt = float(st.get("musik_lautstaerke", MUSIK_LAUTSTAERKE))
        neu = {"leiser": alt * 0.7, "lauter": alt * 1.35, "aus": 0.0, "an": MUSIK_LAUTSTAERKE, "normal": MUSIK_LAUTSTAERKE}.get(wahl)
        if neu is None:
            stufe = "aus" if alt == 0 else f"{round(alt / MUSIK_LAUTSTAERKE * 100)} %"
            telegram_text(f"🎵 Musik-Lautstärke: {stufe} (100 % = Standard)\n/musik leiser · /musik lauter · /musik aus · /musik normal")
            return
        st["musik_lautstaerke"] = round(min(neu, 0.35), 3)
        stufe = "aus" if neu == 0 else f"{round(st['musik_lautstaerke'] / MUSIK_LAUTSTAERKE * 100)} %"
        telegram_text(f"🎵 Musik ist jetzt: {stufe}. Gilt ab dem nächsten Video.")
        return
    if befehl == "einstellungen":
        telegram_text("⚙️ DEINE EINSTELLUNGEN\n\n"
                      f"🤖 Auto-Modus: {'AN (postet ohne Freigabe)' if st.get('auto') else 'AUS (Freigabe nötig)'}\n"
                      f"⏸ Pause: {'AN' if st.get('pause') else 'aus'}\n"
                      f"🎙 Stimme: {st.get('stimme') or TTS_STIMME}\n"
                      f"🎵 Musik: {'aus' if float(st.get('musik_lautstaerke', MUSIK_LAUTSTAERKE)) == 0 else str(round(float(st.get('musik_lautstaerke', MUSIK_LAUTSTAERKE)) / MUSIK_LAUTSTAERKE * 100)) + ' %'}\n"
                      f"🧠 Schwierigkeit: {st.get('max_schwierigkeit', MAX_SCHWIERIGKEIT)}\n"
                      f"📺 Themen-Tage: {'an' if st.get('serie') else 'aus'}\n"
                      f"💬 Kommentar-Vorschläge: {'an' if st.get('kommentare_an', True) else 'aus'}\n"
                      f"📲 Auto-Story: {'an' if st.get('story', True) else 'aus'}\n"
                      f"🎨 KI-Illustrationen: {'an' if st.get('ki_bilder', True) else 'aus'}\n"
                      f"📊 Dashboard: {'an' if st.get('dashboard') else 'aus'}\n"
                      f"🗓 Termine: {', '.join(f'{h:02d}:{m:02d}' for h, m in SLOTS)}\n"
                      f"▶️ YouTube: {'öffentlich' if os.environ.get('YT_SICHTBARKEIT') == 'public' else 'privat (Audit offen)'}\n\n"
                      "Ändern: /auto · /pause · /voice · /musik · /schwierigkeit · /serie · /kommentare · /kibilder · /dashboard")
        return
    if befehl == "auto_an":
        st["auto"] = True
        telegram_text("🤖 AUTO-MODUS AN\nAb jetzt werden alle Videos automatisch zu ihrer Uhrzeit gepostet – ohne Freigabe.\n"
                      "Du bekommst trotzdem jedes Video vorher zu sehen und kannst mit ⛔ stoppen oder mit ❌ neu machen.\n"
                      "Notbremse für alles: /stopp · Zurück zur Freigabe: /auto aus")
        return
    if befehl == "auto_aus":
        st["auto"] = False
        telegram_text("✋ AUTO-MODUS AUS\nAb jetzt musst du jedes Video wieder mit ✅ freigeben. "
                      "Bereits automatisch freigegebene Videos gehen noch raus (mit ⛔ stoppbar).")
        return
    if befehl == "kibilder":
        wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
        if wahl in ("an", "aus"):
            st["ki_bilder"] = wahl == "an"
        telegram_text(f"🎨 KI-Illustrationen sind {'AN' if st.get('ki_bilder', True) else 'AUS'}. Sie springen nur ein, "
                      f"wenn es keine echten Bilder/Clips gibt, und sind immer als „KI-Illustration“ gekennzeichnet. "
                      f"Funktioniert nur mit Gemini-Abrechnung (sonst wird es automatisch übersprungen).")
        return
    if befehl == "admin_pin":
        wert = os.environ.get("AKTION_SLOT", "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", wert):
            neu_gesetzt = not st.get("admin_pin")
            st["admin_pin"] = wert                 # nur ein Hash (mit Geheimschluessel), nie die PIN selbst
            st.setdefault("admin_version", 1)
            telegram_text("🔐 Admin-PIN " + ("festgelegt" if neu_gesetzt else "geändert") + ". Falls du das nicht warst: /admin neu")
        return
    if befehl == "admin_reset":
        st.pop("admin_pin", None)
        st["admin_version"] = int(st.get("admin_version", 1)) + 1
        telegram_text("🔐 Admin-Zugang zurückgesetzt: alte PIN und alle Anmeldungen sind ungültig. "
                      "Den neuen Einrichtungs-Link hast du gerade bekommen.")
        return
    if befehl == "story":
        wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
        if wahl in ("an", "aus"):
            st["story"] = wahl == "an"
        telegram_text(f"📲 Auto-Story ist {'AN' if st.get('story', True) else 'AUS'}: Jedes Reel wird zusätzlich als "
                      f"Story geteilt, damit es alle Follower nochmal sehen. (/story an · /story aus)\n"
                      f"ℹ️ Eigene Reels liken oder reposten erlaubt Instagram über die Schnittstelle nicht – "
                      f"die Story ist der offizielle Weg dafür.")
        return
    if befehl == "kommentare":
        wahl = os.environ.get("AKTION_SLOT", "").strip().lower()
        if wahl in ("an", "aus"):
            st["kommentare_an"] = wahl == "an"
        telegram_text(f"💬 Kommentar-Vorschläge sind {'AN' if st.get('kommentare_an', True) else 'AUS'}. "
                      f"(/kommentare an · /kommentare aus)")
        return
    if befehl == "notstopp":
        gestoppt = []
        for slot, e in st.get("slots", {}).items():
            if e.get("status") in ("wartet", "freigegeben"):
                e["status"] = "gestoppt"
                gestoppt.append(slot)
                tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=e.get("message_id"),
                   reply_markup=json.dumps({"inline_keyboard": [
                       [{"text": "⛔ Gestoppt (Notbremse)", "callback_data": "x"}],
                       [{"text": "🚀 Doch posten", "callback_data": f"jetzt|{slot}"}]]}))
        telegram_text(f"🛑 NOTBREMSE: {len(gestoppt)} geplante(s) Video(s) gestoppt"
                      + (f" ({', '.join(x.split(' ')[1] for x in gestoppt)})" if gestoppt else "")
                      + ". Die Automatik läuft weiter – für eine Pause zusätzlich /pause.")
        return
    if befehl == "idee":
        text = os.environ.get("AKTION_SLOT", "").strip()[:80]
        d = ideen_laden()
        if text and text != "-":
            d["warteschlange"].append(text)
            ideen_speichern(d)
            platz = len(d["warteschlange"])
            telegram_text(f"💡 „{text}“ ist vorgemerkt (Platz {platz}). Kommt "
                          + ("beim nächsten automatischen Video." if platz == 1 else f"in {platz} automatischen Videos.")
                          + "\nSofort statt später? Dann /thema " + text)
        return
    if befehl == "ideen_leeren":
        d = ideen_laden()
        d["warteschlange"] = []
        ideen_speichern(d)
        telegram_text("🧹 Deine Ideen-Liste ist geleert.")
        return
    if befehl == "ideen":
        d = ideen_laden()
        schlange = "\n".join(f"{i+1}. {t}" for i, t in enumerate(d["warteschlange"])) or "(leer – mit /idee Thema hinzufügen)"
        vorrat = "\n".join(f"• {t}" for t in list(d["backlog"])[:10]) or "(wird beim nächsten Video gefüllt)"
        telegram_text(f"💡 DEINE IDEEN-LISTE (kommt als Nächstes dran)\n{schlange}\n\n"
                      f"🧠 IDEEN-VORRAT DER KI ({len(d['backlog'])} Stück, Auszug)\n{vorrat}\n\n"
                      f"/idee Thema – vormerken · /ideen leeren – Liste löschen")
        return
    if befehl == "ziel":
        ziel_senden()
        return
    if befehl == "dashboard":
        dashboard_befehl(st)
        return
    if befehl == "serie":
        serie_befehl(st)
        return
    if befehl in ("stats", "wochenbericht"):
        statistik_senden(st, wochenbericht=befehl == "wochenbericht")
        return
    if befehl == "schwierigkeit":
        roh = os.environ.get("AKTION_SLOT", "").replace(",", ".").strip()
        aktuell = st.get("max_schwierigkeit", MAX_SCHWIERIGKEIT)
        try:
            neu = float(roh)
        except ValueError:
            telegram_text(f"🧠 Aktuell eingestellt: {aktuell}. Ändern mit z. B. /schwierigkeit 5")
            return
        neu = round(max(SCHWIERIGKEIT_MIN, min(SCHWIERIGKEIT_MAX, neu)), 1)
        st["max_schwierigkeit"] = neu
        hinweis = (" Achtung: Bei so niedrigen Werten werden mehr Themen aussortiert, dann kann ein Video "
                   "auch mal länger dauern." if neu <= 4.5 else "")
        telegram_text(f"✅ Schwierigkeits-Grenze von {aktuell} auf {neu} geändert. Gilt ab dem nächsten Video.{hinweis}")
        return
    if befehl == "pause":
        st["pause"] = True
        telegram_text("⏸ Pause aktiviert. Es werden keine automatischen Videos mehr gebaut.\n"
                      "Bereits freigegebene Videos gehen trotzdem raus. /video funktioniert weiter.\n"
                      "Mit /weiter geht's wieder los.")
    elif befehl == "weiter":
        st["pause"] = False
        telegram_text("▶️ Pause beendet. Die Videos kommen wieder automatisch (gebaut 2,5 h vor 8, 12 und 18 Uhr).")
    elif befehl == "status":
        namen = {"wartet": "⏳ wartet auf dich", "freigegeben": "✅ freigegeben", "gepostet": "🎉 gepostet",
                 "verworfen": "❌ verworfen", "verfallen": "⌛ verfallen", "fehler": "⚠️ Fehler", "gestoppt": "⛔ gestoppt"}
        zeilen = []
        for slot, e in sorted(st.get("slots", {}).items())[-8:]:
            z = f"{slot}  {namen.get(e.get('status'), e.get('status'))}\n   „{e.get('titel', '–')}“"
            links = e.get("links") or {}
            if links.get("instagram"):
                z += f"\n   📸 {links['instagram']}"
            if links.get("youtube"):
                z += f"\n   ▶️ {links['youtube']}"
            zeilen.append(z)
        gepostet = sum(1 for e in st.get("slots", {}).values() if e.get("status") == "gepostet")
        ig = st.get("ig_token_erneuert", "noch nie")[:10]
        telegram_text("📊 FAKTENFIEBER STATUS\n\n"
                      + ("⏸ PAUSE ist aktiv (/weiter zum Fortsetzen)\n" if st.get("pause") else "▶️ Automatik läuft\n")
                      + ("🤖 Auto-Modus: AN (postet ohne Freigabe)\n\n" if st.get("auto") else "✋ Auto-Modus: AUS (Freigabe nötig)\n\n")
                      + "\n\n".join(zeilen or ["(noch keine Videos)"])
                      + f"\n\n🎬 Gepostet (letzte 7 Tage): {gepostet}"
                      + f"\n🧠 Schwierigkeits-Grenze: {st.get('max_schwierigkeit', MAX_SCHWIERIGKEIT)}"
                      + f"\n🔑 Instagram-Token zuletzt erneuert: {ig}"
                      + f"\n▶️ YouTube: {'öffentlich' if os.environ.get('YT_SICHTBARKEIT') == 'public' else 'privat (Audit offen)'}")


def aktion_verarbeiten(st, aktion, slot, cb_id=None, vom_tuersteher=False):
    """✅ / ❌ fuer einen Termin umsetzen."""
    antwort = (lambda text: tg("answerCallbackQuery", callback_query_id=cb_id, text=text)) if cb_id else (lambda text: None)
    eintrag = st["slots"].get(slot)
    if aktion in ("antw", "ign"):
        kommentar_antworten(st, slot, senden=aktion == "antw")
        return
    if aktion == "teil2":
        eintrag = st["slots"].get(slot) or {}
        thema = eintrag.get("thema") or eintrag.get("titel", "")
        if thema and _neu_starten("sofort", f"{thema} TEIL2"):
            if not vom_tuersteher:
                telegram_text(f"🔁 Baue Teil 2 zu „{thema}“ mit neuen Fakten. Kommt in ca. 10–15 Minuten.")
        else:
            telegram_text("⚠️ Konnte Teil 2 nicht starten.")
        return
    darf = {"jetzt": ("wartet", "freigegeben", "fehler", "gestoppt"), "stopp": ("wartet", "freigegeben"),
            "neu": ("wartet", "freigegeben", "gestoppt")}.get(aktion, ("wartet",))
    if not eintrag or eintrag.get("status") not in darf:
        antwort("Schon erledigt 👍")
        if vom_tuersteher and eintrag:
            telegram_text(f"ℹ️ Für {slot} ist schon alles erledigt (Status: {eintrag.get('status')}).")
        return
    uhr = slot.split(" ")[1]
    if aktion == "stopp":
        eintrag["status"] = "gestoppt"
        antwort("⛔ Wird nicht gepostet")
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
           reply_markup=json.dumps({"inline_keyboard": [
               [{"text": "⛔ Gestoppt – wird nicht gepostet", "callback_data": "x"}],
               [{"text": "🚀 Doch posten", "callback_data": f"jetzt|{slot}"},
                {"text": "❌ Neues Video", "callback_data": f"neu|{slot}"}]]}))
        log(f"{slot}: gestoppt")
        return
    if aktion == "jetzt":
        eintrag["status"] = "freigegeben"
        eintrag["sofort"] = True
        eintrag["fehler"] = 0
        antwort("🚀 Wird jetzt gepostet!")
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
           reply_markup=json.dumps({"inline_keyboard": [[{"text": "🚀 Wird gerade gepostet …", "callback_data": "x"}]]}))
        log(f"{slot}: sofort posten")
    elif aktion == "ok":
        eintrag["status"] = "freigegeben"
        spaet = jetzt() >= slot_zeit(slot) - timedelta(minutes=5)
        antwort("✅ Freigegeben – geht sofort raus" if spaet else f"✅ Freigegeben für {uhr} Uhr")
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
           reply_markup=json.dumps({"inline_keyboard": [
               [{"text": "✅ Freigegeben – geht jetzt raus" if spaet else f"✅ Freigegeben – geht um {uhr} Uhr raus",
                 "callback_data": "x"}],
               [{"text": "🚀 Doch jetzt sofort posten", "callback_data": f"jetzt|{slot}"}]] if not spaet else [
               [{"text": "✅ Freigegeben – geht jetzt raus", "callback_data": "x"}]]}))
        log(f"{slot}: freigegeben")
    elif aktion == "neu":
        if eintrag.get("neu_gemacht", 0) >= MAX_NEU:
            antwort("Maximal 3x neu pro Termin 🙈")
            telegram_text(f"🙈 Für {uhr} Uhr wurde schon {MAX_NEU}x neu gemacht. Bitte dieses Video freigeben oder verfallen lassen.")
            return
        eintrag["status"] = "verworfen"
        eintrag["neu_gemacht"] = eintrag.get("neu_gemacht", 0) + 1
        eintrag["verworfen"] = (eintrag.get("verworfen") or []) + [f"{eintrag.get('titel', '')} ({eintrag.get('thema', '')})"]
        antwort("❌ Verworfen – ich mache ein neues!")
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=eintrag.get("message_id"),
           reply_markup=json.dumps({"inline_keyboard": [[{"text": "❌ Verworfen", "callback_data": "x"}]]}))
        status_speichern(st)                      # damit der neue Lauf den Status sieht
        if _neu_starten(slot, eintrag.get("wunsch") or ""):
            if not vom_tuersteher:                # der Tuersteher hat das schon bestaetigt
                telegram_text(f"🔄 Baue ein neues Video für {uhr} Uhr. Kommt in ca. 10–15 Minuten.")
        else:
            telegram_text("⚠️ Konnte das neue Video nicht automatisch starten. Bitte in GitHub "
                          "unter Actions → Erstellen → Run workflow starten.")
        log(f"{slot}: verworfen, neues Video angefordert")


def selbstheilung(st):
    """Fehlt kurz vor einem Termin das Video (z. B. weil ein Lauf abgestuerzt ist), wird es automatisch nachgebaut."""
    if st.get("pause"):
        return
    slot = naechster_slot(ab=jetzt() - timedelta(minutes=25))
    zeit = slot_zeit(slot)
    noch = zeit - jetzt()
    if not (timedelta(minutes=20) < noch < timedelta(hours=2, minutes=15)):
        return
    if st["slots"].get(slot, {}).get("status") in ("wartet", "freigegeben", "gepostet"):
        return
    versuche = st.setdefault("nachgebaut", {})
    if versuche.get(slot, 0) >= 1:
        return
    if erstellen_laeuft():
        log("Selbstheilung: Erstellen laeuft gerade noch – alles gut, nichts zu tun.")
        return
    versuche[slot] = versuche.get(slot, 0) + 1
    if _neu_starten(slot):
        telegram_text(f"🩹 Für {slot.split(' ')[1]} Uhr war noch kein Video da (der Lauf ist wohl hängengeblieben). "
                      f"Ich baue es jetzt automatisch nach – kommt in ca. 15 Minuten.")
        log(f"Selbstheilung: Video fuer {slot} nachgebaut")


def erstellen_laeuft():
    """Laeuft oder wartet gerade ein Erstellen-Lauf bei GitHub?"""
    if not (GH_TOKEN and GH_REPO):
        return False
    try:
        for status in ("in_progress", "queued"):
            r = requests.get(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/erstellen.yml/runs",
                             params={"status": status, "per_page": 1}, timeout=20,
                             headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
            if r.json().get("total_count", 0) > 0:
                return True
    except Exception:
        pass
    return False


def token_warnung(st):
    """Warnt rechtzeitig, bevor der GitHub-Zugriffstoken (GH_PAT) ablaeuft."""
    if not GH_PAT or st.get("token_check") == jetzt().strftime("%Y-%m-%d"):
        return
    st["token_check"] = jetzt().strftime("%Y-%m-%d")
    r = requests.get("https://api.github.com/user", timeout=20, headers={"Authorization": f"Bearer {GH_PAT}"})
    ablauf = r.headers.get("github-authentication-token-expiration", "")
    if r.status_code == 401:
        telegram_text("🔑 Achtung: Der GitHub-Token (GH_PAT) funktioniert nicht mehr (abgelaufen oder gelöscht). "
                      "Instagram-Token-Verlängerung und GitHub-Pages-Upload laufen dann nicht. Bitte neuen Token anlegen.")
        return
    if ablauf:
        try:
            tage = (datetime.strptime(ablauf[:19], "%Y-%m-%d %H:%M:%S") - datetime.utcnow()).days
        except ValueError:
            return
        if tage <= 14:
            telegram_text(f"🔑 Erinnerung: Dein GitHub-Token (GH_PAT) läuft in {tage} Tagen ab ({ablauf[:10]}). "
                          f"Rechtzeitig unter github.com/settings/personal-access-tokens verlängern bzw. neu anlegen "
                          f"und das Secret GH_PAT ersetzen. Gleiches gilt für den Token beim Cloudflare-Türsteher.")


def instagram_token_pflegen(st):
    letzte = st.get("ig_token_erneuert")
    if letzte and jetzt() - datetime.fromisoformat(letzte) < timedelta(days=20):
        return
    if not (IG_TOKEN and GH_PAT and GH_REPO):
        return
    r = requests.get(f"{IG_API}/refresh_access_token", timeout=30,
                     params={"grant_type": "ig_refresh_token", "access_token": IG_TOKEN})
    neu = r.json().get("access_token")
    if not neu:
        telegram_text(f"⚠️ Instagram-Token konnte nicht verlängert werden: {r.text[:200]}")
        st["ig_token_erneuert"] = jetzt().isoformat()      # nicht jede Viertelstunde nerven
        return
    from base64 import b64encode
    from nacl import encoding, public
    kopf = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    k = requests.get(f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key", headers=kopf, timeout=30).json()
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    wert = b64encode(box.encrypt(neu.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{GH_REPO}/actions/secrets/IG_ACCESS_TOKEN", headers=kopf,
                     json={"encrypted_value": wert, "key_id": k["key_id"]}, timeout=30)
    if r.status_code in (201, 204):
        st["ig_token_erneuert"] = jetzt().isoformat()
        log("Instagram-Token verlängert ✅")
        cloudflare_token_sync(st, neu)
    else:
        telegram_text(f"⚠️ Neuer Instagram-Token konnte nicht gespeichert werden ({r.status_code}). "
                      f"Bitte GH_PAT prüfen (braucht 'Secrets: Read and write').")


CF_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_KONTO = os.environ.get("CF_ACCOUNT_ID", "")
CF_WORKER = os.environ.get("CF_WORKER", "faktenfieber-bot")


def cloudflare_token_sync(st, token=None):
    """Gibt dem Tuersteher (Cloudflare) immer den aktuellen Instagram-Token, damit das Live-Dashboard
    Instagram-Zahlen minuetlich selbst abfragen kann. Braucht die Secrets CF_API_TOKEN + CF_ACCOUNT_ID."""
    token = token or IG_TOKEN
    if not (CF_TOKEN and CF_KONTO and token):
        return
    import hashlib
    fingerabdruck = hashlib.sha256(token.encode()).hexdigest()[:12]
    if st.get("cf_ig_sync") == fingerabdruck:
        return
    r = requests.put(f"https://api.cloudflare.com/client/v4/accounts/{CF_KONTO}/workers/scripts/{CF_WORKER}/secrets",
                     headers={"Authorization": f"Bearer {CF_TOKEN}"}, timeout=30,
                     json={"name": "IG_ACCESS_TOKEN", "text": token, "type": "secret_text"})
    if r.status_code == 200 and r.json().get("success"):
        st["cf_ig_sync"] = fingerabdruck
        log("Instagram-Token an den Tuersteher (Cloudflare) uebergeben ✅")
    elif not st.get("cf_sync_fehler_gemeldet"):
        st["cf_sync_fehler_gemeldet"] = True
        telegram_text(f"⚠️ Konnte den Instagram-Token nicht an Cloudflare übergeben ({r.status_code}). "
                      f"Das Live-Dashboard nutzt dann die stündlichen Instagram-Zahlen. Details: {r.text[:200]}")


def telegram_text(text):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text[:4000]}, timeout=30)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    try:
        modus = sys.argv[1] if len(sys.argv) > 1 else "test"
        {"test": main, "erstellen": modus_erstellen, "pruefen": modus_pruefen, "noetig": modus_noetig,
         "status": lambda: befehl_ausfuehren(status_laden(), os.environ.get("AKTION") or "status"),
         "hoerprobe": modus_hoerprobe, "wochenvideo": modus_wochenvideo}[modus]()
    except SystemExit as e:
        if e.code not in (0, None):
            telegram_text(f"❌ Faktenfieber: Kein Video erstellt.\n{e.code}")
        raise
    except Exception as e:
        telegram_text(f"❌ Faktenfieber: Fehler im Lauf.\n{e.__class__.__name__}: {e}")
        raise
