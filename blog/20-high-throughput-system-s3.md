# Designing S3: A File Store That Scales Forever

This post builds Amazon S3 from first principles: a flat key-value store where the key is an object path and the value is a blob. Starting from one hard disk on one machine, it grows each component only when a named bottleneck appears: stateless API servers for request load, range partitions for routing and hot-key isolation, a partition control plane for logical ownership and failover, log-structured HDD storage for cheap capacity, and replication plus checksums for durability and integrity.

**Question: you have to store an unbounded number of files for millions of strangers, on the cheapest hardware you can buy, and you may *never* lose a file or return a wrong one. No "just use a database." You own the routing, the partitioning, the disks, and the crash recovery. What is the smallest design that is correct on day zero — and what is the *next* thing that breaks every time you 10× it?** The honest path runs straight through one HDD, a load balancer, three different ways to route a request, a control plane that owns partitions, and a log-structured storage tier — and by the end you've hand-built [Amazon S3](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html), the service that stores hundreds of trillions of objects at [eleven nines of durability](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DataDurability.html).

This is the third post in a small arc. We built a [word dictionary on S3](18-storage-engine-word-dictionary.md) and leaned on it as cheap, cold, embedded storage; we built a [Bitcask-style key-value engine](19-storage-engine-fast-kv-db.md) and learned why high-write systems go log-structured. S3 is where both ideas come home: it is, underneath all the distribution, **just a key-value store** — and its disks are **just a log**.

> **Memory hook:** *S3 is a key-value store where the key is a path and the value is a blob — everything hard about it comes from making that one idea cheap, infinite, and indestructible.*

---

## The brief

**Question: before drawing a single box — what is S3, in one sentence, stripped of all the marketing?**

<img src="../assets/s3/requirements.svg" alt="The brief for designing S3, framed as 'S3 is just a key-value store.' The map (key) is the full object path s3://bucket.s3.aws.com/images/logo.jpg and the value is the blob of bytes (the file). Three requirements, each with a consequence. One, Blob storage: store and serve opaque files of any size — the API is tiny, just PUT(path, blob), GET(path), DELETE(path), every operation touches exactly one key, no ranges and no joins. Two, Network file system: clients talk to it over HTTP from anywhere, so it behaves like a giant shared disk reachable by URL. Three, Scalable and cheap: it must grow to exabytes on the cheapest hardware (commodity spinning HDDs), so cost per byte is a first-class design constraint. Below, the three hard problems that the rest of the post attacks, drawn as the real difficulty: STORAGE — how do we get effectively infinite space out of finite 10TB disks; ACCESS / ROUTING — when a request arrives, which of thousands of disks holds this one key; HOT PARTITION / LOAD BALANCING — one popular bucket or prefix can swamp a single node, how do we spread and isolate load. A final highlighted insight at the bottom, marked 'the most important design decision': directories are logical and virtual — there is no real folder tree on disk, the slashes in the path are just characters in a flat key, so a bucket is a naming convention, not a place. Everything is path-based." width="1000">

S3's public API is almost insultingly small: <span style="color:#ff8bd2"><strong>`PUT(path, blob)`</strong></span>, <span style="color:#8aff8a"><strong>`GET(path)`</strong></span>, <span style="color:#ff8bd2"><strong>`DELETE(path)`</strong></span>. You hand it a path and a pile of bytes; later you hand it the same path and get the bytes back. That is a <span style="color:#ffff99"><strong>key-value store</strong></span> where the key is a string and the value is a blob. Every operation touches exactly one key — no ranges, no scans, no joins — which is the same gift the [Bitcask post](19-storage-engine-fast-kv-db.md) leaned on, now at planetary scale.

The requirements are three words: **blob storage**, **network file system**, **scalable and cheap**. "Blob" means the value is opaque — S3 never looks inside your file. "Network file system" means it is reachable by URL from anywhere, behaving like one infinite shared disk. "Cheap" is not a nice-to-have; it is a hard design constraint that will force us onto <span style="color:#ffff99"><strong>commodity spinning disks</strong></span> and shape the entire storage layer.

Three things are genuinely hard, and the rest of the post is just attacking them in order: <span style="color:#ffff99"><strong>storage</strong></span> (infinite space from finite disks), <span style="color:#8aff8a"><strong>access / routing</strong></span> (which of thousands of disks holds this one key?), and <span style="color:#ff8a8a"><strong>hot partitions</strong></span> (one popular bucket must not melt one node).

And one insight reframes everything — the most important design decision in the whole system. **A directory is a logical, virtual entity.** There is no folder tree on disk. The slashes in `images/logo.jpg` are just characters in a flat key. A "bucket" is a naming convention, not a place. Once you accept that everything is <span style="color:#ffff99"><strong>path-based</strong></span> and flat, routing becomes a question about strings, not about a filesystem.

> **Memory hook:** *the API is PUT/GET/DELETE on one path; the three hard problems are storage, routing, and hot partitions; and the unlock is that directories are fake — it's all one flat keyspace of paths.*

---

## Section 1 — Day Zero: S3 on One Laptop

**Question: forget distribution. What is the absolute smallest thing that already *is* S3 — something you could build this afternoon on one machine?**

Strip away every "distributed" word and S3 is a <span style="color:#8aff8a"><strong>static file server</strong></span>. Take one computer, plug in a hard disk over USB, and run a small program that exposes four operations — create, read, write, delete — over HTTP. That's it. That program is the entire system.

<img src="../assets/s3/day-zero.svg" alt="Day-zero S3 architecture running locally on one computer. Center: a single box labelled 'Computer'. Two users (stick figures) on the left send requests to it over the network. Inside the computer, two responsibilities are bracketed and labelled. One, the API: it exposes operations to talk to the hard disk — create, read, write, delete — over HTTP. Two, to the right, a hard disk drawn as a small drive labelled 'Storage', connected to the computer by a line labelled 'HDD connected via USB'; the storage holds all the files. A caption states: S3 is just like a static file server. An example request is shown as the URL s3://bucket.s3.aws.com/images/logo.jpg, with the flow written out: the request comes to the API server, the API server goes to storage, fetches the file at the location specified by the path, and returns it. At the bottom, a forward-looking question in a highlighted box: if one HDD is not enough, what is the next stage of evolution? The answer is teased as: two HDDs." width="1000">

