# Designing a YouTube View Counter: Counting a Firehose Without Melting the Database

This post builds a **YouTube view counter** from first principles: the machinery that watches millions of people watch videos at once and turns that firehose into a single number under each video — `1,402,318 views` — without ever letting the write load touch, let alone melt, a database. We start from the dumbest possible thing (the API does `UPDATE videos SET views = views + 1`) and grow each component only when a named bottleneck appears: a durable log to absorb the writes, a rule engine to throw out the views that don't count, a second log so other teams can reuse the clean stream, and a partitioned, batching counter fleet that turns a billion increments into a handful of writes.

**Question: a hundred million people are watching YouTube right now. Every player sends a heartbeat every few seconds. You must turn that into an accurate, *filtered* view count — not counting a creator watching their own video, not counting a bot farm, not counting the same person on a loop — and you may never put that write load directly on a database. What is the smallest design that is correct, and what is the *next* thing that breaks every time the traffic 10×s?** The honest path runs straight through a rejected direct-write, a hard choice between two queues, a filtering stage, a *second* queue that surprises people, and a counting fleet that only works because of how it's partitioned — and by the end you've hand-built the counting pipeline that sits under YouTube, ad-click counters, and every "likes / impressions / views" number at scale.

This post picks up a thread we left dangling. In the [YouTube processing pipeline](23-high-throughput-youtube-pipeline.md) we built the Watchtime service and had it emit `ON-VIEW` events onto [Kafka](16-storage-engine-etl-cdc.md) — "the raw demand signal" — and then walked away. *This* post is what happens to those events: how you count them. It leans on three earlier ideas: [event-driven architecture and what a Kafka stream is good at](16-storage-engine-etl-cdc.md), why [high-write systems go log-structured](19-storage-engine-fast-kv-db.md), and how [range/hash partitioning places data on nodes](20-high-throughput-system-s3.md).

> **Memory hook:** *a view counter is a write-throughput problem wearing a counting costume. The number is trivial; surviving the firehose of increments — and throwing out the ones that don't count — is the whole design. Absorb writes in a log, filter in a consumer, fan the clean stream out to a second log, then batch-and-count in a partitioned fleet.*

---

## The brief

**Question: before drawing a single box — what *is* a view counter, stripped down? It looks like one integer. Why is it a system at all?**

<img src="../assets/youtube-views-counter/requirements.svg" alt="The brief for a YouTube view counter, framed as 'it's a write-throughput problem, not a counting problem.' Top: the deceptively simple output — a video thumbnail with a single number under it, 1,402,318 views — labelled 'the easy part: an integer.' Below, three requirements each with a consequence. One, COUNT VIEWS: every watch produces an event, and at YouTube scale that is millions of events per second, so the hard part is INGESTION, surviving the write firehose, not storing one number. Two, FILTER VIEWS (rule engine): not every watch is a real view — a creator watching their own upload, a bot farm, a few-second accidental open, the same viewer on a loop — so a rule engine must drop the events that don't count before they are added. Three, EVENTUALLY CONSISTENT IS FINE: nobody is harmed if the public count is off by a few for a few seconds, so we are allowed to trade real-time exactness for throughput — this single relaxation is what makes the whole design possible. Below, the three hard problems the post attacks in order: INGESTION — absorb millions of writes per second without touching a database; FILTERING — apply the rule engine cheaply, once, and reuse the result; AGGREGATION — turn a billion +1 increments into a handful of database writes. A final highlighted insight at the bottom, marked 'the unlock': you are never allowed to write a view straight to the database — every layer that follows exists to keep that write load away from durable storage until it has been filtered and collapsed into a batch." width="1000">

YouTube's view counter looks like the most trivial thing in the building: a column, `views`, and you add one. If that were the whole story this post would be one line of SQL. It is a system because of the *rate*. A popular video during a premiere can draw **millions of concurrent watchers**, and every player emits a <span style="color:#ff8bd2"><strong>heartbeat</strong></span> every few seconds ("still watching, at second 35"). The output is one integer; the input is a <span style="color:#ff8a8a"><strong>firehose</strong></span> of writes aimed at that integer.

The requirements are three, and each one forces a layer later in the post:

- <span style="color:#ff8bd2"><strong>Count views.</strong></span> Every watch is an event. At scale that's millions of events per second, so the genuinely hard requirement hiding inside "count" is <span style="color:#ff8a8a"><strong>ingestion</strong></span> — absorbing the write firehose — not storing the result.
- <span style="color:#ffff99"><strong>Filter views with a rule engine.</strong></span> Not every watch is a *view*. A creator refreshing their own upload, a click farm, a two-second mis-tap, the same viewer looping — these must be **thrown out before they're counted**. Counting is easy; counting *only the real ones* needs a filtering stage.
- <span style="color:#8aff8a"><strong>Eventual consistency is fine.</strong></span> This is the gift that makes everything possible. **Nobody is harmed if the public count is stale by a few seconds or off by a handful.** YouTube famously freezes counts at "301+" while it verifies — proof that the count is *allowed* to lag. Because we don't need the number exact-to-the-instant, we can trade real-time precision for throughput everywhere.

Three things are genuinely hard, and the rest of the post just attacks them in order: <span style="color:#ff8a8a"><strong>ingestion</strong></span> (millions of writes/sec, nowhere near a database), <span style="color:#93c5fd"><strong>filtering</strong></span> (apply the rule engine cheaply, *once*, and reuse it), and <span style="color:#ffff99"><strong>aggregation</strong></span> (collapse a billion `+1`s into a few writes).

