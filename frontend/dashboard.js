/* Landing page (index.html) — "Now" view.
 * Uses helpers from common.js (window.NSE).
 */
(function () {
    "use strict";
    const NSE = window.NSE;
    if (!NSE) { console.error("common.js must load before dashboard.js"); return; }
    const $ = (id) => document.getElementById(id);

    // ─── Hero card render ─────────────────────────────────────────────
    function setStripe(cls) {
        const hero = $("hero");
        hero.classList.remove("stripe-green", "stripe-amber", "stripe-rose", "stripe-muted");
        hero.classList.add(cls);
    }
    function setTierPill(label, cls) {
        const el = $("hero-tier");
        el.classList.remove("pill-green", "pill-amber", "pill-rose", "pill-grey");
        el.classList.add(cls);
        el.textContent = label;
    }
    function showHero() {
        $("hero-skeleton").classList.add("hidden");
        $("hero-content").classList.remove("hidden");
    }

    function renderClosed(lastRow) {
        showHero();
        setStripe("stripe-muted");
        setTierPill("— waiting", "pill-grey");
        $("hero-time").textContent = lastRow ? NSE.fmtTime(lastRow.ts) : "";
        $("hero-action").textContent = "Market Closed";
        $("hero-instrument").textContent = "Reopens Mon 09:15 IST";
        $("hero-levels").classList.add("hidden");
        $("hero-exit-note").classList.add("hidden");
        $("hero-paper-only").classList.add("hidden");
        $("hero-why-text").textContent = "Take it easy — no signals until next trading session.";
    }

    function renderWait(row) {
        showHero();
        setStripe("stripe-muted");
        setTierPill("— waiting", "pill-grey");
        $("hero-time").textContent = NSE.fmtTime(row.ts);
        $("hero-action").textContent = "Wait";
        $("hero-instrument").textContent = `Spot ${NSE.fmtSpot(row.spot_price)} · No setup yet`;
        $("hero-levels").classList.add("hidden");
        $("hero-exit-note").classList.add("hidden");
        $("hero-paper-only").classList.add("hidden");
        const reasons = NSE.topReasons(row.reasons, 2);
        $("hero-why-text").textContent = reasons.length
            ? reasons.join(" · ")
            : "Engine doesn't see a clear direction right now.";
    }

    function renderBuy(row, tier) {
        showHero();
        setStripe(tier === "GREEN" ? "stripe-green" : "stripe-amber");
        setTierPill(
            tier === "GREEN" ? "🟢 GREEN · trade OK" : "🟡 YELLOW · paper only",
            tier === "GREEN" ? "pill-green" : "pill-amber",
        );
        $("hero-time").textContent = NSE.fmtTime(row.ts);

        const dir = row.signal;
        const spot = Number(row.spot_price);
        const strike = NSE.recommendStrike(spot);
        const opt = NSE.optionType(dir);
        const targetSpot = dir === "PUT" ? spot - NSE.TARGET_PTS : spot + NSE.TARGET_PTS;
        const stopSpot   = dir === "PUT" ? spot + NSE.SL_PTS    : spot - NSE.SL_PTS;

        $("hero-action").textContent = "Buy";
        $("hero-instrument").textContent = `NIFTY ${strike} ${opt}`;

        $("hero-levels").classList.remove("hidden");
        $("hero-spot").textContent = NSE.fmtSpot(spot);
        $("hero-target").textContent = NSE.fmtSpot(targetSpot);
        $("hero-target-inr").textContent = NSE.fmtINR(NSE.estINR(NSE.TARGET_PTS));
        $("hero-stop").textContent = NSE.fmtSpot(stopSpot);
        $("hero-stop-inr").textContent = NSE.fmtINR(-NSE.estINR(NSE.SL_PTS));

        $("hero-exit-note").classList.remove("hidden");
        $("hero-paper-only").classList.toggle("hidden", tier !== "YELLOW");

        const reasons = NSE.topReasons(row.reasons, 2);
        $("hero-why-text").textContent = reasons.length
            ? reasons.join(" · ")
            : "Multiple factors align with the signal direction.";
    }

    function renderHero(row) {
        if (!row) return;
        if (!NSE.isMarketOpen()) { renderClosed(row); return; }
        if (row.signal === "NEUTRAL") { renderWait(row); return; }
        renderBuy(row, NSE.tierOf(row));
    }

    // ─── Today's tally card ───────────────────────────────────────────
    function renderToday(snaps) {
        const r = NSE.simulateDay(snaps);
        $("today-date").textContent = NSE.todayDateIST();
        $("today-trades").textContent = `${r.trades} ${r.trades === 1 ? "trade" : "trades"}`;
        if (r.trades === 0) {
            $("today-record").textContent = "—";
            $("today-pnl").textContent = "—";
            $("today-pnl").className = "text-2xl font-extrabold text-muted stat-mono";
        } else {
            $("today-record").textContent =
                `${r.wins}W / ${r.losses}L` + (r.openCount ? ` · ${r.openCount} open` : "");
            $("today-pnl").textContent = NSE.fmtINR(r.netInr);
            $("today-pnl").className = "text-2xl font-extrabold stat-mono "
                + (r.netInr > 0 ? "text-emerald" : r.netInr < 0 ? "text-rose" : "text-muted");
        }
    }

    // ─── Clock & market-status pill ───────────────────────────────────
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

    // ─── Stale-data banner + footer "last update" ─────────────────────
    function renderStale(lastTs) {
        const ageMin = lastTs ? (Date.now() - new Date(lastTs).getTime()) / 60000 : null;
        const banner = $("stale-banner");
        if (ageMin !== null && ageMin > NSE.STALE_MIN && NSE.isMarketOpen()) {
            banner.classList.remove("hidden");
            $("stale-banner-text").textContent =
                `Data may be stale — last update was ${Math.round(ageMin)} min ago`;
        } else {
            banner.classList.add("hidden");
        }
        $("last-update").textContent = lastTs ? NSE.fmtTime(lastTs) + " IST" : "—";
        $("last-update-age").textContent = lastTs
            ? `(${Math.max(0, Math.round(ageMin))} min ago)` : "";
    }

    // ─── Refresh loop ─────────────────────────────────────────────────
    async function refresh() {
        renderClock();
        const [latest, today] = await Promise.all([
            NSE.fetchLatest(),
            NSE.fetchDay(NSE.todayDateIST()),
        ]);
        if (latest) { renderHero(latest); renderStale(latest.ts); } else { renderStale(null); }
        renderToday(today);
    }

    // ─── Boot ─────────────────────────────────────────────────────────
    NSE.markActiveNav("now");
    refresh();
    setInterval(renderClock, 1000);
    setInterval(refresh, NSE.isMarketOpen() ? 60_000 : 5 * 60_000);

    window.__dashboard = { renderHero, renderToday, refresh };
})();
