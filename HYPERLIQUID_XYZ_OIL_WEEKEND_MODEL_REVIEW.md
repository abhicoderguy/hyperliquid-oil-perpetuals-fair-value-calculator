# Hyperliquid / trade.xyz Oil Weekend Model Review

## Executive Summary

The April 10-13, 2026 `xyz:CL` weekend did **not** behave like a frozen-oracle carry trade.

The best-supported reading of the docs and the data is:

- `externalPrice` freezes when the CME-derived reference is closed.
- `oraclePx` does **not** freeze with it. It keeps moving during off-hours via the platform's internal oracle process.
- For the April roll, the Friday Apr 10 `5:30 PM ET` shift to `40/60` did **not** become a live external reference on Friday night. It became live when external pricing resumed on **Sunday at 6:00 PM ET**.
- The old weekend treatment therefore overstated how much of the apparent gap was collectible roll-down.

That does **not** mean the weekend trade necessarily lost money.

For a realistic portfolio of:

- short `xyz:CL`
- long `0.4 * CLK6 + 0.6 * CLM6`
- CME hedge frozen while CME was shut

the realized path was roughly:

- `+0.50` per unit by Sunday reopen
- `+0.16` per unit by Tuesday `03:00 UTC`

But that outcome came from hedge gap PnL plus funding path, not from harvesting a deterministic frozen-oracle weekend step-down.

The original trade thesis therefore survives only in a narrower form:

- the **on-hours** roll mechanics are real
- the **weekend frozen-oracle** framing is not

## The Original Model

The prior public oil model treated `xyz:CL` as a rolling CME basket and decomposed:

```text
total spread = baseline basis + roll premium
fair mark    = live oracle - fair spread
```

with:

- a fixed `tau = 16h`
- `20%` daily roll steps
- shift times at `5:30 PM ET`
- a weekend treatment that implicitly behaved like stale CME prices plus deterministic roll carry

That framework was useful for the live roll window itself, but it left one weekend question underspecified:

> what exactly is the reference price when CME is closed?

That is the question this review answers.

## What The Docs Actually Say

### 1. Commodities use a 23/5 external reference

The `trade.xyz` commodities docs say industrial metals and energy commodities use designated futures contracts as the external source, with external coverage:

- Sunday `6:00 PM ET` to Friday `5:00 PM ET`
- daily gaps from `5:00 PM ET` to `6:00 PM ET`

For WTI:

- month-start April 2026 active pair: `CLK6 -> CLM6`
- roll window: 5th-10th business day
- daily weights: `100/0 -> 80/20 -> 60/40 -> 40/60 -> 20/80 -> 0/100`

### 2. The roll step is scheduled at 5:30 PM ET, but live external weights resume at 6:00 PM ET

The roll-schedule docs explicitly say:

- updated weightings take effect when the oracle switches back to external pricing at `6:00 PM ET`

That detail matters enormously on Fridays.

From Monday to Thursday:

- `5:30 PM ET` shift happens during the `5:00-6:00 PM ET` maintenance window
- the new weight goes live at `6:00 PM ET` the same evening

On Friday:

- the market closes to external pricing at `5:00 PM ET`
- the `5:30 PM ET` shift is still scheduled
- but there is **no Friday 6:00 PM ET external session**
- external pricing resumes only on **Sunday at 6:00 PM ET**

So for Apr 10, 2026:

- the `40/60` weight was a **latent weekend state**
- the live external `40/60` reference first mattered at **Sunday 6:00 PM ET**

### 3. `externalPrice` freezes, `oraclePx` does not

The `trade.xyz` external price docs say:

- when markets are open, `externalPrice = oraclePx`
- when markets are closed, `externalPrice` remains fixed at the last external close
- while that happens, `oraclePx` advances via the internal pricing mechanism

That is the central correction.

The weekend question is **not**:

> does the CME basket freeze?

It does.

The real question is:

> does the market's actual oracle freeze with it?

The docs say no.

### 4. Off-hours oracle uses an internal EMA-style update

The `trade.xyz` oracle docs describe an internal continuous-time EMA process when external inputs are unavailable.

In words:

- start from the last live external price
- measure order-book pressure through impact prices
- move the oracle part of the way toward that internally discovered level
- revert to live external pricing once external inputs resume

The oracle page documents this as a continuous-time EMA rule with a `tau = 1 hour` internal oracle time constant.

Important nuance:

- the trade.xyz changelog explicitly frames the `8h -> 1h` time-constant change in equity-perp terms
- the commodity docs do not separately restate a commodity-specific constant

So the strictest statement is:

- the docs clearly support a **fast internal EMA-style off-hours oracle**
- the best-documented parameter is `tau = 1 hour`
- the oil weekend data clearly reject an infinite-time-constant / frozen-oracle interpretation

