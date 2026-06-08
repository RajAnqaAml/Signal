# Signal Engine — TODO / Roadmap

> Guiding principle: **measure before you tune.** Build the obviously-correct
> fixes (premium pricing, position state), then backtest, then tune with numbers.

---

## P0 — Premium-priced signals: Entry / T1 / T2 / SL in ₹ (TOP PRIORITY)

Every signal must read like a real trade ticket, in OPTION PREMIUM (₹), not just
spot points. Target format (user-approved):

```
Setup: BUY CE 23300  (breakout continuation)
  Entry     ₹95–96 (market)
  Target 1  ₹108–110  (spot ~23,280)
  Target 2  ₹120+     (spot ~23,320)
  Stop Loss ₹85       (spot back below 23,210)
  Risk      ~₹10 × 65 = ₹650
  Reward T1 ~₹13 × 65 = ₹845
  R:R       ~1:1.3
```

- [ ] **A. Store option premiums in each snapshot** (recorder/db) — GATING STEP
  - `/api/option-chain-v3` already returns per-strike LTP; we just don't store it
  - Capture ATM ± 2 strikes' CE/PE: lastPrice + change + (optional) bid/ask
  - Add to the snapshot payload + a Supabase column; let it collect going forward

- [ ] **B. Compute premium-based Entry / T1 / T2 / SL**
  - Entry  = current premium of the recommended strike (a small zone, ±1–2)
  - T1     = premium at target1 spot (via delta), T2 = premium at a further target
  - SL     = premium at stop_loss spot
  - Risk/Reward in ₹ (× lot), R:R ratio — exactly like the ticket above

- [ ] **C. Surface it everywhere**
  - Notification (ntfy): full ₹ ticket, not spot-only
  - Dashboard hero card: Entry/T1/T2/SL in ₹
  - Chat bot: same ticket on "entry?" and on screenshot verdicts

- [ ] **D. Honesty guard** — premium T1/T2/SL are delta-ESTIMATES (theta/IV drift).
  Label them "approx"; the live screenshot premiums (vision) are the exact source.

---

## P0 — "Day character" call (addresses the 2026-06-08 chop frustration)

On choppy/rangebound days the engine emits scattered CALL/PUT signals and *looks*
confused. It should instead decide ONCE, clearly.

- [ ] **E. Daily regime verdict** — compare intraday range vs net move + flip count.
  Say plainly: "Trending day — trade it" vs "Rangebound/chop — stand aside."
  Stop emitting contradictory per-bar signals on a sideways day.
  (2026-06-08: NIFTY +53 net but 156 range; SENSEX 9 CALL↔PUT flips = noise.)

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
- [ ] **9. Rotate GOOGLE_API_KEY** — STILL the leaked key (it kept working = not
      rotated). Rotate at aistudio.google.com; update .env + Netlify + GitHub Secret.
- [ ] **10. SENSEX option-chain** — no NSE index OC (BSE product). Runs blind on OC.
      Decide: leave as-is, or source BSE OC.
- [ ] **11. Deployment: Netlify credit hit** — evaluate Cloudflare Pages (best free
      tier; our function is Web-standard fetch, ports easily) or Supabase Edge Functions.

---

## P1 — Chat assistant (SHIPPED — keep extending)

- [x] Netlify function + chat page, grounded in Supabase snapshots
- [x] Gemini via raw REST (the @google/genai SDK 401s on Netlify)
- [x] Trend / status / entry-exit answers; conditional plan on WAIT
- [x] Support/resistance: intraday (OC walls) + chart-based (CPR, floor pivots, range)
- [x] Screenshot upload (vision) + clear ✅ TAKE / ⛔ NO-TRADE verdict
- [ ] **12. Premium ticket in bot** — once P0-B lands, give full ₹ Entry/T1/T2/SL
- [ ] **13. History/performance tools** — "how did last week go?", win-rate (needs
      a daily-summary table written by the cron)
- [ ] **14. Optional CHAT_TOKEN PIN** in Netlify to stop quota abuse of the endpoint

---

## P2 — Known limitations / open questions

- [ ] **15. Expiry-day handling** — every Tuesday NIFTY/BANKNIFTY are DTE=0 → never
      TIER_1 by design. Decide if we want a same-day-expiry mode or next-week fallback.
- [ ] **16. P&L realism** — once real premiums are stored (P0-A), replace the
      spot-points × 0.5 delta sim with actual premium P&L (captures theta/IV).
- [ ] **17. Dashboard `push_tier` column** — still derived client-side; consider
      storing push_tier as a top-level Supabase column for cleaner frontend.

---

_Last updated: 2026-06-08_
