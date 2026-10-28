#!/usr/bin/env python3
"""
FAKTENFIEBER – Pipeline v0.5
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
    prompt = f"""Du bist Autor des deutschen Faceless-Kanals FAKTENFIEBER (YouTube Shorts + Instagram Reels).
Schreibe ein Short ueber den folgenden Wikipedia-Artikel.

ABSOLUTE REGEL: Verwende NUR Fakten, die WOERTLICH so im Artikeltext stehen. Nichts dazuerfinden,
nichts aus deinem eigenen Wissen ergaenzen, nicht uebertreiben, keine Zahlen runden oder aendern.

ARTIKEL "{titel}":
\"\"\"{artikel}\"\"\"

STIL:
- 90 bis 125 Woerter gesprochener Text (ca. 40-55 Sekunden).
- Der allererste Satz beginnt IMMER mit "Wusstest du" und ist ein starker Hook mit dem ueberraschendsten Detail.
- Aufbau: Hook -> Spannung aufbauen -> ueberraschendes Detail -> Pointe/Twist am Ende.
- Kurze, lebendige Saetze, Du-Ansprache, locker aber nicht cringe. Keine Emojis, keine Klammern,
  keine Abkuerzungen, keine Sonderzeichen. Der Text wird von einer Computerstimme vorgelesen.
- 5 bis 8 Szenen. Jede Szene: 1-2 Saetze + englischer Stock-Video-Suchbegriff (2-3 Woerter, konkret
  und bildhaft, z. B. "old steam train", "foggy forest night").
- Jede Faktenbehauptung einzeln auflisten, mit "beleg" = exaktes, woertliches Zitat (5-25 Woerter)
  aus dem Artikeltext, das die Behauptung belegt. Copy-Paste, kein Umformulieren!
- Titel, Beschreibung und Hashtags fuer YouTube/Instagram selbst erstellen.