A request like `GET s3://bucket.s3.aws.com/images/logo.jpg` does exactly what your intuition says: it arrives at the <span style="color:#8aff8a"><strong>API server</strong></span>, which walks to <span style="color:#ffff99"><strong>storage</strong></span>, finds the file at the location the path names, and returns the bytes. A `PUT` writes a file; a `DELETE` removes it. There is no magic here, and that is the point: **the data model never gets more complicated than this.** Everything we add from now on exists only to keep this simple behavior working as the numbers explode.

So let's explode the first number. One disk fills up, or one disk dies. **What's the next stage of evolution?** The dumbest possible answer is the right one: <span style="color:#ff8bd2"><strong>plug in a second HDD</strong></span>. Now we have two disks and one immediate new problem — when a file comes in, which disk does it go on, and when a read comes in, which disk do we look at? Hold that question; it becomes *the* question.

> **Memory hook:** *S3 on day zero is a static file server: an HTTP API in front of one disk. Adding the second disk is what creates every interesting problem that follows.*

---

## Section 2 — Splitting the Two Jobs: API Scales, Storage Scales, Separately

**Question: one computer can't serve all the requests *and* hold all the files forever. When you add machines, what is the one structural mistake that will haunt you if you get it wrong on day one?**

The instinct is "add more computers, each with its own disks." That's the trap. If every machine owns both the serving *and* the storing, then the file you want is stuck on whichever machine happens to hold it, and a burst of traffic to that one file melts that one machine while the others sit idle. The two jobs scale for **completely different reasons** — serving scales with *request rate*, storage scales with *bytes* — so they must scale independently.

<img src="../assets/s3/scale-out.svg" alt="Separating the two jobs so each scales independently. Top, the starting point: one computer with HDD1 and HDD2 attached over USB, and the caption 'what if one computer cannot support all the requests?' The answer: add more machines — but first make the storage central and put a load balancer in front. Bottom, the resulting two-plane architecture. On the left, users (stick figures) hit a Load Balancer (LB). The LB fans out to a fleet of stateless S3 API servers (drawn as two identical boxes labelled 'S3 API'), bracketed and labelled 'API — scales independently' (you add API servers when request rate grows). On the right, all the API servers connect to a single shared, network-attached Storage tier (drawn as a box labelled 'Storage, network-attached', sitting on top of physical hard disks), bracketed and labelled 'Storage — scales independently' (you add disks when bytes grow). The key idea: the API tier is stateless and holds no files, so any API server can handle any request; the storage tier holds the bytes and is shared by all of them. A small note points at the storage box: this central storage is itself the next thing we must scale. Caption: let's scale storage." width="1000">

The fix is to **split the system into two planes** that grow on their own axes:

- A <span style="color:#8aff8a"><strong>stateless API tier</strong></span>: a fleet of identical S3 API servers behind a <span style="color:#93c5fd"><strong>load balancer</strong></span>. They hold no files. Any server can handle any request, so you scale request throughput by simply adding more of them. (This is exactly the [load balancer](06-distributed-load-balancer.md) story from earlier in the handbook.)
- A <span style="color:#ffff99"><strong>shared storage tier</strong></span>: central, network-attached, holding the actual bytes. You scale capacity by adding disks.

This split is the spine of S3. The API servers are cattle — interchangeable, disposable, horizontally scaled. The storage is the precious thing they all read from and write to. Now the obvious next question stares at us: that "central storage" is one box in the drawing, but it can't be one box in reality. **How do we turn a pile of finite disks into one infinite store — and how does an API server know which disk holds a given key?** That second half is the routing problem, and it's deep enough to deserve three sections.

> **Memory hook:** *split serving from storing. Stateless API servers behind a load balancer scale with traffic; shared storage scales with bytes. Never let one machine own both for the same data.*

---

## Section 3 — The Routing Problem, Attempt 1: Hash-Based Routing

**Question: storage is now a rack of disks — say twenty HDDs, 10 TB each. A `PUT` arrives for `bucket/images/logo.jpg`. Which disk does it go on, and how will a later `GET` find that exact same disk without asking all twenty?**

This is the heart of the system. Remember the unlock: there are no real directories, just a flat key (the path). So routing is a pure function: given a string, pick a disk. The first idea everyone reaches for is to <span style="color:#ffff99"><strong>hash the path</strong></span>.

<img src="../assets/s3/hash-routing.svg" alt="Routing strategy 1: hash-based routing. Center: an S3 API server holds an object path, bucket.s3.com/images/logo.jpg. It computes hash(path) and takes that modulo the number of disks to pick exactly one HDD out of a row of disks (drawn as a rack of vertical drive slots), then stores or reads the object there. A dotted arrow arcs from the API server to the chosen disk to show the deterministic pick. The same function runs on read, so a GET lands on the same disk a PUT chose — no broadcast needed. Two columns below. Advantages (green): near-random allocation, near-uniform distribution across all disks, and no static or explicit configuration to maintain — the formula decides everything. Disadvantages (red): first, if the number of disks changes (a disk is added or fails), the modulo changes for almost every key, forcing a massive rebalancing and reindexing as nearly all objects move; second, files of the same bucket are scattered across many different disks, so there is no locality; third, and worst, there is no tenant isolation — files from many different customers land on the same disk, so a traffic spike from one customer degrades performance for everyone sharing that disk. The takeaway: hashing distributes evenly but loses control — it cannot keep a tenant's data together or apart." width="1000">

The scheme is dead simple: compute `hash(path) % N` where `N` is the number of disks, and that's your disk — for both writes and reads. Because the function is deterministic, a `GET` recomputes the same number and lands on the same disk a `PUT` chose. No broadcast, no lookup table.

The <span style="color:#8aff8a"><strong>advantages</strong></span> are real: near-random, near-uniform spread across all disks, and **zero configuration** — the formula decides everything. For many systems this is enough. But for S3 it has three <span style="color:#ff8a8a"><strong>fatal flaws</strong></span>:

