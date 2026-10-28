// FAKTENFIEBER – Telegram-Tuersteher + Wecker (Cloudflare Worker)
// 1) Antwortet SOFORT auf ✅ / ❌ / 🚀 und gibt den Klick direkt an GitHub weiter
// 2) Startet als minutengenauer Wecker das Erstellen und Posten (GitHub-Zeitplaene sind oft unpuenktlich)
// 3) Versteht Befehle im Chat: /video  /thema  /status  /pause  /weiter  /hilfe
//
// Variablen in Cloudflare (Settings -> Variables and Secrets):
//   TELEGRAM_BOT_TOKEN (Secret), TELEGRAM_CHAT_ID (Text), GH_TOKEN (Secret),
//   GH_REPO (Text, z. B. faktenfieber/faktenfieber-pipeline), WEBHOOK_SECRET (Secret)
// Trigger in Cloudflare (Settings -> Trigger Events -> Cron): */5 * * * *

// Wecker-Plan in oesterreichischer Zeit (Sommer-/Winterzeit wird automatisch beruecksichtigt)
const PLAN = {
  "05:30": "erstellen",   // Video fuer 08:00 bauen
  "05:35": "pruefen",     // nicht freigegebenes Abend-Video vom Vortag verfallen lassen
  "07:25": "pruefen",     // Erinnerung vor 08:00
  "07:55": "pruefen",     // POSTEN 08:00
  "08:10": "pruefen",     // Sicherheitsnetz
  "09:00": "pruefen",     // zweite Erinnerung
  "15:30": "erstellen",   // Video fuer 18:00 bauen
  "15:35": "pruefen",     // nicht freigegebenes Morgen-Video verfallen lassen
  "17:25": "pruefen",     // Erinnerung vor 18:00
  "17:55": "pruefen",     // POSTEN 18:00
  "18:10": "pruefen",     // Sicherheitsnetz
  "19:00": "pruefen",     // zweite Erinnerung
};

function wienJetzt() {
  // ergibt z. B. "2026-10-29 07:55"
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Europe/Vienna", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date());
}

async function githubStarten(env, workflow, inputs) {
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "faktenfieber-tuersteher",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: inputs || {} }),
  });
  return { ok: r.status === 204, status: r.status, text: r.status === 204 ? "" : (await r.text()).slice(0, 200) };
}

function telegram(env) {
  return (methode, daten) =>
    fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${methode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(daten),
    });
}