And one rule reframes everything: **you are never allowed to write a view straight to the database.** Every layer we add exists to keep that write load *away* from durable storage until it has been filtered down and collapsed into a batch. Hold onto that — it's the thread the whole post pulls.

> **Memory hook:** *the output is one integer; the input is a firehose. The three hard problems are ingestion, filtering, and aggregation — and the iron rule is that a raw view never touches the database.*

### The vocabulary, in one place

- <span style="color:#ff8bd2"><strong>View event</strong></span> — one heartbeat from a player: roughly `{user, video, watched_seconds, ts}`. The *write*. Emitted every few seconds while a video plays.
- <span style="color:#93c5fd"><strong>Producer</strong></span> — anything that *appends* events to the log (here, the Watchtime API server).
- <span style="color:#93c5fd"><strong>Consumer</strong></span> — a process that *reads* events from the log and does work (filter, count).
- <span style="color:#93c5fd"><strong>Topic / partition</strong></span> — a Kafka topic is a named stream; it's split into **partitions**, each an ordered, append-only log. A partition is the **unit of parallelism** — one partition is read by exactly one consumer in a group.
- <span style="color:#93c5fd"><strong>Consumer group</strong></span> — a set of consumers that split a topic's partitions between them. Add consumers (up to the partition count) to go faster.
- <span style="color:#ffff99"><strong>Offset</strong></span> — a consumer's bookmark: "I've processed up to position N in this partition." Committing it is how progress survives a crash.
- <span style="color:#ffff99"><strong>Rule engine</strong></span> — the component that decides whether an event *counts*, by evaluating it against a set of rules (own-view, bot, replay, min-watch-time).
- <span style="color:#ffff99"><strong>Batching</strong></span> — accumulate many increments in memory, then write the *sum* once. Turns N writes into 1.
- <span style="color:#ff8a8a"><strong>Hot partition</strong></span> — one partition (one viral video) taking far more traffic than its consumer can handle while its siblings idle. The recurring villain.

---

## Section 1 — Day Zero: the view event, written straight to the database

**Question: forget scale. A video plays, a view happens. What is the absolute smallest thing that already counts it — something you'd write this afternoon?**

A view is a thing that happened, so the client tells the server it happened. The player fires a <span style="color:#ff8bd2"><strong>view event</strong></span> at an API server, and the API server does the obvious thing: it adds one to that video's row.

<img src="../assets/youtube-views-counter/day-zero-direct-write.svg" alt="Day-zero view counter and why it melts, in two panels. LEFT PANEL, the naive design: several viewers (stick figures) each send a 'view event' to a single API server box; the API server runs the SQL 'UPDATE videos SET views = views + 1 WHERE id = v' directly against a Counts DB drawn as a cylinder. A caption: logically this is perfectly correct — a view happened, we added one. RIGHT PANEL, why it breaks at scale (outlined in red): the same picture but with millions of viewers of ONE popular video, all of whose increments target the SAME ROW. Three red failure labels point at the database: (1) ROW-LOCK CONTENTION — every increment must take a write lock on the one hot row, so the writes serialize into a single-file line no matter how many API servers you add; (2) WRITE AMPLIFICATION — every +1 is a full durable transaction (WAL/redo log flush, page write, index update) just to change one number; (3) CONNECTION / IOPS EXHAUSTION — millions of writes per second blow past the database's connection pool and disk IOPS. The result, in a red banner: the database is the bottleneck, and adding API servers makes it WORSE, because they all hammer the same row harder. A small note at the bottom: the heartbeat makes it worse still — every player re-emits every few seconds, multiplying the write rate. Takeaway: the write is logically trivial and operationally fatal; the rest of the post is about never doing this." width="1000">

The whole thing is one statement:

```sql
UPDATE videos SET views = views + 1 WHERE id = :video_id;
```

**Is this correct?** Logically, yes — completely. A view happened; we added one; a read of the row returns the right number. For a small site this is not just fine, it's *right* — don't build a pipeline for a thousand views a day. So why is it the centerpiece of what *not* to do?

**Because of the hot row.** When millions of people watch *the same* video, every one of those increments targets the **same database row**, and three things pile up at once:

- <span style="color:#ff8a8a"><strong>Row-lock contention.</strong></span> `views = views + 1` is read-modify-write; it must take a write lock on that one row. So no matter how many API servers you run, the increments **serialize** into a single-file queue behind that lock. Your parallelism collapses to one.
- <span style="color:#ff8a8a"><strong>Write amplification.</strong></span> Changing one integer still costs a full durable transaction — a [write-ahead-log](19-storage-engine-fast-kv-db.md) flush, a page write, an index touch. You pay the price of a real write to move a number by one.
- <span style="color:#ff8a8a"><strong>Connection and IOPS exhaustion.</strong></span> Millions of writes per second blow straight past the database's connection pool and disk IOPS budget.

And the heartbeat makes it worse: the player re-emits every few seconds, so it's not one write per view, it's *dozens*. The cruel part is that the database is the bottleneck and **adding API servers makes it worse** — more servers just hammer the one hot row harder. The fix is not a faster database. The fix is to **stop writing to the database on the request path at all.**

> **Memory hook:** *the direct write `views += 1` is logically perfect and operationally fatal — millions of increments hit one hot row, serialize behind its lock, each paying a full durable write. You can't out-scale a hot row by adding API servers. So get the database off the request path.*