- **Rebalancing storms.** The moment `N` changes — a disk is added, or one dies — `% N` becomes `% (N±1)` and the answer changes for *almost every key*. Nearly all data has to physically move. At exabyte scale this is a non-starter.
- **No locality.** Files in the same bucket scatter across every disk. A customer listing their bucket touches the whole fleet.
- **No tenant isolation.** This is the killer. Files from many customers share each disk, so <span style="color:#ff8a8a"><strong>one customer's traffic spike degrades everyone</strong></span> on that disk. In a multi-tenant service, that's unacceptable.

Hashing buys uniformity by *surrendering control*. We want the opposite: control over where data lives. But before we fix that, there's a well-known patch for just the first flaw.

> **Memory hook:** *hash(path) % N picks a disk with zero config and perfect spread — but changing N reshuffles everything, and it can neither keep one tenant together nor keep tenants apart.*

---

## Section 4 — Attempt 2: Consistent Hashing

**Question: the rebalancing storm came from `% N` — every key's home depends on the *total count* of disks. Can we hash in a way where adding or removing one disk only moves the keys near *that* disk, and leaves everyone else alone?**

Yes — that's exactly what <span style="color:#ffff99"><strong>consistent hashing</strong></span> was invented for, and it's the same ring we used in the [distributed KV store](04-database-distributed-kv-store-on-relational-database.md). Map both the disks *and* the keys onto one circular hash space. A key is owned by the first disk you meet walking clockwise from where the key lands.

<img src="../assets/s3/consistent-hashing.svg" alt="Routing strategy 2: consistent hashing. Center: a large circle representing the hash ring (the hash space wrapped into a circle from 0 around to its maximum and back to 0). Four disks (HDD 1 through HDD 4) are placed at points around the ring, each shown as a small rectangle tangent to the circle. An object key, bucket.s3.aws.com/images/logo.jpg, is hashed to a point on the ring (drawn as a small pink tick on the rim) and a short clockwise arrow shows it being assigned to the next disk clockwise — here HDD 1 — which is said to 'own' that arc of the ring. The rule, stated above: partitions and keys sit on one consistent hash ring, and the node to the (clockwise) right owns the data. Advantages (green): minimal data transfer when a storage node is added or removed — only the keys in the arc next to the changed node move, not the whole keyspace, which fixes the rebalancing storm of plain hashing; plus near-random, near-uniform distribution. Disadvantage (red): still no tenant isolation — a workload spike on one node still affects its neighbors, and one tenant's files are still spread across many nodes around the ring, e.g. a/b.png lands on HDD1 while a/c.png lands on HDD2. The takeaway: consistent hashing fixes elastic add/remove but still scatters a tenant and still cannot isolate load." width="1000">

The win is precise. When a disk joins or leaves, only the keys in the <span style="color:#93c5fd"><strong>one arc</strong></span> next to it move; everyone else stays put. The rebalancing storm is gone, and the distribution is still near-uniform. This is why consistent hashing shows up in Dynamo, Cassandra, and countless caches.

But notice what it *didn't* fix. It's still a hash, so it still <span style="color:#ff8a8a"><strong>scatters a tenant's files</strong></span> around the ring (`a/b.png` on HDD1, `a/c.png` on HDD2), and it still gives <span style="color:#ff8a8a"><strong>no tenant isolation</strong></span> — a hot key still hammers whichever node owns it, and that node's neighbors feel it. We solved elasticity but not control. For S3, control is the requirement we keep circling back to. So we abandon hashing entirely.

> **Memory hook:** *consistent hashing = keys and nodes on a ring, clockwise node owns the key. Adding/removing a node moves only one arc — elasticity solved — but it still scatters tenants and still can't isolate load.*

---

## Section 5 — Attempt 3: Range-Based Partitioning (the one S3 uses)

**Question: both hashing schemes destroy locality on purpose — that's *why* they spread evenly. But S3 needs the opposite: a tenant's data kept together, and one tenant kept away from another. What if we just... didn't hash, and assigned *contiguous ranges* of the keyspace to nodes instead?**

That's <span style="color:#ffff99"><strong>range-based partitioning</strong></span>, and it's the decision real S3 makes. Keep the keys in their natural <span style="color:#ffff99"><strong>sorted (lexicographic) order</strong></span> and chop that ordered space into contiguous ranges — `[a,e]`, `[f,h]`, `[i,k]`, and so on. Each range is a <span style="color:#ffff99"><strong>partition</strong></span>, and each partition lives on a node.

<img src="../assets/s3/range-partitioning.svg" alt="Routing strategy 3: range-based partitioning. Top callout (highlighted): hash-based approaches lose locality of objects, and we want more control over the partitioning logic. Center: the sorted keyspace is divided into contiguous lexicographic ranges, each drawn as a labelled partition holding a scatter of object dots: [a,e], [f,h], [i,k], [l,r], [s,u], [v,z]. Because keys stay in sorted order, all objects whose paths fall in a range live together on one partition — for example amzn/images/a.jpg and amzn/images/b.jpg are adjacent and land in the same partition. A column of benefits (green/yellow): easier performance isolation (a noisy tenant can be confined to its own partition), locality of objects (a bucket's keys stay together so listing and scanning are cheap), and much more control over where objects reside (you place ranges deliberately instead of letting a hash decide). The contrast with the prior two sections is the whole point: ranges trade automatic uniformity for deliberate control. The takeaway: keep keys sorted and assign contiguous ranges to nodes — now a tenant's objects are contiguous and a tenant can be isolated, which is exactly what a multi-tenant object store needs." width="1000">

Now the properties flip in our favor:

- <span style="color:#8aff8a"><strong>Locality.</strong></span> Keys that sort together live together. `amzn/images/a.jpg` and `amzn/images/b.jpg` are neighbors on the same partition, so listing a bucket or scanning a prefix is cheap and local.
- <span style="color:#ffff99"><strong>Tenant isolation.</strong></span> Because a tenant's keys share a prefix, they fall in a contiguous range — so you can deliberately put a noisy tenant on its own partition and stop it from hurting anyone else.
- <span style="color:#93c5fd"><strong>Control.</strong></span> You decide where ranges go, instead of surrendering that to a hash function.

This is also exactly how real S3 behaves: object keys are stored in [lexicographic (UTF-8) order](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html) and the index is partitioned by key range. It's why the old advice was to add a random prefix to your keys — and why that advice is now obsolete: S3 auto-partitions hot ranges for you. We trade automatic uniformity for deliberate control, and for a multi-tenant store, control wins. But control has a cost: ranges can become uneven. One range can get *hot*.

