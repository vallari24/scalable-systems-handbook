# Apache Airflow Internals, From First Principles: How a Workflow Orchestrator Actually Works

Sooner or later every backend grows a pipeline: *do step A, then B, then run C and D in parallel, but only start E once **both** C and D finish — and if anything fails, retry it, and let me see where it's stuck.* A `cron` job can't express that (it runs things on a clock, blind to whether the last step succeeded). A plain queue can't either (it hands out one unit of work at a time and has no idea what a *graph* is, or who's done). What you actually need is something that **remembers the state of the whole graph** and drives it forward. That thing is a **workflow orchestrator**, and the most widely used one is **Apache Airflow**.

This post builds Airflow up from nothing. We start with the four words you need to read any DAG, write a real one a piece at a time, learn how data flows between steps and how failures recover — and *only then* open the hood to see how Airflow actually turns your Python file into processes running on machines, in the right order, surviving crashes. By the end you'll understand it well enough to configure it confidently, debug "where is my pipeline stuck?", and see why it's a genuine distributed system.

There's a single idea underneath everything, and it's worth holding onto from the start:

> **The one idea:** *Airflow puts **all state in one database**, then lets stateless "brains" (schedulers) and stateless "muscle" (workers) coordinate through it. Durability, retries, high availability, and scale all fall out of that one decision — and so does the fan-in that a cron job or a queue could never express.*

We'll earn that sentence step by step. Don't worry about it yet.

---

# Part I — The mental model: how you *use* Airflow

## Section 1 — The four words: task, operator, DAG, run

Before any code, get these four nouns straight. Everything else is built from them.

