# Designing a Distributed Lock Manager

**Question: what is a remote lock?**

A remote lock is a lock managed by a machine outside the workers that need the lock.

Instead of each machine deciding locally, all machines coordinate through one shared place:

```text
machine 1 -> lock manager
machine 2 -> lock manager
machine 3 -> lock manager
```

The lock manager answers one basic question:

```text
who is allowed to enter this critical section right now?
```

The shape is:

```text
           lock manager
          /      |      \
         /       |       \
machine 1   machine 2   machine 3

The 3 machines coordinate through one central lock manager.
```

## Why Locks Exist

Locks are about synchronization.

When many actors can touch the same shared thing, the system needs a rule for who can enter the protected section at a time.

The synchronization tool depends on what the actors share.

| Contenders | Fast shared place | Common lock shape |
| --- | --- | --- |
| **Multiple threads** | Same process memory, usually RAM. | Mutex or semaphore. |
| **Multiple processes** | They do not share normal process memory. A shared file or disk-backed lock is a common simple coordination point. | File lock or disk-backed lock. |
| **Multiple machines** | No shared RAM, and no safe local disk shared by all machines. | Remote lock through a lock manager. |

The mental model:

```text
put the lock in the fastest shared place that every contender can see
```

For threads, that place is RAM.

For separate processes on the same machine, normal RAM is isolated. They can coordinate through an operating-system primitive or a shared file on disk. The disk/file-lock model is useful because it makes the shared coordination point visible.

For machines, local memory and local disk are not shared. So the coordination point has to move to a remote component that all machines can call.

That remote component is the lock manager.

## Remote Lock Example

An interesting local example is:

```text
apt-get upgrade cannot be run twice concurrently
```

Two package upgrades on the same machine would both try to update package metadata and installed files. So the package manager uses a lock to ensure only one upgrade process runs at a time.

That is local coordination.

In a distributed system, the same idea appears when multiple machines compete for one remote operation.

Imagine a remote queue with three consumers.

```text
consumer 1
consumer 2  -> remote queue
consumer 3
```

The queue is unprotected. We want only one consumer to update or claim the next item at a time.

Without coordination, two consumers can race:

1. Consumer 1 reads the next item.
2. Consumer 2 reads the same next item.
3. Both make the downstream call.
4. Both try to update the queue state.

The problem is not that the consumers are bad. The problem is that the shared remote queue needs a synchronization rule.

So the design question becomes:

```text
where should consumers acquire a lock before touching the queue?
```

If all consumers run on one machine, a local lock may work.

If consumers run on different machines, the lock must be remote:

```text
consumer -> acquire queue lock -> update queue -> release queue lock
```

Now only one consumer owns the protected queue operation at a time.

This is the first reason a distributed system needs a lock manager:

```text
multiple machines
one shared remote resource
one owner at a time
```

## Add a Shared Lock Entry

**Question: how do we ensure one consumer updates the remote queue and the others do not?**

Add a shared lock entry in a database or key-value store that all consumers can reach.

The shape is:

```text
                    Queue: remote
        +----------------------------------------+
push -> | [] [] [] [] [] [] [] [] [] [] []      |
        +----------------------------------------+
                     |          |          |
                     v          v          v
              +------------+ +------------+ +------------+
              | consumer 1 | | consumer 2 | | consumer 3 |
              +------------+ +------------+ +------------+
                     |          |          |
                     |          |          |
                     +----------+----------+
                                |
                                v
                         +-------------+
                         | Redis / DB  |
                         | lock entry  |
                         +-------------+

                         key:   queue:remote
                         value: consumer-1
                         ttl:   30 seconds

All consumers share the same remote queue and the same remote lock entry.
Only the consumer that creates the lock entry updates the queue.
```

The consumers do not coordinate through their own memory. They coordinate through one shared key in Redis or another key-value store.

Every consumer must claim that same lock key before touching the queue.

Only the consumer that successfully creates the lock entry enters the critical section.

```text
consumer-1: acquire lock -> update queue -> release lock
consumer-2: acquire lock -> lock already exists -> wait or retry
consumer-3: acquire lock -> lock already exists -> wait or retry
```

The important part is that the claim must be **atomic**.

A plain read is not enough:

```text
read lock key
if missing, write lock key
```

That can race. Two consumers can both read "missing" before either write happens.

The lock store needs one atomic operation:

```text
create this key only if it does not already exist
```

That single operation protects the critical section. Either the key is created and the consumer owns the lock, or the key already exists and the consumer does not enter.

This is why atomicity is one of the most important properties for a remote lock. The whole lock depends on turning "check if free" and "mark as owned" into one indivisible operation.

## Why TTL Matters

