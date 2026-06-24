# Designing a Multi-Tiered Datastore: Hot, Warm, Cold, and the Data Engineering Behind It

This post builds a **multi-tiered datastore** from first principles: a system that keeps your *fresh, hot* data in a fast transactional database and continuously ages your *old, cold* data down into cheaper and cheaper storage — without ever losing the ability to read it. Starting from a single MySQL behind an Orders service, it grows each component only when a named bottleneck appears: read replicas for read load, sharding (and why we *reject* it here), then the real fix — moving data across **hot → warm → cold** tiers by age. Along the way we build the entire **data-engineering pipeline** that does the moving: dumpers, staging storage, loaders, ETL/ELT, the HDFS ecosystem, and the query engines (Hive, Spark, Flink, Presto/Trino, Athena, Airbyte, Airflow) that let you query data sitting cheaply in S3.

**Question: you run Amazon's Orders service. Every order ever placed lives in one MySQL table. For the first few months it's fast; then it isn't — reads and writes pile up, indexes thrash, and adding replicas only buys you a few weeks. Sharding is on the table but it's a knife you don't want to hold. What is actually wrong, and what is the smallest change that fixes it permanently?** The honest answer is not "scale the database harder." It's that **most of that table is old** — an order from three years ago is read by nobody except a compliance auditor once a decade, yet it sits in the same expensive, low-latency, strongly-consistent store as the order you placed thirty seconds ago. The fix is to stop treating all data as equally valuable and build a system that **moves data to storage that matches how it's actually used.** By the end you'll have hand-built the tiered architecture that sits underneath Amazon, Uber, and Netflix order/event history — and the data-engineering machinery that every data and ML engineer is expected to design.

This post is the capstone of a small arc. We built [ETL and Change Data Capture](16-storage-engine-etl-cdc.md) and learned how to keep a second database in sync with a first; we built a [Bitcask-style key-value engine](19-storage-engine-fast-kv-db.md) and learned why high-write systems go log-structured; we built [S3](20-high-throughput-system-s3.md) and learned how infinitely cheap, durable storage actually works. This post is where all three come home: tiering is **ETL with a purpose** (move data by age), the cold tier **is S3**, and the whole thing exists to keep the [transactional database](03-database-airline-checkin-transactions-indexes-locks.md) small and fast.

> **Memory hook:** *a multi-tiered datastore matches storage cost to data temperature — hot data lives in an expensive fast transactional DB, and as it ages and cools it's moved to cheaper, slower, read-only tiers (warm distributed DB → cold object storage), keeping the hot store small enough to stay fast forever.*

---

## The brief: one table, growing without bound

**Question: before optimizing anything — what does the system actually look like on day one, and why is it *correct* before it's *slow*?**

You're running the <span style="color:#ff8bd2"><strong>Orders service</strong></span> for Amazon. Every order placed on the entire site flows through it. A <span style="color:#8aff8a"><strong>customer</strong></span> calls `place_order` and `order_details`; downstream services lean on it too — <span style="color:#93c5fd"><strong>Payments</strong></span> asks for payment status, <span style="color:#93c5fd"><strong>Logistics</strong></span> reports delivery status, <span style="color:#93c5fd"><strong>Customer Support</strong></span> pulls up an order to help a caller. Underneath it all is a single <span style="color:#ffff99"><strong>transactional database</strong></span> — a relational store like MySQL — holding one big `orders` table.

<img src="../assets/multi-tiered-db/intro-orders.svg" alt="The starting Orders system. Center: an Orders Service box (pink) backed by a single Transactional DB (MySQL) cylinder on the right. On the left a Customer stick figure calls place_order and order_details. Connected around the Orders Service: Customer Support (top), Payments (asking payment status, bottom-left), Logistics (reporting delivery status, bottom-right). Caption at top: Transactional System — Orders — all orders that happen on Amazon go here; the task is to scale this solution. Below: a note that everything works fine for the first few months, and then database performance degrades because of a large number of writes and reads. Two scaling responses are sketched: 'Scale up' shows a small DB cylinder growing into a bigger one (vertical scaling); 'Scale reads' shows a Master cylinder syncing to two Replica cylinders (read replicas)." width="1000">

This is correct from the first minute. A relational database gives you exactly what an orders system needs: <span style="color:#ffff99"><strong>ACID transactions</strong></span> (charge the card *and* decrement inventory *and* create the order, all-or-nothing), <span style="color:#8aff8a"><strong>strong consistency</strong></span> (the moment an order is written, every service reads the same truth), and <span style="color:#8aff8a"><strong>low-latency point reads and writes</strong></span> (fetch *this one* order by id). There's nothing wrong with the design. There's only something wrong with what happens to it at scale.

Because **everything works fine for the first few months, and then it doesn't.** Database performance degrades under a large number of writes and reads. The table that was thousands of rows is now billions. Every query is slower than it was last quarter, and the slope is getting worse. The rest of this post is the staircase you climb in response — and the trap of climbing it the obvious way.

> **Memory hook:** *the Orders service on a single transactional DB is correct on day one — ACID, strong consistency, fast point reads. It only breaks because the table grows without bound and reads/writes pile up.*

---

## Section 1 — The first two rungs: scale up, then scale reads

**Question: the database is slow. What are the two things every engineer reaches for first, and how far do they actually get you?**

The first instinct is <span style="color:#8aff8a"><strong>scale up</strong></span> — vertical scaling. Buy a bigger box: more RAM so more of the working set fits in the buffer pool, faster NVMe so disk-bound lookups hurt less, more cores. This is the cheapest *engineering* move (you change nothing about your code) and it genuinely helps. But it has a ceiling — there is a biggest instance money can buy — and the cost curve goes vertical near the top. Vertical scaling buys you time, not a solution.

The second instinct, once writes and reads are both heavy, is to split them. Most order traffic is **reads** — every customer refresh, every support lookup, every Payments and Logistics poll hits the database, while actual order *placement* is comparatively rare. So <span style="color:#8aff8a"><strong>scale reads</strong></span> with <span style="color:#93c5fd"><strong>read replicas</strong></span>: keep one <span style="color:#ff8bd2"><strong>master</strong></span> that takes all writes, and **SYNC** its changes out to several read-only <span style="color:#93c5fd"><strong>replicas</strong></span> that serve reads. The application sends writes to the master and fans reads across the replicas. (This is the [primary/replica pattern](04-database-distributed-kv-store-on-relational-database.md) from the distributed-KV post, and it brings the usual tradeoff: replication is asynchronous, so a replica can be a few milliseconds *stale* — fine for "show me my orders," not fine for "did my payment go through," which must read the master.)

Replicas are a real win for read-heavy workloads, and Orders is read-heavy. But notice what they *don't* fix:

- **Writes still all land on one master.** Replicas multiply read capacity; they do nothing for write throughput. Every replica must *also* apply every write to stay in sync — so the write ceiling is unchanged.
- **The table is just as big on every replica.** A replica is a *full copy*. If the master's `orders` table is 4 TB and its indexes no longer fit in RAM, every replica has the same 4 TB table with the same cold indexes. You've copied the problem, not solved it.

So the first two rungs — bigger box, more replicas — push the wall back but don't move it. The wall is still there. **What comes after replicas?**