### 5. Discovery bounds constrain weekend price discovery

For `WTIOIL` the docs specify:

- discovery bound: `+-5%`
- resets: `2`

So weekend price discovery is real, but bounded and ratcheted.

### 6. Funding docs are directionally right, but the premium definition needs care

The `trade.xyz` funding page gives:

```text
Funding Rate XYZ = 0.5 * [P + clamp(r - P, -0.0005, 0.0005)]
```

with hourly funding.

But there is an important implementation nuance:

- Hyperliquid's HIP-3 docs define a more responsive premium for builder-deployed perps:

```text
premium = (0.5 * (impact_bid + impact_ask) / oracle) - 1
```

- live `xyz:CL` API context matches the HIP-3 premium formula essentially exactly
- it does **not** match the one-sided impact-price-difference premium formula nearly as well

For this review, the practical takeaway is:

- use the historical API `premium` directly
- treat the live `xyz:CL` market as behaving like a HIP-3 premium process

## What The Apr 10-13 Data Says

## The Frozen-Oracle Hypothesis Fails

The cleanest falsification is the historical `premium` itself.

If the weekend oracle had been frozen to Friday's pre-close `60/40` basket, or even to a stale `40/60` basket after the scheduled shift, then the observed premium signs should line up with those frozen references.

They do not.

Using the actual historical `xyz:CL` premium from `fundingHistory`:

| Time UTC | `xyz:CL` close | Actual premium | Premium if frozen `60/40` | Premium if frozen stale `40/60` | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| `2026-04-10 22:00` | `91.90` | `-0.80%` | `-1.21%` | `+0.20%` | stale `40/60` already wrong |
| `2026-04-11 00:00` | `91.85` | `-0.06%` | `-1.26%` | `+0.15%` | both frozen variants wrong in magnitude, stale `40/60` wrong in sign |
| `2026-04-12 00:00` | `90.01` | `+0.30%` | `-3.24%` | `-1.86%` | frozen oracle impossible |
| `2026-04-12 22:00` | `99.07` | `+0.26%` | `+6.50%` | `+8.02%` | stale reference massively wrong |

The key row is `2026-04-12 00:00 UTC`.

At that point:

- the market traded around `90.01`
- the actual API premium was **positive**

Against either frozen Friday reference, the premium should have been clearly **negative**.

That alone rejects the frozen-oracle assumption.

## Friday 5:30 PM ET did not produce the old deterministic weekend jump

The minute candles reinforce the same conclusion.

Around the Friday scheduled roll timestamp:

- `21:29 UTC`: `91.870`
- `21:30 UTC`: `91.864`

No deterministic external-style step happened there.

But around the Sunday reopen:

- `21:59 UTC`: `97.871`
- `22:00 UTC`: `98.885`

That is where the market re-anchored in size, which is exactly what you would expect if:

- the Friday `40/60` shift stayed latent through the weekend
- the live external basket returned only when CME-derived external pricing resumed

## The actual Sunday 40/60 basket was nowhere near the stale Friday one

Using Databento minute closes:

- Friday pre-close live `60/40` basket: `93.022`
- stale Friday `40/60` basket: `91.718`
- actual Sunday reopen live `40/60` basket: `99.380`

So the stale-vs-live error at Sunday reopen was:

```text
99.380 - 91.718 = +7.662
```

That is the single biggest numerical reason the old weekend framing broke.

The deterministic weekend carry everyone wanted to harvest was about `-1.304` from the weight shift alone.

The actual Sunday `40/60` basket moved **up** by `+7.662` relative to the stale Friday `40/60` level.

## What The Corrected Weekend Model Is

The corrected model is regime-switching.

### On-hours regime

When external markets are open:

```text
externalPrice_t = w_t * CLK6_t + (1 - w_t) * CLM6_t
oracle_t        = externalPrice_t
```

where `w_t` is the live external roll weight.

### Off-hours regime

When external markets are closed:

```text
externalPrice_t = externalPrice_close
oracle_t        = internal EMA / impact-price process
mark_t          = function(oracle_t, basis EMA, order-book median)
```

So the old frozen-reference gap must be decomposed as:

```text
frozen_external - market
    = (oracle - market) + (frozen_external - oracle)
```

That second term, `frozen_external - oracle`, is the missing weekend component.

It is not collectible basis.

It is oracle drift.

### The crucial Apr 10-13 timeline

The correct chronology for this specific weekend is:

1. Friday pre-close:
   - live external basket is still `60/40`
2. Friday `5:30 PM ET`:
   - `40/60` shift is scheduled
   - but it does not become a live external price yet
