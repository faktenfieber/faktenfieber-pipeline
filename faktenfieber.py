#!/usr/bin/env python3
"""
FAKTENFIEBER – Pipeline v0.1 (Testvideo)
Thema finden -> Skript -> Faktencheck -> Voiceover -> Clips -> Schnitt + Untertitel -> Telegram
"""
import json
import os
import random
import re
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

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

KATEGORIEN = [
    "Geschichte", "Tiere", "Weltall", "Menschlicher Koerper", "Psychologie",
    "Erfindungen und Technik", "Essen und Trinken", "Geografie", "Ozeane",
    "Wissenschaft", "Sprache und Woerter", "Kuriose Gesetze", "Natur und Pflanzen",
    "Marken und Firmen", "Sport-Kuriositaeten", "Kunst und Musik",
    "Historische Kriminalfaelle", "Populaere Irrtuemer (Mythos-Check)",
    "Alltagsgegenstaende", "Wetter und Naturphaenomene",
]

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def log(msg):
    print(f"[Faktenfieber] {msg}", flush=True)


# ---------------------------------------------------------------- Gemini
def gemini_modelle():
    """Sucht die aktuell verfuegbaren Flash-Modelle, bestes zuerst."""
    if os.environ.get("GEMINI_MODEL"):
        return [os.environ["GEMINI_MODEL"]]
    r = requests.get(f"{GEMINI_BASE}/models",
                     params={"key": GEMINI_KEY, "pageSize": 1000}, timeout=30)
    r.raise_for_status()
    kandidaten = []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in name:
            continue
        if any(x in name for x in ("image", "tts", "audio", "live", "embed", "thinking")):
            continue
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        version = float(v.group(1)) if v else 0.0
        score = (
            "preview" not in name and "exp" not in name,
            "lite" not in name,
            version,
        )
        kandidaten.append((score, name))
    kandidaten.sort(reverse=True)
    namen = [n for _, n in kandidaten] or ["gemini-flash-latest"]
    log(f"Gemini-Modelle: {', '.join(namen[:4])}")
    return namen


MODELLE = []
GESPERRT = set()          # (modell, suche) Kombis ohne Gratis-Kontingent
OHNE_SUCHE = False        # wird True, wenn die Live-Suche nicht verfuegbar war


def quota_info(r):
    """Liest aus einer 429-Antwort, welches Limit gerissen ist."""
    try:
        details = r.json().get("error", {}).get("details", [])
    except ValueError:
        return ""
    teile = []
    for d in details:
        for v in d.get("violations", []):
            teile.append(f"{v.get('quotaId', '')} {v.get('quotaValue', '')}".strip())
    return "; ".join(teile)


def _anfrage(modell, body):
    r = requests.post(f"{GEMINI_BASE}/models/{modell}:generateContent",
                      params={"key": GEMINI_KEY}, json=body, timeout=180)
    if r.status_code != 200:
        return r, None
    kand = (r.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in kand.get("content", {}).get("parts", []))
    quellen = [ch["web"]["title"] for ch in kand.get("groundingMetadata", {}).get("groundingChunks", [])
               if (ch.get("web") or {}).get("title")]
    return r, (text, quellen) if text.strip() else None


def gemini(prompt, suche=False, temperatur=0.9):
    global MODELLE, OHNE_SUCHE
    if not MODELLE:
        MODELLE = gemini_modelle()
    modi = [True, False] if suche else [False]
    fehler = ""
    for mit_suche in modi:
        if mit_suche is False and suche:
            OHNE_SUCHE = True
            log("⚠️ Live-Suche nicht verfuegbar, arbeite ohne Google-Suche weiter.")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperatur},
        }
        if mit_suche:
            body["tools"] = [{"google_search": {}}]
        for modell in MODELLE[:8]:
            if (modell, mit_suche) in GESPERRT:
                continue
            for versuch in range(2):
                r, ergebnis = _anfrage(modell, body)
                if ergebnis:
                    log(f"Gemini ok: {modell} ({'mit' if mit_suche else 'ohne'} Suche)")
                    return ergebnis[0], ergebnis[1], modell
                info = quota_info(r)
                fehler = f"{modell}: HTTP {r.status_code} {info or r.text[:200]}"
                log(f"Gemini-Problem -> {fehler}")
                if r.status_code == 429:
                    # Tageslimit oder gar kein Gratis-Kontingent -> sofort naechstes Modell
                    if mit_suche or "PerDay" in info or "limit: 0" in r.text or ("FreeTier" in info and versuch):
                        GESPERRT.add((modell, mit_suche))
                        break
                    time.sleep(20)
                    continue
                if r.status_code in (500, 503):
                    time.sleep(10)
                    continue
                GESPERRT.add((modell, mit_suche))
                break
            else:
                GESPERRT.add((modell, mit_suche))
    raise RuntimeError(f"Gemini nicht erreichbar: {fehler}")


