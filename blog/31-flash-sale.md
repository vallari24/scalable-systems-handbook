# Designing a Flash Sale: Selling Exactly 10,000 Items to a Million Buyers at Once

This post builds a **flash sale** from first principles: the machinery behind a Shopify drop, a BookMyShow ticket release, or an IRCTC tatkal window — a *fixed* pile of inventory, a *huge* crowd that all arrives in the same few seconds, and a hard promise that you sell **exactly what you have**: not one item more, not one item fewer than you can. We start from the dumbest correct thing (`UPDATE inventory SET quantity = quantity - 1`) and grow each component only when a named bottleneck appears: a single counter that melts under contention, a table of *one row per unit* that spreads the heat, a `FOR UPDATE SKIP LOCKED` claim that lets thousands grab in parallel without overselling, a payment phase deliberately cut *out* of the grab transaction, a cron reaper for abandoned carts, and — for seats — a Redis variant that does the whole grab as one atomic operation.

**Question: you have 10,000 phones to sell, and at 12:00:00 sharp a million people press "Buy." You must never sell the 10,001st phone, you must not leave phones unsold because buyers are slow to pay, and you cannot let a million writes pile onto one database row. What is the smallest design that is correct on day zero — and what is the *next* thing that breaks every time the crowd 10×s?** The honest path runs straight through one hot counter, a table split into 10,000 claimable rows, a row-locking grab that thousands run at once, a payment step that is pointedly *not* part of the grab, and a reaper that hands abandoned carts back — and by the end you've hand-built the engine under every flash sale, ticket release, and seat-booking window you've ever raced through.

