# Designing a Rate Limiter: Fixed Windows, Sliding Windows, and Counting Just Enough

This post builds the small box that sits in front of every API and answers one question, millions of times a second: **has this caller used up its allowance, or may this request through?** A rate limiter is what stops one noisy client — a runaway script, a scraper, a credential-stuffing bot, or just a buggy retry loop — from drowning a service that everyone else depends on. The naive version ("keep a counter, reset it every minute") works until someone times their burst around the reset and quietly sends *twice* the limit. The whole post is about the handful of ideas that turn that leaky counter into one you can trust, distribute across many nodes, and run in about a millisecond.

**Question: a key is allowed 5 requests per minute. The caller sends 5 at 00:59 and 5 more at 01:01 — ten requests in two seconds. Each minute's counter only ever saw 5, so a minute-counter waves all ten through. How do we count so that the limiter sees the burst the *caller* actually sent, not the burst the *clock* happened to split?** The tempting answer is "use smaller windows," but that just moves the seam. The real fix is to stop counting inside a window that resets, and instead count over a window that **slides** — one that always looks back exactly `T` seconds from *now*, so there is no boundary to game.

This post sits next to a few we've already built. We shed excess load under a stampede in the [flash-sale post](31-flash-sale.md), leaned on Redis as a fast shared counter in the [distributed cache post](15-storage-engine-distributed-cache.md), and put a stateless fleet behind a [load balancer](06-distributed-load-balancer.md). A rate limiter reuses all three. The new ideas are about *what to count and how cheaply*, and they're worth stating up front because the rest of the post is just earning them:

> **Memory hook:** *the design space is a memory-vs-accuracy ladder. **Fixed-window counter** (1 number per key, cheap, bursts at the seam) → **sliding window log** (1 entry per request, exact, memory grows with traffic) → **bucketed counter** (1 number per second, bounded memory, near-exact) → **weighted sliding counter** (2 numbers per key, approximate, cheap enough for the edge).*

We start with the smallest thing that works, break it on purpose, and add exactly one idea at each rung.

---

## The brief

**Question: what does a rate limiter actually promise, and what does it need to keep that promise?**

The promise is one rule, evaluated on every request: *for this key, fewer than `N` requests in the last `T` seconds.* The `key` is whatever you meter by — a user id, an API token, a source IP, a tenant. `N` and `T` are that key's allowance.

<img src="../assets/rate-limiter/the-brief.svg" alt="The brief for a rate limiter. A client identified by key user:241531 sends a stream of requests to a rate limiter box. The limiter applies one rule: allow if count(key, last T) is less than N. A request that is under the limit gets a 200 and is counted (green path); a request over the limit gets a 429 and is discarded (red path). Below, four requirements: per-key config (each key sets its own N and T, by user, IP, or API token); distributed (many limiter nodes share one count for a key); low latency (it sits in front of every request, so it must be about 1ms); and accurate enough (no big bursts past N, but a small error is acceptable)." width="1000">

Four constraints shape every decision that follows:

- **Per-key configuration.** Different keys get different allowances — a free tier and an enterprise tier are not the same `N`. So there's a <span style="color:#93c5fd"><strong>config lookup</strong></span> on the side: `key → {N, T}`.
- **Distributed.** A real service runs many limiter nodes behind a load balancer. The count for a key cannot live in one node's memory, because the next request for that key might land on a different node. The count must live in a **shared store**.
- **Low latency.** The limiter is on the hot path of *every* request. If it adds 50ms, you've made the whole API slower to protect it. The budget is roughly one network hop to Redis.
- **Accurate enough.** "Never exceed `N`" is the goal, but the honest target is "never exceed `N` by much." As we'll see, the cheapest designs trade a sliver of accuracy for a large saving in memory — and that trade is usually worth it.

> **Memory hook:** *the rule is "fewer than N in the last T seconds, per key." The constraints — per-key config, a shared count across nodes, ~1ms latency, and accuracy that can bend a little — are what rule out the naive in-memory counter and push every real design toward a shared store and a smart way to count.*