> **Memory hook:** *range partitioning keeps keys sorted and gives contiguous ranges to nodes — buying locality, tenant isolation, and control, at the price of ranges that can grow lopsided and hot.*

---

## Section 6 — Hot Partitions: Split, and Throttle

**Question: a celebrity uploads to one bucket and the whole internet pulls from it. That bucket's range is now a furnace while its neighbors idle. With contiguous ranges, what's the natural move to cool it down — and what do you do while the cooling happens?**

Here range partitioning pays off beautifully, because the fix is almost trivial: **when a partition gets hot, split it in two.** Take the hot range `[l,r]` and cut it into two <span style="color:#ffff99"><strong>mutually exclusive</strong></span> halves, `[l,m]` and `[n,r]`, and move one half to a fresh node. Two nodes now share what one was drowning in. Because ranges are contiguous and ordered, the split is a clean cut with no key left ambiguous — something that's painful with a hash.

<img src="../assets/s3/hot-partition-split.svg" alt="Dealing with hot partitions in a range-partitioned store, two complementary tactics. Main diagram, splitting: a single Hot Node owning range [l,r] (drawn as a partition crammed with object dots, outlined in red to show overload) is split into two mutually exclusive partitions — Node 1 owning [l,m] and Node 2 owning [n,r] — with an arrow from the hot node branching down to the two new nodes. The split point is chosen so the two ranges are disjoint and together cover the original, and one half is moved to a new physical node, halving the load. A note: this is simple precisely because the partitioning is range-based — a contiguous range cuts cleanly in two, whereas a hash scheme cannot. Second tactic, throttling (blue): while a split is in progress, or to protect against abuse, cap the number of requests per account — if requests go beyond a certain per-account limit, the extra requests are throttled (S3 returns a 503 SlowDown). Real-world note tied to this: S3 sustains roughly 3,500 writes and 5,500 reads per second per partitioned prefix, with no limit on the number of prefixes, and it auto-splits hot partitions behind the scenes (taking 30 to 60 minutes), returning 503 SlowDown until the new partitioning is ready. The takeaway: splitting cools a hot range structurally and permanently; throttling protects the node in the meantime and caps any single tenant." width="1000">

The structural fix is the <span style="color:#93c5fd"><strong>split</strong></span>; it permanently spreads the load. But a split takes time, so we need an immediate shield: <span style="color:#93c5fd"><strong>throttling</strong></span>. Cap requests per account, and when a tenant exceeds its limit, reject the excess with a "slow down" signal rather than letting it take the node down with it.

This is precisely how production S3 works. Each partitioned prefix sustains about [3,500 writes and 5,500 reads per second](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html), there's no limit on the number of prefixes, and S3 <span style="color:#93c5fd"><strong>auto-splits hot partitions</strong></span> behind the scenes — a process that takes 30–60 minutes, during which you may see `503 SlowDown` while the new partitioning settles. Splitting is the permanent cure; throttling is the bandage that holds until the cure takes effect.

> **Memory hook:** *hot range? split it into two disjoint ranges on two nodes — trivial because ranges are contiguous — and throttle per account (503 SlowDown) while the split is in flight.*

---

## Section 7 — A Quick Detour: Many Logical Shards on Few Machines

**Question: splitting a live partition means physically moving data while traffic flows over it — fiddly and slow. Is there a way to make rebalancing almost free, so moving load between machines doesn't mean re-cutting ranges at all?**

There's a classic trick worth a short detour, because it shapes the design that follows. Instead of a few big partitions that you split under pressure, create a **large number of small logical shards up front** and pack many of them onto each physical machine.

<img src="../assets/s3/logical-shards.svg" alt="A quick detour: solving hot nodes with many logical shards on few physical machines. Three physical nodes are drawn as large boxes (Node 1, Node 2, Node 3). Inside them sit small rectangles, each one a 'logical shard' (a logical partition); the shards are distributed unevenly to make the point — Node 1 holds one shard, Node 2 holds three, Node 3 holds three. An arrow labels one small rectangle as 'logical shard'. The idea: you over-provision logical shards far beyond the number of machines, so each machine just hosts a handful of them. To rebalance or to cool a hot machine, you move an entire logical shard from one node to another — you never re-cut a range or move individual keys. Real-world notes: Elasticsearch uses this strategy (its shards, visible via the HEAD plugin), and Instagram did this with their main posts table. Key reason, highlighted: moving one self-contained, mutually-exclusive subset of data across data nodes is far simpler and more efficient than splitting live ranges — it makes load balancing a matter of relocating whole shards. The takeaway: pre-create many logical shards and treat the shard, not the key, as the unit of movement, so rebalancing is just 'pick up this shard, put it on that node.'" width="1000">

Now the <span style="color:#ffff99"><strong>unit of movement is a whole shard</strong></span>, not a key and not a range you have to re-cut. To cool a hot machine, you pick up one of its logical shards and drop it on a quieter machine. That's it. Moving a <span style="color:#ffff99"><strong>self-contained, mutually-exclusive subset</strong></span> of data is dramatically simpler than splitting a live range.

This isn't theoretical: <span style="color:#93c5fd"><strong>Elasticsearch</strong></span> works exactly this way (its shards, visible through the HEAD plugin), and <span style="color:#93c5fd"><strong>Instagram</strong></span> famously pre-sharded their main posts table into thousands of logical shards over a handful of Postgres machines. The lesson to carry forward: **make the logical partition the unit of ownership and movement.** That idea is about to become the backbone of S3's control plane.

> **Memory hook:** *pre-create many small logical shards on few machines; rebalance by moving a whole shard, never by re-cutting ranges or moving keys one at a time. (Elasticsearch, Instagram.)*

---

## Section 8 — Who Knows Where Things Live? The Partition Map Table and Partition Manager

**Question: we now have logical partitions, and they move between machines for splits and rebalancing. So when a request arrives for key `images/a.png`, *something* has to answer "which node holds that range right now?" — and *something* has to decide when and where partitions move. What are those two somethings?**

