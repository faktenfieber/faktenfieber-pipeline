#!/usr/bin/env python3
"""
FAKTENFIEBER – Pipeline v2.0
Wikipedia-Recherche -> Skript -> Faktencheck gegen Quelltext -> Voiceover -> Clips -> Schnitt -> Telegram

KI-Anbieter (alle gratis, mindestens einer noetig – werden der Reihe nach probiert):
  MISTRAL_API_KEY, GROQ_API_KEY, GEMINI_API_KEY
"""
import difflib
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import wave
from pathlib import Path

import requests

# ---------------------------------------------------------------- Einstellungen
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
WORK = ROOT / "work"
USED_FILE = ROOT / "data" / "used_topics.json"

W, H, FPS = 1080, 1920, 30
MAX_DAUER = 58.0                       # Sekunden, Shorts bleiben unter 60
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
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=90, json=body)
    if r.status_code == 400 and "json_validate" in r.text:
        body.pop("response_format")          # manche Modelle mögen den JSON-Modus nicht
        r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=90, json=body)
    if r.status_code != 200:
        return r.status_code, r.text[:700], None
    return 200, "", r.json()["choices"][0]["message"]["content"]


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
    """Reihenfolge: Mistral (bestes Deutsch) -> Groq (schnell) -> Gemini."""
    liste = []
    if MISTRAL_KEY:
        url = "https://api.mistral.ai/v1/chat/completions"
        for m in ("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"):
            liste.append(("Mistral", m, lambda m_, p, t, u=url: _openai_kompatibel(u, MISTRAL_KEY, m_, p, t)))
    if GROQ_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        for m in _groq_modelle():
            liste.append(("Groq", m, lambda m_, p, t, u=url: _openai_kompatibel(u, GROQ_KEY, m_, p, t)))
    if GEMINI_KEY:
        for m in _gemini_modelle():
            liste.append(("Gemini", m, _gemini))
    if not liste:
        sys.exit("Kein KI-Key gefunden! Mindestens MISTRAL_API_KEY, GROQ_API_KEY oder GEMINI_API_KEY setzen.")
    log("KI-Anbieter: " + ", ".join(f"{a}/{m}" for a, m, _ in liste))
    return liste


def wartezeit(info):
    """Liest 'try again in 13.3s' (Groq) bzw. 'retryDelay': '13s' (Gemini) aus der Fehlermeldung."""
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", info or "") or \
        re.search(r'retryDelay"?\s*:\s*"?([\d.]+)s', info or "")
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
            log(f"KI-Problem -> {fehler}")
            warten = wartezeit(info)
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
    "Mittelalter Alltag", "Antike Rom", "Altes Ägypten", "Wikinger", "Ritter", "Steinzeit", "Pest",
    "Brauch", "Aberglaube", "Hinrichtung", "Gefängnis Geschichte", "Erfindung", "Patent", "Kuriosität",
    "Hochstapler", "Fälschung", "Schwindel", "Unglück", "Katastrophe", "Explosion", "Schiffbruch",
    "Expedition", "Entdeckung", "Medizin Geschichte", "Hygiene", "Mode Geschichte", "Essen Geschichte",
    "Bier Geschichte", "Geld Geschichte", "Post Geschichte", "Eisenbahn Geschichte", "Krieg Kuriosität",
    "Spionage", "Schmuggel", "Piraterie", "Raub", "Kriminalfall", "Gesetz", "Kaiser", "König", "Papst",
    "Industrialisierung", "19. Jahrhundert", "Barock", "Renaissance", "Zirkus", "Jahrmarkt", "Spiel Geschichte",
    "Tier Rekord", "Naturphänomen", "Rekord", "Weltausstellung", "Kunstraub", "Ballon", "Zeppelin",
]
KATEGORIE_SUCHE = "incategory:Wikipedia:Exzellent|Wikipedia:Lesenswert"


