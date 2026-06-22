# Designing "Recent Searches": Storing and Retrieving a User's Search History

This post designs **recent searches** — the last handful of queries the search box shows before you type. It looks trivial; it isn't. The shape of that data decides whether the feature is cheap at a billion users or slow at a million.

But before the deep dive, a **recall**: a reusable checklist for choosing between SQL, document, and key-value stores, pulled from the [airline check-in](03-database-airline-checkin-transactions-indexes-locks.md) and [Slack realtime](05-database-slack-realtime-communication-non-relational-databases.md) posts.

**Question: blank schema, new feature — what do you look at *first*?** Not size, not team familiarity, not what's trendy. The **access pattern**: the exact reads and writes, how they're keyed, and what must be true the instant a write commits. Get it right and the database falls out of it.

```text
access pattern  ──►  storage choice
(reads, writes,      (SQL / document /
 keys, consistency)   key-value)
```

> **Memory hook:** *systems scale when the data model matches the access pattern — so pick the database from the access pattern, not from habit or hype. SQL buys flexibility, constraints, ACID, and rich queries; NoSQL buys a fixed access pattern, denormalization, and easy partitioning.*

---

## Recall 1 — Pick the database from the access pattern

**Question: what does "access pattern" actually mean, concretely enough to drive a decision?**

An access pattern is the *complete* list of how the data is touched. Write down four things for every read and write:

```text
shape        how is the row addressed?  by key · by time-range · by field-search · by join
volume/skew  how often, and does it pile onto one hot key?   read-heavy vs write-heavy
consistency  must a read see the latest write at once, or is a second of staleness fine?
correctness  is there an invariant across rows that must hold the instant a write commits?
```

That list *is* the specification the store has to satisfy — because every storage system is a **bet**: it makes some access patterns cheap by making others expensive. A relational database bets on flexible queries and cross-row correctness, and pays with harder partitioning. A key-value store bets every request starts with a key, and pays by being unable to answer "find all rows where X." There is no neutral choice — so you can only choose well once that list exists.

Here's the same exercise done for *this post's* feature, **recent searches**:

```text
recent searches — access pattern
  read    "my last 10 queries, newest first"     addressed by user_id
  write   add a query, dedupe, cap the list at 10 addressed by user_id
  volume  read on every search-box focus (heavy); one write per search
  bound   a stale entry for a second is fine; no invariant across users
```

Notice what that list does *not* contain: no joins, no cross-row invariant, no search *inside* the value — every operation is addressed by `user_id` and fetches/replaces the whole little list. That shape points hard at one quadrant of the map. To see which, run the list down this funnel — **take the first "yes":**

<img src="../assets/information-retrieval-recent-searches/access-pattern-decision.svg" alt="A top-to-bottom decision funnel titled 'Pick the store from the access pattern: walk top to bottom, take the first yes.' It starts from a box 'Write down the access pattern: reads, writes, keys, volume, consistency.' Then four ordered yes/no questions, each branching right to an outcome when answered yes, and falling through downward on no. Q1 'Correctness spans multiple rows? (multi-row transaction, constraints, joins must hold at commit)' → yes → RELATIONAL (SQL): Postgres/MySQL, shard by the request-scoping entity if one node is too small. Q2 'Every request starts from a known key? (get/put/del, the value is fetched and replaced whole)' → yes → KEY-VALUE: DynamoDB/Redis, hash(key) routes to one partition, Redis structures for ranking/counters. Q3 'Fields inside the record drive queries? (filter, search, partial-update by field; records differ in shape)' → yes → DOCUMENT: MongoDB, flexible schema plus secondary indexes on fields. Q4 'Write-heavy, time-ordered, staleness OK? (firehose of appends; read recent-N by time)' → yes → WIDE-COLUMN: Cassandra/ScyllaDB, key = (entity, time-bucket). If every answer is no, a dashed fallthrough box reads: Still ambiguous? Default to relational for query flexibility, then peel off the one painful access pattern into a specialized store later. Footer: the questions are ordered by how decisive they are — the first yes wins, even if a later one is also true." width="1080">

The questions are **ordered by how decisive they are**, so the first "yes" wins. Run recent searches through it: correctness across rows? No. Every request keyed by `user_id`, value fetched whole? **Yes — stop.** The funnel lands on <span style="color:#8aff8a"><strong>key-value</strong></span> before we've written a line of schema. (The deep dive will pressure-test that verdict — capping the list, dedup, and the read-heavy fan-out add wrinkles — but the *starting point* fell straight out of the access pattern.)

