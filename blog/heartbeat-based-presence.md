# System Design Foundations
System design becomes easier once the problem is framed in terms of state, time, access patterns, and failure. Before picking infrastructure, define what the system stores, how users interact with it, which paths need to be fast, and which guarantees must hold under load and failure.

## Incremental Building

A useful way to design systems is to build them incrementally instead of jumping straight to the final architecture. The first version should be a day-zero architecture: the simplest version that is correct, understandable, and able to serve the product's core workflow.

The point of a day-zero design is not to be impressive. The point is to create a clean baseline that can be stress-tested.

1. Start with a day-zero architecture.
2. See how each component behaves:
   under load
   at scale
3. Identify the bottleneck.
4. Re-architect the part that is failing.
5. Repeat.

This approach is valuable because most systems do not fail everywhere at once. They fail at specific pressure points:

- a database that cannot keep up with reads
- a write path that becomes too synchronous
- a cache that is missing too often
- a queue that grows without bound
- a service that becomes a hotspot because of skew

By starting simple, the team learns which bottleneck is real instead of guessing. That leads to better upgrades. Instead of adding a queue, cache, replica, or partitioning scheme because it sounds scalable, those changes are introduced only when the current design proves why they are necessary.

Day-zero architecture also improves communication. It gives everyone a shared baseline:

- what exists today
- what assumptions it makes
- what traffic it can handle
- what breaks first

That baseline makes later architectural decisions easier to justify. A redesign becomes a response to observed behavior, not an abstract preference.

## Problem Statement: Is a User Online or Offline?

Suppose a platform needs to show whether a user is online or offline. This is a good day-zero problem because it is simple, but it immediately forces decisions around freshness, storage, and API shape.

## Storage

At day zero, store the simplest possible model:

- `user_id -> online/offline`
- key type: user id
- value type: boolean

This is a key-value access pattern and is enough for the first version.

## Interfacing API

The API server supports:

- update a user's status
- read a user's status

That leads to a day-zero API shape like:

```http
POST /presence
{
  "user_id": "u1",
  "status": "online"
}
```

```http
GET /presence/u1
```

The response can be as simple as:

```json
{
  "user_id": "u1",
  "status": "offline"
}
```

## Why Batching Matters Early

Reading one user is simple. For multiple users, expose batching early:

```http
POST /presence/batch
{
  "ids": ["u1", "u2", "u3", "u4"]
}
```

And return:

```json
{
  "users": [
    {"user_id": "u1", "status": "online"},
    {"user_id": "u2", "status": "offline"},
    {"user_id": "u3", "status": "online"},
    {"user_id": "u4", "status": "offline"}
  ]
}
```

## Who Updates Presence?

Presence is pushed by the client, not pulled by the server.

When a user comes online, the client can send:

```http
POST /presence
{
  "user_id": "u1",
  "status": "online"
}
```

## Better Model: Store Last Heartbeat

The boolean model is easy, but it does not tell you when a user silently went away. A better model is:

- `user_id -> last_heartbeat_at`
- key type: user id
- value type: timestamp

Now the system stores the time it last received a heartbeat.

## Push vs Pull

Without a persistent connection, the backend cannot reliably ask the device for status. So presence is usually push-based: the client periodically sends updates.

## Periodic Heartbeat

Use heartbeats:

- client sends `online`
- client sends heartbeat every few seconds
- server marks offline after enough missed heartbeats

For example:

```http
POST /presence/heartbeat
{
  "user_id": "u1",
  "timestamp": "2026-05-05T23:40:00Z"
}
```

On each heartbeat, update:

```sql
UPDATE pulse
SET last_hb = now()
WHERE user_id = 'u1';
```

The server stores `last_hb` and infers offline if that timestamp is too old.

## What Is a Reasonable Threshold?

The threshold should be a small multiple of the heartbeat interval. For example:

- heartbeat every `10` seconds
- offline after `30` seconds

## Get Status API

The read path now derives status from time:

```http
GET /status/u1
```

- no entry -> offline
- `last_hb < now() - threshold` -> offline
- otherwise -> online

## Number Crunching

Now estimate the scale before changing the architecture.

- `user_id` as integer: `4B`
- `last_hb` as integer timestamp: `4B`
- total per entry: `8B`

Rough storage:

- `100` users -> `800B`
- `1 million` users -> about `8MB`
- `1 billion` users -> about `8GB`

That makes one thing clear: storage is not the bottleneck here. The full presence table can fit in memory on a single machine.

## What Actually Breaks First?

The API tier is stateless, so requests can go to any API server. That part scales horizontally without much trouble.

The harder question is the database. A single database instance may be able to store all the data, but it may not be able to handle the query load and update load at high concurrency. Every machine has a physical limit on CPU, memory bandwidth, network, and IOPS.

So the reason to split the database is often not raw storage size. It is compute and throughput.