- A **task** is **one unit of work** — "run this query," "send this email," "copy this file." It's the smallest thing Airflow schedules.
- An **operator** is a **template for a kind of task**. `BashOperator` runs a shell command; `PythonOperator` calls a Python function; there are operators for SQL, HTTP, and hundreds of external systems. You don't write a task from scratch — you *configure an operator*, and that gives you a task.
- A **DAG** (Directed Acyclic Graph) is **the graph of tasks plus the dependency edges between them**. "Directed" = edges have a direction (A then B). "Acyclic" = no loops (a task can't end up waiting on itself), which guarantees a valid running order always exists. The DAG is the *recipe*: what runs, and in what order.
- A **DAG Run** and a **Task Instance** are what you get when that recipe actually *executes*. A **DAG Run** is one execution of the whole DAG (today's run vs. yesterday's run). A **Task Instance** is one task inside one run — e.g. `load` for the June 16th run. The DAG is reusable and stateless; the *runs* carry the state (`running`, `success`, `failed`).

That last distinction trips up everyone, so make it concrete: the DAG is a **blueprint** you write once; each time it fires, Airflow stamps out a fresh set of **Task Instances** with their own independent state. Re-running yesterday doesn't touch today.

<img src="../assets/airflow/airflow-definition-runtime.svg" alt="Two stacked layers showing the difference between an Airflow definition and its runtime. TOP — DEFINITION LAYER, parsed from Python (blue): an Operator/@task box labelled 'a template for work — calling it creates a Task' with an 'instantiate' arrow into a DAG blueprint named daily_report, drawn as five connected task boxes: extract → (fan-out) transform_sales and transform_signups → (fan-in) load → email_report. A note says this is a reusable, stateless recipe. A 'trigger twice' arrow points down. BOTTOM — RUNTIME LAYER, rows with state (pink): two separate DagRun boxes, 'run · 2026-06-16' and 'run · 2026-06-17', each containing the same task names but with their own independent state markers (extract success, transform running, load waiting). A caption notes each TaskInstance is identified by dag_id + run_id + task_id, and that retries and status change only that one run. Bottom banner: definitions describe work; DagRuns and TaskInstances are the stateful executions." width="1180">

> **Memory hook:** *Operator = template for a kind of work. Configure one → you get a **task**. Wire tasks with dependency edges → you get a **DAG** (the reusable recipe, no loops). Run the DAG → you get a **DAG Run** (one whole execution) made of **Task Instances** (one task in one run), and those are the things that carry state.*

---

## Section 2 — Your first DAG: writing tasks and wiring order

A DAG is just a Python file in a folder Airflow watches. Here is the smallest one that does anything — the orchestrator's "hello world":

```python
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from datetime import datetime

def greet():
    print("Hello from a Python task!")

with DAG(
    dag_id="hello_world",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",        # fire once a day
    catchup=False,
) as dag:
    print_date = BashOperator(task_id="print_date", bash_command="date")
    say_hello  = PythonOperator(task_id="say_hello", python_callable=greet)

    print_date >> say_hello    # the edge: run print_date, THEN say_hello
```

Two tasks, one dependency. The `>>` operator is how you draw an edge: `a >> b` means "`b` runs only after `a` succeeds." That's the whole grammar. Read `>>` as *"then."*

Now scale up to something real but still universal — a **daily report pipeline**, the example we'll carry through the rest of the post. It pulls yesterday's data, cleans two slices of it in parallel, loads the result, and emails a summary:

```python
with DAG(
    dag_id="daily_report",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",
    catchup=False,
) as dag:
    extract           = PythonOperator(task_id="extract",           python_callable=pull_yesterdays_rows)
    transform_sales   = PythonOperator(task_id="transform_sales",   python_callable=clean_sales)
    transform_signups = PythonOperator(task_id="transform_signups", python_callable=clean_signups)
    load              = PythonOperator(task_id="load",              python_callable=load_warehouse)
    email_report      = PythonOperator(task_id="email_report",      python_callable=send_summary)
    refresh_dashboard = PythonOperator(task_id="refresh_dashboard", python_callable=refresh_bi)

    # the gate: load runs only after BOTH transforms succeed
    extract >> [transform_sales, transform_signups] >> load >> email_report

    # the independent tail: hangs off extract, never blocks load
    extract >> refresh_dashboard
```

Read the two dependency lines and the whole shape is right there:

- `extract >> [transform_sales, transform_signups]` is a **fan-out**: one task splits into two that run in parallel.
- `[transform_sales, transform_signups] >> load` is a **fan-in**: `load` waits for *both* transforms before it starts. **This is the exact thing cron and queues can't express** — "run E only after C and D both finish" — and in Airflow it's a single line.
- `extract >> refresh_dashboard` is an **independent branch**: the dashboard refresh runs off `extract` and never blocks the report.

<img src="../assets/airflow/airflow-example-dag.svg" alt="The daily_report DAG drawn as a graph, top section the critical path and bottom section the independent tail. TOP — REPORT GATE: a daily-trigger box starts a DagRun, then extract, then a fan-out (labelled) into two parallel tasks transform_sales and transform_signups, which fan-in (labelled, highlighted green) into load, then email_report. A green callout reads 'default trigger rule: all_success — load waits for BOTH transforms.' BOTTOM — INDEPENDENT TAIL: extract also connects to refresh_dashboard, with a note that it runs in parallel and does not block the report. Caption: the DAG is the policy — what may run now, what must wait, and what may proceed independently." width="1180">

### Trigger rules: the knob behind fan-in

Why does `load` wait for *both* transforms? Because the default rule for every task is "run only when **all** my upstreams succeeded" — Airflow calls this `trigger_rule="all_success"`. That default *is* the fan-in. But it's a knob, and the alternatives are where a lot of real-world behavior lives:

| `trigger_rule` | the task runs when… | use it for |
| --- | --- | --- |
| `all_success` (default) | every upstream succeeded | the gate — both transforms must pass before `load` |
| `all_done` | every upstream finished (success *or* fail) | a cleanup task that must run no matter what |
| `one_success` | any one upstream succeeded | "data is ready from *either* source A or source B" |
| `one_failed` | any upstream failed | fire an alert the moment *any* step fails |
| `none_failed` | all upstreams succeeded or were skipped | proceed past optional branches that got skipped |

So "load only if both transforms pass" is the default `all_success`. "Alert the moment any step fails" is a *separate* task wired to the same upstreams with `one_failed`. The fan-in semantics that would cost you a hand-built tracking table on a bare queue are a single keyword here.

### `default_args`: configure once, inherit everywhere

Repeating `retries=2, retry_delay=...` on every task is noise. `default_args` is a dict that every task in the DAG inherits (and may override), while the `DAG(...)` call itself takes the DAG-wide settings:

```python
from datetime import timedelta

default_args = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=30),
}

with DAG("daily_report", default_args=default_args, schedule="@daily",
         start_date=datetime(2026, 1, 1), catchup=False,
         max_active_runs=1, max_active_tasks=16) as dag:
    ...
```

`max_active_runs` caps how many runs of this DAG execute at once; `max_active_tasks` caps concurrent tasks within one run — your first **backpressure knobs**, declared right in the DAG.

> **Memory hook:** *a DAG is a Python file. Configure operators into tasks, wire them with `>>` ("then"); a list `[...]` fans out and fanning back in is the fan-in. The fan-in is just the **default `all_success` trigger rule** — `one_failed`/`one_success`/`all_done` cover alerts, either-or, and cleanup. `default_args` configures every task once; `max_active_runs`/`max_active_tasks` are your first backpressure knobs.*

---

## Section 3 — Passing data between tasks: XCom

So far tasks only pass *control* ("you may start now"). But `transform_sales` needs to know **what `extract` produced** — a file path, a row count, a date. Here's the catch that forces a real mechanism: **Airflow tasks run in separate processes, often on separate machines.** They can't just share a Python variable, because there is no shared memory between them.

The answer is **XCom** ("cross-communication"). A task **pushes** a small value; Airflow stores it (in its database, keyed by run + task); a downstream task **pulls** it:

```python
def extract(**ctx):
    path = dump_to_staging()        # e.g. "s3://staging/2026-06-16/orders.parquet"
    return path                     # a task's return value is auto-pushed to XCom

def transform_sales(**ctx):
    path = ctx["ti"].xcom_pull(task_ids="extract")   # pull what extract returned
    return aggregate(path)
```

`extract` returns the path of the file it staged; Airflow stashes that string in XCom; `transform_sales` pulls it and works on exactly that file. **This is how the output of one task becomes the input of the next.**

The critical rule: **XCom is for small values — paths, IDs, counts, flags — never the data itself.** If `extract` produces a gigabyte of rows, those bytes go to object storage (S3/GCS); XCom carries only the *pointer*. Stuffing the actual payload through XCom bloats Airflow's database and slows every scheduler query — a classic beginner mistake.

<img src="../assets/airflow/airflow-xcom-data-flow.svg" alt="Two parallel paths between an upstream task and a downstream task that run on different machines. TOP — DATA PATH, large payload (pink): the extract task on 'worker A' does a PUT of the bytes into Object storage (a yellow cylinder, e.g. s3://staging/orders.parquet); the transform task on 'worker B' does a GET of those bytes using the URI. BOTTOM — CONTROL PATH, small metadata (blue): extract's return value (the URI string) is pushed to the XCom backend (a blue cylinder labelled 'stores the URI, ID, flag, or count — default: the metadata DB'); transform pulls the URI back. A bottom banner contrasts good XCom ('s3://…/orders.parquet', date='2026-06-16', rows=1240120) with bad XCom (the file bytes themselves — which bloats the store and slows every scheduler query)." width="1180">

> **Memory hook:** *tasks run in separate processes, so they can't share variables. **XCom** passes small values between them: a task's return value is pushed; a downstream task pulls it. Pass **pointers** (a path, an ID, a count) — never the payload. Big data lives in object storage; XCom carries the handle.*

---

## Section 4 — Writing DAGs the modern way: TaskFlow, mapping, branching, groups

The `>>` + `xcom_pull` style works and is worth understanding because it mirrors what's happening underneath. But modern Airflow lets you write tasks as plain decorated functions, and this is where it gets pleasant.

### The TaskFlow API: the function-call graph *is* the DAG

Decorate functions with `@task` and **passing one function's return value as another's argument both creates the dependency edge and hands over the data** — no explicit `>>`, no `xcom_pull`:

```python
from airflow.decorators import dag, task

@dag(schedule="@daily", start_date=datetime(2026, 1, 1), catchup=False)
def daily_report():

    @task
    def extract() -> str:
        return dump_to_staging()        # return value flows downstream automatically

    @task
    def transform(path: str) -> str:
        return aggregate(path)          # receives extract's return value as its argument

    @task
    def load(summary: str):
        write_to_warehouse(summary)

    load(transform(extract()))          # the function calls ARE the edges

daily_report()
```

`load(transform(extract()))` reads exactly like ordinary Python, and that nesting is the DAG. This is the cleanest possible expression of "output of one step becomes input to the next" — XCom is still doing the work underneath, it's just invisible now.

### Dynamic task mapping: one task per item, decided at runtime

We hardcoded `transform_sales` and `transform_signups`. But "the things to transform" is really just a list — why write an operator per entry? `.expand()` creates **one task instance per element, at runtime**:

```python
@task
def transform(table: str) -> str:
    return clean(table)

transform.expand(table=["sales", "signups", "refunds", "sessions"])  # 4 task instances, generated
```

Add a fifth table later and you add a string, not an operator. Better still, that list can be the **output of a previous task** — a `decide_tables` task could emit `["sales", "signups"]` on a normal day but a longer list at month-end, and Airflow maps `transform` over whatever it returns. **The fan-out width becomes data-driven.**

### Branching: take one path, skip the other

Sometimes the graph itself depends on a condition — say a heavier monthly rollup only on the last day of the month. A `@task.branch` function returns the `task_id`(s) to run; everything it *doesn't* return is marked `skipped`:

```python
@task.branch
def pick_path(is_month_end: bool) -> str:
    return "monthly_rollup" if is_month_end else "skip_rollup"
```

Downstream of a branch you usually set `trigger_rule="none_failed_min_one_success"` so the join task proceeds past whichever side was skipped, instead of waiting forever on the path not taken.

### Task groups: keep a big DAG readable

As a DAG grows to dozens of tasks, group related ones so the UI shows one collapsible box instead of forty. This is purely organizational — it changes nothing about execution:

```python
from airflow.utils.task_group import TaskGroup

with TaskGroup("transforms") as transforms:
    sales   = PythonOperator(task_id="sales",   python_callable=clean_sales)
    signups = PythonOperator(task_id="signups", python_callable=clean_signups)
    refunds = PythonOperator(task_id="refunds", python_callable=clean_refunds)
```

> **Memory hook:** *the TaskFlow API turns "output → input" into a plain function call — passing a return value both wires the edge and passes the data. `.expand()` maps a task over a runtime list (data-driven fan-out); `@task.branch` picks a path and skips the rest; `TaskGroup` is cosmetic — it tidies the UI without changing execution.*

---

## Section 5 — Waiting for the world, and recovering from failure

Two things every real pipeline needs that we haven't covered: waiting on something *outside* Airflow, and surviving a step that fails.

### Sensors: wait for an external condition

What kicks off the very first task — the raw data file landing? A **sensor** is a special task that **waits for a condition to become true** and only then succeeds, unblocking what's downstream. A `FileSensor` blocks until a file exists; there are sensors for S3 objects, database rows, other DAGs, and more:

```python
from airflow.sensors.filesystem import FileSensor

wait_for_dump = FileSensor(
    task_id="wait_for_dump",
    filepath="/data/exports/orders.csv",
    poke_interval=60,         # check every 60 seconds
    mode="reschedule",        # release the worker slot between checks (don't hog it)
)
wait_for_dump >> extract
```

`mode="reschedule"` (or a modern *deferrable* sensor) matters at scale: a sensor that naively held a worker slot while it sat idle for hours would starve everything else. Instead it sleeps and frees the slot.

### Retries: failure recovery you *declare* instead of code

A transient failure — the warehouse hiccuped, the network blipped — shouldn't kill the pipeline. Every task takes retry configuration, so recovery is a few keywords, not a hand-rolled loop:

```python
load = PythonOperator(
    task_id="load",
    python_callable=load_warehouse,
    retries=3,
    retry_delay=timedelta(minutes=2),
    retry_exponential_backoff=True,   # wait 2m, then 4m, then 8m
    execution_timeout=timedelta(minutes=30),
)
```

If `load_warehouse` raises, Airflow marks the task `up_for_retry`, waits out the backoff, and re-runs it — up to 3 times — before finally marking it `failed` (which, wired to a `one_failed` task, can page someone). And `execution_timeout` catches the opposite problem — the task that *hangs* forever: a run stuck past 30 minutes is killed and retried instead of blocking the pipeline indefinitely.

Everything you'd otherwise hand-build on a bare queue — retry loops, backoff, timeouts, dead-letter handling — is declarative configuration here.

> **Memory hook:** *Sensors wait for external conditions (a file appears) and free their worker slot while waiting. Retries, backoff, and timeouts are per-task config, not code: a flaky step auto-recovers (`up_for_retry` → re-run), and a hung step is killed by `execution_timeout`. Declare recovery; don't code it.*

---

## Section 6 — Scheduling, backfill, and the rule that makes retries safe

The `schedule` argument controls *when* a DAG fires:

- `schedule="@daily"` or `"0 2 * * *"` — run on a cron-like clock. Each run is tagged with a **logical date** (the data interval it represents — "the 2026-06-16 run processes June 16th's data").
- `schedule=None` — never run on a clock; **triggered externally** instead (by an API call, an event, or a sensor). Use this when work arrives per-event rather than on a timetable.

Two scheduling ideas bite newcomers, and both lead to the single most important discipline in Airflow:

- **Backfill & catchup.** Turn on a `@daily` DAG whose `start_date` was two weeks ago with `catchup=True`, and Airflow will run **all 14 missed intervals** to fill the gap. That's a *feature* for data pipelines (reprocess history on demand) and a *footgun* if it surprises you — so set `catchup=False` unless you specifically want it.
- **Idempotency.** Because Airflow **retries** tasks, can **backfill** history, and lets you **manually re-run** any task, *every task may execute more than once.* So every task must be **idempotent** — safe to run repeatedly with the same result. Concretely: write to a **deterministic path** (`s3://warehouse/2026-06-16/sales.parquet`) and *overwrite*, don't append-with-a-timestamp; make `load` an **upsert/MERGE** keyed on the date so re-running doesn't double-count; make a "publish" or "mark done" step a no-op if it's already done. Idempotency is precisely what makes *"just retry it"* a safe answer instead of a way to corrupt data.

Airflow guarantees **ordering** and **eventual completion**; *you* guarantee **re-run safety**. Split that responsibility cleanly and the system is correct.

> **Memory hook:** *`schedule` sets the cadence — `@daily` for clock-driven, `None` for event-triggered. `catchup=True` replays every missed interval (great for history, a footgun otherwise — default it off). Because Airflow retries, backfills, and re-runs, **every task must be idempotent**: deterministic paths, upserts, no-op-if-already-done. Airflow owns ordering and completion; you own re-run safety.*

---

You now know enough to **write and configure any Airflow DAG**: tasks from operators or `@task` functions, wired with `>>` or by passing return values; fan-in via trigger rules; data via XCom; recovery via sensors, retries, and timeouts; cadence via schedules; correctness via idempotency. That's the whole user-facing model.

So how does Airflow actually *do* any of it? Time to open the hood.

---

# Part II — Under the hood: how Airflow runs your DAG

## Section 7 — The components: what's actually inside Airflow

Your DAG file is just a description. Something has to read it, decide what's ready, launch processes on machines, collect results, and show you the status. That "something" is **five cooperating components arranged around one central database** — and understanding how they hand off to each other *is* understanding Airflow.

<img src="../assets/airflow/airflow-architecture.svg" alt="Airflow's internal architecture drawn as components around a central database. CENTER (yellow cylinder): the METADATA DATABASE (Postgres/MySQL) — the single source of truth holding every DAG's structure, every DAG Run and Task Instance, and each task's state (queued, running, success, failed, up_for_retry). Everything reads and writes here. LEFT: DAG FILES, a folder of .py files defining the DAGs. TOP: the SCHEDULER (blue, 'the brain') — loops continuously, checks the database for which task instances have all their upstream dependencies satisfied and are due, and pushes those ready tasks to the executor; it writes state changes back to the DB and never runs task code. RIGHT: the EXECUTOR plus a queue feeding a pool of WORKERS (stacked boxes) — workers pull ready tasks, run the actual task code, and report success or failure back to the database. BOTTOM: the WEBSERVER / UI (green) — reads the database and renders the DAG graph, task states, logs, and history for humans. Arrows form a loop: scheduler reads DAGs and DB, enqueues ready tasks, workers run them and write results to the DB, scheduler sees the completions and enqueues the now-unblocked downstream tasks. Caption: the database is the heart; the scheduler decides what's ready, workers do the work, the webserver shows the truth." width="1180">

- The **Metadata Database** (Postgres or MySQL) is the **single source of truth**. It stores every DAG's structure, every DAG Run, every Task Instance, and — crucially — each task's **state**: `queued`, `running`, `success`, `failed`, `up_for_retry`. Every other component reads and writes here. This one authoritative place that knows the whole graph's state is exactly what a bare queue lacked.
- The **Scheduler** is the **brain**. It runs a loop: figure out which task instances are *due* and have **all their upstream dependencies satisfied**, and push those ready tasks to the executor. It also writes state changes back to the database. The scheduler is what continuously *polls* — it's the component that answers "who notices that a task finished and starts the next one?"
- The **Executor + Workers** actually run task code. The **executor** manages a queue of ready tasks; a **worker** pulls one, runs its code (the query, the API call, the transform), and reports `success` or `failure` back to the database. (Under the hood the executor often *uses* a queue like Celery/Redis as plumbing — that's fine; the point is the *orchestration logic* lives above it, not in the queue.)
- The **Webserver / UI** reads the database and renders the DAG, every task's color-coded state, the logs, and run history. This is the human "where is my pipeline stuck?" answer.
- The **DAG files** are the plain Python scripts in a folder, each defining one DAG. Both the scheduler and the workers need to know the graph.

Here's the loop that makes the whole thing go, in our example: the scheduler sees `transform_sales` finished `success` in the database → it checks the DAG and sees `load` depends on `transform_sales` *and* `transform_signups` → not both done yet, so `load` waits. A moment later `transform_signups` writes `success` → next loop, the scheduler re-checks `load`'s dependencies, finds *both* now `success` → `load` becomes ready → the scheduler enqueues it → a worker runs it. **That is the fan-in, executed for free** — nobody hand-wrote "wait for both"; the scheduler simply re-reads the database and notices.

> **Memory hook:** *five components around one database. **Metadata DB** = source of truth for task state. **Scheduler** = brain: finds tasks whose upstreams are all done and enqueues them. **Executor/Workers** = run the code and report state back. **Webserver** = the status UI. **DAG files** = the Python graph. The scheduler's read-check-enqueue loop is the fan-in engine.*

---

## Section 8 — Inside the scheduler: the loop that runs everything

The scheduler is a long-running process that loops every few seconds. At heart it's a **state-machine driver**: each tick it reads the current state of every run from the database, computes which tasks just became eligible, writes their new state, and hands them to the executor. **It never runs your task code.** Let's walk the loop.

<img src="../assets/airflow/airflow-scheduler-loop.svg" alt="The Airflow scheduler loop and how DAG parsing feeds it. LEFT (decoupled): a folder of DAG .py files feeds a separate process, the DAG Processor, which parses each file and writes a serialized JSON form of the DAG into the metadata database. A callout notes this runs in its own process so a slow or broken DAG file cannot stall scheduling, and that the scheduler and webserver afterward read the DAG from the database rather than importing Python again. CENTER-BOTTOM: the metadata database cylinder holding DagRuns and TaskInstances with their states. TOP: the scheduler loop drawn as five numbered steps in a cycle with an 'every few seconds' return arrow — (1) create DagRuns that are due or were triggered; (2) gather TaskInstances still waiting; (3) run dependency checks: trigger_rule satisfied, free pool slot, under max_active_tasks, depends_on_past cleared; (4) set eligible tasks to scheduled then hand to the executor, which marks them queued; (5) read back the success/failed states workers wrote last tick. RIGHT: the executor with an internal queue feeds a pool of workers; a 'write success/failed' arrow goes from workers back to the database. A side panel 'Why load waits' shows: when transform_sales=success but transform_signups=running, load fails the all_success check and stays waiting; once both read success, load passes and is queued. Caption: the scheduler polls the database forever, computes eligibility, and enqueues — that loop is the fan-in engine." width="1180">

**Step 0 — DAG parsing happens in a separate process.** Your `.py` files are parsed by a dedicated **DAG Processor**, *not* by the scheduler loop. This matters: a slow import or an infinite loop in one DAG file can't stall scheduling for everyone else. The processor parses each file and writes a **serialized** form of the DAG (its structure, as JSON) into the database. From then on, the scheduler and webserver read the DAG *from the database* — they never import your Python again. That's how the UI can draw a DAG without executing it, and how every scheduler and webserver sees an identical graph.

**Two kinds of rows hold all the state.** A **DagRun** is one execution of the whole DAG (today's `daily_report`). A **TaskInstance** (TI) is one task inside one DagRun (`load` for the June 16th run). Every piece of state in the entire system lives on these rows: `load@2026-06-16 = running`.

**The loop, each tick:**

1. **Create DagRuns** that are due — a schedule fired, or something triggered one externally.
2. For each active DagRun, gather its TaskInstances that are still waiting to run.
3. **Run the dependency checks** on each one: are all upstreams in a state the `trigger_rule` accepts? Is there a free **pool** slot? Is the run under its `max_active_tasks`? A TI becomes eligible only if *every* check passes.
4. Eligible TIs move to state **`scheduled`**, then the scheduler hands them to the **executor**, which moves them to **`queued`**.
5. The loop reads back the states workers wrote last tick (`success`, `failed`). The moment `transform_sales` and `transform_signups` both read `success`, `load`'s dependency check passes on the next tick and it becomes eligible. **That is the fan-in, evaluated by re-reading the database every few seconds.**

So "who polls?" — the scheduler, against the database, forever. "Who announces a task is done?" — the worker writes `success` to the database; the scheduler reads it. There is **one authority** (the database), not a fragile distributed handshake.

### Orchestration: who's actually in charge

It's worth naming the roles precisely, because *orchestration* is the entire point. The **scheduler is the orchestrator** — the single brain that decides *ordering*. But it's a **control-plane** component: it touches only metadata (which task is where in its lifecycle), never a byte of your actual data. The **workers** are the **data plane**: they touch the data — run the query, transform the rows — but make *no* ordering decisions. Between them sits the **metadata database**, the shared blackboard both read and write.

This is the same [control-plane / data-plane split as in S3](20-high-throughput-system-s3.md): there, a manager decided *where* data lived (control) while servers served the bytes (data). Here, the scheduler decides *when* a task runs (control) while workers run it (data). The orchestrator can reason about thousands of tasks precisely *because* it never does any of the heavy lifting itself.

### Two schedulers, no leader election

The scheduler looks like a single point of failure — if the brain dies, nothing schedules. So you run **two or more schedulers active-active**. Remarkably, Airflow needs **no** separate coordination service for this (unlike a [lock manager's leader election](07-distributed-lock-manager.md)). The schedulers coordinate **through the database** using row-level locks — `SELECT ... FOR UPDATE SKIP LOCKED`. When scheduler A grabs a batch of TaskInstances, scheduler B simply skips those locked rows and takes others. Two schedulers never schedule the same task; if one crashes, the rest keep going. Coordination is pushed *into the database*, which is already the source of truth.

> **Memory hook:** *the scheduler is a state-machine driver: each tick it reads run state, runs the dependency checks (trigger_rule + free pool slot + limits), moves eligible tasks `scheduled` → `queued`, hands them to the executor, and re-reads results next tick — that loop is the fan-in engine. DAG parsing is a **separate process** writing serialized DAGs to the DB. The scheduler is the **control plane**; workers are the **data plane**; the DB is the blackboard. Run schedulers **active-active**, coordinated by **row locks** — no leader election.*

---

## Section 9 — The executor and workers: running the code and reporting back

The scheduler decided `load` should run and marked it `queued`. What actually launches a process, on which machine, and how does the result get back into the database? That's the **executor's** job — the bridge between "the scheduler decided this should run" and "a process somewhere is running it." Airflow ships several, all behind one interface; you pick one in config and your DAGs don't change:

| Executor | Where tasks run | Use it for |
| --- | --- | --- |
| **Sequential** | one at a time, in-process | local tinkering only |
| **Local** | parallel subprocesses on the scheduler's machine | small single-machine setups |
| **Celery** | a distributed fleet of worker machines, via a broker (Redis/RabbitMQ) | the classic production choice |
| **Kubernetes** | one fresh pod per task | elastic, isolated, pay-per-task |

**The handoff is a command.** When the scheduler queues a TI, the executor's job is to get it running somewhere — conceptually, `airflow tasks run daily_report load <run_id>`. With Celery, that command is pushed onto the broker queue and an idle worker pulls it. With Kubernetes, the executor launches a pod whose entrypoint *is* that command. (Yes — there's a queue *inside* the executor. The lesson was never "queues are bad"; it's that modeling *dependencies* as a queue is wrong. A queue is perfect for "hand this one ready unit of work to some worker," which is all the executor uses it for.)

**The worker runs your code and closes the loop.** It executes your `python_callable` (or the operator's logic), streams logs, and on completion does two things: it **writes the final state** (`success`/`failed`) to the metadata database, and it **uploads the logs** to remote storage so any webserver can show them later. The scheduler sees the new state on its next tick.

### Liveness: heartbeats and zombies

What if a worker *crashes mid-task* — the machine dies during a long load — and never writes `success` *or* `failed`? That TI would sit in `running` forever. Airflow catches this with **heartbeats**: a running task periodically updates a heartbeat timestamp on its row. The scheduler watches for TIs marked `running` whose heartbeat has gone stale, declares them **zombies**, marks them `failed`, and — if retries remain — lets them re-run on a different worker. No task hangs forever; the safety net is built in.

### The TaskInstance state machine

Every task moves through a small set of states, and seeing them as a machine *is* the answer to "where is my pipeline stuck?" — it's always "which state is this TI in, and why."

<img src="../assets/airflow/airflow-task-states.svg" alt="The Airflow TaskInstance state machine, left to right, with a legend showing which component sets each transition. States as boxes: none (gray) → scheduled (blue) → queued (blue) → running (pink) → then two terminal outcomes, success (green) and failed (red). From running, a yellow arrow goes down to up_for_retry, which loops back to scheduled after the retry delay (labelled 'retry_delay, up to N retries'). Also from running, a blue arrow to up_for_reschedule loops back to scheduled (labelled 'a deferrable sensor sleeps without holding a worker slot'). Branching off scheduled: a gray 'skipped' state (a branch not taken) and a red 'upstream_failed' state (a dependency failed, so this task can never satisfy all_success). A dashed red arrow labelled 'zombie: heartbeat went stale' points from running to failed, annotated as detected by the scheduler. Legend: the SCHEDULER sets scheduled, queued, detects zombies, and sets upstream_failed/skipped; the WORKER sets running, then success or failed. Caption: none→scheduled→queued→running→success|failed is the happy path; up_for_retry and up_for_reschedule loop back; the scheduler owns control-state transitions, the worker owns execution-state transitions." width="1180">

- The **scheduler** sets `scheduled` and `queued`, detects zombies, and sets `upstream_failed` / `skipped`.
- The **worker** sets `running`, then `success` or `failed`.
- `up_for_retry` loops back to `scheduled` after the retry delay; `up_for_reschedule` is how a deferrable sensor sleeps without holding a slot; `skipped` is a branch not taken; `upstream_failed` means a dependency failed, so this task can never satisfy `all_success`.

> **Memory hook:** *the executor bridges `queued` → "running on a machine": Sequential/Local (one box) or Celery/Kubernetes (a fleet / pod-per-task). The handoff is a **command** pushed onto the executor's internal queue; a worker runs it, writes `success`/`failed` to the DB, and uploads logs. **Heartbeats + zombie detection** catch crashed workers. The TI state machine — `none → scheduled → queued → running → success|failed`, with `up_for_retry` looping back — is the "where is it stuck?" answer.*

---

## Section 10 — Airflow as a distributed system

Strip away the report example: what *kind* of system is this, and how does it handle what every distributed system must — where state lives, how it survives crashes, how it stays correct, how it avoids a single point of failure, and where it bottlenecks?

Airflow is a **control plane over a worker fleet, coordinated through one database.** Every systems property falls out of that one sentence.

<img src="../assets/airflow/airflow-scaling.svg" alt="Airflow as a distributed system, drawn as three horizontal planes with systems-property callouts. TOP — CONTROL PLANE (blue): two Scheduler boxes side by side labelled 'active-active, coordinated by DB row locks — no leader election'; several stateless Webserver boxes behind a load balancer; and the DAG Processor. CENTER — STORAGE / COORDINATION (yellow): a Metadata DB drawn as a primary cylinder with a standby replica and a 'streaming replication / failover' arrow between them, labelled 'the source of truth AND the bottleneck — the one true SPOF, so make it highly available'; beside it a remote log store and an XCom backend. BOTTOM — DATA PLANE (pink): a horizontally scalable worker fleet (Celery workers, or one Kubernetes pod per task) drawn as many stacked boxes, with a Pool gauge labelled 'a pool caps concurrency on a scarce resource'. Arrows: schedulers read and write the metadata DB; workers read commands and write success/failed plus upload logs to the remote store. Around the edges six labelled callouts point at the relevant component: STORAGE → metadata DB + remote logs + XCom; DURABILITY → DB write-ahead log + backups, crashes resume from DB state, retries/backfill are replay; INTEGRITY → enforced ordering + at-least-once execution so idempotency is the user's job; REPLICATION/HA → replicate the DB, active-active schedulers, stateless webservers, fungible workers; CONFIGURATION → airflow.cfg/env + Connections/Variables/Pools/secrets; SCALABILITY → add workers and schedulers, the DB is the ceiling, pools and max_active_* are backpressure. Caption: one database coordinates a control plane of brains and a data plane of muscle." width="1320">

**Storage — what lives where.** The **metadata DB** is the system of record: serialized DAG structure, DagRuns, TaskInstances and their states, XCom values, plus Connections, Variables, and Pools. If it's true about your pipeline, it's a row here. **Logs** are written by workers but stored **remotely** (S3/GCS) in production, so they outlive the worker that produced them and any webserver can serve them. **XCom** lives in the DB by default (small pointers only); swap in an object-storage backend if you must pass larger blobs, so the DB doesn't bloat.

**Durability — surviving a crash.** The metadata DB is the **durability boundary**. Because state lives in a real database with its own [write-ahead log](22-high-throughput-lsm-trees.md) and backups, a scheduler or worker crash loses *nothing*: restart, the scheduler reads the DB, and resumes exactly where it left off. Retries and backfill are simply **replay** on top of that durable state.

**Integrity — correct execution.** Ordering is **enforced**, not hoped for: the scheduler won't move a task past its dependency check, so `load` *cannot* start before both transforms are accepted, and the DAG's acyclic shape guarantees a valid order always exists. But execution is **at-least-once, not exactly-once** — a worker can finish a task, crash before writing `success`, and have it re-run. So **idempotency is your job** (Section 6): Airflow guarantees *ordering and eventual completion*; you guarantee *re-run safety*. Uniqueness is a DB constraint (one row per `dag_id + run_id + task_id`), and row locks stop two schedulers from double-scheduling.

**Replication & HA — killing the single points of failure.** The metadata DB is the one component that *must not* lose data — so it's also the true SPOF, and you make it **replicated** (primary + standby, with failover). Protect this one thing above all. **Schedulers**: run two or more active-active (row locks). **Webservers**: stateless — run several behind a load balancer. **Workers**: a fleet — lose one and its in-flight tasks become zombies and retry elsewhere; no worker is special.

**Configuration — the knobs that matter.** `airflow.cfg` and `AIRFLOW__SECTION__KEY` environment variables set the executor, parallelism, and DB connection. **Connections** hold how to reach external systems (credentials, endpoints); **Variables** are key-value config; a **secrets backend** can pull credentials from Vault / a secrets manager instead of the DB. **Pools** cap concurrency on a scarce resource: a pool of size 8 over a fragile external API means no more than 8 tasks hit it at once, no matter how many runs pile up.

**Scalability — where it grows, where it bottlenecks.** Scale the **data plane** horizontally: add Celery workers, or let Kubernetes spawn a pod per task — throughput grows with the fleet. Scale the **control plane**: add schedulers to schedule more tasks per second. The bottleneck is the **metadata DB**: every scheduling decision, heartbeat, and state write hits it, so at scale you scale *it* up, tune it, and reduce write pressure (avoid thousands of trivial tasks; lengthen heartbeats). `max_active_runs`, `max_active_tasks`, and pools are your **backpressure** knobs — they bound concurrency so a flood of work can't overwhelm the workers or a downstream system. The shared source of truth is the thing you scale last and hardest — the same lesson as the single master in the [multi-tiered datastore](21-high-throughput-multi-tiered-db.md).

> **Memory hook:** *Airflow is a control plane over a worker fleet, coordinated through one DB. **Storage** = metadata DB (system of record) + remote logs + XCom. **Durability** = the DB's WAL/backups; crashes resume from DB state; retries/backfill are replay. **Integrity** = enforced ordering + at-least-once execution, so idempotency is your job. **HA** = replicate the DB (the true SPOF), active-active schedulers, stateless webservers, a fungible worker fleet. **Config** = airflow.cfg/env + Connections/Variables/Pools/secrets. **Scale** workers and schedulers horizontally; the metadata DB is the bottleneck and the backpressure point.*

---

## Where this leaves us

We built Airflow from the ground up. First the vocabulary — **operator** (template), **task** (a configured unit), **DAG** (the acyclic graph of tasks), **DAG Run** and **Task Instance** (the stateful executions). Then we wrote real DAGs: wiring order with `>>`, expressing fan-in with the default `all_success` trigger rule, passing data with **XCom** (pointers, never payloads), cleaning it up with the **TaskFlow API**, generating tasks at runtime with **`.expand()`**, choosing paths with **branching**, waiting on the world with **sensors**, recovering with **retries and timeouts**, and keeping it correct with **idempotency**.

Then we opened the hood and found a real distributed system. Five components hand off through one metadata database: the **scheduler** loops every few seconds reading run state, checking dependencies, and enqueuing eligible tasks; the **executor** hands each ready task to a **worker** that runs the code and writes its result back; the **webserver** renders that state for humans; and the **DAG files** (parsed by a separate processor into the DB) declare the graph. Every serious-distributed-system property — durability, integrity, high availability, a clean scaling story — traces back to a single design decision: **all state lives in one database.**

That's the sentence to carry away, now earned: **a workflow orchestrator is a control plane of brains over a data plane of muscle, coordinating through a single source of truth.** The scheduler decides *when*, the workers do *what*, and the metadata DB remembers *everything* — which is exactly why "run E only after C and D both finish," the thing a cron job or a queue could never express, is just an edge the scheduler re-evaluates on its next tick. Understand that, and you understand not just how to use Airflow, but how you'd build one.

> **Memory hook:** *Airflow's whole design is "put all state in one DB; let stateless schedulers (control) and stateless workers (data) coordinate through it." The scheduler decides **when**, workers do **what**, the DB remembers **everything**. Durability, integrity, HA, and scale all fall out of that one decision — and so does native fan-in.*