It comes in two parts. **[Part 1](#section-1--day-zero-one-counter-decremented-in-a-transaction): the storage and contention side** — how you hold fixed inventory and let a stampede claim it correctly. **[Part 2](#part-2--the-operational-side-surviving-the-stampede): the operational side** — pods, the edge throttle, and the knobs you turn on the day of the sale.

It leans on three earlier posts. We built an [airline check-in system](03-database-airline-checkin-transactions-indexes-locks.md) and learned how transactions, indexes, and row locks behave — here the lock *is* the design. We built a [YouTube view counter](30-youtube-views-counter.md) and met the **hot row** — millions of writes serializing behind one row's lock — which is exactly the villain here, seen from the other side. And we built a [distributed task scheduler](29-distributed-task-scheduler.md) whose pullers claim jobs with `FOR UPDATE SKIP LOCKED` — the same one-line trick that makes a flash sale work.

> **Memory hook:** *fixed inventory + contention = locking. The whole design is one move: stop decrementing a single shared counter, and instead let each buyer atomically claim one of N distinct rows with `FOR UPDATE SKIP LOCKED` — so only N succeed, and thousands can try at once without colliding or overselling.*

---

## The brief

**Question: before drawing a single box — what *is* a flash sale, stripped down? It looks like a store. Why is it a distributed-systems problem?**

<img src="../assets/flash-sale/requirements.svg" alt="The brief for designing a flash sale, framed as 'fixed inventory plus contention equals locking.' Top: the deceptively simple setup — a store stocked with 10,000 identical items and a huge crowd of buyers (a wall of stick figures) all arriving at the same instant, labelled 'a fixed set of items, a crowd that comes in a short window.' Below, three requirements each with a consequence. One, SELL EXACTLY N: never sell the 10,001st item (overselling is a broken promise and a refund) and never leave items unsold because buyers were slow — so correctness is an exact-count invariant, not an approximate one. Two, HIGH THROUGHPUT UNDER CONTENTION: a million buyers all want the same small pool at the same second, so the hard part is letting many succeed in parallel without a single shared row serializing everyone. Three, NO DISTRIBUTED TRANSACTION ACROSS PAYMENT: payment is a separate, slow, third-party service, so the grab and the payment cannot live in one transaction — you would hold a lock for minutes and blow your SLA. Below, the three hard problems the post attacks in order: CONTENTION — a million writes aimed at one counter row; CORRECTNESS — sell exactly N, no oversell, no undersell; PAYMENT BOUNDARY — keep the slow payment out of the fast grab. A final highlighted insight at the bottom, marked 'the unlock': do not decrement one shared counter — split the inventory into N individually claimable rows and let each buyer atomically claim one, so contention spreads across rows and the count is exact by construction." width="1000">

A flash sale looks like an ordinary store with one knob turned to the extreme: **fixed inventory** meets **flash crowd**. You have a known, small number of items, and essentially everyone who wants one shows up in the same narrow window. That single combination — *can't make more, everyone arrives at once* — is what turns a CRUD app into a contention problem.

The requirements are three, and each one forces a layer later in the post:

- <span style="color:#ffff99"><strong>Sell exactly N.</strong></span> You must never confirm the <span style="color:#ff8a8a"><strong>10,001st</strong></span> order (an oversell is a broken promise, a refund, and a furious customer), and you must not leave phones on the shelf because buyers were slow. This is an **exact-count invariant** — the kind of correctness a unique constraint or a row lock enforces, not the kind you can eyeball.
- <span style="color:#ff8a8a"><strong>High throughput under contention.</strong></span> A million buyers want the *same* tiny pool in the *same* second. The genuinely hard requirement hiding inside "sell" is letting many buyers succeed **in parallel** — the moment a single shared row sits on the critical path, everyone serializes behind its lock and your throughput collapses to one-at-a-time.
- <span style="color:#93c5fd"><strong>No distributed transaction across payment.</strong></span> Payment is a separate, slow, third-party service that can take seconds to minutes. If "grab the item" and "take the money" live in one transaction, you hold a database lock for the entire payment — at flash-sale concurrency that is fatal. The grab and the payment **must** be split.

Three things are genuinely hard, and the rest of the post just attacks them in order: <span style="color:#ff8a8a"><strong>contention</strong></span> (a million writes aimed at one row), <span style="color:#ffff99"><strong>correctness</strong></span> (exactly N, no over- or under-sell), and the <span style="color:#93c5fd"><strong>payment boundary</strong></span> (keep the slow payment out of the fast grab).

And one move reframes everything: **don't decrement a single shared counter.** Split the inventory into **N individually claimable rows** and let each buyer atomically claim one. Contention spreads across rows instead of piling onto one, and "exactly N" becomes true *by construction* — there are only N rows to claim. Hold onto that; it's the thread the whole post pulls.

> **Memory hook:** *fixed inventory + flash crowd = a contention problem. The three hard parts are contention, exact-count correctness, and keeping slow payment out of the fast grab. The unlock: claim one of N rows, don't decrement one counter.*

### The vocabulary, in one place

- <span style="color:#ffff99"><strong>Inventory</strong></span> — the fixed pool of items for sale. In the naive design it's a single `quantity` integer; in the real design it's N rows, one per physical unit.
- <span style="color:#ff8bd2"><strong>Grab / claim / pick</strong></span> — a buyer reserving one unit (adding it to their cart). The *write* that must be exactly-once-per-unit. We record it as `picked_at` / `picked_by`.
- <span style="color:#ffff99"><strong>Hot row</strong></span> — one row taking far more writes than it can serialize, while the table around it idles. The recurring villain — the same one from the [view counter](30-youtube-views-counter.md).
- <span style="color:#ff8a8a"><strong>Row-lock contention</strong></span> — many transactions queuing single-file behind the write lock on one row. What kills the naive counter.
- <span style="color:#ffff99"><strong>`FOR UPDATE`</strong></span> — a `SELECT` that takes a write lock on the rows it returns, so no other transaction can touch them until you commit.
- <span style="color:#ff8bd2"><strong>`SKIP LOCKED`</strong></span> — a modifier that says "if a row is already locked, don't wait — skip it and take the next free one." Turns a convoy into parallel claims.
- <span style="color:#93c5fd"><strong>Payment service</strong></span> — a separate (often third-party) system. It tells us the result asynchronously via a <span style="color:#93c5fd"><strong>webhook</strong></span> — a callback HTTP request — *after* the grab has already committed.
- <span style="color:#93c5fd"><strong>Reaper</strong></span> — a cron job that releases units whose carts were grabbed but never paid for, handing the inventory back to the pool.
- <span style="color:#93c5fd"><strong>Waiting room</strong></span> — a gate in front of the whole system that admits buyers in controlled batches, so the stampede never reaches the database all at once.

---

## Section 1 — Day Zero: One Counter, Decremented in a Transaction

**Question: forget the crowd. One buyer, one item. What is the absolute smallest thing that already *is* a flash sale — something you'd write this afternoon?**

You're a store owner with 10,000 Mi8 phones. The obvious model is a single row holding a count, and a buyer "adds to cart" by decrementing it. Because two things must happen together — *take one off the shelf* **and** *put it in this buyer's cart* — you wrap them in a [transaction](03-database-airline-checkin-transactions-indexes-locks.md), so either both happen or neither does.

<img src="../assets/flash-sale/day-zero-counter.svg" alt="Day-zero flash sale and why it melts, in two panels. LEFT PANEL, the naive design: an inventory table drawn with columns id, item_id, quantity holding one row — item 720 (Mi8 phone), quantity 10000. A buyer sends 'add to cart' to a single API server, which runs a transaction containing two statements against a Counts DB cylinder: first UPDATE inventory SET quantity = quantity minus 1 WHERE item_id = 720 AND quantity greater than 0, then INSERT INTO cart(user, item). A caption: logically perfect — one unit leaves the shelf and lands in exactly one cart, atomically, and quantity can never go below zero because of the WHERE guard. RIGHT PANEL, why it breaks at flash-sale scale (outlined in red): the same picture but with a million buyers all pressing Buy on the SAME item in the same second, so every transaction targets the SAME single quantity row. Three red failure labels point at that one row: (1) ROW-LOCK CONTENTION — each decrement takes a write lock on the one row, so a million transactions serialize into a single-file convoy and effective throughput collapses to one-at-a-time; (2) LOCK HELD FOR THE WHOLE TRANSACTION — the row stays locked until commit, so any slow step inside the transaction stalls everyone behind it; (3) ADDING API SERVERS MAKES IT WORSE — more servers just pile more transactions onto the same hot row. The result, in a red banner: the single counter row is the bottleneck, the crowd does not get spread, it gets queued. Takeaway: the design is correct but serial — correctness is fine, concurrency is the problem." width="1000">

The whole thing is one transaction:

```sql
BEGIN;
  UPDATE inventory SET quantity = quantity - 1
    WHERE item_id = 720 AND quantity > 0;   -- take one off the shelf
  INSERT INTO cart (user_id, item_id) VALUES (1023, 720);  -- put it in the cart
COMMIT;
```

**Is this correct?** Completely. The `quantity > 0` guard means the row can never go negative, so you can't oversell; the transaction means a buyer never gets a phantom cart entry for a phone that wasn't decremented. For a normal store with a trickle of buyers, this is not just fine — it's *right*. Don't build a locking pipeline for a shop that sells ten phones a day.

So why is it the centerpiece of what *not* to do at flash-sale scale? **Because of the hot row.** Every one of those million "add to cart" transactions targets the **same `quantity` row** for item 720, and the row's write lock is held from the `UPDATE` until `COMMIT`. So the buyers don't get spread out — they get *queued*, single-file, behind one lock. This is exactly the [hot-row contention](30-youtube-views-counter.md) we met in the view counter, and the cruel part is identical: the database is the bottleneck, and **adding API servers makes it worse** — more servers just hammer the one row harder. The fix is not a faster database. The fix is to **stop making everyone fight over one row.**

> **Memory hook:** *one `quantity` row decremented in a transaction is logically perfect and operationally fatal at scale — a million buyers serialize behind one row's write lock, held until commit. Correctness is fine; concurrency is the problem. You can't out-scale a hot row by adding servers.*

---

## Section 2 — The Unlock: Split the One Row into N Rows

**Question: the heat all comes from there being *one* row to fight over. What if there simply weren't one row — what if there were 10,000?**

This is the whole design in one move. Instead of one row holding `quantity = 10000`, create **one row per physical unit** — 10,000 rows, each representing a single phone that is either free or taken. Call this table `units`, and think of it as **Phase 0: stock the shelves.** As the store owner, before the sale opens you lay out every individual item.

<img src="../assets/flash-sale/split-the-row.svg" alt="Splitting the single inventory counter into one row per unit. Top, BEFORE: the old inventory table with a single row, item 720 quantity 10000, drawn with a thick red outline and labelled 'one hot row — everyone fights over this.' A big downward arrow labelled 'split into N rows' leads to AFTER. AFTER: a new table called units with columns id, item_id, picked_at, picked_by, purchased_by, drawn with 10,000 rows — shown as id 1 item 720 with picked_at NULL picked_by NULL purchased_by NULL, then id 2, id 3, an ellipsis, and id 10000, every row initially NULL across the claim columns. Caption under the table: if you are selling 10,000 Mi8 phones (item 720), you have 10,000 rows in the units table — each row is one physical phone that is either free (picked_at IS NULL) or taken. A panel on the right, 'Phase 0: prepare the stock', shows a store owner figure laying items onto shelves, mapped to inserting 10,000 unit rows before the sale opens. A highlighted insight box at the bottom: now the contention is spread across 10,000 rows instead of piled on one, and 'sell exactly N' is true by construction — there are exactly N rows to claim, so you physically cannot claim the N-plus-first. The columns are explained briefly: picked_at and picked_by record who reserved the unit and when (the grab); purchased_by is set later only when payment succeeds. Takeaway: replace a single mutable counter with N immutable-identity rows, turning a decrement-the-counter problem into a claim-a-free-row problem." width="1000">

The schema is small:

```sql
CREATE TABLE units (
  id            BIGINT PRIMARY KEY,
  item_id       BIGINT,        -- 720 = Mi8 phone
  picked_at     TIMESTAMP NULL,  -- when it was grabbed (NULL = free)
  picked_by     BIGINT    NULL,  -- which buyer grabbed it
  purchased_by  BIGINT    NULL,  -- set only when payment succeeds
  purchased_at  TIMESTAMP NULL
);
```

If you're selling 10,000 Mi8 phones, you insert **10,000 rows** with `item_id = 720`, all with `picked_at IS NULL` — every row is one physical phone, free and waiting. A unit is <span style="color:#8aff8a"><strong>available</strong></span> when `picked_at IS NULL`, and <span style="color:#ff8bd2"><strong>taken</strong></span> the moment a buyer stamps it with `picked_at` and `picked_by`. (The `purchased_by` columns stay NULL until payment clears — that's [Section 5](#section-5--phase-2-payment-is-a-separate-transaction).)

Two things change the instant you do this:

- <span style="color:#8aff8a"><strong>Contention spreads.</strong></span> Buyers no longer fight over one row; they fan out across 10,000. Ten thousand buyers can be grabbing ten thousand *different* rows at the same instant with zero conflict between them.
- <span style="color:#ffff99"><strong>"Exactly N" is free.</strong></span> There are exactly N rows. You physically cannot claim the (N+1)th, because it doesn't exist. The exact-count invariant stops being something you enforce with a fragile guard and becomes a property of the data itself.

We've turned a *decrement-the-counter* problem into a *claim-a-free-row* problem. But there's a new, subtler race hiding here: with 10,000 free rows and a million buyers, **two buyers can still reach for the same free row at the same time.** Spreading the rows reduced the contention; it didn't make the claim safe. That last bit of safety is the crux of the whole post.

> **Memory hook:** *replace one mutable `quantity` counter with N rows, one per unit. Contention spreads across N rows and "sell exactly N" is true by construction. New problem: two buyers can still race for the same free row — that's what the locking solves.*

---

## Section 3 — Grabbing an Item: `FOR UPDATE SKIP LOCKED` (the crux)

**Question: 10,000 free rows, a million buyers. Two of them run "find a free phone" at the same microsecond and both see row 1. How do you let each buyer grab a *distinct* free row — fast, in parallel, and with absolutely no double-grab?**

This is the heart of the system, and the answer is a single SQL clause. A buyer grabs an item in a tiny transaction: **find one free row, lock it, stamp it.**

<img src="../assets/flash-sale/skip-locked.svg" alt="The FOR UPDATE SKIP LOCKED grab, the crux of the design. Center: the units table for item 720, with several rows — id 1 already taken (picked_at 04/04 10:01, picked_by 1023), id 2 free (all NULL), id 3 free, and so on. Two SQL statements are shown as the grab transaction. First, SELECT id FROM units WHERE item_id = 720 AND picked_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED, with FOR UPDATE SKIP LOCKED highlighted in pink. Second, UPDATE units SET picked_at = NOW(), picked_by = 1023 WHERE id = the row found. Three buyer figures (1023, 2044, 3199) fire the same query at the same instant; arrows show them each landing on a DIFFERENT free row — 1023 gets row 2, 2044 gets row 3, 3199 gets row 4 — because each row the others have locked is skipped, not waited on. A side panel, 'what each clause does', explains: FOR UPDATE takes a write lock on the selected row so no other transaction can grab it (makes the item unavailable to others); SKIP LOCKED means do not block on an already-locked row, just skip to the next free one — so the three buyers run in parallel instead of convoying behind row 2. A second panel, 'why it is exactly-N': once all 10,000 rows have picked_at set, the WHERE picked_at IS NULL returns zero rows and the SELECT comes back empty — that empty result IS 'sold out', no counter check needed. A red contrast note: with plain FOR UPDATE (no SKIP LOCKED) all buyers would queue behind the same first free row, serializing into a convoy — exactly the hot-row problem again. Takeaway: SKIP LOCKED converts a lock convoy into parallel, non-blocking, non-overlapping claims, and the empty result set is a natural sold-out signal." width="1000">

The grab, in full:

```sql
BEGIN;
  SELECT id FROM units
    WHERE item_id = 720 AND picked_at IS NULL
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED;        -- find one free phone, lock it, skip ones others hold

  UPDATE units SET picked_at = NOW(), picked_by = 1023
    WHERE id = :found_id;          -- stamp it as mine
COMMIT;
```

Two clauses do all the work, and it's worth being precise about each:

- <span style="color:#ffff99"><strong>`FOR UPDATE`</strong></span> takes a **write lock** on the row the `SELECT` returns. While your transaction is open, no other transaction can select-for-update or modify that row — it has, in effect, become **unavailable to everyone else.** This is what prevents a double-grab: the row is yours from the moment you select it until you commit.
- <span style="color:#ff8bd2"><strong>`SKIP LOCKED`</strong></span> is the magic word. Without it, a second buyer whose query also matched row 2 would *block* — wait for your lock to release — and you'd be right back to a single-file convoy behind the first free row. With it, the second buyer's query **skips any row that's already locked** and takes the next free one. So buyer 1023 gets row 2, buyer 2044 gets row 3, buyer 3199 gets row 4 — all at the same time, no waiting, no overlap.

The result is exactly what the brief demanded: a <span style="color:#8aff8a"><strong>non-blocking, non-sequential</strong></span> claim. Thousands of buyers run the identical query concurrently and each walks away with a distinct row. And **"sold out" needs no special handling**: once every row has `picked_at` set, `WHERE picked_at IS NULL` matches nothing, the `SELECT` returns empty, and an empty result set *is* the sold-out signal. No counter to check, no off-by-one to get wrong.

This is the same claim pattern the [task scheduler's pullers](29-distributed-task-scheduler.md) used to pull due jobs without colliding — there the rows were jobs, here they're phones, but the mechanism is identical: **let the database hand each worker a distinct row, atomically.**

> **Memory hook:** *the grab is `SELECT … WHERE picked_at IS NULL … LIMIT 1 FOR UPDATE SKIP LOCKED`, then stamp `picked_at`/`picked_by`. `FOR UPDATE` makes the row unavailable to others; `SKIP LOCKED` turns a lock convoy into parallel claims. Empty result = sold out, for free.*

---

## Section 4 — Why `SKIP LOCKED`, and Where Else It Shows Up

**Question: `FOR UPDATE` already prevents the double-grab. So what exactly does `SKIP LOCKED` buy, and is this a one-off flash-sale trick or a pattern worth keeping?**

It's worth slowing down on the three flavors of locking read, because the difference between them *is* the difference between a system that scales and one that convoys. The standard says, when the row you want is already locked, you may **wait**, **fail fast**, or **skip**.

<img src="../assets/flash-sale/skip-locked-usecases.svg" alt="Comparing the three locking-read modes and listing where SKIP LOCKED is used. Top, three side-by-side panels each showing the same scenario — several workers all wanting the same locked first row. Panel one, plain FOR UPDATE (red): workers queue single-file behind the locked row, drawn as a convoy of figures waiting in line, labelled 'WAIT — blocks until the lock frees; serializes everyone; this is the hot-row convoy again.' Panel two, FOR UPDATE NOWAIT (blue): a worker hits the locked row and immediately gets an error, drawn with a lightning bolt and a 503-style reject, labelled 'FAIL FAST — returns an error instantly instead of waiting; the caller must retry; good when you would rather error than block.' Panel three, FOR UPDATE SKIP LOCKED (green): each worker steps over the locked rows and grabs the next free one, drawn as workers fanning out to different rows, labelled 'SKIP — ignore locked rows, take the next free one; parallel, non-blocking; the basis of a concurrent claim.' Bottom, a panel titled 'SKIP LOCKED is a general concurrent-queue primitive', listing real use cases as a checklist: flash sale / seat booking — many buyers claim distinct units of a fixed pool without blocking; database-backed job queue — many workers pull distinct pending jobs (the task scheduler pullers); message broker on a table — multiple consumers dequeue distinct messages from a topic; batch processing — each worker grabs a distinct batch of unlocked rows. A safety note in a box: because the lock is tied to the transaction, if a worker crashes mid-claim the transaction rolls back and the row becomes free again automatically — no orphaned locks. Available in PostgreSQL 9.5+, MySQL 8+, Oracle, and DB2. Takeaway: pick WAIT when order matters and contention is low, FAIL FAST when blocking is unacceptable, and SKIP LOCKED whenever many workers must claim distinct rows from a shared pool in parallel." width="1000">

The three modes, and when each is right:

| Mode | On a locked row | Use it when |
| --- | --- | --- |
| <span style="color:#ff8a8a"><strong>`FOR UPDATE`</strong></span> | **waits** for the lock to free | order matters and contention is low — but at scale this is the convoy that melts the hot row |
| <span style="color:#93c5fd"><strong>`FOR UPDATE NOWAIT`</strong></span> | **errors immediately** | you'd rather fail fast and retry than block at all |
| <span style="color:#8aff8a"><strong>`FOR UPDATE SKIP LOCKED`</strong></span> | **skips it**, takes the next free row | many workers must each claim a *distinct* row from a shared pool, in parallel |

For a flash sale the choice is obvious: plain `FOR UPDATE` recreates the exact convoy we just escaped (everyone waits behind the first free row), and `NOWAIT` would spray errors at buyers who could perfectly well have taken a different free row. `SKIP LOCKED` is the only one that says *"don't fight over a row someone else has — there are plenty of others, take one of those."*

And this is not a flash-sale curiosity — it's a **general concurrent-queue primitive**. The same one-liner powers:

- <span style="color:#8aff8a"><strong>Seat and ticket booking</strong></span> — many buyers claim distinct seats from a fixed pool without blocking each other (the flash sale itself).
- <span style="color:#93c5fd"><strong>Database-backed job queues</strong></span> — many workers pull distinct pending jobs; this is exactly how the [task scheduler's pullers](29-distributed-task-scheduler.md) avoid running the same job twice.
- <span style="color:#93c5fd"><strong>A message broker on a table</strong></span> — multiple consumers dequeue distinct messages from a topic.
- <span style="color:#ffff99"><strong>Batch processing</strong></span> — each worker grabs a distinct *batch* of unlocked rows to chew through.

There's a quiet safety bonus too: because the lock lives and dies with the **transaction**, a worker that crashes mid-claim simply has its transaction rolled back, and the row becomes free again automatically — no orphaned locks, no manual cleanup. (`SKIP LOCKED` lives in PostgreSQL 9.5+, MySQL 8+, Oracle, and DB2.)

> **Memory hook:** *three locking reads — `FOR UPDATE` waits (convoy), `NOWAIT` fails fast, `SKIP LOCKED` skips and takes the next free row (parallel claim). `SKIP LOCKED` is a general concurrent-queue primitive: flash sales, job queues, table-as-broker, batch work. The lock is tied to the transaction, so a crash auto-frees the row.*

---

## Section 5 — Phase 2: Payment Is a Separate Transaction

**Question: the buyer has grabbed a phone — `picked_at` is stamped, the row is theirs. Now they have to pay, and payment can take a minute on a flaky third-party gateway. Do you keep the grab transaction open until the money clears?**

Absolutely not — and this is the most important boundary in the system. If the grab transaction stayed open through payment, you'd hold a **row lock for the entire payment** (and at flash-sale concurrency, thousands of held locks). Worse, "grab the item in *our* database" and "charge the card in the *payment* service" would become a **distributed transaction spanning two systems** — the thing you can almost never afford. So Phase 1 (the grab) **commits immediately**, releasing the lock, and Phase 2 (payment) runs as its own, separate flow.

<img src="../assets/flash-sale/payment-phase.svg" alt="Payment as a separate phase, decoupled from the grab. Left: the grab transaction from the previous section has already COMMITTED — the unit row id 2 shows picked_at and picked_by 1023 set, purchased_by still NULL, and a green check labelled 'lock released, item reserved.' A bold dashed boundary line down the middle is labelled 'TRANSACTION BOUNDARY — the grab and the payment are NEVER in one transaction.' Right of the boundary: the buyer continues to a Payment Service (drawn as a separate external box, often third-party), which takes seconds to minutes. The Payment Service reports its result asynchronously by calling a webhook back into our system (a callback arrow labelled 'webhook'). Two outcomes branch from the webhook. SUCCESS path (pink/green): UPDATE units SET purchased_by = 1023, purchased_at = NOW() WHERE id = 2, then create the order — the unit is now permanently sold. FAILURE path (blue): UPDATE units SET picked_at = NULL, picked_by = NULL WHERE id = 2 — the unit is released back to the pool and becomes available for someone else to grab. A red callout box, starred: you CANNOT have a distributed transaction spanning add-to-cart and payment — holding a DB lock across a slow external call would destroy throughput and blow the SLA, so we commit the grab first and reconcile payment afterward via the webhook. A note: the webhook is the coordination point that replaces the distributed transaction — at-most-one outcome is applied per unit. Takeaway: commit the reservation fast, let payment run free, and let an asynchronous webhook either finalize the sale or release the unit." width="1000">

The flow after the grab commits:

- The buyer proceeds through the normal **payment** flow against a separate <span style="color:#93c5fd"><strong>payment service</strong></span>. Our database is no longer holding anything on their behalf except a stamped `picked_at` — no lock, no open transaction.
- The payment service reports its result **asynchronously**, by calling a <span style="color:#93c5fd"><strong>webhook</strong></span> back into our system. That webhook is the coordination point that *replaces* the distributed transaction.
- <span style="color:#ff8bd2"><strong>On success:</strong></span> `UPDATE units SET purchased_by = 1023, purchased_at = NOW() WHERE id = 2`, then create the order. The phone is now permanently sold.
- <span style="color:#93c5fd"><strong>On failure:</strong></span> `UPDATE units SET picked_at = NULL, picked_by = NULL WHERE id = 2`. The unit drops straight back into the pool, `picked_at IS NULL` again, and the very next buyer's `SKIP LOCKED` query can grab it.

```sql
-- payment webhook: SUCCESS
UPDATE units SET purchased_by = :user, purchased_at = NOW() WHERE id = :unit;

-- payment webhook: FAILURE  →  make it available again
UPDATE units SET picked_at = NULL, picked_by = NULL WHERE id = :unit;
```

The thing to internalize: **payment is not part of the flash sale.** The flash sale's job is to hand out exactly N reservations, fast and correctly. Whether each reservation turns into money is a *separate* concern, reconciled afterward by the webhook. Each webhook applies at most one outcome per unit — finalize it, or release it — and the slow, failure-prone payment never once touches the fast grab path.

> **Memory hook:** *commit the grab first, pay second — never in one transaction. Holding a row lock across a slow third-party payment (a distributed transaction across two systems) is fatal at scale. A webhook reconciles afterward: success sets `purchased_by`; failure nulls `picked_at`/`picked_by` and the unit falls back into the pool.*

---

## Section 6 — What If No Payment? The Cron Reaper

**Question: a buyer grabs a phone, then closes the tab — no success webhook, no failure webhook, just silence. The row sits with `picked_at` stamped forever. How do you get that phone back on sale?**

The webhook handles the buyer who *finishes* paying or *explicitly* fails. But the most common abandonment is neither: the buyer just **leaves**. Their unit is stuck — `picked_at` set, `purchased_by` still NULL — reserved for a sale that will never happen. If you do nothing, abandoned carts slowly drain your sellable inventory and you **under-sell** a sold-out item. The fix is a background <span style="color:#93c5fd"><strong>reaper</strong></span>: a cron job that releases units whose reservation has gone stale.

<img src="../assets/flash-sale/cron-reaper.svg" alt="The cron reaper that reclaims abandoned carts. Center: a Reaper drawn as a clock-and-broom icon labelled 'cron job, runs every minute', scanning the units table. It finds rows where picked_at is old but purchased_by is still NULL — drawn as a row id 7 with picked_at = 10:01 (12+ minutes ago), picked_by 5567, purchased_by NULL, flagged 'abandoned.' The reaper runs UPDATE units SET picked_at = NULL, picked_by = NULL WHERE picked_at < NOW() minus 12 minutes AND purchased_by IS NULL, shown with the WHERE clause highlighted. An arrow shows row id 7 flipping back to all-NULL — 'released, available again' — so the next buyer's SKIP LOCKED grab can take it. Right side, a tradeoff panel titled 'how long is the timeout?', drawn as a balance scale. One pan, timeout TOO SHORT: you yank carts away from real buyers who are mid-payment, disappointing genuine customers (a frustrated buyer figure whose phone is snatched). Other pan, timeout TOO LONG: abandoned units sit locked, the item shows sold out while phones go unsold, and you under-sell — labelled 'payment drop, lost revenue.' A caption between them: there is no free lunch — either you disappoint a few in-flight buyers, or you, as the company, lose sales to under-selling; the timeout (here ~12 minutes) is where you set that dial. A note: the reaper only ever touches rows with purchased_by IS NULL, so it can never reclaim a paid unit. Takeaway: a periodic reaper turns abandoned reservations back into inventory, and the reservation timeout is a deliberate business tradeoff between disappointing buyers and under-selling." width="1000">

The reaper is one statement on a timer:

```sql
-- runs every minute
UPDATE units
   SET picked_at = NULL, picked_by = NULL
 WHERE picked_at < NOW() - INTERVAL '12 minutes'
   AND purchased_by IS NULL;        -- never reclaim a paid unit
```

The `purchased_by IS NULL` guard is the safety belt: the reaper can *only* touch reservations that never turned into a sale, so it can never claw back a phone someone actually bought. Anything grabbed more than ~12 minutes ago without a purchase is presumed abandoned and released back to `picked_at IS NULL`, where the next `SKIP LOCKED` grab will find it.

And the timeout is not a neutral number — it's a **business dial** with disappointment on both ends:

- <span style="color:#ff8a8a"><strong>Too short</strong></span> and you yank carts away from real buyers who are *still paying*, snatching the phone out from under someone mid-checkout.
- <span style="color:#ff8a8a"><strong>Too long</strong></span> and abandoned units sit locked while the item shows "sold out," so phones go unsold and you suffer <span style="color:#ff8a8a"><strong>payment drop</strong></span> — lost revenue from under-selling.

There's no free lunch here: either you disappoint a few in-flight buyers, or you, as the company, lose sales to under-selling. The timeout is simply where you choose to set that dial.

> **Memory hook:** *abandoned carts (grabbed, never paid, no webhook) would silently under-sell you. A cron reaper nulls `picked_at`/`picked_by` where `picked_at` is old AND `purchased_by IS NULL` — never touching a paid unit. The timeout is a business tradeoff: too short snatches carts from real buyers, too long under-sells.*

---

## Section 7 — Seats, Not Quantities: Redis for Atomic Grabs

**Question: phones are interchangeable — any free one will do. But an airline seat or a train berth is *specific*: 14A is not 14B. And you might want even higher throughput than a relational table can give. Is there a leaner way to do the atomic grab?**

When units are **fungible** (any Mi8 is as good as any other), the `units` table with `SKIP LOCKED` is perfect. But for **seats** — where the buyer picks a *specific* one, or you want to hand out a specific free one at extreme speed — there's a sharper tool: do the entire grab as a single <span style="color:#93c5fd"><strong>atomic Redis operation</strong></span>.

<img src="../assets/flash-sale/redis-alternative.svg" alt="Using Redis for atomic seat grabs as an alternative to the relational table. Left: a Redis store drawn as an in-memory box holding a SET of available seat ids for a flight — {14A, 14B, 14C, 22F, ...}. Multiple buyers fire an atomic operation at it — SPOP (pop a random member) to grab any free seat, or for a specific seat SREM 'seat:14A' which atomically removes-and-returns whether it was present. Arrows show three buyers each receiving a DIFFERENT seat with no overlap, because Redis executes one command at a time. A panel titled 'why Redis is safe here — single-threaded': Redis runs commands one at a time on a single thread, so each SPOP/SREM is atomic by construction — there is no race between check and set, no lock needed. A second panel, 'but isn't single-threaded slow?': no — Redis uses I/O multiplexing (epoll) so thousands of client connections are handled concurrently at the I/O layer; only the actual command execution is serial on the CPU, and each command is microseconds, so throughput is very high. A durability panel (yellow): Redis is in-memory, so to survive a crash without losing the inventory it needs AOF (append-only file) persistence enabled — otherwise a restart forgets which seats were sold. A red caution panel: Redis is a single point of pressure — if you let an unbounded crowd flood it, it can be overwhelmed and crash, so it still sits behind the same admission control; and on payment failure or timeout you must SADD the seat back into the available set, mirroring the relational release. Takeaway: for specific or extremely-high-throughput grabs, an atomic Redis op replaces the SELECT-FOR-UPDATE dance, trading the durability and richness of a relational table for raw speed, with AOF for persistence and admission control to protect it." width="1000">

The model: keep the available seats as a Redis **set**, and grab with a single atomic command.

- To hand out *any* free seat: `SPOP flight:AI302:seats` — atomically pops and returns one member.
- To claim a *specific* seat: `SREM flight:AI302:seats 14A` — atomically removes it and tells you whether it was there to remove. If it returns 1, the seat is yours; if 0, someone beat you to it.

Why is this safe with no `FOR UPDATE`, no transaction? Because <span style="color:#93c5fd"><strong>Redis is single-threaded</strong></span>: it executes one command at a time, so every `SPOP`/`SREM` is **atomic by construction**. There's no window between "check if free" and "mark taken" for a second buyer to slip into — the whole grab *is* one indivisible operation.

The natural worry — *isn't single-threaded slow?* — has a clean answer. Redis uses <span style="color:#93c5fd"><strong>I/O multiplexing</strong></span> (epoll): thousands of client connections are serviced concurrently at the I/O layer, and only the actual command *execution* is serial on the CPU. Since each command takes microseconds, the serial part is a near-non-issue and throughput is enormous. **I/O is parallelized; the CPU work happens one call at a time** — and that's exactly what makes it both fast *and* race-free.

Two cautions keep it honest:

- <span style="color:#ffff99"><strong>Persistence.</strong></span> Redis is in-memory, so a crash would forget which seats were sold. Turn on <span style="color:#ffff99"><strong>AOF</strong></span> (append-only file) so the inventory survives a restart. (This is the same durability concern from the [distributed cache](15-storage-engine-distributed-cache.md) post.)
- <span style="color:#ff8a8a"><strong>It's a single point of pressure.</strong></span> A single-threaded server is fast but finite; let an unbounded crowd flood it and it can be overwhelmed. So Redis doesn't remove the need for the gate in front of everything — it still sits behind admission control. And just like the relational version, a payment failure or timeout must `SADD` the seat back into the available set.

> **Memory hook:** *for specific seats or extreme throughput, do the grab as one atomic Redis op — `SPOP` for any seat, `SREM` for a named one. Safe because Redis is single-threaded (each command is atomic, no lock needed); fast because I/O multiplexing parallelizes connections while only CPU execution is serial. Needs AOF for durability, and admission control so the crowd can't crash it.*

---

## Section 8 — In the Wild: Why Shopify Moved Reservations *Off* Redis and *Onto* SQL

**Question: we just made Redis look great for atomic grabs. So why did Shopify — a company whose entire business is flash-sale-shaped traffic — do the *opposite*, and migrate inventory reservations from Redis back to a relational database?**

The previous section is exactly where Shopify *started*: reservations in <span style="color:#93c5fd"><strong>Redis</strong></span>, because it nails the part we praised — fast, concurrent, atomic decrements (`DECR`/`INCR`) on a hot count. For the *grab* alone, Redis is wonderful. But a flash sale isn't just the grab; it's grab **then** claim-on-payment, and that second half is where the architecture broke.

### The reason for the move: two systems can't share one transaction

The fatal flaw was structural, and it's the same boundary problem from [Section 5](#section-5--phase-2-payment-is-a-separate-transaction) seen from a worse angle. The **reservation lived in Redis** while the **durable inventory ledger lived in MySQL** — *two different systems.* So when a payment succeeded, "permanently deduct from the ledger" (MySQL) and "release the reservation" (Redis) were [two writes that could not be wrapped in one atomic step](https://shopify.engineering/scaling-inventory-reservations). A crash between them left the data inconsistent, which produced exactly the two failures this whole post is built to prevent:

- <span style="color:#ff8a8a"><strong>Oversell</strong></span> — the item sold but was never deducted from the ledger.
- <span style="color:#ff8a8a"><strong>Undersell</strong></span> — the item was deducted while still marked reserved.

Moving reservations *into* MySQL, alongside the ledger, is what makes reserve-and-claim **one ACID transaction** — and the design they landed on is precisely the one we built: <span style="color:#ffff99"><strong>one row per sellable unit</strong></span>, reserve K units by locking K rows in a single transaction with <span style="color:#ff8bd2"><strong>`SKIP LOCKED`</strong></span>. The relational store wins not because it's faster than Redis (it isn't) but because the claim and the durable record finally share a transaction. *Atomicity beat raw speed.*

### The real bottleneck wasn't the lock — it was connections

Here is the most counter-intuitive lesson, and the one worth burning in. After the migration, the system stalled — but **CPU was low**. The constraint was not query speed, not the row lock, not contention on the units. It was <span style="color:#ff8a8a"><strong>database connection-pool exhaustion</strong></span>.

The mechanism: at flash-sale throughput you need *many short transactions per second*, and **connections are finite**. A connection is held for the entire duration of a transaction, so any transaction that holds a connection *longer than it needs* steals it from everyone else. The tell was the symptom shape — <span style="color:#ff8a8a"><strong>low CPU but high queuing</strong></span> meant threads were **blocked waiting for a free connection**, not saturating compute. You cannot fix that by adding CPU or optimizing the reservation query; the reservation query was never the problem.

How they found it: they **tagged every SQL statement with the business process that issued it** (e.g. a `/* conn_tag:checkout_completion */` comment) and measured per-caller connection hold-time at the <span style="color:#93c5fd"><strong>ProxySQL</strong></span> layer. That instrumentation revealed *non-reservation* checkout code holding connections across long transactions, quietly starving the reservation path. Cleaning it up removed **~50% of reads and ~33% of transactions** from the primary — lifting an artificial ceiling that had nothing to do with reservations at all.

> **Memory hook:** *connections are finite and held for the whole transaction, so the flash-sale bottleneck is often connection-pool exhaustion, not CPU or the lock. The tell: low CPU + high queuing = threads blocked waiting for a connection. Find it by tagging SQL with its caller and measuring hold-time at the proxy.*

### How they migrated safely: shadow traffic, then a gradual flip

You don't hard-cut a system that mustn't oversell. Shopify used a <span style="color:#93c5fd"><strong>shadow-mode dual-write</strong></span> rollout:

- <span style="color:#93c5fd"><strong>Dual-write, Redis as source of truth.</strong></span> Every reservation was written to *both* Redis and MySQL, but **Redis stayed authoritative**. MySQL ran alongside on full production traffic, so its correctness and performance could be validated side-by-side without anyone depending on it yet.
- <span style="color:#ff8bd2"><strong>Flip the source of truth, gradually.</strong></span> Once MySQL was proven, they switched the authoritative designation **pod by pod — lowest-traffic merchants first, highest-volume last** — while keeping the dual-write path as a safety net.
- <span style="color:#8aff8a"><strong>Keep a kill switch.</strong></span> Because Redis kept receiving the complete reservation state the entire time, reverting was instant if anything went wrong.

This is the textbook pattern for migrating a stateful, correctness-critical system: **shadow the new store on real traffic, compare, flip slowly, keep the old one warm enough to fall back to.**

### One concrete gotcha: a "gap lock" that blocked the refill

Their pool is *bounded* — they keep ~1,000 free rows per item and **refill** it from the ledger as buyers drain it, rather than materializing all N units. That refill ran into a surprising wall, and it's worth understanding because it's a classic.

First, the one term you need. A normal lock locks a **row**. A <span style="color:#ff8a8a"><strong>gap lock</strong></span> locks the **empty space *between* rows** — it's a "no vacancy" sign hung over a stretch of the table that says *nobody may insert a new row here.* Why would a database ever do that? To keep a *re-read* honest: if your transaction just scanned "all free rows" and found none, the database locks that empty range so no one can sneak a new row in behind your back and change the answer if you look again.

That's exactly what bit them. When a grab ran `SELECT … FOR UPDATE SKIP LOCKED` and found the pool empty, MySQL's default behavior locked the empty range — and that gap lock <span style="color:#ff8a8a"><strong>blocked the refill from inserting new rows</strong></span>. The grab was guarding empty space the refill needed to fill. The two starved each other.

The fix is one setting: the transaction's <span style="color:#ffff99"><strong>isolation level</strong></span> — the knob for how aggressively a transaction guards what it has read.

- <span style="color:#ffff99"><strong>`REPEATABLE READ`</strong></span> (MySQL's default) promises every re-read in a transaction returns the *same* answer — so it takes **gap locks** to freeze ranges. Strong, but it's what blocked the refill.
- <span style="color:#ffff99"><strong>`READ COMMITTED`</strong></span> only promises each statement sees the latest *committed* data at the moment it runs — it makes **no such re-read promise, so it takes no gap locks**, only locks on rows it actually touches.

A reservation grab is a tiny "find one free row and stamp it" — it never re-reads, so the gap locks bought nothing and cost everything. Switching to `READ COMMITTED` keeps the row lock you *do* need (from `FOR UPDATE`) and drops the gap locks you *don't*. **Match the isolation level to what the transaction actually needs — no more.**

> **Memory hook:** *Shopify went Redis → SQL because two systems can't share one transaction (→ over/undersell); the real bottleneck was connection-pool exhaustion, not the lock (low CPU + high queuing); they migrated with shadow dual-writes and a pod-by-pod flip; and they dropped to `READ COMMITTED` — which only sees committed data per-statement and takes no gap locks — so pool replenishment wasn't blocked.*

---

## Section 9 — Protect the Database: The Virtual Waiting Room

**Question: every mechanism so far makes the *grab* cheap and correct. But a million people still arrive in one second. Even with `SKIP LOCKED`, do you really want a million connections stampeding your database at once?**

No. The cheapest contention to handle is the contention that **never reaches your database.** The last layer is a gate in front of everything: a <span style="color:#93c5fd"><strong>virtual waiting room</strong></span> that admits buyers in controlled batches, so the stampede is shaped into a steady stream before it ever touches the units table.

<img src="../assets/flash-sale/waiting-room.svg" alt="A virtual waiting room protecting the flash-sale backend. Left: a massive crowd of buyers (a dense wall of stick figures) all arriving at 12:00:00. They hit a Waiting Room / admission gate drawn as a turnstile with a queue behind it, sitting at the edge before any backend. The gate holds most buyers in a queue (showing each their position, e.g. 'you are number 84,219 in line') and admits only a controlled trickle — a token-bucket rate limiter labelled 'admit N buyers per second' — through to the real system. Right of the gate, a thin controlled stream of admitted buyers reaches the API servers, which run the FOR UPDATE SKIP LOCKED grab against the units database (a cylinder). A key annotation: the number admitted is matched to the inventory plus a margin — there is no point admitting a million people to claim 10,000 phones, so once enough buyers to plausibly sell out have been let in, the rest can be shown 'sold out' without ever hitting the DB. A second annotation, 'protect at the edge': the UI itself does not even render the buy button or the sale until the sale is live and the buyer is admitted, so bots and early refreshers cannot pound the backend — the protection starts in the client and the gate, before the database. A small note tying back: this is the operational side and is expanded in part two. Takeaway: the most effective contention control is admission control — keep the crowd out of the database entirely, admit only as many as the inventory can absorb, and shape a one-second spike into a manageable stream." width="1000">

The waiting room does two things:

- <span style="color:#93c5fd"><strong>Throttle admission.</strong></span> Hold the crowd in a queue (showing each person their position) and admit only a controlled trickle — a token-bucket rate limiter that lets, say, a few thousand buyers per second through to the actual grab. A one-second spike of a million becomes a manageable stream.
- <span style="color:#ffff99"><strong>Match admission to inventory.</strong></span> There is no point admitting a million people to claim 10,000 phones. Once enough buyers to plausibly sell out have been let in, everyone else can be shown "sold out" *at the gate*, without ever touching the database.

And the protection starts even earlier, in the client: **the UI doesn't render the buy button — or the sale at all — until the buyer is admitted.** Bots and early-refreshers can't pound the backend for something they can't see. The principle is to **protect the database from the crowd entering it in the first place**, rather than relying on the database to absorb the full stampede.

The waiting room is the bridge into **Part 2**: every locking trick so far makes the grab *correct and parallel*, but admission control is what keeps the volume *survivable*. Part 2 zooms out from this one gate to the whole operational picture — how the fleet is laid out, and what you actually do on the day of the sale.

> **Memory hook:** *the cheapest contention is the kind that never reaches the DB. A virtual waiting room admits buyers in controlled batches (token bucket) and matches admission to inventory — no point letting a million in to claim 10,000. The UI hides the sale until you're admitted, so the crowd is shaped at the edge, not absorbed by the database.*

---

## Part 2 — The Operational Side: Surviving the Stampede

Part 1 made the *grab* correct under contention. Part 2 is the other half — the day-of plumbing that keeps the boxes standing when a celebrity drop lands. The whole mindset fits in one line: **scale the cheap things freely, protect the one thing you can't scale instantly — the database — and keep a switch you can pull when it's over.** (Drawn from [Shopify's flash-sale engineering](https://shopify.engineering/) and Emil Stolarsky's [SREcon talk](https://www.youtube.com/watch?v=-I4tIudkArY).)

<img src="../assets/flash-sale/operational-architecture.svg" alt="The operational architecture of a flash sale, drawn as a left-to-right request path. The crowd hits the Edge (Nginx + Lua, a 'heat shield + throttle'), which forwards to a warm, pre-cached Load Balancer, which forwards a thin admitted trickle to a fleet of stateless API servers that scale out freely, which do the precious writes against a Pod (an isolated stack holding its own MySQL — units + ledger — and its own Redis hot-reads cache). A green dashed arrow loops back from the load balancer to the crowd labelled 'most buyers wait here on a cached throttle page — cheap reads bounce at the edge, never reaching the app or DB'; a pink path labelled 'only admitted buyers do writes — grab/checkout/payment, the precious metered path' continues into the pod. Below, four operational knobs as cards: 1 Scale up the DB (bigger primary + read replicas, ahead of time, because you can't add DB capacity mid-spike); 2 Warm LB and caches (pre-warm so the first burst doesn't hit cold caches; cache the throttle page so the LB soaks up the polling); 3 Add API servers (stateless, scale horizontally, cheap to add unlike the database); 4 The kill switch (a feature flag that turns the buy button OFF in the UI — no button means no writes — flipped the instant it sells out). Bottom banner: the whole operational job is to turn a one-second spike into a steady stream the database can absorb — cheap reads stay near the edge and caches, precious writes are metered through the throttle so the primary never sees a stampede." width="1180">

### Architecture: pods, not one shared cluster

The first structural decision is how to lay out the fleet. Shopify runs the platform as <span style="color:#ffff99"><strong>pods</strong></span> — and a pod is a *complete, self-contained mini-Shopify*: its own app workers, its own MySQL shard, its own Redis, hosting a set of shops. If one pod's database catches fire under a Kanye drop, the other pods — and every merchant on them — never feel it. The blast radius of a flash sale is exactly **one pod**.

<img src="../assets/flash-sale/pods.svg" alt="An explanation of pods and why they beat logical shards. Top: an Edge router (Nginx + Lua) sends each shop's request to its own pod. Three pods sit side by side, each a self-contained stack of app workers + its own MySQL shard + its own Redis. Pod 1 (shops A, B) and Pod 3 (shops E, F) are drawn green and calm, 'unaffected.' Pod 2 (shop C — a Kanye drop — and shop D) is drawn red and on fire: app workers slammed, hot MySQL shard, busy Redis, with the note 'all the heat is trapped in here.' Caption: a pod is a complete mini-Shopify, so the blast radius of a flash sale is one pod. Bottom, 'Why pods and not the logical shards from the S3 post?', two panels. Left (red): logical shards share ONE app fleet and ONE connection pool; a row of shards where the hot one is highlighted, captioned 'a hot shard starves its neighbors' connections and CPU — great for spreading load and cheap moves, weak blast-radius isolation.' Right (green): pods isolate the whole stack — three pod boxes each 'own app+DB+pool', the hot one keeps its heat inside, captioned 'a noisy merchant literally can't touch another pod — flash sale wants hard isolation, so pods win.' Bottom line: logical shards split the data on a shared fleet; pods split the data AND the fleet, so one sale can't take down the rest." width="1180">

**Why pods and not the [logical shards](20-high-throughput-system-s3.md) from the S3 post?** Both split data up, but they isolate *different things*. Logical shards live on one shared fleet — same machines, same connection pool — so they spread a single dataset's load and make rebalancing cheap, but a hot shard can still starve its neighbors at the *compute and connection* layer (exactly the [connection-pool exhaustion](#section-8--in-the-wild-why-shopify-moved-reservations-off-redis-and-onto-sql) that bit Shopify). A pod isolates the **whole vertical stack**, so a noisy merchant literally cannot touch another pod's connections or CPU. For a flash sale you want hard blast-radius isolation over cheap rebalancing — so **pods win.** (Shops still move between pods when needed, in seconds, using Shopify's GhostFerry.)

### The user flow: a throttle page at the edge

Now the request path, when the sale is hot. The key trick: the buyer's `GET /checkout` doesn't reach the application at all — the <span style="color:#93c5fd"><strong>edge</strong></span> (Nginx + Lua) returns a lightweight **throttle page**, and the load balancer *caches* it so serving it costs almost nothing. The browser quietly **polls**; every poll is answered by the cached page, untouched by the app or database. When capacity frees up, one poll comes back with a redirect and a **checkout pass**, and *only then* does the buyer reach real checkout and the DB.

| Step | Request | What happens | Who answers |
| --- | --- | --- | --- |
| 1 | `GET /checkout` | sale is hot → serve the throttle page | edge → cached at LB |
| 2 | `GET /checkout?poll=1` | still waiting → same cached page | <span style="color:#93c5fd"><strong>LB cache</strong></span> (app/DB never touched) |
| 3 | `GET /checkout?poll=1` | capacity free → `302` + checkout pass | app → admitted to checkout |

Millions can "wait" cheaply because steps 1–2 are just a cached page; only the trickle in step 3 ever spends real backend capacity. This is the [virtual waiting room from Section 9](#section-9--protect-the-database-the-virtual-waiting-room), made concrete.

**What is "the edge," and what is Nginx + Lua?** The <span style="color:#93c5fd"><strong>edge</strong></span> is simply the *first* server a request hits — it sits in front of your application and decides, in microseconds, whether a request even deserves to go further. <span style="color:#93c5fd"><strong>Nginx</strong></span> is a very fast reverse proxy that already terminates connections and forwards traffic; <span style="color:#93c5fd"><strong>Lua</strong></span> is a tiny scripting language you can embed *inside* Nginx (via OpenResty) to run a few lines of your own logic right there at the door — "is this user admitted? if not, hand them the cached throttle page." So "Nginx + Lua" just means **a smart gatekeeper that runs cheap decisions at the front door, before any request reaches your expensive app servers or database.** It can do this for hundreds of thousands of requests per second because it never touches the heavy backend.

When should you reach for an edge router like this? The rule of thumb: **push a decision to the edge whenever it's cheap to make and you want to make it a huge number of times.**

- <span style="color:#93c5fd"><strong>Admission control / throttling</strong></span> — the flash-sale case: hold the crowd on a cached page and admit a trickle, so the stampede dies at the door instead of in your database.
- <span style="color:#93c5fd"><strong>Rate limiting & bot/abuse defense</strong></span> — drop or slow obvious abuse before it costs you a backend round-trip.
- <span style="color:#93c5fd"><strong>Routing</strong></span> — send each request to the right place (e.g. Shopify's "Sorting Hat" routing each shop to its [pod](#architecture-pods-not-one-shared-cluster)), or A/B/canary splits.
- <span style="color:#93c5fd"><strong>Cheap responses</strong></span> — serve cached pages, redirects, health checks, and auth rejects without ever waking the app.

And when *not* to: anything that needs real business logic, a database read, or per-user state that isn't already at the edge belongs in the app, not in a Lua script at the door. The edge is for **fast, stateless, high-volume gatekeeping** — keep it dumb and let it shed load; do the thinking behind it.

### Concurrency: meter writes, let reads fly

The whole game is to treat reads and writes completely differently, because they cost completely different amounts.

<img src="../assets/flash-sale/read-write-paths.svg" alt="Reads and writes drawn as two separate lanes from the same crowd, to show they cost completely different amounts. Top lane (READ PATH, green, 'cheap, scales out'): the crowd of ~1,000,000 polling browsers flows through a CDN + edge cache (static assets, throttle page), then an LB cache (absorbs the relentless polling), then read replicas (product page, inventory count) — a cylinder. A panel states: reads never touch the primary; served from cache, CDN, and replicas, so a million of them are harmless — let them fly. Bottom lane (WRITE PATH, pink, 'precious, metered'): a Throttle gate (admit only a trickle per second) lets a thin trickle through to an API server (runs the grab, then checkout), which writes to the Primary DB cylinder (grab then checkout then pay). A yellow note beside the primary, 'The grab store: pick ONE', shows the two alternatives: SQL units table with FOR UPDATE SKIP LOCKED — OR — Redis seats with SPOP/SREM — making explicit they are two implementations of the same grab, never used together. A panel states: only admitted buyers write; the primary is the one resource you can't conjure mid-spike, so the throttle turns a spike into a stream — protect it. A dashed connector from the crowd down to the throttle is labelled 'the throttle is the valve: the waiting million (reads) become a trickle of writers'. Bottom banner: one request stream, split by cost — scale reads out at the edge, meter writes down to what the primary can absorb; this single split (cheap-and-many versus precious-and-few) is the whole operational strategy." width="1180">

- <span style="color:#8aff8a"><strong>Reads are cheap and scale out.</strong></span> The product page, the inventory count, the throttle page — serve them from cache, CDN, and read replicas, far from the primary. Let a million of these fly; they never threaten correctness.
- <span style="color:#ff8bd2"><strong>Writes are precious and get metered.</strong></span> The grab, the checkout, the payment all hit the primary, and the primary is the one resource you can't conjure mid-spike. So the throttle exists to turn a one-second write *spike* into a steady write *stream* the primary can absorb — which is the same thing the [`SKIP LOCKED` grab](#section-3--grabbing-an-item-for-update-skip-locked-the-crux) does at the row level, now done at the traffic level.

> **One store, not two — pick the grab implementation, don't stack them.** A diagram that shows both a SQL `units` table *and* a Redis seat-set can read as "we run two inventory databases." We don't. They are **two alternative implementations of the same atomic grab** ([Section 3](#section-3--grabbing-an-item-for-update-skip-locked-the-crux) vs [Section 7](#section-7--seats-not-quantities-redis-for-atomic-grabs)) — you choose **one** per inventory type: the SQL `units` table for fungible quantities (and whenever the claim must share a transaction with the ledger — [Section 8](#section-8--in-the-wild-why-shopify-moved-reservations-off-redis-and-onto-sql)), or Redis for specific seats or extreme throughput. The *only* place both appear at once is when Redis plays a **different role** — a read-only cache for hot counts, never the source of truth for what's sold.

### Operations: the four knobs

Before and during the sale, you turn four knobs — three to add capacity to the cheap things, one to cut load at the source:

- <span style="color:#ffff99"><strong>Scale up the DB</strong></span> — give the pod a bigger primary and more read replicas *ahead of time*. You can't add database capacity instantly mid-spike, so this is the one you must do early.
- <span style="color:#93c5fd"><strong>Warm the load balancer & caches</strong></span> — pre-warm so the first burst doesn't slam cold caches, and cache the throttle page so the LB absorbs the relentless polling.
- <span style="color:#8aff8a"><strong>Add API servers</strong></span> — they're stateless, so scale them horizontally to soak up request volume. They're cheap to add; the database is not.
- <span style="color:#ff8bd2"><strong>The kill switch</strong></span> — a feature flag that turns the **add-to-cart / buy button off in the UI** the instant the sale sells out or ends. No button means no write attempts, which means no load reaching the DB. It's the same "protect the database at the source" instinct as the waiting room, used as an off-switch.

> **Memory hook:** *operations = scale the cheap things (API servers, caches, replicas) and protect the one expensive thing (the DB primary). Lay the fleet out as pods so a flash sale's blast radius is one pod, not the platform. Bounce the crowd on a cached throttle page at the edge and admit a trickle with a checkout pass. Meter writes; let reads fly. And keep a kill switch to turn the buy button off the moment it sells out.*

---

## Where this leaves us: the complete flash sale

We started with one counter row decremented in a transaction and grew it, one named bottleneck at a time, into a system that sells exactly N items to an arbitrarily large crowd. Every component earned its place by solving a specific problem the previous step created. Here is the whole machine in one map.

<img src="../assets/flash-sale/final-map.svg" alt="The complete flash-sale architecture in one map, showing all components and the main paths. Far left: a huge crowd of buyers arrives in a short window and hits a Virtual Waiting Room / admission gate (blue), which throttles them with a token-bucket limiter and admits only as many as the inventory can absorb, showing the rest 'sold out' at the edge. The admitted trickle reaches a stateless fleet of API servers (rectangles). GRAB PATH (pink): an API server runs the grab transaction against the units database (yellow cylinder) — SELECT one row WHERE picked_at IS NULL LIMIT 1 FOR UPDATE SKIP LOCKED, then UPDATE picked_at, picked_by — committing immediately so the lock is released; the units table is the N-rows-per-item store that replaced the single counter, and an empty result means sold out. An alternative grab store is shown beside it for seats: a Redis box (blue) doing atomic SPOP/SREM with AOF persistence. PAYMENT PATH (separated by a bold transaction-boundary line): the buyer pays via an external Payment Service (blue), which calls back a webhook; on success the system sets purchased_by/purchased_at and creates the order, on failure it nulls picked_at/picked_by to release the unit — never in the same transaction as the grab. CLEANUP PATH (blue): a cron Reaper periodically releases abandoned units where picked_at is old AND purchased_by IS NULL, returning them to the pool. A legend ties the colors to the roles: pink the grab/write path, yellow the durable inventory and exact-count invariant, blue the control/async plane (waiting room, payment webhook, reaper, Redis), red the failure modes each layer defends against (hot-row contention, oversell, under-sell, distributed transaction). The single sentence under the map: a flash sale is fixed inventory split into N claimable rows, grabbed atomically with FOR UPDATE SKIP LOCKED, with slow payment reconciled out-of-band by a webhook, abandoned carts reaped by cron, and the crowd shaped by a waiting room before it reaches the database." width="1280">

The components, and the one idea each is built around:

| Layer | What it is | The one idea |
| --- | --- | --- |
| <span style="color:#93c5fd"><strong>Waiting room</strong></span> | Admission gate in front of everything | The cheapest contention never reaches the database |
| <span style="color:#ffff99"><strong>Units table</strong></span> | One row per physical unit, not one counter | Spread contention across N rows; "exactly N" by construction |
| <span style="color:#ff8bd2"><strong>The grab</strong></span> | `FOR UPDATE SKIP LOCKED` claim, committed fast | Each buyer gets a distinct free row, in parallel, no oversell |
| <span style="color:#93c5fd"><strong>Payment + webhook</strong></span> | Separate flow, reconciled asynchronously | Never hold a lock across a slow third-party payment |
| <span style="color:#93c5fd"><strong>Cron reaper</strong></span> | Releases stale unpaid reservations | Abandoned carts must become inventory again, or you under-sell |

Read the colors and they narrate the design: a <span style="color:#93c5fd"><strong>blue gate</strong></span> shaping the crowd, a <span style="color:#ffff99"><strong>yellow store</strong></span> of N claimable rows where the exact-count invariant lives, a <span style="color:#ff8bd2"><strong>pink grab</strong></span> that hands out distinct rows in parallel, and a <span style="color:#93c5fd"><strong>blue async plane</strong></span> — payment webhook and reaper — that reconciles money and reclaims abandoned carts well off the hot path. That is a flash sale.

> **Memory hook:** *fixed inventory + contention = locking. Split the counter into N rows, grab one with `FOR UPDATE SKIP LOCKED` and commit fast, reconcile slow payment out-of-band via a webhook, reap abandoned carts by cron, and shape the crowd with a waiting room before it reaches the database.*

---

## Further reading

The design here is built from first principles, but every piece has deep prior art. To go further:

- **[PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html)** — the canonical reference for the locking-read modes (`FOR UPDATE`, `NOWAIT`, `SKIP LOCKED`) at the heart of the grab.
- **[How to implement a database job queue using SKIP LOCKED — Vlad Mihalcea](https://vladmihalcea.com/database-job-queue-skip-locked/)** — the clearest walkthrough of the same primitive applied to job queues, with the convoy-vs-parallel comparison made concrete.
- **[Using `FOR UPDATE SKIP LOCKED` for queue workflows — Netdata](https://www.netdata.cloud/academy/update-skip-locked/)** — practical patterns and pitfalls for table-as-queue designs.
- **[Scaling inventory reservations — Shopify Engineering](https://shopify.engineering/scaling-inventory-reservations)** — the production case study behind [Section 8](#section-8--in-the-wild-why-shopify-moved-reservations-off-redis-and-onto-sql): the Redis→MySQL migration, one-row-per-unit with `SKIP LOCKED`, the connection-pool bottleneck, shadow-traffic rollout, and the `READ COMMITTED` gap-lock fix.
- **[Redis atomic operations and persistence (AOF)](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)** — why single-threaded execution makes commands atomic, and how AOF keeps an in-memory store durable.
- **[Designing Data-Intensive Applications by Martin Kleppmann](https://dataintensive.net/)** — the book for transactions, isolation levels, and the contention and consistency fundamentals underneath all of this.
</content>
</invoke>