---

## Section 2 — Absorb the Firehose: Put a Log in Front

**Question: the request path can't end at the database. Where *can* it end? The API has to do something cheap, durable, and instant — then get out of the way. What's the smallest thing that fits?**

The move is to make the API server a thin <span style="color:#93c5fd"><strong>producer</strong></span>. Instead of doing the expensive, contended write itself, it **appends the view event to a durable log** and immediately returns `200 OK`. Appending to a log is cheap and contention-free (it's a sequential write to the *end* — no hot row, no lock on a shared number), and the database is now nowhere near the request path. Something downstream will read the log and do the counting *later*, at its own pace. We've turned a synchronous write into an <span style="color:#93c5fd"><strong>asynchronous</strong></span> one — and that's the whole point of an **event-driven architecture**: the producer fires an event and forgets; consumers react on their own schedule. (We used exactly this for the [analytics tap in the CDC post](16-storage-engine-etl-cdc.md) and the [fan-out of `ON-PUBLISH`](23-high-throughput-youtube-pipeline.md).)

So we need a queue. **Which one — and why?** The two obvious candidates are **RabbitMQ** (a classic message broker) and **Kafka** (a distributed log). This is the decision people get wrong for the *right-sounding wrong reasons*, so let's be careful.

<img src="../assets/youtube-views-counter/kafka-vs-rabbitmq.svg" alt="Choosing the queue: Kafka vs RabbitMQ for the view firehose, as a decision diagram. Center top: the API producer appends a view event and must hand it to a queue. Two columns compare the candidates. LEFT COLUMN, RabbitMQ (a traditional message broker), drawn as a broker box that PUSHES messages to consumers and deletes each message once acknowledged: strengths listed in white — flexible routing (exchanges, routing keys), per-message acknowledgement, low-latency task delivery, great for request/response and work queues; throughput noted as roughly tens of thousands of messages per second. RIGHT COLUMN, Kafka (a distributed append-only log), drawn as a horizontal tube with sequential log segments that consumers PULL from at their own offset, messages retained not deleted on read: strengths in blue — sequential disk I/O (log-structured), millions of messages per second, consumers pull and can replay, partitions give horizontal parallelism. MIDDLE, the reasoning, with WRONG reasons struck out in grey and the RIGHT reason highlighted: 'because of retention/replay' is marked NOT the deciding reason (both can be configured to retain); 'because of stream processing' is marked NOT the reason here (we are not doing windowed stream joins, just counting); the highlighted RIGHT reason in yellow/blue: THROUGHPUT — this is an extreme write-throughput firehose, and Kafka's log-structured, append-only, sequential-I/O design sustains millions of events per second where RabbitMQ's push-and-ack broker model (designed for tens of thousands) would buckle. A small real-world note: benchmarks commonly cite Kafka at ~1,000,000+ msgs/sec versus RabbitMQ in the tens of thousands; pick the tool by the actual bottleneck. Takeaway banner: match the tool to the bottleneck — for a firehose you want a log, not a broker." width="1000">

Both can durably hold messages. Both can have multiple consumers. So the temptation is to pick on a feature checklist — and most of those features are **not** the deciding factor here:

- **"Kafka, because retention / replay."** Tempting, but not the reason. You *can* configure durability and redelivery on both; retention alone doesn't pick the tool.
- **"Kafka, because stream processing."** Also not it. Kafka Streams is powerful, but we are not doing windowed stream joins — we're counting. Picking Kafka for a capability you won't use is cargo-culting.
- **Separation of concerns.** Note what the queue is *not* for: the queue doesn't filter or count. It only absorbs and buffers. Keep each component doing one job — filtering belongs in the next stage, not bolted onto the transport.

The actual deciding factor is the one requirement we've been circling: <span style="color:#ff8a8a"><strong>raw write throughput</strong></span>. This is a firehose. <span style="color:#93c5fd"><strong>Kafka is a log</strong></span> — append-only, [log-structured](19-storage-engine-fast-kv-db.md), backed by sequential disk I/O — and that's exactly the access pattern that sustains **~a million-plus messages per second**. <span style="color:#ff8a8a"><strong>RabbitMQ is a broker</strong></span>: it *pushes* each message to a consumer and tracks a per-message acknowledgement, bookkeeping that's wonderful for routing and work-queues but tops out around **tens of thousands per second**. For a firehose, the per-message push-and-ack overhead is the wall. **We pick Kafka because the bottleneck is throughput, and a log beats a broker at throughput by two orders of magnitude.**

The clean way to hold the two tools in your head — **match the tool to the bottleneck:**

| | **Kafka (log)** | **RabbitMQ (broker)** |
| --- | --- | --- |
| Model | consumers **pull** at their own offset; messages **retained** | broker **pushes**; message **deleted** on ack |
| Throughput | **~1M+ msgs/sec** (sequential I/O) | ~tens of thousands/sec |
| Best at | high-volume event streams, log/clickstream/metrics ingestion, replayable pipelines | flexible routing, task queues, request/response, low-latency RPC-style work |
| Reach for it when | you're drowning in a firehose of events | you're dispatching thousands of discrete *tasks* with routing logic |

That last row is the whole lesson. The [task scheduler](29-distributed-task-scheduler.md) and a job-dispatch system want RabbitMQ's routing and per-task acks; a view firehose wants Kafka's log. Same shape on a whiteboard, opposite tool.

> **Memory hook:** *make the API a thin producer: append the event to a log, return instantly, count later (event-driven = fire and forget). Pick Kafka over RabbitMQ not for retention or streaming — for raw throughput. Kafka is a log (sequential I/O, ~1M+/sec); RabbitMQ is a broker (push+ack, ~tens of thousands/sec). Firehose → log; routed tasks → broker.*

---

## Section 3 — The Rule Engine: Not Every Watch Is a View

**Question: events are now streaming safely into Kafka. But half of them shouldn't count — the creator testing their own upload, a bot farm inflating numbers, the same viewer looping, a two-second mis-tap. Where does "is this a *real* view?" live, and what shape does that logic take?**

It lives in a consumer, and the logic has a name: a <span style="color:#ffff99"><strong>rule engine</strong></span>. A consumer reads each event off Kafka, runs it through a set of rules that decide *does this count?*, and **drops the ones that fail**, passing through only the genuine views.

<img src="../assets/youtube-views-counter/rule-engine.svg" alt="The rule engine as a filtering stage between Kafka and counting. Center: a horizontal Kafka tube on the left feeds a fleet of stacked consumer boxes labelled 'Rule Engine (Events Filter)'. Each consumer pulls a raw view event and evaluates it against a set of rules loaded from a 'Rules DB' cylinder drawn above (the rules are config/data, not hardcoded, so they can change without redeploying). A list of example rules is shown as a checklist the engine applies to an event {user u, video v, watched_seconds}: DROP if u is the owner of v (creator watching own video); DROP if watched_seconds is below a minimum threshold (a 2-second mis-tap is not a view); DROP if this (u,v) was already counted within a recent window (de-duplicate replays/loops); DROP if u is a known bot / the IP shows click-farm behavior (fraud). KEEP otherwise. The output: a raw stream coming in — {u1,v1},{u2,v2},{u3,v3},{u1,v1 again},{owner,v2} — and a filtered stream coming out — {u1,v1},{u2,v2},{u3,v3} — with the duplicate and the owner-view struck through in red as 'dropped, did not count'. A side box, 'rule engines are everywhere', lists real analogues: ad-click fraud filtering (Google Ads / InMobi deciding which ad clicks are billable), surge/dynamic pricing (Uber/Swiggy applying pricing rules to a ride/order in real time), payment fraud scoring, and feed ranking eligibility. Takeaway: filtering is its own stage with its own data (Rules DB) — keep it separate from both the API that produced the event and the counter that will consume the clean result." width="1000">

A rule engine is just **business logic expressed as data**, evaluated against each event. Crucially the rules live in a <span style="color:#ffff99"><strong>Rules DB</strong></span> (config, not hardcoded `if`s), so the policy team can change "minimum watch time is 3 seconds" without shipping new code. For each incoming event `{u, v, watched_seconds}` the engine asks, in order:

- **Own-view?** Is `u` the owner of `v`? A creator refreshing their own video doesn't count. <span style="color:#ff8a8a"><strong>Drop.</strong></span>
- **Too short?** Is `watched_seconds` below the threshold? A two-second accidental open isn't a view. <span style="color:#ff8a8a"><strong>Drop.</strong></span>
- **Replay / loop?** Has this `(u, v)` already counted within a recent window? De-duplicate. <span style="color:#ff8a8a"><strong>Drop.</strong></span>
- **Bot / fraud?** Is `u` a known bot, or does the IP look like a click farm? <span style="color:#ff8a8a"><strong>Drop.</strong></span>
- Otherwise — <span style="color:#8aff8a"><strong>keep</strong></span>. It's a real view.

So a raw stream `{u1,v1}, {u2,v2}, {u3,v3}, {u1,v1 again}, {owner,v2}` becomes a clean stream `{u1,v1}, {u2,v2}, {u3,v3}` — the loop and the owner-view struck out.

This pattern is everywhere money or trust is at stake, which is why it's worth recognizing by name:

- <span style="color:#93c5fd"><strong>Ad-click fraud.</strong></span> Google Ads and InMobi run a rule engine over every ad click to decide which clicks are *billable* — filtering out bots and accidental clicks before charging the advertiser. Same shape: a firehose of click events, a rule engine deciding which count.
- <span style="color:#93c5fd"><strong>Surge / dynamic pricing.</strong></span> Uber and Swiggy evaluate each ride/order against pricing rules (demand, supply, time, location) in real time. Rules as data, evaluated per event.
- <span style="color:#93c5fd"><strong>Payment fraud scoring</strong></span> and **feed-ranking eligibility** are the same idea wearing different clothes.

The design principle the rule engine teaches is <span style="color:#8aff8a"><strong>separation of concerns</strong></span>: filtering is its *own* stage with its *own* data (the Rules DB). It does not belong in the API (which should just produce events as fast as possible) and it does not belong in the counter (which should just count). One component, one job. Hold that principle — it's about to force the most surprising decision in the whole design.

> **Memory hook:** *a rule engine is business logic as data, run over each event to decide "does this count?" — drop own-views, sub-threshold watches, replays, and bots; keep the rest. It's a consumer reading Kafka and consulting a Rules DB. Same pattern as ad-click-fraud filtering and surge pricing. Keep filtering as its own stage — not in the API, not in the counter.*

---

## Section 4 — The Surprising Layer: Re-Ingest the Clean Stream into a *Second* Kafka

**Question: the rule engine has produced a clean stream of real views. The counter is right there. Why not just have the rule-engine consumer write the counts to the database itself? Why would anyone add *another* Kafka in the middle?**

This is the step that looks like over-engineering and isn't. Let's first try the direct thing and find the crack. The rule-engine consumer has `{u1,v1}, {u2,v2}` in hand — clean, real views. The obvious move: count them and write. But pause on a different question: **who else wants this clean stream?**

Because filtering is *expensive* and *valuable*, the views counter is **not** the only team that wants real-views-only. The <span style="color:#93c5fd"><strong>monetization team</strong></span> wants exactly the same filtered events to compute ad revenue and creator payouts. So does recommendations. So does trending. Now look at what happens if each team filters for itself:

<img src="../assets/youtube-views-counter/second-kafka-fanout.svg" alt="Why the filtered stream is re-ingested into a second Kafka, shown as a bad design versus a good design. TOP, THE BAD DESIGN (outlined in red): the raw Kafka feeds THREE separate copies of the heavy Rule Engine — one owned by Views, one by Monetization, one by Recommendations — each independently consuming the raw firehose and re-running the same expensive filtering. Three red problems are labelled: (1) WASTED COMPUTE — the heavy rule engine runs three times over the same firehose; (2) INCONSISTENT TRUTH — each team's slightly different filter produces a slightly different 'real view', so Views and Monetization disagree on what counted, which is a correctness/billing nightmare; (3) TIGHT COUPLING — every new consumer must re-implement filtering and re-read the giant raw stream. BOTTOM, THE GOOD DESIGN (outlined in green): the raw Kafka feeds ONE Rule Engine fleet, whose clean output is PUBLISHED ONCE onto a SECOND Kafka topic (drawn as a second horizontal tube labelled 'filtered-views, the single source of clean truth'). From that second Kafka, multiple independent consumer groups fan out and each does its OWN downstream processing: a 'Views Counter' group (batch and count into the Count DB), a 'Monetization' group (compute payouts), a 'Recommendations' group. A blue callout states the principle: filter ONCE, fan out MANY — the expensive rule engine runs a single time, every downstream sees the identical clean events (one source of truth), and adding a new consumer is free (just subscribe to the second topic; no re-filtering, no coupling). A note ties it back: this is the same fan-out-with-no-join that Kafka is built for. Takeaway: re-ingesting the filtered stream turns an expensive private result into a shared, consistent, reusable stream." width="1180">

The bad design — **everyone filters for themselves** — fails three ways:

- <span style="color:#ff8a8a"><strong>Wasted compute.</strong></span> The rule engine is *heavy* (it hits the Rules DB, checks bot lists, de-dupes). Running it three times over the same firehose is three times the cost for the same answer.
- <span style="color:#ff8a8a"><strong>Inconsistent truth.</strong></span> This is the real killer. If Views and Monetization each implement "real view" slightly differently, they'll **disagree on what counted** — and now your public view count doesn't match what you paid the creator for. A billing nightmare born from two copies of "the truth."
- <span style="color:#ff8a8a"><strong>Tight coupling.</strong></span> Every new consumer has to re-implement filtering and re-read the giant raw stream.

The fix is to **filter once and fan out many.** The rule engine publishes its clean output **one time** onto a <span style="color:#93c5fd"><strong>second Kafka topic</strong></span> — call it `filtered-views`, the single source of clean truth. Then every downstream is an independent <span style="color:#93c5fd"><strong>consumer group</strong></span> reading that same clean stream and doing *its own* processing: the views counter batches and counts; monetization computes payouts; recommendations updates models. The heavy filtering runs **exactly once**, everyone sees **identical** events, and adding a new consumer is *free* — it just subscribes, no re-filtering, no coupling.

So "why re-ingest into Kafka?" — **because the filtered stream is a reusable asset, and a Kafka topic is how you make one expensive computation serve many independent readers.** This is precisely the [fan-out-with-no-join that Kafka is built for](23-high-throughput-youtube-pipeline.md): one event, many reactors, nobody waits for anybody. The first Kafka absorbed the *raw* firehose; the second Kafka distributes the *clean* result.

> **Memory hook:** *filter once, fan out many. If every team re-runs the heavy rule engine on the raw stream you get triple compute, inconsistent "truth" (Views and Monetization disagree on what counted — a billing bug), and tight coupling. So publish the filtered stream once onto a second Kafka topic; views, monetization, and recommendations each consume the same clean events as independent groups. The second log turns an expensive private result into a shared, consistent asset.*

---

## Section 5 — Counting: Batch, Partition by Video, Increment

**Question: a counter consumer is now reading clean `{u, v}` events off the second Kafka. The naive thing is to do one `UPDATE … views += 1` per event. But that's the *exact* hot-row write we banned in Section 1 — it just moved downstream. So how do we actually turn this clean stream into database rows without re-melting the database?**

Two moves, and they work together: **batching** and **partitioning**.

<img src="../assets/youtube-views-counter/batch-count-partition.svg" alt="How the counter turns a stream of clean events into a few database writes, via partitioning and batching. LEFT: the second Kafka topic 'filtered-views' drawn as a horizontal tube, explicitly split into PARTITIONS (parallel log lanes inside the tube), with a label 'partitioned by hash(video_id)'. A stream of clean events with video letters is shown entering — A,A,A,B,C,A,D,B,A,B,A,A,D,C,B,A — and an arrow shows that ALL events for the same video letter are routed to the SAME partition (every A on lane 1, every B on lane 2, etc.), because the partition key is the video id. MIDDLE: a fleet of Counter consumer boxes, one consumer reading each partition; the consumer for the A-partition is shown holding an in-memory tally 'A: +7' accumulated over a short window. A caption explains BATCHING: instead of writing once per event, each consumer keeps a running per-video count in memory for a flush window (say a few seconds or N events), then writes the SUM once. RIGHT: the Count DB cylinder, receiving a small number of batched writes of the form 'UPDATE counts SET views = views + 7 WHERE video = A' — one write per video per window instead of one per event. A red 'before' bubble shows 1,000,000 events → 1,000,000 writes (melts); a green 'after' bubble shows 1,000,000 events → a few thousand batched writes (survives). Two highlighted reasons why the partition key MUST be video_id: (1) CORRECTNESS — all events for a video land on one consumer, so that consumer alone owns the count and can batch it with no cross-machine coordination or lock; (2) NO CONTENTION — different videos are counted on different machines in parallel, so there is no single hot row. A warning note in red: if you partitioned by user_id or randomly, one video's events would scatter across every consumer, and they'd all fight over the same row again — re-creating the Section 1 problem. Takeaway: partition by video so each video has one owner, then batch increments so a million events become a handful of writes." width="1180">

**Batching.** Instead of one write per event, the counter keeps a **running per-video tally in memory** for a short flush window (a few seconds, or N events), then writes the *sum* once: `UPDATE counts SET views = views + 7 WHERE video = 'A'`. A million events collapse into a few thousand batched writes. This is the move that finally makes the database write rate sane — and it's safe *only because* we decided up front that the count may lag a few seconds (the eventual-consistency gift from the brief). The database does `count += n` instead of `count += 1` a million times.

**Partitioning — and the key choice is *what* to partition by.** A Kafka topic is split into partitions, and the producer chooses each event's partition by a **key**. We key by <span style="color:#ffff99"><strong>video id</strong></span>: `partition = hash(video_id) % num_partitions`. The consequence is the entire trick: **all events for one video land on the same partition, which is read by exactly one consumer.** So that one consumer is the *sole owner* of video A's count. It can keep A's tally in memory and flush it with **no cross-machine coordination and no shared lock** — because nobody else is counting A.

Why does the key *have* to be `video_id`? Walk the alternative. If you partitioned by `user_id`, or randomly (round-robin), then video A's events would <span style="color:#ff8a8a"><strong>scatter across every consumer</strong></span>. Now ten machines each hold a partial count for A and must reconcile into one row — and they'd fight over that row exactly like Section 1. **Partitioning by the thing you're counting is what gives each counter a private, contention-free slice.** Different videos are counted on different machines, fully in parallel; the same video is always counted in one place.

> **Memory hook:** *two moves turn the firehose into a trickle of writes. Batch: keep a per-video tally in memory, flush the sum (`+= n`, not `+= 1` a million times) — safe because the count may lag. Partition by video_id: all of one video's events land on one consumer, so it solely owns that count and batches with no lock or coordination. Partition by user or randomly and you scatter a video across machines and re-create the hot row.*

---

## Section 6 — Parallelism: Consumer Groups, and the Hot-Partition Problem

**Question: we said "one consumer owns a video." But there are billions of videos and one machine can't hold them all. So does one consumer get *all* the videos? How does the work actually spread across machines — and what happens when one video is so viral it overwhelms its single owner?**

This is where Kafka's parallelism model does the work, so let's answer the four questions the design raises head-on.

<img src="../assets/youtube-views-counter/partition-parallelism.svg" alt="How counting parallelizes across machines via Kafka consumer groups, and the hot-partition failure. TOP, the consumer-group mechanism: the second Kafka topic is drawn with 8 partitions (8 log lanes labelled P0 through P7). A 'Counter consumer group' of 4 machines (boxes C1,C2,C3,C4) sits below; arrows assign partitions to consumers so each machine owns a disjoint subset — C1 owns P0,P1; C2 owns P2,P3; C3 owns P4,P5; C4 owns P6,P7. A caption answers 'does one consumer get all videos?': NO — the group splits the partitions, so each machine owns a slice of the videos (the videos whose hash lands on its partitions) and counts them independently, in parallel, with no coordination between machines. A note: parallelism is capped by the number of partitions — to count faster, add partitions and consumers (up to one consumer per partition); add a 5th consumer to an 8-partition topic and it takes over some partitions automatically (rebalance). BOTTOM, THE HOT PARTITION (outlined in red): one video goes viral — a premiere with tens of millions of concurrent viewers. All its events hash to ONE partition (P3) owned by ONE consumer (C2), which is now drowning while C1, C3, C4 sit nearly idle — the skew, not the average, is the problem. Three mitigations are shown: (1) SPLIT THE KEY / sharded counter — append a small random suffix to the hot video's key so it spreads across K partitions ('A#0','A#1',…'A#K'), count each shard independently, and SUM the K shard-counts at read time (trades a tiny read-side add for write-side parallelism); (2) LOCAL PRE-AGGREGATION — the overwhelmed consumer already batches in memory, so even a hot partition emits only one batched write per window, absorbing most of the spike; (3) MORE PARTITIONS up front so the hot key at least has company. Takeaway: consumer groups give horizontal, coordination-free parallelism bounded by partition count; a viral video creates a hot partition, cured by sharding the hot key and summing on read." width="1180">

**Does one consumer get all the videos? No — consumer groups split them.** A counter is a <span style="color:#93c5fd"><strong>consumer group</strong></span> of many machines. Kafka divides the topic's partitions across the machines in the group so each owns a **disjoint subset** of partitions — and therefore a disjoint subset of videos. Eight partitions across four machines = two partitions each. Each machine counts its slice **independently and in parallel**, and because the slices are disjoint *by video*, there is **zero coordination between machines**. That's the answer to "why are we counting on so many machines?" — horizontal scale with no shared state.

**What's the parallelism, and how do we partition?** Parallelism is **capped by the partition count**: at most one consumer per partition does useful work. Want to count faster? Add partitions and add consumers (up to one-per-partition). Add a fifth machine to an eight-partition topic and Kafka **rebalances** — it reassigns some partitions to the newcomer automatically. So you provision partitions generously up front (you can't easily shrink them) and scale the consumer fleet within that ceiling.