def json_aus(text):
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("Kein JSON in der KI-Antwort")
    return json.loads(text[a:b + 1])


# ---------------------------------------------------------------- Thema + Skript
def lade_benutzte():
    if USED_FILE.exists():
        return json.loads(USED_FILE.read_text(encoding="utf-8"))
    return []


def speichere_thema(thema):
    benutzt = lade_benutzte()
    benutzt.append(thema)
    USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    USED_FILE.write_text(json.dumps(benutzt, ensure_ascii=False, indent=1), encoding="utf-8")


def skript_erstellen(benutzt):
    kategorie = random.choice(KATEGORIEN)
    letzte = "\n".join(f"- {t}" for t in benutzt[-150:]) or "- (noch keine)"
    prompt = f"""Du bist Autor des deutschen Faceless-Kanals FAKTENFIEBER (YouTube Shorts + Instagram Reels).
Recherchiere mit der Google-Suche EINEN ueberraschenden, wenig bekannten, aber 100 % WAHREN und gut belegten Fakt
oder eine kurze wahre Geschichte aus der Kategorie: {kategorie}.

REGELN:
- Nur Dinge, die von serioesen Quellen (Fachmedien, Museen, Unis, Enzyklopaedien, grosse Zeitungen) belegt sind.
  Wenn du unsicher bist: anderes Thema nehmen. Keine Mythen als Fakten, keine Uebertreibungen.
- Nicht verwenden (schon gemacht):
{letzte}
- Laenge des gesprochenen Textes: 90 bis 125 Woerter (ca. 40-55 Sekunden).
- Der allererste Satz beginnt IMMER mit "Wusstest du" und ist ein starker Hook, der sofort neugierig macht.
- Aufbau: Hook -> Spannung aufbauen -> ueberraschendes Detail -> Pointe/Twist am Ende.
- Kurze, lebendige Saetze, Du-Ansprache, locker aber nicht cringe. Keine Emojis, keine Klammern,
  keine Abkuerzungen, keine Sonderzeichen. Der Text wird von einer Computerstimme vorgelesen.
- Teile den Text in 5 bis 8 Szenen. Jede Szene: 1-2 Saetze + ein englischer Suchbegriff (2-3 Woerter)
  fuer passendes Stock-Video (konkret und bildhaft, z. B. "old steam train", "octopus underwater").
- Liste jede einzelne Faktenbehauptung separat auf, damit sie geprueft werden kann.
- Erstelle Titel, Beschreibung und Hashtags fuer YouTube/Instagram selbst.

Antworte NUR mit diesem JSON:
{{
  "thema": "kurzes Stichwort zum Thema",
  "kategorie": "{kategorie}",
  "titel": "YouTube-Titel, max. 60 Zeichen, macht neugierig, kein Clickbait-Luege",
  "szenen": [{{"text": "...", "suchbegriff_en": "..."}}],
  "behauptungen": ["..."],
  "quellen": ["Name der Quelle - URL"],
  "beschreibung": "2-3 Saetze, erzaehlt neugierig weiter, endet mit Aufforderung zum Folgen",
  "hashtags": ["#fakten", "#wusstestdu", "..."]
}}"""
    text, grounding, modell = gemini(prompt, suche=True)
    daten = json_aus(text)
    daten["_grounding"] = grounding
    daten["_modell"] = modell
    return daten