Antworte NUR mit JSON:
{{
  "thema": "{titel}",
  "titel": "YouTube-Titel, max. 60 Zeichen, macht neugierig, keine Clickbait-Luege",
  "szenen": [{{"text": "...", "suchbegriff_en": "..."}}],
  "behauptungen": [{{"aussage": "...", "beleg": "woertliches Zitat aus dem Artikel"}}],
  "beschreibung": "2-3 Saetze, erzaehlt neugierig weiter, endet mit Aufforderung zum Folgen",
  "hashtags": ["#fakten", "#wusstestdu", "..."]
}}"""
    daten, modell = ki(prompt, temperatur=0.8)
    daten["thema"] = titel
    daten["_modell"] = modell
    daten["_artikel"] = artikel
    daten["quellen"] = [f"Wikipedia – {titel}: {url}"]
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
Der erste Satz beginnt weiter mit "Wusstest du". Gesamtlaenge 80 bis 125 Woerter.

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
def stimme_laden():
    from piper import PiperVoice
    sprache, sprecher, qualitaet = VOICE_NAME.split("-")
    familie = sprache.split("_")[0]
    basis = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
             f"{familie}/{sprache}/{sprecher}/{qualitaet}/{VOICE_NAME}")
    modell = WORK / f"{VOICE_NAME}.onnx"
    for endung in (".onnx", ".onnx.json"):
        ziel = WORK / f"{VOICE_NAME}{endung}"
        if not ziel.exists():
            log(f"Lade Stimme {ziel.name} ...")
            r = requests.get(basis + endung, timeout=300)
            r.raise_for_status()
            ziel.write_bytes(r.content)
    return PiperVoice.load(str(modell))


def sprich(stimme, text, pfad):
    with wave.open(str(pfad), "wb") as wf:
        if hasattr(stimme, "synthesize_wav"):          # piper-tts >= 1.3
            try:
                from piper import SynthesisConfig
                stimme.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=LENGTH_SCALE))
            except ImportError:
                stimme.synthesize_wav(text, wf)
        else:                                          # aeltere Versionen
            stimme.synthesize(text, wf, length_scale=LENGTH_SCALE)
    with wave.open(str(pfad), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def wavs_verbinden(teile, ziel, pause=0.12):
    with wave.open(str(teile[0]), "rb") as wf:
        params = wf.getparams()
    stille = b"\x00" * int(params.framerate * pause) * params.sampwidth * params.nchannels
    with wave.open(str(ziel), "wb") as out:
        out.setparams(params)
        for i, t in enumerate(teile):
            with wave.open(str(t), "rb") as wf:
                out.writeframes(wf.readframes(wf.getnframes()))
            if i < len(teile) - 1:
                out.writeframes(stille)
    return pause


# ---------------------------------------------------------------- Untertitel
def chunks(text, max_woerter=3, max_zeichen=18):
    woerter, out, akt = text.split(), [], []
    for w in woerter:
        if akt and (len(akt) >= max_woerter or len(" ".join(akt + [w])) > max_zeichen):
            out.append(" ".join(akt))
            akt = []
        akt.append(w)
        if re.search(r"[.!?,:;]$", w):
            out.append(" ".join(akt))
            akt = []
    if akt:
        out.append(" ".join(akt))
    return out


def ass_zeit(t):
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def untertitel_schreiben(szenen_zeiten, ziel):
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
    for idx, (text, start, dauer) in enumerate(szenen_zeiten):
        teile = chunks(text)
        gewichte = [len(c) + (6 if re.search(r"[.!?]$", c) else 2 if re.search(r"[,:;]$", c) else 0)
                    for c in teile]
        summe = sum(gewichte) or 1
        t = start
        for c, g in zip(teile, gewichte):
            d = dauer * g / summe
            stil = "Hook" if idx == 0 else "Main"
            pop = r"{\an5\pos(540,1180)\fscx82\fscy82\t(0,90,\fscx100\fscy100)}"
            zeilen.append(f"Dialogue: 0,{ass_zeit(t)},{ass_zeit(t + d)},{stil},,0,0,0,,"
                          f"{pop}{ass_escape(c.upper())}")
            t += d
    Path(ziel).write_text(kopf + "\n".join(zeilen) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Clips (Pixabay)
def clip_suchen(begriff, dauer, schon_benutzt):
    versuche = [begriff, " ".join(begriff.split()[:2]), begriff.split()[-1], "abstract background"]
    for q in versuche:
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": PIXABAY_KEY, "q": q, "per_page": 20, "safesearch": "true"},
                         timeout=30)
        if r.status_code != 200:
            log(f"Pixabay {r.status_code}: {r.text[:200]}")
            continue
        treffer = [h for h in r.json().get("hits", []) if h["id"] not in schon_benutzt]
        if not treffer:
            continue
        treffer.sort(key=lambda h: (h.get("duration", 0) >= dauer,
                                    h["videos"]["large"].get("height", 0) >= h["videos"]["large"].get("width", 1),
                                    h.get("likes", 0)), reverse=True)
        h = treffer[0]
        for groesse in ("large", "medium", "small"):
            url = h["videos"].get(groesse, {}).get("url")
            if url:
                return h["id"], url
    raise RuntimeError(f"Kein Clip gefunden fuer '{begriff}'")


def ffmpeg(*args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    subprocess.run(cmd, check=True)


def szene_rendern(clip, dauer, ziel):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          f"fps={FPS},eq=brightness=-0.06:saturation=1.1")
    ffmpeg("-stream_loop", "-1", "-i", clip, "-t", f"{dauer:.3f}", "-vf", vf, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", ziel)


# ---------------------------------------------------------------- Video bauen
def video_bauen(daten):
    stimme = stimme_laden()
    szenen = daten["szenen"]

    # 1) Voiceover pro Szene
    wavs, dauern = [], []
    for i, s in enumerate(szenen):
        p = WORK / f"voice_{i:02d}.wav"
        dauern.append(sprich(stimme, s["text"], p))
        wavs.append(p)
    pause = wavs_verbinden(wavs, WORK / "voice_roh.wav")
    gesamt = sum(dauern) + pause * (len(dauern) - 1)
    log(f"Voiceover: {gesamt:.1f} s")

    # zu lang? -> leicht beschleunigen
    tempo = max(1.0, gesamt / MAX_DAUER)
    if tempo > 1.0:
        log(f"Etwas zu lang, beschleunige um Faktor {tempo:.2f}")
        ffmpeg("-i", WORK / "voice_roh.wav", "-filter:a", f"atempo={tempo:.4f}", WORK / "voice.wav")
    else:
        (WORK / "voice.wav").write_bytes((WORK / "voice_roh.wav").read_bytes())

    # Zeiten fuer Szenen/Untertitel
    zeiten, t = [], 0.0
    for i, d in enumerate(dauern):
        d_eff = d / tempo
        p_eff = (pause / tempo) if i < len(dauern) - 1 else 0.6   # am Ende kurz stehen lassen
        zeiten.append((szenen[i]["text"], t, d_eff, d_eff + p_eff))
        t += d_eff + p_eff
    untertitel_schreiben([(tx, st, d) for tx, st, d, _ in zeiten], WORK / "subs.ass")

    # 2) Clips laden + schneiden
    benutzt, segmente = set(), []
    for i, (s, (_, _, _, seg_dauer)) in enumerate(zip(szenen, zeiten)):
        cid, url = clip_suchen(s["suchbegriff_en"], seg_dauer, benutzt)
        benutzt.add(cid)
        roh = WORK / f"clip_{i:02d}.mp4"
        roh.write_bytes(requests.get(url, timeout=300).content)
        seg = WORK / f"seg_{i:02d}.mp4"
        szene_rendern(roh, seg_dauer, seg)
        segmente.append(seg)
        log(f"Szene {i+1}/{len(szenen)}: '{s['suchbegriff_en']}' ok")

    liste = WORK / "liste.txt"
    liste.write_text("".join(f"file '{p.resolve()}'\n" for p in segmente))
    ffmpeg("-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", WORK / "bild.mp4")

    # 3) Alles zusammen + Untertitel einbrennen
    final = OUT / "faktenfieber.mp4"
    subs = str((WORK / "subs.ass").resolve()).replace(":", r"\:")
    ffmpeg("-i", WORK / "bild.mp4", "-i", WORK / "voice.wav",
           "-vf", f"ass='{subs}'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", final)
    return final, t


# ---------------------------------------------------------------- Telegram
def telegram_senden(video, daten, check, dauer):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram nicht konfiguriert, ueberspringe.")
        return
    api = f"https://api.telegram.org/bot{TG_TOKEN}"
    caption = f"🎬 {daten['titel']}\n⏱ {dauer:.0f} s | {daten['thema']}"
    with open(video, "rb") as f:
        r = requests.post(f"{api}/sendVideo",
                          data={"chat_id": TG_CHAT, "caption": caption[:1000], "supports_streaming": "true"},
                          files={"video": ("faktenfieber.mp4", f, "video/mp4")}, timeout=300)
    if r.status_code != 200:
        log(f"Telegram-Video-Fehler: {r.text[:300]}")

    pruef = "\n".join(f"{'✅' if p.get('beleg_im_text') else '⚠️'} {p.get('behauptung')}"
                      for p in check.get("pruefung", []))
    text = (f"📌 TITEL\n{daten['titel']}\n\n"
            f"📝 BESCHREIBUNG\n{daten['beschreibung']}\n\n"
            f"Quelle: {daten['quellen'][0]}\n\n{' '.join(daten.get('hashtags', []))}\n\n"
            f"🔎 FAKTENCHECK (Prüfer: {check.get('pruefer', '?')})\n{pruef}\n\n"
            f"🤖 Geschrieben von: {daten.get('_modell')}")
    requests.post(f"{api}/sendMessage", data={"chat_id": TG_CHAT, "text": text[:4000],
                                              "disable_web_page_preview": "true"}, timeout=60)


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