**And the failure mode — the hot partition.** Here's the crack. We partition by `video_id`, so when one video goes viral — a premiere with tens of millions of concurrent watchers — *all* its events hash to <span style="color:#ff8a8a"><strong>one partition</strong></span>, owned by <span style="color:#ff8a8a"><strong>one consumer</strong></span>, which now drowns while its siblings idle. The skew, not the average, kills you — the same hot-partition villain from the [S3 post](20-high-throughput-system-s3.md). Three cures, in increasing order of effort:

- <span style="color:#8aff8a"><strong>Local pre-aggregation already helps.</strong></span> The consumer *batches* (Section 5), so even a hot partition emits only **one batched write per window**. Batching absorbs most of a spike before it reaches the database — the hot partition stresses one *consumer's* CPU, not the Count DB.
- <span style="color:#ffff99"><strong>Split the hot key — a sharded counter.</strong></span> For a genuinely viral video, append a small random suffix so its events spread across K partitions: `A#0, A#1, …, A#K`. Count each shard on its own machine, in parallel, then **sum the K shard-counts at read time**. You trade a tiny read-side addition for write-side parallelism — the [classic sharded-counter trick](20-high-throughput-system-s3.md).
- <span style="color:#93c5fd"><strong>More partitions up front</strong></span> so even hot keys have neighbors and the blast radius of any one is smaller.