A logical partition is just a range on paper — `[a,b]`. For it to be real, two new components must exist, and naming them is half the design.

<img src="../assets/s3/partition-map-table.svg" alt="Introducing the two control-plane components that track and move partitions. Two questions drive the diagram. Question one: how do we know which partition is on which node? Answer: there must be an entry somewhere — the Partition Map Table (PMT), drawn as a small database cylinder labelled P.M.T. Question two: who manages the movement of partitions? Answer: the Partition Manager, drawn as a control box. The body of the diagram: the Partition Manager on the left connects to three logical partitions (Partition 1, Partition 2, Partition 3, drawn as boxes), which in turn map onto a rack of physical storage disks on the right. The Partition Manager also owns the PMT cylinder beneath it. The PMT contents are listed as a table mapping each partition's range to a node (its disk/rack location): partition 1 [a,b] maps to Node 1, partition 2 [c,f] maps to Node 7, partition 3 [g,z] maps to Node 8. A pointed note: but a partition is just a logical range, so there still has to be a server that actually 'owns' and serves that partition — which is the cliffhanger leading to the next section on partition servers. The takeaway: the PMT is the lookup that turns a key into a node, and the Partition Manager is the brain that decides when partitions split, move, or get reassigned." width="1000">

- The <span style="color:#ffff99"><strong>Partition Map Table (PMT)</strong></span> is the lookup. It records, for every partition, its key range and which node currently holds it: `partition 1 [a,b] → Node 1`, `partition 2 [c,f] → Node 7`, and so on. An API server resolves a key by finding the range it falls into and reading off the node. The PMT is the <span style="color:#ffff99"><strong>source of truth</strong></span> for "where does this key live right now?"
- The <span style="color:#93c5fd"><strong>Partition Manager</strong></span> is the brain. It decides when a partition is too hot and must split, where new partitions go, and how to rebalance — and it writes every such decision back into the PMT.

This is the classic <span style="color:#93c5fd"><strong>control plane</strong></span> / <span style="color:#8aff8a"><strong>data plane</strong></span> split. The PMT and Partition Manager are cold-path coordination; the API servers and disks are the hot path serving bytes. But the diagram still hides a gap: a partition is *logical*. Some actual process has to **own** a partition — accept its writes, serve its reads, run its maintenance. That process is the partition server.

> **Memory hook:** *the Partition Map Table answers "which node holds this key's range?"; the Partition Manager decides when partitions split and move. Lookup vs. brain — the two halves of the control plane.*

---

## Section 9 — Partition Servers: Logical Ownership and Compute Isolation

**Question: the PMT says a range lives on "Node 8." But a node is just a box of disks — it doesn't *do* anything. Who actually accepts a write for that range, serves its reads, and runs its compaction? And why separate that owner from the disk underneath?**

Enter the <span style="color:#ffff99"><strong>partition server</strong></span>: the process that holds <span style="color:#ffff99"><strong>logical ownership</strong></span> of one or more partitions. When a request for `images/a.png` resolves to a partition, it's routed to the partition server that *owns* that partition, and that server does the work — read, write, merge, compact.

<img src="../assets/s3/partition-servers.svg" alt="Partition servers as the owners of logical partitions, separating compute from storage. Top half: the Partition Manager (left) connects to three Partition Servers (Partition Server 1, 2, 3, drawn as boxes), each shown owning a small partition (a little rectangle inside or beside it). The partition servers in turn connect to the actual storage — a rack of physical disks on the right labelled 'actual storage'. A caption points at the partition servers: this is logical ownership of the partition, and the key benefit is that compute load is isolated — each partition's request-handling CPU work lives on its owning server, so a busy partition burns its own server's CPU, not a shared disk's. The Partition Manager owns the Partition Map Table (a cylinder beneath it). The PMT now maps three things: Partition Server 1 owns Partition [a,b] which lives on Node 1 on some disk/rack; Partition Server 2 owns [c,f] on Node 7; Partition Server 3 owns [g,z] on Node 8 — with a note that the node/disk/rack layer can be abstracted away. A rule, starred: one partition is owned by exactly one partition server (so there is a single writer and no conflict), but one partition server can own multiple partitions. Bottom half, 'making the partition manager no SPOF': the Partition Manager is replicated into a stack of instances that run leader election via Raft or Paxos, so if the leader dies another takes over; the replicated managers connect to the same partition servers and storage. The takeaway: the partition server is the single logical owner that isolates per-partition compute, the disk is just bytes underneath, and the manager is made highly available by replication plus leader election." width="1000">

Two design rules make this clean and powerful:

- **One partition is owned by exactly one partition server** — so there is a <span style="color:#ff8bd2"><strong>single writer</strong></span> per partition, and no write conflicts to resolve. **But one partition server can own many partitions.** This is the logical-shard idea from Section 7, now load-bearing: ownership is a lightweight assignment in the PMT, not a data move.
- **Compute is separated from storage.** The partition server is *compute* (CPU to handle requests); the node/disk is *bytes*. A busy partition burns its owner's CPU, not the disk's — so <span style="color:#8aff8a"><strong>compute load is isolated</strong></span> per partition. The PMT now records the full chain: `Partition Server 1 → [a,b] → Node 1 → disk/rack`, and the disk layer can be abstracted away.

One last gap, and it's a scary one: the Partition Manager is starting to look like a <span style="color:#ff8a8a"><strong>single point of failure</strong></span>. If the brain dies, no splits, no failovers, no rebalancing. So we don't run one — we run a **replicated set** that elects a leader via <span style="color:#93c5fd"><strong>Raft or Paxos</strong></span> (the same [leader-election machinery](07-distributed-lock-manager.md) from the lock-manager post). If the leader falls over, a follower takes the crown and the control plane keeps thinking.

> **Memory hook:** *a partition server is the single logical owner of a partition (one owner per partition, many partitions per owner) — it isolates compute from the dumb disk below. Replicate the Partition Manager + leader-elect so the brain is never a SPOF.*

---

## Section 10 — The Whole Control Plane, and What Happens When a Server Dies

**Question: let's assemble the front of the system and then break it on purpose. A partition server crashes at 3 a.m. The partitions it owned are now orphaned. How does S3 keep serving those keys without a human waking up?**