def wiki_kandidaten(benutzt, anzahl=10):
    titel = set()
    # ~3/4 gezielt aus Geschichte & unnuetzem Wissen, Rest zufaellig quer durch alles
    for begriff in random.sample(THEMEN_SUCHE, 5):
        try:
            daten = wiki(list="search", srsearch=f"{KATEGORIE_SUCHE} {begriff}", srlimit=20, srnamespace=0)
            treffer = [t["title"] for t in daten.get("query", {}).get("search", []) if t["title"] not in benutzt]
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
                      if m["title"] not in benutzt]
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


def thema_waehlen(benutzt):
    kandidaten = wiki_kandidaten(benutzt)
    liste = "\n\n".join(f"### {t}\n{intro}" for t, intro in kandidaten.items())
    prompt = f"""Du bist Redakteur des deutschen Shorts-Kanals FAKTENFIEBER (verrueckte, wahre Fakten und Geschichten).
Hier sind Wikipedia-Artikel. Waehle den EINEN, aus dem sich das spannendste 45-Sekunden-Video
fuer ein breites, junges Publikum machen laesst (ueberraschend, kurios, "das glaubt mir keiner").
SCHWERPUNKT DES KANALS: Geschichte und wie die Menschen frueher gelebt haben (Alltag, Braeuche, Berufe,
Strafen, Medizin, Essen, Aberglaube), kuriose historische Ereignisse, verrueckte Erfindungen und
"unnuetzes Wissen", das man sofort weitererzaehlen will. Daneben auch erstaunliche Fakten aus Natur und Wissenschaft.
Bevorzuge ECHTE Ereignisse, Phaenomene, Orte, Erfindungen, Kriminalfaelle, Katastrophen, Kuriositaeten.
Meiden: Romane, Filme, Alben, Fiktion, Verwaltung, Listen, Kirchen/Gebaeude ohne Story, Biografien ohne Twist.
"spannung" ehrlich bewerten: 9-10 nur, wenn ein konkreter Fakt wirklich verblueffend ist.
"verstaendlich" bewerten: Versteht ein 14-Jaehriger ohne Vorwissen das Thema in 40 Sekunden? (1-10)
Waehle nur Themen mit verstaendlich >= 7. Meide Themen, die viel Fachwissen brauchen
(komplizierte Chemie, Mathematik, Politik-Details, Rechtsfragen, Theologie, Sprachwissenschaft).

{liste}

Antworte NUR mit JSON: {{"titel": "exakter Titel aus der Liste", "warum": "1 Satz", "spannung": 1-10, "verstaendlich": 1-10}}"""
    wahl, _ = ki(prompt, temperatur=0.4)
    titel = wahl.get("titel", "")
    if titel not in kandidaten:
        titel = difflib.get_close_matches(titel, list(kandidaten), n=1, cutoff=0) or list(kandidaten)[:1]
        titel = titel[0]
    log(f"Gewaehlt: {titel} (Spannung {wahl.get('spannung')}/10, verstaendlich {wahl.get('verstaendlich')}/10) – {wahl.get('warum', '')}")
    return titel