> **Memory hook:** *write the access pattern down first — shape, volume, consistency, correctness — then walk the decision funnel and take the first "yes." Correctness-across-rows → relational; keyed → key-value; fields-inside → document; write-heavy-time-ordered → wide-column.*

---

## Recall 2 — When SQL is the right call, and *why* each property matters

**Question: "use SQL when you need ACID" is the cliché. But ACID is four letters hiding a dozen real guarantees — so what, specifically, makes a relational database the correct tool, property by property?**

A relational database is strong when **correctness depends on relationships and constraints**, and when the transactional work **stays inside one database or one shard**. The shorthand from the Slack post:

```text
single-shard query   data lives together
ACID matters         constraints matter
fixed schema helps
```

That list is easy to recite and easy to under-appreciate. Here is *why* each line actually earns SQL the job — because each one names a thing the database does for you that you'd otherwise have to build, fragilely, in application code.

| Property | What it means | Why it makes SQL the right tool |
| --- | --- | --- |
| <span style="color:#93c5fd"><strong>Single-shard query</strong></span> | The data a request needs lives on one node, so the query never has to fan out across machines. | A relational engine's joins, transactions, and constraints are *cheap and strongly consistent only within one node*. The moment a query spans shards you pay scatter-gather latency and lose easy cross-shard transactions. If the workload naturally fits one shard, you keep all of SQL's power for free. |
| <span style="color:#ffff99"><strong>Data lives together</strong></span> | Related rows (a user, their orders, those orders' line items) sit in the same store and can be joined. | <span style="color:#8aff8a"><strong>Joins</strong></span> let you keep data **normalized** — one fact in one place — and assemble it at read time. You don't copy a user's name into a thousand rows; you join to it. That kills whole classes of update anomalies. |
| <span style="color:#ffff99"><strong>ACID matters</strong></span> | A multi-step change either fully happens or fully doesn't, and concurrent changes don't corrupt each other. | This is the [airline-seat problem](03-database-airline-checkin-transactions-indexes-locks.md): two people grab seat `6A`, and a <span style="color:#ffff99"><strong>transaction + row lock</strong></span> guarantees exactly one wins. "Charge the card **and** decrement inventory **and** create the order — all or nothing" is one transaction. Without ACID you reimplement it by hand and get it subtly wrong. |
| <span style="color:#ffff99"><strong>Constraints matter</strong></span> | Foreign keys, unique indexes, `NOT NULL`, and `CHECK` rules are enforced *by the database*, on every write, no matter which code path wrote it. | A foreign key rejects an order for a `user_id` that doesn't exist — <span style="color:#ff8a8a"><strong>no orphan rows</strong></span>, ever. A unique index makes "one active seat assignment per passenger" a law of physics, not a hope. The database is the *single* enforcement point, so a buggy new service can't violate the invariant. |
| <span style="color:#8aff8a"><strong>Fixed schema helps</strong></span> | Columns and types are declared up front; every row conforms. | A declared schema is **validation + documentation + an optimizer's contract** in one. The engine rejects malformed rows at the door, every reader knows the exact shape without defensive parsing, and the query planner uses column statistics and types to pick fast plans. When the data's shape is stable and known, rigidity is a feature, not a cost. |

The throughline: **SQL is the right call when the system's correctness is defined by rules across rows that must hold the instant a write commits, and that data is small enough (or shardable enough) to live together.** Payments, ledgers, booking systems, inventory, IRCTC-style flows — these *are* their constraints. You don't bolt ACID onto them later; it's the whole point.

> **Memory hook:** *SQL wins when correctness = relationships + constraints + multi-row transactions that live on one shard. Joins keep data normalized, the schema validates every write, and ACID makes "all-or-nothing" the database's job instead of yours.*

---

## Recall 3 — When NoSQL becomes attractive: model around a single key

**Question: if SQL is so powerful, why ever give up joins, constraints, and transactions? What does NoSQL buy that's worth that price?**

NoSQL becomes attractive when the data can be modeled around a **smaller, fixed access pattern** — small enough that nearly every request is shaped like:

```text
give me key K
write key K
delete key K
```

That looks limiting, and it *is*. **The limitation is the feature.** If every request starts with a key, the system never has to solve arbitrary joins, broad scans, or general aggregations on the hot path. And because the request is key-bound, the database can do the one thing SQL struggles with at scale — **route to exactly one partition**:

```text
partition = hash(key)   ->   one node owns key K   ->   no fan-out, predictable latency
```

This is why <span style="color:#93c5fd"><strong>key-bound workloads partition almost infinitely</strong></span>. There's no cross-node coordination to do, because the work for any single request lives on a single node. You traded query flexibility for massive, predictable horizontal scale.

The second thing NoSQL embraces is <span style="color:#ff8bd2"><strong>denormalization</strong></span> — deliberately copying data so a read needs no joins:

```text
copy display_name into many documents
copy a product snapshot into each order
copy channel metadata into each message view
```

A denormalized read is a single fetch with no joins — fast and partition-local. The price is **redundancy and** <span style="color:#ff8a8a"><strong>eventual consistency</strong></span>: when the user changes their last name, every copy must be updated later, and until they all converge, different reads may show different values. Sometimes that's fine (a profile card showing a stale name for a second). Sometimes it's not (a ledger showing the wrong balance — never). The access pattern's *correctness bar* decides.

So the trade, stated as a single line:

```text
SQL    -> flexibility, constraints, ACID, rich queries
NoSQL  -> fixed access pattern, denormalization, easier partitioning
```

> **Memory hook:** *NoSQL wins when the whole workload reduces to get/write/delete a key — key-bound requests route to one partition (near-infinite scale), and denormalization removes joins at the cost of redundancy and eventual consistency. The narrow access pattern is what makes it scale.*

---

## Recall 4 — The two NoSQL shapes you'll actually reach for

**Question: "NoSQL" isn't one thing. When you've decided to go non-relational, how do you choose between a document store and a key-value store?**

For most product features the choice is between two shapes. (Column-oriented stores like Redshift/BigQuery and graph stores like Neo4j exist for analytics and traversals respectively, but the everyday decision is these two.)

| | <span style="color:#93c5fd"><strong>Document database</strong></span> | <span style="color:#8aff8a"><strong>Key-value store</strong></span> |
| --- | --- | --- |
| **Examples** | MongoDB, Elasticsearch-style document indexes | DynamoDB, Redis |
| **Data shape** | JSON-like documents | Opaque value behind a key |
| **Schema** | Flexible — fields can vary per document | None — the store doesn't look inside the value |
| **Access** | Read/update *parts* of a document; query and filter by fields | `GET` / `PUT` / `DEL` by key; the value is a blob |
| **Query model** | Richer — secondary indexes, field filters, text search, aggregations | None beyond the key; partition aggressively on `hash(key)` |
| **Reach for it when** | The **fields inside the document matter** — you filter, search, or partially update them | **Every operation starts with a key** and you never need to look inside the value to find it |

The dividing line is one question: **do the fields inside the value matter to your queries?**

- If you often **update one field, filter by a field, search text, or aggregate over attributes**, you need the database to *see inside* the value — that's a <span style="color:#93c5fd"><strong>document store</strong></span>. A blog post with `title`, `body`, `tags`, `author`, `updated_at` where the request is "update only the title" or "find posts tagged `redis`" needs partial updates and field indexes. Rewriting the whole blob to change one field, or scanning everything to filter, is the pain a document store removes.
- If the value is **opaque** — you only ever fetch and replace it whole, addressed by a key you already have — you need a <span style="color:#8aff8a"><strong>key-value store</strong></span>. Because it never has to look inside, it partitions on `hash(key)` and routes every request to one node, which is exactly what lets DynamoDB and Redis scale so far for key-based workloads.

> **Memory hook:** *document store = the fields inside matter (filter, search, partial-update by field); key-value store = the value is an opaque blob you fetch whole by a key you already hold. "Do my queries look inside the value?" picks between them.*

---

## Recall 5 — Running the checklist: eight "which store, and why?" calls

The framework only counts if you can apply it cold. Below are eight features, **deliberately shuffled — not grouped by answer**, so no section header hands you the store. For each: read the scenario, decide *which storage system, what's the key, and why*, then check yourself against the reasoning. One checklist produces all eight verdicts:

```text
access pattern  →  correctness across rows?   → relational
                   every request holds a key?  → key-value
                   fields inside drive queries? → document
                →  consistency bar  →  read/write throughput  →  one node or sharded?
```

One reminder before you start: **read throughput** and **write throughput** are separate dials. A store can fit your *shape* and still fall over on your *volume* — and the read:write ratio (and whether writes pile onto one hot key) often picks the engine on its own.

---

### Case 1 — The last seat

**Question: 10,000 fans race for the last 50 concert tickets; two people tap seat `6A` in the same second. The system must never oversell or double-book a seat. Reads are modest, but writes all collide on the *same* contended unit. Which store, and where does it hurt?**

Picture the race with no protection — it's a <span style="color:#ff8a8a"><strong>lost update</strong></span>:

```text
T1: read stock = 1 ┐
T2: read stock = 1 ┘ both see "1 left"
T1: write stock = 0
T2: write stock = 0   → two buyers, one seat: OVERSOLD
```

The answer is a <span style="color:#ffff99"><strong>relational ACID database</strong></span> (the [airline-seat design](03-database-airline-checkin-transactions-indexes-locks.md)), because the engine kills that race natively and in one line:

```text
UPDATE ... SET stock = stock - 1 WHERE sku_id = ? AND stock > 0
        └─ check and write fused into ONE atomic step; 0 rows = sold out
```

`SELECT … FOR UPDATE` covers the multi-step case (pick a seat, hold it, pay), and `UNIQUE(event_id, seat_id)` makes a double-book physically unrepresentable. The instructive part is the **bottleneck**: every buyer of one SKU converges on <span style="color:#ff8a8a"><strong>the same hot row</strong></span>, which tops out around 5–10k serialized updates/sec because each writer must lock it in turn. You can't hash your way out — the **contention *is* the correctness mechanism**. (A pure counter *could* live in a DynamoDB/Redis atomic counter; relational wins the moment the purchase is a multi-row order with line items, payment, and rollback.) ([Preventing oversell in SQL](https://karhdo.dev/blog/how-to-prevent-overbooking-in-sql-with-multiple-methods))

*Takeaway: overselling is a lost-update race on one hot row — the relational engine solves it with a single atomic `UPDATE … WHERE stock > 0`, and that same row is the write-throughput ceiling.*

### Case 2 — The short link

**Question: `GET /abc123` resolves to a long URL on every redirect. Billions of codes exist, a viral link is hit millions of times, the mapping never changes once created, and you never query *by* the URL — only by the code. Which store?**

The spine is the read:write ratio:

```text
1 write  (create the code)  ──────────────►  N reads  (every redirect, forever)
hash(short_code) spreads codes evenly → reads scale out, no single-node ceiling
```

A <span style="color:#8aff8a"><strong>key-value store</strong></span> — DynamoDB for the durable `short_code → long_url` mapping, usually fronted by a Redis cache, partition key `short_code`. This is the textbook <span style="color:#8aff8a"><strong>write-once, read-many</strong></span> workload: a `GET(short_code)` *is* the entire query — no joins, no aggregation, no looking inside the value — so SQL's machinery is dead weight. Hashing on the code spreads entries uniformly, so read throughput grows near-linearly as you add nodes, and a random code spreads even a viral link's load. ([URL shortener design](https://systemdesign.one/url-shortening-system-design/))

> **One trap:** if codes come from an auto-increment counter, *that counter* becomes a <span style="color:#ff8a8a"><strong>write hot-spot</strong></span> even though the lookup table is perfectly spread. Use a distributed ID generator or pre-allocated ranges — the [ID-generators post](08-distributed-id-generators.md) lesson.

*Takeaway: write-once/read-many lookups by an opaque code are the canonical key-value win — hash on the code and reads scale out.*

### Case 3 — The mixed catalog

**Question: one searchable product set holds a bicycle (`gears`, `suspension`) and a fishing rod (`line_weight`, `guides`) — every category has different attributes. Shoppers filter `price > 500` across everything and `gears >= 20` for bikes only, and you add new categories often. Reads dominate. Which store?**

Try to force this into a relational table and you hit a fork, both bad:

```text
one wide table  → a column per attribute of every category → mostly NULLs (sparse)
many tables     → one per category → JOIN sprawl to query the catalog as a whole
```

So the answer is a <span style="color:#93c5fd"><strong>document database</strong></span> — MongoDB's polymorphic pattern, each product a document carrying only its own fields. The queries reach *inside* the record (which a key-value store can't), the records have **different shapes** (which the wide table or the join sprawl punish), and a <span style="color:#ffff99"><strong>flexible schema</strong></span> makes each document self-describing. <span style="color:#8aff8a"><strong>Secondary indexes</strong></span> on `price`/`category`/attributes make the **read-heavy** filtering fast, and a new category is just new-shaped documents — no `ALTER TABLE`. The same shape fits **user profiles / CMS**: nested, co-accessed data read whole in one fetch instead of a join. ([MongoDB polymorphic pattern](https://www.mongodb.com/docs/manual/data-modeling/design-patterns/polymorphic-data/polymorphic-schema-pattern/))

> **One caution:** embed only *bounded* sub-data. Unbounded growth (millions of events on a profile) should be **referenced**, not embedded, or you hit the document-size limit and rewrite the whole document on every append.

**Read-heavy vs write-heavy — does MongoDB prefer one?** Both work, but MongoDB *shines read-heavy*, and a catalog is exactly that:

```text
read-heavy  (catalog)   10,000 shoppers filter  : 1 merchant edit   → MongoDB ✓
write-heavy (firehose)  millions of appends/sec, query by one key   → wide-column (Case 4)
```

A catalog's reads dominate, so MongoDB's secondary indexes earn their keep on every `price`/`category` filter, while the rare merchant edit is a cheap **partial update** of one field — no whole-document rewrite. MongoDB handles moderate write rates fine; it's a sustained *write firehose* (every op a write, data outgrowing RAM) where it breaks and you move to a wide-column store. **Rule of thumb: MongoDB for read-heavy/balanced data you query by field; wide-column once writes become a firehose.**

*Takeaway: when records in one set have different fields AND your queries filter on those fields, the document model removes the "sparse columns vs join sprawl" dilemma — and it's the right call when reads dominate, not when writes become a firehose.*

### Case 4 — The message firehose

**Question: store every chat message ever sent — now trillions, arriving as a relentless write stream — and still serve "the most recent N in this channel" and scroll-back instantly. A second of staleness is fine. One machine can't hold it. Which store, and what's the key?**

The read shape is the clue — it's a contiguous slice, not a scan:

```text
partition key = (channel_id, bucket)      bucket = ~10-day window (keeps partitions small)
  inside a partition: message_id  ↓ newest-first
  "recent N" = read the first N rows of the partition — no scan, no sort
```

The answer is a <span style="color:#ffff99"><strong>wide-column store</strong></span>, and Discord's path is the canonical lesson: **MongoDB → Cassandra → ScyllaDB**. The workload is overwhelmingly <span style="color:#ff8bd2"><strong>write-heavy</strong></span> with **eventual consistency acceptable** (QUORUM) — exactly what a wide-column store is built for: linear write scale, tunable consistency, and a partition key that makes time-range reads cheap.

**Why not MongoDB — it's a document store too?** Because this workload doesn't use what a document DB is *for*, and its volume breaks what it's *good at*:

- **You never query by fields inside a message.** The only read is "recent N in this channel" — a partition + time lookup, not a filter on message fields. MongoDB's field-query / secondary-index strength sits unused; you'd pay for flexibility you don't need.
- **It's a write firehose, and the storage engines differ.** Cassandra/ScyllaDB are <span style="color:#ffff99"><strong>LSM / log-structured</strong></span> (the [LSM post](22-high-throughput-lsm-trees.md)) and **masterless** — every node accepts writes as cheap appends, no read-before-write — so write throughput scales linearly with nodes. MongoDB funnels writes through **one primary per shard** and maintains a B-tree index on every write: fine for moderate rates, not a trillion-message firehose.
- **Data ≫ RAM.** MongoDB is happiest when the working set (especially indexes) fits in memory — the exact wall Discord hit at ~100M messages. A wide-column store is built for data far larger than RAM, served straight off the partition key.

So the dividing line *inside* NoSQL:

| | <span style="color:#93c5fd"><strong>Document (MongoDB)</strong></span> | <span style="color:#ffff99"><strong>Wide-column (Cassandra/Scylla)</strong></span> |
| --- | --- | --- |
| **Pick when** | you query by varied fields inside the record | the access pattern is fixed and known: partition + clustering key |
| **Throughput** | <span style="color:#8aff8a"><strong>read-heavy</strong></span> or balanced | <span style="color:#ff8bd2"><strong>write-heavy</strong></span> firehose, linear write scale |
| **Data size** | working set ~ fits RAM | data ≫ RAM |
| **Shape** | rich/nested docs, ad-hoc queries | time-series · events · messages |

Read vs write here: the headline is **write** (every message is a write, trillions of them). Reads are cheap recent-N slices, but spike on popular channels — a *read* hotspot Discord fixed with **request coalescing** (fold concurrent reads of one hot row into a single DB hit), not a write problem. That, plus <span style="color:#ff8a8a"><strong>hot partitions</strong></span>, **JVM GC pauses**, and compaction backlog at 177 Cassandra nodes, is why they moved to ScyllaDB (C++, no GC) — 177 → 72 nodes, tail latency collapsed. ([How Discord stores trillions of messages](https://discord.com/blog/how-discord-stores-trillions-of-messages))

*Takeaway: document store = query by fields, read-heavy, fits RAM; wide-column = one known key, write firehose, data ≫ RAM. Messages are the second — keyed by `(entity, time-bucket)`, and operational reality (GC, compaction, hot partitions) can drive the engine as much as the data model.*

### Case 5 — The money ledger

**Question: money moves — a debit on one account, a matching credit on another. Both must land or neither must, every cent must reconcile, and a double-submit must never post twice. Volume is moderate; correctness is absolute. Which store?**

A <span style="color:#ffff99"><strong>relational ACID database</strong></span> — Postgres or MySQL/InnoDB, money modeled as an append-only **double-entry ledger** (at global scale, distributed SQL like Spanner — still relational, still ACID). Here the deciding dial isn't throughput at all — it's that correctness is *defined* across rows:

```text
BEGIN
  INSERT debit  (acct A, -100)
  INSERT credit (acct B, +100)   ← both, or neither
COMMIT
```

<span style="color:#ffff99"><strong>Atomicity</strong></span> makes the debit+credit one indivisible unit. <span style="color:#ffff99"><strong>Constraints encode the invariants in the engine</strong></span> — `CHECK (balance >= 0)`, foreign keys, and a unique idempotency key that turns a double-post into a database error rather than a duplicate charge, no matter which buggy service issued it. Under concurrency you escalate isolation (atomic `UPDATE` → `SELECT … FOR UPDATE` → `SERIALIZABLE`) to block <span style="color:#ff8a8a"><strong>lost updates and write-skew</strong></span>. An eventually-consistent store that shows the wrong balance for even a moment is simply wrong — which rules NoSQL out. ([Postgres isolation](https://www.postgresql.org/docs/current/transaction-iso.html), [Square's ledger on Spanner](https://developer.squareup.com/blog/books-an-immutable-double-entry-accounting-database-service/))

*Takeaway: when money correctness is non-negotiable, make illegal states unrepresentable with ACID transactions + constraints — the one thing NoSQL can't natively give you.*

### Case 6 — The login check

**Question: every authenticated request must answer "is this token valid, and whose is it?" — a read on *every* page load — and idle sessions should vanish on their own. The value is a small opaque blob you only ever fetch whole by its id. Which store?**

A <span style="color:#8aff8a"><strong>key-value store</strong></span> — Redis (in-memory) or DynamoDB with TTL, key `session:{id}`. The access pattern is a pure point op (`GET`/`SET` by one id, never a join or field filter), so SQL's planner + index cost is pure overhead on the **hottest read path in the whole system**. This is brutally **read-heavy** — <span style="color:#8aff8a"><strong>every request reads the session</strong></span>, writes only happen at login/refresh — and an in-memory `GET` answers sub-millisecond where a disk-backed `SELECT` can't. The clincher is <span style="color:#93c5fd"><strong>native TTL</strong></span>: idle sessions auto-expire with no cleanup job, and a "sliding" session just refreshes the TTL on each read. ([Redis session store](https://redis.io/docs/latest/develop/use-cases/session-store/nodejs/), [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html))

*Takeaway: read-on-every-request + opaque value + self-expiry = in-memory key-value with native TTL.*

### Case 7 — Past one machine

**Question: you run a B2B work-management app — workspaces full of projects, tasks, members, and permissions. One action like *"move this task to another project and reassign its owner"* updates several related rows at once, almost every screen joins tasks → projects → users, and a member can only touch tasks their role allows. You're now at tens of millions of workspaces and a single Postgres can't absorb the write volume. Which store?**

**Name the data's *shape* before its scale.** The task-move updates several rows at once (a transaction), every screen joins across entities, and permissions are constraints → **relational**. That's a fact about the *data*; having too much of it doesn't change it. Shape and scale are independent axes:

```text
                    │ fits on one node   │ outgrew one node
 ───────────────────┼────────────────────┼───────────────────
 RELATIONAL         │ single Postgres    │ SHARDED SQL   ← here
 txns · joins · FKs │                    │ (logical shards)
 ───────────────────┼────────────────────┼───────────────────
 DOCUMENT           │ single MongoDB     │ sharded MongoDB
 fields drive query │                    │
```

Your shape fixes the **row**; growth only slides you **right** along it — relational that outgrows one node → sharded SQL, never *down* to the document row (that drops the txns, joins, and FKs you need, just to dodge scale). <span style="color:#ffff99"><strong>Shard the relational database and keep it relational.</strong></span>

**Pattern to bank:** *relational data + a single-node write ceiling = a **logical-sharding** case, not a NoSQL rewrite.*

### What lands on a shard — and why ACID survives

Look at the tables to see *why* a request lands on one shard. **Every table that belongs to a workspace carries the shard key `workspace_id`,** and every row is routed by it:

```text
workspaces( id,               name, plan )            shard key = id
projects(   id, workspace_id, name )                  shard key = workspace_id
tasks(      id, workspace_id, project_id, owner_id )  shard key = workspace_id
members(    id, workspace_id, user_id, role )         shard key = workspace_id

route every row:   shard = hash(workspace_id) % 4096   →   same workspace, same shard
```

Now the asymmetry is visible. "Load workspace W's board" or "move a task, reassign its owner" touches only `workspace_id = W` rows — **all on one shard** — so the join `projects ⨝ tasks ⨝ members` and the `BEGIN … COMMIT` run locally with full ACID. Only the rare cross-workspace query — "user X's tasks *everywhere*" — must fan out (both paths drawn below).

**Why `workspace_id`, not `task_id`/`user_id`?** Shard by whatever sits in the `WHERE` of almost every query. Shard by `user_id` and a workspace's members (each a *different* user) scatter, making every board-load cross-shard.

**Partitioning & scale.** Hash the key (`hash(workspace_id) % N`, or bake the shard id into the primary key as Instagram/Pinterest do) for even spread and no <span style="color:#ff8a8a"><strong>recent-range hotspot</strong></span>. Each shard is an **independent primary**, not a read-only replica — so sharding scales <span style="color:#ff8bd2"><strong>writes</strong></span>, not just <span style="color:#8aff8a"><strong>reads</strong></span> (add per-shard replicas too if reads dominate).

Routing goes through one layer of indirection — a <span style="color:#ffff99"><strong>logical shard</strong></span> — not straight to a machine:

```text
data is keyed into many LOGICAL shards    S0  S1  S2  S3  S4  S5 ... S4095
                                            \   \   |   /   /   /
those map onto a few PHYSICAL machines  →  [  box A  ]  [  box B  ]  [  box C  ]

route:  logical shard = hash(workspace_id) % 4096   →   lookup table   →   which box
```

A **logical shard** is a unit of data ownership (a keyspace slice, often a Postgres **schema**); a **physical shard** is a machine. Create thousands up front, pack many per box — then rebalancing is just **moving a logical shard between boxes** (copy a schema, flip the routing table, no re-keying). Notion re-sharded [32 → 96 machines](https://www.notion.com/blog/the-great-re-shard) this way.

The bet in two lines — then the two paths it produces:

```text
writes split across shards   each box owns a DISJOINT slice of workspaces
                             → total write capacity = sum of all boxes (the scaling win)

ACID per shard               a txn touching rows in ONE shard → full ACID, unchanged
                             a txn SPANNING shards            → no cheap distributed txn
```

<img src="../assets/information-retrieval-recent-searches/single-vs-cross-shard.svg" alt="Two side-by-side request flows. LEFT, 'Common case · single-shard transaction' (green): an App/API box sends a request 'workspace W → shard B' to exactly one of three shard cylinders; Shard B is highlighted green while Shard A and Shard C are dimmed and labelled 'untouched.' An arrow runs from Shard B into a transaction box reading BEGIN / UPDATE task SET project = P / UPDATE task SET owner = U / CHECK member role permits it / COMMIT, annotated 'all rows in one box · joins + FKs work.' A badge reads '✓ ordinary single-node ACID — unchanged.' RIGHT, 'Rare case · cross-shard query' (red): the App/API box sends dashed 'scatter → ask EVERY shard' arrows to all three shard cylinders (A, B, C, all lit red); each shard returns rows via 'gather partial rows' arrows into an 'App-side join' box that reads 'the app collects each shard's rows and stitches + joins them itself — no DB join.' A badge reads '✗ no single transaction across shards.' Footer: 'Shard by the entity that scopes nearly every request, so the left path is the rule and the right path stays the exception.'" width="1080">

Instagram, Notion, and Pinterest all did exactly this at the single-node write ceiling:

- **Instagram** — shard Postgres by `user_id`, logical shards *as Postgres schemas*, IDs generated in-DB (41-bit time + 13-bit shard + sequence) so an id sorts by time *and* names its own shard. ([Sharding & IDs at Instagram](https://instagram-engineering.com/sharding-ids-at-instagram-1cf5a71e5a5c))
- **Notion** — shard Postgres by `space_id` (workspace), because nearly every request is workspace-scoped, so a workspace's blocks/comments stay <span style="color:#ffff99"><strong>co-located on one shard</strong></span> — exactly where ACID still holds. They kept relational *specifically* to preserve transactions across the block model. ([Sharding Postgres at Notion](https://www.notion.com/blog/sharding-postgres-at-notion))
- **Pinterest** — shard MySQL with the shard id embedded in a 64-bit object id, **deliberately giving up cross-shard joins/FKs** for app-side mapping lookups, keeping each shard a boring, fully-relational DB. ([Sharding Pinterest](https://medium.com/pinterest-engineering/sharding-pinterest-how-we-scaled-our-mysql-fleet-3f341e96ca6f))

*Takeaway: shape and scale are separate questions — relational data that outgrows one node slides right (→ sharded SQL), it doesn't switch rows to NoSQL. Shard by the request-scoping entity so the common transaction stays single-shard with full ACID, and use logical shards so rebalancing is a move, not a re-key.*

### Case 8 — The live ranking

**Question: a game leaderboard takes thousands of score updates per second and must answer "top 10" and "what's my rank?" instantly. A relational `ORDER BY score LIMIT 10` re-sorts or scans on every call. The data is small but the update rate is brutal. Which store?**

A <span style="color:#8aff8a"><strong>key-value store with a purpose-built structure</strong></span> — Redis **sorted sets**. The value here isn't an opaque blob; it's a structure that keeps members <span style="color:#ffff99"><strong>pre-sorted by score</strong></span>, so `ZADD`/`ZINCRBY` (update) and `ZREVRANGE 0 9` / `ZREVRANK` (top-N, rank) are `O(log N)` even with millions of players — where the relational query pays a re-sort each call. It's a <span style="color:#ff8bd2"><strong>write-heavy</strong></span> workload, and `ZINCRBY`/`INCR` are **atomic without locks** — no transaction, no row contention. The same engine does rate limiting (fixed-window `INCR`+`EXPIRE`, or a sliding-window log in one Lua script), with TTL making per-window counters self-cleaning. ([Redis sorted sets](https://redis.io/docs/latest/develop/data-types/sorted-sets/))

*Takeaway: for ordered or atomic-counter ops at high write throughput, an in-memory structure turns an expensive query into an O(log N) primitive.*

---

## The whole framework on one page

| Signal in the requirements | Pick | Throughput shape |
| --- | --- | --- |
| Every request holds a known key; value opaque; want TTL + horizontal scale | **Key-value** (DynamoDB/Redis) — sessions, cart, URL shortener | read-heavy point ops; scale out on `hash(key)` |
| Need ordering or counting at high update rate | **Redis structures** — leaderboards, rate limiters | write-heavy, atomic, O(log N) |
| Fields *inside* the record drive queries; shapes differ; schema evolves | **Document** (MongoDB) — catalog, profiles, CMS | read-heavy with field indexes |
| Multi-row atomicity, constraints, joins, money correctness | **Relational** (Postgres/MySQL) — ledgers, inventory, orders | correctness-bound; hot-row write contention |
| Write-heavy, time-ordered range reads, eventual consistency OK | **Wide-column** (Cassandra/ScyllaDB) — chat, feeds | write-firehose; partition by `(entity, time)` |
| Outgrew one SQL node but still need transactions/joins | **Sharded SQL** — Instagram, Notion, Pinterest | writes split across shards; ACID per shard |

Notice that **the same checklist produced every row**: write the access pattern, ask whether correctness lives across rows (→ relational) or every request holds a key (→ key-value) or the fields inside drive queries (→ document), then size it against read vs write throughput and ask whether it fits one node. That's the whole method.

> **Memory hook (recall):** *access pattern → keyed-or-relational-or-field-driven → consistency bar → read/write throughput → one node or sharded. Run those five questions in order and the storage choice falls out — every case above is just that checklist applied.*

---

With the storage-selection framework in hand, we can now run it on the actual subject of this post — **recent searches** — and watch which way each question tips. That deep dive is next.

