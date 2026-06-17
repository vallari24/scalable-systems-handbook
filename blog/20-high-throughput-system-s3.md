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

So how do you *browse* a folder, if folders don't exist? You don't open a directory — there's nothing to open. You ask the API to **list every key that starts with a prefix**:

- <span style="color:#8aff8a"><strong>`LIST(prefix = "photos/2024/")`</strong></span> returns every object key in that slice of the keyspace.
- The trailing `/` is just a **delimiter convention** that lets the API roll keys up into one "folder level."
- The folder tree you see in the console is **reconstructed from key prefixes on the fly** — it was never stored as a tree.

This is why keeping keys **sorted** will matter so much later: a prefix is a *contiguous slice* of a sorted keyspace, so "browse this directory" becomes a cheap <span style="color:#8aff8a"><strong>prefix scan</strong></span> instead of a fan-out across every disk.

> **Memory hook:** *the API is PUT/GET/DELETE on one path; browsing a "folder" is a prefix scan, not a tree walk; the three hard problems are storage, routing, and hot partitions; and the unlock is that directories are fake — it's all one flat keyspace of paths.*

### Buckets and "folders": both are naming conventions, not places

We just said directories are fake. The same truth holds one level up, at the <span style="color:#ffff99"><strong>bucket</strong></span> — the **top-level namespace** for your keys, the first segment of every path. When you store:

`s3://my-photos/2024/summer/cat.jpg`

the real key S3 keeps is the flat string `2024/summer/cat.jpg`, and `my-photos` is the **bucket** that scopes it. There is no `my-photos` folder, and no `2024/` or `summer/` folder, anywhere on disk — it's one flat key, and the bucket is just the prefix that says "this key belongs to this namespace." What the bucket actually buys you:

- **Uniqueness scope** — keys must be unique *within* a bucket, so your `cat.jpg` never collides with mine. (Bucket names themselves are globally unique, which is what makes the URL routable.)
- **A unit of config** — permissions, region, billing, versioning, and lifecycle rules all attach at the bucket level.
- **A cheap prefix scan** — "list my bucket" is just `LIST(prefix="…")` over a contiguous slice of the sorted keyspace, reconstructed on the fly — never a folder you open.

So **a directory is a logical, virtual entity — and a bucket is too.** Both are labels that *group and scope* keys, not places that *hold* them.

### How a real filesystem does it: inodes

If folders are fake in S3, how does a *real* filesystem (ext4 and friends) find your file? Through an <span style="color:#ffff99"><strong>inode</strong></span> — a small record holding everything about a file **except its name**: size, permissions, timestamps, and crucially the **pointers to the disk blocks** that hold the actual bytes. A **directory** is then just a tiny table mapping **names → inode numbers**.