Here is the full request-and-control picture, and it contains the single most important design decision in the system.

<img src="../assets/s3/control-plane.svg" alt="The complete S3 control plane and its failover behavior — labelled 'the important design decision.' Top-left, inside a highlighted boundary box: a user hits one of several stateless S3 API servers (two boxes drawn), and those API servers read from a shared, replicated Partition Map Table (a cylinder with replicas, center). The API servers use the PMT to resolve a key to its owning partition server. Top-right: the Partition Manager, drawn as a replicated stack of boxes (highly available via leader election), also reads and writes the PMT, and sends periodic health checks down to the partition servers (a downward arrow labelled 'healthcheck'), and runs rebalancing. Middle row: three Partition Servers, each owning some partitions. Bottom: all partition servers sit on top of a shared rack of physical storage disks. Blue lines connect the API servers down to the partition servers (the request path: API server looks up the PMT, then forwards to the owning partition server); yellow lines connect partition servers to storage. The failure scenario, written at the bottom as the key question: what happens when a partition server goes down? Answer (highlighted): because partitions are logical, the partitions owned by the dead server are simply reassigned by the Partition Manager to other, healthy partition servers — a seamless transition, since nothing physical has to move; only the ownership entry in the PMT changes, and the new owner reads the same bytes from the same shared storage. The takeaway: separating logical ownership (partition server) from physical bytes (shared storage) is what makes failover instant — reassigning a dead server's partitions is just editing the PMT." width="1180">

Trace a request end to end. A user hits the load balancer, which lands on any <span style="color:#8aff8a"><strong>stateless API server</strong></span>. That server reads the <span style="color:#ffff99"><strong>PMT</strong></span> to resolve the key's range to its owning <span style="color:#ff8bd2"><strong>partition server</strong></span>, and forwards the request there. The partition server reads or writes the bytes on <span style="color:#ffff99"><strong>shared storage</strong></span>. Meanwhile, off the request path, the replicated <span style="color:#93c5fd"><strong>Partition Manager</strong></span> health-checks every partition server and rebalances partitions as load shifts.

Now break it. A partition server dies. Because ownership is <span style="color:#ffff99"><strong>logical</strong></span> and the bytes live on <span style="color:#ffff99"><strong>shared storage</strong></span> that the dead server didn't *contain*, recovery is almost embarrassingly cheap: the Partition Manager notices the missed health check and <span style="color:#93c5fd"><strong>reassigns</strong></span> the dead server's partitions to healthy partition servers. **No data moves.** The new owner just starts reading the same bytes from the same disks, and the PMT entry is updated to point at it. <span style="color:#8aff8a"><strong>Seamless transition.</strong></span>

**This is the payoff of separating logical ownership from physical bytes.** It's why we fought so hard for it across the last three sections: failover becomes a one-line edit to a table, not a frantic data migration. The control plane is done. Now we finally descend into the disks themselves.

> **Memory hook:** *API server → PMT lookup → owning partition server → shared storage. When a partition server dies, the manager just reassigns its (logical) partitions to live servers — no data moves, because the bytes were never inside the server.*

---

## Section 11 — The Storage Layer: Infinite, Cheap, and Log-Structured

**Question: under every partition server is the real thing — the disks. What kind of storage makes S3 both *cheap* and *infinite*, and how does a single disk stay fast when the whole premise is "use the slowest, cheapest hardware"?**

Walk down the [storage hierarchy](19-storage-engine-fast-kv-db.md) — cache, RAM, SSD, disk, tape — and "cheap and huge" points straight at the <span style="color:#ffff99"><strong>spinning HDD</strong></span>. So the storage tier is racks of commodity machines, each holding 10–20 TB across many disks, and "infinite" is just **add more racks**. Capacity scales horizontally and forever.

<img src="../assets/s3/storage-hierarchy.svg" alt="The storage layer: choosing the medium and getting infinite capacity. Top: the storage hierarchy drawn as a horizontal axis from fast/expensive/small on the left to slow/cheap/huge on the right — cache, RAM, SSD, then a gap, then Disk (HDD), then Tape storage. A bracket marks Disk as the chosen medium: it is cheap per byte and durable. Bottom-left: a storage rack drawn as a tall cabinet of horizontal slots, each slot a node of 10 to 20 TB, with 20 to 30 nodes per rack; the rack is labelled, and 'infinite storage' is achieved by simply adding more racks. Bottom-right, the reasoning chain for the disk choice: spinning disk HDD is cheap; to get good write performance out of a slow HDD you must avoid disk seeks; the way to avoid seeks is log-structured storage (a sequential, append-only filesystem) — the exact Bitcask lesson reused. So each node runs a log-structured store on cheap HDDs, writing sequentially. The closing question that sets up the next diagram: how do we get infinite / scalable storage, and how does a partition server actually write to these disks? The takeaway: cheap HDDs in racks give infinite capacity, and log-structured (append-only, seek-free) writes are what keep those cheap disks fast." width="1000">

But a raw HDD is slow in exactly one way: the <span style="color:#ff8a8a"><strong>seek</strong></span>. The fix is the central lesson of the [Bitcask post](19-storage-engine-fast-kv-db.md): go <span style="color:#ffff99"><strong>log-structured</strong></span>. Never overwrite in place; only ever append to the end of a file. Sequential writes turn the HDD's weakness into its strength, and even cheap spinning rust writes at full speed. (Real S3's storage backend, [ShardStore](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3), is exactly this — a log-structured merge tree, sequential writes on HDD.)

So how does a partition server actually write? It always appends to the <span style="color:#ff8bd2"><strong>HEAD</strong></span> — the one *active* disk currently taking writes. When that disk fills to about 70%, it's "moved down" (frozen, read-only) and the HEAD advances to a fresh disk. Background <span style="color:#93c5fd"><strong>merge and compaction</strong></span> defragment the frozen disks, reclaiming the space left by overwrites and deletes — the same compaction we built in the KV engine.

