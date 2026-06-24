# Impression Counting: How Many *Unique* People Saw This Ad?

This post builds an **impression-counting** system from first principles — the machinery behind "how many people saw this ad," "views on this video," "reach of this campaign." It is one of the most reused systems in the industry: LinkedIn counts views on a post, YouTube on a video, Google counts impressions on a search result, AdSense on an ad. We start from the dumbest correct thing (`put every viewer in a set, count the set`) and grow each component only when a named cost appears: a set that is too big to ship over the network, a count that eats gigabytes of RAM, a query that is too slow to render a dashboard, and finally a storage bill that makes keeping everything in memory impossible.

The whole post turns on one realization: **the customer wants a number, not a guest list.** An advertiser staring at a dashboard asks "how many unique people saw my ad between 10:00 and 10:05?" — they never need *who*. The moment you stop tracking identities and start *estimating a count*, a problem that needed gigabytes collapses into kilobytes.

**Question: a million people view an ad every minute, and thousands of advertisers want a live graph of "unique viewers in the last *n* minutes" for their campaign. You cannot store every viewer's id, you cannot ship millions of ids to a query server, and you cannot make the advertiser wait. What is the smallest design that answers "how many unique people" — and what breaks each time the traffic 10×s?** The honest path runs straight through a naive set, a memory explosion, a probabilistic data structure called HyperLogLog, a per-minute bucketing scheme, and a two-tier hot/cold store — and by the end you've hand-built the engine under Google Analytics, ad dashboards, Datadog, and YouTube's view counter.

It leans on two earlier posts. We built a [YouTube view counter](30-youtube-views-counter.md) and learned to absorb a firehose of view events through Kafka and counting consumers — here that same firehose feeds a *distinct*-count instead of a running total. And we used a [time-series database](16-storage-engine-etl-cdc.md) for append-only event data — here we ask why a TSDB, and not a column store, is the natural home for impressions.

But first, a short warm-up on a completely different little system — one that shares the *spirit* of this post: replace an exact, expensive computation with a cheap one that is good enough.

> **Memory hook:** *the customer wants a number, not a guest list. Stop storing viewer identities and start estimating cardinality — and gigabytes of ids become kilobytes of HyperLogLog.*

---

## A warm-up: how does an app know you've *arrived*?

**Question: your phone has one GPS coordinate. The airport is a shape on a map. How does Lyft know your dot is *inside* the airport so it can switch you into the airport-pickup queue?**