def skript_erstellen(titel):
    titel, artikel, url = wiki_artikel(titel)
    bilder = wiki_bilder(titel)
    bildliste = "\n".join(f"{i}: {b['beschreibung']}" for i, b in enumerate(bilder)) or "(keine Bilder vorhanden)"
    prompt = f"""Du bist Autor des deutschen Faceless-Kanals FAKTENFIEBER (YouTube Shorts + Instagram Reels).
Schreibe ein Short ueber den folgenden Wikipedia-Artikel.

ABSOLUTE REGEL: Verwende NUR Fakten, die WOERTLICH so im Artikeltext stehen. Nichts dazuerfinden,
nichts aus deinem eigenen Wissen ergaenzen, nicht uebertreiben, keine Zahlen runden oder aendern.

ARTIKEL "{titel}":
\"\"\"{artikel}\"\"\"

ECHTE BILDER ZUM THEMA (Nummer: Beschreibung):
{bildliste}

STIL:
- 95 bis 120 Woerter gesprochener Text (ca. 45-55 Sekunden bei ruhigem Erzaehltempo). Jedes Wort muss sitzen.
- Der allererste Satz beginnt IMMER mit "Wusstest du" und ist ein starker Hook mit dem ueberraschendsten Detail.
- GUT ERKLAEREN: Der Zuschauer weiss NICHTS ueber das Thema. Mach klar, WAS es ist, WO und WANN,
  und WARUM es so besonders oder verrueckt ist. Konkrete Zahlen und Details aus dem Artikel nennen.
  Roter Faden, jeder Satz baut auf dem vorherigen auf, nichts Unverstaendliches.
- KOMPLIZIERTES EINFACH MACHEN: Erklaere so, dass es ein 14-Jaehriger sofort versteht.
  Jeder Fachbegriff, jedes Fremdwort, jeder unbekannte Ort und jede Epoche bekommt sofort eine kurze,
  einfache Erklaerung ("ein Gasometer, also ein riesiger Speicher fuer Gas"). Lieber ein Detail weglassen
  als etwas unerklaert lassen. Bildhafte Vergleiche aus dem Alltag sind erlaubt ("so hoch wie ein
  20-stoeckiges Haus"), aber nur, wenn die Zahlen dahinter aus dem Artikel stammen.
  Ist das Thema eher kompliziert, darfst du bis zu 125 Woerter nutzen.
- Erklaere den Zusammenhang: Nicht nur WAS passiert ist, sondern auch WARUM und WIE. Der Zuschauer soll
  am Ende das Gefuehl haben, wirklich etwas verstanden zu haben.
- Aufbau: Hook -> Einordnung (was/wo/wann) -> Spannung aufbauen -> ueberraschendes Detail -> Pointe/Twist am Ende.
- Bei historischen Themen: Lass den Zuschauer spueren, wie anders das Leben damals war (Vergleich mit heute,
  aber nur mit Fakten aus dem Artikel). Ton: "unnuetzes Wissen", das man beim naechsten Treffen erzaehlen will.
- Kurze, lebendige Saetze, Du-Ansprache, locker aber nicht cringe. Keine Emojis, keine Klammern,
  keine Abkuerzungen, keine Sonderzeichen. Der Text wird von einer Computerstimme vorgelesen.
- 8 bis 12 Szenen, jede Szene genau EIN kurzer Satz (fuer schnelle Schnitte).
- VIRAL-REGELN:
  * Der erste Satz enthaelt SOFORT den krassesten Fakt. Kein Anlauf, keine Einleitung.
  * Offene Schleife: Im 2. oder 3. Satz andeuten, dass noch etwas Verrueckteres kommt
    (z. B. "Aber das ist noch nicht mal das Verrueckteste."), und es am Ende wirklich liefern.
  * Loop-Ende: Der LETZTE Satz ist so formuliert, dass er nahtlos in den ERSTEN Satz uebergeht,
    wenn das Video von vorne startet (z. B. endet mit "... und genau deshalb fragen sich heute noch viele:").
    KEIN "Folg mir", kein "Abonnieren", keine Verabschiedung im gesprochenen Text.
- BILDER: Es duerfen NUR Bilder gezeigt werden, die wirklich das Thema zeigen. Nichts Beliebiges!
  "bilder": Liste mit 1-2 Nummern ECHTER Bilder aus der Liste oben, die zu diesem Satz passen.
            Die echten Bilder sind IMMER die erste Wahl. Du darfst ein Bild in mehreren Szenen verwenden.
  "suchbegriff_en": englischer Stock-Video-Suchbegriff (2-4 Woerter), NUR wenn ein Stock-Video GENAU das
            zeigen kann, was im Satz passiert, inkl. Epoche/Ort (z. B. "medieval town street", "1930s steam train",
            "octopus swimming"). Wenn kein Stock-Video wirklich passt: null.
- Jede Faktenbehauptung einzeln auflisten, mit "beleg" = exaktes, woertliches Zitat (5-25 Woerter)
  aus dem Artikeltext, das die Behauptung belegt. Copy-Paste, kein Umformulieren!
- Titel, Beschreibung und Hashtags fuer YouTube/Instagram selbst erstellen.

Antworte NUR mit JSON:
{{
  "thema": "{titel}",
  "titel": "YouTube-Titel, max. 60 Zeichen, macht neugierig, keine Clickbait-Luege",
  "szenen": [{{"text": "...", "bilder": [0], "suchbegriff_en": "... oder null"}}],
  "behauptungen": [{{"aussage": "...", "beleg": "woertliches Zitat aus dem Artikel"}}],
  "beschreibung": "2-3 Saetze, erzaehlt neugierig weiter, endet mit Aufforderung zum Folgen",
  "hashtags": ["#fakten", "#wusstestdu", "..."],
  "musik_stimmung": "eins von: spannend, mysterioes, dramatisch, froehlich, episch, historisch, kurios, ruhig",
  "suchbegriff_thema": "englischer Stock-Video-Suchbegriff fuer das Thema allgemein (2-3 Woerter)",
  "hook_text": "Texteinblendung fuer die ersten Sekunden: max. 6 Woerter, der krasseste Fakt, wahr laut Artikel",
  "kommentar_frage": "kurze Frage an die Zuschauer fuer die Kommentare, passend zum Thema"
}}"""
    daten, modell = ki(prompt, temperatur=0.8)
    daten["thema"] = titel
    daten["_modell"] = modell
    daten["_artikel"] = artikel
    daten["quellen"] = [f"Wikipedia – {titel}: {url}"]
    daten["_bilder"] = bilder
    return daten


