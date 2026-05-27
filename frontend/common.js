/* Shared helpers used by every page (index.html, day.html, history.html).
 * Exposes a single namespace: window.NSE
 */
(function () {
    "use strict";

    // ─── Constants (mirror notify.py / eod_report.py) ─────────────────
    const DEFAULT_SYMBOL = "NIFTY";
    const SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"];

    // Per-symbol option-chain constants.
    const SYMBOL_CONFIG = {
        NIFTY:     { lot: 75, step: 50,  target: 30,  sl: 15, label: "NIFTY"     },
        BANKNIFTY: { lot: 15, step: 100, target: 60,  sl: 30, label: "BANK NIFTY" },
        SENSEX:    { lot: 20, step: 100, target: 100, sl: 50, label: "SENSEX"    },
    };

    // Legacy aliases (keep until all callsites migrated)
    const SYMBOL = DEFAULT_SYMBOL;
    const LOT_SIZE = SYMBOL_CONFIG.NIFTY.lot;
    const STRIKE_STEP = SYMBOL_CONFIG.NIFTY.step;
    const TARGET_PTS = SYMBOL_CONFIG.NIFTY.target;
    const SL_PTS = SYMBOL_CONFIG.NIFTY.sl;

    const ATM_DELTA = 0.5;
    const BROKERAGE_PER_TRADE = 100;
    const COOLDOWN_MIN = 60;
    const STALE_MIN = 15;

    function cfg(symbol) {
        return SYMBOL_CONFIG[symbol] || SYMBOL_CONFIG[DEFAULT_SYMBOL];
    }

    // ─── Supabase client (lazy init so each page only opens it once) ──
    let _supa = null;
    function supa() {
        if (_supa) return _supa;
        const cfg = window.SUPABASE_CONFIG;
        if (!cfg || !cfg.url || !cfg.anonKey) {
            console.error("Missing SUPABASE_CONFIG. Check config.js.");
            return null;
        }
        _supa = window.supabase.createClient(cfg.url, cfg.anonKey);
        return _supa;
    }

    // ─── Time helpers (all IST) ───────────────────────────────────────
    function nowIST() {
        return new Date(Date.now() + 5.5 * 3600 * 1000);
    }
    function todayDateIST() {
        const d = nowIST();
        return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
    }
    function fmtClock(d) {
        return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} IST`;
    }
    function fmtTime(isoTs) {
        const d = new Date(isoTs);
        const ist = new Date(d.getTime() + 5.5 * 3600 * 1000);
        return `${String(ist.getUTCHours()).padStart(2, "0")}:${String(ist.getUTCMinutes()).padStart(2, "0")}`;
    }
    function fmtDateLong(yyyymmdd) {
        const [y, m, dd] = yyyymmdd.split("-").map(Number);
        const d = new Date(Date.UTC(y, m - 1, dd));
        const dow = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
        const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
        return `${dow} ${dd} ${mon}`;
    }
    function isMarketOpen() {
        const d = nowIST();
        const dow = d.getUTCDay();
        const min = d.getUTCHours() * 60 + d.getUTCMinutes();
        return dow >= 1 && dow <= 5 && min >= 555 && min <= 930;
    }

    // ─── Number formatters ────────────────────────────────────────────
    function fmtINR(n) {
        const sign = n >= 0 ? "+" : "-";
        return `${sign}₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;
    }
    function fmtSpot(n) {
        return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
    }
    function fmtPct(n, digits = 2) {
        const sign = n >= 0 ? "+" : "";
        return `${sign}${n.toFixed(digits)}%`;
    }

    // ─── Engine logic (ports from notify.py) ──────────────────────────
    function tierOf(row) {
        const score = Math.abs(Number(row.score) || 0);
        const conf = Number(row.confidence) || 0;
        const oi = Math.abs(Number(row.oi_score) || 0);
        const reasons = row.reasons || [];
        const contrarian = reasons.some(r => r && (r.includes("Contrarian") || r.includes("Sharp")));
        if (score >= 4 && conf >= 48 && oi >= 2 && !contrarian) return "GREEN";
        if (score >= 3 && conf >= 30 && !contrarian) return "YELLOW";
        return "RED";
    }

    // V3 push-tier classifier: ports app.py:_classify_v3_tier so dashboard
    // knows whether a fired signal would have been auto-pushed (TIER_1) or
    // dashboard-only "watch" (TIER_2).
    // V3.1 Option A tuned values - keep in sync with app.py
    const LATE_PCT_OPEN_V3 = { NIFTY: 0.60, BANKNIFTY: 0.70, SENSEX: 0.60 };
    const LATE_PCT_EXTREME_V3 = { NIFTY: 0.10, BANKNIFTY: 0.10, SENSEX: 0.10 };
    const EXPIRY_DOW_V3 = { NIFTY: 2, BANKNIFTY: 2, SENSEX: 4 };  // JS getDay(): Tue=2, Thu=4

    function pushTierOf(row, symbol = DEFAULT_SYMBOL) {
        const sig = row.signal || "NEUTRAL";
        if (sig === "NEUTRAL") return "TIER_3";

        const score = Number(row.score) || 0;
        const absScore = Math.abs(score);
        const conf = Number(row.confidence) || 0;
        const absOi = Math.abs(Number(row.oi_score) || 0);
        const reasons = row.reasons || [];
        const contrarian = reasons.some(r => r && (r.includes("Contrarian") || r.includes("Sharp")));

        const blocks = [];
        // Gate 1 (V3.1 Option A): lenient -- score>=3 AND no contrarian
        // (OI requirement dropped to catch fresh opening signals like Mon's BN CALL)
        if (absScore < 3) blocks.push("G1");
        if (contrarian) blocks.push("G1");

        // Gate 4: late-entry
        const dayOpen = Number(row.spot_open) || Number(row.spot_price) || 0;
        const dayHigh = Number(row.spot_high) || dayOpen;
        const dayLow = Number(row.spot_low) || dayOpen;
        const spot = Number(row.spot_price) || 0;
        const tsIST = new Date(new Date(row.ts).getTime() + 5.5 * 3600 * 1000);
        const rangePct = dayOpen > 0 ? (dayHigh - dayLow) / dayOpen * 100 : 0;
        const skipG4 = (tsIST.getUTCHours() === 9 && tsIST.getUTCMinutes() < 30) || rangePct < 0.15;
        if (!skipG4 && dayOpen > 0) {
            const pctFromOpen = (spot - dayOpen) / dayOpen * 100;
            const pctBelowHigh = (dayHigh - spot) / dayOpen * 100;
            const pctAboveLow = (spot - dayLow) / dayOpen * 100;
            const lateOpen = LATE_PCT_OPEN_V3[symbol] || 0.30;
            const lateExtreme = LATE_PCT_EXTREME_V3[symbol] || 0.10;
            if (sig === "CALL") {
                if (pctFromOpen > lateOpen) blocks.push("G4");
                else if (pctBelowHigh < lateExtreme && pctBelowHigh < pctAboveLow) blocks.push("G4");
            } else {
                if (pctFromOpen < -lateOpen) blocks.push("G4");
                else if (pctAboveLow < lateExtreme && pctAboveLow < pctBelowHigh) blocks.push("G4");
            }
        }

        // Gate 7: after 14:30 IST late session
        if (tsIST.getUTCHours() > 14 || (tsIST.getUTCHours() === 14 && tsIST.getUTCMinutes() >= 30)) {
            blocks.push("G7");
        }

        // Gate 8: expiry day
        if (tsIST.getUTCDay() === (EXPIRY_DOW_V3[symbol] || 2)) {
            if (absScore < 5) blocks.push("G8");
            if (tsIST.getUTCHours() >= 13) blocks.push("G8");
        }

        // Gate 11: trend must confirm signal direction
        const trendScore = Number(row.trend_score) || 0;
        if (sig === "CALL" && trendScore < 1) blocks.push("G11");
        else if (sig === "PUT" && trendScore > -1) blocks.push("G11");

        // Classify
        if (blocks.length === 0) return "TIER_1";
        const criticalGates = new Set(["G4", "G7", "G8", "G11"]);
        const hasCritical = blocks.some(b => criticalGates.has(b));
        if (hasCritical) return "TIER_3";
        return "TIER_2";
    }
    function recommendStrike(spot, symbol = DEFAULT_SYMBOL) {
        const step = cfg(symbol).step;
        return Math.round(Number(spot) / step) * step;
    }
    function optionType(signal) {
        return signal === "PUT" ? "PE" : "CE";
    }
    function estINR(pts, symbol = DEFAULT_SYMBOL) {
        return Math.round(Math.abs(pts) * ATM_DELTA * cfg(symbol).lot);
    }

    // ─── Day P&L (same rule as eod_report.py, but using snapshots only) ──
    /**
     * Walk snapshots in time order. First non-NEUTRAL = entry. 60-min cooldown.
     * Exit when subsequent spot moves ≥+30 (win) or ≤-15 (loss). Otherwise open.
     * Last snapshot's spot is the proxy "current" for any still-open trade.
     */
    function simulateDay(snaps, symbol = DEFAULT_SYMBOL) {
        const c = cfg(symbol);
        const result = { trades: 0, wins: 0, losses: 0, openCount: 0, netPts: 0, netInr: 0, entries: [] };
        if (!snaps || snaps.length === 0) return result;
        const latestSpot = Number(snaps[snaps.length - 1].spot_price);

        let lastTradeTs = -Infinity;
        for (const s of snaps) {
            if (s.signal === "NEUTRAL") continue;
            const ts = new Date(s.ts).getTime();
            if (ts - lastTradeTs < COOLDOWN_MIN * 60 * 1000) continue;
            lastTradeTs = ts;

            const entry = Number(s.spot_price);
            const move = s.signal === "PUT" ? entry - latestSpot : latestSpot - entry;
            let outcome, pts;
            if (move >= c.target)  { outcome = "WIN";  pts = c.target; }
            else if (move <= -c.sl) { outcome = "LOSS"; pts = -c.sl; }
            else                    { outcome = "OPEN"; pts = move; }

            result.trades += 1;
            if (outcome === "WIN")       { result.wins += 1;   result.netInr += estINR(c.target, symbol); }
            else if (outcome === "LOSS") { result.losses += 1; result.netInr -= estINR(c.sl, symbol); }
            else                          { result.openCount += 1; }
            result.netPts += pts;
            result.entries.push({ ts: s.ts, signal: s.signal, entry, outcome, pts });
        }
        result.netInr -= result.trades * BROKERAGE_PER_TRADE;
        return result;
    }

    // ─── Data fetchers ────────────────────────────────────────────────
    async function fetchLatest(symbol = SYMBOL) {
        const c = supa(); if (!c) return null;
        const { data, error } = await c.from("snapshots")
            .select("ts,signal,score,confidence,trend_score,oi_score,gap_weight,spot_price,reasons")
            .eq("symbol", symbol).order("ts", { ascending: false }).limit(1);
        if (error) { console.warn("fetchLatest:", error); return null; }
        return data && data[0];
    }
    async function fetchDay(dateStr, symbol = SYMBOL) {
        const c = supa(); if (!c) return [];
        const { data, error } = await c.from("snapshots")
            .select("ts,signal,score,confidence,trend_score,oi_score,gap_weight,spot_price,spot_change_pct,spot_open,spot_high,spot_low,entry,target1,stop_loss,reasons")
            .eq("symbol", symbol)
            .gte("ts", `${dateStr}T00:00:00+05:30`)
            .lte("ts", `${dateStr}T23:59:59+05:30`)
            .order("ts", { ascending: true });
        if (error) { console.warn("fetchDay:", error); return []; }
        return data || [];
    }
    /** Fetch distinct dates with snapshots, newest first, up to `limit` days. */
    async function fetchRecentDays(limit = 30, symbol = SYMBOL) {
        const c = supa(); if (!c) return [];
        // Pull last N*40 rows (40 snaps/day max) then group by date
        const { data, error } = await c.from("snapshots")
            .select("ts,signal,spot_price,score")
            .eq("symbol", symbol)
            .order("ts", { ascending: false })
            .limit(limit * 50);
        if (error) { console.warn("fetchRecentDays:", error); return []; }
        const byDate = new Map();
        for (const row of data || []) {
            // Convert UTC ts to IST date
            const d = new Date(row.ts);
            const ist = new Date(d.getTime() + 5.5 * 3600 * 1000);
            const key = `${ist.getUTCFullYear()}-${String(ist.getUTCMonth() + 1).padStart(2, "0")}-${String(ist.getUTCDate()).padStart(2, "0")}`;
            if (!byDate.has(key)) byDate.set(key, []);
            byDate.get(key).push(row);
        }
        const days = [...byDate.entries()]
            .map(([date, rows]) => ({ date, rows: rows.reverse() }))
            .sort((a, b) => b.date.localeCompare(a.date))
            .slice(0, limit);
        return days;
    }

    // ─── Tier UI helpers ──────────────────────────────────────────────
    function tierPillClass(tier) {
        return tier === "GREEN" ? "pill-green"
             : tier === "YELLOW" ? "pill-amber"
             : tier === "RED" ? "pill-rose"
             : "pill-grey";
    }
    function tierStripeClass(tier) {
        return tier === "GREEN" ? "stripe-green"
             : tier === "YELLOW" ? "stripe-amber"
             : tier === "RED" ? "stripe-rose"
             : "stripe-muted";
    }
    function signalEmoji(signal, tier) {
        if (signal === "CALL") return "📈";
        if (signal === "PUT") return "📉";
        return "⏸";
    }

    // ─── Reasons filter (drop noise lines) ────────────────────────────
    function topReasons(reasons, n = 2) {
        return (reasons || [])
            .filter(r => r && !r.includes("Real intraday") && !r.includes("below threshold"))
            .slice(0, n);
    }

    // ─── Active-nav helper ────────────────────────────────────────────
    /** Highlight the nav link matching the current page (by data-page attr). */
    function markActiveNav(pageId) {
        document.querySelectorAll("[data-nav]").forEach(el => {
            const isActive = el.getAttribute("data-nav") === pageId;
            el.classList.toggle("text-blush-600", isActive);
            el.classList.toggle("font-semibold", isActive);
            el.classList.toggle("text-muted", !isActive);
        });
    }

    // ─── Expose namespace ─────────────────────────────────────────────
    window.NSE = {
        // constants
        SYMBOL, LOT_SIZE, STRIKE_STEP, TARGET_PTS, SL_PTS, ATM_DELTA,
        BROKERAGE_PER_TRADE, COOLDOWN_MIN, STALE_MIN,
        // symbol-aware config
        DEFAULT_SYMBOL, SYMBOLS, SYMBOL_CONFIG, cfg,
        // client
        supa,
        // time
        nowIST, todayDateIST, fmtClock, fmtTime, fmtDateLong, isMarketOpen,
        // format
        fmtINR, fmtSpot, fmtPct,
        // engine
        tierOf, pushTierOf, recommendStrike, optionType, estINR, simulateDay, topReasons,
        // ui
        tierPillClass, tierStripeClass, signalEmoji, markActiveNav,
        // data
        fetchLatest, fetchDay, fetchRecentDays,
    };
})();