So "open `cat.jpg`" really means: look up `cat.jpg` in the directory to get an inode number → read that inode to find the data blocks → read the blocks. The name and the data are **decoupled**, which is why you can rename a file, or hard-link two names to one inode, without moving a single byte. S3's design rhymes with this: the [PMT plus the partition server's index](#section-8--who-knows-where-things-live-the-partition-map-table-and-partition-manager) play the inode's role — mapping a key to *where the bytes physically live* — while the path is just a name. S3 took the filesystem's "names are separate from locations" trick and stretched it across a planet.

### The vocabulary, in one place

- <span style="color:#ffff99"><strong>Object</strong></span> — one file: the *value*. Opaque bytes S3 never looks inside.
- <span style="color:#8aff8a"><strong>Key</strong></span> — the full path string that *names* an object (`bucket/images/logo.jpg`). The thing you hash or range-partition on.
- <span style="color:#ffff99"><strong>Bucket</strong></span> — the top-level namespace/prefix that scopes keys (above).
- <span style="color:#ff8bd2"><strong>Partition</strong></span> — a contiguous *range* of the sorted keyspace (`[a,e]`), owned by one partition server. Emphasis on **how the data is divided**.
- <span style="color:#ff8bd2"><strong>Shard</strong></span> — the same slice seen as the **unit you place and move** between machines. *Partition = how it's split; shard = what you move — same thing, two angles.*
- <span style="color:#93c5fd"><strong>Node</strong></span> — one **physical machine** (a box of disks + CPU) in the fleet. Partitions/shards are the *logical* slices; nodes are the *physical* hardware they sit on. The PMT maps a partition to the node currently holding it (`[a,b] → Node 1`), and "move a shard" / "a node died" are about this physical layer. *Logical (partition) vs. physical (node) is the split that makes failover a table edit.*
- <span style="color:#93c5fd"><strong>Replica</strong></span> — a *copy* of a partition/shard for durability, **not** a different slice. Sharding spreads load; replication survives failure.
- <span style="color:#8aff8a"><strong>Tenant</strong></span> — one customer of a shared (multi-tenant) service. S3 serves millions of tenants off the same fleet.
- <span style="color:#ff8a8a"><strong>Tenant isolation</strong></span> — keeping one tenant's workload from hurting another's. A noisy tenant's traffic spike should not degrade everyone sharing the hardware. The measure of it is <span style="color:#ff8a8a"><strong>blast radius</strong></span>: when one tenant misbehaves, how many others feel it? This is *the* requirement that kills hash routing (which scatters every tenant across all disks, so one spike hits everyone) and picks **range partitioning** (a tenant's keys share a prefix → fall in one contiguous range → can be put on their own partition, so the blast radius is just that one tenant). It's also why hot partitions get **split** and abusive accounts get **throttled** ([Sections 5–6](#section-5--attempt-3-range-based-partitioning-the-one-s3-uses)).
- <span style="color:#ff8a8a"><strong>Hot partition</strong></span> — one partition taking far more traffic than it can serve, while its neighbors idle: a celebrity bucket the whole internet pulls from, or a single hammered key. The skew, not the average, is the problem — load is uneven across the keyspace, so one node melts while the fleet sits cool. The structural cure is to **split** the hot range into two disjoint ranges on two nodes (trivial because ranges are contiguous). See [Section 6](#section-6--hot-partitions-split-and-throttle).
- <span style="color:#93c5fd"><strong>Throttle</strong></span> — capping a tenant's request rate and rejecting the excess with a "slow down" signal (`503 SlowDown`) instead of letting it take a node down. It's the *immediate* shield while a split is still in flight — the bandage, not the cure. It lives on the **partition server**, not the API fleet: only the range's owner sees all of one tenant's traffic, so only it can count per-tenant and rate-limit precisely. See [Section 6](#section-6--hot-partitions-split-and-throttle).

### How it all nests: tenant → buckets → partitions

These three terms trip people up because they sit on *different axes*. One concrete example untangles them. Say **Lyft** has one S3 account — that's one <span style="color:#8aff8a"><strong>tenant</strong></span>. Under it, Lyft creates many <span style="color:#ffff99"><strong>buckets</strong></span>, one per dataset: `driver-photo/`, `rider-photo/`, `driver-pay/`, `rider-pay/`, `ride-history/`, `ml-model/`. And each bucket, as it fills up or gets hot, is sliced into many <span style="color:#ff8bd2"><strong>partitions</strong></span> — contiguous key ranges served on different machines.

The four rules that actually matter:

- **One tenant → many buckets.** Lyft owns all six.
- **One bucket → exactly one owner.** `ride-history/` belongs to Lyft and only Lyft; a bucket is *never* shared across tenants.
- **One bucket → many partitions.** `ride-history/` might be `partition 1 [a–h]`, `partition 2 [i–p]`, `partition 3 [q–z]` — and it splits into more as it grows or heats.
- **One partition → never two buckets.** A partition is a range *inside one bucket*; the bucket boundary is always a split point, so `driver-pay/` and `rider-pay/` can never share a partition. That's exactly what keeps one dataset's load from leaking into another's.

<img src="../assets/s3/tenant-bucket-partition.svg" alt="How tenant, bucket, and partition nest, using Lyft as the example. Top: a single tenant box, 'Lyft account (tenant = one AWS account)', fans out by grey arrows to six bucket boxes in a row: driver-photo/, rider-photo/, driver-pay/, rider-pay/, ride-history/, and ml-model/, each labelled 'bucket'. Caption: one tenant maps to many buckets, and each bucket has exactly one owner, never shared across tenants. Middle, 'Zoom into one bucket maps to many partitions': the ride-history/ bucket box on the left points by a yellow arrow to three partition boxes — partition 1 [a–h], partition 2 [i–p], partition 3 [q–z] — with a note that it splits into more as it grows or heats. Caption: one bucket maps to many partitions, each partition owning a contiguous key range inside that one bucket. Bottom, two side-by-side panels stating the rule. Left, green check: 'A partition lives inside ONE bucket' — the ride-history/ bucket drawn as a container holding partition 1, 2, and 3. Right, red cross: 'A partition can NOT span two buckets' — two buckets, driver-pay/ and rider-pay/, with a single dashed partition box drawn across both and struck through with a big red X, captioned 'a bucket boundary is always a split point'. Takeaway bar: tenant is who owns it (Lyft); bucket is the named dataset with one owner; partition is a key-range slice of one bucket. So one tenant to many buckets, one bucket to many partitions, one partition to exactly one bucket." width="1180">

> **Memory hook:** *tenant owns many buckets; a bucket has one owner and splits into many partitions; a partition lives inside exactly one bucket. Tenant = who, bucket = the named dataset, partition = a key-range slice of it.*

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

**The clean way to hold all three strategies in your head — match the strategy to what you're routing:**

- <span style="color:#93c5fd"><strong>Hash is for stateless work.</strong></span> Load balancers and API servers hash to spread requests *uniformly* across interchangeable machines — there you *want* to give up control and just smear load evenly.
- <span style="color:#ffff99"><strong>Range is for stateful placement.</strong></span> When you must dictate *where data physically lives* — drop a new HDD exactly where you want it, keep a tenant together, split a range cleanly — a hash function simply can't give you that control. Ranges can.

And the tenant-isolation win is really about <span style="color:#ff8a8a"><strong>blast radius</strong></span>: with hashing, a hot bucket shares disks with many other tenants, so one company's spike hurts all of them; with ranges, a hot bucket sits in its own range, so the blast radius is just that one tenant.

> **Memory hook:** *range partitioning keeps keys sorted and gives contiguous ranges to nodes — buying locality, tenant isolation, and control, at the price of ranges that can grow lopsided and hot. Hash for stateless load-spreading; range for stateful placement you need to control.*

---

## Section 6 — Hot Partitions: Split, and Throttle

**Question: a celebrity uploads to one bucket and the whole internet pulls from it. That bucket's range is now a furnace while its neighbors idle. With contiguous ranges, what's the natural move to cool it down — and what do you do while the cooling happens?**

Here range partitioning pays off beautifully, because the fix is almost trivial: **when a partition gets hot, split it in two.** Take the hot range `[l,r]` and cut it into two <span style="color:#ffff99"><strong>mutually exclusive</strong></span> halves, `[l,m]` and `[n,r]`, and move one half to a fresh node. Two nodes now share what one was drowning in. Because ranges are contiguous and ordered, the split is a clean cut with no key left ambiguous — something that's painful with a hash.

<img src="../assets/s3/hot-partition-split.svg" alt="Dealing with hot partitions in a range-partitioned store, two complementary tactics. Main diagram, splitting: a single Hot Node owning range [l,r] (drawn as a partition crammed with object dots, outlined in red to show overload) is split into two mutually exclusive partitions — Node 1 owning [l,m] and Node 2 owning [n,r] — with an arrow from the hot node branching down to the two new nodes. The split point is chosen so the two ranges are disjoint and together cover the original, and one half is moved to a new physical node, halving the load. A note: this is simple precisely because the partitioning is range-based — a contiguous range cuts cleanly in two, whereas a hash scheme cannot. Second tactic, throttling (blue): while a split is in progress, or to protect against abuse, cap the number of requests per account — if requests go beyond a certain per-account limit, the extra requests are throttled (S3 returns a 503 SlowDown). Real-world note tied to this: S3 sustains roughly 3,500 writes and 5,500 reads per second per partitioned prefix, with no limit on the number of prefixes, and it auto-splits hot partitions behind the scenes (taking 30 to 60 minutes), returning 503 SlowDown until the new partitioning is ready. The takeaway: splitting cools a hot range structurally and permanently; throttling protects the node in the meantime and caps any single tenant." width="1000">

The structural fix is the <span style="color:#93c5fd"><strong>split</strong></span>; it permanently spreads the load. But a split takes time, so we need an immediate shield: <span style="color:#93c5fd"><strong>throttling</strong></span>. Cap requests per account, and when a tenant exceeds its limit, reject the excess with a "slow down" signal rather than letting it take the node down with it.

And the throttle belongs on the <span style="color:#93c5fd"><strong>partition server</strong></span>, not the API server — this is the whole payoff of range partitioning showing up again. The stateless API fleet is the wrong place to rate-limit: any client's requests can land on any API server, so no single one sees a tenant's full request rate — the traffic for one hot bucket is smeared across the whole fleet, and no node has the local count needed to decide "this account is over its limit." The partition server is the opposite: it's the single owner of a contiguous range, so *every* request for that bucket's prefix funnels through it. It already knows exactly which partition and account each request belongs to, which means it's the one place that can keep an accurate per-tenant counter and throttle precisely the tenant that's hot — without touching anyone else. Ownership of a range is also ownership of that range's rate limit.

This is precisely how production S3 works. Each partitioned prefix sustains about [3,500 writes and 5,500 reads per second](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html), there's no limit on the number of prefixes, and S3 <span style="color:#93c5fd"><strong>auto-splits hot partitions</strong></span> behind the scenes — a process that takes 30–60 minutes, during which you may see `503 SlowDown` while the new partitioning settles. Splitting is the permanent cure; throttling is the bandage that holds until the cure takes effect.

> **Memory hook:** *hot range? split it into two disjoint ranges on two nodes — trivial because ranges are contiguous — and throttle per account (503 SlowDown) while the split is in flight. Throttle on the partition server, not the API fleet: only the range's owner sees all of a tenant's traffic, so only it can count per-tenant and rate-limit precisely.*

---

## Section 7 — A Quick Detour: Many Logical Shards on Few Machines

**Question: splitting a live partition means physically moving data while traffic flows over it — fiddly and slow. Is there a way to make rebalancing almost free, so moving load between machines doesn't mean re-cutting ranges at all?**

There's a classic trick worth a short detour, because it shapes the design that follows. Instead of a few big partitions that you split under pressure, create a **large number of small logical shards up front** and pack many of them onto each physical machine.

<img src="../assets/s3/logical-shards.svg" alt="A quick detour: solving hot nodes with many logical shards on few physical machines. Three physical nodes are drawn as large boxes (Node 1, Node 2, Node 3). Inside them sit small rectangles, each one a 'logical shard' (a logical partition); the shards are distributed unevenly to make the point — Node 1 holds one shard, Node 2 holds three, Node 3 holds three. An arrow labels one small rectangle as 'logical shard'. The idea: you over-provision logical shards far beyond the number of machines, so each machine just hosts a handful of them. To rebalance or to cool a hot machine, you move an entire logical shard from one node to another — you never re-cut a range or move individual keys. Real-world notes: Elasticsearch uses this strategy (its shards, visible via the HEAD plugin), and Instagram did this with their main posts table. Key reason, highlighted: moving one self-contained, mutually-exclusive subset of data across data nodes is far simpler and more efficient than splitting live ranges — it makes load balancing a matter of relocating whole shards. The takeaway: pre-create many logical shards and treat the shard, not the key, as the unit of movement, so rebalancing is just 'pick up this shard, put it on that node.'" width="1000">

Now the <span style="color:#ffff99"><strong>unit of movement is a whole shard</strong></span>, not a key and not a range you have to re-cut. To cool a hot machine, you pick up one of its logical shards and drop it on a quieter machine. That's it. Moving a <span style="color:#ffff99"><strong>self-contained, mutually-exclusive subset</strong></span> of data is dramatically simpler than splitting a live range.

This isn't theoretical: <span style="color:#93c5fd"><strong>Elasticsearch</strong></span> works exactly this way (its shards, visible through the HEAD plugin), and <span style="color:#93c5fd"><strong>Instagram</strong></span> famously pre-sharded their main posts table into thousands of logical shards over a handful of Postgres machines. The lesson to carry forward: **make the logical partition the unit of ownership and movement.** That idea is about to become the backbone of S3's control plane.

### The key idea: two maps, not one

Why doesn't moving a shard force you to rewrite every key inside it? Because routing is split into **two levels of indirection**, and only one of them ever changes:

- <span style="color:#ffff99"><strong>key → shard</strong></span> is a **fixed** function — `shard_id = hash(key) % N` with `N` large and **frozen forever** (you pick thousands of shards up front). This map *never* changes.
- <span style="color:#93c5fd"><strong>shard → node</strong></span> is a **small lookup table** — "which machine hosts shard 4,217 right now?" This is the *only* thing rebalancing touches.

So a key's shard is computed the same way before and after a move; you relocate the *container* and edit one row of the second map. (Contrast plain `hash(key) % num_machines`, where the divisor changes and every key's home moves — the disaster this avoids.)

### End-to-end: a live shard move (data-on-node model, e.g. Elasticsearch)

The hard case is when the shard's bytes physically live on the node you're moving them *off* of, while traffic keeps flowing. The move is a careful copy-then-cutover:

1. **Decide.** The coordinator notices a hot node and picks shard `S` to move from node `A` to a quieter node `B`.
2. **Copy in background.** `B` pulls a snapshot of `S` from `A` — and `A` <span style="color:#8aff8a"><strong>keeps serving reads and writes</strong></span> the whole time.
3. **Log the delta.** Writes that land on `S` during the copy are recorded in a <span style="color:#ff8a8a"><strong>changelog</strong></span>, because `B`'s snapshot is now stale.
4. **Catch up.** `B` replays the delta until it's nearly current with `A`.
5. **Cutover.** `A` briefly <span style="color:#ffd27f"><strong>freezes `S`</strong></span> (queues writes), ships the final delta, and hands off ownership — a millisecond-to-second window, the only blocking moment.
6. **Flip the map.** The coordinator updates <span style="color:#8aff8a"><strong>`S → B`</strong></span> and `A` drops its copy. **No key was ever rewritten.**

### How does a request find out the shard isn't there anymore?

Routing tables are <span style="color:#ffff99"><strong>caches</strong></span>, so right after a move a router can still hold a stale `S → A` and send a request to the wrong node. The system doesn't push an update to every router synchronously — it lets stale routes **self-correct**:

- The old owner replies <span style="color:#ff8a8a"><strong>"I no longer own this shard"</strong></span> (HBase's `RegionMovedException`, MongoDB's *stale shard version*).
- The router <span style="color:#93c5fd"><strong>refreshes its map</strong></span> from the control plane (the source of truth) and **retries** against the new owner.

So nobody coordinates a fleet-wide cache invalidation; routers discover moves *lazily*, on the next miss.

<img src="../assets/s3/shard-move.svg" alt="Moving a logical shard live, in two panels. Panel one, a live shard move where data lives on the node (Elasticsearch model): a control plane / master owns the shard-to-node map at top. Node A on the left is the current owner of shard S, which is LIVE and serving reads and writes, and keeps a changelog of writes that arrive during the copy. Node B on the right is the quieter target, where shard S is filling up and becomes owner only at cutover. A blue dashed arrow shows step 2, copy snapshot in the background while A keeps serving; a pink dashed arrow shows step 4, replay the delta of writes made during the copy; a blue arrow from the control plane to Node B shows step 6, flip the map S to B. A six-step ribbon spells out the full sequence: 1 Decide — coordinator picks shard S to move A to B; 2 Copy — B copies a snapshot from A while A still serves; 3 Log delta — writes during the copy recorded as a changelog; 4 Catch up — B replays the delta until nearly current; 5 Cutover — A freezes S, ships the final delta, hands off in a milliseconds-to-seconds window; 6 Flip map — the map flips S to B and A drops its copy, and keys never change. Panel two, how a stale request finds out the shard moved: a Router holds a stale cached map S to A; Node A is the former owner, Node B is the new owner, and the control plane is the source of truth holding S to B. Step 1 (red), the router sends a request on the stale route to A; step 2 (red dashed), A redirects with 'not my shard / moved' (RegionMovedException, stale-version); step 3 (blue dashed), the router refreshes its map from the source of truth; step 4 (green), the router retries and B serves. A note: self-correcting — routers cache the map and learn of moves lazily via redirects, no synchronous push to every router needed. Takeaway: keys are pinned to shards forever; only the shard-to-node map moves; copy-then-cutover when data lives on the node, just flip ownership when storage is shared as in S3." width="1180">

In S3 this whole dance collapses. Because storage is **shared and network-attached**, steps 2–4 vanish — there are no bytes to copy. "Moving a shard" becomes just the cutover and the map flip: the [Partition Manager](#section-10--the-whole-control-plane-and-what-happens-when-a-server-dies) edits one PMT row and the new owner reads the *same* bytes off the *same* disks. That's the payoff the next sections are built to earn.

> **Memory hook:** *pre-create many small logical shards on few machines; rebalance by moving a whole shard, never by re-cutting ranges or moving keys one at a time. Two maps: key→shard is fixed, shard→node moves. Live move = copy in background, log+replay the delta, freeze-and-cutover, flip the map. Stale routers self-correct via a "moved" redirect. (Elasticsearch, Instagram.)*

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

**Now the scalability question: one HDD fills up — how do you add another *without moving data*?** Model a storage rack as a <span style="color:#ffff99"><strong>linked list of HDDs</strong></span>.

<img src="../assets/s3/storage-linked-list.svg" alt="Scaling the storage layer by modelling a rack as a linked list of HDDs. Top: a Partition Server appends writes (pink arrow, 'append to log head') into the active HDD. A horizontal linked list of disk nodes — HDD 1 -> HDD 2 -> HDD 3 -> active HDD — connected by 'next' pointers; HDD 1 to 3 are frozen and read-only (yellow), and the active HDD is the tail (pink) where writes land. A dashed blue 'new HDD' node with a dashed 'link' pointer shows a fresh disk being appended onto the tail. A callout, 'Dynamic scaling = appending to a linked list': when the active HDD hits about 70 percent, freeze it (read-only) and link a new HDD at the tail, then advance the active pointer — an O(1) append with no data moves and no rebalancing; writes are never blocked because there is always an active HDD at the tail (the log head). A 'Many racks' strip shows three racks, each its own little linked list ending in its own active (pink) HDD, with the note that the proxy routes each key to a rack by range and within a rack every write appends to that rack's active HDD, so you add capacity by adding HDDs or whole racks. A 'Read tradeoff' panel: a read follows the index to whichever HDD holds the bytes (frozen or active), so reads may touch older disks; writes stay O(1) at the head while reads pay a little more. Takeaway: a rack is a logical linked list of HDDs with an active tail, so scaling out is just linking on another disk and writes are always accepted." width="1000">

- The <span style="color:#ff8bd2"><strong>active HDD</strong></span> is the tail of the list — every write appends there (it's the log head).
- When it fills to ~70%, **freeze it** (read-only) and **link a new HDD onto the tail**, then advance the active pointer. That's an `O(1)` append — <span style="color:#8aff8a"><strong>no data moves, no rebalancing</strong></span>.
- So <span style="color:#ff8bd2"><strong>writes are always accepted</strong></span>: there is always an active HDD at the tail. Scaling capacity is literally "link on another disk" — or another whole rack, with the proxy routing each key to a rack by range.

This also explains why **reads are the slower path**. Log-structured storage is *write-optimized*: a key's latest value can live on any HDD in the list, and stale versions linger until compaction, so a read must consult the index and may touch an older disk. That's a deliberate trade — S3's workload is <span style="color:#ff8a8a"><strong>write-heavy and infrequently read</strong></span> (huge volumes ingested, much of it rarely accessed), so spending a little on reads to keep writes and ingestion cheap is exactly the right bet.

Zoom into one node and the write mechanics are exactly that linked-list rule: the partition server always appends to the <span style="color:#ff8bd2"><strong>active HDD (the HEAD)</strong></span>, a <span style="color:#93c5fd"><strong>storage monitor</strong></span> freezes it at ~70% and advances the HEAD, and background <span style="color:#93c5fd"><strong>merge and compaction</strong></span> defragment the frozen disks, reclaiming the space left by overwrites and deletes — the same compaction we built in the KV engine.

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

## Section 14 — A Request, End to End: A Rider's Photo

**Question: enough boxes — let's trace one real request all the way through. A rider opens the Lyft app and their profile photo needs to appear. Follow that one read from the phone in their hand to the exact byte on a spinning disk: who reads the key, where the bucket comes from, which database answers "which server?", and how we land on the right rack, the right HDD, the right file, at the right offset.**

The object we're fetching has the key `rider-photo/r-99281/profile.jpg`. Watch what each layer does to that string. *(The component names below are the ones we built; where real S3 uses a different name, it's noted in parentheses — the architecture is the same. Mapping drawn from Andy Warfield's [S3 talk](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html) and the [ShardStore paper](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3).)*

<img src="../assets/s3/request-flow.svg" alt="An architecture diagram of reading a rider's photo, drawn as component boxes with the Partition Map Table as a database cylinder, and ten numbered arrows tracing the request. Outside AWS, a top row: the Rider's phone (Lyft app) box, the Lyft backend API box with a Lyft users DB cylinder beneath it (r-99281 maps to the S3 key), and a CloudFront CDN box. Arrow 1: phone to Lyft API. Arrow 2: Lyft API down to the Lyft users DB to look up the key (Lyft returns a presigned URL). Arrow 3: phone to CloudFront. A large 'AMAZON S3' boundary box contains the rest. Arrow 4: CloudFront down into S3 to the Load Balancer box (Route 53 + ELB; the bucket is the hostname, rider-photo.s3... resolves to an IP). Arrow 5: Load Balancer down to the S3 API server box (a stateless front-end fleet, drawn as two stacked boxes, that authenticate with SigV4/IAM and parse the bucket off the key). Arrow 6, highlighted: a two-way link between the S3 API server and the Partition Map Table cylinder — the range lookup — whose answer is Partition Server 7; a note says the API server READS the PMT while the Partition Manager box (top right) is the only WRITER of the PMT and also sends a dashed healthcheck arrow down to the partition servers. Arrow 7: the S3 API server forwards down to Partition Server 7 (highlighted), one of three Partition Server boxes in a row; Partition Server 7 owns the range [r-90000, r-100000]. Arrow 8: Partition Server 7 down to the Storage box — a rack of log-structured HDD slots (ShardStore, erasure-coded across at least 3 AZs) — using its own index to reach node 12, file 0007.log, offset 4,182,016, length 51,204, then seek, read, and verify checksum. Arrow 9 is that storage read; Arrow 10 (green dashed) is the return path: bytes flow back UP the chain — storage to partition server to S3 API server — then out via CloudFront, which caches them, and the phone shows the photo. A step-by-step legend at the bottom lists all ten steps. Takeaway: the key is read once at the front door to find the bucket, looked up by range in the PMT to find the owning partition server, then by that server's own index to the exact node, file, and offset." width="1340">

### Stage 1 — Outside AWS: the app turns "a rider" into a key

The phone has no idea where the photo physically lives, and it shouldn't. All it knows is "show me rider `r-99281`." Turning that into actual bytes is **Lyft's** job, not S3's.

So the app calls **Lyft's own backend**, which looks the rider up in **Lyft's own database** (a Postgres or DynamoDB table — nothing to do with S3). That database row stores the photo's **S3 key**: `r-99281 → rider-photo/r-99281/profile.jpg`. This is the moment a friendly internal id becomes the flat string S3 actually understands.

Lyft *could* now download the photo and forward it, but that would funnel every image through Lyft's own servers. Instead it hands the phone a **presigned URL** — an ordinary S3 link with a temporary signature attached that says, in effect, *"whoever holds this may read this one object for the next few minutes."* The phone fetches it directly, and Lyft never has to expose its AWS credentials. That URL almost always points at a **CDN (CloudFront)** — a worldwide network of edge caches sitting close to users. If a nearby edge already has the photo, it's returned instantly and **S3 is never touched at all**. Everything below is the harder case: a **cache miss**, where the edge has to go fetch the object from S3.

### Stage 2 — The S3 front door: find a server, prove who you are

The very first thing that happens at S3 is pure addressing, and **the bucket is the address**. The URL's hostname is `rider-photo.s3.amazonaws.com`, and **DNS** (Amazon's Route 53) translates that name into a real IP like `52.219.40.12` — exactly the way `google.com` becomes an IP. Bucket names are globally unique precisely so this translation is never ambiguous. That IP belongs to a **load balancer**, whose only job is to forward the request to *any* one of thousands of interchangeable **front-end API servers**. Those servers are **stateless** — they hold no data themselves — so it genuinely doesn't matter which one you land on.

The server's first real task is **authentication**: proving the request is from someone allowed to make it. The request arrived carrying a **signature** (AWS's scheme is called SigV4) — a code computed from the request contents plus the caller's secret key. The server recomputes that code and checks it matches, which proves two things at once: the caller holds valid credentials, and nothing was tampered with in transit. It then checks the **permissions** attached to the bucket — *is this caller actually allowed to read from `rider-photo`?* No valid signature, or no permission, and the request dies here with a `403` — no data is ever touched.

Only after that does the server do the cheapest but most pivotal step in the whole system: it **reads the key and slices off the bucket** — everything before the first `/`, here `rider-photo`. There's no lookup and no database involved; the bucket name is sitting right at the front of the key string.

**So what is the bucket's role in this whole flow?** It does exactly three jobs, all of them *identity*, none of them *location*:

- **The address (Stage 2).** The bucket *is* the hostname — `rider-photo.s3.amazonaws.com` — so DNS resolves it to an IP and gets the request to the right service and region.
- **The auth boundary (Stage 2).** Permissions and ownership are looked up *by bucket name*: "is this caller allowed to read from `rider-photo`?" Wrong bucket or no permission → `403`, before any data lookup.
- **The keyspace scope (Stage 3).** The PMT really sorts on `(bucket, key)`, so the bucket is the **high-order prefix** of the sort order — the range lookup happens *inside* `rider-photo`'s slice, and the bucket boundary is a hard wall (which is why a partition never spans two buckets).

And what the bucket does **not** do: it never points at a server, a rack, an HDD, or a file. It narrows *which keyspace*; the **rest of the key** does the actual locating. To S3 the name `rider-photo` is opaque — it means "rider photos" only to Lyft's app, never to S3.

### Stage 3 — Inside S3: which server owns this key?

Now the front-end has to answer one question: *which machine is responsible for this key right now?* It answers by reading the **Partition Map Table (PMT)** — a small, replicated lookup database that every front-end shares. (This confirms the intuition from the control-plane diagram: yes, the S3 API server talks directly to the PMT. Real S3 calls this the *index subsystem*.)

The PMT does **not** keep a row per object — there are hundreds of trillions of objects, so that would be hopeless. Instead it stores **ranges**: "every key from *here* to *there* lives on that server." Because keys are kept in **sorted order**, finding the right range is fast: the server does a **binary search** — jump to the middle of the list, decide whether the key is above or below, throw away half, repeat. Even across millions of ranges that's a couple dozen comparisons, never a scan. Our key `rider-photo/r-99281/...` lands in the range `[r-90000, r-100000]`, which the PMT says is owned by **Partition Server 7**.

Two points are worth being precise about:

- The front-end only ever **reads** the PMT. The one component allowed to **write** it is the **Partition Manager** — the control-plane brain that decides when to split a hot range or reassign one after a crash. *Many readers, a single writer* is what keeps the map consistent.
- **Partition Server 7 owns the range but holds none of the actual photo bytes.** It's a process — just CPU and memory — that has been *assigned* responsibility for `[r-90000, r-100000]` by that PMT row. The bytes live on separate storage machines. This is exactly why a crash is cheap: if Server 7 dies, the Partition Manager simply writes a new PMT row handing its ranges to another server, which starts serving at once — nothing has to be copied, because nothing was ever stored *inside* Server 7.

The front-end forwards the request to Partition Server 7, and now a **second, finer lookup** happens — this time *inside* that server.

### Stage 4 — From the key to the exact bytes

Partition Server 7 must turn the full key into a precise physical location, and it does this with **its own index**. It's worth being concrete about what that index *is*: an **in-memory map living in the partition server's RAM**, pairing each key the server owns with *where that object's bytes sit on disk*. It is **not** another database call across the network — it's a local memory lookup, which is what makes this step fast. (It's the same trick as the [Bitcask key-value engine](19-storage-engine-fast-kv-db.md): hold a map of `key → file + position` in memory, keep the data itself in files on disk, and rebuild the map from disk when the server restarts.)

For our key, the index hands back something like: *node 12, file `0007.log`, offset 4,182,016, length 51,204.* Decoding that:

- **Why a file called `0007.log`?** Remember the storage is **log-structured** (Section 11): the server never overwrites data in place, it only ever **appends** to the end. To keep that orderly, writes are poured into big sequential files called **segments** — `0001.log`, `0002.log`, and so on. The server keeps appending to the *current* segment until it fills up, then opens the next one. Our object simply happened to be written into segment `0007.log`. (A "segment file" is just one of those append-only chunks of the log.)
- **Offset and length** then make the read trivial: the object's bytes begin at **byte number 4,182,016** inside that file and run for **51,204 bytes**. Reading is "jump to that position, read that many bytes" — one **seek**, one read, no searching. Note nothing *chose* this disk by a routing rule: the write went to whatever segment was open at the time, and the in-memory index **remembered** the spot. Reads follow that memory; writes always go to the current open segment.

The partition server reaches that storage machine as if its files were local, through a **FUSE mount** — a Linux feature that makes a remote disk look like an ordinary local folder, so the server can just `open`, `seek`, and `read`.

But there's one thing we glossed over: the photo **isn't stored as a single whole file on a single disk.** Keeping three full copies would be safe but wasteful, so S3 uses a cheaper scheme called **erasure coding**. The plain-language version: the object is split into some number of **data pieces**, and from those the system computes a few **extra "parity" pieces** (think of the parity as math-generated backup fragments, the same idea behind RAID). All the pieces — data and parity — are written to **different machines in different datacenters**; S3 spreads them across at least **three Availability Zones** (an Availability Zone is an independent datacenter in the same region, with its own power and network). The useful property: the *whole* object can be rebuilt from **any sufficient subset** of the pieces. So several disks — even an entire datacenter — can fail and the photo still reconstructs, at a fraction of the storage cost of full copies.

Reading, then, really means: fetch enough pieces from those storage nodes and **reassemble** the object. And before any of it is returned, each piece's **checksum** is verified. A checksum is a short fingerprint computed from the bytes; S3 stored it when the object was written, recomputes it on read, and compares. If a single bit silently flipped on disk over the years ("bit rot"), the fingerprints won't match — and S3 rebuilds that piece from the others rather than ever handing back a corrupted photo.

### Stage 5 — The return trip

The reassembled, verified bytes travel **back up the same chain** they came down: storage node → Partition Server 7 → the front-end API server → out to **CloudFront**, which now **caches** the photo at the edge (so the next rider in that region skips this entire journey) → and finally the phone, which paints the image. The rider saw none of this — just a face appearing on screen in a few hundred milliseconds.

### Who is hit, and what each one's job is

| Hop | Component | Its one job |
| --- | --- | --- |
| 1 | **Lyft API + Lyft DB** | Turn "rider r-99281" into an S3 key + a presigned URL |
| 2 | **CloudFront CDN** | Serve from edge cache; only forward to S3 on a miss |
| 3 | **Route 53 + ELB** | Resolve endpoint, spread to a free front-end |
| 4 | **Front-end API server** | Authenticate (SigV4/IAM); parse the **bucket** off the key |
| 5 | **PMT** *(index subsystem)* | Range-lookup: key → owning **partition server** |
| 6 | **Partition server** | Owns the key-range; its in-memory index turns the key → node·file·offset |
| 7 | **Storage node** | Seek to the offset in the right segment file, read the bytes |
| 8 | **Erasure coding + checksum** | Rebuild the object from its pieces across 3 datacenters; verify the fingerprints |

The shape to remember: **the key is read once at the door (→ bucket), routed by *range* through the PMT to the owning partition server, then resolved by that server's *own in-memory index* to the exact file and offset.** A range lookup to find the *server*; a direct index lookup to find the *bytes*.

> **Memory hook:** *phone → Lyft (gets the key from Lyft's own DB) → CDN → S3 front-end (check the signature, slice off the bucket) → PMT range-lookup (which server owns this key?) → partition server (owns the range, holds no bytes) → its in-memory index → segment file + offset → rebuild the object from its pieces spread across 3 datacenters, check the fingerprint → back up through the CDN to the phone.*

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

- **[Windows Azure Storage: A Highly Available Cloud Storage Service with Strong Consistency](https://www.sigops.org/s/conferences/sosp/2011/current/2011-Cascais/11-calder-online.pdf)** — the canonical paper on a production object store's partition layer, partition manager, and stream layer. The closest published mirror of the architecture we built.
- **[Building a Database on S3](https://disco.ethz.ch/courses/hs08/seminar/papers/donald-kossman1.pdf)** — what it means to treat S3 itself as the storage substrate for a database.
- **[Scuba (Facebook)](https://research.facebook.com/publications/scuba-diving-into-data-at-facebook/)** — in-memory, sharded, log-structured analytics at scale; good for the partitioning and ingestion mindset.
- **[Using Lightweight Formal Methods to Validate a Key-Value Storage Node in Amazon S3 (ShardStore)](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3)** — S3's real log-structured storage node, and how it's verified.
- **[Building and operating a pretty big storage system (S3), by Andy Warfield](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html)** — the best public narrative on how S3 actually runs.
- **Database Internals by Alex Petrov** — the book to read for the storage-engine and distributed-systems fundamentals underneath all of this.
