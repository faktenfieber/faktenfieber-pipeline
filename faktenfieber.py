#!/usr/bin/env python3
"""
FAKTENFIEBER – Pipeline v1.0
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


def wiki_kandidaten(benutzt, anzahl=7):
    titel = set()
    for _ in range(6):
        kat = random.choice(WIKI_KATEGORIEN)
        prefix = random.choice(string.ascii_uppercase)
        daten = wiki(list="categorymembers", cmtitle=kat, cmlimit=50, cmnamespace=0,
                     cmstartsortkeyprefix=prefix)
        mitglieder = [m["title"] for m in daten.get("query", {}).get("categorymembers", [])]
        random.shuffle(mitglieder)
        titel.update(t for t in mitglieder[:4] if t not in benutzt)
        if len(titel) >= anzahl:
            break
    if not titel:   # Notfall: zufaellige Artikel
        daten = wiki(list="random", rnnamespace=0, rnlimit=anzahl)
        titel = {m["title"] for m in daten["query"]["random"]}
    titel = list(titel)[:anzahl]
    intros = wiki(prop="extracts", exintro=1, explaintext=1, exlimit=20, titles="|".join(titel))
    return {p["title"]: p.get("extract", "")[:350] for p in intros["query"]["pages"] if p.get("extract")}


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


def wiki_bilder(titel, maximal=12):
    """Bilder aus dem Artikel (Wikimedia Commons) mit freier Lizenz + Urheberangabe."""
    try:
        daten = wiki(prop="images", titles=titel, imlimit=50, redirects=1)
        dateien = [i["title"] for i in daten["query"]["pages"][0].get("images", [])
                   if not BILD_AUSSCHLUSS.search(i["title"])]
        if not dateien:
            return []
        info = wiki(prop="imageinfo", titles="|".join(dateien[:40]), iiprop="url|size|mime|extmetadata",
                    iiurlwidth=1600, iiextmetadatalanguage="de")
    except Exception as e:
        log(f"Wiki-Bilder nicht ladbar: {e}")
        return []
    bilder = []
    for seite in info["query"]["pages"]:
        ii = (seite.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata", {})
        lizenz = meta.get("LicenseShortName", {}).get("value", "")
        if ii.get("mime") not in ("image/jpeg", "image/png") or min(ii.get("width", 0), ii.get("height", 0)) < 450:
            continue
        if not any(l in lizenz.lower() for l in FREIE_LIZENZEN):
            continue
        ohne_html = lambda t: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip()
        bilder.append({
            "datei": seite["title"],
            "url": ii.get("thumburl") or ii.get("url"),
            "beschreibung": ohne_html(meta.get("ImageDescription", {}).get("value", ""))[:160]
                            or seite["title"].split(":", 1)[-1].rsplit(".", 1)[0],
            "urheber": ohne_html(meta.get("Artist", {}).get("value", "")) or "unbekannt",
            "lizenz": lizenz,
        })
        if len(bilder) >= maximal:
            break
    log(f"{len(bilder)} passende Bilder aus dem Artikel gefunden")
    return bilder


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
Bevorzuge ECHTE Ereignisse, Phaenomene, Tiere, Orte, Erfindungen, Kriminalfaelle, Katastrophen, Kuriositaeten.
Meiden: Romane, Filme, Alben, Fiktion, Verwaltung, Listen, Kirchen/Gebaeude ohne Story, Biografien ohne Twist.
"spannung" ehrlich bewerten: 9-10 nur, wenn ein konkreter Fakt wirklich verblueffend ist.

{liste}

Antworte NUR mit JSON: {{"titel": "exakter Titel aus der Liste", "warum": "1 Satz", "spannung": 1-10}}"""
    wahl, _ = ki(prompt, temperatur=0.4)
    titel = wahl.get("titel", "")
    if titel not in kandidaten:
        titel = difflib.get_close_matches(titel, list(kandidaten), n=1, cutoff=0) or list(kandidaten)[:1]
        titel = titel[0]
    log(f"Gewaehlt: {titel} (Spannung {wahl.get('spannung')}/10) – {wahl.get('warum', '')}")
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

ECHTE BILDER AUS DEM ARTIKEL (Nummer: Beschreibung):
{bildliste}

