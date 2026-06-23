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

This is why <span style="color:#93c5fd"><strong>key-bound workloads scale out so well across many keys</strong></span> — there's no cross-node coordination, because the work for any single request lives on one node. The one catch is <span style="color:#ff8a8a"><strong>skew</strong></span>: the spread is even only when traffic is spread across *many* keys; a single hot key still lands on one node, so you cache or split it. Across well-distributed keys, you've traded query flexibility for massive, predictable horizontal scale.

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
| **Data shape** | JSON-like documents | A value you fetch whole by its key — a blob, or a purpose-built structure (e.g. a Redis sorted set) |
| **Schema** | Flexible — fields can vary per document | You model around the **key**, not the value's internals |
| **Access** | Read/update *parts* of a document; query and filter by fields | `GET` / `PUT` / `DEL` by key |
| **Query model** | Richer — secondary indexes, field filters, text search, aggregations | Key-based by default; partition on `hash(key)`. (Real engines add more — DynamoDB secondary indexes, Redis structures — but you pick it for keyed access.) |
| **Reach for it when** | The **fields inside the document matter** — you filter, search, or partially update them | **Every operation starts with a key** and you fetch/replace the value whole |

*(The table is the **stance you pick them for**, not a capability ceiling: DynamoDB also has secondary indexes and ACID transactions, Redis has rich structures. You reach for key-value because your access is key-shaped — not because the engine can't do more.)*

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

A <span style="color:#8aff8a"><strong>key-value store</strong></span> — DynamoDB for the durable `short_code → long_url` mapping, usually fronted by a Redis cache, partition key `short_code`. This is the textbook <span style="color:#8aff8a"><strong>write-once, read-many</strong></span> workload: a `GET(short_code)` *is* the entire query — no joins, no aggregation, no looking inside the value — so SQL's machinery is dead weight. Hashing on the code spreads *distinct* codes uniformly, so aggregate read throughput grows near-linearly as you add nodes. One thing the hashing does **not** solve: a *single* viral link is one key, so every hit lands on the **same** partition — that hot key is absorbed by the fronting cache/CDN, not by sharding (hashing spreads many keys, never the reads of one). ([URL shortener design](https://systemdesign.one/url-shortening-system-design/))

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

**Read-heavy vs write-heavy — does MongoDB prefer one?** Both work — and **sharded MongoDB scales writes**, too — but for *this* catalog the workload is read-heavy, which plays straight to MongoDB's secondary indexes:

```text
read-heavy / balanced     filter by field; query inside the record   → MongoDB ✓
fixed key · data ≫ RAM    recent-N by (channel, time); huge volume   → wide-column (Case 4)
```

A catalog's reads dominate, so MongoDB's secondary indexes earn their keep on every `price`/`category` filter, while the rare merchant edit is a cheap **partial update** of one field — no whole-document rewrite. MongoDB scales writes via sharding when you need it; you switch to a wide-column store when the access pattern collapses to **one fixed key at data ≫ RAM** and you want masterless, linear-scale appends instead of a primary-per-shard maintaining a B-tree on every write. **Rule of thumb: MongoDB when fields inside drive queries (read-heavy/balanced); wide-column when access is one fixed key at massive scale.**

*Takeaway: when records in one set have different fields AND your queries filter on those fields, the document model removes the "sparse columns vs join sprawl" dilemma — and it's the right call when reads dominate, not when writes become a firehose.*

### Case 4 — The message firehose

**Question: store every chat message ever sent — now trillions, arriving as a relentless write stream — and still serve "the most recent N in this channel" and scroll-back instantly. A second of staleness is fine. One machine can't hold it. Which store, and what's the key?**

The read shape is the clue — it's a contiguous slice, not a scan:

```text
partition key = (channel_id, bucket)      bucket = ~10-day window (keeps partitions small)
  inside a partition: message_id  ↓ newest-first
  "recent N" = read the first N rows of the partition — no scan, no sort
```

The answer is a <span style="color:#ffff99"><strong>wide-column store</strong></span>, and Discord's path is the canonical lesson: **MongoDB → Cassandra → ScyllaDB**. The deciding traits aren't write-dominance — Discord reported a roughly <span style="color:#8aff8a"><strong>50/50 read/write ratio with extremely random reads</strong></span> — they are **data far larger than RAM**, a **fixed key-scoped access pattern** (partition + time), **linear horizontal scale**, and **tunable consistency** (QUORUM). A wide-column store is built for exactly that: every node takes writes as cheap appends, and the partition key keeps the recent-N slice cheap no matter how large the dataset grows.

**Why not MongoDB — it's a document store too?** Because this workload doesn't use what a document DB is *for*, and its volume breaks what it's *good at*:

- **You never query by fields inside a message.** The only read is "recent N in this channel" — a partition + time lookup, not a filter on message fields. MongoDB's field-query / secondary-index strength sits unused; you'd pay for flexibility you don't need.
- **The volume outgrows a single primary, and the storage engines differ.** Cassandra/ScyllaDB are <span style="color:#ffff99"><strong>LSM / log-structured</strong></span> (the [LSM post](22-high-throughput-lsm-trees.md)) and **masterless** — every node accepts writes as cheap appends, no read-before-write — so write throughput scales linearly with nodes. MongoDB funnels writes through **one primary per shard** and maintains a B-tree index on every write: fine at moderate scale, costly when every node must absorb a share of a trillion-row stream.
- **Data ≫ RAM.** MongoDB is happiest when the working set (especially indexes) fits in memory — the exact wall Discord hit at ~100M messages, on a **single replica set**. (Sharded MongoDB pushes that wall out, but the fixed key-scoped access above already favors a wide-column store.) A wide-column store is built for data far larger than RAM, served straight off the partition key.

So the dividing line *inside* NoSQL:

| | <span style="color:#93c5fd"><strong>Document (MongoDB)</strong></span> | <span style="color:#ffff99"><strong>Wide-column (Cassandra/Scylla)</strong></span> |
| --- | --- | --- |
| **Pick when** | you query by varied fields inside the record | the access pattern is fixed and known: partition + clustering key |
| **Throughput** | <span style="color:#8aff8a"><strong>read-heavy</strong></span> or balanced, working set ~ RAM | **any read/write mix at huge scale**, linear horizontal scale |
| **Data size** | working set ~ fits RAM | data ≫ RAM |
| **Shape** | rich/nested docs, ad-hoc queries | time-series · events · messages |

Read vs write here: roughly **balanced** (~50/50), but both sides are **key-scoped** — writes are appends to a partition, reads are recent-N slices of one. Reads are random and spike on popular channels — a *read* hotspot Discord fixed with **request coalescing** (fold concurrent reads of one hot row into a single DB hit). That, plus <span style="color:#ff8a8a"><strong>hot partitions</strong></span>, **JVM GC pauses**, and compaction backlog at 177 Cassandra nodes, is why they moved to ScyllaDB (C++, no GC) — 177 → 72 nodes, tail latency collapsed. ([How Discord stores trillions of messages](https://discord.com/blog/how-discord-stores-trillions-of-messages))

*Takeaway: document store = query by fields, read-heavy/balanced, working set fits RAM; wide-column = fixed key-scoped access, data ≫ RAM, linear scale at **any** read/write mix (Discord's was ~50/50, not a write firehose). Messages are the second — keyed by `(entity, time-bucket)`, and operational reality (GC, compaction, hot partitions) can drive the engine as much as the data model.*

### Case 5 — The money ledger

**Question: money moves — a debit on one account, a matching credit on another. Both must land or neither must, every cent must reconcile, and a double-submit must never post twice. Volume is moderate; correctness is absolute. Which store?**

A <span style="color:#ffff99"><strong>relational ACID database</strong></span> — Postgres or MySQL/InnoDB, money modeled as an append-only **double-entry ledger** (at global scale, distributed SQL like Spanner — still relational, still ACID). Here the deciding dial isn't throughput at all — it's that correctness is *defined* across rows:

```text
BEGIN
  INSERT debit  (acct A, -100)
  INSERT credit (acct B, +100)   ← both, or neither
COMMIT
```

<span style="color:#ffff99"><strong>Atomicity</strong></span> makes the debit+credit one indivisible unit. <span style="color:#ffff99"><strong>Constraints encode the invariants in the engine</strong></span> — foreign keys, a `UNIQUE` idempotency key that turns a double-post into a database error rather than a duplicate charge (no matter which buggy service issued it), and a `CHECK (balance >= 0)` on a running-balance row updated *in the same transaction* (in an append-only ledger the balance is a **sum of rows**, not a column, so the rule lives on that maintained row). Under concurrency you escalate isolation (atomic `UPDATE` → `SELECT … FOR UPDATE` → `SERIALIZABLE`) to block <span style="color:#ff8a8a"><strong>lost updates and write-skew</strong></span>. The reason relational is the default isn't that NoSQL *can't* do transactions — DynamoDB and MongoDB both offer ACID transactions now — it's that a ledger **is** its constraints, joins, and audit trail, and an engine whose *default* posture is eventual consistency showing a wrong balance is the wrong fit. ([Postgres isolation](https://www.postgresql.org/docs/current/transaction-iso.html), [Square's ledger on Spanner](https://developer.squareup.com/blog/books-an-immutable-double-entry-accounting-database-service/))

*Takeaway: when money correctness is non-negotiable, make illegal states unrepresentable with ACID transactions + constraints. Some NoSQL stores now offer ACID too — but relational stays the default because a ledger's invariants **are** its constraints, joins, and audit trail.*

### Case 6 — The login check

**Question: every authenticated request must answer "is this token valid, and whose is it?" — a read on *every* page load — and idle sessions should vanish on their own. The value is a small opaque blob you only ever fetch whole by its id. Which store?**

A <span style="color:#8aff8a"><strong>key-value store</strong></span> — Redis (in-memory) or DynamoDB with TTL, key `session:{id}`. The access pattern is a pure point op (`GET`/`SET` by one id, never a join or field filter), so SQL's planner + index cost is pure overhead on the **hottest read path in the whole system**. This is brutally **read-heavy** — <span style="color:#8aff8a"><strong>every request reads the session</strong></span>, writes only happen at login/refresh — and an in-memory `GET` answers sub-millisecond where a disk-backed `SELECT` can't. The clincher is <span style="color:#93c5fd"><strong>native TTL</strong></span>: idle sessions expire on their own with no cleanup job. Two honest caveats: **validate the stored expiry timestamp on read** rather than trusting deletion — DynamoDB removes expired items only *eventually* (up to ~48h) — and a "sliding" session refreshes the TTL with an explicit **write** (`EXPIRE`/`GETEX`), not a plain `GET`. ([Redis session store](https://redis.io/docs/latest/develop/use-cases/session-store/nodejs/), [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html))

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

**Question: a game leaderboard takes thousands of score updates per second and must answer "top 10" and "what's my rank?" instantly. SQL can index `score` so the top-10 is cheap — but "what's *my* rank?" for an arbitrary player means counting everyone above them, and every score update re-maintains that index under brutal write contention. The data is small; the update rate is brutal. Which store?**

A <span style="color:#8aff8a"><strong>key-value store with a purpose-built structure</strong></span> — Redis **sorted sets**. The value here isn't an opaque blob; it's a structure that keeps members <span style="color:#ffff99"><strong>pre-sorted by score</strong></span>, so `ZADD`/`ZINCRBY` (update) and `ZREVRANGE 0 9` / `ZREVRANK` (top-N, rank) are `O(log N)` even with millions of players — where SQL serves the top-10 cheaply from an index but pays an **O(N) count** for an arbitrary player's *rank*, and re-maintains that index on every one of thousands of updates per second. It's a <span style="color:#ff8bd2"><strong>write-heavy</strong></span> workload, and `ZINCRBY`/`INCR` are **atomic without locks** — no transaction, no row contention. The same engine does rate limiting (fixed-window `INCR`+`EXPIRE`, or a sliding-window log in one Lua script), with TTL making per-window counters self-cleaning. ([Redis sorted sets](https://redis.io/docs/latest/develop/data-types/sorted-sets/))

*Takeaway: for ordered or atomic-counter ops at high write throughput, an in-memory structure turns an expensive query into an O(log N) primitive.*

---

## The whole framework on one page

Read it top to bottom and **take the first row that fits** — the signals are ordered by how decisive they are, so a true match high up wins even if a lower row also looks plausible. The right-hand column is where most wrong picks actually come from: a correct headline paired with a false reason.

| The decisive signal (take the first that fits) | Pick | Why it wins — and the trap to avoid |
| --- | --- | --- |
| **Correctness spans rows** — multi-row atomicity, constraints, joins, money must reconcile | **Relational** (Postgres/MySQL) — ledgers, inventory, orders, bookings | ACID + constraints enforce the invariant the instant a write commits. *Trap:* the ceiling is hot-row **write** contention, not reads; and "needs a transaction" alone doesn't forbid NoSQL — the **modeling** (joins, constraints, audit) is the real reason. |
| **Outgrew one SQL node** but you still need those transactions/joins | **Sharded SQL** — Instagram, Notion, Pinterest | Shard by the request-scoping entity so the common txn stays single-shard with full ACID; writes split across independent primaries. *Trap:* cross-shard queries become scatter-gather + app-side join — shard by the **wrong** key and every request fans out. |
| **Every request starts from a known key**; you fetch/replace the value whole; want TTL + horizontal scale | **Key-value** (DynamoDB/Redis) — sessions, cart, URL shortener | `hash(key)` routes to one node → predictable scale across **many** keys. *Trap:* one **hot** key still lands on one node — front it with a cache/CDN; hashing spreads many keys, never the reads of one. Don't trust physical TTL deletion for correctness — check the stored expiry. |
| **Ordering or counting at a high update rate** | **Redis structures** (sorted sets, counters) — leaderboards, rate limiters | Pre-sorted / atomic structures make rank, top-N, and increment `O(log N)` and lock-free. *Trap:* in-memory — durability (persistence/replication) is your job. |
| **Fields *inside* the record drive queries**; shapes differ; schema evolves | **Document** (MongoDB) — catalog, profiles, CMS | Secondary indexes on fields + flexible schema; partial updates avoid whole-doc rewrites. *Trap:* "read-heavy" is a **fit, not a limit** — sharded MongoDB scales writes; embed only bounded sub-data. |
| **Data ≫ RAM** with a fixed key-scoped access pattern (partition + time), at any read/write mix | **Wide-column** (Cassandra/ScyllaDB) — chat, feeds, events | Masterless LSM appends + a partition key make huge-scale writes *and* recent-N reads cheap regardless of ratio. *Trap:* it's **not** "write-heavy only" (Discord was ~50/50) — the driver is **scale + fixed access**, and you give up ad-hoc field queries. |

Notice that **the same checklist produced every row**, ordered by how decisive each signal is — **take the first that fits**: correctness across rows (→ relational, shard it if it outgrows a node), every request holds a key (→ key-value, or a Redis structure for ranking/counting), fields inside drive queries (→ document), or data ≫ RAM with fixed key-scoped access (→ wide-column). Size each against read **and** write throughput, and watch the traps in the right column — most wrong picks are a true headline with a false reason. That's the whole method.

> **Memory hook (recall):** *access pattern → keyed-or-relational-or-field-driven → consistency bar → read/write throughput → one node or sharded. Run those five questions in order and the storage choice falls out — every case above is just that checklist applied.*

---

With the storage-selection framework in hand, we now run it on the actual subject of this post — **recent searches**. Recall 1's funnel already handed us a starting verdict (key-value), but it warned that capping the list, the dual read/write load, and dedup would *add wrinkles*. The rest of this post is those wrinkles — and the punchline is that no single store wins. The real design is a **blend**, and the interesting part is watching *each decision get forced by the data*.

---

# Part 2 — The deep dive: designing recent searches

## Step 1 — Read the feature as numbers, not words

**Question: "show a user their last 10 searches" sounds like a toy. What in the *usage* makes it a real systems problem — and which numbers will end up dictating the architecture?**

Three facts about how people actually use the search box do all the work:

```text
behaviour                                    ──►  what it forces on the design
"tap the bar → see my last 10, newest first"      a tiny, ordered, per-user list
50% of users tap the bar within 5 seconds         READ-heavy + must be instant → preload
30% of all searches go THROUGH recent searches     WRITE-heavy + recent = high-intent data
```

Read the consequences, not the sentences. **50% tapping within 5 seconds** means the recent list is one of the most-read things in the product *and* the read can't wait on a database round-trip — by the time the finger lands, the data must already be sitting in memory. **30% of searches flowing through the recent list** means two things at once: every search is also a *write* to this feature (so write volume tracks total search volume — enormous), and the recent list is not a nicety, it's load-bearing — a third of all search traffic is people re-running a past query.

And keep three dimensions in your hand the whole way down, because every fork below resolves by appealing to one of them:

```text
BOUNDED data    — the thing the user sees is capped: exactly 10 items
UNBOUNDED data  — the thing we store grows forever: every query a user ever fired
TIME            — newest-first ordering, and "old searches stop mattering"
```

That split — *bounded what they see, unbounded what we keep* — is the seed of the entire design. The bounded view wants a tiny, hot, in-memory structure; the unbounded log wants a cheap, write-optimized, partitioned store; and **time** is what lets us throw most of the unbounded log away.

> **Memory hook:** *recent searches = bounded view (last 10, newest-first) over an unbounded log (every query ever). 50%-tap-in-5s ⇒ read-heavy + must preload; 30%-through-recent ⇒ write-heavy + high-intent. Every later decision is forced by bounded-vs-unbounded-vs-time.*

---

## The two endpoints — and how we'll trace them

**Question: before designing storage, what does the client actually call, and what must each call do?**

The whole feature is two endpoints — one read, one write:

```text
/search/recent   READ   "give me my last 10, newest-first"     ← fired on bar-tap (50% within 5s)
/search          WRITE  "run this query AND remember it"        ← every search (this IS the write)
```

The asymmetry shapes everything: `/search` is *both* a read (fetch results from Elasticsearch) *and* a write (record the query); `/search/recent` is a pure read of the bounded list. We'll trace both as the design grows — each major step ends with the **current flow**, so you watch the read and write paths thicken from a single naive DB call into the full preloaded, write-through system. Where it started, before any of our decisions:

```text
Flow v0 (naive — everything hits the DB):
  /search        → Elasticsearch (results) · write query to DB · return        ← also a write
  /search/recent → scan + sort the user's scattered records in the DB · return ← slow & hot
```

Both lines are problems we'll fix: the write path needs to keep the recent list warm, and the read path's scan is far too slow for a bar-tap. Hold this v0 in mind — every step below upgrades one of these two lines.

---

## Step 2 — Storage: what we store, and why not a relational DB

**Question: strip it to the storage layer. We are saving "every search query every user fires." Write down *that* access pattern — and does it want a relational database?**

The access pattern is blunt:

```text
recent-searches storage — access pattern
  what    every search query a user has ever fired
  volume  LARGE — tracks total search traffic across all users
  writes  HIGH ingestion — one append per search, never updated in place
  reads   always scoped to ONE user_id (never "search across users")
  shape   no relations, no joins, no cross-row invariant
```

No relations, no joins, no correctness-across-rows — Recall 2's entire case for SQL is absent. What *is* present is a firehose of appends keyed by `user_id`. Push that through the funnel and it never reaches the relational branch.

But "no joins" is the weak argument. The strong one is **what a relational write actually costs under this firehose.** A relational engine keeps its primary key (and every secondary index) in a <span style="color:#ffff99"><strong>B+ tree</strong></span>, and a B+ tree is a *read-optimized, in-place* structure. Every insert must find its leaf, and when a leaf fills it **splits and rebalances**, propagating up the tree — under sustained high-volume writes that's constant page splitting, lock contention on hot pages, and write amplification:

```text
high-volume appends into a B+ tree index
  insert → locate leaf → leaf full? → SPLIT → push key up → maybe split parent → ...
  every write pays tree-maintenance; hot pages serialize; throughput caps out
```

That is exactly the wall the [LSM-tree post](22-high-throughput-lsm-trees.md) was built to break. A high-ingestion log wants a **log-structured / LSM** engine — append to a memtable, flush sorted files, merge in the background — where a write is a cheap in-memory insert and the disk only sees sequential batches, *no rebalancing on the write path*. That's the storage engine inside Cassandra, and it's why the right home here is a **partitioned, write-optimized NoSQL store** — MongoDB, Cassandra, or Elasticsearch — sharded by `user_id` so write capacity scales by adding nodes.

```text
high write ingestion + no relations + per-user access
        └─►  partitioned NoSQL (sharded by user_id), LSM-backed
             scale writes by adding shards — not a single relational primary
```

> **Memory hook:** *storing every query = large volume, high write ingestion, no relations, per-user reads → partitioned NoSQL, not relational. The deciding cost isn't "no joins," it's the B+ tree: in-place index splits/rebalance on every write. A high-ingestion log wants LSM (Cassandra), which appends without rebalancing.*

---

## Step 3 — The document-modeling fork: one fat doc, or one record per query?

**Question: we picked a partitioned NoSQL store. Now the modeling choice that quietly decides everything — do we keep *one document per user* holding all their queries, or *one record per query*?**

<img src="../assets/information-retrieval-recent-searches/storage-modeling-fork.svg" alt="Two side-by-side options for modeling a user's search queries in a NoSQL store. LEFT (red, rejected): 'Option A, one document per user' — a single document { uid: u1, queries: [q1, q2, q3, ... q100000 ...] } with a red brace 'UNBOUNDED, a heavy user fires 100k+ queries, the doc grows forever' and a red cost box: every append rewrites a huge doc; reading the last 10 means loading the WHOLE doc into memory, sorting, and slicing, which is heavy disk and network I/O; and you eventually hit the document-size limit. Marked with a red cross. RIGHT (green, chosen): 'Option B, one record per query' — small independent records { uid: u1, q: sachin, ts }, { uid: u2, q: sehwag, ts }, { uid: u1, q: laxman, ts }, each bounded and tiny so an append never touches old records. Marked with a green check. A green honest-cost note: a user's records now scatter across the partition, so 'give me the last 10' becomes a scan plus sort, a read cost fixed with a cache later. BOTTOM rule bar (yellow): decide the document shape from whether it is BOUNDED; unbounded history means one record per query, never one fat doc per user." width="1080">

The two options look almost equivalent until you ask the bounded-vs-unbounded question from Step 1:

```text
Option A — one doc per user        Option B — one record per query
{ uid: u1, queries:[ q1 … q100k ] }   { uid:u1, q:"sachin" }
                                      { uid:u2, q:"sehwag" }
  ONE doc, grows FOREVER (unbounded)  { uid:u1, q:"laxman" }
                                        many small docs, each BOUNDED
```

Option A is a trap, and it's worth naming *why* precisely, because the same trap recurs across NoSQL modeling. A user's query history is **unbounded** — a power user fires tens of thousands of searches. If they all live in one document, then:

- Every write **rewrites a growing document** — append `q100001` and the engine rewrites the whole blob.
- Every read of "last 10" must **load the entire document into memory, sort, and slice** — pulling 100k queries off disk and over the network to return 10. Disk I/O and network I/O scale with the user's *whole history*, to answer a question about its *tail*.
- You eventually hit the engine's **document-size limit** and have to re-shard the document itself.

So we take **Option B — one record per query.** Each record is tiny and bounded; an append never touches old data (perfect for the LSM write path). The rule generalizes:

> **Decision rule — model from the bound.** Before choosing one-doc vs many-docs, ask: *is the embedded collection bounded?* Bounded sub-data (a user's 5 addresses) → embed in one doc. **Unbounded** sub-data (a user's search log, a channel's messages, an account's transactions) → one record each, *referenced* not embedded. This is the same "embed only bounded sub-data" caution from Recall 5's catalog case.

There's an honest cost to Option B, and we'll spend the next few sections paying it: once each query is its own record, **a single user's records scatter across the partition** (interleaved with everyone else's, ordered by write time, not by user). So "give me u1's last 10" is no longer a single document fetch — it's a **scan + filter + sort**, an expensive `LIST` operation. We chose the shape that's cheap to *write* and accepted a shape that's expensive to *read*. Holding that tension is the rest of the post.

> **Memory hook:** *unbounded history ⇒ one record per query, never one fat per-user doc (which rewrites-on-append and loads-everything-to-read-ten). The rule: embed only bounded sub-data; reference unbounded. The cost we now owe: per-user records scatter, so "last 10" becomes a scan+sort — a read problem we solve with a cache.*

---

## Step 4 — The write path, and the sync-vs-async decision

**Question: zoom out to where this store sits. A user fires a search; the actual matching documents live in Elasticsearch. Where does recording the *query* fit — and should that write be synchronous or asynchronous?**

First, the surrounding flow, because recent searches is a *side-effect* of normal search:

```text
user ──/search "sachin"──► Search API ──► Elasticsearch ──► matching docs ──► user
                               │
                               └──► persist the QUERY into our partitioned NoSQL store
```

The search API already calls Elasticsearch to get relevant documents and return them. Recording the query for "recent searches" is an extra write hanging off that same request. So: sync or async?

<img src="../assets/information-retrieval-recent-searches/sync-vs-async-write.svg" alt="The search write path and the synchronous-versus-asynchronous decision. Top row: a stick-figure user sends /search (pink) to a stacked, horizontally-scaled Search API box; the API has a double-headed arrow down to a small Elasticsearch cylinder labelled 'actual search documents (relevance)', and an arrow right toward a cluster of partitioned NoSQL cylinders labelled 'Sharded NoSQL, MongoDB / Cassandra, all queries, unbounded'. Middle: two lanes from API to DB. Lane 1 (pink, SYNC): a direct arrow API to DB with a caption — write straight to DB then respond; delay query-to-persisted is about zero; simplest; minus, the DB must absorb the write load on the hottest API. Lane 2 (blue, ASYNC): API to a Kafka/SQS queue pill to DB, caption — absorbs spikes, decouples, great for fan-out/extensibility; minus, extra hop plus the record is visible later (lag). Bottom yellow DECISION badge: the DB is a sharded NoSQL store built for high write throughput, so start SYNC (add shards if writes slow) and introduce the queue only when measured write latency degrades; don't pre-build async you don't need; a green check sits next to 'start SYNC'." width="1080">

Lay the two options side by side and make the trade explicit:

```text
DECISION — should API → DB be SYNC or ASYNC?

SYNC   API writes to DB, then responds        ASYNC  API → Kafka/SQS → DB (a worker drains it)
  + delay(query → persisted) ≈ 0                + absorbs traffic spikes, decouples producer/consumer
  + simplest path, fewest moving parts          + the queue is a natural fan-out point (extensibility)
  − the DB must absorb writes on the            − extra hop + the record is visible LATER (queue lag)
    single most-hit API in the product          − more infra to run and reason about
```

Here's the reasoning that picks it, and it's a reusable one. People reach for a queue reflexively under "high write volume" — but a queue's job is to *protect a downstream that can't keep up*. Our downstream is a **sharded NoSQL store specifically chosen for high write throughput**; if it's slow, the native fix is **more shards**, not a buffer in front of it. Meanwhile the cost of async is real: the recent list should reflect a search *immediately*, and a queue inserts lag between "user searched" and "it's persisted." So:

> **Call: start synchronous.** The persistence delay should be ~zero, and the DB is built to take the load. Add the queue **only if you measure write latency degrading** — at which point `API → Kafka → DB` buys you spike absorption and a fan-out seam for free. Don't pre-build async you can't yet justify; *but know exactly where it slots in when the metric tells you to.*

That's the general template: **don't add a queue to "handle load" if the downstream already handles load — add it when measurement, or a need to fan out, demands it.**

```text
Flow update — WRITE path (/search) now persists synchronously:
  /search → Elasticsearch (docs) · persist {uid,q} to NoSQL (SYNC) · return
  /search/recent → still a DB scan+sort   ← unsolved; Steps 5–7 rebuild this line
```

> **Memory hook:** *search → Elasticsearch for docs, and the query is also persisted to our store. Sync vs async: a queue protects a downstream that can't keep up — ours (sharded NoSQL) can, so start SYNC (delay≈0); add `API→Kafka→DB` only when measured latency degrades or you need fan-out. Don't pre-build async.*

---

## Step 5 — The read trap: a simple cache that's stale before it's warm

**Question: now the read. "Last 10, newest-first," demanded by 50% of users within 5 seconds, on top of a write firehose. The reflex is "put Redis in front of the DB." Walk that through carefully — does a normal read-through cache actually work here?**

Recall the cost we deferred in Step 3: a user's records are scattered, so reading "last 10" from the partitioned DB is a scan+sort — too slow and too hot to do on every bar-tap. The textbook fix is a **read-through (cache-aside)** cache. Let's actually trace it:

<img src="../assets/information-retrieval-recent-searches/cache-stale-vs-writethrough.svg" alt="Two panels contrasting a failing naive cache with the write-through fix. LEFT panel (red) titled 'Naive read-through cache (cache-aside)': a user reads /recent from a Redis cache; on a miss it loads from the persistent DB and populates the cache. Separately, when the user makes a new search, the write goes straight to the persistent DB, bypassing the cache. Numbered steps: 1, read /recent → cache MISS → load from DB → populate cache; 2, user searches 'x' → writes to DB directly; 3, the cache is now STALE the instant step 2 happens, shown by a red STALE stamp on the Redis box. Red caption: high-intent users search constantly, so the list is invalidated almost every time it is read, reuse approaches zero, cache thrash, near-zero hit rate, the cache barely helps. RIGHT panel (green) titled 'Fix, write-through (cache-first)': on every search the Search service writes to Redis FIRST, shown as LPUSH uid q and LTRIM uid 0 9, then writes to the persistent DB. Reading /recent is served straight from Redis and is always fresh. The Redis value is a bounded list uid → [q1, q2, q3, ... q10] labelled BOUNDED to 10. Green caption: the same action that would stale the cache now updates it, so it is never stale; reads are O(1) from RAM with no DB hit. Bottom yellow rule bar: a cache pays off only when reuse-interval is much greater than change-interval; when writes invalidate before the next read, don't cache-then-invalidate, write THROUGH the cache so writes keep it warm." width="1080">

```text
NAIVE read-through (cache-aside)
  read  /recent(u1):  cache MISS → scan DB → populate cache[u1] → return
  later  u1 searches "x":  write goes straight to the DB
         └─►  cache[u1] is now STALE — it still shows the pre-"x" list
```

Look at what just happened. The **write path and the cache don't talk.** Every new search writes the DB directly, which **invalidates the cached list immediately.** And who searches the most? The high-intent users — exactly the ones hammering the recent-searches feature. So the cached list for an active user is invalidated *almost as often as it's read.* The cache is **stale before it's warm.**

This is the failure mode worth burning into memory, because it's not specific to search — it's a general law:

> **A cache only pays off when a cached value is *reused many times between changes*.** Formally, the win scales with the **reuse ratio** = reads-per-change on a key. When writes invalidate a key faster than reads reuse it, you get [**cache thrashing**](https://aws.amazon.com/builders-library/caching-challenges-and-strategies/): a near-zero hit rate, every "hit" is actually a miss + recompute, and you've *added* a layer (and a consistency headache) while *subtracting* performance.

### Where this exact trap shows up (so you recognize it next time)

The "data changes as fast as it's read" pattern recurs all over systems design. A plain read-through cache is the **wrong reflex** for all of these:

```text
high-churn / hot-write data where naive caching thrashes
  • live counters        likes, view counts, vote tallies, reaction counts
  • real-time prices     stock/crypto tickers, ride-surge fares, ad-bid prices
  • inventory on sale     a flash-sale SKU's stock — one hot key, decremented constantly
  • presence / status     "last seen", typing indicators, online/offline
  • leaderboards          rank updated on every score event
  • rate-limit counters    sliding-window quota counts, refreshed every request
  • a hyperactive feed     the head of a very active user's timeline
  • per-request-unique     results unique to each request → ~0 reuse, cache "does no good"
```

The last one is the limiting case [AWS calls out](https://aws.amazon.com/builders-library/caching-challenges-and-strategies/): if every request is unique, hit rate is ~0 and the cache is pure overhead. The counters/prices/inventory cases are the [**hot-key** problem](https://blog.bytebytego.com/p/a-crash-course-in-caching-final-part) — one key both read and written thousands of times a second, where naive invalidation just melts the node. Recent searches sits squarely in this family: **the user who reads the list most is the user who invalidates it most.**

And the tempting "fixes" that *don't* work here:

- **Replicate the DB into Redis** (mirror Mongo → Redis): you'd hold every user's entire unbounded history in RAM — the [memory wall](22-high-throughput-lsm-trees.md) we spent all of Step 2 escaping. RAM cost is absurd; rejected.
- **Swap Cassandra for Elasticsearch** because ES is read-friendly: ES *is* nicer for reads, but it's **not a high-write-ingestion** store — you'd just move the pain from the read side to the write side. Rejected.

So: don't reach for the cache *pattern*; fix the *interaction*. That's the next section.

> **Memory hook:** *a read-through cache fails when writes invalidate a key faster than reads reuse it — cache thrash, ~0 hit rate. The reuse ratio (reads-per-change) decides if caching helps. Same trap: counters, live prices, sale inventory, presence, leaderboards, rate-limits, hot keys. The heaviest reader of recent searches is also its heaviest writer.*

---

## Step 6 — The flip: write *through* the cache (cache-first)

**Question: the problem is that writes go around the cache and stale it. What if we made the write go *through* the cache instead — so the very action that would invalidate it is the action that updates it?**

The fix is to **invert the order**. Right now a search updates the DB and the cache only finds out *later* (and wrongly). Instead, make updating the cache *part of* the search itself: on every search we touch **Redis first**, then the durable DB.

But to read that write, you need the two tiny Redis commands it uses. They look cryptic; they're not.

### The two Redis commands, in plain English

A **Redis list** is just an ordered row of items with two ends — a **head** (the left end) and a **tail** (the right end). Recent searches wants *newest first*, so we always add at the head and keep the row short. Two commands do exactly that:

- **`LPUSH key value`** — *"**L**eft **PUSH**."* Put `value` on the **head** (left end). The newest search becomes item #1; everything else slides one spot to the right. *(Redis also has `RPUSH`, which adds on the right — we don't use it, because we always want newest-on-the-left.)*
- **`LTRIM key 0 9`** — *"keep only positions **0 through 9**"* (the first ten) and throw the rest away. Run it right after every `LPUSH`, and the list can **never** grow past 10.

<img src="../assets/information-retrieval-recent-searches/lpush-ltrim.svg" alt="A two-row diagram showing how two Redis commands maintain the recent:u1 list. Row 1, LPUSH recent:u1 'sachin' — described as 'left push', putting the value on the HEAD (left end) so the newest becomes number 1 and the others shift right; a green 'sachin' box sits at the head of a row of grey boxes laxman, dravid, kohli, and an ellipsis. Row 2, LTRIM recent:u1 0 9 — keep positions 0 to 9 (the first ten), and anything past number 10 is dropped; a row of boxes ends with a red dashed '11th' box past a dashed 'cut after #10' line, labelled dropped. A bottom strip reads: result — a fixed-size list of the 10 newest searches, fetched whole, no scan, no sort." width="1040">

That's the entire mechanism: `LPUSH` puts the newest search on top, `LTRIM` lops off anything past the tenth. **Newest-first and capped-at-ten, automatically, on every write.**

### The write, cache-first

Now the write-through itself. On each search the Search service runs, in order:

```text
on search(u1, "sachin"):
  ① LPUSH recent:u1 "sachin"   # newest onto the head  (Redis)
  ② LTRIM recent:u1 0 9        # keep only the 10 newest (Redis)
  ③ persist (u1,"sachin",ts)   # durable history (NoSQL store)
```

Steps ① and ② hit **Redis first**; step ③ writes the durable log. Reading is now trivial: `/search/recent` returns `recent:u1` straight from Redis — O(1), in RAM, already newest-first.

### Why the staleness is gone — by construction

The staleness is gone *by construction*: the search that used to **invalidate** `recent:u1` is now the search that **refreshes** it. There's no window where the cache disagrees with reality, because the cache is updated on the **write path**, not lazily rebuilt on the read path.

And notice what Redis holds — this is where the **bounded** half of Step 1 finally pays off. A Redis **list per user, capped at 10** by `LTRIM`, is the *bounded document the DB could never be* (Step 3): tiny, fixed-size, identical on every device, and read with zero processing — no scan, no sort, no "load 100k to return 10." It's a textbook key-value win (Recall 4): a small structure fetched whole by a key you already hold (`user_id`).

### The cost, and why we pay it

The trade is honest and worth stating: **we now do two writes per search** (cache + DB) instead of one. We accept it, because the read side is the one under 50%-in-5-seconds pressure, and a second write is cheap insurance against a perpetually-cold cache. (The DB write can still be made async later per Step 4; the *cache* write stays synchronous and first.)

This is the resolution of Recall 1's promise: the funnel said "key-value," and indeed the **serving** layer is key-value (Redis) — but it sits *in front of* a partitioned NoSQL **log** (durability + history) and *beside* Elasticsearch (the actual search). One feature, three stores, each doing the one thing it's best at.

**The write path, complete.** With write-through in place, `/search` does five ordered things — and step 3 (cache-first) is the whole trick:

<img src="../assets/information-retrieval-recent-searches/write-path-flow.svg" alt="The complete write path for the /search endpoint as an architecture diagram. A stick-figure user on the left fires /search 'sachin' (step 1, pink) into a stacked Search API box. The API calls Elasticsearch below it (step 2, blue, two-way) to get matching documents. The API then does a cache-first write to a Redis cylinder top-right (step 3, pink) running LPUSH plus LTRIM bounded to 10, holding recent:u1 as a list of max 10. The API also persists to a sharded NoSQL MongoDB cluster on the right (step 4, pink) with persist {u1,'sachin',ts}. A green arrow returns results to the user (step 5). A bottom strip reads: the write that would STALE the cache now REFRESHES it, so it is never stale." width="1080">

On a warm key the read is now instant — but a *cold* key (eviction, new device, first open of the session) still falls through to the slow DB scan from Step 3. That lingering cold path is exactly what the next step kills.

> **Memory hook:** *flip the order — write to Redis FIRST (`LPUSH` puts the newest on the head, `LTRIM 0 9` caps it at 10), then the DB. The search that would stale the cache now refreshes it, so it's never stale. Redis holds a per-user list bounded to 10 — the bounded doc the DB couldn't be; reads are O(1), no processing, same across devices. Cost: two writes per search. Accept it.*

---

## Step 7 — Preload: the 50%-tap-in-5-seconds problem

**Question: write-through keeps the cache *fresh*, but not necessarily *present*. A user opens the app on a new device, or their key was evicted — `recent:u1` is empty. They tap the bar within 5 seconds and we'd have to hit the slow DB scan. How do we make sure the list is already in RAM before the tap?**

Write-through only warms the cache *after a user searches*. But the killer stat is that 50% of users tap the bar **before** they've searched this session — on app-open. For them the cache may be cold (eviction, first login, new device), and a cold tap falls through to the expensive per-user DB scan from Step 3. That's the poor-UX path we must avoid.

The fix is to stop waiting for the tap and **pre-warm** on a signal that *precedes* it — opening the app. Here's the whole read-path story in one picture — the *same* `/search/recent` endpoint across all three stages, from the naive scan to the preloaded hit:

<img src="../assets/information-retrieval-recent-searches/read-path-flow.svg" alt="How the /search/recent read path got fast, shown as three stacked stage bands for the same endpoint. Stage v0 NAIVE (red): user calls /search/recent into the Search API, which scans, filters, and sorts the user's scattered records in a sharded NoSQL cylinder and returns them — tagged slow and hot, runs on every tap (50% within 5 seconds), marked with a red cross. Stage v1 WRITE-THROUGH CACHE (amber): user calls /search/recent, Search API does GET recent:u1 on a Redis cylinder; a green HIT branch returns the 10 from RAM instantly (checked), but a red dashed MISS branch falls through to scan the NoSQL store, populate recent:u1, and return, tagged 'cold start still slow'. Stage v2 PRELOADED (green): before the tap, an app-open emits an ON-LOGIN/APP_OPEN event through a queue into a Cache Warmer, which scans the NoSQL store once and populates recent:u1 in Redis ahead of time; then the actual tap — /search/recent → Search API → Redis GET recent:u1 → HIT → return — touches no database at all. A bottom takeaway strip reads: moved the unavoidable DB scan OFF the tap (critical path) ONTO app-open (a predictable earlier signal)." width="1080">

A **Cache Warmer** service consumes `ON-LOGIN`/`APP_OPEN` events, does the one expensive DB scan *proactively*, and loads the user's recent-10 into Redis. By the time the finger reaches the bar, `/search/recent` is a pure in-memory read. We've moved the unavoidable slow scan **off the critical path** (the tap) and onto a **predictable earlier trigger** (the open) — the same "do the expensive work when you can see it coming" idea as the [LSM capacity-planning / pre-provisioning](22-high-throughput-lsm-trees.md) discussion, and a close cousin of read-ahead.

This is a genuinely reusable move: **when a high-value read is preceded by a reliable earlier signal, prefetch on the signal instead of serving on demand.** App-open → warm the search list. Page-load → warm the next paginated chunk. Add-to-cart → warm checkout data.

> **Memory hook:** *write-through keeps the cache fresh but not present (eviction, new device, first open). Since 50% tap within 5s of opening, prefetch: `ON-LOGIN/APP_OPEN` → queue → Cache Warmer → load recent-10 into Redis before the tap. Move the unavoidable DB scan off the critical path (tap) onto an earlier predictable signal (open).*

---

## Step 8 — Leverage the 30%: cache the *results*, not just the queries

**Question: 30% of all searches are people tapping a recent query. That's the most *predictable* search traffic in the system — we know the exact queries before they're run. Can we exploit that beyond just showing the list?**

When a user taps "sachin" from their recent list, we already know they're about to re-run that exact search against Elasticsearch. And [Elasticsearch is comparatively slow for complex queries](https://www.elastic.co/). Since these queries are *known in advance and re-run often*, they're ideal to **precompute**:

```text
for each user's top recent queries:
  precompute the search result  ──►  cache it in Redis (a SECOND cache: query → results)
tap a recent query  ──►  serve precomputed results from Redis, skip the Elasticsearch round-trip
```

This is a *different* cache from Step 6's recent-list cache, and it has a healthy reuse ratio — a popular recent query is read many times before its results change, so caching genuinely pays here (the opposite of Step 5's thrash). Two cautions keep it sane:

- **Bound what you store.** Caching full result payloads for every query is huge and redundant. Cache results only for the **top-N recent queries per user** (or globally hot queries), and dedup identical queries across users where possible.
- **Pick the right structure.** Redis is reasonable for modest precomputed result sets; if results are large or need ranking/feature-rich serving, a purpose-built read store may fit better. The principle is what matters: *predictable, repeated, expensive reads are worth precomputing.*

```text
Flow — tapping a recent query (the 30% path), /search "sachin":
  1 Redis results-cache GET results:"sachin"
       HIT  → return precomputed results            ← skips Elasticsearch entirely
       MISS → Elasticsearch → results → populate results-cache → return
  2 write-through still runs: LPUSH/LTRIM recent:uid · persist {uid,q} to NoSQL
```

> **Memory hook:** *30% of searches are taps on known recent queries — the most predictable traffic there is. Precompute their Elasticsearch results into a second Redis cache (query → results), so a tap skips the slow ES round-trip. This cache has high reuse (unlike the recent-list trap), so it actually pays. Bound it to top-N and dedup.*

---

## Step 9 — Cost optimization: bound the hot store, archive the cold log

**Question: Redis RAM and a forever-growing Mongo log are both expensive. Where's the waste, and what does **time** (our third dimension) let us throw away?**

Two cuts, both falling straight out of bounded-vs-unbounded-vs-time:

**1. Redis stores only the bounded view, never the unbounded log.** Redis holds the recent **10** per user (`LTRIM`), full stop — *not* a user's full history. RAM is the most expensive tier; spend it only on the tiny bounded thing that must be instant. The unbounded log lives on cheap disk in the partitioned DB.

**2. Archive cold history out of the hot DB.** A user's searches from a year ago are almost never read. Keeping them in the live, sharded, write-hot Mongo cluster is paying SSD-and-IOPS prices for data nobody touches. So run **ETL jobs (Spark)** that sweep records **older than 6 months** out to **cold object storage (S3)**:

```text
MongoDB (hot, sharded)  ──ETL: records older than 6 months──►  Spark jobs  ──►  S3 [cold storage]
  keeps the working set small, the indexes small, compaction cheap, $$ down
```

This shrinks the hot dataset (smaller indexes, cheaper compaction, less RAM pressure — the [LSM working-set](22-high-throughput-lsm-trees.md) argument again) and moves rarely-read bytes to storage that costs a fraction as much. The honest part: it's ultimately a **product call** — do you refuse to show searches older than 6 months at all, or serve them with *higher latency* from cold storage on the rare request? Either is fine; the engineering just makes the cheap-by-default path the common one.

> **Memory hook:** *time lets you discard. Redis keeps only the bounded last-10 (never the full log) — RAM is dearest, spend it on the hot tiny thing. ETL (Spark) sweeps DB records older than 6 months to S3 cold storage: smaller hot working set, smaller indexes, big $ savings. Whether old searches show at all (or just slower) is a product call.*

---

## The whole design on one page

We started from "show the last 10 searches" and let the **data** force every decision: bounded view vs unbounded log vs time. No single store won — the right answer is a **blend**, each component doing its one best thing.

<img src="../assets/information-retrieval-recent-searches/recent-searches-architecture.svg" alt="The complete recent-searches architecture. On the left, users send two requests to a central Search service: /search/recent (green, a read) and /search (pink, a write). Below the Search box sit two small attached cylinders: ES (Elasticsearch, the actual search documents) and Cache (Redis, precomputed search results for queries tapped from the recent list). Top center-right is a Redis Cluster cylinder (with a red dollar-sign cost marker) holding u1 → [q1, q2, ... q10], the recent-10 per user, bounded; the Search service write-throughs to it (pink) and reads recent from it (green/yellow). On the right, a cluster of MongoDB cylinders labelled Sharded NoSQL, all queries, unbounded (with a red dollar-sign); the Search service persists every query there (pink). Lower-left, the user path taps an Events box emitting yellow ON-LOGIN / APP_OPEN events into a queue pill, feeding a stacked Cache Warmer service; the Cache Warmer reads recent-10 from MongoDB and writes them up into the Redis Cluster to preload before the user taps. Bottom right, from MongoDB a blue ETL Jobs arrow labelled 'data older than 6 months' feeds three Spark boxes that write into an S3 cold-storage cloud. A legend maps pink to write, green to read, yellow to event/storage, blue to async/ETL, and red dollar-signs to cost." width="1180">

### The cast — each box, one job

Before tracing a request, name every box in the diagram and the **single** job it owns. If a component is doing two jobs, that's usually a smell — here each does exactly one thing, and that's *why* the design is legible:

| Component | What it holds | Its one job |
| --- | --- | --- |
| <span style="color:#cbd5e1"><strong>Search service</strong></span> | nothing (stateless) | the front door — fan each request out to the right store |
| <span style="color:#93c5fd"><strong>Elasticsearch</strong></span> | the searchable documents | answer the *actual* search — relevance ranking for `/search` |
| <span style="color:#b79bff"><strong>Redis · recent list</strong></span> | `recent:u → [q1…q10]` | serve `/search/recent` in O(1) — bounded to 10, write-through |
| <span style="color:#b79bff"><strong>Redis · results cache</strong></span> | `results:"q" → page` | serve the predictable 30% re-runs without re-hitting ES |
| <span style="color:#ffff99"><strong>Partitioned NoSQL</strong></span> | every query ever, sharded by `user_id` | the durable, **unbounded** log — source of truth & history |
| <span style="color:#93c5fd"><strong>Cache Warmer</strong></span> | nothing (stateless) | on app-open, preload `recent:u` into Redis *before* the tap |
| <span style="color:#93c5fd"><strong>ETL → S3</strong></span> | the cold archive | move >6-month data off the hot store to cut cost |

Notice Redis appears **twice** — keep them separate in your head: one is the *bounded recent-list* (write path), the other is the *results cache* (the 30% optimization). Same engine, two unrelated jobs.

### Follow one write, end to end — `/search "sachin"` (user u1)

The Search service does four things, in order. Step 2 (cache-first) is the load-bearing one:

1. <span style="color:#93c5fd"><strong>Elasticsearch</strong></span> ranks documents for `"sachin"` → the result page the user actually sees.
2. <span style="color:#b79bff"><strong>Redis recent-list, cache-first:</strong></span> `LPUSH recent:u1 "sachin"` then `LTRIM recent:u1 0 9`. The write that *would* have staled the list is the write that **refreshes** it — so the list is never stale.
3. <span style="color:#ffff99"><strong>NoSQL log:</strong></span> persist `{u1,"sachin",ts}` — durable history (sync now, async-able under Step 4's rule).
4. <span style="color:#8aff8a"><strong>Return</strong></span> the ranked page. *Shortcut:* if `"sachin"` was **tapped from the recent list** (not freshly typed), its page is already in the results cache — serve that and skip step 1 entirely.

### Follow one read, end to end — `/search/recent` (user u1)

The read is engineered to **never touch a database** on the happy path:

- **Step 0 — before the tap (the whole trick):** app-open fires an <span style="color:#ffff99">`ON-LOGIN/APP_OPEN`</span> event → <span style="color:#93c5fd">Cache Warmer</span> scans NoSQL once → preloads `recent:u1` into Redis.
- **Step 1 — the tap:** `GET recent:u1`
  - <span style="color:#8aff8a"><strong>HIT</strong></span> (the designed-for path) → return the 10 straight from RAM, **no DB**.
  - <span style="color:#ff8a8a"><strong>MISS</strong></span> (warmer hasn't run — new device, eviction) → scan NoSQL, populate Redis, return. Slow, but rare and self-healing.

### What runs in the background

- <span style="color:#93c5fd"><strong>Cache Warmer</strong></span> turns the one unavoidable scan into a *pre*-fetch on a predictable signal (open), lifted **off** the critical path (tap).
- <span style="color:#93c5fd"><strong>ETL (Spark) → S3</strong></span> sweeps records older than 6 months out of the hot NoSQL store into cheap cold storage. Redis only ever holds the bounded 10, so every expensive tier stays small.

The decision trail, top to bottom — and notice every row is settled by appealing to the data, not to taste:

| Decision | What forced it | Choice |
| --- | --- | --- |
| Storage engine | high write ingestion, no relations, per-user reads | <span style="color:#ffff99"><strong>Partitioned NoSQL</strong></span> (Mongo/Cassandra), LSM-backed, sharded by `user_id` |
| Document shape | history is **unbounded** | <span style="color:#8aff8a"><strong>One record per query</strong></span>, not one fat per-user doc |
| Persist sync/async | downstream already absorbs load; delay must be ~0 | <span style="color:#ff8bd2"><strong>Sync first</strong></span>; add `API→Kafka→DB` only on measured pain |
| Serve the read | per-user scan too slow; naive cache **thrashes** | <span style="color:#8aff8a"><strong>Write-through Redis</strong></span> list, bounded to 10 (`LPUSH`+`LTRIM`) |
| Cold start (50%/5s) | reads precede the search; cache may be empty | <span style="color:#ffff99"><strong>Cache Warmer</strong></span> on `APP_OPEN` events — prefetch off the critical path |
| The 30% re-runs | those queries are known in advance | <span style="color:#8aff8a"><strong>Precompute results</strong></span> into a second (high-reuse) cache |
| Cost | **time**: old searches rarely read | <span style="color:#93c5fd"><strong>ETL → S3</strong></span> for >6-month data; Redis holds only the bounded 10 |

The throughline, and the thing to take to your next design: **a "simple" feature became a real system the moment we read its access pattern as numbers.** The bounded view wanted an in-memory key-value list; the unbounded log wanted a write-optimized partitioned store; and *time* let us archive most of it away. The one decision most people get wrong — reaching for a plain read-through cache — fails here for a precise, reusable reason: **when the heaviest reader is also the heaviest writer, you write *through* the cache, you don't invalidate it.**

> **Memory hook (recall):** *recent searches = three stores, one per job — Elasticsearch (search), partitioned NoSQL (durable unbounded log, sharded by user), Redis (bounded write-through hot list). Persist sync first; preload on app-open; precompute the predictable 30%; archive >6-month data to S3. Every choice was forced by bounded-vs-unbounded-vs-time — and the cache must be write-through, because its busiest reader is its busiest writer.*