> **Memory hook:** *a consumer group splits a topic's partitions across machines, so each machine owns a disjoint slice of videos and counts in parallel with no coordination — parallelism capped by partition count, scaled by adding consumers (Kafka rebalances). The villain is the hot partition: a viral video hashes all its events onto one consumer. Cure it with batching (one write/window), sharded keys (A#0…A#K, sum on read), and generous partition counts.*

---

## Section 7 — Correctness Under Replay: Offsets, At-Least-Once, and the Read Path

**Question: Kafka is at-least-once — a consumer can crash after writing to the database but before recording its progress, and replay the same events. If our whole job is *counting*, a double-count is a wrong number. How do we keep the count right across crashes, and how do reads get served?**

We've built the write path; now we have to make it *correct*, because counting is exactly the workload where the usual "at-least-once is fine" hand-wave bites. A consumer's progress is its <span style="color:#ffff99"><strong>offset</strong></span> — "I've processed partition P up to position N." If it writes `+= 7` to the database and crashes *before committing offset N*, it restarts at N−7 and adds those seven **again**. The count drifts up.

Two ways to defend the number:

- <span style="color:#ffff99"><strong>Atomic count-and-offset.</strong></span> Store the per-video count **and** the last-processed offset in the *same* store, updated in one transaction. On restart the consumer reads the committed offset and resumes exactly where the count already reflects — replays become no-ops because the offset already moved past them. This is the clean, exactly-once-*effective* path.
- <span style="color:#ffff99"><strong>Idempotent batches.</strong></span> Tag each batch with its `(partition, offset-range)` and make the write idempotent — applying the same range twice has no effect. Same outcome, different bookkeeping.

