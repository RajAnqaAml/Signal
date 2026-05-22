/* day.html — snapshot timeline for a single trading day.
 * If URL has ?date=YYYY-MM-DD, show that day. Otherwise today.
 */
(function () {
    "use strict";
    const NSE = window.NSE;
    if (!NSE) { console.error("common.js must load before day.js"); return; }
    const $ = (id) => document.getElementById(id);

    // ─── Pick the date to display ─────────────────────────────────────
    const params = new URLSearchParams(location.search);
    const dateParam = params.get("date");
    const date = (dateParam && /^\d{4}-\d{2}-\d{2}$/.test(dateParam))
        ? dateParam
        : NSE.todayDateIST();
    const isToday = date === NSE.todayDateIST();

    // ─── Header chrome ────────────────────────────────────────────────
    NSE.markActiveNav(isToday ? "today" : "history");
    $("day-title").textContent = isToday ? "Today" : NSE.fmtDateLong(date);
    $("day-date").textContent = date;
    document.title = `NIFTY · ${isToday ? "Today" : NSE.fmtDateLong(date)}`;

    // ─── Clock + market-status ────────────────────────────────────────
    function renderClock() {
        $("clock").textContent = NSE.fmtClock(NSE.nowIST());
        const open = NSE.isMarketOpen();
        $("market-status-text").textContent = open ? "Open" : "Closed";
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

    // ─── Summary card ─────────────────────────────────────────────────
    function renderSummary(snaps) {
        if (!snaps.length) {
            $("day-sub").textContent = "No data for this date.";
            $("sum-open").textContent = "—";
            $("sum-close").textContent = "—";
            $("sum-move").textContent = "—";
            $("sum-snaps").textContent = "0";
            $("sum-signals").textContent = "0";
            $("sum-pnl").textContent = "—";
            return;
        }
        const first = Number(snaps[0].spot_price);
        const last  = Number(snaps[snaps.length - 1].spot_price);
        const move  = last - first;
        const movePct = (move / first) * 100;
        const nonNeutral = snaps.filter(s => s.signal !== "NEUTRAL").length;
        const r = NSE.simulateDay(snaps);

        $("day-sub").textContent = `${snaps.length} snapshots · session ${NSE.fmtSpot(snaps[0].spot_open || first)} → ${NSE.fmtSpot(last)}`;
        $("sum-open").textContent = NSE.fmtSpot(snaps[0].spot_open || first);
        $("sum-close").textContent = NSE.fmtSpot(last);

        const moveEl = $("sum-move");
        moveEl.textContent = `${move >= 0 ? "+" : ""}${move.toFixed(0)} (${NSE.fmtPct(movePct)})`;
        moveEl.className = "text-sm font-bold stat-mono "
            + (move > 0 ? "text-emerald" : move < 0 ? "text-rose" : "text-ink");

        $("sum-snaps").textContent = String(snaps.length);
        $("sum-signals").textContent = `${nonNeutral}`;

        const pnlEl = $("sum-pnl");
        if (r.trades === 0) {
            pnlEl.textContent = "—";
            pnlEl.className = "text-sm font-bold stat-mono text-muted";
        } else {
            pnlEl.textContent = NSE.fmtINR(r.netInr) + (r.trades ? ` · ${r.wins}W/${r.losses}L` : "");
            pnlEl.className = "text-sm font-bold stat-mono "
                + (r.netInr > 0 ? "text-emerald" : r.netInr < 0 ? "text-rose" : "text-muted");
        }
    }

    // ─── Timeline list ────────────────────────────────────────────────
    function rowHtml(s) {
        const tier = s.signal === "NEUTRAL" ? null : NSE.tierOf(s);
        const tierClass = tier ? "tier-" + (tier === "GREEN" ? "green" : tier === "YELLOW" ? "amber" : "rose") : "";
        const sigBadge = s.signal === "NEUTRAL"
            ? '<span class="pill pill-grey">⏸ neutral</span>'
            : `<span class="pill ${NSE.tierPillClass(tier)}">${s.signal === "PUT" ? "📉 PUT" : "📈 CALL"} · ${tier}</span>`;
        const time = NSE.fmtTime(s.ts);
        const spot = NSE.fmtSpot(s.spot_price);
        const score = (s.score != null) ? Number(s.score).toFixed(2) : "—";
        const reasons = NSE.topReasons(s.reasons, 1);
        const reasonHtml = reasons[0]
            ? `<p class="text-[11px] text-muted mt-1 leading-snug">${escapeHtml(reasons[0])}</p>`
            : "";
        return `
            <div class="card snap-row ${tierClass} px-4 py-3">
                <div class="flex items-center justify-between mb-0.5">
                    <div class="flex items-center gap-2">
                        <span class="text-sm font-semibold stat-mono">${time}</span>
                        <span class="text-sm font-mono text-ink stat-mono">${spot}</span>
                    </div>
                    ${sigBadge}
                </div>
                <div class="flex items-center justify-between mt-1">
                    <span class="text-[10px] text-muted uppercase tracking-wider">score ${score}</span>
                    <span class="text-[10px] text-muted">trend ${formatNum(s.trend_score)} · oi ${formatNum(s.oi_score)} · conf ${Number(s.confidence||0).toFixed(0)}%</span>
                </div>
                ${reasonHtml}
            </div>
        `;
    }
    function formatNum(n) {
        if (n == null) return "—";
        const v = Number(n);
        return (v >= 0 ? "+" : "") + v.toFixed(1);
    }
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    function renderTimeline(snaps) {
        const el = $("timeline");
        if (!snaps.length) {
            el.innerHTML = `<div class="card px-4 py-6 text-center text-sm text-muted">No snapshots recorded.</div>`;
            return;
        }
        // Newest first for scanning
        const reversed = [...snaps].reverse();
        el.innerHTML = reversed.map(rowHtml).join("");
    }

    // ─── Refresh loop (only when showing TODAY) ───────────────────────
    async function refresh() {
        renderClock();
        const snaps = await NSE.fetchDay(date);
        renderSummary(snaps);
        renderTimeline(snaps);
    }

    refresh();
    setInterval(renderClock, 1000);
    if (isToday) {
        setInterval(refresh, NSE.isMarketOpen() ? 60_000 : 5 * 60_000);
    }

    window.__day = { refresh };
})();