---

## Section 1 — The simplest correct limiter: a fixed-window counter

**Question: what's the smallest thing that enforces "N per T" at all?**

One integer per key, reset every `T` seconds. This is the <span style="color:#ffff99"><strong>fixed-window counter</strong></span>, and in Redis it's almost embarrassingly small. Bucket time into fixed windows — say, each calendar minute — and make the key include the window:

```text
key = "user:241531:minute:202606241203"

INCR  key            # atomically +1, returns the new count
EXPIRE key  60       # let the window evaporate on its own

if returned_count > N:  reject (429)
else:                   allow
```

Two properties make this attractive. The count is **one number**, so memory per key is constant no matter how much traffic flows. And `INCR` is <span style="color:#ffff99"><strong>atomic</strong></span> — even with a thousand limiter nodes all incrementing the same key at once, Redis serializes them, so the count is never lost to a race. The `EXPIRE` means you never clean up; the old window's key simply dies when its minute passes.

For a lot of systems, this is genuinely enough. If "roughly 5 a minute, give or take" is an acceptable promise, ship it. But there's a specific way it leaks, and for anything adversarial — login attempts, payment attempts, anything someone is *motivated* to abuse — that leak matters.

> **Memory hook:** *fixed-window = one counter per (key, window), `INCR` + `EXPIRE`. Constant memory, atomic, self-cleaning. It's the right answer when an approximate cap is fine — and the wrong answer the moment someone is motivated to game the window boundary.*

---

## Section 2 — Why the fixed window leaks: the boundary burst

**Question: if every minute's counter is capped at 5, how does anyone ever get more than 5 in a minute?**

By straddling the reset. The counter is capped *per window*, but a real 60-second span can overlap *two* windows — and each of those windows is independently willing to grant the full `N`.

<img src="../assets/rate-limiter/fixed-window-burst.svg" alt="The fixed-window burst problem. The limit is 5 requests per 60-second window. A timeline shows window 1 covering 00:00 to 01:00 and window 2 covering 01:00 to 02:00, with a reset at 01:00. The caller sends 5 requests at 00:59, all inside window 1, so window 1's counter is 5, which is allowed. Then it sends 5 more requests at 01:01, inside window 2, so window 2's counter is also 5, which is allowed. But the real 2-second span around the boundary contains 10 requests, which is 2 times the limit. No fixed window ever saw more than 5, so the limiter never noticed. Take-home: counting inside a static window blinds the limiter to traffic that straddles the reset." width="1000">

Send 5 requests at 00:59 and 5 at 01:01. Window 1's counter reads 5 (allowed). The clock ticks past 01:00, the counter resets, and window 2's counter climbs to 5 (also allowed). Both windows are individually law-abiding, yet in the **two seconds** spanning the boundary the caller landed <span style="color:#ff8a8a"><strong>10 requests — twice the limit</strong></span>. In the worst case, any fixed-window limiter can be pushed to nearly `2N` over a `T`-length span.

The root cause is that the window's *origin is fixed to the clock*, not to the caller. The limiter asks "how many in *this calendar minute*?" when the question it should ask is "how many in *the last 60 seconds*?" Those differ exactly when traffic clusters at a seam — which is precisely where an abuser will put it.

Shrinking the window doesn't fix this; it just shrinks the seam and makes resets more frequent. The fix is to delete the seam entirely: count over a window anchored to **now**, sliding forward with every request.

> **Memory hook:** *the fixed window's flaw is its origin is the clock, not the caller. Traffic that straddles a reset can reach ~2N over a T-span while every individual window stays legal. The cure isn't a smaller window — it's a window with no fixed boundary, anchored to "now."*

---

## Section 3 — The sliding window log: be exactly right

**Question: what's the most honest possible answer to "how many requests in the last T seconds?"**

