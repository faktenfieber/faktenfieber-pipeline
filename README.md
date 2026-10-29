# 🔥 Faktenfieber – Handbuch

Vollautomatischer Faceless-Kanal für **YouTube Shorts + Instagram Reels**.
Recherche, Skript, Faktencheck, Stimme, Schnitt, Musik und Upload laufen von selbst – du gibst nur per Telegram frei.

---

## 📅 Tagesablauf (läuft automatisch)

| Termin | gebaut um | Erinnerung | gepostet | Freigabe möglich bis |
|---|---|---|---|---|
| **08:00** | 05:30 | 07:25 + 09:00 | 07:55 | 11:00 |
| **12:00** | 09:30 | 11:25 + 13:00 | 11:55 | 17:00 |
| **18:00** | 15:30 | 17:25 + 19:00 | 17:55 | 07:00 (nächster Tag) |
| Sonntag | | | 📈 Wochenbericht 19:30 | |

Freigabe **nach** dem Termin → wird sofort gepostet. Ohne ✅ wird nichts gepostet – außer der **Auto-Modus** ist an.

---

## 🔘 Knöpfe unter jedem Video

| Knopf | Wirkung |
|---|---|
| ✅ Freigeben | pünktlich zum Termin posten |
| 🚀 Jetzt sofort posten | geht in 2–3 Minuten raus |
| ❌ Neu machen | neues Video in ca. 15 Min. (max. 3× pro Termin) |
| ⛔ Nicht posten | im Auto-Modus: dieses Video stoppen |
| 🔁 Teil 2 | nach dem Posten: zweites Video zum selben Thema |
| 🔁 Nochmal posten | erscheint, wenn Posten 3× fehlgeschlagen ist |

## 💬 Befehle

| Befehl | Wirkung |
|---|---|
| **Videos** | |
| `/thema Titanic` | Wunsch-Video – **sofort** |
| `/video` | Zufalls-Video – **sofort** |
| `/idee Area 51` | fürs **nächste automatische** Video vormerken |
| `/ideen` · `/ideen leeren` | Ideen-Liste ansehen / löschen |
| `/test` | Testvideo nur für dich (wird nirgends gepostet) |
| **Automatik** | |
| `/auto an` · `/auto aus` | ohne / mit deiner Freigabe posten |
| `/stopp` | 🛑 Notbremse: alle geplanten Videos stoppen |
| `/pause` · `/weiter` | keine automatischen Videos (z. B. Urlaub) |
| **Stil** | |
| `/voice` | alle Stimmen |
| `/voice test` · `/voice test Orus` | Hörproben als Sprachnachricht |
| `/voice Orus` | Stimme wechseln |
| `/musik leiser` · `lauter` · `aus` · `normal` | Musik-Lautstärke |
| `/schwierigkeit 5` | wie leicht die Videos sind (4 = sehr leicht … 10 = schwer) |
| **Info** | |
| `/status` · `/stats` · `/einstellungen` · `/hilfe` | Überblick |

---

## ⚙️ Wie ein Video entsteht

1. **Thema:** deine Ideen-Liste → sonst KI-Ideen (viral, bunt gemischt alt/neu) + aktuelle Wikipedia-Trends
2. **Skript** aus dem echten Wikipedia-Artikel, gesprochen von deiner gewählten Stimme, freier Hook-Stil (wechselt automatisch)
3. **Catch & Loop:** erster Satz wird extra geschärft, Ende führt in den Anfang zurück
4. **Checks:** Länge · Verständlichkeit (Wiener Sachtextformel) · jedes Zitat steht im Artikel · zweite KI prüft Fakten
5. **Video:** Gemini-Stimme, echte Bilder (Wikimedia, freie Lizenz), passende Clips (Pixabay),
   Karaoke-Untertitel, Hook-Einblendung, ruhige Musik ohne Gesang, Fortschrittsbalken, Wasserzeichen
6. **Upload:** YouTube (bis zum Audit privat) + Instagram (Reel + erster Kommentar mit Frage)

---

## 🛠 Was tun, wenn …

| Problem | Lösung |
|---|---|
| Video soll doch nicht raus (Auto-Modus) | ⛔ unter dem Video oder `/stopp` für alle |
| Kein Video kam | Selbstheilung baut es automatisch nach (Nachricht 🩹). Sonst `/status` oder Actions → Erstellen → Run workflow |
| Knopfdruck reagiert nicht | `/status` schreiben – kommt keine Antwort, ist der Cloudflare-Türsteher down (dash.cloudflare.com) |
| ❌ YouTube: „uploadLimitExceeded“ | Tageslimit des Kanals – setzt sich nach 24 h zurück, Instagram läuft trotzdem |
| ❌ Instagram-Fehler | Token prüfen; er wird alle 20 Tage automatisch verlängert (braucht GH_PAT) |
| Videos zu schwer / zu leicht | `/schwierigkeit` anpassen |
| Rote Läufe bei Actions | Lauf öffnen → roten Schritt aufklappen → Log an Claude schicken |

---

## 🔑 Wartung

| Was | Wann |
|---|---|
| GitHub-Token **GH_PAT** | läuft nach 1 Jahr ab – Bot warnt 14 Tage vorher |
| GitHub-Token beim Cloudflare-Türsteher (**GH_TOKEN**) | ebenfalls 1 Jahr – gleichzeitig erneuern |
| Instagram-Token | erneuert sich selbst |
| YouTube-Zugang | läuft nicht ab |
| GitHub-Minuten | privat nur 2.000/Monat – bei 3 Videos am Tag knapp! Repo **öffentlich** machen = unbegrenzt |

**Nach dem YouTube-Audit:** Repo → Settings → Secrets and variables → Actions → **Variables** →
`YT_SICHTBARKEIT` = `public` anlegen. Dann gehen Videos direkt öffentlich raus.

---

## 💸 Sinnvolle Upgrades (wenn der Kanal läuft)

| Upgrade | Kosten | Effekt |
|---|---|---|
| Gemini-API mit Abrechnung | ca. 2–5 €/Monat | keine Tageslimits, schnellere und stabilere Videos |
| ElevenLabs Starter | 6 $/Monat | noch natürlichere Stimme: Secret `ELEVENLABS_API_KEY` anlegen, dann `/voice eleven:VOICE-ID` |
| Repo öffentlich | 0 € | unbegrenzte GitHub-Minuten |

---

## 📁 Dateien

| Datei | Zweck |
|---|---|
| `faktenfieber.py` | die komplette Pipeline |
| `.github/workflows/erstellen.yml` | Video bauen |
| `.github/workflows/pruefen.yml` | Freigaben, Posten, Erinnerungen, Selbstheilung |
| `.github/workflows/status.yml` | `/status`, `/stats`, `/ideen`, `/einstellungen`, Wochenbericht |
| `.github/workflows/stimme.yml` | Hörproben für `/voice test` |
| `.github/workflows/testvideo.yml` | Testvideo nur an Telegram |
| `data/` | Gedächtnis: benutzte Themen, Status, Ideen, Musik-Archiv (nicht von Hand ändern) |
| Cloudflare-Worker `faktenfieber-bot` | Telegram-Türsteher + minutengenauer Wecker |