def faktencheck(daten):
    behauptungen = "\n".join(f"{i+1}. {b}" for i, b in enumerate(daten["behauptungen"]))
    sprechtext = " ".join(s["text"] for s in daten["szenen"])
    prompt = f"""Du bist strenger Faktenpruefer. Pruefe mit der Google-Suche jede Behauptung einzeln.
Urteil "korrekt" nur, wenn mindestens eine serioese Quelle sie klar bestaetigt.
Pruefe auch, ob der Sprechtext insgesamt etwas Falsches oder Irrefuehrendes sagt.

SPRECHTEXT:
{sprechtext}

BEHAUPTUNGEN:
{behauptungen}

Antworte NUR mit JSON:
{{
  "pruefung": [{{"behauptung": "...", "urteil": "korrekt|falsch|unsicher", "begruendung": "kurz", "quelle": "Name - URL"}}],
  "gesamt": "ok|nicht_ok"
}}"""
    text, grounding, _ = gemini(prompt, suche=True, temperatur=0.1)
    ergebnis = json_aus(text)
    ergebnis["_grounding"] = grounding
    alle_ok = ergebnis.get("gesamt") == "ok" and all(
        p.get("urteil") == "korrekt" for p in ergebnis.get("pruefung", []))
    return alle_ok, ergebnis


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
    caption = f"🎬 {daten['titel']}\n⏱ {dauer:.0f} s | {daten.get('kategorie', '')}"
    if OHNE_SUCHE:
        caption += "\n⚠️ Ohne Live-Google-Suche erstellt: Fakten bitte besonders genau checken!"
    with open(video, "rb") as f:
        r = requests.post(f"{api}/sendVideo",
                          data={"chat_id": TG_CHAT, "caption": caption[:1000], "supports_streaming": "true"},
                          files={"video": ("faktenfieber.mp4", f, "video/mp4")}, timeout=300)
    if r.status_code != 200:
        log(f"Telegram-Video-Fehler: {r.text[:300]}")

    pruef = "\n".join(f"{'✅' if p.get('urteil') == 'korrekt' else '⚠️'} {p.get('behauptung')}\n   ↳ {p.get('quelle', '')}"
                      for p in check.get("pruefung", []))
    text = (f"📌 TITEL\n{daten['titel']}\n\n"
            f"📝 BESCHREIBUNG\n{daten['beschreibung']}\n\n{' '.join(daten.get('hashtags', []))}\n\n"
            f"🔎 FAKTENCHECK\n{pruef}\n\n"
            f"📚 QUELLEN\n" + "\n".join(daten.get("quellen", [])))
    requests.post(f"{api}/sendMessage", data={"chat_id": TG_CHAT, "text": text[:4000],
                                              "disable_web_page_preview": "true"}, timeout=60)


# ---------------------------------------------------------------- Main
def main():
    for d in (OUT, WORK):
        d.mkdir(parents=True, exist_ok=True)
    if not (GEMINI_KEY and PIXABAY_KEY):
        sys.exit("GEMINI_API_KEY oder PIXABAY_API_KEY fehlt (GitHub Secrets pruefen).")

    benutzt = lade_benutzte()
    daten = check = None
    for versuch in range(1, MAX_VERSUCHE + 1):
        log(f"Versuch {versuch}: suche Thema ...")
        try:
            kandidat = skript_erstellen(benutzt)
            if not kandidat["szenen"][0]["text"].lower().startswith("wusstest du"):
                kandidat["szenen"][0]["text"] = "Wusstest du schon? " + kandidat["szenen"][0]["text"]
            log(f"Thema: {kandidat['thema']} ({kandidat['kategorie']}) – pruefe Fakten ...")
            ok, ergebnis = faktencheck(kandidat)
        except Exception as e:
            log(f"Fehler: {e}")
            continue
        if ok:
            daten, check = kandidat, ergebnis
            log("Faktencheck bestanden ✅")
            break
        log("Faktencheck NICHT bestanden, nehme neues Thema.")
        benutzt = benutzt + [kandidat.get("thema", "")]

    if not daten:
        sys.exit("Kein Thema hat den Faktencheck bestanden. Bitte nochmal starten.")

    video, dauer = video_bauen(daten)
    meta = {**daten, "faktencheck": check, "dauer": round(dauer, 1)}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    speichere_thema(daten["thema"])
    telegram_senden(video, daten, check, dauer)
    log(f"FERTIG: {video} ({dauer:.1f} s)")


if __name__ == "__main__":
    main()