The eventual-consistency gift from the brief is what makes all of this tolerable: between a crash and recovery the public count may be briefly stale or replay a window, and **that's acceptable** — it self-heals on the next flush. We were never promising exact-to-the-millisecond; we were promising *eventually correct*.

And the **read path** — the part a billion viewers actually hit — is the easy half, precisely because we did all this work on the write side. A video page asks "how many views?" and that's a single point read of one pre-aggregated row, served from a <span style="color:#8aff8a"><strong>cache</strong></span> in front of the Count DB (the number changes slowly and staleness is fine, so it caches beautifully). For a sharded hot key, the read sums its K shard-rows — a tiny fan-in. Reads are cheap because **every hard thing happened upstream**: the firehose was absorbed, filtered, fanned out, partitioned, and batched long before anyone asked for the number.

> **Memory hook:** *Kafka is at-least-once, so a crash mid-batch can replay and double-count. Defend the number by storing count and offset together in one transaction (replays become no-ops) or by idempotent (partition, offset-range) batches. Eventual consistency makes brief staleness fine. The read path is trivial — a cached point read of the pre-aggregated row (sum the shards for a hot key) — because all the hard work already happened on the write side.*

---

## Section 8 — The Whole Pipeline on One Map

**Question: let's assemble the entire machine and trace a single watch from a player's heartbeat to the number under the video — and see why every layer had to exist.**

