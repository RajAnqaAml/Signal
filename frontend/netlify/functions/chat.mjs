/**
 * Personal Signals chat assistant — Netlify Function.
 *
 * Flow:
 *   1. (optional) verify x-chat-token against CHAT_TOKEN env (quota protection)
 *   2. read latest snapshot per symbol + today's signals from Supabase
 *   3. compute deterministic trade plans (entry zone / target / SL) in JS
 *   4. build a grounded prompt and call Gemini 2.5 Flash
 *   5. return the reply
 *
 * Env vars (set in Netlify Site Settings -> Environment):
 *   GOOGLE_API_KEY            required (Gemini)
 *   PUBLIC_SUPABASE_URL       required (already set for the build)
 *   PUBLIC_SUPABASE_ANON_KEY  required (already set for the build; read-only)
 *   CHAT_TOKEN                optional — if set, requests must send matching x-chat-token
 *
 * NOTE: this reads everything from Supabase (snapshots are <=2 min old). It does
 * NOT re-run the engine or fetch NSE live. Premium (Rs) figures are ESTIMATES via
 * a 0.5 ATM delta — real per-strike premium is a v2 upgrade (needs recorder change).
 */

// Gemini via raw REST — avoids the @google/genai SDK, which returns 401 on
// Netlify's runtime even with a valid key (raw REST with the same key works).
async function callGemini(apiKey, model, contents, hasImage) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
      contents,
      // give vision answers more room (reading a chart + full setup)
      generationConfig: { temperature: 0.3, maxOutputTokens: hasImage ? 1200 : 800, thinkingConfig: { thinkingBudget: 0 } },
    }),
  });
  const text = await r.text();
  if (!r.ok) { const e = new Error(`gemini ${r.status}: ${text.slice(0, 200)}`); e.status = r.status; throw e; }
  const data = JSON.parse(text);
  const parts = data?.candidates?.[0]?.content?.parts || [];
  return parts.map(p => p.text || "").join("").trim();
}