3. Friday night through Sunday pre-open:
   - `externalPrice` is fixed at the Friday close
   - `oraclePx` moves internally with the weekend order book
4. Sunday `6:00 PM ET`:
   - external pricing resumes
   - the live basket is now `40/60`
   - the basket uses **current Sunday CME prices**, not stale Friday closes
5. Monday `5:30 PM ET`:
   - next shift to `20/80` is scheduled during the maintenance hour
6. Monday `6:00 PM ET`:
   - `20/80` becomes the live external basket

## What This Means Economically

The old model treated the weekend like a deterministic carry problem.

The corrected model says the weekend is partly a carry problem and partly a **state-dependent internal-oracle problem**.

That changes the economic meaning of the hedge.

If you are:

- short `xyz:CL`
- long CME
- and CME is closed

then over the weekend you are not just harvesting a pre-programmed oracle roll.

You are also holding exposure to the path of the internal oracle.

That is why the funding feels like capped long delta.

If oil rips higher over the weekend:

- your long CME hedge will benefit when CME reopens
- but the weekend internal oracle may chase the move higher
- so the perp does not stay artificially cheap relative to a frozen Friday reference
- the basis you thought you would collect gets amortized away inside the oracle itself

That is exactly the intuition behind the funding comment in the prompt, and the Apr 10-13 path validates it.

## Hedged Portfolio Review

## Portfolio Definition

The minimum realistic review portfolio was:

- short `1.0` `xyz:CL`
- long `0.4 * CLK6 + 0.6 * CLM6`
- hedge set at the Friday close, matching the **post-Apr-10-shift** weight
- CME hedge frozen while CME was shut
- actual Hyperliquid hourly funding applied

Funding uses an oracle notional. Because historical `oraclePx` is not exposed for `xyz:CL`, the review uses a transparent proxy from the historical premium and price path. That proxy is sufficient for this exercise because:

- the frozen/not-frozen verdict already comes directly from the premium signs
- the funding contribution is second-order relative to the reopening basket move

## Cumulative PnL checkpoints

| Checkpoint | Perp MTM | CME hedge | Funding | Net |
| --- | ---: | ---: | ---: | ---: |
| Sunday reopen | `-7.385` | `+7.662` | `+0.226` | `+0.503` |
| Monday close | `-1.617` | `+2.632` | `-0.829` | `+0.186` |
| Monday reopen | `-1.562` | `+2.712` | `-0.955` | `+0.195` |
| Tuesday `03:00 UTC` | `-0.759` | `+1.936` | `-1.017` | `+0.160` |

## Segment view

### Friday close -> Sunday reopen

This is the pure weekend segment:

- perp short loses `7.385`
- CME hedge gains `7.662`
- funding adds `0.226`
- net is `+0.503`

That is the headline path.

The trade was not a blow-up.
But the gain did **not** come from a clean frozen-oracle roll-down.

It came from:

- reopening hedge PnL
- plus a modest weekend funding credit

### Sunday reopen -> Monday close

This is where the post-reopen drag shows up:

- the short perp gains back `5.768`
- the hedge gives up `5.030`
- funding costs another `1.055`
- net segment PnL is `-0.317`

So the weekend credit was not a clean free lunch. Once the market normalized into the reopened session, funding became a drag again.

### Monday close -> Monday reopen

This is the next roll-maintenance window:

- small positive hedge/perp offset
- small additional funding drag
- net roughly flat

That is consistent with the corrected framing:

- Monday `20/80` does not become live at `5:30 PM ET`
- it becomes live when external pricing resumes at `6:00 PM ET`

## Old Model vs Corrected Model

## Where the old model was right

The old model was directionally right about one thing:

- during on-hours, `xyz:CL` really is tied to a rolling CME basket
- roll timing really matters
- deterministic scheduled shifts are real once the external reference is live

That part survives.

## Where the old model was wrong

The old model was wrong about the weekend reference process.

Specifically, it treated the weekend as if the relevant oracle state were effectively:

- frozen CME closes
- plus deterministic scheduled roll carry

That is not how the market actually behaved.

The missing state variable was the off-hours internal oracle.

## Error decomposition

The weekend error came from three places, in order of importance.

### 1. Calendar / curve repricing at Sunday reopen

This was the dominant error.

The Sunday live `40/60` basket was `+7.662` above the stale Friday `40/60` basket.

That completely dwarfed the old deterministic `-1.304` step everyone was focused on.

### 2. Weekend internal-oracle drift

Even before Sunday reopen, the apparent gap to frozen Friday references was not real spread.

By Saturday night, most of it had become oracle drift.

That means a trader staring at a frozen Friday anchor would think they still owned large basis, when in reality the market's internal reference had already moved.

### 3. Baseline basis