**Question: what happens if the lock owner dies?**

Without a TTL, the lock can be stuck forever:

```text
consumer-1 acquires lock
consumer-1 crashes
consumer-2 waits forever
consumer-3 waits forever
```

So the lock entry needs an expiration:

```text
lock key: queue:remote
value:    consumer-1
ttl:      30 seconds
```

If the owner crashes, the key eventually expires and another consumer can try again.

The two required properties are:

| Property | Why it matters |
| --- | --- |
| **Atomic create-if-absent** | Prevents two consumers from entering the critical section at the same time. |
| **TTL / expiration** | Prevents a dead consumer from holding the lock forever. |

## Why Redis Is Common

**Question: which database gives us these two properties with very low latency?**

Redis is commonly used for remote locks because it is a fast key-value store and supports atomic lock-style operations with expiration.

The basic Redis shape is:

```text
SET queue:remote consumer-1 NX PX 30000
```

Meaning:

```text
NX -> set only if the key does not already exist
PX -> set a TTL in milliseconds
```

So the lock acquire becomes:

```text
create lock key if absent, with TTL
```

That is exactly the first version of a remote lock.

Redis is not the only possible lock store, but it is popular because the remote lock use case wants a small, fast, shared key-value entry with atomicity and expiration.

## Acquiring and Releasing the Lock

**Question: how should consumers acquire and release the lock safely?**

Every consumer runs the same shape:

```text
acquire lock
read message
release lock
```

Only one consumer should be inside `read message` at a time.

The acquire path can be one Redis command:

```text
SET queue:remote consumer-1 NX PX 30000
```

Redis executes that command atomically. The check and the write happen as one operation:

```text
if key is missing:
  create key with owner and TTL
else:
  do not create key
```

That means two consumers cannot both observe the key as missing and both acquire the same lock.

The expiration must be part of the same command. Do not do this as two separate operations:

```text
SETNX queue:remote consumer-1
EXPIRE queue:remote 30
```

If the consumer crashes between those two commands, the key may be created without an expiration.

The consumer pseudocode looks like this:

```text
def acquire_lock(queue):
    consumer_id = get_my_id()

    while true:
        ok = redis.set(queue, consumer_id, nx=true, px=30000)
        if ok:
            return
        else:
            wait and retry
```

This is enough for acquire, but release has a different problem.

A consumer should not delete the lock unless it still owns the lock.

This release path is unsafe:

```text
owner = redis.get(queue)
if owner == consumer_id:
    redis.delete(queue)
```

These two lines must be atomic because they are one logical decision:

```text
delete the lock only if it is still mine
```

The `GET` checks whether the lock is still owned by this consumer. The `DEL` changes shared state by removing the lock. If another consumer can change the key between those two commands, the check is no longer proof that the delete is safe.

The check and delete are two separate commands. Another consumer can slip in between them.

Here is the race:

```text
consumer-1 acquires lock: queue:remote = consumer-1
consumer-1 starts work
consumer-1 reads owner and sees consumer-1
consumer-1 pauses before delete
consumer-1 lock expires
consumer-2 acquires lock: queue:remote = consumer-2
consumer-1 resumes
consumer-1 deletes queue:remote
```

The bug is that consumer 1 deletes consumer 2's lock.

So release also needs atomicity. The lock manager must do this as one indivisible operation:

```text
if current owner is me:
  delete the lock
else:
  do nothing
```

In Redis, use a Lua script:

```lua
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
```

The release call becomes:

```text
EVAL <script> 1 queue:remote consumer-1
```

Redis runs the whole Lua script atomically. No other consumer can acquire or change that key between the `GET` and the `DEL` inside the script.

Now if consumer 1's lock expired and consumer 2 acquired the key, the script sees this:

```text
current owner: consumer-2
release owner: consumer-1
```

The values do not match, so the script does nothing.

The core rule is:

```text
acquire: atomic create-if-absent with TTL
release: atomic compare-owner-and-delete
```

In real systems, the value should usually be a unique lock token, not only the consumer name.

```text
queue:remote = consumer-1:lock-token-abc123
```

That token identifies this specific lock acquisition. If the same consumer later acquires the lock again, it gets a new token.

## Where This Shows Up

**Question: where do we see locks like this in real systems?**

Databases use this pattern internally.

For example, MongoDB uses locking and concurrency control so multiple clients do not modify the same data at the same time. With the WiredTiger storage engine, most reads and writes use intent locks at higher levels and document-level concurrency control below that.

Inside a transaction, if MongoDB modifies a document, it can take a lock for that document. If another write tries to modify the same document before the transaction commits, that other write must wait or hit a write conflict.

The shape is the same mental model:

```text
transaction -> acquire document lock -> modify document -> commit -> release lock
```

The difference is that the database owns the lock manager. Application code does not call Redis directly. MongoDB's transaction engine decides which locks to take, when to wait, when to abort, and when to release locks after commit or rollback.

Another well-known example is Google's Chubby lock service.

Chubby is a lock service for loosely coupled distributed systems. It was built for coarse-grained coordination: leader election, naming, configuration, and small pieces of reliable metadata.

That means Chubby is not trying to lock every message in a hot queue. It is more often used for decisions like:

```text
which server is the current master?
where should clients find the master?
which worker owns this coarse partition of work?
```

The mental model is:

```text
many distributed processes
one reliable coordination service
coarse-grained locks or small metadata
```

Chubby itself is replicated for availability, but clients see it as one coordination service. That is the same high-level purpose as our Redis lock manager example: move the shared synchronization point out of the workers and into a service that every worker can reach.

So remote locks show up in two broad forms:

| Lock type | Who manages it | Example |
| --- | --- | --- |
| **Database-managed lock** | The database engine. | MongoDB transaction locks a document it modifies. |
| **External coordination service** | Application code calls a shared lock or coordination service. | Consumers use Redis before reading a shared queue; systems use Chubby for leader election or coarse work ownership. |

The purpose is the same:

```text
many actors
one shared resource
one safe owner for the protected operation
```

## Distributed Locks and Redlock

**Question: what if the lock manager itself is a single point of failure?**

With one Redis lock manager, all consumers depend on one Redis instance.

```text
consumer-1
consumer-2  ->  one Redis lock manager
consumer-3
```

If that Redis instance is down, nobody can acquire a new lock.

Redlock is Redis's distributed lock algorithm. The idea is to take the single-instance Redis lock pattern and run it across several independent Redis masters.

The common Redlock shape uses 5 Redis masters:

```text
Redis master 1
Redis master 2
Redis master 3
Redis master 4
Redis master 5
```

These are independent masters. They are not replicas of one another for this lock key. The client tries to acquire the same lock key with the same unique token on each master.

```text
SET queue:remote token NX PX 30000  -> Redis 1
SET queue:remote token NX PX 30000  -> Redis 2
SET queue:remote token NX PX 30000  -> Redis 3
SET queue:remote token NX PX 30000  -> Redis 4
SET queue:remote token NX PX 30000  -> Redis 5
```

The client uses a short timeout for each Redis node so one slow or dead node does not block the whole acquire attempt.

The lock is acquired only if both conditions are true:

```text
acquired on a majority of Redis masters
and
acquired before the lock validity time is mostly gone
```

For 5 Redis masters, majority means 3.

```text
3 of 5 -> acquired
4 of 5 -> acquired
5 of 5 -> acquired

2 of 5 -> failed
1 of 5 -> failed
0 of 5 -> failed
```

If the client fails to acquire a majority, it releases any partial locks it did acquire.

```text
acquired Redis 1
acquired Redis 2
failed Redis 3
failed Redis 4
failed Redis 5

result: no majority
action: release Redis 1 and Redis 2
```

This avoids waiting for TTL expiration on the nodes where the client partially succeeded.

What happens if nodes fail?

| Redis nodes unavailable | Can a new lock still be acquired? | Why |
| --- | --- | --- |
| **1 node down** | Usually yes. | 4 nodes remain, and the client needs 3 successful locks. |
| **2 nodes down** | Yes, if all 3 remaining nodes respond quickly. | 3 nodes remain, which is still a majority of 5. |
| **3 nodes down** | No. | Only 2 nodes remain, so the client cannot reach majority. |

Redlock improves availability compared with one Redis lock manager because the system can tolerate one or two Redis-node failures.

It does **not** increase throughput for one protected resource.

The protected operation is still exclusive:

```text
one queue lock
one owner
one consumer reading the message
```

In fact, one Redlock acquire sends work to multiple Redis nodes, not one. Implementations usually send those requests in parallel so latency stays low.

The win is not "more consumers can hold the same lock." The win is:

```text
the lock manager no longer depends on one Redis node
```

Redlock is still a lease-based lock. The consumer must finish the protected work before the lock validity time runs out. If the protected operation can affect an external system after the lease expires, use an additional protection such as fencing tokens, idempotency, or a database transaction around the final write.

Useful references:

- [Redis distributed locks and Redlock](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
- [MongoDB concurrency FAQ](https://www.mongodb.com/docs/manual/faq/concurrency/)
- [MongoDB transaction production considerations](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)
- [The Chubby lock service for loosely-coupled distributed systems](https://research.google/pubs/the-chubby-lock-service-for-loosely-coupled-distributed-systems/)
