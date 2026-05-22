/* history.html — past trading days with summary + drill-in.
 */
(function () {
    "use strict";
    const NSE = window.NSE;
    if (!NSE) { console.error("common.js must load before history.js"); return; }
    const $ = (id) => document.getElementById(id);

    NSE.markActiveNav("history");

    function renderClock() {
        $("clock").textContent = NSE.fmtClock(NSE.nowIST());
        const open = NSE.isMarketOpen();
        $("market-status-text").textContent = open ? "Open" : "Closed";
        const pill = $("market-status");
        const dot = pill.querySelector("span");
        if (open) {
            pill.classList.remove("pill-grey"); pill.classList.add("pill-pink");
            dot.classList.add("live-dot", "bg-blush-500"); dot.classList.remove("bg-gray-400");
        } else {
            pill.classList.remove("pill-pink"); pill.classList.add("pill-grey");
            dot.classList.remove("live-dot", "bg-blush-500"); dot.classList.add("bg-gray-400");
        }
    }

    /** Build per-day summary from raw rows. Days are pre-sorted newest first. */
    function summarize(days) {
        return days.map(({ date, rows }) => {
            if (!rows.length) {
                return { date, count: 0, signals: 0, first: null, last: null, move: 0, movePct: 0, sim: null };
            }
            const first = Number(rows[0].spot_price);
            const last = Number(rows[rows.length - 1].spot_price);
            const move = last - first;
            const movePct = (move / first) * 100;
            const signals = rows.filter(r => r.signal !== "NEUTRAL").length;
            const sim = NSE.simulateDay(rows);
            return { date, count: rows.length, signals, first, last, move, movePct, sim };
        });
    }

    function rollupHtml(summaries) {
        const totalDays = summaries.length;
        const totalTrades = summaries.reduce((s, d) => s + (d.sim?.trades || 0), 0);
        const totalNet = summaries.reduce((s, d) => s + (d.sim?.netInr || 0), 0);
        const totalWins = summaries.reduce((s, d) => s + (d.sim?.wins || 0), 0);
        const totalLosses = summaries.reduce((s, d) => s + (d.sim?.losses || 0), 0);
        const winRate = totalTrades ? (100 * totalWins / totalTrades).toFixed(0) + "%" : "—";

        $("ro-days").textContent = String(totalDays);
        $("ro-trades").textContent = String(totalTrades);
        const pnl = $("ro-pnl");
        if (totalTrades === 0) {
            pnl.textContent = "—";
            pnl.className = "text-sm font-bold stat-mono text-muted";
        } else {
            pnl.textContent = NSE.fmtINR(totalNet);
            pnl.className = "text-sm font-bold stat-mono "
                + (totalNet > 0 ? "text-emerald" : totalNet < 0 ? "text-rose" : "text-muted");
        }
        $("ro-extra").textContent =
            totalTrades > 0
                ? `${totalWins}W / ${totalLosses}L across ${totalDays} day${totalDays === 1 ? "" : "s"} · win rate ${winRate}`
                : `No trades fired across ${totalDays} day${totalDays === 1 ? "" : "s"} yet.`;
    }

    function dayCardHtml(s) {
        const dateLabel = NSE.fmtDateLong(s.date);
        const moveStr = s.count === 0
            ? "—"
            : `${s.move >= 0 ? "+" : ""}${s.move.toFixed(0)} pts · ${NSE.fmtPct(s.movePct)}`;
        const moveClass = s.move > 0 ? "text-emerald" : s.move < 0 ? "text-rose" : "text-muted";

        let signalsPill = `<span class="pill pill-grey">${s.signals || 0} signal${s.signals === 1 ? "" : "s"}</span>`;
        if (s.signals > 0) signalsPill = `<span class="pill pill-pink">${s.signals} signal${s.signals === 1 ? "" : "s"}</span>`;

        let pnlHtml = `<span class="text-sm font-bold text-muted stat-mono">—</span>`;
        if (s.sim && s.sim.trades > 0) {
            const cls = s.sim.netInr > 0 ? "text-emerald" : s.sim.netInr < 0 ? "text-rose" : "text-muted";
            pnlHtml = `<span class="text-sm font-bold stat-mono ${cls}">${NSE.fmtINR(s.sim.netInr)}</span>`;
        }

        const winLossText = s.sim && s.sim.trades > 0
            ? `${s.sim.wins}W / ${s.sim.losses}L${s.sim.openCount ? ` · ${s.sim.openCount} open` : ""}`
            : `${s.count} snapshots`;

        return `
            <a href="day.html?date=${s.date}" class="block card day-card px-4 py-3.5">
                <div class="flex items-center justify-between mb-1.5">
                    <div>
                        <p class="text-sm font-bold text-ink">${dateLabel}</p>
                        <p class="text-[10px] text-muted stat-mono">${s.date}</p>
                    </div>
                    ${signalsPill}
                </div>
                <div class="flex items-center justify-between">
                    <span class="text-xs ${moveClass} stat-mono font-semibold">${moveStr}</span>
                    ${pnlHtml}
                </div>
                <p class="text-[10px] text-muted mt-1">${winLossText}</p>
            </a>
        `;
    }

    function renderDays(summaries) {
        const el = $("days");
        if (!summaries.length) {
            el.innerHTML = `<div class="card px-4 py-6 text-center text-sm text-muted">No history yet.</div>`;
            return;
        }
        el.innerHTML = summaries.map(dayCardHtml).join("");
    }

    async function refresh() {
        renderClock();
        const raw = await NSE.fetchRecentDays(30);  // last 30 days max
        const summaries = summarize(raw);
        rollupHtml(summaries);
        renderDays(summaries);
    }

    refresh();
    setInterval(renderClock, 1000);
    // Re-fetch every 5 min — history doesn't change often
    setInterval(refresh, 5 * 60_000);

    window.__history = { refresh };
})();