<img src="../assets/youtube-views-counter/architecture.svg" alt="The complete YouTube view-counter pipeline on one map, left to right. FAR LEFT: viewers (stick figures) watching, each sending a 'view event' heartbeat every few seconds (pink write path) to a horizontally-scaled WATCHTIME API fleet (stacked boxes). The API is a thin PRODUCER: it appends the event and returns instantly, never touching a database on the request path. STEP 1, INGEST: the API appends to KAFKA #1 (a horizontal tube, blue), the raw firehose, partitioned by video_id; a label notes 'Kafka not RabbitMQ — log-structured, ~1M+/sec throughput'. STEP 2, FILTER: a RULE ENGINE consumer-group (stacked boxes) reads Kafka #1 and consults a RULES DB cylinder (yellow) to drop own-views, sub-threshold watches, replays, and bots — keeping only real views. STEP 3, FAN OUT: the rule engine publishes the clean stream ONCE onto KAFKA #2 (a second horizontal tube, blue) labelled 'filtered-views — single source of clean truth'. From Kafka #2, multiple independent consumer groups fan out: (a) the VIEWS COUNTER group (stacked boxes, the main path) — partitioned by video_id, each consumer batches a per-video tally in memory and flushes 'views += n' as a few batched writes into the COUNT DB cylinder (yellow), storing count and offset together for replay-safety; (b) a MONETIZATION group consuming the same clean events for payouts; (c) a RECOMMENDATIONS group. READ PATH (green): a viewer's video page asks 'how many views?' and is served a cached point read of the pre-aggregated row from a CACHE in front of the Count DB (summing shard-rows for a viral video). Callouts mark the two anti-patterns avoided: a red 'X' from the API directly to the Count DB labelled 'never write a raw view to the DB' and a red 'X' showing three separate rule engines collapsed into one. A legend maps colors: pink = view/write path, blue = Kafka/async transport and consumers, yellow = durable storage and rules, green = read/serve path, red = the failure we avoid. The whole story: absorb the firehose in a log, filter once, fan out the clean stream, then batch-and-count in a video-partitioned fleet, so the database only ever sees a trickle of pre-aggregated writes." width="1280">