Remember every request's timestamp, and on each new request, count the ones that still fall inside `[now − T, now]`. This is the <span style="color:#8aff8a"><strong>sliding window log</strong></span>, and it's exact by construction — there's no window origin to game because the window's right edge *is* the current request.

A <span style="color:#ffff99"><strong>Redis sorted set</strong></span> (`ZSET`) per key fits this perfectly: store each request as a member scored by its timestamp, and the score ordering does the windowing for you.

<img src="../assets/rate-limiter/sliding-window-log.svg" alt="The sliding window log. A Redis sorted set per key stores one entry per request, scored by timestamp. A vertical cutoff line marks now minus T. Timestamps to the left of the cutoff (12:00:01, 12:00:03, 12:00:04, 12:00:05) are older than now minus T and are removed by ZREMRANGEBYSCORE. Timestamps to the right (12:00:42, 12:00:50, 12:00:58, 12:00:59) are inside the window and counted by ZCARD, giving 4. The algorithm, run as one atomic transaction on each request at time now: step 1, ZREMRANGEBYSCORE key 0 (now minus T) drops timestamps that fell out of the window; step 2, count = ZCARD key gives how many remain inside the window; step 3, if count is less than N, ZADD key now now and allow, else reject. Cost: one stored entry per request, so a key doing 10,000 requests per minute holds 10,000 timestamps — exact, but memory grows with traffic." width="1000">

The whole operation is three commands, run as one atomic unit (a `MULTI`/`EXEC` transaction or a Lua script, so no other request interleaves):

1. **Evict the expired.** `ZREMRANGEBYSCORE key 0 (now − T)` drops every timestamp that has aged out of the window.
2. **Count what's left.** `ZCARD key` is exactly the number of requests in `[now − T, now]`.
3. **Admit and record, or shed.** If the count is `< N`, `ZADD key now now` to record this request and <span style="color:#8aff8a">allow</span>; otherwise <span style="color:#ff8a8a">reject</span> and don't record.

This is as accurate as a rate limiter gets. The catch is in the picture: it stores <span style="color:#ff8a8a"><strong>one entry per request</strong></span>. A key doing 10,000 requests a minute holds 10,000 timestamps; a million active keys at that rate is billions of stored members. The memory grows with *traffic*, which is the one thing you don't control. For a high-volume API, the log is too expensive to keep — even though it's the most correct answer.

So the question becomes: can we keep the sliding window's honesty about *boundaries* while paying memory proportional to *time* instead of *traffic*?

> **Memory hook:** *the sliding window log stores every request's timestamp in a sorted set; evict-then-count (`ZREMRANGEBYSCORE` + `ZCARD`) gives the exact in-window count with no boundary to game. It's perfectly accurate but costs one entry per request — memory scales with traffic, which is why high-volume systems can't afford it.*

---

## Section 4 — The bucketed counter: count per second, not per request

**Question: do we really need every individual timestamp — or just how many landed in each slice of time?**