> **Memory hook:** *scale up (vertical) buys time but hits a ceiling; read replicas (master + SYNC'd replicas) multiply read capacity but not write capacity, and every replica carries the same oversized table. Neither shrinks the problem.*

---

## Section 2 — Sharding, and why we *don't* reach for it here

**Question: the textbook next step after replicas is sharding — split the table across many databases. Why is that the wrong tool for *this* problem, even though it's the right tool for others?**

<span style="color:#8aff8a"><strong>Sharding</strong></span> takes the one database and splits its rows across many independent databases — <span style="color:#93c5fd"><strong>shard 1, shard 2, shard 3</strong></span> — each holding a disjoint slice of the data (say, orders partitioned by `customer_id`). Now writes *and* reads spread across shards, and each shard's table is a fraction of the whole. This genuinely solves the write-ceiling problem, and for many systems it's the correct answer. We use it elsewhere in this handbook — it's exactly how [S3 range-partitions its keyspace](20-high-throughput-system-s3.md) and how [the distributed KV store](04-database-distributed-kv-store-on-relational-database.md) scales.

<img src="../assets/multi-tiered-db/scaling-sharding.svg" alt="Two ideas. Top: 'The next phase of DB scaling seems: Sharding' — a single DB cylinder splitting into Shard 1, Shard 2, Shard 3. To the right, a caution: 'Sharding is not always a great choice because' two downsides listed — multi-tenant isolation, and operational overhead. Bottom: 'What next after replicas?' The root cause of DB degradation, with an arrow, is 'Table is too large' — computation takes time, and index lookups are disk-bound. A highlighted box asks: 'Can we reduce the table size?'" width="1000">

But sharding is <span style="color:#ff8a8a"><strong>not always a great choice</strong></span>, and for a multi-tenant order system it carries real costs:

- **Multi-tenant isolation.** Once you shard by customer, a single huge customer (a marketplace seller doing millions of orders a day) can land mostly on one shard and turn it into a hotspot, while small customers' shards idle. Rebalancing across shards is painful, and a noisy tenant's blast radius is a whole shard.
- **Operational overhead.** Sharding is a permanent tax on every part of your system. Cross-shard queries need scatter-gather. Transactions that span shards need distributed-transaction machinery (two-phase commit) or have to be redesigned away. Schema migrations run N times. Backups, failover, and capacity planning all multiply by the shard count. You've turned one database into a fleet you have to operate forever.

And here's the deeper objection — **sharding doesn't address the actual root cause.** Step back and ask *why* the database degraded in the first place. The <span style="color:#ffff99"><strong>root cause of DB degradation is that the table is too large.</strong></span> When a table is enormous:

- **Computation takes time.** Aggregations, sorts, and scans are proportional to row count. More rows, more work, every query.
- **Index lookups are disk-bound.** A B-tree index speeds lookups *only while it fits in RAM* (the buffer pool). As the table grows, the index grows — more levels, more pages — until it no longer fits in memory. Now every lookup that misses the cache is a **disk seek**, and you've fallen off a performance cliff. The index was supposed to save you from disk; once it spills to disk, it can't.

Sharding makes each shard's table smaller, yes — but it does so by *adding a fleet of databases and all their operational weight*. The blunt question that reframes everything: **can we reduce the table size *without* sharding?** Can we just make the hot table small again?

> **Memory hook:** *sharding solves the write ceiling but adds multi-tenant hotspots and permanent operational overhead — and it sidesteps the real root cause: the table is too large, so indexes spill out of RAM and lookups become disk-bound. The better question is "can we shrink the hot table?"*

---

## Section 3 — Reading the access pattern: the lifecycle of an order

**Question: to shrink the table intelligently we need to know which rows actually matter. So look at the data's life — how is a single order accessed from the moment it's created to years later?**

This is the most important observation in the whole design, and it costs nothing to make: **not all data is accessed equally, and access falls off sharply with age.** Plot how often a single order is touched against its age and you get a steep <span style="color:#ffff99"><strong>time-decay curve</strong></span> — heavy access right after creation, collapsing toward zero as the order ages.

<img src="../assets/multi-tiered-db/access-pattern.svg" alt="Left: an 'Access Pattern' graph — access frequency (y-axis) versus age of an order (x-axis), titled 'Life cycle of an order'. The curve starts very high and decays sharply toward zero, divided by two vertical lines into three regions. Region 1 (newest): 'Reads / Writes' — the order is actively being created and updated. Region 2 (middle): 'Frequent Reads' — the order is read often but no longer written. Region 3 (oldest, long tail): 'Infrequent Reads [Compliance]' — the order is almost never read, only for compliance/audit. Right: 'The idea: Move orders from one DB to another depending on its age to reduce the load on the transactional database.'" width="1000">

The lifecycle splits cleanly into three phases:

1. <span style="color:#ff8bd2"><strong>Reads and writes (newest).</strong></span> The order is live: being placed, paid, picked, packed, shipped. Its row is updated many times and read constantly. It *needs* the transactional database — ACID, strong consistency, low latency. This phase lasts days to a few weeks.
2. <span style="color:#8aff8a"><strong>Frequent reads (recent).</strong></span> The order is delivered and final. Nobody writes to it anymore, but it's still read often — the customer checks "my orders," returns and refunds reference it, support looks it up. It needs to be *readable and reasonably fast*, but it does **not** need transactions or write capacity. This phase lasts months.
3. <span style="color:#ff8a8a"><strong>Infrequent reads — compliance (oldest).</strong></span> The order is years old. Almost nobody reads it. It exists for <span style="color:#ff8a8a"><strong>compliance</strong></span>, accounting, tax, legal discovery, and offline analytics. A read here can be *slow* — minutes, even — because it happens once in a blue moon. But you may be legally required to keep it for 7–10 years. This phase lasts effectively forever.

Stare at the three phases and the design writes itself. Phase 1 is a tiny sliver of all orders (only the last few weeks) but it consumes nearly all the *write* load and demands the most expensive guarantees. Phase 3 is the vast majority of rows but consumes almost no load. **Yet today they all live in the same expensive database.** That's the waste.

So <span style="color:#ffff99"><strong>the idea</strong></span>: **move orders from one database to another depending on their age, to reduce the load on the transactional database.** Keep only phase-1 (and maybe phase-2) data in the hot transactional store; push older data down to cheaper stores that match how it's actually used. The hot table shrinks back to "just the last few weeks," its indexes fit in RAM again, and it's fast forever — no sharding required.

How do you *decide* the boundaries between phases — by raw age, or by measured access? That's a real question, and Section 6 answers it. First, let's name the tiers.

> **Memory hook:** *an order's access decays sharply with age — hot reads+writes for weeks, frequent reads for months, then rare compliance reads forever. Most rows are old and barely touched, so moving old data to cheaper stores shrinks the hot table back to fast.*

---

## Section 4 — The three tiers: hot, warm, cold

**Question: if we're going to move data by temperature, what exactly are the tiers — and what makes each one the right home for its phase of the lifecycle?**

A <span style="color:#ffff99"><strong>tiered datastore</strong></span> is a sequence of stores, each cheaper and slower than the last, each matched to one phase of the access curve. The governing insight: **by moving data from one tier to the next, we reduce the time-to-computation on the hot store** — because we keep it small. Here are the three tiers, top to bottom.

<img src="../assets/multi-tiered-db/three-tiers.svg" alt="Three stacked tiers of a tiered datastore. Header: 'By moving data from one tier to another we are reducing the time to computation.' Tier 1 — HOT STORE: 'Transactional Store (Read/Write)' — transactional, low latency, strong consistency, read/writes, expensive. Tier 2 — WARM STORE: 'Read-only workloads' — read-only, non-transactional, frequent reads, could be a little slower, horizontally scalable, less expensive. Tier 3 — COLD STORE: 'Infrequent reads, cheap, very slow' — read-only, very infrequent reads, compliance and accounting, offline analytics." width="1000">

### Hot store — the transactional database

The <span style="color:#ff8a8a"><strong>hot store</strong></span> is the original transactional database (MySQL, PostgreSQL). It serves <span style="color:#ff8bd2"><strong>phase 1</strong></span>: live orders being read *and written*.

- **Transactional** (ACID) and **strongly consistent** — the moment a write commits, everyone sees it.
- **Low latency** — single-digit-millisecond point reads and writes.
- **Expensive** — strong guarantees on fast hardware cost the most per byte, which is exactly why we don't want years of dead data living here.

### Warm store — the read-only distributed database

The <span style="color:#ffd27f"><strong>warm store</strong></span> serves <span style="color:#8aff8a"><strong>phase 2</strong></span>: delivered orders that are still read often but never written.

- **Read-only** and **non-transactional** — no ACID, because nothing mutates these rows anymore.
- **Frequent reads**, and it's allowed to be **a little slower** than hot (tens of milliseconds is fine).
- **Horizontally scalable** and **less expensive** — because we dropped the transactional and strong-consistency requirements, we can use a distributed database built for cheap horizontal read scaling instead of an expensive single-master relational box.

Dropping ACID is what unlocks the cost savings. You can't drop it for live orders; you absolutely can for finished ones.

### Cold store — cheap object storage

The <span style="color:#93c5fd"><strong>cold store</strong></span> serves <span style="color:#ff8a8a"><strong>phase 3</strong></span>: ancient orders kept for compliance and analytics.

- **Read-only**, **very infrequent reads**.
- **Cheap** — the cheapest storage that exists, because this is where most of the bytes end up.
- **Very slow** is acceptable — a compliance query that takes minutes is fine when it runs twice a year.
- Used for **compliance, accounting, and offline analytics**.

This is object storage — [S3](20-high-throughput-system-s3.md) — possibly in its archival classes. The whole point of the cold tier is that storing a petabyte there costs a tiny fraction of storing it in a transactional database, and you almost never pay the slow-read penalty because you almost never read it.

> **Memory hook:** *three tiers, matched to the access curve. Hot = transactional, low-latency, strongly consistent, expensive (live data). Warm = read-only, non-transactional, horizontally scalable, cheaper (finished-but-popular data). Cold = read-only, very slow, dirt cheap (compliance/analytics archive). Moving data down keeps the hot store small and fast.*

---

## Section 5 — Deciding *when* to move: age, access, and cost

**Question: the tiers are clear, but what actually triggers a move? Do you move an order to warm because it's 30 days old, or because nobody's read it in 30 days? How do you read the access pattern in practice?**

There are three families of policy, in increasing order of sophistication. Most real systems use the first because it's simple and good enough; the others are refinements.

**1. Age-based (time-to-live) policy.** The simplest and by far the most common: move data when it crosses an **age threshold**, derived from the access curve. "Orders older than 90 days move to warm; orders older than 2 years move to cold." You pick the thresholds by looking at where the access curve bends — the "knees" between phases. This is what the diagrams call a <span style="color:#ffff99"><strong>Data Delete Policy</strong></span> / data-retention policy, and it's attractive because it's *predictable and stateless*: you don't have to track anything per-row, you just run a query for `created_at < now() - 90d`. The downside is it's a blunt instrument — a 91-day-old order that's being actively returned gets moved even though it's still hot.

**2. Access-frequency-based policy.** More precise: track *actual* access and move data that's gone quiet, regardless of age. This requires **measuring the access pattern**, and that's a real engineering task:

- **Last-accessed timestamps.** Stamp each row (or each object) with the time of its last read. Move anything not touched in N days. Accurate, but writing a timestamp on every read is itself load — you usually sample or batch it.
- **Access-frequency histograms.** Periodically aggregate your **query logs** (the database's slow-query log, or an access log in front of the service) into a histogram of "how many times was each key read in the last window." The long, flat tail of that histogram is your cold data. This is how you'd discover, empirically, where the curve bends — rather than guessing.
- **Heuristics combining both.** "Move to cold if older than 1 year AND fewer than 3 reads in the last 6 months."

**3. Cost-based / automatic tiering.** Let the storage layer watch access and move data for you. This is exactly what **S3 Intelligent-Tiering** does (Section 9): it monitors per-object access and automatically shuffles objects between frequent- and infrequent-access tiers, so you don't write the policy at all — you pay a small monitoring fee and the system optimizes cost. Great when access patterns are unpredictable.

**The practical answer:** start with **age-based** thresholds read off the access curve, because orders have a naturally predictable lifecycle (almost no order is updated after ~30 days). Layer in **access-frequency** signals only if you find age alone is moving still-hot data. And remember the policy cuts both ways — moving data *out* of a tier is also a **deletion** from that tier, governed by the same retention rules. We'll see in the architecture that each tier has its own data-delete policy: the hot store deletes what it's already copied to warm, warm deletes what it's copied to cold.

One caution that bites everyone: **compliance and legal hold override your tiering policy.** GDPR's "right to be forgotten" means a user can demand deletion even from the cold archive — so your cold store must support targeted deletes, not just append. Conversely, a *legal hold* can freeze data you'd otherwise be required to delete. The retention engine has to honor both.

### What a data-delete policy actually does

Moving data to the next tier means *deleting it from this one* — and that delete is where pipelines lose data if done carelessly. Three rules govern it.

<img src="../assets/multi-tiered-db/data-deletion.svg" alt="The data-delete policy in three parts. Part 1, the safe order: COPY (load rows into next tier) → VERIFY (row counts / checksums match) → DELETE (remove from this tier); doing delete-before-verify means permanent data loss. Part 2, lazy/soft delete (the default) vs hard delete: soft delete marks a tombstone (set deleted=true or a TTL/expiry) — instant, cheap, reversible until a background compaction/GC physically purges it; hard delete physically removes the bytes now — expensive (rewrites pages/files), irreversible, required for GDPR erase, and on an S3 lakehouse needs a delete-file plus rewrite via Iceberg/Delta to truly purge. Part 3, the cache: a deleted row can still live in a cache, so you must evict it via write-through invalidation (delete drops the cache key), TTL expiry (entry ages out, with a stale window), or tombstone read (a soft-deleted row stays cacheable as 'deleted'); moving a tier is itself a cache event that should invalidate the hot-tier entry." width="1120">

- <span style="color:#8aff8a"><strong>Order: copy → verify → delete.</strong></span> Load into the next tier, confirm it landed (row counts or checksums), and *only then* delete here. Delete-before-verify is how you lose data forever.
- <span style="color:#ffd27f"><strong>Lazy (soft) delete is the default.</strong></span> Don't rewrite gigabytes to drop a row — just write a **tombstone** (`deleted=true`, or a TTL/expiry), and let a background **compaction/GC** reclaim the space later (the same [log-structured](19-storage-engine-fast-kv-db.md) trick: deletes are appends too). It's instant, cheap, and reversible until collected. A <span style="color:#ff8a8a"><strong>hard delete</strong></span> — physically removing bytes now — is expensive and irreversible, and you reach for it only when compliance (GDPR erase) demands it; on an S3 lakehouse that means a delete-file plus a rewrite through Iceberg/Delta.
- <span style="color:#93c5fd"><strong>Don't forget the cache.</strong></span> A deleted (or migrated) row can still sit in a cache and resurface as a ghost. Evict it: **write-through invalidation** (the delete also drops the key), **TTL expiry** (simplest, with a stale window), or a cacheable **tombstone**. Migrating an order to warm is itself a cache event — invalidate the hot entry.

> **Memory hook:** *three move-policies — age-based (move at N days; simple, stateless, most common), access-frequency-based (track last-accessed / mine query logs; move what's gone quiet), and cost-based (let S3 Intelligent-Tiering auto-move). Start with age thresholds read off the access curve. Compliance (GDPR erase, legal hold) overrides all of them.*

---

## Section 6 — The full architecture

**Question: let's assemble the whole machine. Trace an order from the moment a customer places it, through every tier, to the analyst who queries it years later. What are all the moving parts?**

Here is the complete multi-tiered datastore. It's worth studying the whole thing once, then we'll walk each piece in depth.

<img src="../assets/multi-tiered-db/architecture.svg" alt="The complete multi-tiered datastore pipeline. Top-left: a Customer calls place_order / get_order on the Orders service; Payments and Logistics also feed into Orders. Orders writes to the HOT store — a Transactional DB (MySQL, PostgreSQL) — which has a self-loop labelled 'Data Delete policy'. From HOT, a CRON-triggered arrow runs a 'Dumper' (drawn as stacked boxes) that writes to a 'Staging storage' cylinder. The Dumper feeds a 'Loader' (stacked boxes) labelled 'Loading & transformations', which writes into the WARM store. WARM is a box of three cylinders labelled 'Distributed Database, Horizontally Scalable: MongoDB, ElasticSearch, TiDB, DynamoDB, HDFS'; it serves 'historical orders' back up to the Orders service and has its own 'Data Delete Policy' self-loop. From WARM, a 'Dump & load' stage writes to COLD — an S3 cloud labelled 'Extremely cheap storage'. From S3, one path goes to 'PrestoDB' (labelled 'Slow response') which serves 'Internal Teams — Analytics, legal, etc'. A second path from S3 runs a 'Loader' into a 'Temp Queryable DB' cylinder, which also serves the Internal Teams." width="1180">

Read it as a river flowing downhill, hot to cold:

- A <span style="color:#8aff8a"><strong>customer</strong></span> (and Payments, Logistics) hits the <span style="color:#ff8bd2"><strong>Orders</strong></span> service, which reads and writes the <span style="color:#ff8a8a"><strong>HOT</strong></span> transactional store (MySQL/PostgreSQL). This is the live path — every `place_order` and `get_order` for a fresh order is served entirely here, fast.
- On a schedule, a <span style="color:#93c5fd"><strong>CRON</strong></span> job kicks off a <span style="color:#ffd27f"><strong>Dumper</strong></span> that extracts aged-out rows from HOT into <span style="color:#ffff99"><strong>staging storage</strong></span>, and a <span style="color:#ffd27f"><strong>Loader</strong></span> that transforms and loads them into the <span style="color:#ffd27f"><strong>WARM</strong></span> store. This is **ETL** (Section 7). The hot store then *deletes* the rows it has safely handed off — that's its **data-delete policy**.
- The <span style="color:#ffd27f"><strong>WARM</strong></span> store is a horizontally scalable distributed database (MongoDB, Elasticsearch, TiDB, DynamoDB, or HDFS). When a customer asks for an old order — <span style="color:#ffff99"><strong>historical orders</strong></span> — the Orders service reads it from warm instead of hot. Warm has its *own* data-delete policy and feeds the next stage.
- Another <span style="color:#ffd27f"><strong>dump-and-load</strong></span> stage pushes the coldest data from WARM into <span style="color:#93c5fd"><strong>COLD</strong></span> — <span style="color:#93c5fd"><strong>S3</strong></span>, extremely cheap storage.
- Finally, <span style="color:#93c5fd"><strong>internal teams</strong></span> (analytics, legal, finance) query the cold tier two ways: directly via <span style="color:#93c5fd"><strong>Presto/Trino</strong></span> (a SQL engine that reads S3 in place — slow response, but no data movement), or by running a <span style="color:#ffd27f"><strong>Loader</strong></span> that pulls a *subset* of cold data into a <span style="color:#ffff99"><strong>temp queryable DB</strong></span> they can hammer with fast queries for the duration of an investigation, then throw away.

Every arrow that crosses a tier boundary is a small ETL pipeline. The two recurring components are the **dumper** (extract) and the **loader** (transform + load), with **staging storage** between them. Let's open those up.

> **Memory hook:** *Orders → HOT (transactional) → [CRON dumper → staging → loader/ETL] → WARM (distributed, serves historical orders) → [dump & load] → COLD (S3) → queried by Presto in place or loaded into a temp queryable DB. Each tier has its own data-delete policy; every cross-tier arrow is an ETL job.*

---

## Section 7 — Hot → Warm: the dumper, staging storage, and the loader (ETL in depth)

**Question: "move aged orders from MySQL to the warm store" sounds like one step. It's actually three, and skipping the middle one is how pipelines lose data. What are the three, and why is the middle one non-negotiable?**

Moving data between stores is the bread and butter of **data engineering**, and the canonical shape is <span style="color:#ff8bd2"><strong>ETL — Extract, Transform, Load</strong></span> (introduced in the [ETL/CDC post](16-storage-engine-etl-cdc.md), now put to work). In this architecture the three letters map to three components.

### The dumper (Extract)

A <span style="color:#ffd27f"><strong>dumper</strong></span> is a job — typically a script or a Spark job — whose only responsibility is to **extract** a batch of rows from the source and write them out, untransformed, to an intermediate location. It's triggered by <span style="color:#93c5fd"><strong>CRON</strong></span> (say, nightly at 2am) because tiering is not urgent — a batch cadence is exactly right. The dumper runs a bounded query against the hot store:

```sql
SELECT * FROM orders
WHERE created_at < now() - interval '90 days'
  AND created_at >= :last_watermark   -- only rows we haven't dumped yet
ORDER BY created_at
LIMIT :batch_size;
```

Two details make this safe and repeatable:

- **It's incremental, via a watermark.** The dumper records a <span style="color:#ffff99"><strong>high-water mark</strong></span> — the `created_at` (or an `id`/`updated_at`) of the last row it successfully dumped. Next run starts from there. This turns "dump the whole table every night" (impossible at scale) into "dump only what's new since last time" (cheap and constant-time).
- **It reads in bounded batches** so it never holds a giant transaction open against the live database or blows up memory. Tiering must not degrade the very hot store it's trying to protect, so the dumper throttles itself and runs off-peak (or against a *read replica* — a perfect use for those replicas from Section 1).

### Staging storage (the landing zone)

The dumper does **not** write directly into the warm store. It writes to <span style="color:#ffff99"><strong>staging storage</strong></span> — a temporary landing area (usually cheap object storage, e.g. an S3 staging bucket, holding files like Parquet). This intermediate hop looks like overhead but it's the single most important reliability decision in the pipeline. Staging **decouples extract from load**, which buys you:

- **Idempotent retries.** If the load step crashes halfway, the staged files are still there. You re-run the load against the same files — no need to re-query the hot database, no risk of double-charging it. The extract and the load fail independently.
- **A replay/audit point.** The raw extracted data sits in staging unchanged, so if a transformation bug corrupts the warm store, you can fix the transform and reload from staging — you don't have to go back to the source.
- **Impedance matching.** Extract is bounded by the source's read throughput; load is bounded by the destination's write throughput. They're rarely equal. Staging is the buffer that lets each run at its own pace.
- **Format conversion.** Staging is where row-shaped database records get written as columnar **Parquet** files (Section 11), ready for cheap analytical reads downstream.

This is the same instinct as the [dual-write problem](16-storage-engine-etl-cdc.md): never make one step responsible for two systems atomically. Stage in between, and each side can fail and retry alone.

**Staging also absorbs the format chaos of many source databases.** Your hot data rarely lives in one system — orders in Postgres, sessions in MongoDB, events in Cassandra, third-party data behind SaaS APIs. Each has a different shape (typed rows, nested JSON documents, wide column families). Staging is where that mess gets flattened: each source's extractor lands its data **raw and as-is** (usually JSON/Avro — the "bronze" layer), and a single normalization step rewrites all of it into **one columnar format, Parquet, with a unified schema**. So the format conversion isn't one transform — it's the place where four incompatible stores become one queryable table. (You can normalize *eagerly* on land, or *lazily* on read — ELT defers it.)

<img src="../assets/multi-tiered-db/staging-unify.svg" alt="Staging storage takes many source shapes and emits one format. Left: heterogeneous sources — MySQL/Postgres (typed relational rows), MongoDB (nested JSON documents), Cassandra (wide rows / column families), and SaaS APIs/events (JSON or Avro with drifting schema). Per-source extractors/connectors pull from each. Middle: staging storage on a cheap object store (S3) does two things — step 1, land everything RAW and as-is as JSON/Avro with schema-on-read (the bronze layer, no transform yet, replay-safe, decoupling extract from load); step 2, normalize to Parquet — one columnar schema, flattening documents and wide rows into typed columns, partitioned by date, now uniformly queryable. The transform can be eager (on land) or lazy (on read), and ELT defers it. Right: one engine, one SQL — Spark or Trino read every source the same way because the shape difference is gone. Key idea: staging absorbs the format chaos so everything downstream sees one schema." width="1180">

### The loader (Transform + Load)

The <span style="color:#ffd27f"><strong>loader</strong></span> reads from staging, performs <span style="color:#8aff8a"><strong>transformations</strong></span>, and writes into the warm store. "Transform" is doing real work here:

- **Reshape for the destination.** The hot store is normalized relational rows; the warm store might be a document database. The loader denormalizes — folding `order` + `order_items` + `shipping` into a single self-contained document — so warm reads need no joins. (Warm is non-transactional and join-light by design; the loader is where the data is pre-joined.)
- **Clean and enrich.** Drop columns nobody queries cold, mask PII, standardize formats, compute derived fields, attach partition keys (like `order_date`) used for pruning later.
- **Idempotent upserts.** The loader writes with the order id as the key so that re-running it (after a crash) overwrites rather than duplicates. Idempotency is what makes the whole pipeline safe to retry.

Once the loader confirms the batch is durably in warm, the hot store's <span style="color:#ffff99"><strong>data-delete policy</strong></span> fires and removes those rows from MySQL. **This is the step that actually shrinks the hot table** — and it must come strictly after a confirmed load, never before. (Order matters: copy, verify, *then* delete. Delete-before-verify is how you lose data forever.)

### ETL vs ELT — and why the order flipped

You'll hear both **ETL** and <span style="color:#ff8bd2"><strong>ELT</strong></span>, and the difference is exactly where the **T** happens:

- **ETL (Extract, Transform, Load):** transform *before* loading. You reshape data in a dedicated processing step and write only clean, final-shape data into the destination. Classic for **data warehouses**, where storage is expensive and rigid (schema-on-write), so you pay the transform cost up front and store only what's modeled.
- **ELT (Extract, Load, Transform):** load raw data *first* (into a cheap data lake / lakehouse), then transform it *in place* using the destination's own compute (Spark, dbt, Trino). This is the modern default because **object storage is so cheap** that hoarding raw data costs almost nothing, and **compute is elastic** — so you keep an untouched raw copy (great for replay and for use cases you haven't imagined yet) and transform on demand. The medallion architecture (Section 13) is ELT: land raw "bronze," then refine into "silver" and "gold."

For hot→warm, ETL fits (you transform into a specific warm shape). For warm→cold and the broader analytics lake, ELT fits (dump raw to S3, transform later only if asked). A mature pipeline uses both.

### Continuous, not batch: Change Data Capture

The CRON dumper polls the table on a schedule — fine for nightly tiering, but it misses deletes, adds query load, and lags by a whole batch interval. When you want the warm store to track the hot store **continuously and exactly**, use <span style="color:#8aff8a"><strong>Change Data Capture (CDC)</strong></span> (the engine behind the [CDC post](16-storage-engine-etl-cdc.md)). The insight: the database *already* writes every change to a durable, ordered log for its own crash recovery — Postgres's WAL, MySQL's binlog. CDC just **reads that log** instead of polling the table.

<img src="../assets/multi-tiered-db/cdc-internals.svg" alt="How CDC works internally, five steps. 1) The HOT DB commits a write. 2) It records the change in its WAL/binlog — an append-only, ordered change log it keeps anyway for durability. 3) A CDC connector (Debezium) tails that log and tracks its offset. 4) It emits change events into a durable, replayable Kafka topic. 5) A sink connector applies each change to the WARM store / lake. A sample change event for an UPDATE on orders is shown: { op: 'u' (c=create, u=update, d=delete), ts_ms, source: {table: 'orders', lsn: 24117248 (log offset)}, before: {id:91, status:'PACKED'}, after: {id:91, status:'SHIPPED'} }. Why it beats batch dumps: no dual-write (the log is the source of truth), it captures every change including deletes, it's near-real-time, and the offset/LSN makes restarts exactly-once." width="1180">

Implementing it, at a high level: run a connector like **Debezium** that connects to the database as a replication client, reads the WAL/binlog from a saved **offset (LSN)**, and emits one **change event** per row mutation — each carrying `op` (create/update/delete), the **before and after** images, and the log position. Those events flow into a durable, replayable log (**Kafka**); a **sink** consumes them and applies each change to the warm store. Because the log is the single source of truth, CDC sidesteps the dual-write problem entirely, captures **deletes** (which polling can't see), runs in near-real-time, and resumes **exactly-once** from its last committed offset after a crash. CDC is the streaming-grade alternative to the batch dumper — reach for it when warm must stay tightly in sync, and keep the CRON dumper when a nightly lag is fine.

### Implementation: load and transform in practice (Airbyte + dbt)

Concretely, how do you build the extract-load and the transform? The modern ELT answer is a **connector tool for the EL** (Airbyte) plus **SQL for the T** (dbt) — and the key move is that they're *separate*: the connector lands data raw and untyped, and the transform is just SQL run over that raw table afterward.

<img src="../assets/multi-tiered-db/etl-implementation.svg" alt="A worked example of load and transform with Airbyte EL plus dbt. Top, the data shape at each stage: SOURCE row from postgres.public.orders (id=91, total=4999, currency='INR', status='SHIPPED', created=2023-07-02, with cursor updated_at) → AIRBYTE lands it RAW into _airbyte_raw_orders as one JSON blob in a _airbyte_data column plus metadata (_airbyte_ab_id, _emitted_at) → dbt transforms it into a MODELED typed table analytics.orders (order_id INT 91, total_inr DECIMAL 49.99, status ENUM SHIPPED, order_date DATE 2023-07-02, items[] denormalized) — cleaned, typed, partitioned. Bottom-left, the Airbyte connection config (the EL step): source type postgres with replication CDC (logical decoding); destination type s3 format parquet bucket orders-lake/bronze; stream orders with sync_mode incremental | append_dedup, cursor_field updated_at (the watermark), primary_key id (idempotent upsert), schedule every 1h. Airbyte handles extract, incremental cursor, schema detection, and dedup-on-primary-key automatically; it does NOT transform — it lands raw, that's the EL. Bottom-right, the dbt model (the T step, plain SQL): SELECT (_airbyte_data->>'id')::int AS order_id, (_airbyte_data->>'total')::dec/100 AS total_inr, (_airbyte_data->>'status') AS status, (_airbyte_data->>'created')::date AS order_date FROM _airbyte_raw_orders WHERE status <> 'TEST'. The transform is just SQL over the raw table: pluck fields out of the JSON blob, cast types (cents to decimal), filter/clean/denormalize, and materialize as a partitioned table. Raw stays untouched so you can re-run transforms, backfill, or fix a bug without re-extracting." width="1180">

Read the diagram left to right and the whole ELT clicks:

- **The Airbyte config** (the EL) names a `source` (Postgres, via CDC), a `destination` (S3 Parquet in the bronze bucket), and per-`stream` settings that *are* the reliability guarantees from earlier: `sync_mode: incremental` with `cursor_field: updated_at` is the **watermark**, and `primary_key: id` makes the load an **idempotent upsert**. Airbyte does extraction, incremental cursoring, schema detection, and dedup for you — but it deliberately **does not transform**; it lands the row as one JSON blob in `_airbyte_data`.
- **The dbt model** (the T) is *just SQL* over that raw table: pluck fields out of the JSON (`_airbyte_data->>'id'`), cast types (cents → decimal), filter test rows, denormalize, and materialize a clean, typed, date-partitioned `orders` table.

The payoff of keeping them separate: the **raw table is never mutated**, so you can re-run the transform, backfill history, or fix a logic bug *without re-extracting from the source database* — the source is touched exactly once.

> **Memory hook:** *hot→warm is ETL in three parts — Dumper (incremental extract via a watermark, in bounded batches, off-peak), Staging storage (decouples extract from load: idempotent retries, replay point, format conversion to Parquet), Loader (transform: denormalize/clean/enrich, then idempotent upsert). Copy → verify → THEN delete from hot. ETL transforms before load (warehouse); ELT loads raw cheap then transforms in place (lakehouse, the modern default).*

---

## Section 8 — The warm tier: distributed, horizontally scalable, read-only

**Question: the warm store holds finished orders that are read often but never updated. We dropped ACID and strong consistency to get here. What kind of database do those dropped requirements unlock, and which one do you pick?**

Because warm data is <span style="color:#8aff8a"><strong>read-only and non-transactional</strong></span>, you're freed from the single-master relational box and can use a <span style="color:#ffd27f"><strong>distributed, horizontally scalable database</strong></span> — add nodes to add read capacity and storage, linearly, far more cheaply than scaling up MySQL. The diagram lists five representative choices, and the *right* one depends on how the warm data is queried:

- <span style="color:#93c5fd"><strong>MongoDB</strong></span> — a document store. Perfect when the loader denormalized each order into a single self-contained document; "get order X" is a one-document fetch with no joins. (This is the [non-relational store](05-database-slack-realtime-communication-non-relational-databases.md) from the Slack post.)
- <span style="color:#93c5fd"><strong>Elasticsearch</strong></span> — a search/analytics engine. Choose it when warm reads are *searches* — "find all my orders containing 'headphones' between two dates" — rather than primary-key lookups. Inverted indexes make full-text and faceted queries fast.
- <span style="color:#93c5fd"><strong>TiDB</strong></span> — a distributed NewSQL database. Choose it when you still want SQL and horizontal scale together (it speaks the MySQL protocol), easing migration from the hot store.
- <span style="color:#93c5fd"><strong>DynamoDB</strong></span> — a managed key-value/document store with predictable latency at any scale and no servers to run. Choose it for simple, massive-scale key lookups when you'd rather not operate the database at all.
- <span style="color:#93c5fd"><strong>HDFS</strong></span> — the Hadoop Distributed File System. The odd one out: not a serving database but a bulk file store for analytics. It belongs here when "warm" means "queried in bulk by data jobs," not "looked up one order at a time." (We dissect HDFS in Section 10 — it's the gateway to the whole data-engineering ecosystem.)

The serving win: when the Orders service needs a <span style="color:#ffff99"><strong>historical order</strong></span> (older than the hot window), it reads from warm. The customer sees their full order history; the hot database never sees the query. But this raises the question that decides whether tiering is invisible or painful: **how does the Orders service know which tier to read from?**

### How does Orders know whether to read hot or warm?

A read arrives — `get_order(id)` — and *something* must pick a tier before touching a disk. There are four options, and they trade accuracy against cost.

<img src="../assets/multi-tiered-db/hot-warm-routing.svg" alt="Four ways the Orders router decides between HOT (recent, fast) and WARM (historical) for a get_order(id) read. Option A, age/timestamp threshold (the recommended default): the ID encodes creation time, so if age < 90d go to HOT, else WARM — no extra lookup, one branch; use when the cutover age is predictable. Option B, try HOT then fall back to WARM: look in HOT, and on a miss read WARM — always correct with no clock guess, but costs a wasted HOT lookup on every old read; use when ages are fuzzy. Option C, tier flag in a metadata/index table: a tiny table maps id to its current tier, updated when data moves — exact, but adds one lookup and must stay in sync; use when data moves irregularly. Option D, SLA/latency-based: a latency-critical call goes to HOT only while a tolerant call may read WARM — routes by the caller's need rather than the data's age, and combines with A. In practice: start with A (age), add D when some calls are latency-critical, and reach for B or C only when the move boundary is unpredictable." width="1180">

- <span style="color:#8aff8a"><strong>A · Age / timestamp threshold (the default).</strong></span> Make the order id **encode its creation time** (a time-sortable id like a Snowflake/ULID — see [distributed id generators](08-distributed-id-generators.md)), so the service computes age from the id alone: `age < 90d → hot, else → warm`. One branch, **zero extra lookups**. Use it when the cutover age is predictable — which, for orders, it is.
- <span style="color:#93c5fd"><strong>B · Try hot, fall back to warm.</strong></span> Look in hot; on a miss, read warm. Always correct with no clock assumption, but every *old* read pays a wasted hot lookup first. Use when ages are fuzzy.
- <span style="color:#93c5fd"><strong>C · Tier flag in a metadata table.</strong></span> Keep a tiny index mapping `id → current tier`, updated atomically when data moves. Exact, but it's one more lookup and a thing to keep in sync. Use when data migrates irregularly rather than by clean age.
- <span style="color:#ffd27f"><strong>D · SLA / latency-based.</strong></span> Route by the *caller's* need, not the data's age: a latency-critical path (checkout) reads hot only; a tolerant path (a yearly statement) may read warm. Layer it on top of A.

**The practical recipe:** start with **A** (age, free), add **D** when some endpoints are latency-critical, and only reach for B/C when the hot↔warm boundary genuinely can't be derived from age. That routing is the price of tiering, and done with A it's nearly free.

> **Memory hook:** *warm being read-only + non-transactional unlocks a horizontally scalable distributed DB — pick by query shape: MongoDB (document fetch), Elasticsearch (search), TiDB (distributed SQL), DynamoDB (managed KV at scale), HDFS (bulk analytics files). The Orders service routes recent reads to hot, historical reads to warm.*

---

## Section 9 — Warm → Cold: dump and load into S3, and S3's own storage tiers

**Question: warm data eventually cools too. Where does it go, and isn't "cold storage" itself just another tiering decision?**

The same dump-and-load pattern repeats one level down: a <span style="color:#ffd27f"><strong>dump-and-load</strong></span> job extracts the oldest data from warm and writes it to <span style="color:#93c5fd"><strong>cold storage — S3</strong></span>, "extremely cheap storage." Here the format is almost always **columnar Parquet, partitioned by date** (`s3://orders-archive/year=2023/month=07/...`), because the only readers are analytics engines that scan by date range and select a few columns. ELT applies: dump the raw data cheaply now, transform only if someone asks.

And cold storage is itself **tiered internally.** S3 isn't one thing — it's a ladder of storage classes that trade retrieval speed for price, and a <span style="color:#ffff99"><strong>lifecycle policy</strong></span> auto-transitions objects down the ladder by age. The rough shape (US pricing, order-of-magnitude):

| S3 storage class | ~Storage cost (per GB-month) | Retrieval latency | Use |
| --- | --- | --- | --- |
| **S3 Standard** | ~$0.023 | milliseconds | frequently-read cold data |
| **S3 Standard-IA** (Infrequent Access) | ~$0.0125 | milliseconds (+ per-GB retrieval fee) | monthly-ish reads |
| **S3 Intelligent-Tiering** | auto + small monitoring fee | milliseconds | unknown/changing patterns — auto-moves objects |
| **S3 Glacier Instant Retrieval** | ~$0.004 | milliseconds | rarely read, but instantly when needed |
| **S3 Glacier Flexible Retrieval** | ~$0.0036 | minutes to hours | archive, occasional restore |
| **S3 Glacier Deep Archive** | ~$0.00099 | up to ~12 hours | compliance archive, "hope to never read" |

Deep Archive is ~**20× cheaper** than Standard. For seven years of order history that's read approximately never, that ratio is the entire business case for the cold tier. A lifecycle rule like "Standard-IA at 90 days → Glacier Instant at 1 year → Deep Archive at 3 years" runs with zero application code — S3 moves the bytes for you. This is the **cost-based tiering** from Section 5, operating *inside* the cold tier.

One sharp edge to remember (it surprises people): **you cannot directly SQL-query data in Glacier Flexible Retrieval or Deep Archive** — it must be restored to an accessible class first. So if you expect to run the occasional Presto/Athena query over cold data, keep it in Glacier **Instant** Retrieval or Standard-IA, which stay queryable. Deep Archive is for data you'll only ever bulk-restore under a legal subpoena.

> **Memory hook:** *warm→cold is the same dump-and-load, writing date-partitioned Parquet to S3. Cold is itself tiered: Standard → Standard-IA → Glacier Instant → Glacier Flexible → Deep Archive (~20× cheaper, ~12h retrieval), auto-transitioned by an S3 lifecycle policy. But Glacier Flexible/Deep aren't directly queryable — keep queryable cold data in Glacier Instant or Standard-IA.*

---

## Section 10 — The HDFS ecosystem, in detail

**Question: HDFS keeps showing up as a "warm" or analytics store, and half the data-engineering world is built on top of it. What *is* it, how does it actually store a file, and is it even still the right choice in 2026?**

The <span style="color:#ffff99"><strong>Hadoop Distributed File System (HDFS)</strong></span> is a distributed file system for storing very large files across a cluster of cheap commodity machines, built (circa 2006, modeled on Google's GFS paper) for one workload: **high-throughput sequential reads over huge files for batch analytics** — not low-latency random access. Its design assumes hardware *will* fail constantly, and it follows a **write-once, read-many** model (files are append-only; no in-place edits).

### Architecture: NameNode + DataNodes

HDFS is master/slave:

- The <span style="color:#ff8bd2"><strong>NameNode (master)</strong></span> holds all the **metadata in RAM**: the directory tree, the file→block mapping, and which DataNodes hold which blocks. It stores **no actual file data**. It persists the namespace to two files: `fsimage` (a snapshot) plus an `edits` write-ahead log. Because it's a single brain, it was historically the single point of failure — production clusters run **NameNode HA**: an active + standby NameNode sharing the edit log through a quorum of **JournalNodes**, with automatic failover via ZooKeeper. (The confusingly-named "Secondary NameNode" is *not* a standby — it just periodically merges `edits` into `fsimage` to keep the log from growing forever.)
- The <span style="color:#93c5fd"><strong>DataNodes (slaves)</strong></span> store the actual data as blocks on their local disks. They send **heartbeats** (every ~3s) and periodic **block reports** to the NameNode. If a DataNode goes silent (~10 minutes), the NameNode marks it dead and **re-replicates its blocks** elsewhere to restore the replication factor.

### Blocks and replication

A file is split into fixed-size <span style="color:#ffff99"><strong>blocks</strong></span> — default **128 MB** (huge, to amortize seek time against transfer time and to minimize NameNode metadata). Each block is stored on **3 DataNodes** by default (<span style="color:#ffff99"><strong>replication factor 3</strong></span>) for fault tolerance and read parallelism. Placement is **rack-aware**: replica 1 local, replica 2 on a *different rack* (survives a whole-rack/switch failure), replica 3 on another node in that second rack (limits cross-rack write traffic, which is the scarce bandwidth). Hadoop 3 added **erasure coding** as a cheaper alternative to 3× replication for cold data — ~1.5× overhead instead of 3×, the same [erasure-coding tradeoff S3 makes](20-high-throughput-system-s3.md).

The **write path** is a pipeline: the client asks the NameNode for DataNodes, then streams packets to DataNode1 → DataNode2 → DataNode3 with ACKs flowing back. The **read path** keeps the NameNode out of the data path entirely: the client asks the NameNode only for *block locations*, then reads blocks directly from the nearest DataNode (checksums verified; on corruption it tries another replica). Move-compute-to-data: jobs run on the nodes that already hold the blocks.

### The famous weakness, and where HDFS sits today

HDFS has a notorious <span style="color:#ff8a8a"><strong>small-files problem</strong></span>: every file/block/directory costs ~150 bytes of NameNode *RAM*, so millions of tiny files exhaust the master's memory. HDFS wants a few enormous files, not many small ones. Its other structural limits: the single-NameNode metadata ceiling, and — the big one — **storage and compute are coupled** (you scale disks and CPUs together, and you pay for an always-on cluster).

**Is HDFS still used in 2026?** For *new* builds, largely **displaced by cloud object storage** — S3, GCS, Azure ADLS, or self-hosted MinIO. The reason is exactly the theme of this whole post and the [S3 post](20-high-throughput-system-s3.md): object stores **decouple storage from compute** (scale and pay for each independently), give 11-nines durability, near-infinite elastic capacity, and lower total cost with no NameNode to operate. HDFS persists in on-prem, regulated, air-gapped, and legacy Cloudera estates, and in latency/locality-sensitive jobs. The net: **greenfield = object storage + open table formats; HDFS = the incumbent you'll meet but rarely choose.** Crucially, the *concepts* — blocks, replication, the metadata master, move-compute-to-data — are exactly what S3 and the lakehouse re-implement under the hood, which is why understanding HDFS makes the rest of the ecosystem legible.

### The rest of the Hadoop stack: HDFS vs YARN vs MapReduce

Classic Hadoop is three layers people constantly conflate:

- <span style="color:#ffff99"><strong>HDFS</strong></span> = the **storage** layer (above).
- <span style="color:#93c5fd"><strong>YARN</strong></span> (Yet Another Resource Negotiator) = the **resource-management/scheduling** layer — a ResourceManager hands out cluster CPU/RAM as "containers" to jobs. YARN is what let Hadoop run *many* engines, not just MapReduce. (In the cloud, **Kubernetes** increasingly plays YARN's role.)
- <span style="color:#93c5fd"><strong>MapReduce</strong></span> = the original **batch compute** engine (map → shuffle/sort → reduce). It writes intermediate results to disk between every stage, which makes it slow and is exactly why **Spark replaced it.** MapReduce is legacy today.

The mental model: **HDFS stores, YARN allocates, an engine (Spark/Tez/MapReduce) computes, and a SQL layer (Hive/Trino) sits on top.** That SQL layer is how you actually query any of this — which is the next section.

<img src="../assets/multi-tiered-db/hadoop-ecosystem.svg" alt="The Hadoop ecosystem drawn as four stacked layers — HDFS stores, YARN allocates, an engine computes, SQL queries. Top layer, SQL/Query: Hive (legacy), Spark SQL, Trino/Presto (highlighted), Impala. Second layer, Compute engines: MapReduce (slow/legacy), Tez, Spark (highlighted — in-memory DAG, the workhorse), Flink. Third layer, Resource management: YARN (ResourceManager + NodeManagers) and Kubernetes (cloud-native). Bottom layer, Storage: the HDFS detail — a NameNode master holding metadata in RAM (the file→block→DataNode map, HA via JournalNodes, no file data) feeding three DataNodes that each hold 128MB blocks, with each block replicated three times and rack-aware. A dashed callout marks the 2026 default: a cloud object store (S3/GCS/ADLS) replaces HDFS — decoupling storage from compute, 11-nines durability, no NameNode to operate — while HDFS lingers in on-prem, regulated, and legacy clusters. Arrows on the left show queries flowing down and bytes flowing up between layers." width="1180">

> **Memory hook:** *HDFS = NameNode (metadata in RAM, HA via JournalNodes) + DataNodes (128MB blocks, replication factor 3, rack-aware), write-once-read-many, batch throughput. Weakness: small-files problem + coupled storage/compute. In 2026 mostly displaced by S3-style object stores (decoupled storage/compute) for greenfield. The stack: HDFS stores, YARN allocates, MapReduce/Spark computes, Hive/Trino query.*

---

## Section 11 — Querying cold storage: how do you actually run SQL on S3 or HDFS?

**Question: your seven years of orders are sitting as Parquet files in an S3 bucket — not in any database. An analyst writes `SELECT region, SUM(total) FROM orders WHERE year=2023 GROUP BY region`. There's no database to receive that query. How does it run?**

This is the question that the entire modern data stack exists to answer, and the answer is a **three-layer separation** that you must hold in your head, because every engine below is just a different filling of these three slots:

1. <span style="color:#ffff99"><strong>Storage</strong></span> — the files themselves, sitting in S3/HDFS as Parquet/ORC. Cheap, durable, dumb. Knows nothing about tables.
2. <span style="color:#8aff8a"><strong>Metadata catalog</strong></span> — the **Hive Metastore** (or **AWS Glue Data Catalog**, or an Iceberg/Unity catalog). This is the glue: a small service backed by a relational DB that records *"the files under `s3://…/orders/` form a table called `orders` with these columns, these types, and these date partitions."* It imposes table structure on a pile of files. **This catalog is the most important and most overlooked component in data engineering** — it's what lets a bucket of files be queried as a table, and it outlived Hive itself.
3. <span style="color:#93c5fd"><strong>Query engine (compute)</strong></span> — reads the catalog to learn the schema and file locations, then reads the files **directly from storage** and executes the SQL. The engine is **stateless and scales independently** of the storage. No bulk "load into a database" step ever happens.

The mechanism that ties storage to catalog is the <span style="color:#ffff99"><strong>external table</strong></span>: `CREATE EXTERNAL TABLE orders (...) PARTITIONED BY (year, month) STORED AS PARQUET LOCATION 's3://orders-archive/'`. "External" means the engine manages only the *metadata* — drop the table and the files survive. You're describing pre-existing files, not ingesting them. This is <span style="color:#ffff99"><strong>schema-on-read</strong></span>: the schema is applied when you query, not when you write.

### Why columnar files + partitioning make this fast *and* cheap

Two file-level tricks are what make querying-files-in-place practical rather than catastrophically slow:

- <span style="color:#ffff99"><strong>Columnar formats (Parquet, ORC).</strong></span> Analytical queries touch a few columns over many rows (`SUM(total)` reads only `total` and `region`). Columnar layout stores each column contiguously, so the engine **reads only the columns it needs**, and same-type columns **compress far better** (run-length, dictionary, delta encoding). Footers carry per-chunk **min/max statistics** enabling **predicate pushdown** — skip whole chunks that can't match the `WHERE`. (Contrast row-based **Avro**, which is great for streaming ingest and whole-row reads but bad for column-pruned analytics. Rule of thumb: **Avro for writes/streaming, Parquet/ORC for analytical reads.**)
- <span style="color:#ffff99"><strong>Partitioning.</strong></span> Lay the files out in directories by a column — `year=2023/month=07/` — and a query with `WHERE year=2023` reads **only that directory**, skipping everything else (**partition pruning**). For pay-per-scan engines this directly cuts the bill. (Bucketing — hashing rows into a fixed number of files by key — further helps equality filters and shuffle-free joins.)

Together: partition pruning skips directories → columnar stats skip chunks → projection reads only needed columns. That stack is why scanning a date-range of one column out of a petabyte costs cents, not a fortune.

### The engines, and the "slow but cheap" cold-query path

With the three layers in place, several engines can fill the compute slot over the *same* files:

- <span style="color:#93c5fd"><strong>Presto / Trino</strong></span> — a distributed **MPP, in-memory** SQL engine built for **interactive, ad-hoc** queries directly over object storage (and dozens of other sources via connectors, so it can even *join* S3 data against a live MySQL — **federation**). It owns no storage. This is the **"PrestoDB → slow response"** box in the architecture: querying cold Parquet straight off S3 is slower than a warehouse, but it costs almost nothing because there's no always-on cluster and no data duplication — perfect for data read approximately never. (History note: the original Facebook **Presto** forked in 2019; the fork was renamed **Trino** in 2020 and is what most people mean today.)
- <span style="color:#93c5fd"><strong>AWS Athena</strong></span> — **serverless Trino**. AWS runs the engine for you; you point it at S3, use the Glue Data Catalog for schema, and **pay per terabyte scanned** (~$5/TB, 10 MB minimum). Zero infrastructure. This is the cold-query path with no servers at all — and the reason columnar + partitioning matters becomes literal: fewer bytes scanned = smaller bill. (Caveat from Section 9: Athena can't read Glacier Flexible/Deep Archive directly.)
- <span style="color:#93c5fd"><strong>Spark SQL</strong></span> — Spark's SQL interface; reads the same files, but shines when the query is part of a larger **ETL or ML** job rather than an interactive lookup.
- <span style="color:#93c5fd"><strong>Hive</strong></span> — the original SQL-on-Hadoop (HiveQL compiled to MapReduce/Tez/Spark jobs). As an *engine* it's legacy and slow; but its **Metastore** became the industry-standard catalog that Trino, Spark, and Athena all speak. Hive's lasting contribution is the catalog, not the engine.

### The temp queryable DB pattern

There's a second cold-query path in the architecture, and it solves a specific pain: Presto-over-S3 is *slow*, so if the legal team needs to run **dozens of fast, interactive queries** over one slice of cold data (say, all of one seller's 2021 orders for a lawsuit), querying S3 directly each time is miserable. The fix is the <span style="color:#ffff99"><strong>temp queryable DB</strong></span>: run a <span style="color:#ffd27f"><strong>loader</strong></span> once to pull just that subset out of S3 into a temporary, fast, indexed database; let the team hammer it with quick queries for the duration of the investigation; then **throw the database away**. You pay for fast compute only for the window you need it, over only the data you need — a temporary "rehydration" of cold data into a hot-shaped store. It's tiering in reverse, on demand.

> **Memory hook:** *querying files-in-place = three layers — Storage (Parquet/ORC in S3/HDFS) + Catalog (Hive Metastore / Glue: "these files are a table") + stateless Query Engine. External tables + schema-on-read tie them together. Columnar format + partition pruning + predicate pushdown make it fast and cheap. Engines: Trino/Presto (interactive, slow-but-cheap over cold S3), Athena (serverless Trino, pay-per-TB-scanned), Spark SQL (ETL/ML), Hive (legacy engine, but its Metastore is the standard catalog). For repeated fast cold queries, load a subset into a throwaway temp queryable DB.*

---

## Section 12 — The compute engines: Spark, Flink, Hive, Trino, Airbyte, Airflow — when to use which

**Question: the ecosystem has a dozen tools with overlapping names and overlapping jobs. If you're handed a data problem, how do you pick the right one without cargo-culting?**

Every tool in the data stack fills one of four roles: **move data in (ingest), transform it (process), query it (serve), or coordinate the jobs (orchestrate).** Sort the tools by role and the choices become obvious.

<img src="../assets/multi-tiered-db/pipeline-ingest-transform-query.svg" alt="The general data pipeline: the same three stages — ingest, transform, query/serve — run at two cadences. Sources (apps, DBs, SaaS, events) on the left fan into two lanes. The BATCH lane (minutes to hours, the default for tiering): Airbyte/dumper on CRON or an Airflow DAG ingests into the lake (bronze), Spark batch plus dbt models transform it into the lake (gold), and Trino/Athena query it for BI dashboards. The STREAMING lane (milliseconds to seconds, real-time only): CDC/Kafka change events are ingested, Flink does stateful event-time transformation, and the result lands in a live store / alerts / real-time dashboard. Footnote: Lambda = run both lanes and merge; Kappa = one streaming lane, replay the log for history." width="1180">

The same three stages appear whether you run them in **batch** (cheap, minutes-to-hours — the right default for tiering) or **streaming** (costly, milliseconds-to-seconds — only where real-time matters). Now the per-role picks:

### Process / transform: Spark vs Flink (batch vs streaming)

- <span style="color:#93c5fd"><strong>Apache Spark</strong></span> — the dominant **batch ETL and ML** engine. It keeps intermediate data **in memory** and chains operations as a DAG (rather than writing to disk between stages like MapReduce), which is why it's **up to ~100× faster than MapReduce** on iterative workloads and won the batch crown. You write transformations over **DataFrames**; the **Catalyst** optimizer and **Tungsten** execution engine make them fast. Architecture: a **driver** builds the DAG and schedules tasks onto **executors** (worker processes), via a cluster manager (YARN/Kubernetes). It does streaming too, but as **micro-batch** (Structured Streaming): the stream is sliced into tiny batches, giving *seconds*-range latency with one unified batch/stream API. **Use Spark for:** heavy transformations, big joins/aggregations, the loaders in our pipeline, ML feature engineering and training.
- <span style="color:#93c5fd"><strong>Apache Flink</strong></span> — the **true streaming** engine: it processes records **one event at a time** through a continuous operator pipeline, giving **millisecond** latency. Its superpowers are **stateful** processing (maintain huge running state in RocksDB), **event-time** semantics with **watermarks** (correct windowing even when events arrive late or out of order), and **exactly-once** guarantees via distributed checkpointing. **Use Flink for:** genuine real-time needs — fraud detection, real-time alerting, sessionization, anything where seconds is too slow. **The real Spark-vs-Flink distinction:** Spark Structured Streaming is *micro-batch* (seconds, simpler); Flink is *true streaming* (milliseconds, native event-time/state). Pick Flink when latency and event-time correctness genuinely matter; pick Spark when seconds is fine and you want one engine for batch + stream + ML.

### Ingest / move data in: Airbyte

- <span style="color:#93c5fd"><strong>Airbyte</strong></span> — open-source **ELT / data-integration**. It's the **extract-and-load** layer: 600+ pre-built **connectors** pull data from sources (databases, SaaS APIs, files) and land it in your warehouse/lake, where you transform it later (often with dbt). It replaces hand-written extract scripts with maintained, incremental-sync connectors. **Use Airbyte for:** wiring up many sources cheaply and flexibly, self-hosted, when you have engineers to operate it. (Managed alternative: **Fivetran** — turnkey and reliable but pricier and not self-hostable. Rule of thumb: Airbyte for cost/control/customization, Fivetran for hands-off reliability.) For *continuous* low-latency replication off a database specifically, **Change Data Capture** ([from the CDC post](16-storage-engine-etl-cdc.md)) — tapping the DB's write-ahead log — is the streaming-grade alternative to batch dumpers.

### Serve / query: Hive, Trino/Presto, Athena, Spark SQL

Covered in Section 11. The one-line picker: **Trino/Presto** for interactive ad-hoc SQL over the lake; **Athena** when you want that serverless; **Spark SQL** when the query is part of an ETL/ML job; **Hive** only because you inherited it (but keep its **Metastore**).

### Orchestrate: Airflow (and friends)

- <span style="color:#93c5fd"><strong>Apache Airflow</strong></span> — the dominant **workflow orchestrator**. Pipelines are **DAGs** of tasks defined in Python; the scheduler triggers them on a schedule (it's the grown-up version of the **CRON** that kicks off our dumper) and handles dependencies, retries, backfills, and monitoring that bare cron can't. **Use Airflow for:** coordinating the whole hot→warm→cold pipeline — "dump at 2am, *then* load, *then* run the delete policy, *then* refresh the analytics tables," with retries and alerting. (Newer alternatives: **Dagster**, asset/lineage-centric; **Prefect**, Python-native and lightweight.)

### Batch vs streaming, in one frame

The two end-to-end philosophies: **Lambda architecture** runs *two* paths — a batch layer (accurate, slow) and a speed/streaming layer (fast, approximate) — merged at query time, at the cost of maintaining two codebases. **Kappa architecture** (Jay Kreps) drops the batch layer: a *single* streaming path handles both real-time and reprocessing (replay the log from an earlier offset through the same code). For an orders-tiering system, **batch is the right default** (tiering isn't urgent); reach for streaming only for the real-time parts (live fraud checks, live dashboards).

> **Memory hook:** *sort every data tool by role. Ingest: Airbyte (batch ELT connectors) / CDC (continuous). Process: Spark (batch ETL+ML, micro-batch streaming, seconds) vs Flink (true streaming, ms, stateful, event-time). Serve: Trino/Presto + Athena (interactive over lake) / Spark SQL (in-job) / Hive (legacy + the Metastore). Orchestrate: Airflow DAGs (grown-up CRON). Default to batch; stream only where latency truly matters.*

---

## Section 13 — Data lake vs warehouse vs lakehouse, and designing a modern data/ML pipeline

**Question: you now have all the parts. How do you assemble them into the pipeline a data or ML engineer is actually asked to build — and what's the difference between the "lake," the "warehouse," and the "lakehouse" everyone argues about?**

### Three storage philosophies

- <span style="color:#ffff99"><strong>Data warehouse</strong></span> — structured, modeled, **schema-on-write** data in a proprietary columnar store (Snowflake, Redshift, BigQuery). Clean and fast for BI, but rigid and expensive; you must know your schema up front and pay to transform before loading. ETL.
- <span style="color:#ffff99"><strong>Data lake</strong></span> — raw data of any shape in cheap object storage (S3 + Parquet), **schema-on-read**. Cheap and flexible, but with no transactions and weak governance it can rot into a "data swamp." ELT.
- <span style="color:#ffff99"><strong>Lakehouse</strong></span> — the modern synthesis: lake economics (cheap object storage, open formats) **plus** warehouse reliability (ACID, schema enforcement, BI performance). The magic is an **open table format** — <span style="color:#93c5fd"><strong>Apache Iceberg</strong></span>, <span style="color:#93c5fd"><strong>Delta Lake</strong></span>, or <span style="color:#93c5fd"><strong>Apache Hudi</strong></span> — a metadata/transaction log layered over your Parquet files that gives them **ACID transactions, schema evolution, and time travel** (query or roll back to a past snapshot) right on S3. Iceberg is the engine-neutral default in 2026; Delta is strongest in Databricks/Spark; Hudi pioneered record-level upserts/CDC. The lakehouse is the natural home for the cold tier of this whole design.

Within a lakehouse, data is refined in stages — the <span style="color:#ffff99"><strong>medallion architecture</strong></span>: **Bronze** (raw, append-only, exactly as ingested — your replayable source of truth) → **Silver** (cleaned, deduplicated, conformed — the trustworthy single version) → **Gold** (aggregated business marts and ML features). Quality rises Bronze→Gold; it's ELT made into a discipline.

### Designing the end-to-end pipeline (for data science and ML)

Put it all together and here is the modern pipeline a data/ML engineer designs — every box is a tool from above:

1. **Ingest.** **Airbyte** connectors and **CDC** streams (and our **CRON dumpers**) land raw data from the hot stores and external sources into the **Bronze** layer of an S3 data lake, as Parquet. **Airflow** schedules and supervises it.
2. **Transform.** **Spark** (batch) and/or **Flink** (streaming) jobs clean and conform Bronze → **Silver**, then aggregate Silver → **Gold** feature tables. **Iceberg/Delta** gives these tables ACID and time travel so reprocessing is safe.
3. **Serve / query.** Analysts and BI hit **Trino/Athena** over the Gold tables (interactive, no copy). The **temp queryable DB** pattern rehydrates subsets for heavy interactive sessions.
4. **ML.** The Gold **feature tables** feed model training (Spark MLlib, or exported to a training framework); **time travel** gives you reproducible training snapshots ("train on the data as it was on 2026-01-01"); a **feature store** serves the same features online for low-latency inference. The hot/warm/cold tiering keeps training data cheap to store and the serving features fast to read.

The through-line of the whole post: **match the store to the access pattern.** Live data → hot transactional DB. Recent reads → warm distributed DB. Cold archive and analytics → S3 lakehouse, queried in place. A data engineer's job is to build the rivers that move data between those stores reliably, idempotently, and cheaply — which is exactly the dumper → staging → loader machinery, repeated at every tier and dressed up with the right engine for each step.

> **Memory hook:** *warehouse = schema-on-write, structured, rigid (ETL); lake = schema-on-read, raw, cheap (ELT); lakehouse = lake + open table format (Iceberg/Delta/Hudi) giving ACID + time travel on S3, refined Bronze→Silver→Gold (medallion). The modern pipeline: Airbyte/CDC ingest → Spark/Flink transform → Trino/Athena serve → Gold features + time-travel snapshots feed ML training and a feature store.*

---

## Where this leaves us: the complete multi-tiered datastore

We started with one MySQL table that worked great for a few months and then buckled, and we resisted the obvious-but-wrong fix (shard harder). Instead we read the data's own access pattern, discovered that almost all of it is old and barely touched, and built a system that **moves data to storage matched to its temperature** — keeping the hot store small enough to stay fast forever. Around that one idea we assembled the entire data-engineering toolkit.

| Tier | Store | Workload | Built from |
| --- | --- | --- | --- |
| <span style="color:#ff8a8a"><strong>Hot</strong></span> | Transactional DB (MySQL/Postgres) | Read+write, ACID, low latency, expensive | Live orders (phase 1) |
| <span style="color:#ffd27f"><strong>Warm</strong></span> | Distributed DB (Mongo/ES/TiDB/Dynamo/HDFS) | Read-only, frequent, horizontally scalable, cheaper | Finished-but-popular orders (phase 2) |
| <span style="color:#93c5fd"><strong>Cold</strong></span> | S3 + lakehouse (Iceberg/Delta) | Read-only, rare, very cheap/slow | Compliance + analytics archive (phase 3) |

| Pipeline piece | Job | Tool |
| --- | --- | --- |
| **Dumper** | Incremental extract (watermark, batched, off-peak) | CRON / Spark, scheduled by Airflow |
| **Staging** | Decouple extract from load; idempotent retries; Parquet | S3 staging bucket |
| **Loader** | Transform (denormalize/clean/enrich) + idempotent upsert | Spark / dbt |
| **Query (cold)** | SQL over files in place | Trino/Presto, Athena (+ Hive Metastore/Glue) |
| **Temp queryable DB** | Rehydrate a subset of cold data for heavy interactive use | Loader → throwaway fast DB |

The shape to remember: **a transactional database stays fast not by getting bigger, but by getting *emptier* — continuously shedding its cold data down a ladder of cheaper stores, via ETL pipelines that extract, stage, transform, and load at every rung.** That ladder, and the engines that move data along it, *is* data engineering.

> **Memory hook:** *a multi-tiered datastore keeps the hot transactional DB small and fast by continuously aging data down hot → warm → cold to storage matched to its access pattern, using dumper → staging → loader ETL at every tier — and the cold tier is an S3 lakehouse queried in place by Trino/Athena. Match the store to the access pattern; build reliable, idempotent rivers between stores.*

---

## Further reading

- **[Designing Data-Intensive Applications, by Martin Kleppmann](https://dataintensive.net/)** — the foundational text for everything in this post: replication, partitioning, batch vs stream processing, and storage engines.
- **[Questioning the Lambda Architecture, by Jay Kreps](https://www.oreilly.com/radar/questioning-the-lambda-architecture/)** — the essay behind Kappa architecture and log-centric data movement.
- **[Building and operating a pretty big storage system (S3), by Andy Warfield](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html)** — how the cold tier really works underneath.
- **[The Google File System (2003)](https://static.googleusercontent.com/media/research.google.com/en//archive/gfs-sosp2003.pdf)** and **[HDFS Architecture](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HdfsDesign.html)** — the origins of the distributed-file-system ideas in Section 10.
- **[Apache Iceberg](https://iceberg.apache.org/), [Delta Lake](https://delta.io/), and [Apache Hudi](https://hudi.apache.org/)** — the open table formats that make the lakehouse possible.
- **[Trino: The Definitive Guide](https://www.starburst.io/info/oreilly-trino-guide/)** — querying data where it lives, in depth.
- The companion posts in this handbook: **[ETL and Change Data Capture](16-storage-engine-etl-cdc.md)**, **[the Bitcask-style KV engine](19-storage-engine-fast-kv-db.md)**, and **[Designing S3](20-high-throughput-system-s3.md)**.