This is the same question behind a pile of features: <span style="color:#93c5fd"><strong>location-based reminders</strong></span> ("remind me when I reach the office"), <span style="color:#93c5fd"><strong>airport rides</strong></span> (Lyft/Uber detect you're inside the terminal polygon), <span style="color:#93c5fd"><strong>navigation</strong></span> ("you have arrived"), and <span style="color:#8aff8a"><strong>Pokémon Go</strong></span> (is a Pokémon within the region around you?). Geometrically they're all one problem: **is this point inside this polygon?**

The boundary of an airport, a neighborhood, or a geofence is a <span style="color:#ffff99"><strong>polygon</strong></span> — a closed loop of straight edges. The question "am I inside it?" has a beautifully simple answer called <span style="color:#8aff8a"><strong>ray casting</strong></span>.

<img src="../assets/impression-counting/ray-casting.svg" alt="Ray casting for point-in-polygon, explained in two panels. LEFT PANEL, the idea: a yellow polygon (a rough airport shape) on a dark grid. Two points are tested. A red point sits inside the polygon; a horizontal ray is drawn from it going right to infinity, and it crosses the polygon's edges exactly 1 time (odd), with a label 'odd crossings = INSIDE.' A blue point sits outside the polygon; its horizontal ray to the right crosses the edges 2 times (even), labelled 'even crossings = OUTSIDE.' RIGHT PANEL, why it works: a strip showing a ray starting far outside the shape at infinity, where you are definitely OUTSIDE. Each time the ray crosses an edge it flips state: cross once and you are INSIDE, cross again and you are OUTSIDE, and so on — like a light switch toggled at every wall. So the parity (odd or even) of the number of crossings tells you the answer: odd means you ended inside, even means you ended outside. A small note distinguishes the circle case: 'within 5 m of the drop point' is not a polygon at all but a circle, so that check is just distance(you, drop) is less than 5 metres — a single comparison, no ray needed. Caption across the bottom: a geofence is a polygon; arriving is a point-in-polygon test; proximity to a single point is a distance test." width="1000">

The trick: from your point, shoot a <span style="color:#8aff8a"><strong>ray</strong></span> in any fixed direction — say straight to the right, off to infinity. Count how many polygon edges it crosses. **Odd number of crossings → you're inside. Even → you're outside.**

Why does parity work? Start your imagination way out at infinity, where you are unambiguously *outside* the shape. Now walk back along the ray toward your point. Every time you step across an edge, you flip: outside → inside → outside → inside, like a switch thrown at every wall you pass through. So if the ray hits an *odd* number of edges between infinity and your point, you flipped an odd number of times and ended up *inside*. Even, and you're back outside. That's the entire algorithm — no trigonometry, just counting edge crossings.

A close cousin shows up in "you have arrived" and "did the driver reach the drop?": being **within 5 metres of a single point** is not a polygon test at all — it's a circle, so it's just `distance(you, drop) < 5m`, one comparison. Use point-in-polygon when the region has a *shape* (an airport, a neighborhood); use a distance check when the region is "within *r* of a point."

> **Memory hook:** *to test inside-a-shape, shoot a ray and count edge crossings — odd is inside, even is outside. "Within r of a point" is simpler still: just a distance check.*

That warm-up shares the soul of the rest of this post: **trade an exact, expensive computation for a cheap, good-enough one.** Now the main event.

---

## The brief

**Question: before any boxes — what *is* impression counting, and why isn't it just `COUNT(*)`?**

<img src="../assets/impression-counting/requirements.svg" alt="The brief for an impression-counting system. Top: the system is everywhere — a list pairing platforms with what they count: LinkedIn counts views on a post, YouTube views on a video, Google Search impressions on a result, Reddit views on a post, Google AdSense impressions on an ad, Instagram views on a photo, TikTok views on a short video. Middle, three metrics defined: IMPRESSION (white) — the ad/post was shown to a screen, counted every time it is seen; REACTION (blue) — an engagement like a like, haha, or angry; CTR — click-through rate, clicks divided by impressions, the engagement the advertiser actually cares about. Then the key metric the dashboard must render: REACH — the number of UNIQUE people who saw the ad in the last n minutes, where n is given at query time, plotted as a live graph over time. Bottom, the requirements box: real-time or near real-time; no pre-aggregation (n is arbitrary, chosen at runtime); each user counted ONCE per time window even if they viewed many times; the count may be a close APPROXIMATION, not exact; and a rule engine must filter out unwanted events (self-views, too-fast replays). A highlighted insight at the bottom: this is a COUNT DISTINCT over a sliding window — and COUNT DISTINCT, not COUNT, is what makes it hard." width="1000">

Impression counting sits under an enormous range of products. The pattern is always the same: something was **shown**, and we want to know how often, and to how many distinct people.

A few definitions the rest of the post leans on:

- An <strong>impression</strong> is one *showing* — the ad or post appeared on a screen. <span style="color:#8aff8a"><strong>Views</strong></span> are impressions; you count one every time the thing is seen.
- A <span style="color:#93c5fd"><strong>reaction</strong></span> is an *engagement* — a like, a haha, an angry. Not everyone reacts, so reactions measure something deeper than mere exposure.
- <span style="color:#ffff99"><strong>CTR (click-through rate)</strong></span> is <span style="color:#93c5fd"><strong>reactions/clicks</strong></span> divided by <span style="color:#8aff8a"><strong>impressions</strong></span> — the ratio an advertiser stares at to judge whether a campaign is working. To plot it, you need both the numerator and the denominator over time.
- <span style="color:#ffff99"><strong>Reach</strong></span> is the headline number: **how many *unique* people saw the ad in the last *n* minutes.** This is what the advertiser's dashboard graphs, minute by minute.

That word *unique* is the whole problem. If the customer only wanted total views, this would be a running counter — the [YouTube view counter](30-youtube-views-counter.md) we already built. But "unique people" means **count distinct user ids**, deduplicated, over a window whose size *n* the advertiser picks at query time.

The requirements that shape every later decision:

- <span style="color:#8aff8a"><strong>Real-time / near-real-time.</strong></span> The graph should move as traffic flows, not after an overnight batch.
- <span style="color:#ff8a8a"><strong>No pre-aggregation.</strong></span> *n* is chosen at runtime — last 5 minutes, last hour, last 4 days. You can't precompute one magic total.
- <span style="color:#ffff99"><strong>Each user counted once per window.</strong></span> A user who reloads 50 times is *one* unique viewer. Dedup is mandatory.
- <span style="color:#ffff99"><strong>The count may be approximate.</strong></span> A dashboard reading "1,000,000" vs "1,001,200" reach is the same decision to an advertiser. **Close is good enough** — and that single permission is the key that unlocks everything.
- <span style="color:#ffd27f"><strong>Filter unwanted events.</strong></span> A creator viewing their own video, a bot replaying 10× a second — a rule engine drops these before they're counted.

> **Memory hook:** *reach = COUNT DISTINCT users over a sliding window of arbitrary size n. The two words that make it hard are "distinct" (must dedup) and "n at runtime" (can't pre-aggregate). The mercy is that the count may be approximate.*

---

## Section 1 — What the query engine must actually answer

**Question: pin down the exact computation. Forget storage — what does the dashboard ask, and what's the right answer?**

The advertiser asks things like *"count distinct users on this post from 10:00 to 10:05"* or *"...from 04/01 to 08/01."* The answer is the size of the **set union** of everyone who viewed in those minutes.

<img src="../assets/impression-counting/distinct-count-window.svg" alt="What 'count distinct in a window' means, shown on a timeline of raw view events. A vertical list of events, each a timestamp mapping to a user id: 10:00 to A, 10:00 to B, 10:01 to C, 10:01 to B, 10:02 to B, 10:02 to A. To the right, the answer for the window 10:00 to 10:02 is the SET of distinct users {A, B, C} — note B viewed three times but is counted once. Below, a second block of events: 10:03 to A, 10:03 to B, 10:04 to D, 10:05 to B, 10:05 to D, 10:05 to E, whose distinct set for 10:03 to 10:05 is {A, B, D, E}. Finally the full window 10:00 to 10:05 is the UNION of the two: {A, B, C, D, E}, cardinality 5. The lesson, highlighted: the answer to any window is the cardinality of the union of per-minute viewer sets — so if we store one set per minute, any range query is just 'union the minutes in range, then count.'" width="1000">

The mechanics are worth making concrete. Bucket viewers by minute, each bucket a set of user ids:

```text
10:00–10:02  ->  { A, B, C }          (B viewed 3×, counted once)
10:03–10:05  ->  { A, B, D, E }
10:00–10:05  ->  { A, B, C } ∪ { A, B, D, E } = { A, B, C, D, E }  ->  5
```

This shape is the entire design in miniature. If we keep **one set of viewers per minute per post**, then *any* range query — last 5 minutes, last hour, last week — is just "**union** all the minute-buckets in range, then take the **size**." The size of a set is its <span style="color:#ffff99"><strong>cardinality</strong></span>. Hold that word; it returns as the hero of the post.

> **Memory hook:** *store one viewer-set per minute; any range answer is "union the minutes in range, then count." The answer is always the cardinality of a union.*

---

## Section 2 — The naive solution, and exactly where it melts

**Question: just do it. One hash-set of user ids per minute, union them on query, return the size. Why isn't that the whole post?**

It works — beautifully, at small scale. Store `set<user_id>` per minute, union on demand, count. The logic is correct on the first day. Then the traffic grows, and three costs detonate.

<img src="../assets/impression-counting/naive-set-union.svg" alt="The naive set-union approach and the three costs that kill it at scale. Left: the design — per-minute hash-sets of user ids stored in a database cylinder, e.g. 20220401_1200 = {a,b,c,d,e,f}, 20220401_1300 = {a,c,z,w,x}, 20220401_1400 = {c,d,f,l,z}. A query server pulls the relevant sets out of the database and computes set-union {a,b,c,d,e,f,l,w,x,z} = 10, then returns the size to the customer's dashboard. Right, three red failure labels. ONE, HUGE NETWORK I/O: to union sets the query server must pull every raw id out of the database over the network — millions of ids per query. TWO, MEMORY BLOWUP: an in-memory set of 1 million 4-byte user ids is about 4 MB; computing unique visitors over the last hour means 60 minute-buckets, 60 times 4 MB = 240 MB of data processing for ONE ad; and there are thousands of advertisers — 5000 ads times 240 MB equals 1,200,000 MB which is about 1200 GB. THREE, SLOW QUERY EVALUATION: building and unioning million-element sets takes real CPU time, and the dashboard needs answers now. A red banner concludes: the identities are the problem — we are shipping and storing millions of user ids just to produce a single number. Granularity is critical: too coarse and the windows are wrong, too fine and the bucket count explodes." width="1000">

The data model is innocent: a set per minute.

```text
20220401_1200 = { a, b, c, d, e, f }
20220401_1300 = { a, c, z, w, x }
20220401_1400 = { c, d, f, l, z }

distinct over the 3 minutes = union of the three sets = 10
```

Now scale it, and watch each cost fire:

1. <span style="color:#ff8a8a"><strong>Huge network I/O.</strong></span> To union sets that live in the database, the query server has to *pull every raw id over the wire*. A popular ad over an hour is millions of ids streamed to the query box — per query.
2. <span style="color:#ff8a8a"><strong>Memory blowup.</strong></span> Do the arithmetic. A `user_id` is a 4-byte int. A million viewers in one minute is `1M × 4B = 4MB` just for that minute's set. "Unique in the last hour" needs 60 of those: `60 × 4MB = 240MB` of processing **for one ad**. There are thousands of advertisers watching live: `5000 × 240MB = 1,200,000MB ≈` <span style="color:#ff8a8a"><strong>1200 GB</strong></span>. You cannot hold that in memory.
3. <span style="color:#ff8a8a"><strong>Slow query evaluation.</strong></span> Allocating and unioning million-element hash-sets burns CPU and time, and the dashboard wants an answer *now*.

There's also a quieter knob: <span style="color:#ffff99"><strong>granularity</strong></span>. Bucket too coarsely (per hour) and you can't answer "last 5 minutes." Bucket too finely (per second) and the number of buckets explodes. Per-minute is the usual sweet spot — fine enough for live graphs, coarse enough to keep bucket counts sane.

Stare at the three costs and notice what they share: **they're all about the identities.** Network I/O ships ids; memory holds ids; CPU hashes ids. But the customer never asked for ids — they asked for a *count*. We are paying, in full, for information we are about to throw away.

> **Memory hook:** *the naive set is correct but pays for identities the customer never wanted — network to ship ids, RAM to hold them (1200 GB at scale), CPU to union them. The waste is the guest list; only the count was ordered.*

---

## Section 3 — Where does this live? Time-series databases

**Question: before optimizing the count, where do these append-only, timestamped view events even belong?**

View events have a very specific shape: they are **append-only**, they **arrive in time order**, they are **never updated**, and you almost always query them **by time range**. That shape has a name — it's a <span style="color:#93c5fd"><strong>time-series</strong></span> — and there's a whole class of databases tuned for exactly it: <span style="color:#93c5fd"><strong>InfluxDB</strong></span>, Prometheus, TimescaleDB. The same engines behind <span style="color:#93c5fd"><strong>Datadog</strong></span> metrics, Google Analytics, and YouTube watch-time.

A <span style="color:#93c5fd"><strong>time-series database (TSDB)</strong></span> is built on a few assumptions a general database can't make: every row has a timestamp, writes are almost always *appends* at the current time (never random updates to old rows), and reads are almost always *ranges* over time. With those assumptions it can do things a generic store can't — order data physically by time so a range scan is sequential, aggressively compress long runs of similar values, and expire old data with a simple time-based retention policy. It is purpose-built to **absorb a high write throughput from clients** and serve time-bucketed reads.

**Why a TSDB and not a column store?** A column store (the kind we used in the [ETL post](16-storage-engine-etl-cdc.md)) is built for *analytical scans across columns* — "average revenue by region over all history." A TSDB is built for *the time axis specifically*: high-rate appends ordered by time, retention windows, downsampling old data to coarser granularity. Impressions are a firehose of timestamped events you query by recent window — that is the TSDB's home turf, not the column store's. Use a column store when the query is "slice and aggregate across many dimensions"; use a TSDB when the query is "what happened in this time range."

The TSDB holds the **raw, durable event log**. But — and this is the pivot — we are *not* going to answer reach queries by scanning raw events out of the TSDB. That's the naive set-union we just killed. The TSDB is the system of record; the *fast count* needs a different structure entirely.

> **Memory hook:** *append-only, time-ordered, queried-by-range = a time series, so the durable log lives in a TSDB (Influx/Datadog-style), not a column store. But the raw log is the system of record, not the fast path — scanning it is the naive approach we just rejected.*

---

## Section 4 — The reframe: we only want the *length* of the set

**Question: what is the single thing the dashboard reads off every one of those expensive sets? And what if that were all we ever stored?**

Go back to the costs in Section 2. Every one of them — network, memory, CPU — exists to faithfully preserve **which** users were in the set. But look at the actual output: `PFCOUNT` of the union, a single integer. **5.** **1,000,000.** The dashboard reads the *length* of the set and discards everything else.

So we never needed the elements. We needed `|S|` — the **cardinality**. And the requirements already granted us the crucial license: the count may be **approximate**.

That converts the whole system into a classic, well-studied problem:

> <span style="color:#ff8bd2"><strong>The Cardinality Estimation Problem:</strong></span> *efficiently approximate the number of distinct elements in a stream — and the cardinality of unions of such streams — without storing the elements themselves.*

If we can keep a tiny fixed-size sketch per minute that answers "about how many distinct ids have I seen?" and that can be **merged** with other sketches (so unions still work), then all three costs vanish at once: nothing to ship, almost nothing to store, near-instant to count.

That sketch exists. It's called HyperLogLog.

> **Memory hook:** *the only output is the set's length, and approximate is allowed — so the real problem is cardinality estimation: approximate |distinct| (and |unions|) without keeping the elements.*

---

## Section 5 — HyperLogLog: counting distinct without remembering anyone

**Question: how can a few kilobytes estimate "a million distinct users" when storing a million ids takes megabytes — without remembering a single id?**

<span style="color:#ff8bd2"><strong>HyperLogLog (HLL)</strong></span> — built on the <span style="color:#ffff99"><strong>Flajolet–Martin</strong></span> algorithm — is a fixed-size sketch that estimates how many distinct things it has seen. The intuition is a coin-flip game.

Hash each user id to a random-looking bit string. Random bits means: about half of ids start with `0`, a quarter start with `00`, an eighth with `000`. **A long run of leading zeros is rare** — seeing a hash that starts with `000000` (six zeros) is a 1-in-64 event. So if, across everyone you've hashed, the *longest* leading-zero run you've ever seen is 6, a good guess is that you've seen roughly `2^6 = 64` distinct values. The rarest streak you witnessed is a fingerprint of how many distinct things rolled the dice.

That's the whole idea: **the longest leading-zero run you've seen estimates `log2(distinct count)`.** A single run is noisy, so HLL splits the hashes into many buckets (registers), keeps the max leading-zero run per bucket, and averages — turning a wild guess into a tight estimate (standard error around 0.81%). You store only the per-bucket maxes — a small, fixed bit-array, *not* the ids.

<img src="../assets/impression-counting/hyperloglog.svg" alt="HyperLogLog explained as a leading-zeros game, plus its costs and operations. TOP, the intuition: each user id is hashed to a random bit string. A column of example hashes shows runs of leading zeros: 0... is common (half the time), 00... a quarter, 000... an eighth, with a rare hash 000001... highlighted. The rule, boxed: the longest leading-zero run you have ever seen estimates log2 of the distinct count — a run of 6 zeros suggests about 2^6 = 64 distinct values, because such a run is a 1-in-64 event. A note: HLL splits hashes across many buckets and averages the per-bucket maxima to cut the noise, reaching about 0.81% standard error. MIDDLE, the payoff in a yellow-to-green arrow: 4 MB in an exact set becomes about 12 KB in an HLL — roughly 0.15% of the space — and crucially the size is FIXED no matter how many ids you add. BOTTOM, operations Redis gives out of the box: PFADD to add an element, O(1); PFCOUNT to read the estimated cardinality, O(1); PFMERGE to union N HLLs into one, O(N). A red caveat: you CANNOT delete from an HLL — it is lossy and append-only, like a Bloom filter; if you need exact counts or the actual ids, you must fall back to a real set. Caption: HLL trades a small, bounded error for a massive, fixed memory saving." width="1000">

The payoff is staggering. The exact set that cost <span style="color:#ff8a8a"><strong>4 MB</strong></span> becomes about <span style="color:#8aff8a"><strong>12 KB</strong></span> in an HLL — roughly <span style="color:#8aff8a"><strong>0.15%</strong></span> of the space. And the size is **fixed**: it's ~12 KB whether you add a thousand ids or a billion, because you're only ever updating per-bucket maxima, never appending elements. That fixed, tiny footprint is what makes the whole system affordable.

Better still, you don't implement any of this. <span style="color:#ff8bd2"><strong>Redis ships HLL as a native type</strong></span> — treat it as a black box with three operations:

| Operation | Redis command | Cost | Meaning |
| --- | --- | --- | --- |
| Add an element | <span style="color:#ff8bd2"><strong>`PFADD`</strong></span> | `O(1)` | record that this user viewed |
| Count distinct | <span style="color:#8aff8a"><strong>`PFCOUNT`</strong></span> | `O(1)` | estimated cardinality |
| Union HLLs | <span style="color:#ffd27f"><strong>`PFMERGE`</strong></span> | `O(N)` | merge N sketches into one |

`PFMERGE` is the magic that makes windows work: the union of two HLLs is itself an HLL, and its count estimates the distinct union — exactly the "union the minutes in range" operation from Section 1, now in kilobytes instead of gigabytes.

One honest limitation: <span style="color:#ff8a8a"><strong>you cannot delete from an HLL.</strong></span> It's lossy and append-only, just like a [Bloom filter](19-storage-engine-fast-kv-db.md) — there's no element to remove because no elements are stored. If a use case truly needs exact counts or the actual ids, you fall back to a real set and pay for it. For reach, approximate-and-tiny wins every time.

> **Memory hook:** *HLL estimates distinct count from the rarest leading-zero run it has seen — store per-bucket maxima, never ids. 4 MB → 12 KB (~0.15%), fixed size forever. Redis gives PFADD/PFCOUNT/PFMERGE out of the box; the catch is you can't delete (lossy, like a Bloom filter).*

---

## Section 6 — Bucketing: one HLL per post, per minute

**Question: HLL counts a stream. But the dashboard asks about arbitrary windows. How do we slice the firehose so any window is answerable?**

Combine Section 1 (one bucket per minute) with Section 5 (each bucket is an HLL, not a set). The data model becomes one HLL keyed by **post + minute**:

```text
key                              value
p1729:20220401_1200   =   HLL { a, b, c, d, e, f }   <- post-id : timestamp(minute)
p1729:20220401_1300   =   HLL { a, c, z, w, x }
p1729:20220401_1400   =   HLL { c, d, f, l, z }
```

<img src="../assets/impression-counting/windowed-hll.svg" alt="Per-post, per-minute HyperLogLog bucketing and how range queries fold over it. Top: the key schema is post-id colon timestamp-at-minute, mapping to an HLL value. Three example rows for post p1729: 20220401_1200 = HLL{a,b,c,d,e,f}, 20220401_1300 = HLL{a,c,z,w,x}, 20220401_1400 = HLL{c,d,f,l,z}. Each arriving view event does one PFADD into the current minute's HLL, O(1), so writes are cheap and the HLLs stay a fixed ~12 KB. Below, two query examples. Query one, total unique visitors across all three minutes: temp = PFMERGE(1200, 1300, 1400) creates a new HLL by unioning the minute buckets, then return PFCOUNT(temp), the cardinality of the union. Query two, unique visitors between 1200 and 1359: temp = PFMERGE(1200, 1300) over just the minutes in range, then PFCOUNT(temp). The highlighted lesson: every range query is the same two-step fold — PFMERGE the minute-buckets that fall in the window, then PFCOUNT — and because n is chosen at query time, nothing needs to be pre-aggregated; the minute buckets are the universal building block." width="1000">

The key is literally `post-id : timestamp-at-minute-granularity`, and the value is a per-minute HLL. Two things fall out immediately:

- **Write** is one `PFADD` per event into the current minute's HLL — `O(1)`, and the bucket stays a fixed ~12 KB no matter how viral the post gets.
- **Read** for any window is the Section-1 fold, now in HLL form:

```text
unique over 12:00–14:00:
  temp = PFMERGE(p1729:..1200, p1729:..1300, p1729:..1400)   # union the minutes
  return PFCOUNT(temp)                                        # cardinality of the union

unique over 12:00–13:59:
  temp = PFMERGE(p1729:..1200, p1729:..1300)
  return PFCOUNT(temp)
```

Because *n* is chosen at query time and the minute buckets compose by `PFMERGE`, we never pre-aggregate anything. The per-minute HLL is a **universal building block**: any window the advertiser dreams up is just "merge the minutes in range, count." This is the foundation; everything left is architecture around it.

> **Memory hook:** *key = post:minute, value = one HLL. Writes are O(1) PFADDs into the current minute; any range read is PFMERGE the minutes in window, then PFCOUNT. Per-minute HLLs are the universal building block — no pre-aggregation, ever.*

---

## Section 7 — The write path: from view event to HLL

**Question: a billion view events a day are flying around. How does each one end up `PFADD`-ed into the right minute's HLL — and which events should never be counted at all?**

We already have the firehose machinery from the [YouTube view counter](30-youtube-views-counter.md): clients emit view events, and they land in <span style="color:#93c5fd"><strong>Kafka</strong></span>. From there the write path is a short pipeline.

<img src="../assets/impression-counting/write-path.svg" alt="The write path for impression counting, left to right. Users (stick figures) emit view events into stacked client-facing API servers, which publish onto a Kafka stream (drawn as a horizontal tube). The stream forks: one branch archives raw events into a TSDB cylinder (the durable system of record), the other feeds a RULE ENGINE / FILTER box (orange) that drops unwanted events using a side Rules store — examples listed: 0 views if a user watches their own video, a minimum watch time of 5 minutes to count, and more than 10 views in 1 minute from one user counts as 0 (bot/replay guard). The surviving 'views to count' flow into a fleet of COUNTING consumers (pink, stacked), which for each event do a single PFADD into the corresponding post-and-minute HLL inside Redis (cylinder). A concrete command is shown: PFADD p1729:20220401_1720 user_123 — add user_123 to post p1729's HLL for minute 20220401_1720. A note: events always move forward in time, so a consumer only ever writes the current (or very recent) minute bucket. Caption: Kafka absorbs the firehose, the rule engine filters, counting consumers fan out PFADDs into per-minute HLLs in Redis, and the raw log is archived to the TSDB in parallel." width="1000">

The path has four moves:

1. <span style="color:#8aff8a"><strong>Ingest.</strong></span> Clients send view events to API servers, which publish them onto a <span style="color:#93c5fd"><strong>Kafka</strong></span> stream. Kafka absorbs the spiky, high-throughput firehose so nothing downstream has to.
2. <span style="color:#93c5fd"><strong>Archive.</strong></span> One branch of the stream lands raw events in the <span style="color:#93c5fd"><strong>TSDB</strong></span> — the durable, replayable system of record from Section 3.
3. <span style="color:#ffd27f"><strong>Filter (rule engine).</strong></span> Before counting, a rule engine drops events that shouldn't reach the metric:
   - a creator viewing their **own** video counts as `0`,
   - a view under the **minimum watch time** (say 5 s/5 min for the format) doesn't count,
   - **more than 10 views in a minute** from one user is a replay/bot and counts as `0`.
4. <span style="color:#ff8bd2"><strong>Count.</strong></span> A fleet of <span style="color:#ff8bd2"><strong>counting consumers</strong></span> reads the surviving events and, for each, does one `PFADD` into the post's current-minute HLL in <span style="color:#ffff99"><strong>Redis</strong></span>:

```text
PFADD  p1729:20220401_1720  user_123
```

One detail makes the whole thing tractable: <span style="color:#93c5fd"><strong>events always move forward in time.</strong></span> A consumer is essentially always writing the *current* (or very recent) minute's bucket, never reaching back to rewrite ancient history. That property is what lets us, in the next section, safely evict old minutes from Redis.

> **Memory hook:** *clients → Kafka (absorbs the firehose) → fork: archive raw to the TSDB, and filter via the rule engine → counting consumers PFADD each survivor into its post:minute HLL in Redis. Events move forward in time, so consumers only ever touch recent buckets.*

---

## Section 8 — The read path: answering a reach query

**Question: an advertiser's dashboard asks "unique viewers, 10:00–10:05." Who computes the answer, and what exactly do they run?**

Keep the read path **separate** from the write path — a different fleet of machines so heavy dashboard queries never slow down ingestion.

<img src="../assets/impression-counting/read-path.svg" alt="Read and write paths drawn together but kept separate. TOP, the WRITE PATH (green label): Kafka stream feeds a fleet of COUNTING consumers (pink, stacked) which PFADD into Redis (cylinder) — the same ingestion path as before, shown compactly. BOTTOM, the READ PATH (green label): customers (three stick figures) send reach queries to a separate fleet of ANALYTICS ENGINE machines (blue, stacked), which read HLLs from the same Redis. The flow described beside it: depending on the request's time window, the analytics engine fetches the relevant per-minute HLLs from Redis, PFMERGEs them into one temporary HLL, runs PFCOUNT to get the cardinality, and returns the number to the dashboard. The two Redis-touching commands are highlighted: PFMERGE and PFCOUNT. A separation note: write fleet and read fleet are different machines sharing only Redis, so a spike of dashboard queries cannot stall event ingestion and vice versa. At the bottom a looming question in yellow: does it work at scale? YouTube has millions of videos, millions of users, billions of events — keeping every minute's HLL for every post in Redis forever is very costly, which sets up the next section." width="1000">

The read path is short and mirrors Section 6 exactly:

1. The advertiser's dashboard hits a fleet of <span style="color:#93c5fd"><strong>analytics-engine</strong></span> machines — separate from the counting consumers, so query load and ingest load never fight.
2. For the requested window, the analytics engine fetches the relevant per-minute HLLs from Redis, then runs:
   - <span style="color:#ffd27f"><strong>`PFMERGE`</strong></span> to union the minutes in range into one temporary HLL,
   - <span style="color:#8aff8a"><strong>`PFCOUNT`</strong></span> to read its estimated cardinality,
   - and returns that single number to the dashboard.

That's the entire reach query: **merge the minutes, count, respond.** No raw ids leave Redis; the heaviest object in flight is a 12 KB sketch. Compare that to Section 2, where the same query shipped millions of ids and chewed hundreds of megabytes — same answer, ~0.15% of the cost.

But a shadow is already falling across this design. <span style="color:#ff8a8a"><strong>Does it work at scale?</strong></span> YouTube has millions of videos, millions of users, billions of events. One HLL per post per minute, kept in Redis *forever*, is a lot of expensive RAM. That's the last problem to solve.

> **Memory hook:** *read path = a separate analytics-engine fleet that PFMERGEs the in-window minute HLLs and PFCOUNTs the result — merge, count, respond, with only 12 KB sketches in flight. Write and read fleets share only Redis, so neither stalls the other.*

---

## Section 9 — The scale problem: Redis can't hold all of history

**Question: how much HLL is there, really? And why does that break the "just keep it in Redis" plan?**

Do the back-of-envelope. Suppose ~1,000,000 ads/posts are active, each accumulating one HLL per minute, and we keep history for a year:

```text
365 days × 24 h × 60 min × 1,000,000 posts × (HLL size)
≈ 10^13 minute-buckets  ->  terabytes of HLLs
```

Even at 12 KB each — even after the heroic 0.15% saving — a year of per-minute HLLs for every post is **terabytes**. Redis is RAM; RAM is the most expensive storage you have. <span style="color:#ff8a8a"><strong>You cannot keep all of history in Redis.</strong></span>

But notice two facts the write path already handed us:

1. <span style="color:#93c5fd"><strong>Events always move forward in time.</strong></span> Once a minute is a few minutes in the past, **no new writes will ever touch it.** Its HLL is finished — immutable.
2. <span style="color:#ffff99"><strong>An HLL is just a byte array.</strong></span> Like a Bloom filter, it's a fixed blob of bits. You can `GET` it out of Redis as raw bytes and store those bytes anywhere — even in a database that has never heard of HyperLogLog.

Together these say: keep only the **hot, still-being-written** minutes in Redis, and push the **cold, finished** minutes to cheap storage. That's a two-tier store.

> **Memory hook:** *a year of per-minute HLLs for every post is terabytes — too much for RAM. But finished minutes never change (events move forward) and an HLL is just a byte blob you can GET out — so keep hot minutes in Redis and evict cold ones to cheap storage.*

---

## Section 10 — Two-tier storage: hot Redis, cold DynamoDB

**Question: which minutes stay in fast memory, where do the rest go, and what happens on a query that needs a minute that's already been evicted?**

Split storage by temperature:

- <span style="color:#ff8bd2"><strong>Hot tier — Redis.</strong></span> Keep only the last ~30 min / 1 hour of minute-HLLs, where all the *writes* are happening. Small, fast, expensive — and that's fine because it's tiny.
- <span style="color:#ffff99"><strong>Cold tier — DynamoDB.</strong></span> Older, finished HLLs are serialized to their raw byte array and stored as <span style="color:#ffff99"><strong>blobs</strong></span> in a cheap key-value store. DynamoDB doesn't understand HLLs — it doesn't need to; it just holds `key -> bytes`.

<img src="../assets/impression-counting/tiered-storage.svg" alt="Two-tier hot/cold storage for HLLs with the full read and write paths. TOP, write path: users emit events into Kafka (tube), counting consumers (pink, stacked) PFADD into Redis (cylinder, labelled '2 hours / last 30 min–1 hour of minute buckets, hot'), and raw events are also archived to a TSDB. BOTTOM-RIGHT, cold tier: a DynamoDB cylinder (DDB) holds older HLLs as serialized byte-array blobs, labelled 'cheap KV storage, data is anyway immutable.' Pink arrows show counting consumers/Redis flushing down to DDB; blue arrows show the analytics engine reading up from both Redis and DDB. BOTTOM-LEFT, read path: customers (stick figures) query a fleet of ANALYTICS ENGINE machines (blue, stacked) which gather the in-window minute HLLs — recent ones from Redis, older ones from DDB — PFMERGE and PFCOUNT them. The lifecycle rules are listed, numbered: (1) periodically, every ~10 seconds, finished HLLs are copied from Redis to DDB via GET key -> raw bytes like \x0a0b1x29; (2) HLLs are always updated in Redis first; (3) if a key is not in Redis it is brought back from DDB and loaded into Redis; (4) for an analytics query, all relevant HLLs are pulled into Redis and then cardinality is computed. A note explains the late-write case: if a write arrives for an already-evicted minute, load that HLL from DDB into Redis, PFADD, and flush it back. Caption: Redis is the hot write buffer and merge engine; DDB is the cheap immutable archive; the analytics engine pulls cold blobs up to Redis on demand because only Redis understands HLL operations." width="1000">

The lifecycle, made concrete:

1. <span style="color:#ff8bd2"><strong>Flush hot → cold.</strong></span> Periodically (say every 10 s), finished minute-HLLs are copied from Redis to DynamoDB. The copy is trivial because an HLL is bytes: `GET p1729:20220401_1200` returns a raw byte array (`\x0a0b1x29...`), and those bytes become the DynamoDB blob.
2. <span style="color:#ff8bd2"><strong>Writes always hit Redis first.</strong></span> New events `PFADD` into the current minute's HLL in Redis, exactly as before.
3. <span style="color:#8aff8a"><strong>Read miss → pull cold up to hot.</strong></span> A query for an old window needs minutes that may have been evicted. If a key isn't in Redis, the analytics engine fetches the blob from DynamoDB, loads it back into Redis, and proceeds. Why round-trip through Redis? Because **only Redis understands `PFMERGE`/`PFCOUNT`** — DynamoDB stores the bytes but can't operate on them. So every reach query first stages all the HLLs it needs into Redis, then merges and counts there. After the query, those re-loaded keys can carry a short TTL and expire again, since the durable copy is safe in DynamoDB.
4. <span style="color:#ffd27f"><strong>Late write after eviction.</strong></span> The rare straggler — an event for a minute already flushed and evicted — is handled the same way: load the HLL from DynamoDB, `PFADD` the late user, and flush the updated blob back. Because the data is **immutable** the moment its minute closes, this almost never happens; events move forward in time.

The division of labor is clean: <span style="color:#ff8bd2"><strong>Redis is the hot write buffer and the only thing that can merge/count HLLs</strong></span>; <span style="color:#ffff99"><strong>DynamoDB is the cheap, immutable archive of finished sketches</strong></span>; the analytics engine shuttles cold blobs up to Redis on demand. You pay for expensive RAM only for the live working set, and pennies-per-GB for the long tail of history.

> **Memory hook:** *hot minutes live in Redis (writes + the only place PFMERGE/PFCOUNT run); finished minutes are flushed every ~10 s to DynamoDB as raw HLL byte-blobs (cheap, immutable). On a read or late-write miss, pull the blob up into Redis, operate, optionally re-evict. RAM cost tracks the working set, not all of history.*

---

## The complete map

**Question: with every piece in place, what is the whole system in one picture — write path, read path, and the two storage tiers?**

<img src="../assets/impression-counting/final-map.svg" alt="The complete impression-counting architecture in one reference map. Left: users emit view events into client API servers, which publish to a Kafka stream (horizontal tube). The stream forks three ways: (a) raw events archived to a TSDB cylinder (durable system of record, blue); (b) into a rule-engine/filter (orange) that drops self-views, too-short views, and bot replays; (c) the filtered events into a fleet of counting consumers (pink, stacked). The counting consumers PFADD each event into a per-post-per-minute HyperLogLog in Redis (hot tier, yellow cylinder, labelled 'last ~1 hour of minute HLLs'). A flush arrow (pink) runs every ~10 seconds from Redis down to DynamoDB (cold tier, cylinder), where finished HLLs are stored as serialized byte-array blobs — cheap, immutable. On the read side: customers' dashboards query a separate fleet of analytics-engine machines (blue, stacked) over the read path (green); the analytics engine gathers the in-window minute HLLs — recent ones from Redis, older ones pulled up from DynamoDB into Redis — then runs PFMERGE to union them and PFCOUNT to get the estimated unique reach, returning a single number to the dashboard. A legend maps the colors: green = read/serve path, pink = write/count path, blue = control/async plane (Kafka, TSDB, analytics engine), yellow = HLL storage and consistency, orange = rule-engine transform. The one-line summary across the bottom: events fan in through Kafka and the rule engine, counting consumers PFADD into per-minute HLLs in hot Redis, finished HLLs age out to cheap DynamoDB blobs, and reach queries PFMERGE-plus-PFCOUNT the relevant minutes — turning 'count distinct over an arbitrary window' from a 1200 GB set problem into a kilobyte sketch problem." width="1180">

Read the map as two journeys that meet at Redis:

- The <span style="color:#ff8bd2"><strong>write path</strong></span> (left to right): clients → Kafka → \[archive to TSDB] + \[rule-engine filter] → counting consumers → `PFADD` into per-minute HLLs in hot Redis → flushed every ~10 s into cold DynamoDB blobs.
- The <span style="color:#8aff8a"><strong>read path</strong></span> (bottom): dashboards → analytics engine → gather in-window minute HLLs (recent from Redis, older pulled up from DynamoDB) → `PFMERGE` + `PFCOUNT` → one number.

Every hard requirement from the brief is now answered by a specific piece: *distinct* by HLL, *arbitrary n* by per-minute bucketing and `PFMERGE`, *real-time* by Redis + Kafka, *approximate-is-ok* by HLL's 0.81% error, *filtering* by the rule engine, and *affordability at scale* by the hot/cold split.

> **Memory hook:** *fan in through Kafka + rule engine → counting consumers PFADD per-minute HLLs in hot Redis → age finished HLLs out to cheap DynamoDB blobs → reach queries PFMERGE+PFCOUNT the in-window minutes. "Count distinct over any window" went from a 1200 GB set problem to a kilobyte sketch problem.*

---

## Questions that complete the mental model

**Why is approximate acceptable here when so much of systems design is about exactness?** Because the *consumer of the number* is a human reading a graph. The decision an advertiser makes on "reach ≈ 1,000,000" is identical to the one they'd make on the exact 1,001,238. The 0.81% error is invisible at the resolution of a dashboard. Exactness would cost 600× the memory to change no decision — that's the trade HLL exists to make. (Contrast a flash sale's inventory count, where being off by one means overselling — there, approximate is forbidden.)

**Why per-minute buckets specifically — why not per-second or per-hour?** Granularity is a direct trade between query flexibility and bucket count. Per-second buckets let you answer "last 10 seconds" but multiply the number of HLLs (and flush traffic) 60×. Per-hour buckets are cheap but can't answer "last 5 minutes." Per-minute is the usual sweet spot: fine enough for a live graph, coarse enough that a year of buckets is merely terabytes, not petabytes.

**Why route cold reads back through Redis instead of computing on DynamoDB?** Because the HLL *operations* — merge and count — live only in Redis. DynamoDB stores the sketch as opaque bytes; it has no `PFMERGE`. So any query touching evicted minutes must stage those blobs back into Redis, operate there, and (optionally) let them expire again. DynamoDB is the wallet; Redis is the calculator.

**What happens to a late event for a minute that's already been evicted?** Load that minute's HLL blob from DynamoDB, `PFADD` the straggler, flush it back. It's correct but slightly expensive, which is fine because it's rare — events move forward in time, so a minute is "done" within a few minutes of real time and stragglers are the exception, not the rule.

**Can two advertisers share work?** Yes — that's the beauty of `PFMERGE`. Overlapping windows, rolling "last hour" graphs that slide one minute at a time, campaign-level totals across many posts — all are just different unions of the same per-minute building blocks. You compute each minute's HLL once and reuse it across every query that touches that minute.

> **Memory hook:** *approximate is fine because a human reads the graph and the 0.81% error changes no decision; per-minute balances flexibility against bucket count; cold reads route through Redis because only Redis can merge/count; late events reload-add-reflush but are rare; and overlapping queries all reuse the same per-minute HLL building blocks.*
</content>
</invoke>