STIL:
- 110 bis 140 Woerter gesprochener Text (ca. 45-55 Sekunden).
- Der allererste Satz beginnt IMMER mit "Wusstest du" und ist ein starker Hook mit dem ueberraschendsten Detail.
- GUT ERKLAEREN: Der Zuschauer weiss NICHTS ueber das Thema. Mach klar, WAS es ist, WO und WANN,
  und WARUM es so besonders oder verrueckt ist. Konkrete Zahlen und Details aus dem Artikel nennen.
  Roter Faden, jeder Satz baut auf dem vorherigen auf, nichts Unverstaendliches, keine Fachbegriffe ohne Erklaerung.
- Aufbau: Hook -> Einordnung (was/wo/wann) -> Spannung aufbauen -> ueberraschendes Detail -> Pointe/Twist am Ende.
- Kurze, lebendige Saetze, Du-Ansprache, locker aber nicht cringe. Keine Emojis, keine Klammern,
  keine Abkuerzungen, keine Sonderzeichen. Der Text wird von einer Computerstimme vorgelesen.
- 9 bis 13 Szenen, jede Szene genau EIN kurzer Satz (fuer schnelle Schnitte).
- Pro Szene das passende Bild:
  "bild": Nummer eines ECHTEN Bildes aus der Liste oben, wenn es genau zu diesem Satz passt (sonst null).
          Nutze die echten Bilder moeglichst oft, vor allem fuer das Hauptthema und den Hook.
  "suchbegriff_en": IMMER angeben: englischer Stock-Video-Suchbegriff (2-3 Woerter), der GENAU zeigt,
          worum es in diesem Satz geht (konkret, bildhaft, z. B. "gas tank explosion", "dragonfly closeup").
- Jede Faktenbehauptung einzeln auflisten, mit "beleg" = exaktes, woertliches Zitat (5-25 Woerter)
  aus dem Artikeltext, das die Behauptung belegt. Copy-Paste, kein Umformulieren!
- Titel, Beschreibung und Hashtags fuer YouTube/Instagram selbst erstellen.

