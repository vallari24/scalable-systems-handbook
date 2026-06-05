# Designing Distributed ID Generators

**Question: how do we assign a globally unique ID to anything?**

An ID gives uniqueness to an object, row, document, or event.

The problem statement is simple:

```text
write a function that returns something unique every time it is invoked
```

For now, assume the ID generation function is part of the application logic.

It is not a separate central service yet.

```text
application code -> get_id() -> unique ID
```

We will build the function incrementally.

## Start With Time

Time feels like a natural source of uniqueness because it usually moves forward.

```text
func get_id() {
  return get_epoch_ms()
}
```

If the function returns the current epoch millisecond, then every call made in a different millisecond gets a different value.

Example:

```text
call 1 at t=1729 -> 1729
call 2 at t=1730 -> 1730
call 3 at t=1731 -> 1731
```

This works when two assumptions hold:

```text
one machine
not more than one ID request in the same millisecond
```

Those assumptions are too weak for a real distributed system.

Even on one machine, two calls can happen in the same millisecond.

```text
call 1 at t=1729 -> 1729
call 2 at t=1729 -> 1729

collision
```

Time by itself is not enough.

## Add The Machine ID

**Question: what happens when there are multiple machines?**

Two machines can call the function at the same millisecond.

```text
machine 1 at t=1729 -> 1729
machine 2 at t=1729 -> 1729

collision
```

The smallest fix is to add the machine ID.

```text
func get_id() {
  return concat(machine_id, get_epoch_ms())
}
```

Now the same timestamp produces different IDs on different machines:

```text
machine 1 at t=1729 -> m1-1729
machine 2 at t=1729 -> m2-1729
```

The machine ID separates ID ranges across machines.

But this still has a local problem.

If the same machine generates two IDs in the same millisecond, both IDs are still the same:

```text
machine 1 call 1 at t=1729 -> m1-1729
machine 1 call 2 at t=1729 -> m1-1729

collision
```

## Add Local Differentiation

**Question: what if the program has multiple threads?**

Two threads can invoke the function at the same time.

One possible fix is to add the thread ID:

```text
func get_id() {
  return concat(machine_id, thread_id, get_epoch_ms())
}
```

That separates two threads on the same machine:

```text
thread 1 at t=1729 -> m1-t1-1729
thread 2 at t=1729 -> m1-t2-1729
```

But thread ID is not the cleanest source of uniqueness.

The same thread can still ask for two IDs in the same millisecond. Threads can also be reused by a runtime or worker pool.

A better local differentiator is a counter.

```text
int counter = 0

func get_id() {
  return concat(machine_id, get_epoch_ms(), counter++)
}
```

Now a single machine can generate multiple IDs in the same millisecond:

```text
m1-1729-01
m1-1729-02
m1-1729-03
```

At this point, the timestamp is not doing the uniqueness work by itself.

The pair is:

```text
machine_id + counter
```

If every machine has a unique machine ID, and each machine increments its own counter safely, the timestamp becomes optional for uniqueness.

So the local generator can be reduced to:

```text
int counter = 0

func get_id() {
  return concat(machine_id, counter++)
}
```

Example:

```text
machine 1 -> m1-01, m1-02, m1-03
machine 2 -> m2-01, m2-02, m2-03
```

The machine ID separates machines.

The counter separates calls on the same machine.

The counter increment must still be safe inside the process. If multiple threads call `get_id()` concurrently, `counter++` should be atomic or protected by a local lock.

## Volatile vs Non-Volatile State

**Question: what happens when the process restarts?**

The counter we just added lives in memory.

Memory is **volatile**.

Volatile state disappears when the process exits, the machine crashes, or the container restarts.

| State location | Volatile? | What happens on restart? |
| --- | --- | --- |
| **Memory** | Yes. | The counter is lost. |
| **Disk** | No, for normal process restarts. | The counter can be loaded again. |
| **Remote database** | No, if the database commits the value durably. | The counter can be loaded again over the network. |