Trace one watch end to end:

1. <span style="color:#ff8bd2"><strong>Heartbeat.</strong></span> A player emits a view event every few seconds to the stateless <span style="color:#ff8bd2"><strong>Watchtime API</strong></span>, which appends it to <span style="color:#93c5fd"><strong>Kafka #1</strong></span> and returns instantly. The database is nowhere near the request path. *(Section 1–2: never write a raw view to the DB; absorb the firehose in a log — Kafka, for throughput.)*
2. <span style="color:#93c5fd"><strong>Filter.</strong></span> The <span style="color:#ffff99"><strong>rule engine</strong></span> consumes Kafka #1, consults the <span style="color:#ffff99"><strong>Rules DB</strong></span>, and drops every event that isn't a real view. *(Section 3: filtering is its own stage.)*
3. <span style="color:#93c5fd"><strong>Fan out.</strong></span> It republishes the clean stream **once** onto <span style="color:#93c5fd"><strong>Kafka #2</strong></span>, the single source of filtered truth, which <span style="color:#8aff8a"><strong>views</strong></span>, <span style="color:#93c5fd"><strong>monetization</strong></span>, and recommendations each consume independently. *(Section 4: filter once, fan out many.)*
4. <span style="color:#ffff99"><strong>Count.</strong></span> The views-counter group, partitioned by `video_id`, keeps a per-video tally in memory and flushes `views += n` as a trickle of batched, replay-safe writes into the <span style="color:#ffff99"><strong>Count DB</strong></span>. *(Sections 5–7: batch, partition by video, store count+offset together.)*
5. <span style="color:#8aff8a"><strong>Read.</strong></span> A viewer's page reads the pre-aggregated number from a <span style="color:#8aff8a"><strong>cache</strong></span> in front of the Count DB — cheap, because all the hard work happened upstream.

Every layer earns its place against a specific failure: the log exists because the database melts on raw writes; the rule engine exists because half the events aren't views; the *second* log exists because filtering is too expensive to repeat and too important to fork; partitioning-by-video exists so each counter owns a contention-free slice; batching exists so a billion increments become a handful of writes. **The number under the video is one integer. Everything above is what it costs to keep that integer honest at a billion events an hour.**

> **Memory hook:** *viewer → Watchtime API (thin producer) → Kafka #1 (absorb firehose) → rule engine + Rules DB (drop non-views) → Kafka #2 (clean stream, fan out) → views counter (partition by video, batch `+= n`, replay-safe) → Count DB → cache → reader. Each layer answers one failure; the database only ever sees a trickle of pre-aggregated writes.*

---

## Sources

- [Kafka vs RabbitMQ — AWS](https://aws.amazon.com/compare/the-difference-between-rabbitmq-and-kafka/)
- [Kafka vs RabbitMQ: Key Differences & When to Use Each — DataCamp](https://www.datacamp.com/blog/kafka-vs-rabbitmq)
- [When to use RabbitMQ or Apache Kafka — CloudAMQP](https://www.cloudamqp.com/blog/when-to-use-rabbitmq-or-apache-kafka.html)
- [RabbitMQ vs Kafka: Use Cases, Performance & Architecture — Upsolver](https://www.upsolver.com/blog/kafka-versus-rabbitmq-architecture-performance-use-case)