export default {
  // ---------------- Wecker (laeuft alle 5 Minuten)
  async scheduled(event, env, ctx) {
    const jetzt = wienJetzt();
    const uhr = jetzt.slice(11, 16);
    const aufgabe = PLAN[uhr];
    if (!aufgabe) return;
    const r = await githubStarten(env, `${aufgabe}.yml`, {});
    if (!r.ok) {
      await telegram(env)("sendMessage", {
        chat_id: env.TELEGRAM_CHAT_ID,
        text: `⚠️ Wecker ${uhr}: Konnte „${aufgabe}“ bei GitHub nicht starten (${r.status}).\n${r.text}`,
      });
    }
  },

  // ---------------- Telegram (Knoepfe + Befehle)
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response(`Faktenfieber-Tuersteher laeuft ✅ (Wien: ${wienJetzt()})`);
    }
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const tg = telegram(env);
    const update = await request.json().catch(() => ({}));

    // ----- Befehle im Chat
    const msg = update.message;
    if (msg) {
      if (String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok");
      const roh = String(msg.text || "").trim();
      const text = roh.toLowerCase();
      const senden = (t) => tg("sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: t });
      const befehl = async (aktion, bestaetigung) => {
        const r = await githubStarten(env, "pruefen.yml", { aktion, slot: "-" });
        await senden(r.ok ? bestaetigung : `⚠️ Konnte das nicht an GitHub weitergeben (${r.status}).\n${r.text}`);
      };
      if (text.startsWith("/video")) {
        const r = await githubStarten(env, "erstellen.yml", { slot: "sofort", thema: "" });
        await senden(r.ok
          ? "⚡ Alles klar! Ich baue ein Extra-Video zu einem zufälligen Thema. Kommt in ca. 10–15 Minuten – dann einfach auf 🚀 drücken."
          : `⚠️ Konnte das Extra-Video nicht starten (${r.status}).\n${r.text}`);
      } else if (text.startsWith("/thema")) {
        const thema = roh.replace(/^\/thema(@\S+)?/i, "").trim().slice(0, 80);
        if (!thema) {
          await senden("✍️ Schreib das Thema direkt dahinter, z. B.:\n/thema Pest im Mittelalter\n/thema Titanic\n/thema Kraken");
        } else {
          const r = await githubStarten(env, "erstellen.yml", { slot: "sofort", thema });
          await senden(r.ok
            ? `🔎 Super! Ich recherchiere zu „${thema}“ und baue ein Wunsch-Video. Kommt in ca. 10–15 Minuten – dann auf 🚀 drücken.`
            : `⚠️ Konnte das Wunsch-Video nicht starten (${r.status}).\n${r.text}`);
        }
      } else if (text.startsWith("/pause")) {
        await befehl("pause", "⏸ Pause wird aktiviert … (Bestätigung kommt gleich)");
      } else if (text.startsWith("/weiter")) {
        await befehl("weiter", "▶️ Automatik wird wieder eingeschaltet … (Bestätigung kommt gleich)");
      } else if (text.startsWith("/status")) {
        await befehl("status", `🟢 Türsteher läuft (Wien: ${wienJetzt()}). Ausführlicher Status kommt in ca. 1 Minute …`);
      } else if (text.startsWith("/hilfe") || text.startsWith("/start") || text.startsWith("/help")) {
        await senden("🤖 FAKTENFIEBER-BOT\n\n" +
          "Unter jedem Video:\n✅ Freigeben = pünktlich um 8:00 / 18:00 posten\n🚀 Jetzt sofort posten\n❌ Neu machen = neues Video\n\n" +
          "Befehle:\n/video – Extra-Video zu einem zufälligen Thema\n/thema Titanic – Extra-Video zu deinem Wunschthema\n" +
          "/status – was läuft, was wurde gepostet\n/pause – keine automatischen Videos (z. B. Urlaub)\n/weiter – Automatik wieder an\n" +
          "/hilfe – diese Hilfe");
      }
      return new Response("ok");
    }

    // ----- Knoepfe unter den Videos
    const cb = update.callback_query;
    if (!cb) return new Response("ok");
    if (String(cb.message?.chat?.id ?? "") !== String(env.TELEGRAM_CHAT_ID)) {
      await tg("answerCallbackQuery", { callback_query_id: cb.id });
      return new Response("ok");
    }
    const [aktion, slot] = String(cb.data || "").split("|");
    if (!slot || !["ok", "neu", "jetzt"].includes(aktion)) {
      await tg("answerCallbackQuery", { callback_query_id: cb.id, text: "👍" });
      return new Response("ok");
    }
    const uhr = slot.split(" ")[1] || slot;
    const schonVorbei = slot <= wienJetzt();          // Termin schon erreicht? (Text-Vergleich funktioniert)

    const r = await githubStarten(env, "pruefen.yml", { aktion, slot });
    if (!r.ok) {
      await tg("answerCallbackQuery", { callback_query_id: cb.id, text: "⚠️ Konnte GitHub nicht erreichen" });
      await tg("sendMessage", {
        chat_id: env.TELEGRAM_CHAT_ID,
        text: `⚠️ Dein Klick ist angekommen, aber GitHub hat abgelehnt (${r.status}). Bitte nochmal drücken.\n\n${r.text}`,
      });
      return new Response("ok");
    }

    const antworten = {
      ok: schonVorbei
        ? ["✅ Angekommen! Geht jetzt sofort raus.", "✅ Angekommen! Der Termin ist schon vorbei, also geht das Video jetzt sofort raus. Du bekommst gleich die Links."]
        : [`✅ Angekommen! Geht um ${uhr} Uhr raus.`, `✅ Angekommen! Das Video geht um ${uhr} Uhr auf YouTube + Instagram raus. Du bekommst dann die Links.`],
      jetzt: ["🚀 Wird jetzt gepostet!", "🚀 Alles klar, das Video wird jetzt sofort gepostet. Du bekommst in ein paar Minuten die Links."],
      neu: ["🔄 Alles klar, ich baue ein neues!", `🔄 Alles klar, das Video wird verworfen. Das neue kommt in ca. 10–15 Minuten.`],
    }[aktion];

    await tg("answerCallbackQuery", { callback_query_id: cb.id, text: antworten[0] });
    await tg("editMessageReplyMarkup", {
      chat_id: env.TELEGRAM_CHAT_ID,
      message_id: cb.message.message_id,
      reply_markup: { inline_keyboard: [[{ text: "⏳ " + antworten[0], callback_data: "x" }]] },
    });
    await tg("sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: antworten[1] });
    return new Response("ok");
  },
};
