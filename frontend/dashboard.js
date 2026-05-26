/* Landing page (index.html) - "Now" view.
 * Renders TWO hero cards (NIFTY + BANKNIFTY) and a two-row Today tally.
 * Uses helpers from common.js (window.NSE).
 */
(function () {
    "use strict";
    const NSE = window.NSE;
    if (!NSE) { console.error("common.js must load before dashboard.js"); return; }
    const $ = (id) => document.getElementById(id);

    // Each hero card has the same DOM structure with different id prefixes.
    // For NIFTY:     prefix = "hero"        (legacy ids: hero-tier, hero-time, ...)
    // For BANKNIFTY: prefix = "hero-bn"     (ids: hero-bn-tier, hero-bn-time, ...)
    // Today tally NIFTY: "today-*"     BN: "today-bn-*"
    const HERO_PREFIX = { NIFTY: "hero", BANKNIFTY: "hero-bn" };
    const TODAY_PREFIX = { NIFTY: "today", BANKNIFTY: "today-bn" };

    function el(prefix, suffix) {
        return $(suffix ? `${prefix}-${suffix}` : prefix);
    }

    // --- Hero card renderers (parameterized by symbol/prefix) -----------
    function setStripe(prefix, cls) {
        const hero = $(prefix);
        hero.classList.remove("stripe-green", "stripe-amber", "stripe-rose", "stripe-muted");
        hero.classList.add(cls);
    }
    function setTierPill(prefix, label, cls) {
        const e = el(prefix, "tier");
        e.classList.remove("pill-green", "pill-amber", "pill-rose", "pill-grey");
        e.classList.add(cls);
        e.textContent = label;
    }
    function showHero(prefix) {
        el(prefix, "skeleton").classList.add("hidden");
        el(prefix, "content").classList.remove("hidden");
    }

    function renderClosed(prefix, lastRow, symbol) {
        showHero(prefix);
        setStripe(prefix, "stripe-muted");
        setTierPill(prefix, "— waiting", "pill-grey");
        el(prefix, "time").textContent = lastRow ? NSE.fmtTime(lastRow.ts) : "";
        el(prefix, "action").textContent = "Market Closed";
        el(prefix, "instrument").textContent = "Reopens Mon 09:15 IST";
        el(prefix, "levels").classList.add("hidden");
        el(prefix, "exit-note").classList.add("hidden");
        el(prefix, "paper-only").classList.add("hidden");
        el(prefix, "why-text").textContent = "Take it easy — no signals until next trading session.";
    }

    function renderWait(prefix, row, symbol) {
        showHero(prefix);
        setStripe(prefix, "stripe-muted");
        setTierPill(prefix, "— waiting", "pill-grey");
        el(prefix, "time").textContent = NSE.fmtTime(row.ts);
        el(prefix, "action").textContent = "Wait";
        el(prefix, "instrument").textContent = `Spot ${NSE.fmtSpot(row.spot_price)} · No setup yet`;
        el(prefix, "levels").classList.add("hidden");
        el(prefix, "exit-note").classList.add("hidden");
        el(prefix, "paper-only").classList.add("hidden");
        const reasons = NSE.topReasons(row.reasons, 2);
        el(prefix, "why-text").textContent = reasons.length
            ? reasons.join(" · ")
            : "Engine doesn't see a clear direction right now.";
    }

    function renderBuy(prefix, row, tier, symbol) {
        const cfg = NSE.cfg(symbol);
        // V3: classify into Tier 1 (auto-push, BUY action) vs Tier 2 (WATCH only)
        const pushTier = NSE.pushTierOf(row, symbol);
        const isAutoPush = pushTier === "TIER_1";

        showHero(prefix);

        // Stripe + tier pill: Tier 1 -> green/amber based on quality; Tier 2 -> muted pink
        if (isAutoPush) {
            setStripe(prefix, tier === "GREEN" ? "stripe-green" : "stripe-amber");
            setTierPill(prefix,
                tier === "GREEN" ? "🟢 BUY · trade OK" : "🟡 BUY · paper only",
                tier === "GREEN" ? "pill-green" : "pill-amber",
            );
        } else {
            setStripe(prefix, "stripe-muted");
            setTierPill(prefix, "👀 WATCH · low conviction", "pill-pink");
        }

        el(prefix, "time").textContent = NSE.fmtTime(row.ts);

        const dir = row.signal;
        const spot = Number(row.spot_price);
        const strike = NSE.recommendStrike(spot, symbol);
        const opt = NSE.optionType(dir);
        const targetSpot = dir === "PUT" ? spot - cfg.target : spot + cfg.target;
        const stopSpot   = dir === "PUT" ? spot + cfg.sl     : spot - cfg.sl;

        el(prefix, "action").textContent = isAutoPush ? "Buy" : "Watch";
        el(prefix, "instrument").textContent = isAutoPush
            ? `${cfg.label} ${strike} ${opt}`
            : `${cfg.label} ${strike} ${opt} (manual decision)`;

        el(prefix, "levels").classList.remove("hidden");
        el(prefix, "spot").textContent = NSE.fmtSpot(spot);
        el(prefix, "target").textContent = NSE.fmtSpot(targetSpot);
        el(prefix, "target-inr").textContent = NSE.fmtINR(NSE.estINR(cfg.target, symbol));
        el(prefix, "stop").textContent = NSE.fmtSpot(stopSpot);
        el(prefix, "stop-inr").textContent = NSE.fmtINR(-NSE.estINR(cfg.sl, symbol));

        el(prefix, "exit-note").classList.remove("hidden");
        el(prefix, "paper-only").classList.toggle("hidden", tier !== "YELLOW");

        const reasons = NSE.topReasons(row.reasons, 2);
        el(prefix, "why-text").textContent = reasons.length
            ? reasons.join(" · ")
            : "Multiple factors align with the signal direction.";
    }

    function renderHero(symbol, row) {
        const prefix = HERO_PREFIX[symbol];
        if (!row) return;
        if (!NSE.isMarketOpen()) { renderClosed(prefix, row, symbol); return; }
        if (row.signal === "NEUTRAL") { renderWait(prefix, row, symbol); return; }
        renderBuy(prefix, row, NSE.tierOf(row), symbol);
    }

    // --- Today tally renderer -------------------------------------------
    function renderTodayOne(symbol, snaps) {
        const prefix = TODAY_PREFIX[symbol];
        const r = NSE.simulateDay(snaps, symbol);
        el(prefix, "trades").textContent = `${r.trades} ${r.trades === 1 ? "trade" : "trades"}`;
        if (r.trades === 0) {
            el(prefix, "record").textContent = "—";
            el(prefix, "pnl").textContent = "—";
            el(prefix, "pnl").className = "text-xl font-extrabold text-muted stat-mono";
        } else {
            el(prefix, "record").textContent =
                `${r.wins}W / ${r.losses}L` + (r.openCount ? ` · ${r.openCount} open` : "");
            el(prefix, "pnl").textContent = NSE.fmtINR(r.netInr);
            el(prefix, "pnl").className = "text-xl font-extrabold stat-mono "
                + (r.netInr > 0 ? "text-emerald" : r.netInr < 0 ? "text-rose" : "text-muted");
        }
    }

    function renderToday(dataBySymbol) {
        $("today-date").textContent = NSE.todayDateIST();
        renderTodayOne("NIFTY", dataBySymbol.NIFTY);
        renderTodayOne("BANKNIFTY", dataBySymbol.BANKNIFTY);
    }

    // --- Clock & market-status pill -------------------------------------
    function renderClock() {
        const now = NSE.nowIST();
        $("clock").textContent = NSE.fmtClock(now);
        const open = NSE.isMarketOpen();
        $("market-status-text").textContent = open ? "Market Open" : "Market Closed";
        const pill = $("market-status");
        const dot = pill.querySelector("span");
        if (open) {
            pill.classList.remove("pill-grey"); pill.classList.add("pill-pink");
            dot.classList.add("live-dot", "bg-blush-500");
            dot.classList.remove("bg-gray-400");
        } else {
            pill.classList.remove("pill-pink"); pill.classList.add("pill-grey");
            dot.classList.remove("live-dot", "bg-blush-500");
            dot.classList.add("bg-gray-400");
        }
    }

    // --- Stale-data banner + footer "last update" -----------------------
    // Uses whichever symbol has the more recent snapshot
    function renderStale(latestTs) {
        const ageMin = latestTs ? (Date.now() - new Date(latestTs).getTime()) / 60000 : null;
        const banner = $("stale-banner");
        if (ageMin !== null && ageMin > NSE.STALE_MIN && NSE.isMarketOpen()) {
            banner.classList.remove("hidden");
            $("stale-banner-text").textContent =
                `Data may be stale — last update was ${Math.round(ageMin)} min ago`;
        } else {
            banner.classList.add("hidden");
        }
        $("last-update").textContent = latestTs ? NSE.fmtTime(latestTs) + " IST" : "—";
        $("last-update-age").textContent = latestTs
            ? `(${Math.max(0, Math.round(ageMin))} min ago)` : "";
    }

    // --- Refresh loop ---------------------------------------------------
    async function refresh() {
        renderClock();
        const today = NSE.todayDateIST();
        const [niftyLatest, bnLatest, niftyDay, bnDay] = await Promise.all([
            NSE.fetchLatest("NIFTY"),
            NSE.fetchLatest("BANKNIFTY"),
            NSE.fetchDay(today, "NIFTY"),
            NSE.fetchDay(today, "BANKNIFTY"),
        ]);
        renderHero("NIFTY", niftyLatest);
        renderHero("BANKNIFTY", bnLatest);
        renderToday({ NIFTY: niftyDay, BANKNIFTY: bnDay });

        // Stale banner: use whichever symbol is most recent
        const latestTs = [niftyLatest, bnLatest]
            .filter(r => r && r.ts)
            .map(r => r.ts)
            .sort()
            .pop() || null;
        renderStale(latestTs);
    }

    // --- Boot -----------------------------------------------------------
    NSE.markActiveNav("now");
    refresh();
    setInterval(renderClock, 1000);
    setInterval(refresh, NSE.isMarketOpen() ? 60_000 : 5 * 60_000);

    window.__dashboard = { renderHero, renderToday, refresh };
})();