<img src="../assets/s3/active-hdd.svg" alt="How a partition server writes to the storage nodes — the active-HEAD model and the FUSE mount. Top: a single Partition Server writes to a stack of storage slots on one node; an arrow into the top slot is labelled 'writes to the HEAD' and the top slot is marked the active HDD. The rule, stated to the right: the partition server always writes to the HEAD; when a node reaches about 70% of capacity it is 'moved down' (frozen as read-only) and the HEAD advances to the next slot. A storage monitor (a small box) watches the node's capacity and triggers the rotation, plus background merge-and-compaction / defragmenting to reclaim space from overwritten and deleted objects. Two example objects show variable-size blobs: s3://amzn/img/a.jpg is 1KB and s3://amzn/imp/a.jpg is 2KB, both appended to the log. Bottom: three Partition Servers are each 'mounted' to three storage nodes by crossing colored lines (a many-to-many mount), and the file operations they issue over the mount are listed: open, read, close, write, seek, stat. Each storage node has its own Storage Monitor box beneath it. The mount mechanism is labelled FUSE (filesystem in userspace), which lets a partition server treat remote storage-node files as if they were local files. The takeaway: writes always go to the active HEAD disk and roll forward as disks fill; a storage monitor handles rotation and compaction; and partition servers reach the bytes through a FUSE mount exposing ordinary file operations." width="1000">

A small but real detail: partition servers reach the disks through a <span style="color:#93c5fd"><strong>FUSE mount</strong></span> (filesystem in userspace), so remote storage-node files look like ordinary local files — `open`, `read`, `write`, `seek`, `close`, `stat`. A <span style="color:#93c5fd"><strong>storage monitor</strong></span> on each node watches capacity, triggers HEAD rotation, and runs compaction. Writes roll forward across disks; reads follow the index to whichever frozen or active disk holds the bytes.

> **Memory hook:** *cheap HDDs in racks = infinite capacity; log-structured append = fast on cheap disks; always write to the active HEAD, freeze at ~70% and roll forward; reach the bytes over a FUSE mount, and let a storage monitor compact behind you.*

---

## Section 12 — Durability: Never Lose a Byte

**Question: cheap disks fail constantly — at S3's scale, several die every hour. Yet S3 promises it will lose roughly one object in ten thousand once every ten million years. With hardware that *breaks*, how do you get [eleven nines](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DataDurability.html)?**

There is exactly one way to survive losing a disk: have the data somewhere else too. <span style="color:#ffff99"><strong>Durability is redundancy.</strong></span> S3 layers it at every distance.

<img src="../assets/s3/durability.svg" alt="Data durability through layered replication and redundancy. Center: a primary Storage Node replicates the same object outward at three distances, each with a different consistency and latency tradeoff. One, SYNC replication to Storage Node 2 within the same rack — a solid arrow labelled SYNC, noted as 'replicated within rack', fast and on the write path so the write is not acknowledged until the in-rack copy is safe. Two, ASYNC replication to Storage Node 3 in another data center — a dotted arrow labelled ASYNC, noted as 'replicated across DC (availability zone)'. Three, ASYNC replication to a far Storage Node 8 across geography — a dotted arrow labelled ASYNC, noted as 'replicated across geography', drawn reaching a distant globe icon, for disaster recovery. The principle: synchronous nearby for a durable acknowledgement, asynchronous far away to survive a whole data center or region failure, which is how S3 spreads every object across at least three availability zones. Below: within a single storage node, durability comes from RAID (and, in real S3, erasure coding with Reed-Solomon, which stores an object as data shards plus parity shards so it can be reconstructed even if several shards are lost — far cheaper in space than full copies). A red callout: the only way to achieve durability is duplicating data; the design question is just how near, how far, sync or async, and full copies versus erasure-coded shards. The takeaway: replicate sync within the rack and async across DCs and geographies, and protect each node with RAID / erasure coding — redundancy at every distance is what buys eleven nines." width="1000">

- <span style="color:#ff8bd2"><strong>Synchronous, in-rack.</strong></span> Before a write is acknowledged, a second copy is made on a nearby node. The write isn't "done" until at least one redundant copy is safe — durability you can promise on the write path.
- <span style="color:#93c5fd"><strong>Asynchronous, across data centers and geographies.</strong></span> Copies fan out to other availability zones and distant regions in the background, so an object survives a whole DC — or a whole region — going dark. (Real S3 spreads every object across [at least three availability zones](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DataDurability.html).)
- <span style="color:#ffff99"><strong>Within a single node:</strong></span> RAID, and in real S3 <span style="color:#ffff99"><strong>erasure coding</strong></span> (Reed-Solomon) — split each object into data shards plus parity shards so it can be rebuilt even if several shards die, at a fraction of the space cost of full copies.

The whole game is: replicate near for a fast durable ack, replicate far to survive disasters, and shard within a node so no single disk failure even registers. Redundancy at every distance is what buys the eleven nines.

> **Memory hook:** *durability = duplication. Sync copy in-rack for the ack, async copies across DCs and geographies for disaster survival, RAID/erasure-coding within a node. ≥3 AZs per object.*

---

## Section 13 — Data Integrity: Never *Return* a Wrong Byte

**Question: durability keeps the bytes *present*. But what if a byte silently flips on disk — bit rot — or gets mangled crossing the network? You'd faithfully store and serve corruption. How do you guarantee the bytes that come out are the exact bytes that went in?**

Losing data is loud and obvious; <span style="color:#ff8a8a"><strong>silent corruption</strong></span> is worse, because nothing alarms. The rule is absolute: **never store and never return corrupted data.** The tool is the same one from the [KV engine](19-storage-engine-fast-kv-db.md) — the <span style="color:#ffff99"><strong>checksum</strong></span> — but applied <span style="color:#ffff99"><strong>end to end</strong></span>, at every hop.

<img src="../assets/s3/data-integrity.svg" alt="End-to-end data integrity via checksums at every hop. Top, the write path (pink): a client computes a checksum over the object before upload; at every component the data passes through — the API server, the partition server, and the storage node — the checksum is recomputed and compared to the one received, and only if they match does the data proceed; the final checksum is stored alongside the object on disk. If any comparison fails, the write is rejected rather than persisting corruption. Bottom, the read path (green): on the way back out, each hop re-verifies the stored checksum against the bytes it reads, so corruption introduced by bit rot on disk or by a bad network link is caught before the bytes ever reach the client. A separate panel, multi-part objects: a large object stored as multiple chunks each carries its own checksum, and when the chunks are combined the system computes a combined/rolled-up checksum over the whole object so the reassembled result is verified as a unit (this is how S3's multipart ETags work). A background-scrubbing note: storage nodes also continuously re-read objects in the background and compare against stored checksums to detect and repair bit rot proactively, before a client ever asks. A red caution that a single flipped bit turns one valid word into another (bat versus cat) with no error unless a checksum catches it. The takeaway: validate checksums at every component on the way in and on the way out, checksum each chunk and the combined whole, and scrub continuously — so a corrupted byte is always detected, never served." width="1000">