def _norm(t):
    t = t.lower().replace("„", '"').replace("“", '"').replace("”", '"').replace("–", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%-]", " ", t)).strip()


def beleg_gefunden(beleg, artikel):
    """Programmatischer Check: steht das Zitat wirklich im Artikel? (kleine Abweichungen ok)"""
    b, a = _norm(beleg), _norm(artikel)
    if len(b) < 15:
        return False
    if b in a:
        return True
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size >= 0.85 * len(b)


def faktencheck(daten):
    artikel = daten["_artikel"]
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
        log(f"Stufe 1: {len(fehlend)} Zitat(e) stehen NICHT im Artikel.")
        return False, {"pruefung": pruefung, "gesamt": "nicht_ok", "probleme": fehlend or ["Keine Behauptungen angegeben"]}

    # Stufe 2: Eine (moeglichst andere) KI vergleicht den Sprechtext mit dem Artikel
    sprechtext = " ".join(s["text"] for s in daten["szenen"])
    if daten.get("hook_text"):
        sprechtext = f"[Texteinblendung: {daten['hook_text']}] " + sprechtext
    autor = daten["_modell"]
    prompt = f"""Du bist strenger Faktenpruefer. Vergleiche den SPRECHTEXT eines Videos mit dem ARTIKEL.
Jede FAKTENAUSSAGE im Sprechtext muss durch den Artikel gedeckt sein.
schwere "faktisch" = erfundene oder falsche Fakten, falsche Zahlen/Daten/Namen, verdrehte Zusammenhaenge.
schwere "stil" = harmlose Wertungen, Meinungen, Uebergaenge, rhetorische Fragen, "Wusstest du", Spannungsaufbau,
einfache Erklaerungen von Begriffen und veranschaulichende Alltagsvergleiche (solange die Zahlen stimmen).
Melde nur echte Probleme, keine Kleinigkeiten.

ARTIKEL:
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
Jede Behauptung braucht als "beleg" ein EXAKTES, woertliches Zitat aus dem Artikel (Copy-Paste).
Der erste Satz beginnt weiter mit "Wusstest du". Gesamtlaenge 95 bis 125 Woerter. Felder "bilder", "suchbegriff_en", "hook_text" und "kommentar_frage" beibehalten.

GEFUNDENE FEHLER:
{chr(10).join('- ' + str(p) for p in probleme)}

ARTIKEL:
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
    return daten


# ---------------------------------------------------------------- Voiceover
# 1. Wahl: Gemini-TTS (natuerliche, betonte Stimme, gratis) – 2. Wahl: Piper (offline, robotischer)
TTS_STIMME = os.environ.get("TTS_STIMME", "Fenrir")      # Gemini-Stimmen: Fenrir, Puck, Charon, Orus, Kore, Zephyr ...
TTS_MODELLE = [m for m in (os.environ.get("TTS_MODEL", ""), "gemini-3.1-flash-tts-preview",
                           "gemini-2.5-flash-preview-tts") if m]
TTS_STIL = os.environ.get("TTS_STIL", (
    "Lies den folgenden deutschen Text wie ein packender Erzaehler eines erfolgreichen YouTube-Shorts-Kanals vor: "
    "ruhiges, deutliches Erzaehltempo (nicht hetzen!), lebendig und neugierig, natuerliche Betonung, "
    "kurze Pausen nach jedem Satz und eine kleine Spannungspause vor der Pointe. "
    "Lies nur den Text vor, sonst nichts:"))
STIMME_GENUTZT = "?"


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
MUSIK_LAUTSTAERKE = float(os.environ.get("MUSIK_LAUTSTAERKE", "0.20"))
STIMMUNG_SUCHE = {
    "spannend": ["suspense cinematic", "tension underscore", "documentary suspense"],
    "mysterioes": ["mystery ambient", "mysterious documentary", "dark ambient"],
    "dramatisch": ["dramatic cinematic", "emotional orchestral", "drama underscore"],
    "froehlich": ["upbeat happy", "cheerful acoustic", "fun background"],
    "episch": ["epic orchestral", "cinematic adventure", "inspiring epic"],
    "historisch": ["medieval", "baroque", "classical documentary", "historical cinematic"],
    "kurios": ["quirky", "playful pizzicato", "funny background"],
    "ruhig": ["calm ambient", "relaxing piano", "chill lofi"],
}
ENTSPANNT = ["calm ambient", "relaxing piano", "chill lofi", "soft acoustic", "ambient background"]
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
    params = {"q": q, "license_type": "commercial,modification", "page_size": 30}
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
            and ((t.get("duration") or 60000) >= 40000)]


def _nachweis(t):
    lizenz = "gemeinfrei (CC0)" if t["license"] in ("cc0", "pdm") else f"CC {t['license'].upper()} {t['license_version']}".strip()
    return f"„{t['title']}“ von {t['creator']} – {lizenz} – {t['landing']}".strip(" –")


ARCHIV_THEMEN = {
    "spannend": ["cinematic", "soundtrack", "suspense", "dark ambient"],
    "mysterioes": ["dark ambient", "ambient", "drone", "mystery"],
    "dramatisch": ["cinematic", "orchestral", "soundtrack", "dramatic"],
    "froehlich": ["acoustic", "happy", "folk", "ukulele"],
    "episch": ["orchestral", "epic", "cinematic", "soundtrack"],
    "historisch": ["classical", "baroque", "medieval", "piano"],
    "kurios": ["quirky", "jazz", "acoustic", "chiptune"],
    "ruhig": ["ambient", "piano", "chill", "lofi"],
}
ARCHIV_ENTSPANNT = ["ambient", "piano", "chill", "lofi", "relaxing"]
ARCHIV_LIZENZ = ('(licenseurl:*publicdomain/zero* OR licenseurl:*publicdomain/mark* OR '
                 'licenseurl:*licenses/by/* OR licenseurl:*licenses/by-sa/*)')


def _archive(themen):
    """Internet Archive (archive.org): riesige, zuverlaessige Sammlung freier Musik, ohne Key."""
    teil = " OR ".join(f'subject:"{t}"' for t in themen)
    try:
        r = requests.get("https://archive.org/advancedsearch.php", headers=WIKI_HEADERS, timeout=40, params={
            "q": f"mediatype:audio AND {ARCHIV_LIZENZ} AND ({teil}) AND NOT subject:podcast",
            "fl[]": ["identifier", "title", "creator", "licenseurl", "subject"],
            "rows": 60, "sort[]": "downloads desc", "output": "json"})
        if r.status_code != 200:
            log(f"Archive.org antwortet {r.status_code}")
            return []
        docs = r.json().get("response", {}).get("docs", [])
    except Exception as e:
        log(f"Archive.org nicht erreichbar: {e.__class__.__name__}")
        return []
    out = []
    for d in docs:
        lic = str(d.get("licenseurl", ""))
        art = "cc0" if "publicdomain" in lic else ("by-sa" if "by-sa" in lic else "by")
        version = (re.search(r"/(\d\.\d)", lic) or [None, ""])[1]
        subj = d.get("subject", "")
        out.append({"id": f"ia_{d['identifier']}", "archive_id": d["identifier"], "url": None,
                    "title": d.get("title") or d["identifier"], "creator": (d.get("creator") or "unbekannt")
                    if isinstance(d.get("creator"), str) else ", ".join(d.get("creator", [])[:2]),
                    "license": art, "license_version": version,
                    "landing": f"https://archive.org/details/{d['identifier']}",
                    "tags": ", ".join(subj[:6]) if isinstance(subj, list) else str(subj)[:80], "dauer": 0})
    log(f"Archive.org ({', '.join(themen[:2])}…): {len(out)} Alben gefunden")
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
    index = _lade_json(MUSIK_CACHE / "index.json") or {}
    index[ziel.name] = {"nachweis": _nachweis(t), "stimmung": t.get("_stimmung", ""), "tags": t.get("tags", "")}
    for alt in list(index)[:-MUSIK_CACHE_MAX]:
        (MUSIK_CACHE / alt).unlink(missing_ok=True)
        index.pop(alt, None)
    (MUSIK_CACHE / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return ziel


def musik_waehlen(daten):
    """Die KI waehlt aus frei lizenzierten Tracks den passendsten. Sonst entspannte Musik,
    sonst ein Track aus dem selbst gesammelten Archiv, und als allerletztes ein selbst erzeugter Ambient-Teppich."""
    global MUSIKNACHWEIS
    stimmung = str(daten.get("musik_stimmung", "spannend")).lower()
    if stimmung not in ARCHIV_THEMEN:
        stimmung = "spannend"
    zuletzt = set((_lade_json(USED_MUSIK) or [])[-12:])
    runden = [(STIMMUNG_SUCHE.get(stimmung, STIMMUNG_SUCHE["spannend"]), ARCHIV_THEMEN[stimmung], stimmung),
              (ENTSPANNT, ARCHIV_ENTSPANNT, "ruhig")]
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
Gewuenschte Stimmung: {label}. Die Musik laeuft LEISE unter einer Erzaehlstimme.
Waehle die 4 passendsten Tracks/Alben (Atmosphaere, Epoche, Tempo). Meide Gesang, Rap, Hoerspiele,
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
    archiv = [(n, i) for n, i in index.items() if (MUSIK_CACHE / n).exists()]
    if archiv:
        ruhig = [a for a in archiv if a[1].get("stimmung") == "ruhig"] or archiv
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


def audio_mischen(stimme, musik, gesamt, ziel):
    """Stimme + leise Musik, die unter der Stimme automatisch noch leiser wird (Ducking)."""
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
            f"volume={MUSIK_LAUTSTAERKE},afade=t=in:d=0.2,afade=t=out:st={max(0, gesamt - 0.4):.2f}:d=0.4[m];"
            f"[m][sc]sidechaincompress=threshold=0.04:ratio=5:attack=20:release=400[musik];"
            f"[stimme][musik]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
            f"alimiter=limit=0.95[out]")
    ffmpeg("-i", stimme, "-ss", f"{start:.1f}", "-stream_loop", "-1", "-i", musik,
           "-filter_complex", filt, "-map", "[out]", "-t", f"{gesamt:.2f}", "-ar", "44100", ziel)


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
    try:
        STIMME_GENUTZT = gemini_tts(volltext, roh)
    except Exception as e:
        log(f"⚠️ {e} -> nehme Ersatzstimme Piper")
        STIMME_GENUTZT = piper_tts(volltext, roh)
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
                              for b in genutzte_bilder]
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
    ffmpeg("-i", WORK / "bild.mp4", "-i", ton,
           "-vf", f"ass='{subs}',setsar=1,setdar=9/16", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", final)
    return final, ende_gesamt


# ---------------------------------------------------------------- Telegram
def telegram_senden(video, daten, check, dauer):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram nicht konfiguriert, ueberspringe.")
        return
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
    text = (f"📌 TITEL\n{daten['titel']}\n\n"
            f"🪝 HOOK-TEXT\n{daten.get('hook_text', '–')}\n\n"
            f"📝 BESCHREIBUNG\n{daten['beschreibung']}\n\n"
            f"Quelle: {daten['quellen'][0]}\n"
            + (("Bilder: " + "; ".join(daten.get("bildnachweise", [])) + "\n") if daten.get("bildnachweise") else "")
            + ((f"Musik: {daten['musiknachweis']}\n") if daten.get("musiknachweis") else "")
            + f"\n{' '.join(daten.get('hashtags', []))}\n\n"
            f"🔎 FAKTENCHECK (Prüfer: {check.get('pruefer', '?')})\n{pruef}\n\n"
            f"🤖 Geschrieben von: {daten.get('_modell')}\n🎙 Stimme: {STIMME_GENUTZT}")
    requests.post(f"{api}/sendMessage", data={"chat_id": TG_CHAT, "text": text[:4000],
                                              "disable_web_page_preview": "true"}, timeout=60)


# ---------------------------------------------------------------- YouTube-Upload
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")


def youtube_beschreibung(daten):
    teile = [daten.get("beschreibung", "")]
    if daten.get("kommentar_frage"):
        teile += ["", f"💬 {daten['kommentar_frage']} Schreib's in die Kommentare!"]
    teile += ["", f"📚 Quelle: {daten['quellen'][0]}"]
    if daten.get("bildnachweise"):
        teile += ["", "📸 Bildnachweise:"] + [f"• {b}" for b in daten["bildnachweise"]]
    if daten.get("musiknachweis"):
        teile += ["", f"🎵 Musik: {daten['musiknachweis']}"]
    teile += ["", " ".join(daten.get("hashtags", [])) + " #shorts"]
    return "\n".join(teile).replace("<", "").replace(">", "")[:4900]


def youtube_hochladen(video, daten, sichtbarkeit="private"):
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
    teile += ["", f"📚 Quelle: {daten['quellen'][0]}"]
    if daten.get("bildnachweise"):
        teile += ["📸 " + "; ".join(daten["bildnachweise"])]
    if daten.get("musiknachweis"):
        teile += [f"🎵 Musik: {daten['musiknachweis']}"]
    teile += ["", " ".join(hashtags)]
    return "\n".join(teile)[:2150]


def instagram_hochladen(video, daten):
    """Reel hochladen: Container anlegen -> Datei hochladen -> warten bis verarbeitet -> veroeffentlichen."""
    if not (IG_TOKEN and IG_USER):
        raise RuntimeError("Instagram-Secrets fehlen (IG_ACCESS_TOKEN, IG_USER_ID)")
    r = requests.post(f"{IG_API}/{IG_USER}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": instagram_caption(daten),
        "share_to_feed": "true", "access_token": IG_TOKEN})
    if r.status_code != 200 or "id" not in r.json():
        raise RuntimeError(f"Instagram-Container fehlgeschlagen: {r.status_code} {r.text[:400]}")
    container = r.json()["id"]
    upload_url = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{container}"
    groesse = Path(video).stat().st_size
    with open(video, "rb") as f:
        r = requests.post(upload_url, data=f, timeout=600, headers={
            "Authorization": f"OAuth {IG_TOKEN}", "offset": "0", "file_size": str(groesse)})
    if r.status_code != 200:
        raise RuntimeError(f"Instagram-Datei-Upload fehlgeschlagen: {r.status_code} {r.text[:400]}")
    for _ in range(40):                          # bis zu ~10 Minuten auf die Verarbeitung warten
        time.sleep(15)
        st = requests.get(f"{IG_API}/{container}", timeout=30,
                          params={"fields": "status_code,status", "access_token": IG_TOKEN}).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram-Verarbeitung fehlgeschlagen: {st}")
    else:
        raise RuntimeError("Instagram-Verarbeitung dauert zu lange")
    r = requests.post(f"{IG_API}/{IG_USER}/media_publish", timeout=60,
                      data={"creation_id": container, "access_token": IG_TOKEN})
    if r.status_code != 200:
        raise RuntimeError(f"Instagram-Veroeffentlichung fehlgeschlagen: {r.status_code} {r.text[:400]}")
    media = r.json()["id"]
    link = requests.get(f"{IG_API}/{media}", timeout=30,
                        params={"fields": "permalink", "access_token": IG_TOKEN}).json().get("permalink", "")
    log(f"Instagram-Upload ok: {link or media}")
    return link or media


# ---------------------------------------------------------------- Main
def main():
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
            erste = kandidat["szenen"][0]["text"]
            if not erste.lower().startswith("wusstest du"):
                kandidat["szenen"][0]["text"] = "Wusstest du schon? " + erste
            log(f"Skript fertig: {kandidat['titel']} – pruefe Fakten ...")
            ok, ergebnis = faktencheck(kandidat)
            for runde in range(1, 3):
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

    video, dauer = video_bauen(daten)
    meta = {k: v for k, v in daten.items() if k != "_artikel"}
    meta.update(faktencheck=check, dauer=round(dauer, 1))
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    speichere_thema(daten["thema"])
    telegram_senden(video, daten, check, dauer)
    if os.environ.get("YT_UPLOAD") == "1":
        try:
            vid = youtube_hochladen(video, daten)
            telegram_text(f"▶️ Auf YouTube hochgeladen (privat):\nhttps://youtube.com/shorts/{vid}\n\n"
                          "Solange das API-Audit läuft, bitte in der YouTube-Studio-App auf „Öffentlich“ stellen.")
        except Exception as e:
            log(f"YouTube-Fehler: {e}")
            telegram_text(f"❌ YouTube-Upload fehlgeschlagen:\n{e}")
    if os.environ.get("IG_UPLOAD") == "1":
        try:
            link = instagram_hochladen(video, daten)
            telegram_text(f"📸 Auf Instagram veröffentlicht:\n{link}")
        except Exception as e:
            log(f"Instagram-Fehler: {e}")
            telegram_text(f"❌ Instagram-Upload fehlgeschlagen:\n{e}")
    log(f"FERTIG: {video} ({dauer:.1f} s)")


def telegram_text(text):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text[:4000]}, timeout=30)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            telegram_text(f"❌ Faktenfieber: Kein Video erstellt.\n{e.code}")
        raise
    except Exception as e:
        telegram_text(f"❌ Faktenfieber: Fehler im Lauf.\n{e.__class__.__name__}: {e}")
        raise
