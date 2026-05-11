# Pricing The Hyperliquid Oil Perp Against The CME Curve

This note explains, in plain English, how to think about Hyperliquid's `xyz:CL` oil perpetual during the trade.xyz oracle roll, why the trade looked interesting in the first place, how to derive a fair value for the mark, and how to tell whether the market is actually offering an edge.

The target reader is a hobbyist or semi-professional trader who is comfortable with futures and funding, but does not want to wade through exchange docs and reverse-engineer the pricing model from scratch.

## The trade in one sentence

The `xyz:CL` perpetual on Hyperliquid references a rolling blend of two CME crude oil futures contracts. During the roll window, the oracle is scheduled to step down day by day as weight shifts from the more expensive front-month future into the cheaper next-month future. If the perpetual mark does not price that scheduled drop correctly, there may be an arbitrage-like spread trade.

In trader language:

- Short the Hyperliquid perp when the mark is too rich relative to the rolling oracle.
- Hedge the outright oil exposure with the CME contracts that make up the oracle.
- Pay or receive funding along the way.
- Exit when the market spread converges toward fair value.

## Why this looked attractive

At first glance, the setup looks simple:

1. The current oracle is tied mostly to the front-month CME contract.
2. The front-month contract is much more expensive than the next-month contract.
3. Over a few business days, the oracle will rotate into the cheaper contract.
4. If you are short the perp and hedge the outright oil risk, the mark should eventually come down with the oracle.

That makes people think:

> "The oracle is going to fall by several dollars. If I short the perp now and hedge the delta, I should collect that roll-down."

That instinct is directionally right, but incomplete.

## Why the naive model is wrong

The missing piece is funding.

If the Hyperliquid mark sits below the oracle, shorts pay funding. That funding is not noise. It is the mechanism that lets the market price the future oracle drop ahead of time.

This means the correct question is not:

> "How much will the oracle fall?"

It is:

> "How much of that future oracle fall should already be embedded in the current mark-vs-oracle spread?"

That is the whole game.

If the market is already pricing the future roll correctly, there is no free lunch. The short makes money on mark-to-market, but gives it back through funding.

## The key insight

The total spread between the oracle and the mark is not just one thing.

It is better understood as:

```text
total spread = baseline basis + roll premium
```

Where:

- `baseline basis` is the normal non-roll spread between the Hyperliquid perp and the oracle.
- `roll premium` is the extra spread the market needs in order to compensate shorts for the scheduled future oracle drops.

This note and the script model the second part.

That is the important conceptual jump.

The exchange schedule tells us exactly when the oracle weights change. The CME curve tells us how large each scheduled oracle jump is right now. Funding tells us how fast those future jumps should be pulled into today's spread.

## What exactly rolls

For WTI, trade.xyz references a fixed calendar schedule.

At the start of the month, the oracle references the next calendar-month CME contract. The protocol roll period spans the 5th through 10th exchange business days of the month: five 20% step transitions begin on the 5th business day, leaving the oracle fully rolled into the following contract on the 10th business day.

For the April 2026 case:

- Start of month: `CLK6`
- Roll target: `CLM6`
- Shift schedule: `100/0 -> 80/20 -> 60/40 -> 40/60 -> 20/80 -> 0/100`

If `CLK6` is above `CLM6`, every shift pushes the oracle lower.

The size of one shift is approximately:

```text
shift jump = 20% x (front future - back future)
```

So if the front-back calendar spread is `$8.70`, each 20% shift is worth about `$1.74`.

## How funding turns future jumps into today's spread

Now imagine there were only one future oracle drop of size `J`.

If the mark did not move until the shift happened, a delta-hedged short would earn the full jump at the event. But because shorts are paying funding while the mark is below the oracle, the market has an incentive to "amortize" that future jump into the present.

In continuous-time approximation, the roll component `R(t)` follows:

```text
dR/dt = R / tau
```

between shifts, and at each scheduled oracle shift:

```text
R(after shift) = R(before shift) - J
```

This means:

- Between shifts, the roll premium grows exponentially as the shift gets closer.
- At a shift, the premium resets lower because one scheduled oracle drop has now been realized.

That gives a piecewise-exponential, jump-reset curve.

It is not a generic statistical spline. It is a structural carry curve.

## What `tau` means

This is the parameter that confuses most people.

`tau` is not "hours until the next shift."

`tau` is the funding time constant. It tells you how quickly a future oracle jump should be reflected in today's spread.

In this project we use `tau = 16 hours` as the default structural assumption. That is not chosen because the first shift happened to be around 16 hours away when the work started. It comes from the shape of the trade.xyz funding formula in the relevant regime.

In plain English:

- Smaller `tau` means future oracle drops get priced in faster.
- Larger `tau` means they get priced in more slowly.

The actual time until each shift is handled separately by the clock. The script recalculates the distance to every remaining shift from the current timestamp each time it runs.

## Deriving the fair roll spread

Here is the cleanest way to derive the fair value:

1. Take the current front-month and next-month CME prices.
2. Convert the current calendar spread into a jump size for each remaining 20% weight change.
3. Start from the terminal condition that the roll component is zero after the final shift.
4. Work backward across the remaining shifts.

For one backward step:

```text
R_start = (R_end + J) * exp(-dt / tau)
```

Where:

- `R_end` is the roll premium after the later shift
- `J` is the scheduled shift jump
- `dt` is the time between the two shift points

Do that across every remaining shift and you get the fair roll premium today.

Then:

```text
fair spread = baseline spread + roll spread
fair mark   = live oracle - fair spread
```

That is the core pricing logic implemented in the script.

## How we know the fair mark is sensible

There is no single theorem that "proves" the fair mark is perfect, but there are several strong reasons to trust the framework.

### 1. It matches the exchange mechanics

The model uses:

- the published oracle roll schedule,
- the documented funding rule,
- the live Hyperliquid oracle and funding data,
- and the current CME curve.

So we are not making up the moving parts. We are combining the exchange's own rules into a pricing framework.

### 2. The signs come out right

The model produces the economically correct behavior:

- If the front-month contract is richer than the back-month contract, future oracle shifts are downward.
- The fair roll premium is positive.
- As a shift gets closer, that premium grows.
- Right after a shift, the premium drops.
- After the final shift, the roll component goes to zero.

Those are all the behaviors you want.

### 3. It respects no-arbitrage intuition

A hedged short should not magically earn the entire scheduled oracle drop for free. If the market is pricing correctly, some or most of that future drop has to show up as a spread today and as funding paid along the way.

That is exactly what the model does.

### 4. We do not overfit

There was only one prior realized roll event available to use as a check. That is not enough data to responsibly fit a structural parameter like `tau` and then declare victory.

So the public model does **not** fit `tau` by default.

Instead:

- `tau` is fixed by default as a structural prior.
- historical roll data is used mainly to validate the shape,
- and the live market is used to estimate only the residual non-roll baseline basis.

That is a much safer modeling choice.

## What the model captures and what it does not

### What it captures

- The exact roll schedule
- The current oracle composition
- The current CME calendar spread
- The way fair value should change as time passes toward each shift
- The live gap between market spread and fair spread

### What it does not yet forecast

It does **not** explicitly forecast the future path of the CME calendar spread.

That matters because the jump size at each future shift should really be:

```text
future jump_i = 20% x expected calendar spread at shift_i
```

The current public model uses the simplest live-marking assumption:

```text
expected future calendar spread = current calendar spread
```

So the model updates immediately if the current curve changes, but it does not yet build a separate forecast for how the curve itself may converge over the coming days.

That is a reasonable choice for real-time marking, but it is still a simplification.

## How the script works

The repository includes `fair_value_spline.py`, which:

- builds the roll schedule automatically from calendar dates,
- pulls Hyperliquid perp context,
- pulls CME front/back snapshots,
- computes the roll component of fair spread,
- estimates a residual baseline basis,
- computes fair mark,
- logs the edge,
- and emits simple buy/sell signals when the live market deviates from fair by more than a chosen threshold.

The script now supports:

- `Databento` snapshots
- `Yahoo Finance` delayed snapshots
- an `auto` mode that tries Databento first and falls back to Yahoo

## Quick usage

Install dependencies:

```bash
uv add databento requests yfinance
```

Run in live mode:

```bash
python fair_value_spline.py --quote-source auto
```

Run in demo mode:

```bash
python fair_value_spline.py --demo --iterations 1
```

Evaluate a specific timestamp:

```bash
python fair_value_spline.py --demo --as-of 2026-04-08T08:00:00+00:00 --iterations 1
```

Use a fixed structural `tau` and do not fit it from history:

```bash
python fair_value_spline.py --tau 16 --quote-source auto
```

Only if you deliberately want to fit `tau` from a sample window that spans at least one realized shift reset:

```bash
python fair_value_spline.py --fit-tau
```

## How to think about the edge as a trader

The edge is not:

> "Oracle goes down by X, therefore my short makes X."

The edge is:

> "Market spread is wider or narrower than the fair spread implied by the current curve, the funding rule, and the known roll schedule."

So the right workflow is:

1. Pull the live front/back CME snapshot.
2. Pull the live Hyperliquid oracle, mark, and funding.
3. Compute fair spread.
4. Compare live market spread to fair spread.

If:

```text
market spread > fair spread
```

the perp is cheap versus the curve and the trade favors a long perp / short CME hedge.

If:

```text
market spread < fair spread
```

the perp is rich versus the curve and the trade favors a short perp / long CME hedge.

## Final takeaway

The instrument is not hard because it is exotic. It is hard because the oracle is rolling on a known schedule while funding continuously pulls future curve moves into the present.

Once you separate the spread into:

- ordinary perp basis
- and roll premium

the pricing problem becomes much cleaner.

That is the point of this model.

It does not claim to predict every tick. It gives you a disciplined way to ask the only question that matters:

> "Given the current CME curve, the current time, the remaining oracle shifts, and the funding rule, where should the mark be trading right now?"

If the market is not there, that gap is the thing you can trade.