If the counter starts from zero after every restart, the same machine can regenerate old IDs.

```text
before restart:
m1-01
m1-02
m1-03

process restarts
counter = 0

after restart:
m1-01
m1-02
m1-03

collision
```

So the counter needs durability.

The simplest durable version is:

```text
int counter = load_counter_from_disk()

func get_id() {
  counter++
  save_counter_to_disk(counter)
  return concat(machine_id, counter)
}
```

Now the process can restart and continue from the last saved counter.

```text
disk counter = 103
process starts
counter = 103

next ID -> m1-104
```

This improves fault tolerance, but it creates a throughput problem.

Every ID now requires disk I/O.

```text
get_id()
  -> increment counter in memory
  -> write counter to disk
  -> return ID
```

Disk is non-volatile, but it is much slower than memory.

If the hot path performs a disk write for every ID, ID generation throughput drops.

## Why Not Store Every Counter In A Database?

A database would also give us durable state.

```text
application -> database counter row -> increment -> return ID
```

But now every ID generation call pays network I/O.

That is usually worse for a hot local function than writing to local disk because the call crosses the network and depends on another service.

The database version also starts to become a central ID service. That may be useful later, but it is not the simplest improvement to this local generator.

The design question is:

```text
can we keep the counter mostly in memory, but persist enough state to survive crashes?
```

## Reserve Counter Blocks

**Question: can we reduce disk I/O without losing uniqueness after restart?**

Yes.

Persist a future counter boundary instead of persisting every single counter value.

For example, reserve IDs in blocks of `1000`.

```text
BLOCK_SIZE = 1000

counter = load_reserved_counter_from_disk()
reserved_until = counter

func reserve_next_block() {
  reserved_until = counter + BLOCK_SIZE
  save_reserved_counter_to_disk(reserved_until)
}

func get_id() {
  if counter == reserved_until {
    reserve_next_block()
  }

  counter++
  return concat(machine_id, counter)
}
```

The important detail is that the generator writes the next reserved boundary to disk before it hands out IDs from that block.

If that disk write fails, the generator must not use the block. The block is safe only after the future boundary is durable.

Then the normal path is memory-only for most calls:

```text
first call in block -> reserve boundary on disk
next call           -> memory increment
...
most calls          -> memory increment
first call in next block -> reserve next boundary on disk
```

The modulo version has the same idea:

```text
func get_id() {
  if counter % BLOCK_SIZE == 0 {
    save_reserved_counter_to_disk(counter + BLOCK_SIZE)
  }

  counter++
  return concat(machine_id, counter)
}
```

The modulo check runs before the counter enters a new block, so the generator writes the future boundary before using that block.

The save happens once per block, not once per ID.

That is the huge throughput improvement.

```text
before: 1 disk write per ID
after:  1 disk write per 1000 IDs
```

The tradeoff is gaps.

Suppose the generator reserves up to `1000`, emits only a few IDs, and then crashes.

```text
disk says reserved_until = 1000

emitted:
m1-001
m1-002
m1-003

crash
```

On restart, the generator loads `1000` and starts after that reserved range.

```text
next safe ID -> m1-1001
```

It skips unused IDs from the previous block.

That is acceptable for most distributed ID generators.

The requirement is uniqueness, not gap-free numbering.

## Current Shape

The generator now has three pieces:

```text
machine_id       -> separates machines
local counter    -> separates calls on one machine
reserved boundary -> survives process restart without per-ID disk writes
```

The request path is still local:

```text
application thread -> get_id() -> memory counter -> ID
```

Only once per block does it touch disk:

```text
counter reaches block boundary -> save next reserved boundary to disk
```

This is the first practical version:

```text
unique across machines
safe across restarts
fast for the common path
allows gaps after crashes
```

The memory hook:

```text
machine id gives ownership
counter gives sequence
disk checkpoint gives restart safety
block reservation gives throughput
```