Antworte NUR mit JSON:
{{
  "thema": "{titel}",
  "titel": "YouTube-Titel, max. 60 Zeichen, macht neugierig, keine Clickbait-Luege",
  "szenen": [{{"text": "...", "bild": null, "suchbegriff_en": "..."}}],
  "behauptungen": [{{"aussage": "...", "beleg": "woertliches Zitat aus dem Artikel"}}],
  "beschreibung": "2-3 Saetze, erzaehlt neugierig weiter, endet mit Aufforderung zum Folgen",
  "hashtags": ["#fakten", "#wusstestdu", "..."],
  "musik_stimmung": "eins von: spannend, mysterioes, dramatisch, froehlich, episch, ruhig, kurios"
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
    autor = daten["_modell"]
    prompt = f"""Du bist strenger Faktenpruefer. Vergleiche den SPRECHTEXT eines Videos mit dem ARTIKEL.
Jede FAKTENAUSSAGE im Sprechtext muss durch den Artikel gedeckt sein.
schwere "faktisch" = erfundene oder falsche Fakten, falsche Zahlen/Daten/Namen, verdrehte Zusammenhaenge.
schwere "stil" = harmlose Wertungen, Meinungen, Uebergaenge, rhetorische Fragen, "Wusstest du", Spannungsaufbau.
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
Der erste Satz beginnt weiter mit "Wusstest du". Gesamtlaenge 100 bis 140 Woerter. Felder "bild" beibehalten.

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
    "zuegiges Tempo, energiegeladen, neugierig, natuerliche Betonung, kleine Spannungspausen vor der Pointe. "
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


def untertitel_schreiben(eintraege, ziel):
    """eintraege = [(text, start, ende, ist_hook)]"""
    kopf = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1
Style: Hook,{FONT},100,&H0000E1FF,&H0000E1FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,8,4,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    zeilen = []
    for t, s, e, hook in eintraege:
        text = ass_escape(t.upper())
        basis = 100 if hook else 92
        groesse = schriftgroesse(text, basis)
        pop = (f"{{\\an5\\pos(540,1180)\\fs{groesse}\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)}}")
        zeilen.append(f"Dialogue: 0,{ass_zeit(s)},{ass_zeit(e)},{'Hook' if hook else 'Main'},,0,0,0,,{pop}{text}")
    Path(ziel).write_text(kopf + "\n".join(zeilen) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Clips (Pixabay)
def clips_suchen(begriff, dauer, schon_benutzt, anzahl=1):
    """Sucht bis zu `anzahl` verschiedene Pixabay-Videos zum Begriff."""
    woerter = begriff.split()
    versuche = [begriff, " ".join(woerter[:2]), " ".join(woerter[-2:]), woerter[-1], woerter[0]]
    gefunden = []
    for q in dict.fromkeys(versuche):
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": PIXABAY_KEY, "q": q, "per_page": 30, "safesearch": "true"}, timeout=30)
        if r.status_code != 200:
            log(f"Pixabay {r.status_code}: {r.text[:200]}")
            continue
        treffer = [h for h in r.json().get("hits", [])
                   if h["id"] not in schon_benutzt and h["id"] not in {g[0] for g in gefunden}]
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
    if not gefunden:
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": PIXABAY_KEY, "q": "abstract background", "per_page": 30}, timeout=30)
        gefunden = [(h["id"], h["videos"]["medium"]["url"]) for h in r.json().get("hits", [])
                    if h["id"] not in schon_benutzt][:anzahl]
    if not gefunden:
        raise RuntimeError(f"Kein Clip gefunden fuer '{begriff}'")
    return gefunden


def ffmpeg(*args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    subprocess.run(cmd, check=True)


def szene_rendern(clip, dauer, ziel):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          f"fps={FPS},eq=brightness=-0.06:saturation=1.1")
    ffmpeg("-stream_loop", "-1", "-i", clip, "-t", f"{dauer:.3f}", "-vf", vf, "-an",
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
    rein = nummer % 2 == 0          # abwechselnd rein- und rauszoomen
    zoom = f"min(1+0.12*on/{frames},1.12)" if rein else f"max(1.12-0.12*on/{frames},1)"
    ffmpeg("-i", komp, "-vf",
           f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
           f"setsar=1,format=yuv420p", "-frames:v", frames,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", ziel)


# ---------------------------------------------------------------- Musik + Soundeffekte
MUSIK_ORDNER = ROOT / "musik"                    # optional: eigene Tracks (z. B. von Pixabay Music) hier ablegen
USED_MUSIK = ROOT / "data" / "used_music.json"
MUSIK_LAUTSTAERKE = float(os.environ.get("MUSIK_LAUTSTAERKE", "0.20"))
SFX_LAUTSTAERKE = float(os.environ.get("SFX_LAUTSTAERKE", "0.35"))
STIMMUNG_SUCHE = {
    "spannend": ["suspense cinematic", "tension background", "thriller underscore"],
    "mysterioes": ["mystery ambient", "dark ambient", "mysterious"],
    "dramatisch": ["dramatic cinematic", "epic drama", "dark orchestral"],
    "froehlich": ["upbeat happy", "fun ukulele", "cheerful"],
    "episch": ["epic orchestral", "cinematic trailer", "inspiring epic"],
    "ruhig": ["calm piano", "ambient chill", "lofi"],
    "kurios": ["quirky funny", "playful pizzicato", "comedy background"],
}
MUSIKNACHWEIS = ""


def _lade_json(pfad):
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except Exception:
        return []


def musik_waehlen(stimmung):
    """Eigener Ordner zuerst (wechselt jedes Mal), sonst automatisch frei lizenzierte Musik von Openverse."""
    global MUSIKNACHWEIS
    zuletzt = _lade_json(USED_MUSIK)[-12:]
    endungen = ("*.mp3", "*.wav", "*.m4a", "*.ogg")
    eigene = [p for e in endungen for p in MUSIK_ORDNER.glob(f"**/{e}")] if MUSIK_ORDNER.exists() else []
    if eigene:
        passend = [p for p in eigene if stimmung and stimmung in str(p.parent).lower()] or eigene
        frisch = [p for p in passend if p.name not in zuletzt] or passend
        wahl = random.choice(frisch)
        _merke_musik(wahl.name)
        MUSIKNACHWEIS = ""
        log(f"Musik (eigener Ordner): {wahl.name}")
        return wahl

    for q in random.sample(STIMMUNG_SUCHE.get(stimmung, STIMMUNG_SUCHE["spannend"]), 3):
        try:
            r = requests.get("https://api.openverse.org/v1/audio/", headers=WIKI_HEADERS, timeout=30, params={
                "q": q, "license_type": "commercial,modification", "category": "music", "page_size": 40})
            treffer = [t for t in r.json().get("results", [])
                       if t.get("license") in ("cc0", "by", "by-sa", "pdm") and (t.get("duration") or 0) >= 45000
                       and t.get("url") and t.get("id") not in zuletzt]
        except Exception as e:
            log(f"Openverse nicht erreichbar: {e}")
            continue
        random.shuffle(treffer)
        for t in treffer[:5]:
            try:
                daten = requests.get(t["url"], headers=WIKI_HEADERS, timeout=60).content
            except requests.RequestException:
                continue
            if len(daten) < 200_000:
                continue
            ziel = WORK / "musik_download.mp3"
            ziel.write_bytes(daten)
            _merke_musik(t["id"])
            lizenz = f"CC {t['license'].upper()} {t.get('license_version') or ''}".strip()
            if t["license"] in ("cc0", "pdm"):
                lizenz = "gemeinfrei (CC0)"
            MUSIKNACHWEIS = f"„{t.get('title', 'Ohne Titel')}“ von {t.get('creator') or 'unbekannt'} – {lizenz} – {t.get('foreign_landing_url', '')}"
            log(f"Musik ({stimmung}): {MUSIKNACHWEIS}")
            return ziel
    log("Keine Musik gefunden, Video ohne Musik.")
    MUSIKNACHWEIS = ""
    return None


def _merke_musik(kennung):
    liste = _lade_json(USED_MUSIK)
    liste.append(kennung)
    USED_MUSIK.parent.mkdir(parents=True, exist_ok=True)
    USED_MUSIK.write_text(json.dumps(liste[-50:], ensure_ascii=False), encoding="utf-8")


def sfx_erzeugen():
    """Soundeffekte werden selbst erzeugt -> keinerlei Lizenzprobleme."""
    sfx = {
        "whoosh": "anoisesrc=d=0.5:c=pink:a=0.9,highpass=f=500,lowpass=f=5000,"
                  "afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.3",
        "pop": "sine=f=1100:d=0.09,afade=t=out:st=0.02:d=0.07",
        "impact": "sine=f=55:d=0.9,afade=t=out:st=0.05:d=0.85,volume=2.2",
        "riser": "anoisesrc=d=1.2:c=white:a=0.6,highpass=f=1500,afade=t=in:d=1.1,afade=t=out:st=1.1:d=0.1",
    }
    pfade = {}
    for name, filt in sfx.items():
        p = WORK / f"sfx_{name}.wav"
        ffmpeg("-f", "lavfi", "-i", filt, "-ar", "44100", "-ac", "1", p)
        pfade[name] = p
    return pfade


def audio_mischen(stimme, musik, gesamt, schnitt_zeiten, twist_zeit, ziel):
    """Stimme + Musik (duckt automatisch unter der Stimme) + leise Soundeffekte."""
    sfx = sfx_erzeugen()
    eingaben = ["-i", stimme]
    filter_teile = ["[0:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[stimme][sc]"]
    mix = ["[stimme]"]
    n = 1
    if musik:
        eingaben += ["-stream_loop", "-1", "-i", musik]
        filter_teile.append(
            f"[{n}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{gesamt:.2f},"
            f"volume={MUSIK_LAUTSTAERKE},afade=t=in:d=0.8,afade=t=out:st={max(0, gesamt - 1.8):.2f}:d=1.8[m];"
            f"[m][sc]sidechaincompress=threshold=0.04:ratio=6:attack=15:release=350[musik]")
        mix.append("[musik]")
        n += 1
    else:
        filter_teile.append("[sc]anullsink")

    ereignisse = [("pop", 0.05)] + [("whoosh", max(0, t - 0.25)) for t in schnitt_zeiten]
    if twist_zeit:
        ereignisse += [("riser", max(0, twist_zeit - 1.2)), ("impact", twist_zeit)]
    zaehler = {}
    for name, t in ereignisse:
        zaehler[name] = zaehler.get(name, 0) + 1
    index = {}
    for name in zaehler:
        eingaben += ["-i", sfx[name]]
        index[name] = n
        filter_teile.append(f"[{n}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                            f"asplit={zaehler[name]}" + "".join(f"[{name}{k}]" for k in range(zaehler[name])))
        n += 1
    benutzt = {}
    for name, t in ereignisse:
        k = benutzt.get(name, 0)
        benutzt[name] = k + 1
        ms = int(t * 1000)
        filter_teile.append(f"[{name}{k}]adelay={ms}|{ms},volume={SFX_LAUTSTAERKE}[e_{name}{k}]")
        mix.append(f"[e_{name}{k}]")
    filter_teile.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,"
                        f"apad=whole_dur={gesamt:.2f},loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.95[out]")
    ffmpeg(*eingaben, "-filter_complex", ";".join(filter_teile), "-map", "[out]", "-t", f"{gesamt:.2f}",
           "-ar", "44100", ziel)


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
    zeiten = [(a / tempo, b / tempo) for a, b in wort_zeiten(WORK / "voice.wav", woerter)]
    ende_gesamt = wav_dauer(WORK / "voice.wav") + 0.6
    eintraege = []
    for i in range(len(szenen)):
        idx = [k for k, s in enumerate(szene_von) if s == i]
        for gruppe in chunk_grenzen([woerter[k] for k in idx]):
            ks = [idx[g] for g in gruppe]
            eintraege.append([" ".join(woerter[k] for k in ks), zeiten[ks[0]][0], zeiten[ks[-1]][1], i == 0])
    for n in range(len(eintraege) - 1):          # Untertitel lueckenlos bis zum naechsten
        eintraege[n][2] = max(eintraege[n][2], eintraege[n + 1][1])
    eintraege[-1][2] = ende_gesamt
    untertitel_schreiben(eintraege, WORK / "subs.ass")

    starts = [0.0] + [zeiten[szene_von.index(i)][0] for i in range(1, len(szenen))]
    grenzen = starts + [ende_gesamt]
    dauern = [max(0.8, grenzen[i + 1] - grenzen[i]) for i in range(len(szenen))]

    # 3) Visuals: pro Szene echte Artikel-Bilder + passende Clips, alle ~3 s ein Schnitt
    bilder = daten.get("_bilder", [])
    benutzt, segmente, genutzte_bilder = set(), [], []
    bild_dateien = {}
    for i, (s, seg_dauer) in enumerate(zip(szenen, dauern)):
        teile = max(1, round(seg_dauer / MAX_SEGMENT))
        teil_dauer = seg_dauer / teile
        visuals = []
        nr = s.get("bild")
        if isinstance(nr, str) and nr.isdigit():
            nr = int(nr)
        if isinstance(nr, int) and 0 <= nr < len(bilder):
            visuals.append(("bild", nr))
        if len(visuals) < teile:
            for cid, url in clips_suchen(s.get("suchbegriff_en") or daten["thema"], teil_dauer, benutzt,
                                         anzahl=teile - len(visuals)):
                benutzt.add(cid)
                visuals.append(("clip", url))
        while len(visuals) < teile:             # zu wenige Treffer -> letztes Visual nochmal
            visuals.append(visuals[-1])
        for j, (art, quelle) in enumerate(visuals[:teile]):
            seg = WORK / f"seg_{i:02d}_{j}.mp4"
            if art == "bild":
                if quelle not in bild_dateien:
                    datei = WORK / f"bild_{quelle:02d}.jpg"
                    datei.write_bytes(requests.get(bilder[quelle]["url"], headers=WIKI_HEADERS, timeout=120).content)
                    bild_dateien[quelle] = datei
                    genutzte_bilder.append(bilder[quelle])
                bild_rendern(bild_dateien[quelle], teil_dauer, seg, len(segmente))
            else:
                clip = WORK / f"clip_{i:02d}_{j}.mp4"
                clip.write_bytes(requests.get(quelle, timeout=300).content)
                szene_rendern(clip, teil_dauer, seg)
            segmente.append(seg)
        log(f"Szene {i+1}/{len(szenen)}: {teile} Schnitt(e), "
            f"{'echtes Bild + ' if visuals[0][0] == 'bild' else ''}'{s.get('suchbegriff_en')}' ok")
    daten["bildnachweise"] = [f"{b['datei'].split(':', 1)[-1]} – {b['urheber']} – {b['lizenz']} (Wikimedia Commons)"
                              for b in genutzte_bilder]
    log(f"{len(segmente)} Schnitte, {len(genutzte_bilder)} verschiedene echte Bilder aus dem Artikel")

    liste = WORK / "liste.txt"
    liste.write_text("".join(f"file '{p.resolve()}'\n" for p in segmente))
    ffmpeg("-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", WORK / "bild.mp4")

    # 4) Ton: Stimme + Musik + Soundeffekte
    try:
        musik = musik_waehlen(str(daten.get("musik_stimmung", "spannend")).lower())
        twist = starts[-1] if len(starts) > 2 else None
        audio_mischen(WORK / "voice.wav", musik, ende_gesamt, starts[1:], twist, WORK / "ton.wav")
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
    teile = [daten.get("beschreibung", ""), "", f"📚 Quelle: {daten['quellen'][0]}"]
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