// Minimal Supabase REST (PostgREST) reader — avoids the supabase-js SDK and its
// realtime/WebSocket dependency (which breaks on Node < 22). We only do SELECTs.
async function sbSelect(baseUrl, key, table, query) {
  const r = await fetch(`${baseUrl}/rest/v1/${table}?${query}`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!r.ok) throw new Error(`supabase ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

// ── Engine constants (mirror notify.py — the P&L source of truth) ──────────
const SYMBOL_CFG = {
  NIFTY:     { step: 50,  target: 30,  sl: 18, lot: 65, entryBuf: 10 },
  BANKNIFTY: { step: 100, target: 60,  sl: 30, lot: 30, entryBuf: 25 },
  SENSEX:    { step: 100, target: 100, sl: 50, lot: 20, entryBuf: 30 },
};
const DELTA = 0.5;
const SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"];
const MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-1.5-flash-latest"];

// ── helpers ────────────────────────────────────────────────────────────────
function istNow() {
  return new Date(Date.now() + 5.5 * 3600 * 1000);
}
function istDateStr() {
  const d = istNow();
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}
function istClock(iso) {
  const d = new Date(new Date(iso).getTime() + 5.5 * 3600 * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}
function roundStrike(spot, step) {
  return Math.round(spot / step) * step;
}

// Pull the per-symbol signal block out of a snapshot row's raw_payload.
function sigOf(row) {
  const raw = row?.raw_payload || {};
  const sig = raw.signal || {};
  const oc = raw.option_chain || {};
  const sp = raw.spot || {};
  return {
    signal: sig.signal || "NEUTRAL",
    confidence: Number(sig.confidence || 0),
    tier: sig.push_tier || "TIER_3",
    regime: sig.ai_regime || "?",
    reasoning: sig.ai_reasoning || "",
    target1: Number(sig.target1 || 0),
    stop_loss: Number(sig.stop_loss || 0),
    spot: Number(row.spot_price || sp.price || 0),
    ts: row.ts,
    // support/resistance inputs (available even when signal is WAIT)
    dayHigh: Number(sp.high || row.spot_high || 0),
    dayLow: Number(sp.low || row.spot_low || 0),
    prevClose: Number(sp.prev_close || 0),
    dayOpen: Number(sp.open || 0),
    maxPain: Number(oc.max_pain || 0),
    callWall: Number(oc.max_ce_oi_strike || 0),   // resistance (highest CE OI)
    putWall: Number(oc.max_pe_oi_strike || 0),    // support (highest PE OI)
    pcr: Number(oc.pcr_total || 0),
  };
}

// Chart-based S/R from previous daily candles: CPR, floor pivots, multi-day range.
async function fetchChartLevels(baseUrl, key, symbol) {
  const since = new Date(Date.now() - 16 * 24 * 3600 * 1000).toISOString();
  // NEWEST-first + limit: PostgREST caps at 1000 rows, so ts.asc would return the
  // OLDEST 1000 and miss recent days (the pivot bug). Fetch desc, then reverse.
  const q = `select=ts,open,high,low,close`
    + `&symbol=eq.${symbol}&interval_minutes=eq.5`
    + `&ts=gte.${encodeURIComponent(since)}&order=ts.desc&limit=1000`;
  let rows;
  try { rows = await sbSelect(baseUrl, key, "historical_candles", q); }
  catch { return null; }
  if (!rows || !rows.length) return null;
  rows.reverse(); // back to chronological so daily close = last candle of day

  // aggregate 5-min candles -> daily OHLC (ts is already IST iso)
  const days = {};
  for (const r of rows) {
    const d = String(r.ts).slice(0, 10);
    if (r.high == null || r.low == null || r.close == null) continue;
    if (!days[d]) days[d] = { o: +r.open, h: +r.high, l: +r.low, c: +r.close };
    else { days[d].h = Math.max(days[d].h, +r.high); days[d].l = Math.min(days[d].l, +r.low); days[d].c = +r.close; }
  }
  const today = istDateStr();
  const dates = Object.keys(days).filter(d => d < today).sort(); // completed days only
  if (!dates.length) return null;

  const prev = dates[dates.length - 1];
  const { h: H, l: L, c: C } = days[prev];
  const PP = (H + L + C) / 3;
  const bc = (H + L) / 2;
  const cpr = { pivot: PP, bc, tc: 2 * PP - bc };
  const piv = {
    PP, R1: 2 * PP - L, S1: 2 * PP - H,
    R2: PP + (H - L), S2: PP - (H - L),
    R3: H + 2 * (PP - L), S3: L - 2 * (H - PP),
  };
  const rng = (n) => {
    const ds = dates.slice(-n);
    return { hi: Math.max(...ds.map(d => days[d].h)), lo: Math.min(...ds.map(d => days[d].l)) };
  };
  return { prevDate: prev, prevH: H, prevL: L, prevC: C, cpr, piv, r5: rng(5), r10: rng(10), nDays: dates.length };
}

function formatChartLevels(symbol, cl) {
  const R = Math.round;
  const L = [];
  L.push(`=== ${symbol} CHART LEVELS (from previous daily candles; prev day ${cl.prevDate} H/L/C ${R(cl.prevH)}/${R(cl.prevL)}/${R(cl.prevC)}) ===`);
  L.push(`  CPR: pivot ${R(cl.cpr.pivot)} | TC ${R(cl.cpr.tc)} | BC ${R(cl.cpr.bc)}`);
  L.push(`  Floor pivots — Resistance: R1 ${R(cl.piv.R1)}, R2 ${R(cl.piv.R2)}, R3 ${R(cl.piv.R3)}`);
  L.push(`                 Support:    S1 ${R(cl.piv.S1)}, S2 ${R(cl.piv.S2)}, S3 ${R(cl.piv.S3)}  (PP ${R(cl.piv.PP)})`);
  L.push(`  Multi-day range: 5-day ${R(cl.r5.lo)}–${R(cl.r5.hi)} | 10-day ${R(cl.r10.lo)}–${R(cl.r10.hi)}`);
  return L.join("\n");
}

function detectSymbols(t) {
  t = (t || "").toLowerCase();
  const out = new Set();
  if (/sensex/.test(t)) out.add("SENSEX");
  if (/bank\s*nifty|banknifty|\bbank\b/.test(t)) out.add("BANKNIFTY");
  if (/\bnifty\b/.test(t) && !/bank\s*nifty|banknifty/.test(t)) out.add("NIFTY");
  return out.size ? [...out] : SYMBOLS;
}

// Organise raw levels into support (below spot) / resistance (above spot).
function keyLevels(s) {
  const spot = s.spot;
  const cand = [
    ["call-wall (OI)", s.callWall],
    ["put-wall (OI)", s.putWall],
    ["max-pain", s.maxPain],
    ["day-high", s.dayHigh],
    ["day-low", s.dayLow],
    ["prev-close", s.prevClose],
    ["day-open", s.dayOpen],
  ].filter(([, v]) => v > 0);
  const res = cand.filter(([, v]) => v > spot).sort((a, b) => a[1] - b[1]);
  const sup = cand.filter(([, v]) => v < spot).sort((a, b) => b[1] - a[1]);
  return { res, sup };
}

// Deterministic trade plan — computed in code so the LLM never does the math.
function tradePlan(symbol, s) {
  const cfg = SYMBOL_CFG[symbol];
  const dir = s.signal;
  if (dir !== "CALL" && dir !== "PUT") return null;

  const spot = s.spot;
  const strike = roundStrike(spot, cfg.step);
  const optType = dir === "PUT" ? "PE" : "CE";

  // prefer engine's stored ATR levels; fall back to scalp pts
  const tPts = s.target1 ? Math.abs(s.target1 - spot) : cfg.target;
  const sPts = s.stop_loss ? Math.abs(s.stop_loss - spot) : cfg.sl;

  const targetSpot = dir === "PUT" ? spot - tPts : spot + tPts;
  const slSpot = dir === "PUT" ? spot + sPts : spot - sPts;

  const buf = cfg.entryBuf;
  const entryLo = Math.round(spot - buf);
  const entryHi = Math.round(spot + buf);
  // "don't chase" line: beyond this in the trade direction, the move is extended
  const chaseLine = dir === "PUT" ? Math.round(spot - buf * 2) : Math.round(spot + buf * 2);

  const targetINR = Math.round(tPts * DELTA * cfg.lot);
  const slINR = Math.round(sPts * DELTA * cfg.lot);

  return {
    symbol, direction: dir, option: `${strike} ${optType}`,
    entry_zone: `${entryLo}-${entryHi}`,
    dont_chase_beyond: chaseLine,
    target_spot: Math.round(targetSpot), target_pts: Math.round(tPts), target_inr_est: targetINR,
    sl_spot: Math.round(slSpot), sl_pts: Math.round(sPts), sl_inr_est: slINR,
    confidence: s.confidence, regime: s.regime, lot: cfg.lot,
  };
}

const SYSTEM_PROMPT = `You are the personal trading assistant for an NSE/BSE index-options signal engine (NIFTY, BANKNIFTY, SENSEX). You help ONE user — the engine's owner — understand today's signals and turn them into actionable trade plans.

GROUND RULES:
- Answer ONLY from the MARKET CONTEXT provided below. Never invent prices, signals, or levels.
- Be concise and mobile-friendly. Lead with the answer.
- Rupee (Rs) figures are ESTIMATES via a 0.5 ATM delta — always note "approx" for Rs values. Spot levels are exact.
- This is educational, not financial advice.

WHAT THE TIERS MEAN:
- TIER_1 / GREEN  = high conviction (>=85% + TRENDING) — a real "act now" signal.
- TIER_2 / YELLOW = watch only — bias present but not high conviction.
- WAIT / TIER_3   = stand aside, no clean setup.

ALWAYS state the confidence % when discussing any symbol's signal.

HOW TO ANSWER:
- "support / resistance / levels?" -> ALWAYS give levels; NEVER refuse. Two sources, use whichever fits the question (or both):
    * INTRADAY (today): the Resistance/Support lines — call-wall, put-wall, max-pain, day-high/low.
    * CHART-BASED (from previous daily candles), shown in a "CHART LEVELS" block when present: CPR (pivot/TC/BC), floor pivots (R1-R3 / S1-S3 / PP), and 5-day & 10-day range. If the user says "previous charts", "pivot", "CPR", or "based on charts", LEAD with these chart levels. Label each level so the user knows its source.
- "trend?" -> state direction (CALL=bullish, PUT=bearish, WAIT=no clean trade), regime, confidence %, and a one-line read.
- "entry / exit / SL?" ->
    * If there's a TRADE PLAN in context: give it — option (strike+CE/PE), entry zone, don't-chase line, target (spot + approx Rs), stop (spot + approx Rs), and confidence %.
    * If the engine is on WAIT: do NOT just refuse. Instead give a CONDITIONAL plan using the levels:
        - State current bias + confidence %.
        - Resistance to watch (nearest above) and support (nearest below).
        - Conditional entry: "If price breaks ABOVE [resistance] with momentum -> CALL setup; if it breaks BELOW [support] -> PUT setup."
        - Suggested exit: the next level in that direction; stop: just beyond the broken level.
        - Make clear these are levels to WATCH, not a live signal — wait for the break + the engine to confirm (TIER_1/GREEN) before committing real size.
- "status today?" -> per symbol: net move, current signal + confidence %, TIER_1 count.
- History/performance beyond today: not wired up yet (coming soon) — you only have today's data.

SCREENSHOT ANALYSIS (when the user uploads an image):
- The image is the user's LIVE broker screen — an option chain and/or chart. It is the most up-to-date source: read it directly.
- Extract what you can SEE: current spot, the CE/PE strikes and their premiums (LTP) + change, OI, and any chart pattern (higher-highs/lows, breakout, consolidation, waterfall drop in a premium = that side getting crushed).
- Cross-check with the engine MARKET CONTEXT (signal, regime, confidence, support/resistance). If the screenshot and engine agree, conviction is higher; if they disagree, say so.
- Use the REAL premiums from the screenshot for the trade setup (entry/target/SL in Rs) — these are exact, not estimates.
- If the image is unreadable or not a trading screen, say so and ask for a clearer option-chain/chart screenshot.

ALWAYS END WITH A CLEAR VERDICT. Every trade-intent question (and every screenshot) must finish with ONE of:
  ✅ TAKE THE TRADE — then the setup: option (strike + CE/PE), Entry (Rs), Target 1 & 2 (Rs), Stop (Rs), Risk and Reward in Rs (× lot), R:R ratio.
  ⛔ NO TRADE — WAIT — then why (chop / low confidence / extended / engine on WAIT), and the exact condition that would change it (e.g. "if NIFTY closes a 5-min candle above 23,240").
Be decisive but disciplined: prefer ⛔ WAIT unless there is genuine confluence (engine signal + screenshot agree + clean level break). Never scream BUY on a choppy/low-conviction setup — the engine over-fires in chop and capital protection comes first.

DISCIPLINE to include with any ✅ TAKE: 1 lot only, no averaging, hard stop (no exceptions), and exit by 3:00 PM IST regardless.

NEVER fabricate numbers. Use only the engine context values and what is visibly readable in the screenshot.`;

function buildContext(latest, plans, todaySummary) {
  const lines = [];
  lines.push(`Time: ${istClock(istNow().toISOString())} IST | Date: ${istDateStr()}`);
  lines.push("");
  for (const sym of SYMBOLS) {
    const s = latest[sym];
    if (!s) { lines.push(`${sym}: no data today`); continue; }
    const sm = todaySummary[sym] || {};
    lines.push(`=== ${sym} ===`);
    lines.push(`  Current: ${s.signal} ${s.confidence}% conf | tier ${s.tier} | regime ${s.regime} | spot ${Math.round(s.spot)} (as of ${istClock(s.ts)})`);
    if (sm.open != null) {
      const mv = Math.round(s.spot - sm.open);
      lines.push(`  Day: open ${Math.round(sm.open)} -> now ${Math.round(s.spot)} (${mv >= 0 ? "+" : ""}${mv} pts) | TIER_1 signals today: ${sm.tier1 || 0}`);
    }
    // Support/resistance — always available, even on WAIT
    const { res, sup } = keyLevels(s);
    if (res.length) lines.push(`  Resistance (above): ${res.map(([n, v]) => `${Math.round(v)} ${n}`).join(", ")}`);
    if (sup.length) lines.push(`  Support (below):    ${sup.map(([n, v]) => `${Math.round(v)} ${n}`).join(", ")}`);
    if (s.pcr) lines.push(`  PCR: ${s.pcr} (${s.pcr > 1 ? "put-heavy / supportive" : "call-heavy / capped"})`);
    if (s.reasoning) lines.push(`  Engine reasoning: ${s.reasoning}`);
    const p = plans[sym];
    if (p) {
      lines.push(`  TRADE PLAN: ${p.direction} ${p.option}`);
      lines.push(`    Entry zone (spot): ${p.entry_zone}  | don't chase beyond spot ${p.dont_chase_beyond}`);
      lines.push(`    Target: spot ${p.target_spot} (${p.target_pts} pts, approx +Rs ${p.target_inr_est}/lot of ${p.lot})`);
      lines.push(`    Stop:   spot ${p.sl_spot} (${p.sl_pts} pts, approx -Rs ${p.sl_inr_est}/lot)`);
    } else {
      lines.push(`  TRADE PLAN: none — engine is ${s.signal} (no actionable entry).`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

export default async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  // optional quota guard
  const token = process.env.CHAT_TOKEN;
  if (token && req.headers.get("x-chat-token") !== token) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const apiKey = process.env.GOOGLE_API_KEY;
  const sbUrl = process.env.PUBLIC_SUPABASE_URL;
  const sbKey = process.env.PUBLIC_SUPABASE_ANON_KEY;
  if (!apiKey) return Response.json({ error: "GOOGLE_API_KEY not set in Netlify env" }, { status: 500 });
  if (!sbUrl || !sbKey) return Response.json({ error: "Supabase env not set" }, { status: 500 });

  let body;
  try { body = await req.json(); } catch { return Response.json({ error: "bad json" }, { status: 400 }); }

  const messages = Array.isArray(body.messages) ? body.messages.slice(-12) : [];
  if (!messages.length) return Response.json({ error: "no messages" }, { status: 400 });

  // optional screenshot: { mime, data(base64, no prefix) } or a data: URL string
  let image = null;
  if (body.image) {
    if (typeof body.image === "string") {
      const m = body.image.match(/^data:(image\/\w+);base64,(.+)$/s);
      if (m) image = { mime: m[1], data: m[2] };
    } else if (body.image.data) {
      image = { mime: body.image.mime || "image/png", data: body.image.data.replace(/^data:[^,]+,/, "") };
    }
  }

  // ── fetch today's snapshots ────────────────────────────────────────────
  const dateStr = istDateStr();
  const startIso = new Date(`${dateStr}T00:00:00+05:30`).toISOString();

  const latest = {}, todaySummary = {}, plans = {};
  try {
    for (const sym of SYMBOLS) {
      const q = `select=ts,symbol,spot_price,raw_payload`
        + `&symbol=eq.${sym}`
        + `&ts=gte.${encodeURIComponent(startIso)}`
        + `&order=ts.asc`;
      const rows = await sbSelect(sbUrl, sbKey, "snapshots", q);
      if (!rows.length) continue;
      const sigs = rows.map(sigOf);
      // Ignore broken snapshots (spot_price 0 / missing) when picking open & latest.
      const valid = sigs.filter(s => s.spot > 0);
      if (!valid.length) continue;
      latest[sym] = valid[valid.length - 1];
      todaySummary[sym] = {
        open: valid[0].spot,
        tier1: sigs.filter(s => s.tier === "TIER_1" && (s.signal === "CALL" || s.signal === "PUT")).length,
      };
      const p = tradePlan(sym, latest[sym]);
      if (p) plans[sym] = p;
    }
  } catch (e) {
    return Response.json({ error: "supabase read failed: " + e.message }, { status: 500 });
  }

  let context = buildContext(latest, plans, todaySummary);

  // ── Chart-based S/R (CPR + pivots + multi-day range) — only when asked ─────
  const lastUserMsg = [...messages].reverse().find(m => m.role !== "assistant")?.content || "";
  const wantsLevels = /support|resist|\blevel|pivot|\bcpr\b|chart|\bs\/r\b/i.test(lastUserMsg);
  if (wantsLevels) {
    const syms = detectSymbols(lastUserMsg);
    const chunks = [];
    for (const sym of syms) {
      const cl = await fetchChartLevels(sbUrl, sbKey, sym);
      if (cl) chunks.push(formatChartLevels(sym, cl));
    }
    if (chunks.length) context += "\n\n" + chunks.join("\n\n");
  }

  // ── Gemini via raw REST (the @google/genai SDK 401s on Netlify) ────────────
  const contents = messages.map(m => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: [{ text: String(m.content || "") }],
  }));
  // attach the screenshot (if any) to the most recent user turn for vision
  if (image) {
    for (let i = contents.length - 1; i >= 0; i--) {
      if (contents[i].role === "user") {
        contents[i].parts.push({ inline_data: { mime_type: image.mime, data: image.data } });
        break;
      }
    }
  }
  // inject context as a leading user turn so the model always sees fresh data
  contents.unshift({ role: "user", parts: [{ text: "MARKET CONTEXT (live, from the engine):\n" + context }] });
  contents.splice(1, 0, { role: "model", parts: [{ text: "Got the latest market context. What would you like to know?" }] });

  let reply = null, lastErr = null;
  for (const model of MODELS) {
    try {
      reply = await callGemini(apiKey, model, contents, !!image);
      if (reply) break;
    } catch (e) {
      lastErr = e;
      const msg = String(e?.message || e);
      if (msg.includes("404") || msg.includes("NOT_FOUND")) continue; // try next model
      break;
    }
  }

  if (!reply) {
    return Response.json({ error: "gemini failed: " + (lastErr?.message || "no reply") }, { status: 502 });
  }
  return Response.json({ reply });
};