Baseline basis still existed, but it was second-order relative to:

- the weekend oracle drift
- and the massive Sunday curve move

## Did the old model produce the wrong signal?

For the actual weekend, yes in the only sense that matters:

- it did not provide a stable, reliable weekend signal

Using frozen references, the sign of the "edge" became model-choice dependent:

- frozen `60/40` and stale `40/60` gave different stories on Friday night
- both failed badly by Sunday

By `2026-04-12 00:00 UTC`, the actual premium had flipped **positive**, while both frozen variants still implied a negative premium.

Once that happens, the frozen-oracle signal is no longer just noisy.

It is wrong.

## How To Think About This Market Going Forward

The right mental model is:

### On-hours

Use the roll-aware CME basket model.

### Off-hours

Do **not** treat the market as a stale CME basket with automatic carry.

Instead treat it as:

- frozen `externalPrice`
- live internal `oraclePx`
- bounded but real weekend discovery
- path-dependent funding

That means a proper weekend framework should separate:

```text
observed gap to frozen external
    = residual market spread to live oracle
    + internal-oracle drift
```

and it should treat the weekend oracle as endogenous to order flow, not predetermined by the stale Friday curve.

## Practical Recommendation

For future oil weekend work:

- keep the old roll model for on-hours roll analysis
- drop the frozen-oracle weekend assumption
- treat Friday `5:30 PM ET` shifts as latent until the next `6:00 PM ET` external session
- explicitly scenario-test weekend internal-oracle paths instead of pretending there is one deterministic fair curve
- use live weekend `premium`, `funding`, and `oraclePx` where available as state variables, not just weekend CME stale marks

## Should You Hold The Weekend Or Exit Friday?

The practical decision rule is not:

> can I build a better weekend model?

It is:

> is the extra expected weekend PnL large enough to justify the extra model risk?

For a cleaner like-for-like comparison, I ran a second path test starting from the first live April roll step:

- entry: `2026-04-08 22:00 UTC`
- short `xyz:CL`
- long the live CME hedge weights
- rebalance from `80/20` to `60/40` on `2026-04-09 22:00 UTC`
- for the hold path, continue rebalancing to `40/60` at Sunday reopen and `20/80` at Monday reopen

Because public `1m` `xyz:CL` history is not retained before `2026-04-10 21:10 UTC`, this comparison uses:

- `15m` `xyz:CL` candles
- hourly funding history
- Databento `1m` CME bars

So these exact figures should be read as good approximations, not tick-perfect fills.

## Path comparison

| Path | Perp PnL | Hedge PnL | Funding | Net |
| --- | ---: | ---: | ---: | ---: |
| Exit Friday `2026-04-10 20:59 UTC` | `+3.131` | `-0.698` | `-1.928` | `+0.506` |
| Hold through Tuesday `2026-04-14 03:00 UTC` | `+2.265` | `+1.632` | `-2.945` | `+0.952` |

So ex post:

- holding through the weekend did better
- but only by about `+0.447` per unit

That is a real gain, but it is not large relative to the additional weekend uncertainty.

There is also a second way to look at it.

If you isolate the **Friday-close weekend carry** and define the hedge using the post-shift `40/60` weight, the Friday-close to Tuesday `03:00 UTC` path only earned about:

```text
+0.160 per unit
```

That is exactly the kind of payoff profile where the model risk matters more than the ex post headline.

## Recommendation

My recommendation is:

- if the thesis is **weekday roll mispricing**, flatten before the weekend by default
- only hold the weekend if you explicitly want exposure to weekend internal-oracle / macro oil repricing

In other words:

- yes, the weekend can be modeled better with a regime-switch component
- no, that does not automatically make the weekend the cleanest part of the trade

The improved model is valuable because it stops us from hallucinating frozen carry.

But from a trading-process perspective, the cleaner default is still:

- trade the weekday roll
- treat the weekend as optional, higher-variance exposure

## Final Takeaway

The answer to the central question is:

### Was the weekend oracle frozen?

No.

### What is the correct weekend oracle rule?

The best-supported rule is:

- `externalPrice` freezes when CME-derived external pricing is shut
- `oraclePx` continues to move off-hours via the internal trade.xyz / HIP-3 oracle process
- scheduled roll weights only become live external references when external pricing resumes

### How does that change the trade?

It turns the weekend from a deterministic roll-carry trade into a regime-switching, path-dependent trade where:

- hedge gap PnL
- internal oracle drift
- and capped funding transfer

all matter.

### Did the original trade thesis survive?

Economically, partially.

Model-wise, no.

The actual Apr 10-13 hedged trade path was roughly flat to mildly positive, but the profit source was **not** the one the old weekend model claimed.

That is the real lesson.