The discipline is to <span style="color:#ffff99"><strong>validate the checksum at every component</strong></span>, both directions. On the way in, the client computes a checksum; the API server, the partition server, and the storage node each recompute and compare before passing the bytes along, and the checksum is stored beside the object. On the way out, every hop re-verifies against the stored checksum, so a bit that rotted on disk or got mangled on the wire is <span style="color:#ff8a8a"><strong>caught before it reaches the client</strong></span>. A single flipped bit can turn `bat` into `cat` with no error at all — only a checksum notices.

Two refinements complete it. For <span style="color:#ffff99"><strong>multi-part objects</strong></span>, each chunk carries its own checksum and the combined object gets a <span style="color:#ffff99"><strong>rolled-up checksum</strong></span> over the whole (this is what S3's multipart ETags are). And storage nodes <span style="color:#93c5fd"><strong>scrub continuously</strong></span> in the background — re-reading objects and comparing against stored checksums to find and repair bit rot *before* anyone asks for the data.

> **Memory hook:** *checksum at every hop, both directions, plus a combined checksum across chunks and continuous background scrubbing — so corruption is always detected and never served. Durability keeps bytes present; integrity keeps them correct.*

---

## Where this leaves us: the complete S3

We started with a hard disk plugged into a laptop and grew it, one named bottleneck at a time, into a planetary object store. Every component earned its place by solving a specific problem the previous step created. Here is the whole machine in one map.

<img src="../assets/s3/final-map.svg" alt="The complete S3 architecture in one map, four planes shown together. Plane one, the request / data path (green for reads, pink for writes): a user hits a Load Balancer, which spreads requests across a stateless fleet of S3 API servers. An API server resolves the object key by reading the Partition Map Table, learns the owning partition server, and forwards the request to it. Plane two, the partition / serving layer: a row of Partition Servers, each the single logical owner of one or more range partitions; the server handling the request reads or writes the object's bytes on the storage layer. Plane three, the storage layer (yellow): racks of commodity HDD nodes running log-structured, append-only storage; writes go to the active HEAD disk and roll forward as disks fill at ~70%, reached over a FUSE mount, with per-node storage monitors running compaction; every object is replicated sync in-rack and async across at least three availability zones and across geographies, and protected within a node by erasure coding, with end-to-end checksums validated at every hop and scrubbed in the background. Plane four, the control plane (blue): a replicated, leader-elected (Raft/Paxos) Partition Manager owns the Partition Map Table, health-checks the partition servers, splits hot partitions, and on a partition-server failure reassigns that server's logical partitions to healthy servers with no data movement. A legend ties the colors to the planes: green read path, pink write path, yellow storage and durability, blue control and async plane, red the failure modes each plane defends against (rebalancing storms, hot partitions, SPOF, corruption). The single sentence under the map: S3 is a flat key-value store of paths-to-blobs, made cheap by log-structured HDDs, infinite by range partitions over racks, indestructible by replication and checksums, and self-healing by a control plane that owns partitions logically so failover is just editing a table." width="1280">

The four machines, and the one idea each is built around:

| Plane | What it is | The one idea |
| --- | --- | --- |
| <span style="color:#8aff8a"><strong>API tier</strong></span> | Stateless servers behind a load balancer | Serving scales with traffic, independently of storage |
| <span style="color:#93c5fd"><strong>Control plane</strong></span> | Partition Manager (replicated, leader-elected) + Partition Map Table | Own partitions *logically* so failover is a table edit, not a data move |
| <span style="color:#ff8bd2"><strong>Partition servers</strong></span> | Single logical owner per range partition | One writer per partition; compute isolated from bytes |
| <span style="color:#ffff99"><strong>Storage layer</strong></span> | Racks of log-structured HDDs, replicated + checksummed | Cheap and infinite, made fast by appending and safe by redundancy |

Read the colors top to bottom and they narrate the design: a <span style="color:#8aff8a"><strong>green serve path</strong></span> over a stateless tier, a <span style="color:#93c5fd"><strong>blue control plane</strong></span> that thinks about partitions, <span style="color:#ff8bd2"><strong>pink ownership</strong></span> with a single writer, and a <span style="color:#ffff99"><strong>yellow storage floor</strong></span> that is cheap, infinite, and indestructible. That is S3.

> **Memory hook:** *S3 = flat keyspace of paths→blobs, made cheap by log-structured HDDs, infinite by range partitions over racks, indestructible by replication + checksums, and self-healing by a control plane that owns partitions logically so failover is just editing a table.*

---

## Further reading

The design here is derived from first principles, but every piece has deep prior art. To go further:

- **[Windows Azure Storage: A Highly Available Cloud Storage Service with Strong Consistency](https://www.cs.purdue.edu/homes/csjgwang/cloud/WAS.pdf)** — the canonical paper on a production object store's partition layer, partition manager, and stream layer. The closest published mirror of the architecture we built.
- **[Building a Database on S3](https://www.csd.uoc.gr/~hy460/pdf/p251-brantner.pdf)** — what it means to treat S3 itself as the storage substrate for a database.
- **[Scuba (Facebook)](https://research.facebook.com/publications/scuba-diving-into-data-at-facebook/)** — in-memory, sharded, log-structured analytics at scale; good for the partitioning and ingestion mindset.
- **[Using Lightweight Formal Methods to Validate a Key-Value Storage Node in Amazon S3 (ShardStore)](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3)** — S3's real log-structured storage node, and how it's verified.
- **[Building and operating a pretty big storage system (S3), by Andy Warfield](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html)** — the best public narrative on how S3 actually runs.
- **Database Internals by Alex Petrov** — the book to read for the storage-engine and distributed-systems fundamentals underneath all of this.