Just the counts. If forty requests arrive during the same second, the log stores forty timestamps; but all the limiter ever does with them is *count* them. Replace those forty entries with a single number: `second → 40`. That one swap is the [Arpit Bhayani sliding-window design](https://arpitbhayani.me/blogs/sliding-window-ratelimiter), and it's the rung most production limiters actually stand on.

Keep a small map per key: epoch-second → count. To answer "how many in the last `T` seconds?", sum the buckets whose second is still inside the window and drop the ones that aren't.

<img src="../assets/rate-limiter/bucketed-counter.svg" alt="The bucketed counter. Per key, requests_store maps the key user:241531 to an inner map of epoch-second to count. A bar chart shows one bucket per second. A vertical cutoff line marks now minus T. Buckets to the left of the cutoff (second :38 with count 3, :39 with count 5, :40 with count 2) are dropped because they are older than now minus T. Buckets to the right (:41 with count 4, :42 with count 2, :43 with count 6, :44 with count 1) are summed to 4 + 2 + 6 + 1 = 13. The current second :45 is highlighted and gets bumped by plus 1 for this request. On each request: sum the buckets with second greater than now minus T, drop the rest, compare to N, then increment the current second. A 60-second window holds at most about 60 buckets instead of thousands of timestamps, so memory is bounded by the window, not by traffic." width="1000">

The handshake per request is the same shape as the log, but over buckets instead of timestamps: sum the in-window buckets, compare to `N`, and if there's room, increment the **current second's** bucket. Expired buckets get dropped as you pass them (or swept lazily). The win is in the memory: a 60-second window holds **at most ~60 buckets** per key, whatever the traffic. A key doing 10,000 requests a second still has only 60 numbers, not 600,000 timestamps. <span style="color:#8aff8a"><strong>Memory is now bounded by the window, not by the load.</strong></span>

The cost is a sliver of accuracy, and it's worth naming precisely. Within the current second, the bucket can't tell you *where* in that second the requests landed — it's a count, not a set of timestamps. So the window edge is sharp to the *second* but fuzzy *within* a second. For a limiter, second-level precision is almost always plenty: you've kept the sliding window's defense against the boundary burst (no fixed origin, you always look back a true `T`) while shedding the per-request memory that made the log unaffordable. This is the sweet spot the rest of the design is built around.

> **Memory hook:** *the bucketed counter replaces per-request timestamps with one count per second: `key → {second → count}`. Sum the in-window buckets to decide; bump the current second to record. Memory is bounded by the window (~60 buckets for a 60s window), not by traffic — at the price of being precise only to the second, which is fine for rate limiting.*

---

## Section 5 — The architecture: where config, decisions, and counts live

**Question: we've been saying "the limiter" as if it's one box. In a real distributed service, what are the actual pieces?**

Three, and keeping them separate is the whole trick to making the limiter both fast and horizontally scalable.

<img src="../assets/rate-limiter/architecture.svg" alt="The complete rate limiter architecture. A client's keyed traffic goes through a load balancer to a stateless decision-engine fleet (drawn as stacked boxes) that evaluates count(key, last T) less than N but holds no state itself. The decision engine reads each key's rule from a config store (a NoSQL database with a cache, holding key to {T, N} mappings that rarely change) — this is the control-plane path, in blue. It reads and increments the live count in a counter store, Redis, reached through a shard proxy that routes a key to shard A or shard B; the counter store holds buckets or a sorted set per key with atomic increment and TTL eviction — this is the yellow count path. A request under the limit is forwarded to the upstream service on the green allow path; a request over the limit becomes a 429 reject that sheds the request, on the red path." width="1180">

- The <span style="color:#93c5fd"><strong>decision engine</strong></span> is a **stateless fleet**. Every node runs the identical logic — look up the rule, read the count, compare, decide — and holds *no* state of its own. That's what lets you put it behind a [load balancer](06-distributed-load-balancer.md) and scale it horizontally: any node can handle any request because the truth lives elsewhere. This is the same stateless-compute-over-shared-state shape we used in the [flash-sale design](31-flash-sale.md).
- The <span style="color:#93c5fd"><strong>config store</strong></span> holds `key → {N, T}`. It's a control-plane concern: it changes rarely (you edit a tier's limit occasionally, not per request), so it lives in a NoSQL store fronted by an aggressive cache. The decision engine reads it on essentially every request, so that read must be a local cache hit, not a round-trip.
- The <span style="color:#ffff99"><strong>counter store</strong></span> holds the live counts — the buckets or sorted set from the last two sections — in Redis. It's the only stateful, hot component, and it's the one that has to be **atomic** (so concurrent nodes don't lose increments) and **sharded** (so one key's traffic, or one hot Redis node, doesn't become the bottleneck). A shard proxy routes each key to its Redis shard by hashing the key — the same [consistent-hashing](08-distributed-id-generators.md) idea used to spread any keyed workload.

Trace one request through the colors: it arrives <span style="color:#cdd9e5">(white)</span>, a decision node reads the rule <span style="color:#93c5fd">(blue)</span>, reads-and-increments the count <span style="color:#ffff99">(yellow)</span>, and then either forwards it upstream <span style="color:#8aff8a">(green)</span> or returns `429` <span style="color:#ff8a8a">(red)</span>. The two reads are the entire latency budget, and both are designed to be a single fast hop.

> **Memory hook:** *split the limiter into three: a **stateless decision fleet** (scales behind a load balancer, holds nothing), a cached **config store** (`key → {N,T}`, control-plane, rarely changes), and a sharded, atomic **counter store** in Redis (the only hot state). The decision is two fast reads — rule, then count — so it stays on the ~1ms budget.*

---

## Section 6 — Getting it right under concurrency and scale

**Question: the design is clean on paper — what actually bites when thousands of nodes hammer one key, or when a key's window is an hour instead of a minute?**

Three things, and each has a standard fix.

**Atomic read-modify-write.** "Sum the buckets, check against `N`, then increment" is three steps, and between any two of them another node can act. If two nodes both read a count of 4 against a limit of 5, both conclude "room for one more," and both admit — the count lands at 6. The window's edges are honest but the *check itself* raced. The fix is to make the whole decision one atomic operation: a **Lua script** in Redis (which runs to completion with nothing interleaved) or the equivalent `MULTI`/`EXEC` transaction. For the plain fixed-window case, `INCR` is already atomic and returns the post-increment value, so you increment *first* and reject if the result exceeds `N` — no separate read to race. This is the same "make the contended step indivisible" lesson as the [distributed lock manager](07-distributed-lock-manager.md), applied to a counter instead of a lock.

**The deletion race.** When you lazily delete buckets older than `now − T`, a request computing an *older* `start_time` can be summing the very buckets a request with a *newer* `start_time` is deleting — and miscount. Don't scatter deletes across request handlers. Either let Redis **TTL-expire** whole bucket keys (the cleanup is the database's job, not the request path's), or do the evict-and-sum inside the same atomic script so no two requests ever touch the bucket set concurrently.

**Granularity for large windows.** Per-second buckets are great for a 60-second window — 60 numbers to sum. But "1,000 requests per *day*" at per-second granularity is 86,400 buckets, and summing them on every request is wasteful. Coarsen the bucket to match the window: minute-level buckets turn a one-hour window into ~60 buckets, and an hour-level bucket turns a one-day window into ~24. You trade edge precision (now you're sharp to the minute, not the second) for a cheap sum — the same memory-vs-accuracy dial, turned to fit the window. If even that sum is too hot, keep a **running aggregate**: maintain the rolling sum incrementally as buckets enter and leave the window, so a decision is O(1) instead of O(buckets).

> **Memory hook:** *three production hazards: (1) the check-then-increment **race** — make the whole decision atomic with a Lua script, or increment-first with atomic `INCR`; (2) the **deletion race** — let Redis TTL-expire buckets instead of deleting them in the request path; (3) **large windows** — coarsen bucket granularity (minute/hour) to keep the sum cheap, or maintain a running aggregate for O(1) decisions.*

---

## Section 7 — The cheapest good-enough: the weighted sliding counter

**Question: the bucketed counter is bounded by the window — but on a CDN edge serving the whole internet, even ~60 buckets per key times a billion keys is a lot. Can we get the sliding-window behavior from just *two* numbers?**

Almost exactly, yes. Keep only two fixed-window counters per key — the **previous** window's total and the **current** window's total — and *estimate* the sliding count by weighting the previous window by how much of it still overlaps the real `T`-second window. This is the <span style="color:#ffd27f"><strong>sliding window counter</strong></span> approximation, and it's what runs on high-volume edges like Cloudflare's.

<img src="../assets/rate-limiter/weighted-approximation.svg" alt="The sliding window counter approximation. A timeline shows the previous fixed window with count 8 and the current fixed window with count 3. A marker shows now is 25 percent into the current window. The true sliding window of width T, drawn as a dashed pink box, looks back from now and covers the last 75 percent of the previous window plus all of the current window so far. The estimate formula is: estimate equals prev_count times overlap_fraction plus current_count, which evaluates to 8 times 0.75 plus 3 equals 9. Since 9 is less than the limit of 10, the request is allowed. The note explains this uses two counters per key instead of N timestamps; the cost is approximation, since it assumes the previous window's traffic was evenly spread. Cloudflare reported this stays within about 0.003 percent of exact on real traffic, cheap enough to run on every edge request." width="1000">

The formula is one line:

```text
estimate = prev_count × overlap_fraction + current_count
```

If you're 25% into the current window, then the trailing `T`-second window still covers the **last 75%** of the previous window, so you count 75% of its requests plus all of the current window's:

```text
estimate = 8 × 0.75 + 3 = 9       # limit 10 → allow
```

As the current window fills, `overlap_fraction` slides from 1 down to 0, smoothly retiring the previous window's weight — no reset cliff, so the boundary burst from Section 2 is gone. The approximation's one assumption is that the previous window's traffic was **spread evenly** across it; when it wasn't (all 8 at the very start, say), the estimate is slightly off. In practice that error is tiny: <span style="color:#ffd27f">Cloudflare reported it stays within ~0.003% of exact</span> on real traffic — for two integers per key and a multiply-add per request, that's an extraordinary deal, and it's why this is the default at internet scale.

> **Memory hook:** *the weighted sliding counter keeps just two numbers per key — previous and current fixed-window totals — and estimates the sliding count as `prev × overlap + current`. The overlap fraction slides 1→0 as the current window fills, so there's no reset cliff. It assumes even spread in the previous window (a tiny error, ~0.003% on real traffic) and is cheap enough for every edge request.*

---

## Questions that complete the mental model

**Should the limiter fail open or fail closed when Redis is down?** Usually **fail open** — if the counter store is unreachable, allow the request rather than 503-ing your whole API to enforce a rate limit. A rate limiter is a guardrail, not the product; taking the service down to protect it inverts the goal. (For abuse-critical paths like login, you might fail closed instead — decide per route.) Either way, give the decision engine a tight timeout to Redis so a slow store doesn't blow the latency budget.

**Rate limiting vs. throttling vs. load shedding — same thing?** Related, different intents. **Rate limiting** caps a *caller's* usage by a configured rule (this post). **Load shedding** drops requests when the *server* is overloaded regardless of who sent them — the [flash-sale](31-flash-sale.md) admission control. **Throttling** often means *slowing* a caller (queue or delay) rather than rejecting outright. A complete system uses all three: per-key limits, server-health shedding, and sometimes a `Retry-After` to throttle politely.

**What's the difference between a rate limiter and a token bucket?** They're two models for the same job. This post counted requests in a sliding window; a **token bucket** instead refills `r` tokens per second into a bucket of capacity `b`, and each request spends one — which naturally allows short bursts up to `b` while holding the long-run rate to `r`. Token bucket is the better fit when you *want* to permit bursts; the sliding window is the better fit when you want a hard "no more than N per T." Many APIs expose limits in token-bucket terms even when the backing implementation is a counter.

**What should a rejected caller actually receive?** `429 Too Many Requests`, plus headers that make the limit *discoverable*: `RateLimit-Limit`, `RateLimit-Remaining`, and a `Retry-After` telling them when to come back. A well-behaved client backs off on its own; surfacing the numbers turns the limiter from an opaque wall into a contract, and cuts the retry storms that would otherwise pound the limiter itself.

> **Memory hook:** *the rate limiter is a memory-vs-accuracy ladder over one rule — "N per T per key." Fixed window is cheap but bursts at the seam; the log is exact but heavy; the bucketed counter bounds memory by the window; the weighted counter approximates it with two numbers. Wrap whichever rung you pick in atomic operations, a sharded counter store, a stateless decision fleet, and a fail-open default — and return a 429 with the limit headers so callers can behave.*