If the data is partitioned across `3` nodes:

- each node stores roughly one third of the data
- each node handles roughly one third of the traffic

If more nodes are added, the per-node load drops further.

This is why number crunching matters. It tells you what the real bottleneck is. In this presence system, the first bottleneck is more likely query load than storage capacity.

## Better Operational Model: KV with Expiration

After the timestamp-based model, the next improvement is to use a key-value store with built-in expiration.

- key: `user_id`
- value: presence entry
- TTL: `30 seconds`

On every heartbeat, update the entry and move the expiration forward by `30` seconds. If heartbeats stop, the key naturally expires.

This is usually better than building a separate cleanup job. Do not reinvent the wheel with a cron job if the database already supports TTL or expiration natively.

## Database Choices

Once the required properties are clear, two reasonable options are:

- `Redis`
- `DynamoDB`

Both can support a key-value access pattern, expiration, and partitioning for request load.

## How to Think About the Choice

`Redis`

- good fit for very low-latency access
- often cost-effective for simple hot-key workloads
- comes with additional utilities and data structures
- may require more infrastructure decisions depending on how it is deployed

`DynamoDB`

- persistent by default
- fully managed
- attractive for startups that want lower operational headcount
- can be a strong choice when you want the cloud provider to handle scaling and operations

## Trade-Offs

The choice is not only technical. It can also depend on:

- cost
- persistence requirements
- latency targets
- operational headcount
- vendor lock-in or strategic dependency concerns
- whether you want extra built-in utilities beyond plain key-value storage

For example, some teams prefer a fully managed database early because it reduces infrastructure work. Others prefer a store like Redis because of latency characteristics or extra features.

## Final Rule

Use the built-in database feature if it already solves the problem. For presence, expiration support is one of those features.

And before making the final call, benchmark. The right database choice should be validated against real latency, throughput, and cost expectations rather than assumed in advance.

## Capacity Planning

Heartbeat systems create steady write traffic.

For example:

- one user sends `6` heartbeats per minute
- `1 million` active users means `6 million` heartbeat requests per minute
- if each heartbeat causes one database write, the database must handle `6 million` updates per minute

That means database selection is not enough by itself. You also need to think about:

- I/O capacity
- provisioned or on-demand throughput
- hardware limits
- partition count
- headroom under peak load

The storage size may be small, but write throughput can still be very large. So plan capacity around request volume, not just bytes stored.

## Connection Pooling

If the backend uses a SQL database, another practical bottleneck appears: connection setup.

An API server usually talks to the database over TCP. Writing a query may be fast, but establishing a fresh TCP connection for every request is expensive. Connection setup, acknowledgements, and teardown add avoidable overhead.

A simple improvement is to use a connection pool.

- when the server boots, create a small number of ready database connections
- keep them in a pool
- when a request arrives, borrow a connection from the pool
- after the query finishes, return it to the pool for reuse

This avoids paying the full connection setup cost on every request.

## Min and Max Pool Size

A pool usually has:

- a minimum number of warm connections
- a maximum number of allowed connections

For example:

- server starts with `2` open connections
- two requests arrive and both connections are busy
- a third request arrives, so the pool creates one more connection
- that connection is used and then returned to the pool

The pool can keep growing until it reaches the configured maximum.

If demand falls and extra connections stay unused for long enough, the pool can close them and shrink back down. That way the system keeps reuse benefits without holding unnecessary connections forever.

## Core Questions

- What are the main reads and writes?
- Which entities define the system?
- What must be strongly consistent?
- What can be approximate or eventually consistent?
- Where does ordering matter?

## Think in Properties, Not Products

One of the most common mistakes in system design is building an affinity for specific tools too early. A design discussion should not begin with "we need Redis" or "we need MySQL." It should begin with the properties the system actually needs.

Ask questions like these first:

- What data needs to be stored?
- How will that data be queried?
- Does the system require uniqueness guarantees?
- Do some operations need atomicity?
- Can the data be sharded cleanly?
- What level of consistency is required?
- Does the workload favor fast reads, fast writes, or balanced behavior?
- Is the data model relational, document-shaped, key-addressable, or time-series heavy?

This changes the conversation in a useful way. Instead of selecting a database by habit, the system is described in terms of constraints:

- uniqueness
- atomicity
- consistency
- durability
- partitionability
- ordering
- latency sensitivity

Once those properties are clear, the storage and infrastructure choices become much easier to justify. A tool should be chosen because it satisfies the core requirements of the system, not because it is familiar or popular.

## Thinking in Access Patterns

Start with the hottest operations:

1. Identify the most frequent reads.
2. Identify the most important writes.
3. Estimate rough scale for traffic, object count, and data size.
4. Design the storage model around the dominant path.
5. Add caching, queues, or replication only when a concrete bottleneck appears.
