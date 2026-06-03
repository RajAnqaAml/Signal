# Signal Engine — TODO / Roadmap

> Guiding principle: **measure before you tune.** We have ~1.5 days of clean data.
> Don't stack more discipline layers on an unvalidated strategy. Build the one
> obviously-correct fix (position state), then backtest, then tune with numbers.

---

## P0 — Sniper Redesign: "2 clean trades, held properly"

The core bug: the engine has **no concept of holding a position**. It treats every
2-min bar as a fresh entry decision, so one sustained move (e.g. BANKNIFTY
2026-06-03 morning, −322 pts) fires 20+ notifications instead of 1 trade.
A sniper enters once and HOLDS until the thesis breaks.

- [ ] **1. Position state machine** (foundational — do first)
  - Track per symbol: `FLAT` vs `IN_POSITION`
  - While `IN_POSITION`, the only question each bar is "should I EXIT?" — never "enter again?"
  - Kills ~20 of the 23 daily BANKNIFTY notifications on its own
  - Low risk, correct regardless of what backtest says

- [ ] **2. Hold logic** — ride the move
  - While in a position, ignore all continuation signals (no notify, no re-eval of entry)
  - Only watch for the exit condition

- [ ] **3. Exit logic** — thesis break, not noise
  - Exit on: target hit, OR structure break (genuine trend reversal), OR time stop
  - One exit alert → back to FLAT, hunting next setup
  - Consider trailing logic so winners run (don't cap a 800-pt move at a 30-pt target)

- [ ] **4. A+ entry bar** (confluence-gated, self-limiting)
  - Fresh entry needs CONFLUENCE, not just "88% TIER_1":
    EMA + CPR + OC/PCR + structure all aligned, at a key level (range/CPR break),
    clean regime, good time-of-day
  - A high enough bar naturally yields ~2–3 entries/day — the scarcity IS the discipline

- [ ] **5. Daily cap (backstop only)**
  - Soft cap ~2–3 fresh entries/day as a safety net, NOT the primary mechanism
  - Selection approach: **quality bar** (preferred) over session-split or rising-bar

---

## P0 — Validation (do BEFORE tuning the entry bar)

- [ ] **6. Backtest position-state vs current re-fire behaviour**
  - Replay stored snapshot data two ways:
    (a) "hold one position per move" vs (b) current re-fire
  - Answer: does the sniper model actually make more money? On trend days?
  - Today's 3-strike TIER_2 (more trades, tight risk) may have captured MORE of
    BANKNIFTY's 800-pt move than 2 held positions would — verify, don't assume

- [ ] **7. Derive A+ criteria from measured win-rates**
  - Once we have clean data, find which setup features actually predict T1 vs SL
  - Tune the entry bar from numbers, not intuition

- [ ] **8. Get a full clean week of data first**
  - We've never seen the engine run unbroken for 5 sessions
  - Stop adding features until we have a real track record to design against

---

## P1 — Reliability (mostly done, keep watching)

- [x] Model auto-fallback list (gemini-2.5-flash primary) — done 2026-06-02
- [x] Engine health alert on 3+ consecutive NEUTRAL — done 2026-06-02
- [x] Option chain via /api/option-chain-v3 — done
- [ ] **9. Rotate GOOGLE_API_KEY** — was exposed in chat logs; rotate at
      aistudio.google.com and update GitHub Secret
- [ ] **10. SENSEX option-chain** — no NSE index OC (BSE product). Runs blind on OC.
      Decide: leave as-is, or source BSE OC.

---

## P2 — Known limitations / open questions

- [ ] **11. Expiry-day handling** — every Tuesday NIFTY/BANKNIFTY are DTE=0 → never
      TIER_1 by design. Decide if we want a same-day-expiry mode or next-week fallback.
- [ ] **12. P&L realism** — current sim uses spot-points × 0.5 delta × lot. Ignores
      theta decay + delta drift. Real option P&L is worse, esp. on adverse moves.
      Consider modelling premium, not just spot points.
- [ ] **13. Dashboard `push_tier` column** — still derived client-side; consider
      storing push_tier as a top-level Supabase column for cleaner frontend.

---

_Last updated: 2026-06-03_
