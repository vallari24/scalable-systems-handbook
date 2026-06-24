# Designing a Distributed Task Scheduler — and the Message Broker Beneath It

This post builds a **distributed task scheduler** from first principles: a system that accepts "run *this command* at *this time*" and guarantees it actually runs, on time, across thousands of jobs and a fleet of machines. Starting from a single `cron` line on one box, it grows each component only when a named bottleneck appears: a relational table to store tasks, lightweight **pullers** split off from heavyweight **executors**, a row-locking pull query (`FOR UPDATE SKIP LOCKED`) so many pullers never collide, a broker between pulling and running, an **orchestrator** that scales the fleet ahead of demand, and finally a `tasks`/`jobs` split that turns a recurring cron rule into an ordinary stream of fixed executions. We scope out retries deliberately — at-least-once delivery is its own post — so we can focus on the spine: *store, pick, execute, on time.*

**Question: you have to run hundreds of thousands of jobs — "build this commit," "shut down staging at 11pm," "email this invoice at 3pm every day" — each at a precise time, with a hard promise that a job due at 10:00:00 runs by 10:00:30. The jobs aren't uniform: one needs a GPU box, the next needs a kilobyte of RAM. What is the smallest design that fires every job inside its SLA on day zero — and what is the *next* thing that breaks every time you 10× the job count?** The honest path runs straight through one cron line, a single MySQL table, a pile of pullers fighting over the same rows, a queue, an autoscaler that reads the future, and a two-table trick for recurrence — and by the end you've hand-built the engine underneath [dkron](https://dkron.io/), [Quartz](https://www.quartz-scheduler.org/), and [AWS EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html).

This post leans on a few earlier ones. We built a [distributed lock manager](07-distributed-lock-manager.md) and learned how leader election and mutual exclusion work — here the database itself becomes the lock. We built [distributed id generators](08-distributed-id-generators.md) and learned why a **time-sortable id** is so useful — here it decides which shard a task lives on. We built [Airflow](24-high-throughput-airflow.md) and saw orchestration of *dependent* DAGs — this post is the simpler, harder-real-time sibling: independent jobs, each with a clock deadline.

> **Memory hook:** *a task scheduler is "store, pick, execute" — a durable table of `(command, run_at)`, a fleet of pullers that atomically claim due rows, and a fleet of executors that run them — and everything hard about it comes from doing that fast enough to hit a per-task deadline while many pullers race over the same rows.*

This post comes in two parts. **Part 1** builds the scheduler and leans on a broker (SQS/RabbitMQ) as a black box between pullers and executors. **[Part 2](#part-2--a-message-broker-on-a-relational-database)** opens that black box and *builds the broker itself* on the same relational table and the same `FOR UPDATE SKIP LOCKED` claim — because a message broker is just the scheduler's "puller" half, generalized.

---

## The brief

**Question: before drawing a single box — what *is* a task scheduler, stripped to one sentence, and where have you already used one without naming it?**

You've used one this morning. Every time you `git push`, something notices the new commit, drops its id into a queue, and a **build worker** pulls it and runs the build. That "notice an event, enqueue work, run it later" loop *is* a task scheduler. So is the humblest `cron` line on a single box — `0 23 * * * /shut-down-staging.sh` — that shuts your staging instances down at 11pm every night. One machine, one crontab, and it works perfectly.

<img src="../assets/distributed-task-scheduler/requirements.svg" alt="The brief for designing a distributed task scheduler, titled 'Distributed Task Scheduler — without retries', with the real systems dkron and AWS CloudWatch Events / EventBridge Scheduler named at the top as production examples. The core job, drawn as a single line: schedule a task to be executed. Two flavors branch off it. One, FIXED — execute once at a certain absolute time (e.g. run this build now, or shut down staging at 11pm tonight). Two, RECURRING — execute on a repeating CRON schedule (e.g. every day at 3pm). Below, the hard requirement that shapes everything: a 30-second SLA, illustrated with an example — a task scheduled at 10:01:00am must begin executing before 10:01:30am, i.e. within 30 seconds of its scheduled time. Plus minute-level granularity: schedules are specified to the minute. At the bottom, the whole post framed as a three-word brainstorm, STORE → PICK → EXECUTE: where do we durably store a task, how do many workers pick the due ones without colliding, and how do we execute them on heavy specialized machines fast enough to hit the SLA. A note clarifies the deliberate scope cut: no retries — at-least-once redelivery is out of scope so we can focus on the on-time spine." width="1000">

So why is this a system-design problem at all? Because the numbers explode. One crontab on one machine is fine for ten jobs. It is not fine when:

- **The job count grows** to hundreds of thousands of distinct schedules, more than one machine can track or fire.
- **The jobs are heterogeneous.** A build needs a beefy CPU box; an ML job needs a GPU; an email needs almost nothing. One machine can't be the right shape for all of them.
- **There's a deadline.** A job scheduled for 10:00 that fires at 10:45 is *wrong*, not just slow. Lateness is a correctness bug.

Production systems make this precise. **dkron** is a distributed cron with a fault-tolerant cluster; **AWS EventBridge Scheduler** (the grown-up successor to CloudWatch Events rules) fires millions of scheduled invocations; **Quartz** is the classic JVM job scheduler. They all solve the same three problems we're about to attack — store, pick, execute — under a clock.

### The requirements, in one place

Three requirements drive every decision below:

1. <span style="color:#ffff99"><strong>Two schedule types.</strong></span> <span style="color:#ffff99"><strong>Fixed</strong></span> — run once at an absolute time. <span style="color:#ffff99"><strong>Recurring</strong></span> — run on a repeating cron rule (`every day at 3pm`). We'll design fixed first, then show recurrence is *just* a generator of fixed executions.
2. <span style="color:#ff8a8a"><strong>A 30-second SLA.</strong></span> A task due at `10:01:00` must *start executing* by `10:01:30`. This single number is the whole game: it's what forces pullers and executors apart, what sizes the fleet, and what every metric measures against.
3. **Minute-level granularity.** Schedules are specified to the minute, not the microsecond. That's a gift — it means a polling design (check the table every few seconds) is comfortably good enough; we never need nanosecond timers.

### The vocabulary

- <span style="color:#ffff99"><strong>Task</strong></span> — the *definition*: "run command X, on this schedule." For a fixed task that's one execution; for a recurring task it's a template that spawns many.
- <span style="color:#ff8bd2"><strong>Job / run</strong></span> — one concrete *execution* of a task at one specific time. A fixed task has one job; a recurring task has an endless stream of jobs.
- <span style="color:#8aff8a"><strong>Puller</strong></span> — a lightweight worker that *claims* due jobs from the database and hands them to the broker. It never runs the command.
- <span style="color:#ffd27f"><strong>Executor</strong></span> — a heavy, often specialized machine (GPU, high-CPU, high-memory) that pulls a claimed job from the broker and *actually runs the command*.
- <span style="color:#93c5fd"><strong>Broker</strong></span> — the queue (SQS, RabbitMQ) between pullers and executors that decouples claiming from running.
- <span style="color:#93c5fd"><strong>Orchestrator</strong></span> — the control-plane brain that watches near-future load and scales pullers and executors up and down.
- <span style="color:#ff8a8a"><strong>SLA</strong></span> — the deadline: `started_at − scheduled_at ≤ 30s`. Every timestamp we store exists to measure or defend this.

> **Memory hook:** *store, pick, execute — under a 30-second clock. Fixed runs once; recurring is just a fixed-execution generator. Minute granularity means polling is fine. Task = definition, job = one execution, puller claims, executor runs, broker decouples them, orchestrator scales them.*

---

## Section 1 — Storage: where does a task even live?

**Question: the very first decision is where to put a task when someone schedules it. The reflex answers are "a queue" or "a database" — and if a database, SQL or NoSQL? Which one, and *why* — what specific property of this workload decides it?**

Start with the tempting wrong answer: **just put it in a queue.** You schedule a task, you push it onto a message queue, an executor pops it. Clean — until you remember the requirement. A queue delivers messages *as soon as they're available*, in roughly FIFO order. It has no concept of "deliver this one at 3pm tomorrow and not a second before." Some brokers offer a delay/visibility timer, but capped at minutes-to-hours and not at the scale of *hundreds of thousands of independently-timed* messages sitting around for weeks. A queue is a *transport*, not a *time-indexed store*. We'll use one later — but not as the source of truth.

So it's a database. **SQL or NoSQL?** Here's the productive way to decide: don't argue brand; ask what *operations* the workload demands.

<img src="../assets/distributed-task-scheduler/storage-choice.svg" alt="A decision diagram, 'Where do we store a task? Queue vs SQL vs NoSQL.' Three columns. Column one, QUEUE (crossed out in red): a horizontal tube; note 'delivers when available, not at an absolute future time; no time-range query; capped delayed-delivery'. Verdict: a transport, not a time-indexed store — rejected as the source of truth. Column two, NoSQL (key-value / document, e.g. DynamoDB, drawn as a cylinder): great for point lookups by id and massive write scale, but the access pattern we actually need is a RANGE SCAN over time — 'give me every task whose scheduled_at has passed' — plus a per-row atomic claim. A range query over a sort key is possible but the row-level locking / SELECT-FOR-UPDATE-SKIP-LOCKED claim primitive is a relational strength. Column three, SQL / relational (MySQL, Postgres, drawn as a cylinder, highlighted as the choice): the workload is a time-range read plus a transactional claim — exactly what a B-tree index on a timestamp column plus row locks were built for. A box lists the three operations that decide it: (1) WRITE a task — a single-row insert; (2) READ due tasks — a range scan scheduled_at <= now(), which a B-tree index on scheduled_at answers in log time; (3) CLAIM atomically — many workers must each grab a disjoint set of due rows with no double-pick, which relational row locking (FOR UPDATE SKIP LOCKED) gives directly. Verdict at the bottom: modest write volume, a time-range read, and a transactional multi-reader claim → a plain relational table; no sharding needed on day one. Registering a task is just one INSERT into the tasks table." width="1000">

Lay the three operations side by side:

- <span style="color:#ff8bd2"><strong>Write a task</strong></span> — a single-row insert. Both kinds of store do this trivially.
- <span style="color:#8aff8a"><strong>Read the due tasks</strong></span> — *"give me every task whose `scheduled_at` has passed."* This is a **range query over a timestamp**, and a <span style="color:#ffff99"><strong>B-tree index on `scheduled_at`</strong></span> answers it in logarithmic time. NoSQL can range-scan a sort key too, but this is the relational engine's home turf.
- <span style="color:#ffff99"><strong>Claim a task atomically</strong></span> — the killer requirement. Many pullers will hit the table at once, and each due row must be picked by *exactly one* of them. Relational databases hand you this directly with **row-level locking** (`SELECT … FOR UPDATE SKIP LOCKED`, Section 4). That single primitive is worth the whole choice.

The write volume is modest (people schedule far less often than executors run), the read is a clean time-range scan, and the claim is a transactional multi-reader problem. That profile points straight at a **plain relational table** — MySQL or PostgreSQL — and, crucially, **no sharding, no exotic store on day one.** Registering a task is as simple as one `INSERT`. We earn the right to add complexity only when a named bottleneck forces it (Section 8).

This is the same instinct as the [multi-tiered datastore](21-high-throughput-multi-tiered-db.md): a relational database is *correct first*, and you only move off it when a specific property — here, write throughput per shard — actually breaks.

> **Memory hook:** *a queue delivers "now," not "at 3pm" — so the source of truth is a relational table, chosen not by brand but by three operations: insert a task, range-scan due tasks on a B-tree timestamp index, and atomically claim rows with row-level locks. That claim primitive is why SQL wins. No sharding on day one.*

---

## Section 2 — The schema: what exactly do we store?

**Question: we have a `tasks` table. What columns does it need — and the subtle one: how many *timestamps*, and do we store an explicit `status`, or can we derive it?**

The obvious columns write themselves: an `id`, the `command` to run, and a `scheduled_at` telling us *when*. But a good schema here is mostly about **timestamps**, because every timestamp is a measurement point for the SLA — and the design lives or dies on the SLA.

<img src="../assets/distributed-task-scheduler/schema.svg" alt="The tasks table schema, drawn as a relational table with seven columns and annotations above each. Columns left to right: id (primary key), command (the shell command / payload to run), scheduled_at (a TIMESTAMP — annotated 'store as UTC, never local time; this is the column we INDEX'), picked_at (when a puller claimed the row, initially NULL), started_at (when an executor began running, initially NULL — annotated 'SLA gate: started_at − scheduled_at must be ≤ 30s'), completed_at (when it finished successfully, NULL until done), failed_at (when it errored, NULL unless it failed). A sample row is shown: id=1, command='build', scheduled_at='13/01/23 10:00', picked_at=NULL, started_at=NULL, completed_at=NULL, failed_at=NULL. Three callouts point at the column group. Callout one (over scheduled_at): 'INDEX here — the hot read is a range scan scheduled_at <= now()'. Callout two (over picked_at / started_at): 'the SLA ledger — each gap between consecutive timestamps measures one stage of the pipeline'. Callout three (over a dashed phantom status column): 'Do we need an explicit status column? You can DERIVE it: NULL started_at + NULL failed_at = pending; failed_at set = failed; completed_at set = success. Storing status is faster to read but can DRIFT out of sync with the timestamps after a crash, needing a reconciliation job. Deriving is always correct but needs a small CASE expression. Tradeoff, not a law.' Bottom note: timestamp type should be a timezone-aware/UTC TIMESTAMP so 3pm means the same instant everywhere." width="1000">

### The timestamps, and what each one *measures*

We keep a chain of timestamps, each `NULL` until its stage happens:

- <span style="color:#ffff99"><strong>`scheduled_at`</strong></span> — when the task *should* run. **Store it as UTC**, never local time, or daylight-saving will fire your job an hour off twice a year. This is the column we **index**, because the hot read is `WHERE scheduled_at <= now()`.
- <span style="color:#ff8bd2"><strong>`picked_at`</strong></span> — when a *puller* claimed the row. `NULL` means "still up for grabs."
- <span style="color:#ffd27f"><strong>`started_at`</strong></span> — when an *executor* actually began running the command. This is the SLA gate: `started_at − scheduled_at ≤ 30s`.
- <span style="color:#8aff8a"><strong>`completed_at`</strong></span> — when it finished successfully.
- <span style="color:#ff8a8a"><strong>`failed_at`</strong></span> — when it errored out.

Why so many? Because **each gap between two consecutive timestamps measures one stage of the pipeline**, and that's how we'll diagnose *which* part is blowing the SLA (Section 5). The timestamps aren't bookkeeping; they're an instrument.

### The status question: store it, or derive it?

Here's the genuinely interesting design call. You're tempted to add a `status` column — `pending / running / done / failed`. **Do you need it?**

You don't *have* to store it, because the timestamps already encode it:

```text
started_at IS NULL  AND failed_at IS NULL   → pending
failed_at IS NOT NULL                       → failed
completed_at IS NOT NULL                    → success
```

So `status` is **derivable** from columns you already keep. The tradeoff is real and worth stating plainly:

- **Derive it** (a small `CASE` expression on read): always correct, never drifts, costs a tiny bit of read-side logic.
- **Store it** (an explicit column you update): faster and simpler to query — but it can <span style="color:#ff8a8a"><strong>drift out of sync</strong></span>. A crash between "write `completed_at`" and "set `status = done`" leaves a row that's secretly done but labeled running, and now you need a **reconciliation job** to scan for and fix the mismatches.

There's no universal answer — it's implementation-specific. If your business logic reads status constantly, store it and accept the reconciliation cost. If reads are rare, derive it and stay correct for free. The point isn't the answer; it's that **a redundant column buys read-speed at the price of consistency**, and you should make that trade on purpose, not by reflex.

> **Memory hook:** *store `id, command, scheduled_at` (UTC, indexed) plus a chain of NULL-until-it-happens timestamps — `picked_at, started_at, completed_at, failed_at` — because each gap measures one pipeline stage. `status` is derivable from those timestamps; storing it is faster to read but can drift and needs a reconciliation job. Redundancy buys speed, costs consistency.*

---

## Section 3 — Pick and execute: why pulling and running must be different machines

**Question: a row is due. Something has to read it from the database and run its command. The naive move is one machine that does both — poll the table, run whatever it finds. Why is that a trap, and what does fixing it force into existence?**

Picture the single-machine version: a worker loops — query the DB for due tasks, run them, repeat. It's beautifully simple and it **violates the SLA the moment any task takes real time to run.**

Here's the failure. Say a worker pulls a build that takes 4 minutes. While it's busy compiling, it is *not polling the database.* Every other task that comes due in those 4 minutes sits unclaimed, blowing past its 30-second deadline. The worker's two jobs — *find work* and *do work* — have completely different time profiles, and **fusing them lets the slow one starve the fast one.** This is the same lesson as [splitting serving from storage in S3](20-high-throughput-system-s3.md): never let one machine own two jobs that scale for different reasons.

There's a second, hardware reason to split them, and it's just as decisive:

<img src="../assets/distributed-task-scheduler/pull-execute-split.svg" alt="A two-panel diagram contrasting the fused design with the split design. Panel one, titled 'WRONG: puller executes inline', shows a single fleet of worker boxes that both query the DB and run commands. A red callout: while a worker is busy running a 4-minute build, it stops polling, so every task due in that window misses its 30s SLA — the slow job starves the fast job. Panel two, titled 'RIGHT: split pull from execute', is the architecture: on the left a user talks to a Task API which writes to a MySQL cylinder (DB Tasks). Three small identical PULLER boxes (lightweight machines) each read from the DB. The pullers feed a horizontal broker tube in the middle. On the right, a fleet of much larger EXECUTOR boxes (drawn bigger, labelled 'bulky / specialized: GPU, high-CPU, high-mem') pull from the broker and run the actual command, then write started_at back to the DB. Annotations: 'Pullers are cheap and many — their only job is to claim due rows fast and never block.' 'Executors are big and few-per-type — a task may need a GPU or 64GB RAM; you size them to the work, not to the polling.' 'A puller must NEVER execute — keep it free to keep claiming, so the SLA is defended.' A note on the broker: it decouples claim-rate from run-rate so each side scales independently. Below: this is the same separation-of-concerns as stateless API tier vs storage tier — find-work and do-work scale for different reasons." width="1000">

Tasks are **heterogeneous**. One needs a GPU. The next needs 64 GB of RAM. The next needs almost nothing. **Executors** are therefore *big, specialized, expensive* machines you size to the work. **Pullers** are *small, cheap, identical* machines whose only job is to claim due rows from the database as fast as possible and never get stuck. Putting a "claim the next row" loop on a $30/hr GPU box is absurd; putting a GPU workload on a tiny puller is impossible. The two roles want opposite hardware.

So the design splits cleanly into two fleets:

- <span style="color:#8aff8a"><strong>Pullers</strong></span> — lightweight, many, identical. Read the DB, claim due jobs, hand them off. Always free to claim more.
- <span style="color:#ffd27f"><strong>Executors</strong></span> — heavy, specialized, sized to the workload. Take a claimed job and run it.

And the instant you have two fleets that produce and consume at different rates, you need something between them to absorb the mismatch. That something is the **broker** — and it deserves its own beat.

> **Memory hook:** *never let the puller execute. A worker running a 4-minute build stops polling and starves every task due in that window. Split into lightweight pullers (claim rows, stay free) and heavyweight specialized executors (GPU/CPU/RAM, run the work) — they want opposite hardware and scale for different reasons, so a broker sits between them.*

---

## Section 4 — The broker: why a queue belongs between pull and execute

**Question: pullers claim due jobs; executors run them. Why not have the puller call the executor directly? What does inserting a queue buy that a direct hand-off can't?**

A direct hand-off — puller picks a row, synchronously calls a free executor — couples the two fleets rigidly. The puller now has to *know which executor is free*, wait for it, and retry if it's busy. That's load-balancing and back-pressure logic you'd have to build by hand, and it stalls the puller (the one thing we swore to keep free).

A <span style="color:#93c5fd"><strong>broker</strong></span> — SQS, RabbitMQ — dissolves all of that. The puller drops the claimed job into the queue and immediately goes back to claiming. Executors pull from the queue whenever they finish their current job. Neither side knows or waits for the other.

What the broker actually buys:

- **Decoupling of rates.** Pullers claim at the DB's pace; executors run at the work's pace. The queue absorbs the difference — it fills when a burst arrives and drains as executors catch up. (Exactly the *impedance matching* role staging storage played in the [tiering pipeline](21-high-throughput-multi-tiered-db.md).)
- **A back-pressure signal.** Queue depth *is* the load signal. A growing queue means "not enough executors" — which is precisely what the orchestrator watches in Section 6 to scale the fleet.
- **Natural load-balancing.** Any free executor grabs the next message. No puller has to track executor health or assign work; the queue is the rendezvous point.

This finally lets us see the whole data path:

```text
user → Task API → MySQL (tasks)
                    ↑   ↓ claim
                  pullers → broker (SQS / RabbitMQ) → executors → run command
                                                          ↓ write started_at
                                                        MySQL
```

The puller's job ends at "dropped into the broker." The executor's job begins at "pulled from the broker," and its first act is to write `started_at` back to the database — the moment that the SLA clock is finally satisfied.

> **Memory hook:** *put a broker (SQS/RabbitMQ) between pullers and executors so neither waits on the other: it decouples claim-rate from run-rate, turns queue depth into the autoscaling signal, and load-balances work to whatever executor is free. Puller's job ends at "enqueued"; executor's job starts at "dequeued" and writes started_at.*

---

## Section 5 — The pull query: how many pullers claim rows without colliding

**Question: now the heart of it. Many pullers hit the same table at the same instant, all running the same "give me the due tasks" query. If two pullers read the same row, the job runs twice. How do you make every due row go to *exactly one* puller — without the pullers waiting in line for each other?**

This is where the relational choice from Section 1 pays off. Let's build the query one failure at a time.

**Attempt 1 — just select the due rows:**

```sql
SELECT * FROM tasks
WHERE scheduled_at <= now()
  AND picked_at IS NULL
ORDER BY scheduled_at;
```

Correct for *one* puller. But run it on ten pullers simultaneously and **they all read the same rows** — `picked_at` is still `NULL` for everyone until someone writes it, so every puller claims every due job. Tenfold duplicate execution. We need the read and the claim to be **one atomic step**.

**Attempt 2 — claim by updating `picked_at` in the same transaction.** Closer, but a plain `SELECT` doesn't stop two transactions from reading the same row before either writes. We need the database to *lock* the rows we're about to claim so no one else can read-to-claim them. That's `FOR UPDATE`:

```sql
SELECT * FROM tasks
WHERE scheduled_at <= now() AND picked_at IS NULL
ORDER BY scheduled_at
FOR UPDATE;          -- take a row lock on every matched row
```

Now puller A locks the rows it reads, and puller B's identical query… **blocks**, waiting for A to commit and release. No double-pick — but we've created a new problem: pullers are now **serialized**, standing in line behind each other. With a 30-second SLA and a flood of due jobs, waiting in line is a non-starter. We don't want B to *wait* for A's rows; we want B to *skip* them and grab the next free ones.

**Attempt 3 — the real query — `FOR UPDATE SKIP LOCKED`:**

<img src="../assets/distributed-task-scheduler/pull-query.svg" alt="A diagram explaining the pull query and its concurrency, titled 'How many pullers claim due rows without colliding'. Center, the SQL in a code panel: SELECT * FROM tasks WHERE scheduled_at <= now() + 2min AND picked_at IS NULL ORDER BY scheduled_at ASC LIMIT 10 FOR UPDATE SKIP LOCKED; followed by UPDATE … SET picked_at = now() for the claimed rows, all in one transaction. To the right, four clauses each annotated: 'scheduled_at <= now() + 2min — a LOOKAHEAD BUFFER: pull jobs slightly before they are due so they sit staged in the broker and fire on time, absorbing pipeline latency'; 'picked_at IS NULL — only unclaimed rows'; 'FOR UPDATE — take a row lock so no other puller can claim these'; 'SKIP LOCKED — and do NOT wait on rows another puller already locked; skip them and grab the next free ones, so pullers never serialize'; 'LIMIT 10 — bound the batch so each claim has a DETERMINISTIC, bounded latency instead of scanning an unbounded number of pages'. On the left, three puller boxes hit the same DB cylinder at once: puller A's lock grabs rows 1–10 (green), puller B SKIPS those locked rows and grabs rows 11–20 (blue), puller C skips both and grabs 21–30 (orange) — a red 'X' over the alternative where B waits in line behind A, labelled 'FOR UPDATE without SKIP LOCKED serializes the pullers — kills throughput'. Bottom takeaway: SELECT … FOR UPDATE SKIP LOCKED LIMIT N is the disjoint-claim primitive — each puller walks away with its own batch, no double-pick and no waiting." width="1000">

`SKIP LOCKED` is the magic word. It tells the database: *if a row I'd match is already locked by another transaction, don't wait for it — skip past it and give me the next unlocked one.* Now ten pullers running the identical query walk away with **ten disjoint batches**: A gets rows 1–10, B skips those and gets 11–20, C gets 21–30. No double-pick (the locks guarantee it) and no waiting (skip guarantees it). This is *the* throughput primitive for a database-backed queue, and it's why PostgreSQL and MySQL both shipped `SKIP LOCKED` specifically for this pattern.

Two refinements complete the query:

- <span style="color:#ffff99"><strong>`LIMIT 10`</strong></span> — claim a **bounded batch**, not "all due rows." Why? Determinism. An unbounded claim might match 5 rows one second and 500,000 the next, so the time to run the query — and to lock all those rows — swings wildly. A fixed `LIMIT` gives each claim a **predictable, bounded latency**, which is exactly what you need when you're budgeting against a 30-second SLA. Bounded work per pull, more pulls in flight.
- <span style="color:#93c5fd"><strong>`scheduled_at <= now() + buffer`</strong></span> (e.g. `+ 2 min`) — a **lookahead buffer**. **Do you need a buffer?** Yes — if you only pull tasks that are *already* due, you've spent your pipeline latency (claim + enqueue + executor pickup) *after* the deadline starts ticking. By pulling jobs that will be due *soon*, they sit staged in the broker, ready to fire the instant they come due. The buffer pre-pays the pipeline latency so the executor can start *on* time, not *after* time.

The final claim, then, is one transaction: `SELECT … FOR UPDATE SKIP LOCKED LIMIT 10`, then `UPDATE … SET picked_at = now()` on those rows, then enqueue them. Atomic, disjoint, bounded, and ahead of schedule.

> **Memory hook:** *the claim query is `SELECT … WHERE scheduled_at <= now()+buffer AND picked_at IS NULL ORDER BY scheduled_at LIMIT 10 FOR UPDATE SKIP LOCKED`, then set `picked_at`. FOR UPDATE stops double-picks; SKIP LOCKED stops pullers waiting in line; LIMIT makes each claim bounded and deterministic; the lookahead buffer pre-pays pipeline latency so jobs fire on time. That one line is a concurrent work queue on a relational table.*

---

## Section 6 — The timestamp ledger: turning four columns into an SLA dashboard

**Question: we chose to keep `picked_at`, `started_at`, `completed_at`, and `failed_at`. We've used `picked_at`. What are the *other* three actually for — and why keep both `completed_at` and `failed_at` instead of one "done" flag?**

This is the payoff of the schema from Section 2. **The gap between each pair of consecutive timestamps measures exactly one stage of the pipeline**, and together they form a live SLA ledger that tells you *which fleet* to scale.

<img src="../assets/distributed-task-scheduler/sla-ledger.svg" alt="A timeline-style diagram, 'The timestamp ledger: each gap measures one pipeline stage', drawn as a horizontal axis with four labelled instants and the gaps between them annotated. Instant 1: scheduled_at (when the job is due). Instant 2: picked_at (puller claimed it). Instant 3: started_at (executor began). Instant 4: completed_at OR failed_at (executor finished). Gap A, scheduled_at → picked_at, labelled 'PULLER THROUGHPUT — how long a due job waits to be claimed; if this grows, add more PULLERS'. Gap B, picked_at → started_at, labelled 'BROKER WAIT + EXECUTOR THROUGHPUT — how long a claimed job sits in the queue waiting for a free executor; if this grows, add more EXECUTORS'. Gap C, started_at → completed_at, labelled 'EXECUTION TIME — how long the command itself ran; compare to the historical average to detect a hung or failing machine'. The whole span scheduled_at → started_at is bracketed and labelled 'THE SLA: must be ≤ 30s' with a sample budget underneath — roughly 4s to claim and enqueue, ~1s broker handoff, leaving ~25s of slack to read from the broker and start. A side panel, 'Why keep BOTH completed_at and failed_at?': completed_at − started_at gives the execution duration, which you average over history to (1) flag anomalies — a run far longer than its historical mean signals a stuck executor or a dying machine — and (2) feed predictive scaling — knowing average runtime lets the orchestrator estimate how many executors a future burst needs. failed_at distinguishes 'errored' from 'still running' (a NULL completed_at alone is ambiguous). Bottom takeaway: the ledger localizes the bottleneck — Gap A means scale pullers, Gap B means scale executors, Gap C means investigate the machine." width="1000">

Read the ledger gap by gap:

- <span style="color:#8aff8a"><strong>`picked_at − scheduled_at` = puller throughput.</strong></span> How long a due job waited before any puller claimed it. If this number creeps up, your pullers can't keep pace with the arrival of due jobs — **add more pullers.** This gap is the cheap part of the SLA budget; the image shows ~4 seconds spent claiming and enqueuing.
- <span style="color:#ffd27f"><strong>`started_at − picked_at` = broker wait + executor throughput.</strong></span> How long a *claimed* job sat in the queue before a free executor grabbed it. This is a limitation of the *executor* fleet, not the queue — the longer a job waits here, the more executors you need. **Add executors.**
- <span style="color:#ff8bd2"><strong>`completed_at − started_at` = execution time.</strong></span> How long the command itself ran. Compare it to the **historical average** for that task: a run dragging far past its usual time is a signal that the executor is **hung or the machine is dying** — something to alert on.

The whole SLA is the span `scheduled_at → started_at`, and it must stay under 30 seconds. With ~4s to claim+enqueue and ~1s of broker handoff, you've got roughly **25 seconds of slack** to read from the broker and begin — comfortable, and the ledger tells you the moment any stage starts eating into it.

### Why both `completed_at` and `failed_at`?

Because a single "done" flag is ambiguous and because the *duration* is a goldmine. `completed_at − started_at` is the execution time, and averaging it over history gives you two superpowers:

1. **Anomaly detection.** A job running far longer than its historical mean means a stuck process or a failing machine — exactly the kind of thing you want to catch *before* it cascades.
2. **Predictive scaling.** If you know the average runtime of the jobs about to come due, you can compute how many executors a future burst will need — which is precisely what the orchestrator does next.

And `failed_at` earns its column by **disambiguating failure from in-flight**: a row with `completed_at IS NULL` could be still running *or* dead. With a separate `failed_at`, "errored" and "still going" are distinct states, no guessing required.

> **Memory hook:** *the four timestamps are an SLA dashboard. `picked_at − scheduled_at` = puller lag (scale pullers); `started_at − picked_at` = queue+executor lag (scale executors); `completed_at − started_at` = runtime (compare to history to catch dying machines and to forecast capacity). Keep both completed_at and failed_at so "errored" and "still running" are never confused.*

---

## Section 7 — Handling bursts: the orchestrator and predictive scaling

**Question: at 9:00am sharp, fifty thousand "good morning" jobs all come due at once. If you only react *after* the queue backs up, you've already missed the SLA for the first wave. How do you have enough machines ready *before* the burst lands?**

Reactive autoscaling — "queue is deep, add machines" — is always a step behind, because spinning up an executor (especially a GPU box) takes minutes, and your SLA is 30 seconds. By the time the new machine boots, the wave has crested and broken. The fix is to **scale on the future, not the present** — and the future is sitting right there in the database, because *every job's `scheduled_at` is already known.*

<img src="../assets/distributed-task-scheduler/bursts-orchestrator.svg" alt="A diagram titled 'Handling bursts: predictive scaling via the orchestrator', showing the full architecture with a new control-plane component added. On the left a user hits the Scheduler / Task API which writes to the MySQL cylinder. Pullers read from MySQL (claim) and feed three broker tubes; executors pull from the broker and run jobs, writing back to MySQL. New: an ORCHESTRATOR box (purple/blue, control plane) at the bottom, with arrows reading FROM the MySQL DB and writing TO both the puller fleet and the executor fleet to add/remove machines. A callout over the orchestrator: 'reads the DB, counts how many jobs are due in the next ~10 minutes, and scales pullers and executors UP and DOWN ahead of demand — predictive (not reactive) scaling, because every scheduled_at is known in advance.' A formula sketch: future_due_count × avg_runtime ÷ executor_capacity ≈ executors_needed; future_due_count ÷ claim_rate ≈ pullers_needed. A second callout: 'reactive scaling is too slow — booting a GPU executor takes minutes, the SLA is 30s, so you must pre-warm.' On the right, a 'Key metrics to monitor (all SLA inputs)' panel listing: (1) tasks pulled per minute — puller throughput; (2) task wait time = started_at − scheduled_at — the SLA itself; (3) average time to completion = completed_at − started_at — runtime, the predictive-scaling input. Bottom takeaway: the orchestrator turns the known-in-advance schedule plus historical runtimes into a capacity forecast, so the fleet is already warm when the burst arrives." width="1000">

The <span style="color:#93c5fd"><strong>orchestrator</strong></span> is a control-plane process that does one thing: it **reads the future and provisions for it.** On a loop, it queries the DB for *"how many jobs come due in the next ~10 minutes?"*, multiplies by the historical average runtime (from the ledger), divides by per-executor capacity, and arrives at *"we'll need N executors and M pullers."* Then it scales both fleets up — *ahead* of the wave — and back down once it passes. Because the schedule is known minutes in advance, the orchestrator can have the machines warm and waiting when 9:00am hits.

This is <span style="color:#93c5fd"><strong>predictive scaling</strong></span>, and it's only possible because, unlike a web service reacting to unpredictable traffic, a scheduler **knows its own future load exactly.** The job is its own forecast.

### The metrics that matter

The orchestrator (and your on-call dashboard) watches three numbers, all of them straight off the timestamp ledger:

- **Tasks pulled per minute** — puller throughput. Falling behind the arrival rate means add pullers.
- **Task wait time** (`started_at − scheduled_at`) — *the SLA itself.* This is the number that must stay under 30 seconds; everything else is a leading indicator of it.
- **Average time to completion** (`completed_at − started_at`) — runtime. The input to the capacity forecast, and the tripwire for dying machines.

Every one of these is a *difference of two columns you already store.* The schema *is* the observability.

> **Memory hook:** *scale on the future, not the present. The orchestrator reads the DB for jobs due in the next ~10 minutes, multiplies by historical runtime, and pre-warms pullers and executors before the burst lands — predictive scaling, possible only because a scheduler knows its own future load. Watch three metrics: tasks pulled/min, wait time (the SLA), and avg completion time (the forecast input).*

---

## Section 8 — Recurring tasks: turning a cron rule into a stream of fixed jobs

**Question: everything so far handles "run once at an absolute time." But half the requirement was *recurring* — "every day at 3pm, forever." A cron rule has infinitely many future executions; you obviously can't insert infinite rows. So how do you reduce recurrence to the fixed-execution machinery we already built?**

The whole trick is to **never treat recurrence as special at execution time.** The executor should only ever see concrete, fixed, absolute-time jobs. So we split the schema into two tables and add a tiny generator.

<img src="../assets/distributed-task-scheduler/recurring.svg" alt="A diagram titled 'Recurring tasks: cron → absolute → the regular flow', showing two tables and the materialization logic between them. Top, a tasks table (the definitions): columns id, command, schedule (a cron expression like '0 15 * * *'), created_at. A sample row: id=2, command='send-report', schedule='0 15 * * *'. Bottom, a jobs table (the concrete executions): columns id, task_id (foreign key to tasks.id), scheduled_at, picked_at, started_at, completed_at/failed_at — i.e. exactly the fixed-job schema from earlier. Sample rows: id=20972 task_id=2 scheduled_at='2026-06-23 15:00', and id=20973 task_id=2 scheduled_at='2026-06-24 15:00', both with NULL timestamps, drawn linked back to task 2. An arrow labelled 'materialize' connects tasks to jobs. Two rules are written out as the generation logic. Rule 1, 'When a recurring task is CREATED': parse its cron expression, compute the next ~10 absolute execution times, and INSERT 10 rows into jobs. Rule 2, 'When a job is PICKED for execution': the puller also computes the NEXT execution after this one from the parent task's cron rule and INSERTs one new job row — pick one, add one — so the jobs table is a sliding window that always stays ~10 ahead and never runs dry, without ever storing infinite rows. A note: a fixed (one-shot) task simply inserts a single jobs row and no parent recurrence. The pipeline downstream (pullers, broker, executors, SLA ledger) is UNCHANGED — it only ever sees fixed jobs. Right side: the equation CRON → ABSOLUTE → regular flow, with the analogy 'like Google Calendar recurring meetings: the rule is stored once, but each occurrence is a concrete event you can move or cancel.' Takeaway: recurrence is a generator of fixed jobs, materialized as a rolling window, so the hard real-time path never has to understand cron." width="1000">

Two tables:

- <span style="color:#ffff99"><strong>`tasks`</strong></span> — the **definitions**. For a recurring task it stores the **cron expression** (`0 15 * * *`) and the command. This table holds the *rule*, not the executions.
- <span style="color:#ff8bd2"><strong>`jobs`</strong></span> — the **concrete executions**, with exactly the fixed-job schema from Section 2 (`scheduled_at`, `picked_at`, `started_at`, …) plus a `task_id` pointing back to the parent. This is the table the pullers query. **The pullers, broker, executors, and SLA ledger never change** — they only ever see fixed jobs.

A **fixed** task is the trivial case: insert one `tasks` row and one `jobs` row. Done.

A **recurring** task uses a rolling-window generator with two rules:

1. <span style="color:#ff8bd2"><strong>On creation</strong></span> — parse the cron expression, compute the **next ~10 absolute execution times**, and insert 10 rows into `jobs`. Now there's a finite, concrete buffer of upcoming runs.
2. <span style="color:#8aff8a"><strong>On pick</strong></span> — when a puller claims a recurring job, it *also* computes the **next** execution after that one from the parent's cron rule and inserts one fresh `jobs` row. **Pick one, add one.**

That second rule is the elegant part: the `jobs` table becomes a **sliding window** that always stays ~10 executions ahead and *never runs dry*, yet never stores more than a handful of future rows per task. You've represented an infinite schedule with a constant number of rows, and you've pushed the generation work onto the puller — which is already touching the row anyway, so it's nearly free.

The mental model collapses to one line: **cron → absolute → regular flow.** A recurring rule is just a machine that keeps emitting fixed jobs, and once a job is fixed, it flows through the exact same pipeline as everything else. If you've ever edited a single occurrence of a **Google Calendar recurring meeting** — the rule lives once, but each occurrence is a concrete, movable event — you already understand the model.

> **Memory hook:** *recurrence never reaches the executor. Split into `tasks` (the cron rule) and `jobs` (concrete fixed executions). On creation, materialize the next ~10 jobs; on every pick, compute the next one and insert it — "pick one, add one" — so `jobs` is a sliding window that's always ahead and never infinite. cron → absolute → the same regular flow. (Think Google Calendar recurring events.)*

---

## Section 9 — Scaling out: sharding the task store

**Question: one MySQL box absorbs the writes and the claim-queries for a long time — but eventually the `jobs` table and the pull traffic outgrow a single primary. How do you split it, and what do you shard *on* so a single puller can still claim a clean batch?**

We deferred sharding in Section 1 on purpose — *correct first, distribute only when a named bottleneck forces it.* The bottleneck, when it comes, is the same one the [multi-tiered datastore](21-high-throughput-multi-tiered-db.md) hit: one primary can't absorb all the writes (job materialization) and all the claim-reads. The cure is to **partition the `jobs` table across shards**, with each puller assigned to claim from a shard.

<img src="../assets/distributed-task-scheduler/sharding.svg" alt="A diagram titled 'Sharding the jobs store to scale out', showing the jobs table partitioned across multiple database shards with pullers bound to shards. Center: three database cylinders labelled Shard 0, Shard 1, Shard 2, each holding a slice of the jobs table. Above them, the sharding-key decision is laid out as a comparison. Option A, shard by task_id / a hash of the task (rejected-ish for time-skew): keeps all of one task's executions together but can pile a hot recurring task onto one shard. Option B, shard by a TIME BUCKET of scheduled_at (highlighted as the natural fit): e.g. shard = floor(scheduled_at to the minute) mod N, so jobs due at the same minute spread evenly across shards and each puller claims a bounded, balanced batch — but a single popular minute (9:00am) can still hotspot one bucket. Option C, hybrid — hash(task_id) for spread combined with a scheduled_at index within each shard (recommended): even distribution across shards AND an efficient scheduled_at <= now() range scan within a shard. Below, the puller-to-shard mapping: each puller is assigned one or more shards (drawn as puller boxes each pointing at one shard cylinder) and runs the same FOR UPDATE SKIP LOCKED claim within its shard, so the row-lock contention stays local to a shard and total claim throughput scales linearly with shard count. A note: keep the scheduled_at B-tree index per shard so the due-range scan stays log-time. A second note on rebalancing: use many logical shards mapped onto fewer physical nodes (the consistent-/logical-shard trick) so adding a node is moving a shard, not re-keying every row. Takeaway: shard the executions table for write+claim throughput; pick a key that spreads load AND preserves the per-shard scheduled_at range scan; bind pullers to shards to keep locking local." width="1000">

The design question is the **shard key**, and the brainstorm tension is real:

- **Shard by `task_id`** keeps all of one task's executions together — but a single hot recurring task (one that fires every minute) piles its entire stream onto one shard.
- **Shard by a time bucket of `scheduled_at`** (e.g. `floor(scheduled_at to minute) mod N`) spreads jobs due at the same minute evenly across shards, so each puller claims a balanced batch — but one wildly popular minute (9:00am again) can still hotspot a bucket.
- <span style="color:#ffff99"><strong>Hybrid (recommended)</strong></span> — distribute rows across shards by `hash(task_id)` for even spread, *and* keep a `scheduled_at` B-tree index **within** each shard so the `scheduled_at <= now()` range scan stays log-time locally. You get balanced shards *and* an efficient per-shard due-query.

Each <span style="color:#8aff8a"><strong>puller is bound to one or more shards</strong></span> and runs the same `FOR UPDATE SKIP LOCKED` claim *within its shard*. That keeps row-lock contention **local to a shard** — pullers on shard 0 never contend with pullers on shard 1 — so total claim throughput scales roughly linearly with the shard count. And to make rebalancing cheap, use the [logical-shard trick from S3](20-high-throughput-system-s3.md): map many *logical* shards onto fewer *physical* nodes, so adding a node is "move a shard," not "re-key every row."

> **Memory hook:** *shard the `jobs` (executions) table only when one primary can't take the writes+claims. Pick a key that both spreads load and preserves the per-shard `scheduled_at` range scan — `hash(task_id)` across shards with a `scheduled_at` index inside each. Bind each puller to a shard so SKIP LOCKED contention stays local and claim throughput scales with shard count. Use logical shards for cheap rebalancing.*

---

## Where this leaves us: the complete task scheduler

We started with a single `cron` line on one box and grew it, one named bottleneck at a time, into a distributed scheduler that fires hundreds of thousands of jobs inside a 30-second deadline. Every component earned its place by solving a problem the previous step created. Here is the whole machine in one map.

<img src="../assets/distributed-task-scheduler/final-map.svg" alt="The complete distributed task scheduler architecture in one map, showing all components and the colored paths between them. Left: a user (and upstream services) call a Task API / Scheduler that performs the WRITE path (pink) — registering a task: one INSERT into the tasks table, plus materializing the first ~10 jobs for a recurring task. Center: the relational store (yellow cylinder), now two tables — tasks (the cron definitions) and jobs (the concrete fixed executions, sharded across Shard 0..N), with a B-tree index on scheduled_at per shard. The CLAIM path (green): a fleet of lightweight pullers, each bound to a shard, run SELECT … WHERE scheduled_at <= now()+buffer AND picked_at IS NULL ORDER BY scheduled_at LIMIT 10 FOR UPDATE SKIP LOCKED, set picked_at, and on pick of a recurring job also INSERT the next execution (pick-one-add-one). The async plane (blue): pullers drop claimed jobs into a broker (SQS/RabbitMQ) tube; queue depth is the back-pressure signal. The EXECUTE path (orange): a fleet of heavy, specialized executors (GPU / high-CPU / high-mem) pull from the broker, write started_at (satisfying the SLA), run the command, and write completed_at or failed_at. The control plane (blue/purple): an Orchestrator reads the jobs table for load due in the next ~10 minutes and historical runtimes, then scales pullers and executors up and down ahead of demand (predictive scaling). A legend ties the colors to the planes: pink = register/write, green = claim/read, yellow = durable store + invariants (exactly-once pick via row locks), blue = broker + orchestrator (async/control), orange = heavy execution, red = the hazards each plane defends against (double-pick, puller starvation, missed SLA, status drift). One sentence under the map: a task scheduler is a durable timestamp-indexed table of jobs, claimed atomically and in bounded batches by cheap pullers via FOR UPDATE SKIP LOCKED, run by heavy executors behind a broker, scaled predictively by an orchestrator that reads the known-in-advance future, with recurrence reduced to a rolling window of fixed jobs." width="1280">

The components, and the one idea each is built around:

| Component | What it is | The one idea |
| --- | --- | --- |
| <span style="color:#ff8bd2"><strong>Task API</strong></span> | Front door; writes definitions | Registering a task is one `INSERT` |
| <span style="color:#ffff99"><strong>Relational store</strong></span> | `tasks` + `jobs`, timestamp-indexed, sharded | Chosen for the *atomic claim* primitive, not the brand |
| <span style="color:#8aff8a"><strong>Pullers</strong></span> | Lightweight, many, shard-bound | `FOR UPDATE SKIP LOCKED LIMIT N` = disjoint, bounded, wait-free claim |
| <span style="color:#93c5fd"><strong>Broker</strong></span> | SQS/RabbitMQ between the fleets | Decouple claim-rate from run-rate; queue depth is the load signal |
| <span style="color:#ffd27f"><strong>Executors</strong></span> | Heavy, specialized, sized to work | Never let the puller execute; size hardware to the job |
| <span style="color:#93c5fd"><strong>Orchestrator</strong></span> | Reads the future, scales both fleets | A scheduler knows its own load in advance — scale predictively |

Read the colors and they narrate the design: a <span style="color:#ff8bd2"><strong>pink write</strong></span> registers a task into a <span style="color:#ffff99"><strong>yellow store</strong></span>, <span style="color:#8aff8a"><strong>green pullers</strong></span> claim due rows atomically and drop them on a <span style="color:#93c5fd"><strong>blue broker</strong></span>, <span style="color:#ffd27f"><strong>orange executors</strong></span> run them and stamp the SLA clock, and a <span style="color:#93c5fd"><strong>blue orchestrator</strong></span> reads the known future to keep the fleet warm. That is a distributed task scheduler.

> **Memory hook:** *a task scheduler = a timestamp-indexed table of jobs, claimed atomically in bounded batches by cheap pullers via `FOR UPDATE SKIP LOCKED`, run by heavy specialized executors behind a broker, scaled predictively by an orchestrator that reads the known-in-advance future — with recurrence reduced to a rolling window of fixed jobs so the hot path never understands cron.*

---

# Part 2 — A Message Broker on a Relational Database

In Part 1 we dropped claimed jobs into a **broker** — SQS, RabbitMQ — and treated it as a magic tube that decouples pullers from executors. Now we open the tube. **We are going to *be* SQS:** build a message broker on nothing but a relational table.

**Question: a broker has to accept a message now, hand it to exactly one of many competing consumers later, and let that consumer mark it done — at high throughput, in order, and never delivering the same message to two consumers at once. You've already built 90% of this. Where have you seen it?** You've seen it in the **puller**. A broker is the scheduler's "claim a row that no one else is working on" half, generalized from "due jobs" to "any message." The whole of Part 2 is that one realization plus two new ideas a *general* queue needs that the puller didn't: a **receipt handle** (so the right consumer deletes the right message, securely) and a **visibility timeout** (so a message a crashed consumer never finished comes back).

> **Memory hook:** *a message broker on a relational DB is the scheduler's puller generalized: a `messages` table, claimed by competing consumers with `FOR UPDATE SKIP LOCKED` (read → invisible to others), acknowledged by a soft delete. The two new pieces are the receipt handle (capability to delete) and the visibility timeout (resurface what a dead consumer never finished). This is, almost literally, how a Postgres-backed queue works.*

---

### The brief: PUT, GET, DELETE — and the four requirements

**Question: before any schema — what does a queue actually promise its users, and what is the smallest API that delivers it?**

<img src="../assets/distributed-task-scheduler/broker-brief.svg" alt="The brief for a message broker on a relational DB, titled 'We are SQS now'. Left, four requirements: (1) FIFO — messages come out in the order they went in; (2) a consumer reads a message and then deletes it (read-then-acknowledge, a two-step consume); (3) high throughput — many consumers pulling concurrently; (4) when one consumer reads a message it must become invisible to all other consumers, so no message is ever processed twice concurrently. Right, the three-endpoint API: PUT /messages/q — enqueue a message (a single INSERT); GET /messages/q?n=10 — receive up to n messages, atomically claiming them; DELETE /messages/q/{receipt_handle} — acknowledge/remove a message the caller has finished. A callout ties it back to Part 1: this is the scheduler's 'task puller' generalized — replace 'due jobs' with 'any message' and the claim machinery is identical. A second note: Kafka is a different model (a log with committed offsets); this is a competing-consumers queue with per-message claim and acknowledge — which is exactly what a Postgres-backed queue does behind the scenes." width="1000">

A queue makes four promises, and they line up almost exactly with the scheduler's requirements:

1. <span style="color:#ffff99"><strong>FIFO</strong></span> — messages come out roughly in the order they went in.
2. <span style="color:#8aff8a"><strong>Read, then delete.</strong></span> Consuming is **two steps**: a consumer *receives* a message, processes it, and only then *deletes* it. (The delete is the acknowledgement — "I'm done, don't give this to anyone else.")
3. **High throughput** — many consumers pulling at once.
4. <span style="color:#ff8a8a"><strong>Read makes it invisible.</strong></span> The instant one consumer receives a message, it must vanish from every other consumer's view, so two workers never process the same message simultaneously.

That maps onto a tiny three-verb API — <span style="color:#ff8bd2"><strong>`PUT /messages/q`</strong></span> to enqueue, <span style="color:#8aff8a"><strong>`GET /messages/q?n=10`</strong></span> to receive a batch, and <span style="color:#ff8bd2"><strong>`DELETE /messages/q/{receipt_handle}`</strong></span> to acknowledge. Hold the question of what that `receipt_handle` is; it's the one genuinely new design decision, and it gets its own section.

> **Memory hook:** *a queue promises FIFO, read-then-delete, high throughput, and "read → invisible to others." The API is three verbs: PUT (enqueue), GET (receive a batch, atomically claim), DELETE (acknowledge by receipt handle). Requirement #4 is the same exactly-one-claimer rule the puller already solved.*

---

### Storage and PUT: the messages table

**Question: what columns does a queue row need — and how is "enqueue" anything more than an `INSERT`?**

It isn't. `PUT` is one `INSERT`, exactly like registering a task in Part 1. The schema is the scheduler's `jobs` table with two renamed columns and one new one.

<img src="../assets/distributed-task-scheduler/broker-architecture.svg" alt="The messages table schema and the broker topology, titled 'Very similar to the distributed task scheduler'. Top, the messages table with six columns: id (AUTO_INCREMENT — gives a monotonic FIFO order), msg (the payload), created_at (timestamp, the FIFO sort key), picked_at (NULL until a consumer claims it — the visibility flag), deleted_at (NULL until acknowledged — soft delete), and receipt_handle (a random UUID written when the message is claimed, e.g. 'uuid-1'). Bottom, the architecture: a MySQL cylinder feeds a Broker API fleet (stacked rectangles), which fans messages out to Consumer 1, Consumer 2, Consumer 3. The three endpoints are labelled on the Broker API: PUT /messages/q (enqueue = one INSERT, pink), GET /messages/q?n=10 (receive a batch, green), DELETE /messages/q/{receipt-handle} (acknowledge, pink). A note: the API tier is stateless and the messages table is the single source of truth — any Broker API instance can serve any consumer, just like the stateless API tier in S3 and the scheduler. Takeaway: enqueue is an INSERT; everything interesting happens on GET (claim) and DELETE (ack)." width="1100">

The columns:

- <span style="color:#ffff99"><strong>`id`</strong></span> — `AUTO_INCREMENT`. A monotonically increasing id *is* a FIFO order for free, and it's the stable handle for a row.
- <span style="color:#ff8bd2"><strong>`msg`</strong></span> — the opaque payload.
- <span style="color:#ffff99"><strong>`created_at`</strong></span> — the timestamp we **order by** to get FIFO (oldest first).
- <span style="color:#ff8bd2"><strong>`picked_at`</strong></span> — `NULL` until a consumer claims it. This is the **visibility flag**: `picked_at IS NULL` means "available."
- <span style="color:#ff8bd2"><strong>`deleted_at`</strong></span> — `NULL` until acknowledged. We **soft-delete** (more below).
- <span style="color:#ffff99"><strong>`receipt_handle`</strong></span> — a random UUID stamped on the row *when it's claimed*, returned to the receiving consumer, and later required to delete it.

`picked_at` and `deleted_at` are the scheduler's lifecycle timestamps wearing queue clothes. Everything interesting happens on `GET` and `DELETE`.

> **Memory hook:** *the messages table is `id` (AUTO_INC = free FIFO), `msg`, `created_at` (the FIFO sort key), `picked_at` (the visibility flag, NULL = available), `deleted_at` (soft-delete ack), `receipt_handle` (UUID stamped on claim). PUT is just an INSERT; the Broker API tier is stateless with the table as the single source of truth.*

---

### GET: receive with the same SKIP LOCKED claim

**Question: a `GET` arrives asking for 10 messages while five other consumers ask at the same instant. Each consumer must walk away with its own messages, none shared, nobody waiting. What query does that?**

The one you already wrote in Part 1 — verbatim, with `tasks` swapped for `messages`:

```sql
SELECT * FROM messages
WHERE picked_at IS NULL
  AND deleted_at IS NULL
ORDER BY created_at ASC          -- FIFO: oldest first
LIMIT :n
FOR UPDATE SKIP LOCKED;
```

Then, in the **same transaction**, claim the rows you got:

```sql
UPDATE messages
SET picked_at = now(),
    receipt_handle = uuid()       -- mint the capability to delete
WHERE id IN (:ids);
```

<img src="../assets/distributed-task-scheduler/broker-consume.svg" alt="The two-step consume lifecycle, drawn as two lanes, titled 'GET = claim, DELETE = acknowledge'. Left lane, RECEIVE (green): the GET claim query — SELECT * FROM messages WHERE picked_at IS NULL AND deleted_at IS NULL ORDER BY created_at ASC LIMIT n FOR UPDATE SKIP LOCKED — followed in the SAME transaction by UPDATE messages SET picked_at = now(), receipt_handle = uuid() WHERE id IN (...). Annotations: 'picked_at IS NULL AND deleted_at IS NULL = only available, un-acked messages'; 'ORDER BY created_at ASC = FIFO'; 'FOR UPDATE SKIP LOCKED = each consumer gets disjoint messages, no double-delivery, no waiting'; 'SELECT + UPDATE are ONE transaction so the claim is atomic — read makes it invisible to others'; 'the UPDATE mints a fresh receipt_handle and returns it to this consumer only'. Right lane, DELETE (pink): the acknowledge step — DELETE /messages/q/{receipt_handle} runs UPDATE messages SET deleted_at = now() WHERE receipt_handle = :rh (a SOFT delete, not a physical row removal; a background GC reclaims space later). A security contrast box in the middle, red vs yellow: deleting by raw id (DELETE /messages/q/42) is INSECURE — ids are sequential AUTO_INCREMENT, so any caller can guess and delete another consumer's message; deleting by receipt_handle (an unguessable random UUID handed only to the consumer that received the message) is a capability — only the rightful consumer can ack. Takeaway: GET claims with SKIP LOCKED exactly like the scheduler's puller; DELETE is a soft delete gated by an unguessable receipt handle." width="1120">

This is the puller, line for line. `picked_at IS NULL AND deleted_at IS NULL` selects the *available, un-acknowledged* messages; `ORDER BY created_at ASC` gives FIFO; `FOR UPDATE SKIP LOCKED` hands each concurrent consumer a **disjoint** batch with **no waiting** (the whole argument from [Part 1, Section 5](#section-5--the-pull-query-how-many-pullers-claim-rows-without-colliding) applies unchanged); and because the `SELECT` and `UPDATE` ride in **one transaction**, the claim is atomic — the moment a row's `picked_at` is set, requirement #4 is satisfied and the message is invisible to everyone else. The new wrinkle is that the claiming `UPDATE` also **mints a fresh `receipt_handle`** and returns it to this consumer alone.

> **Memory hook:** *GET is the puller's claim verbatim: `SELECT … WHERE picked_at IS NULL AND deleted_at IS NULL ORDER BY created_at ASC LIMIT n FOR UPDATE SKIP LOCKED`, then in the same transaction `UPDATE picked_at = now(), receipt_handle = uuid()`. One transaction = atomic claim = "read makes it invisible." FIFO comes from ORDER BY created_at.*

---

### DELETE: the receipt handle, a security decision worth remembering

**Question: the consumer finished its message and calls `DELETE`. The obvious endpoint is `DELETE /messages/q/{id}`. Why is that quietly dangerous — and what's the fix?**

The danger is that `id` is a sequential `AUTO_INCREMENT`. Anyone who can call the API can pass `42`, `43`, `44`… and **delete messages they never received** — including messages another consumer is actively processing. Identity isn't proven; the id is just guessable public knowledge.

The fix is a <span style="color:#ffff99"><strong>receipt handle</strong></span>: when a consumer receives a message, the claiming `UPDATE` stamps the row with a **random, unguessable UUID** and returns it *only to that consumer*. `DELETE` is then keyed on the receipt handle, not the id:

```sql
UPDATE messages SET deleted_at = now()
WHERE receipt_handle = :receipt_handle;
```

The receipt handle is a <span style="color:#ffff99"><strong>capability</strong></span> — "whoever holds this token may delete this one message." It's unforgeable because it's random and temporary (minted per receive), so only the consumer that actually read the message can acknowledge it. **This is exactly how real SQS receipt handles work**, and it's the one design decision from Part 2 worth memorizing. (The alternative is to store *which consumer* claimed the message — an `owner_id` you check on delete, like a lock owner in the [lock manager](07-distributed-lock-manager.md). The receipt handle is nicer because the server stays stateless about consumer identity: the token carries the proof.)

And the delete is a <span style="color:#ff8bd2"><strong>soft delete</strong></span> — set `deleted_at = now()` rather than physically removing the row. It's instant and cheap (no page rewrite), it keeps a brief history for de-duplication and audit, and a background GC/compaction reclaims the space later — the same [log-structured](19-storage-engine-fast-kv-db.md) "a delete is just another write" trick from the KV engine.

> **Memory hook:** *never delete by raw id — sequential ids are guessable, so anyone could ack anyone's message. Delete by receipt handle: a random UUID minted on receive and returned only to the receiver — a capability token that proves "I'm the one who read this." (Alternative: store an owner_id like a lock owner.) The delete is soft (`deleted_at = now()`), GC'd later.*

---

### Visibility timeout: what if a consumer reads but never deletes?

**Question: a consumer receives a message, sets its `picked_at`, and then crashes — or just hangs — before it ever calls `DELETE`. That row now has `picked_at` set and `deleted_at` still `NULL`: invisible to everyone, acknowledged by no one. The message is stranded forever. How do you get it back?**

This is the gap a *general* queue has that the scheduler glossed over. A claim that's never acknowledged must not strand the message. The cure is a <span style="color:#ffff99"><strong>visibility timeout</strong></span>: a claim is only good for a while (say **10 minutes**); if the consumer hasn't deleted the message by then, it **resurfaces** for someone else.

<img src="../assets/distributed-task-scheduler/broker-visibility-timeout.svg" alt="The visibility timeout, drawn as a timeline plus a cron job, titled 'Read but not deleted → resurface'. Timeline: at t0 a consumer receives a message — picked_at is set, the message goes invisible. The consumer then crashes (or hangs) before calling DELETE, so deleted_at stays NULL. A shaded 10-minute visibility window extends from t0. At t0+10min, a sweeper detects the message is still claimed-but-not-acked and resets it to available. Below, the cron job that does it: UPDATE messages SET picked_at = NULL WHERE picked_at < now() - 10min AND deleted_at IS NULL — clears the claim on any message picked more than 10 minutes ago and not yet acknowledged, so it becomes visible again and, because it has an old created_at, returns to the FRONT of the FIFO queue. A note: this gives AT-LEAST-ONCE delivery — a slow-but-alive consumer might finish after the timeout, so the message can be processed twice; consumers must be idempotent. Contrast callout: in the happy path the consumer DELETEs within the window (soft delete, gone for good); only stranded messages are swept. Takeaway: the visibility timeout turns a crashed consumer into a redelivery instead of a lost message." width="1080">

A small <span style="color:#93c5fd"><strong>cron job</strong></span> per queue does it:

```sql
UPDATE messages
SET picked_at = NULL
WHERE picked_at < now() - interval '10 min'
  AND deleted_at IS NULL;
```

It clears the claim on any message picked more than 10 minutes ago and not yet acknowledged. With `picked_at` back to `NULL`, the message is **available again** — and because its `created_at` is old, it sorts to the **front** of the FIFO queue and gets handed to the next consumer promptly. The crashed consumer's work isn't lost; it's *redelivered*.

The honest consequence: this is <span style="color:#ff8a8a"><strong>at-least-once delivery</strong></span>. A consumer that was merely slow (not dead) might finish *after* the timeout fired, so the message can be processed twice. So **consumers must be idempotent** — processing the same message twice must be safe. (Want exactly-once? That's a much harder promise and a different post — which is why Part 1 scoped out retries.)

> **Memory hook:** *a claimed-but-never-acked message is stranded (picked_at set, deleted_at NULL, invisible forever). The visibility timeout fixes it: a cron resets `picked_at = NULL WHERE picked_at < now() - 10min AND deleted_at IS NULL`, resurfacing it to the front of the queue. Net guarantee: at-least-once delivery → consumers must be idempotent.*

---

### How this differs from Kafka — and "just use Postgres"

**Question: you keep hearing "use Kafka for this." Is what we built a Kafka? And is a database-backed queue a real thing or a toy?**

What we built is a <span style="color:#ffff99"><strong>competing-consumers queue</strong></span>: every message is a row that exactly one consumer **claims** and then **acknowledges** (deletes). <span style="color:#93c5fd"><strong>Kafka is a different animal</strong></span> — a partitioned, append-only **log**. Kafka consumers don't claim and delete individual messages; they read a partition in order and **commit an offset** ("I've consumed up to position 4,182"). The broker never hands one message to "whichever consumer is free," never locks a message, and never redelivers a single message on its own — parallelism comes from splitting *partitions* across a consumer group, not from competing over rows. Queue semantics (per-message ack, redelivery, visibility timeout) are our model; **log semantics (ordered replay, offset commits, retention)** are Kafka's. Different tools for different shapes of problem.

And yes — a relational database makes a genuinely good queue at modest-to-serious scale. **This is essentially what a Postgres-backed queue does behind the scenes**: `SELECT … FOR UPDATE SKIP LOCKED` over a table, a soft-delete ack, a sweeper for timeouts. PostgreSQL and MySQL shipped `SKIP LOCKED` *specifically* to make this pattern fast, and plenty of production systems run queues straight on their existing database rather than operating a separate broker. You only graduate to a dedicated broker when throughput, retention, or fan-out outgrow what one (or a sharded) table can serve — and when you do, you shard the `messages` table exactly the way [Part 1, Section 9](#section-9--scaling-out-sharding-the-task-store) shards `jobs`.

> **Memory hook:** *queue (claim + ack + redeliver per message) ≠ log (Kafka: ordered partitions, offset commits, no per-message lock or redelivery). A relational table IS a real queue — `SKIP LOCKED` + soft-delete ack + a timeout sweeper is exactly how a Postgres-backed queue works. Reach for a dedicated broker only when throughput/retention/fan-out outgrow the table; then shard it like Part 1's jobs.*

### Where Part 2 leaves us

We took the broker we'd been treating as magic and discovered it was the puller all along — a `messages` table, claimed by competing consumers with `FOR UPDATE SKIP LOCKED`, acknowledged by a soft delete. The two ideas a general queue adds over the scheduler are both small and both essential: a **receipt handle** so the right consumer deletes the right message without trusting a guessable id, and a **visibility timeout** so a message a dead consumer never finished comes back instead of vanishing.

| Verb | What it is | The one idea |
| --- | --- | --- |
| <span style="color:#ff8bd2"><strong>PUT</strong></span> | Enqueue | Just an `INSERT` |
| <span style="color:#8aff8a"><strong>GET</strong></span> | Receive a batch | The puller's `SKIP LOCKED` claim → read makes it invisible |
| <span style="color:#ffff99"><strong>receipt handle</strong></span> | Per-receive UUID | A capability to delete — unguessable, proves identity |
| <span style="color:#ff8bd2"><strong>DELETE</strong></span> | Acknowledge | Soft delete by receipt handle, GC'd later |
| <span style="color:#93c5fd"><strong>visibility timeout</strong></span> | Sweeper cron | Resurface unacked claims → at-least-once delivery |

> **Memory hook:** *a message broker on a relational DB = the scheduler's puller plus two pieces: receipt handle (capability to ack) and visibility timeout (resurface what a crashed consumer never finished). PUT inserts, GET claims with SKIP LOCKED, DELETE soft-deletes by receipt handle. It's a queue, not a log — and it's exactly what a Postgres-backed queue is under the hood.*

---

## Further reading

The design here is derived from first principles, but every piece has prior art. To go further:

- **[dkron](https://dkron.io/)** — an open-source distributed cron; a clean reference for the store-and-claim architecture at cluster scale.
- **[AWS EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html)** — a managed, one-time-and-recurring scheduler that fires millions of invocations; the production embodiment of this post.
- **["SELECT … FOR UPDATE SKIP LOCKED" — PostgreSQL docs](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)** — the locking clause that makes a relational table into a concurrent work queue (Parts 1 *and* 2).
- **[Quartz Scheduler](https://www.quartz-scheduler.org/)** — the classic JVM job scheduler; good for the schema and misfire-handling vocabulary.
- **[Amazon SQS — visibility timeout & receipt handles](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)** — the production queue whose semantics Part 2 rebuilds: receive, receipt handle, visibility timeout, at-least-once delivery.
- **["Choose Postgres queue technology" / `SKIP LOCKED` queue pattern](https://www.2ndquadrant.com/en/blog/what-is-select-skip-locked-for-in-postgresql-9-5/)** — why a relational table makes a perfectly good message queue, and how `SKIP LOCKED` powers it.
- **[Putting Apache Kafka To Use: A Practical Guide to Building an Event Streaming Platform — Jay Kreps](https://www.confluent.io/blog/event-streaming-platform-1/)** — the *log* model Part 2 contrasts against the competing-consumers *queue*.
- **The companion posts in this handbook:** the [distributed lock manager](07-distributed-lock-manager.md) (mutual exclusion and leader election), [distributed id generators](08-distributed-id-generators.md) (time-sortable ids for shard keys), the [multi-tiered datastore](21-high-throughput-multi-tiered-db.md) (why relational-first, then shard), and [Airflow](24-high-throughput-airflow.md) (orchestrating *dependent* DAGs, the sibling problem).
- **Designing Data-Intensive Applications, by Martin Kleppmann** — partitioning, transactions, and the consistency tradeoffs underneath all of this.
