# Beating Bitcask: Building an LSM-Tree Storage Engine

This post picks up exactly where the [Bitcask key-value engine](19-storage-engine-fast-kv-db.md) left off and removes its one hard limit. Bitcask gave us O(1) reads, writes, and deletes on durable disk — but it paid for that speed with a rule that doesn't scale: **every key must fit in RAM.** Here we relax that rule. We move the in-memory part from "an index of every key" to "a small write buffer," push the keys back onto disk, and in doing so derive — one named problem at a time — the **LSM tree** (log-structured merge tree): the storage engine inside RocksDB, LevelDB, Cassandra, HBase, and BadgerDB. Along the way we build memtables, SSTables, compaction, Bloom filters, and a write-ahead log, and we're honest about what each one costs.

**Question: Bitcask kept a hash entry in RAM for *every key you've ever written*. That's the wall — past a few hundred million keys you simply run out of memory, no matter how cheap your disk is. Can we build a store with the same append-only, high-write-throughput shape, but where the number of keys is bounded by your *disk*, not your *RAM*? And while we're at it — can we make writes even faster than Bitcask's?** The honest path runs through one deceptively simple move: stop writing to disk on the hot path at all. Write to memory, flush to disk in batches, and pay back the complexity that move creates — sorted files, merging, a probabilistic filter, and a recovery log — until what's left is a real LSM engine.

This is the second half of a pair. Read the [Bitcask post](19-storage-engine-fast-kv-db.md) first if you haven't — this post assumes you know why high-write systems go log-structured, what a tombstone is, and why append-only beats update-in-place.

> **Memory hook:** *Bitcask's limit is "all keys fit in RAM." An LSM tree spends RAM on a small write buffer instead of a full index, so keys become disk-bound, not memory-bound — at the cost of slower reads, which the rest of the design claws back.*

---

## The brief: same shape as Bitcask, but lift the RAM ceiling

**Question: before changing anything — what *exactly* are we trying to keep from Bitcask, and what one thing are we trying to fix?**

Keep the good parts. We still want the append-only, sequential-write shape that makes writes scream on any disk. We still want a tiny API — `PUT(k, v)`, `GET(k)`, `DEL(k)` — touching one key at a time. We still want durability.

<img src="../assets/lsm-trees/bitcask-wall.svg" alt="The Bitcask wall and the two things we want to fix. Left: a recap of Bitcask — an in-RAM hash index mapping every key to a (file, offset) location, sitting above an append-only data file on disk. A red brace around the index is labelled 'one entry PER KEY, forever' and a red callout reads 'the wall: keys must fit in RAM — run out of memory past a few hundred million keys, no matter how big your disk is.' Right: two goals for the new design, drawn as two arrows. Goal 1 (yellow): make the number of keys bounded by DISK, not RAM — push the keys back onto disk and keep only a small buffer in memory. Goal 2 (pink): make writes even faster than Bitcask by not touching disk on the hot path at all — write to memory first, flush in batches. Bottom caption: we keep Bitcask's append-only, high-write-throughput shape; we change what lives in RAM." width="1000">

Fix the wall. Bitcask's index holds one entry for *every key that exists*, and that index lives in <span style="color:#ff8a8a"><strong>RAM</strong></span>. Values are on cheap disk, but the keys aren't — so the number of keys you can store is capped by how much memory you can buy. For a store meant to hold *billions* of small records, that's the ceiling we have to break.

So the design goal is a swap: instead of spending RAM on *an index of every key*, spend it on *a small buffer of recent writes*. The keys move back to disk. The number of keys is now bounded by disk, which is enormous and cheap. That single decision is the seed of the entire LSM tree — and it immediately raises a second, tempting idea: if we're putting a buffer in RAM anyway, why not write to it *first* and skip the disk on the hot path entirely?

> **Memory hook:** *swap "RAM holds every key" for "RAM holds a small write buffer" — keys go back to disk (bounded by disk, not memory), and writes get to land in memory first.*

---

## Section 1 — Write to RAM first: the memtable

**Question: Bitcask's write was already cheap — one sequential append to disk. But disk, even sequential, is slower than memory. What if a write didn't touch disk at all, and just landed in a buffer in RAM?**

That's the core move. Every `PUT(k, v)` writes into an in-memory buffer — the <span style="color:#ff8bd2"><strong>memtable</strong></span> — and returns immediately. No disk on the write path. Periodically, in the background, the whole buffer is <span style="color:#93c5fd"><strong>flushed</strong></span> to disk in one shot. We've split storage into two <span style="color:#ffff99"><strong>tiers</strong></span>: a fast volatile tier (RAM) that absorbs writes, and a slow durable tier (disk) that holds everything older.

<img src="../assets/lsm-trees/memtable-write.svg" alt="Writing to RAM first, with a tiered storage layout. Center flow, left to right: a user/client writes with a WRITE (sync) arrow into a RAM box labelled 'memtable (in-memory buffer)'; from the memtable a second arrow labelled 'WRITE (async, periodic)' flows into a DISK cylinder. The sync arrow into RAM is pink (the hot write path); the async arrow to disk is blue (background). Above: caption 'because we write directly to RAM, write throughput is even higher than Bitcask — excellent for high ingestion volumes.' Left side, a red callout: 'durability takes a hit — RAM is volatile, so a crash before the next flush loses everything still in the buffer; we fix this later with a write-ahead log.' Right side: example workloads that fit this shape, drawn as small labelled icons — location tracking, user watch-stats / clickstream, IoT metrics — all high-volume, write-heavy, recent-data-hot. Bottom caption: two tiers — a fast volatile RAM tier absorbs writes, a slow durable disk tier holds everything older." width="1000">

What does this buy? <span style="color:#ff8bd2"><strong>Even higher write throughput than Bitcask.</strong></span> A write is now a memory operation; the disk only sees occasional large batched flushes, which are themselves cheap sequential writes. This is the right shape for *high ingestion volumes* — workloads that fire a firehose of small writes where the most recent data is the hottest: <span style="color:#8aff8a"><strong>location tracking</strong></span> (a fleet of phones pinging coordinates), <span style="color:#8aff8a"><strong>clickstream / watch-stats</strong></span> (every user interaction), <span style="color:#8aff8a"><strong>IoT metrics</strong></span> (sensors reporting every second).

But name the cost honestly, because it's the whole reason Section 10 exists. The memtable is in <span style="color:#ff8a8a"><strong>volatile RAM</strong></span>. If the process crashes *before* the next flush, every write still sitting in the buffer is **gone**. Bitcask wrote to disk on every PUT, so it never had this exposure. We just traded durability for speed — a trade we'll have to buy back later with a write-ahead log. For now, hold the gap in your mind.

> **Memory hook:** *the memtable is an in-RAM write buffer — writes land there instantly (faster than Bitcask) and flush to disk in batches later. The price: a crash before flush loses the buffer, because RAM is volatile.*

---

## Section 2 — Reading across two tiers

**Question: a key's value might be sitting in the memtable (just written, not yet flushed) or down on disk (written earlier, already flushed). On a `GET(k)`, where do we look, and in what order?**

The data now lives in two places, so a read has to consult both — but the *order* is what makes it correct. The memtable holds the **most recent** writes, the ones that haven't been flushed yet. So if a key is in the memtable, that copy is by definition the newest. Read RAM first.

<img src="../assets/lsm-trees/read-two-tiers.svg" alt="The two-tier read path for GET(k). A GET(k) request enters at the left. Step 1 (green): look in the memtable (RAM) first; if the key is there, return its value immediately — because the memtable holds the most recent writes, an in-memory hit is guaranteed to be the latest value. A green arrow labelled 'hit → return (latest value)' exits. Step 2 (yellow): if the key is NOT in the memtable, go to disk and search the on-disk files; if found there, return the value. Step 3 (blue): if it's in neither tier, return not-found (404). The memtable (RAM) box is drawn on top, the disk files below it, with the read arrow flowing top-to-bottom: RAM, then disk, then not-found. A side note reads: 'if a key is in memory it has to be the most recent value — that's why we always check RAM before disk.' Bottom caption: tiered storage — a key may live in RAM, on disk, or in both; read the fast tier first." width="1000">

So the read path is three checks, in order:

1. <span style="color:#8aff8a"><strong>Look in the memtable (RAM).</strong></span> If the key is there, return it — it's the latest value, full stop.
2. <span style="color:#ffff99"><strong>Otherwise, go to disk</strong></span> and search the on-disk files. If found, return it.
3. <span style="color:#93c5fd"><strong>In neither?</strong></span> Return not-found.

The memory-first ordering is what makes "newest wins" fall out for free: a key that was just updated sits in the memtable, shadowing any older copy still on disk. We never have to reconcile — the first place we look is always the freshest. Reads are no longer Bitcask's single O(1) seek, though. Step 2 is vague — *"search the on-disk files"* hides a lot of work, and most of this post is about making that step fast. First, let's pin down how and when the memtable actually becomes those on-disk files.

> **Memory hook:** *read RAM then disk: the memtable holds the newest writes, so an in-memory hit is always the latest value — checking RAM first makes "newest wins" automatic.*

---

## Section 3 — When to flush: periodic flush and capacity planning

**Question: the memtable fills as writes pour in. Flush too rarely and it overflows RAM (and a crash loses more); flush too often and you waste disk bandwidth on tiny writes. When *should* the buffer flush — and how much RAM should we even give it?**

This is a real provisioning decision, not a constant to hardcode. Two simple triggers cover it, and you flush on whichever fires first: a <span style="color:#ffff99"><strong>size threshold</strong></span> (the buffer reaches, say, 64 MB) or a <span style="color:#ffff99"><strong>time threshold</strong></span> (every `t` minutes, even if not full, so data doesn't sit unflushed too long). Both are config, because the right values depend entirely on your workload.

<img src="../assets/lsm-trees/flush-provisioning.svg" alt="Periodic flush and capacity planning. Top: a horizontal timeline with evenly spaced flush ticks; one tick is annotated 'every t minutes, the in-memory buffer is flushed to disk at once.' A note: flush whenever EITHER trigger fires first — size threshold (buffer reaches its byte cap) OR time threshold (t minutes elapse). Middle, a boxed formula labelled 'capacity math': time-to-fill = RAM buffer size ÷ ingestion rate, where ingestion rate = write throughput × average payload size. Worked example: a 64 MB buffer at 8 MB/s of incoming data fills in 8 seconds, so you'd flush on the size trigger long before any minutes-based timer. The point: you size the RAM buffer and pick t from a benchmark of your real ingestion rate and your hardware's tolerance, not by guessing. Bottom, a 'watch for signals' panel: a load graph with a big spike, labelled with examples — a marketing campaign, Amazon Prime Day, a product launch — historical spikes you can predict and pre-provision RAM for. Right callout defines SLA: a Service Level Agreement is the promise you make on latency / durability / availability; the flush frequency is one knob you tune to keep that promise (flush more often = less data at risk on a crash = stronger durability SLA, but more disk I/O)." width="1000">

How big should the buffer be, and how often will it flush? Don't guess — compute it from a benchmark. The buffer fills in:

```
time-to-fill = RAM buffer size ÷ ingestion rate
ingestion rate = write throughput (writes/sec) × average payload size (bytes/write)
```

Measure your real <span style="color:#ff8bd2"><strong>write throughput</strong></span> and <span style="color:#ff8bd2"><strong>average payload size</strong></span>, and the math tells you both how much RAM to provision and how frequently flushes will fire. A 64 MB buffer taking 8 MB/s fills in 8 seconds — so you'd flush on size, not time, and you'd size RAM knowing each node turns over its buffer every few seconds.

Then **watch for signals.** Ingestion isn't constant. A marketing team runs a <span style="color:#93c5fd"><strong>campaign</strong></span> and your write rate spikes; <span style="color:#93c5fd"><strong>Prime Day</strong></span> or a product launch is a spike you can *see coming* from last year's data. Keep an eye on load and pre-provision RAM for the peaks you can predict.

And this is where an <span style="color:#ffff99"><strong>SLA</strong></span> enters. A *Service Level Agreement* is the promise you make to the people using your store — on latency, durability, availability. Flush frequency is one knob you turn to keep that promise: flush **more often** and less data is at risk in a crash (stronger durability), but you spend more disk I/O; flush **less often** and you save I/O but widen the window of data you can lose. There's no universal right answer — you tune the knob to the SLA you signed up for, and you expose it as config so different deployments can choose differently.

> **Memory hook:** *flush on size-OR-time, whichever trips first. Size the RAM buffer from a benchmark: time-to-fill = buffer ÷ ingestion rate. Flush frequency is an SLA knob — more often = less crash exposure but more I/O.*

---

## Section 4 — Where does a flush go? One new immutable file per flush

**Question: the buffer is full and we're flushing it to disk. Do we append it to one big growing file, or write a brand-new file each time? It sounds like a minor implementation detail — it isn't.**

Appending to one open log file is technically viable. Operating systems support append mode, so the file's current size does **not** make each append require a progressively longer seek. Filesystem limits exist, but avoiding one eventual giant file is not the main reason an LSM creates a new file for every flush.

<img src="../assets/lsm-trees/flush-target.svg" alt="Why an LSM writes a new immutable file for every flush instead of appending every flush to one shared data file. Appending is viable and does not become slower merely because the file is large. The problem is structure: one shared file mixes generations, couples indexing and cleanup to the whole file, and cannot be replaced in small independent units. A fresh SSTable gives every flush a bounded sorted key range, its own index and metadata, and an immutable unit that compaction can safely replace. Bottom: a row of immutable files named 001.sst through 005.sst, each representing one frozen memtable flush." width="1000">

So we choose the other option: **every flush writes a brand-new file.** Not because appending to a large file is inherently slow, but because each flush should become an independently <span style="color:#8aff8a"><strong>indexable, bounded sorted run</strong></span>. Once written, the file is <span style="color:#93c5fd"><strong>immutable</strong></span>: readers can use it without coordinating with writers, metadata can describe its key range, and compaction can replace a selected set of complete files atomically. This is the same active-vs-immutable insight from Bitcask, but now the immutable units are *whole flushed buffers*, not rotated log segments.

Each of these files is an <span style="color:#ffff99"><strong>SSTable</strong></span> (Sorted String Table). They pile up over time — `001.sst`, `002.sst`, `003.sst`, … — one per flush. Our data now genuinely lives in two places: the live <span style="color:#ff8bd2"><strong>memtable</strong></span> in RAM, and a growing stack of immutable <span style="color:#ffff99"><strong>SSTables</strong></span> on disk. The next question is what's *inside* one of those files, because that's what makes the on-disk read fast.

> **Memory hook:** *one new immutable file per flush, not because large-file append gets slower, but because each flush needs its own sorted, indexable, replaceable unit. Each file is an SSTable.*

---

## Section 5 — Inside an SSTable: sorted data plus a sparse index

**Question: an SSTable is the flushed contents of a memtable. To find one key inside it without scanning the whole file, what does it need to store — and why does it help to write the keys in *sorted* order?**

The name gives away the trick: **Sorted** String Table. When we flush the memtable, we write its entries out **sorted by key**. (A memtable is typically a balanced tree or skip list precisely so that producing a sorted dump is cheap.) Sorting unlocks two things: a compact index, and — crucially for later — the ability to merge two files in linear time.

<img src="../assets/lsm-trees/sstable-internals.svg" alt="The internal structure of one SSTable, with two components. Bottom-right, the DATA block: a horizontal strip of key-value records written in sorted key order — k1:v1, k2:v2, k3:v3 — each record at a known byte offset within the file. Bottom-left, the INDEX block: a small two-column table mapping key to offset — k1→o1, k2→o2, k3→o3 — with thin yellow arrows pointing from each index entry to the matching record's position in the data block. Caption above: an SSTable = a sorted DATA section + an INDEX section (key → byte offset). The index is what lets a lookup jump straight to a record instead of scanning. A note on the right explains the GET-inside-one-file path: 1, look the key up in this file's index to get its offset; 2, seek to that offset in the data block; 3, read the record — one index lookup plus one seek-and-read, just like Bitcask, but the index is per-file and lives with the file on disk, not one giant index for all keys in RAM. A second note: because records are sorted, the index can be SPARSE — store one entry every N records and binary-search to the right block — so the index is tiny even for a large file. Bottom caption: the keys are on disk now (in each file's index), not all in RAM — that's how we broke Bitcask's memory ceiling." width="1000">

An SSTable has two parts:

- The <span style="color:#ffff99"><strong>data block</strong></span> — the records themselves, `k1:v1, k2:v2, k3:v3, …`, laid out in sorted key order, each at a known byte offset.
- The <span style="color:#8aff8a"><strong>index block</strong></span> — a small map of `key → byte offset` that lets a read jump straight to a record instead of scanning.

Reading one key out of one SSTable is now Bitcask-shaped again: look the key up in *that file's* index to get an offset, seek, read. One lookup plus one seek-and-read. The vital difference from Bitcask: this index is **per-file and stored on disk with the file**, not one giant in-RAM index of every key in the system. That's the whole point — *the keys are back on disk.* RAM holds only the small live memtable. We broke the memory ceiling.

Sorting pays a second dividend: the index can be <span style="color:#8aff8a"><strong>sparse</strong></span>. Because records are in key order, we don't need an index entry for every key — store one every N records, binary-search to the nearest indexed key, then scan a tiny block. A handful of index entries can address a large file, so even the per-file index stays cheap. (Hold onto "sorted enables linear-time merge" — Section 7 cashes it in.)

> **Memory hook:** *an SSTable = sorted data + a per-file key→offset index, both on disk. Sorting lets the index be sparse (tiny) and — later — lets two files merge in O(n). The keys live on disk now, not in RAM.*

---

## Section 6 — GET across many SSTables, and the worst case

**Question: a key wasn't in the memtable, so we go to disk — where there are now dozens of SSTables. Which do we search, and in what order? And what's the *worst* thing a reader can be asked to do?**

The files have an age order: `005.sst` was flushed after `004.sst`, which came after `003.sst`. Since newer writes are in newer files, the **newest file most likely holds the latest copy** of any key. So we search disk **newest-first** and stop at the first hit.

<img src="../assets/lsm-trees/get-across-sstables.svg" alt="The full GET(k) flow across the memtable and many on-disk SSTables, plus the worst case. Top: the data layout — a memtable box in RAM, then a row of SSTables on disk labelled 001.sst (oldest) through 005.sst (newest), each drawn as a small file with its own index strip on top. The GET(k) flow, numbered: 1, look up k in the memtable (RAM) — if present, return the value. 2, if not, start from the NEWEST file on disk (005.sst) and check its index to see if k is in that file; if yes, seek-and-read and return the value (we stop at the first hit because the newest file has the freshest copy). 3, if not in that file, move to the next-older file (004, then 003, ...) and repeat. 4, if no file has the key, return not-found (404). A green 'return' arrow leaves whichever file first contains the key. A key efficiency note: we don't scan each file's data — we check the file's INDEX first to know whether k is even in that file. Bottom, a red 'worst case' panel: a key that does NOT exist forces us to check the index of EVERY file (001 through 005) before we can conclude not-found — k files on disk means k index lookups (and disk I/O) just to answer 'no.' This is the cost we attack next with compaction and Bloom filters." width="1000">

So the complete read is:

1. **Memtable** (RAM) — hit? Return.
2. **Newest SSTable** — check its index; if the key's there, seek-read, return.
3. **Next-older SSTable** — repeat, walking back through `004`, `003`, …
4. **No file has it** — return not-found.

We don't blindly scan each file's data — we consult each file's **index** first, so a file that doesn't contain the key costs an index check, not a full read. Still, look at the shape of the cost. A key written recently is found fast (it's in the memtable or a recent file). But a key written *long ago* — or worse, a key that **doesn't exist at all** — drives us through *every file on disk*, one index lookup each, before we can answer. With `k` SSTables, the <span style="color:#ff8a8a"><strong>worst case is `k` index probes (and disk I/O) just to say "no."</strong></span> Reads got expensive, and the cost grows with the number of files. The next two sections attack exactly this: first shrink `k` (compaction), then make the "does this file even have the key?" question nearly free (Bloom filters).

> **Memory hook:** *search memtable, then SSTables newest-first, stopping at the first hit. The killer is a missing or very old key: it probes every one of the k files before returning — reads cost O(number of files).*

---

## Section 7 — Merge and compaction: fewer files to search

**Question: every flush adds another SSTable, so `k` grows without bound — and we just saw reads cost O(k). Stale and tombstoned records pile up too. How do we collapse many files into few without ever blocking reads or writes?**

The fix is <span style="color:#ff8bd2"><strong>merge and compaction</strong></span>: a background job that reads several immutable SSTables and writes out a smaller set, keeping only the live data. This is where *sorted* pays off. Merging sorted files is a <span style="color:#8aff8a"><strong>merge-sort merge</strong></span> — walk all inputs with one pointer each, always emit the smallest key, and when the same key appears in several files, keep only the newest. It runs in <span style="color:#8aff8a"><strong>O(n)</strong></span>, linear in total records, because every input is already sorted. (Unsorted, this would be a far more expensive operation — another reason the "S" in SSTable matters.)

<img src="../assets/lsm-trees/merge-compaction.svg" alt="Merge and compaction collapsing many SSTables into fewer. Left: several immutable input SSTables on disk (each sorted by key) feeding into a merge step. The merge is drawn as a merge-sort: one read pointer per input file advancing together, always emitting the smallest key next; an arrow labelled 'O(n) merge — inputs are already sorted, so this is a linear merge-sort pass.' Two skip rules are applied during the merge: stale entries skipped (if a key has several versions across files, only the newest survives — older overwritten versions are dropped) and tombstoned entries skipped (a delete tombstone and the dead record it shadows are both dropped once compaction passes them, finally reclaiming the space). Right: the output — a single, smaller, dense, still-sorted SSTable that replaces all the inputs. A graph callout shows the number of files over time as a sawtooth: it climbs as flushes add files, then drops each time compaction runs, keeping k (the number of files a read must check) bounded. Note: compaction only touches immutable files, never the live memtable, so reads and writes never block. Bottom caption: fewer files = fewer index probes per read; compaction is how we keep read cost from growing forever." width="1000">

Compaction applies two skip rules as it merges, the same pair as Bitcask:

- <span style="color:#ff8a8a"><strong>Stale entries skipped.</strong></span> If a key has several versions across the input files, only the newest survives.
- <span style="color:#ff8a8a"><strong>Tombstoned entries skipped.</strong></span> A delete tombstone and the dead record it shadows are both dropped once compaction passes them — the moment a deleted key's space is finally reclaimed.

The output is one smaller, dense, still-sorted SSTable that replaces all its inputs. Plotted over time, the file count becomes a bounded <span style="color:#ff8bd2"><strong>sawtooth</strong></span> — it climbs as flushes add files, then drops each time compaction runs — so `k` never runs away, and read cost stops growing. And because compaction only ever touches <span style="color:#93c5fd"><strong>immutable</strong></span> files (never the live memtable), reads and writes never block on it.

Compaction shrinks `k`, which helps every read. But it doesn't fully solve the worst case from Section 6: even with a handful of compacted files, a **key that doesn't exist** still forces a probe into *every one of them* before we can answer "no." For that specific pain, we need a way to ask "could this file possibly contain key `k`?" without touching the file at all.

> **Memory hook:** *compaction merges sorted SSTables in O(n), dropping stale and tombstoned records, collapsing many files into few — the file count becomes a bounded sawtooth, so read cost stops growing. It never blocks, because it only touches immutable files.*

---

## Section 8 — The missing-key problem and the Bloom filter

**Question: a lookup for a key that isn't in a file still pays a disk index probe to find that out, once per file. We want to ask "is key `k` definitely not in this file?" and get an answer from RAM, instantly, without reading the file. What data structure does that — cheaply enough to keep one per file in memory?**

The naive answer is a <span style="color:#ff8a8a"><strong>set</strong></span> of every key in the file: lookups are O(1) and exact. But a set stores every key, which means loading all those keys back into RAM — the exact memory cost we just spent the whole post escaping. At billions of keys it's hopeless. We don't need an *exact* membership structure, though. We need one that's allowed to be a little wrong in a *safe* direction, in exchange for being tiny.

<img src="../assets/lsm-trees/bloom-filter.svg" alt="How a Bloom filter works, by example. Top: a bit array of 8 slots, indices 0 through 7, all starting at 0. To INSERT a key, hash it with one or more hash functions to bit positions and set those bits to 1. Worked inserts shown: 'Apple' hashes to position 3 (set bit 3 = 1); 'Banana' hashes to position 2 (set bit 2 = 1); 'Cat' hashes to position 3 (also). After inserts, bits 2 and 3 are 1, the rest 0. Two queries demonstrate the two possible answers. Query 'is dog present?': dog hashes to position 6; bit[6] = 0, so the answer is a DEFINITE NO — if any hashed bit is 0, the key was never inserted. Query 'is elephant present?': elephant hashes to position 2; bit[2] = 1, so the answer is MAYBE (possibly present) — the bit is set, but it might have been set by a different key (a collision / false positive). The two guarantees, boxed: returns NO → key is DEFINITELY not present (no false negatives); returns YES → key is POSSIBLY present (false positives allowed). Right panel, 'why it's space-efficient': a Bloom filter stores NO keys and NO values — only a bit array plus a few hash functions. A few bits per key (≈10 bits/key for a ~1% false-positive rate) instead of the full key, so it's orders of magnitude smaller than a set and fits in RAM. Bottom caption: a Bloom filter trades exactness for size — it can say 'maybe yes,' but when it says 'no' it is always right." width="1000">

A <span style="color:#ffff99"><strong>Bloom filter</strong></span> is that structure. It's a bit array plus a few hash functions, and it stores **no keys and no values at all**:

- **Insert** key `k`: hash it with each hash function to a few bit positions, and set those bits to `1`.
- **Query** key `k`: hash it the same way and check those bits. If **any** of them is `0`, the key was *never* inserted — a <span style="color:#8aff8a"><strong>definite NO</strong></span>. If they're **all** `1`, the key is *possibly* present — a <span style="color:#ff8bd2"><strong>MAYBE</strong></span>, because some other keys might have set those same bits (a collision).

That asymmetry is the whole magic, and it's in exactly the safe direction:

- <span style="color:#8aff8a"><strong>Returns NO → definitely not present</strong></span> (no false negatives, ever).
- <span style="color:#ff8bd2"><strong>Returns YES → possibly present</strong></span> (false positives allowed, at a tunable rate).

Why so small? Because it throws away the data and keeps only *evidence of presence*. No keys, no values — just bits. About <span style="color:#ffff99"><strong>10 bits per key</strong></span> gets you a ~1% false-positive rate, versus storing whole multi-byte keys in a set. That's the property that lets us keep **one Bloom filter per SSTable resident in RAM** without reintroducing Bitcask's memory wall. Use it where a cheap "definitely-absent" answer saves expensive work; don't use it where you need exact membership or need to *enumerate* what's inside (it can't list its keys, only vote on one).

> **Memory hook:** *a Bloom filter is a bit array + hashes that stores no keys — "NO" means definitely absent (never wrong), "YES" means maybe present (rare false positives). ~10 bits/key buys ~1% error, so it's tiny enough to keep one per file in RAM.*

---

## Section 9 — Bloom filters in front of the SSTables

**Question: we have a per-file Bloom filter that answers "is `k` possibly in this file?" from RAM. How does that change the read path, and specifically how does it kill the missing-key worst case?**

Put a Bloom filter in front of every SSTable's on-disk lookup. Before touching a file, ask its Bloom filter. If the filter says **NO**, skip the file entirely — no index probe, no seek, no disk I/O at all. Only when it says **MAYBE** do we go to disk and do the real index lookup.

<img src="../assets/lsm-trees/bloom-over-sstables.svg" alt="Bloom filters guarding each SSTable on the read path. A row of SSTables on disk — 001.sst through 005.sst — each with a small Bloom filter box sitting on top of it in RAM. A GET(k) for a missing key flows across them: at each file, it first asks that file's Bloom filter. For 005, 004, 003, 002 the filter returns NO (drawn in green) and the file is skipped with zero disk I/O — a green 'skip, no disk touch' label on each. The read never has to open these files. Two outcomes are contrasted on the right: if EVERY filter says NO, the key is definitely absent and we return not-found after only cheap in-RAM bit checks — the missing-key worst case from Section 6 (k disk probes) collapses to k in-memory bit-checks. If a filter says MAYBE (drawn in pink), only THEN do we pay the disk cost: go to that file's index, seek, and read — and most MAYBEs are true hits, with the occasional false positive costing one wasted lookup. Bottom caption: the Bloom filter turns 'probe every file on disk to prove a key is missing' into 'check a few bits in RAM per file' — the single biggest read win for absent and old keys." width="1000">

Now re-run the painful case — a `GET` for a key that doesn't exist. Before, we probed every file on disk to prove it. Now we ask each file's Bloom filter, get a fast in-RAM **NO** from each, skip them all, and return not-found having touched **zero disk**. The worst case collapsed from *`k` disk probes* to *`k` in-memory bit-checks*. The same win helps any old key that lives in only one file: the filters let us skip straight past the files that can't have it.

When a filter says **MAYBE**, only *then* do we pay for a disk index lookup — and most MAYBEs are genuine hits, with the occasional false positive costing one wasted lookup (which is why the false-positive rate is a tuning knob). That's the trade: a tiny, bounded amount of RAM and the occasional wasted probe, in exchange for deleting the dominant read cost. This is the standard read path in every production LSM engine.

> **Memory hook:** *ask each file's Bloom filter before touching disk — a NO skips the file with zero I/O. The missing-key worst case drops from k disk probes to k RAM bit-checks; only a MAYBE pays for a real lookup.*

---

## Section 10 — Durability: the write-ahead log

**Question: we've made writes fast and reads cheap — but the memtable is still in volatile RAM. Section 1 flagged it: a crash before the next flush loses every un-flushed write. How do we get those writes back after a reboot without giving up the in-memory speed?**

There's exactly one place a write can be made durable: disk. So before a `PUT`/`DEL` is applied to the memtable, we **append it to a log file on disk** — the <span style="color:#ffff99"><strong>write-ahead log</strong></span> (WAL). It records the raw operations in order: `PUT k1 v1`, `DEL k1`, `PUT k1 v2`, `PUT k2 v3`. This is the same idea as a relational database's redo log or MySQL's binlog — write the *intent* to durable storage first, apply it to the in-memory state second.

<img src="../assets/lsm-trees/wal.svg" alt="The write-ahead log protecting the in-memory memtable. Center: the write path with the WAL inserted first. A PUT/DEL operation arrives and goes two places in order: step 1 (yellow) append the operation to the WAL file on disk — an append-only log shown holding a sequence of ops: PUT k1 v1, DEL k1, PUT k1 v2, PUT k2 v3; step 2 (pink) apply it to the in-memtable. A label: 'write to the durable log FIRST, then the memtable — that's what write-ahead means.' Left, the crash-recovery story drawn as a cycle: on crash/reboot the memtable (RAM) is empty; we REPLAY the WAL from disk, re-applying each logged operation to rebuild the exact memtable that existed before the crash — zero data loss. Right, the truncation story: when the memtable is flushed to an SSTable, those writes are now safely on disk in the SSTable, so the WAL that covered them is no longer needed and is truncated to zero bytes — the WAL only ever holds operations not yet flushed, so it stays small. Bottom, the cost/trade panel: for 100% durability the WAL must be fsync'd to disk on EVERY write, which means a disk write per operation — that erases the 'writes never touch disk' speed advantage. So WAL durability is a CONFIG / SLA knob: fsync-every-write (zero loss, slower) ... batch/async fsync (tiny loss window, faster). Caption: the WAL buys durability back; how hard you fsync it is where you trade durability against throughput." width="1000">

The write becomes two steps, in order: **append to the WAL on disk, then apply to the memtable.** Now picture the crash. On reboot the memtable is empty, but the WAL is sitting on disk. We <span style="color:#8aff8a"><strong>replay it</strong></span> — re-apply every logged operation in order — and rebuild the *exact* memtable that existed the instant before the crash. Zero data loss.

The WAL doesn't grow forever, because it's only protecting un-flushed writes. The moment the memtable is flushed into an SSTable, those writes are durable *in the SSTable*, so the WAL that covered them is dead weight — we <span style="color:#93c5fd"><strong>truncate it to zero bytes</strong></span> on every flush. The WAL only ever holds the operations since the last flush, so it stays small.

Now the honest cost, and it's a big one. For **100% durability**, the WAL append must be <span style="color:#ff8a8a"><strong>fsync'd to disk on every single write</strong></span> — and that means a disk write per operation, which *erases the "writes never touch disk" advantage we built in Section 1.* You don't have to run it that way: you can batch or async the fsync, accepting a tiny window of loss (the last few milliseconds of writes) in exchange for keeping the speed. So WAL durability is, once again, a <span style="color:#ffff99"><strong>config / SLA knob</strong></span> — fsync-every-write for zero loss and lower throughput, or grouped fsync for a small loss window and high throughput. You pick the point on that line your SLA demands.

> **Memory hook:** *write-ahead log = append the op to a durable log before touching the memtable, so a crash replays the log to rebuild the buffer (zero loss). Truncate it on every flush. fsync-per-write = full durability but kills the speed win — so it's an SLA knob.*

---

## Section 11 — So is this actually faster than Bitcask? And why not Redis?

**Question: hold on — if full durability means the WAL fsyncs to disk on every write, then we're writing to disk per operation just like Bitcask. So where's the win? Did we build all this for nothing?**

Be precise about what we did and didn't gain. With a fully-durable WAL, the per-write disk cost is comparable to Bitcask — we did *not* magically make durable writes free. The real, durable win is elsewhere: <span style="color:#ffff99"><strong>the keys are disk-bound, not memory-bound.</strong></span> Bitcask capped you at the number of keys whose index fit in RAM; here, RAM holds only the live memtable plus the small per-file Bloom filters, so you can store *vastly* more keys on the same machine — bounded by disk, which is cheap and huge. We pay comparable write amplification to Bitcask while removing its memory ceiling. That was the entire goal from the brief.

<img src="../assets/lsm-trees/vs-bitcask-redis.svg" alt="Comparing the LSM engine against Bitcask and Redis, with use cases. Left panel, 'vs Bitcask': a small table — Bitcask: keys MEMORY-bound (all keys' index in RAM), O(1) reads. LSM: keys DISK-bound (RAM holds only the memtable + per-file Bloom filters), reads cost a bit more (memtable + Bloom-guarded SSTable probes) but you store far more keys per machine. Verdict (yellow): comparable write cost, but the LSM lifts Bitcask's memory ceiling — keys bounded by disk, not RAM. Middle panel, 'why not just Redis?': Redis keeps ALL data in RAM, so it's also memory-bound (the same wall) and a key with a TTL expires and the data is gone. An LSM keeps recent/hot data in RAM (memtable) and the rest durably on disk — some data in memory, some on disk — so it persists beyond RAM size and beyond a TTL. Right panel, 'who uses LSM + use cases': production engines — RocksDB, LevelDB, BadgerDB, Cassandra, HBase. Use case spotlight: ad-tech / real-time bidding — a bid lives ~4 minutes then must persist; in Redis the key would expire and the data would vanish, but an LSM serves the hot recent bid fast from the memtable AND keeps it durably on disk. The pattern named: 'read-after-write on recent data' — you just wrote it, you're about to read it, and it must also survive. Bottom caption: LSM wins when recent data is hot AND everything must persist, at a scale too large to keep every key in RAM." width="1000">

That also answers "why not just use Redis?" Redis keeps **everything in RAM**, so it hits the *same* memory wall — and worse, a key with a TTL <span style="color:#ff8a8a"><strong>expires and the data is gone</strong></span>. An LSM tree keeps the hot, recent data in RAM (the memtable) *and* everything else durably on disk — some data in memory, some on disk — so it persists far beyond RAM size and never silently drops a key at a TTL.

> **Redis persistence side note:** Redis Open Source can persist its in-memory dataset with **RDB snapshots**, an **append-only file (AOF)**, or both. That improves restart durability, but the active dataset is still memory-resident; persistence does not turn it into an LSM-style dataset that can grow beyond RAM. A TTL is also an explicit application choice: when configured, expiration is intentional deletion, not a failure of Redis persistence. See the [official Redis persistence guide](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/).

Where does this shine? The canonical case is <span style="color:#ff8bd2"><strong>ad-tech / real-time bidding</strong></span>. A bid is written, lives hot for ~4 minutes while the auction runs, and is then read back — but it *also* must persist for billing and audit. In Redis you'd set a TTL and the data would simply vanish; an LSM serves that hot recent bid fast from the memtable **and** keeps it durably on disk. The general pattern is <span style="color:#8aff8a"><strong>read-after-write on recent data that must also survive</strong></span>, at a scale too large to hold every key in RAM. And it's not theoretical — this exact engine is <span style="color:#93c5fd"><strong>RocksDB, LevelDB, BadgerDB, Cassandra, and HBase.</strong></span>

> **Memory hook:** *the LSM win over Bitcask isn't write speed — it's that keys are disk-bound, not RAM-bound, so you store far more per machine. Beats Redis because hot data sits in RAM while everything persists on disk (no TTL data loss). Ideal for read-after-write on recent data at scale — e.g. ad-tech bidding.*

---

## Where this leaves us: the complete LSM tree

We started from Bitcask's one wall — every key in RAM — and lifted it by changing what RAM is *for*: a small write buffer instead of a total index. That single move forced everything else into existence. Writing to the <span style="color:#ff8bd2"><strong>memtable</strong></span> made writes fast but volatile; <span style="color:#93c5fd"><strong>periodic flushes</strong></span> turned the buffer into immutable <span style="color:#ffff99"><strong>SSTables</strong></span>; sorting those files made their indexes sparse and their merges linear; <span style="color:#ff8bd2"><strong>compaction</strong></span> kept the file count bounded; <span style="color:#8aff8a"><strong>Bloom filters</strong></span> killed the missing-key read cost; and a <span style="color:#ffff99"><strong>write-ahead log</strong></span> bought back the durability the memtable gave away.

<img src="../assets/lsm-trees/final-map.svg" alt="The complete LSM-tree storage engine in one map, with every path labelled. Write path (pink): a PUT/DEL first appends to the write-ahead log (WAL) on disk for durability, then applies to the memtable (an in-RAM sorted buffer). Flush path (blue): when the memtable hits its size or time threshold, it is flushed in one shot to a new immutable SSTable on disk, and the WAL is truncated to zero. Storage layer (center/yellow): a stack of immutable SSTables on disk, each = sorted data + a sparse key→offset index, and each fronted by an in-RAM Bloom filter. Read path (green): a GET(k) checks the memtable first (newest data), and on a miss walks the SSTables newest-first, but at each file it consults that file's Bloom filter in RAM and skips the file entirely on a NO, only doing a disk index-lookup + seek-read on a MAYBE; first hit wins, else not-found. Maintenance path (blue/background): a compaction job merges immutable SSTables in O(n) (they're sorted), dropping stale and tombstoned records, collapsing many files into fewer and keeping read cost bounded. Legend: pink = write path, green = read path, yellow = durable storage + indexes, blue = background/flush/compaction plane, red = the costs (volatile memtable, fsync trade). Caption: this is an LSM tree — RocksDB, LevelDB, Cassandra, HBase, BadgerDB. Real-world note: a typical deployment is leveled — the memtable flushes to small L0 files that compact into larger L1, L2 files; you rarely need more than ~3 levels." width="1180">

One last real-world note the diagram hints at: production LSM engines are usually <span style="color:#ffff99"><strong>leveled</strong></span>. The memtable flushes into small **Level 0** files, which compact into larger **Level 1** files, which compact into still-larger **Level 2** files — a hierarchy of size tiers rather than one flat pile. The mechanics are exactly what we built (sorted files, merge, Bloom filters); leveling just organizes *which* files compact together. In practice you rarely need more than about three levels.

The whole engine is one idea followed honestly to its conclusions: **keep a small sorted buffer in RAM, flush it to immutable sorted files on disk, merge those files in the background, and use a Bloom filter and a write-ahead log to make reads cheap and writes durable.** That shape — the log-structured merge tree — is how a modern key-value store holds more keys than memory could ever fit, while still absorbing writes at the speed of RAM.

> **Memory hook:** *an LSM tree = memtable (RAM, sorted) + WAL (durability) → flush to immutable sorted SSTables (each with a Bloom filter) → background compaction keeps files few. Reads check RAM then Bloom-guarded files newest-first. Disk-bound keys, RAM-speed writes — that's RocksDB/LevelDB/Cassandra.*

---

## LevelDB internals: the complete path and why each part exists

**Question: what does the deliberately small LevelDB implementation add around the basic LSM idea so writes, reads, recovery, and file replacement all remain correct?**

<img src="../assets/lsm-trees/leveldb-architecture.svg" alt="LevelDB end-to-end architecture. The write path appends an atomic WriteBatch to the current log, then inserts its records into the active sorted memtable. When the memtable fills, LevelDB rotates to a new log and memtable while the old immutable memtable flushes in the background to an overlapping Level-0 SSTable. Compaction merges overlapping Level-0 files with Level 1, then moves bounded key ranges through non-overlapping Level 1 through Level 6 files. The read path checks the active memtable, immutable memtable, Level-0 files newest-first, and then at most one candidate file per higher level; optional Bloom filters, the table cache, and block cache avoid unnecessary file and block reads. CURRENT points to the MANIFEST, which records the live files, levels, and key ranges so recovery can reconstruct the serving state. Each component includes a short explanation of why it exists." width="1180">

The write path is intentionally short. A `WriteBatch` first enters the current append-only log, then the same ordered mutations enter the active memtable. The log exists so recovery can replay acknowledged updates; the sorted memtable exists so current reads see the newest state and a later flush can produce an ordered table efficiently. A synchronous write can request durable storage before returning, while the default asynchronous mode trades machine-crash durability for throughput.

When the active memtable reaches its configured size, LevelDB makes it immutable and immediately installs a new log plus a new active memtable. The old memtable then flushes in the background. This <span style="color:#93c5fd"><strong>rotation separates foreground writes from flush I/O</strong></span>: new writes continue while the frozen state becomes one Level-0 SSTable.

Level 0 is special because independently flushed files may cover overlapping key ranges. Reads may therefore need to check several L0 files, newest first. Levels 1 and above enforce non-overlapping ranges within each level, so range metadata identifies at most one candidate file per level. Inside an SSTable, the index points to sorted data blocks; optional filters reject absent keys, the table cache avoids reopening files, and the block cache keeps hot blocks in memory.

Compaction chooses files from one level plus overlapping files from the next, merges their sorted streams, and writes a sequence of bounded output files. It removes versions that are no longer visible and advances data toward larger levels. The reason is not merely cleanup: compaction converts overlapping write-optimized runs into a read-efficient hierarchy while bounding file count and reclaiming obsolete space.

Finally, `CURRENT` identifies the active `MANIFEST`, and the MANIFEST records the live SSTables, levels, key ranges, and file-set changes. On restart, LevelDB reconstructs that versioned file map and replays remaining log records. This metadata layer is what makes replacing immutable files safe: readers use a consistent version while a compaction prepares and atomically installs the next one.

Implementation references: [LevelDB implementation notes](https://github.com/google/leveldb/blob/main/doc/impl.md), [SSTable format](https://github.com/google/leveldb/blob/main/doc/table_format.md), and [API, synchronous writes, snapshots, and caching](https://github.com/google/leveldb/blob/main/doc/index.md).

> **Memory hook:** *LevelDB is the compact reference design: log + sorted memtable on write, immutable tables arranged into one overlapping level and several non-overlapping levels, caches and filters on read, and a MANIFEST that atomically names the live file set.*

---

## RocksDB at a high level: LevelDB's shape engineered for production workloads

**Question: RocksDB began from LevelDB's design. What did it add so the same LSM shape can handle concurrent server workloads, multiple logical datasets, fast SSDs, and sustained ingestion?**

<img src="../assets/lsm-trees/rocksdb-architecture.svg" alt="High-level RocksDB architecture. Concurrent writes enter a write coordinator that can combine compatible writes into one WAL append and fsync. One database can contain multiple column families; they share the WAL for ordered crash recovery while each column family has its own mutable and immutable memtables, options, and LSM tree. Full memtables enter a flush pipeline served by background flush workers. SSTables are arranged using configurable leveled, universal, or FIFO compaction, with parallel compaction workers and optional subcompactions. Reads check mutable and immutable memtables, then use file metadata, Bloom filters, indexes, table cache, and block cache to reach candidate SSTable blocks. The MANIFEST records file-set changes. Backpressure slows or stops writes when immutable memtables, Level-0 files, or compaction debt exceed configured limits. Callouts explain why each production feature exists." width="1180">

RocksDB keeps the same three foundations—WAL, memtables, and immutable SSTables—but makes the paths independently tunable. Concurrent compatible writes can be grouped into one WAL write and one `fsync`, amortizing durability cost. Column families share the database WAL so cross-column-family batches and recovery preserve ordering, while each column family owns its memtables, options, and LSM file hierarchy. That gives logical isolation without requiring a separate database process or durability stream for every dataset.

A full memtable becomes immutable, a new mutable memtable takes over, and background flush workers drain the immutable pipeline. RocksDB can retain several immutable memtables and reserve threads specifically for flushing. The reason is sustained concurrency: foreground writers should not wait for each individual flush, and flush work should not be starved by long compactions.

On disk, leveled compaction remains the default, but RocksDB also offers universal and FIFO styles because no one write/read/space tradeoff fits every workload. Multiple compactions can run in parallel, and selected compactions can use subcompactions. These features exist because on fast storage the compaction pipeline—not the initial memtable insert—often determines sustainable write throughput.

The read path combines mutable and immutable memtables with SSTable metadata, indexes, Bloom filters, a table cache, and a block cache. Index and filter blocks can be cached or partitioned, and data blocks compete for a bounded cache budget. RocksDB exposes these choices because memory should be spent differently for point-lookups, scans, large databases, and different storage devices.

Production throughput also requires a brake. If flush or compaction falls behind, RocksDB deliberately delays or stops writes based on immutable-memtable count, Level-0 file count, or pending compaction bytes. <span style="color:#ff8a8a"><strong>Backpressure protects the database</strong></span> from unbounded read amplification, space amplification, and eventual disk exhaustion. The MANIFEST records every installed file-set change, while the WAL protects updates not yet represented in SSTables.

Architecture references: [RocksDB overview](https://github.com/facebook/rocksdb/wiki/RocksDB-Overview), [memtables and flush triggers](https://github.com/facebook/rocksdb/wiki/MemTable), [leveled compaction](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction), [WAL group commit](https://github.com/facebook/rocksdb/wiki/WAL-Performance), [block cache](https://github.com/facebook/rocksdb/wiki/Block-Cache), and [write stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls).

> **Memory hook:** *RocksDB keeps LevelDB's LSM core, then adds production controls around it: group commit, column families, pipelined memtables, parallel and selectable compaction, explicit cache management, and backpressure when background work cannot keep up.*

---

## Questions that complete the mental model

### How does `GET` choose the newest value across multiple files?

**Question: the same user key may appear in the active memtable, an immutable memtable, and several SSTables. How does the engine know which copy is newest without trusting only the file name or level?**

Every mutation receives a monotonically increasing <span style="color:#ffff99"><strong>sequence number</strong></span>. The engine stores an internal key shaped like:

```text
(user key, sequence number, record type)
```

Records sort first by user key and then by sequence number in descending order. For one user key, the newest mutation therefore appears before its older versions:

```text
k @ 105 -> PUT v3
k @ 101 -> DELETE
k @ 92  -> PUT v2
k @ 40  -> PUT v1
```

A normal `GET(k)` reads at the database's latest sequence number. A snapshot read uses the sequence number captured when that snapshot was created. The lookup asks for the newest record whose sequence number is less than or equal to that read sequence:

```text
latest read at sequence 110  -> k @ 105 -> v3
snapshot at sequence 103     -> k @ 101 -> not-found
snapshot at sequence 95      -> k @ 92  -> v2
```

The physical search still starts with the mutable memtable, then the immutable memtable, then candidate SSTables. But correctness comes from <span style="color:#8aff8a"><strong>sequence-number visibility</strong></span>, not merely “the newest file wins.” A matching deletion record is also a result: it means the key is logically absent at that read sequence, so the engine must not expose an older value.

LevelDB's internal-key comparator orders equal user keys by decreasing sequence number, and its lookup key includes the read's sequence number. See the official [internal-key comparator and lookup-key construction](https://github.com/google/leveldb/blob/main/db/dbformat.cc) and [snapshot API](https://github.com/google/leveldb/blob/main/doc/index.md#snapshots).

> **Memory hook:** *every mutation carries a sequence number. `GET` returns the newest value—or tombstone—visible at the read's sequence number, wherever that record physically lives.*

### Does `GET` use the same Bloom filter after a key is deleted?

**Question: an older SSTable may still contain `k → v1`, while a newer memtable or SSTable contains `k → tombstone`. Could the Bloom filter say the key exists even though `GET(k)` must return not-found?**

Yes—and that is correct. A Bloom filter answers a <span style="color:#ffff99"><strong>physical membership question</strong></span>: “could this SSTable contain a record for user key `k`?” It does not answer the logical question: “does `k` currently have a live value?”

`DELETE(k)` follows the write path rather than consulting a separate delete filter:

```text
DELETE(k)
  -> append a deletion record to the WAL
  -> insert that deletion record into the memtable
```

Suppose the physical state is:

```text
new memtable:  k -> TOMBSTONE
old SSTable:   k -> v1
```

A `GET(k)` checks newer state first. It finds the tombstone and returns <span style="color:#93c5fd"><strong>not-found</strong></span>; it must not continue to the older value. After the tombstone is flushed, the new SSTable's filter may answer **MAYBE** for `k`, because that file really does contain a record for `k`. The subsequent lookup reads that record, discovers that it is a deletion marker, and returns not-found.

Bloom filters are not edited after every delete. SSTables are immutable. A flush creates a new SSTable and a new filter; compaction creates replacement SSTables and replacement filters; only after the new files are installed can the old files and filters be retired. Compaction can eventually remove both the old value and tombstone, but only when no snapshot or unexamined lower level still needs the deletion marker.

LevelDB's internal records distinguish values from deletion markers, while its filter policy derives membership from the user-key portion of those records. See the official [internal-key format](https://github.com/google/leveldb/blob/main/db/dbformat.h), [memtable lookup behavior](https://github.com/google/leveldb/blob/main/db/memtable.cc), and [filter-block implementation](https://github.com/google/leveldb/blob/main/table/filter_block.cc).

> **Memory hook:** *a Bloom filter says “this file may contain a record for `k`,” not “`k` is logically alive.” The tombstone decides the result; compaction cleans up the physical history later.*

### What does “embedded database” mean?

**Question: if RocksDB and LevelDB are not database servers, where do they execute, where does their data live, and how do they scale?**

An <span style="color:#ffff99"><strong>embedded database</strong></span> is a library linked into the application. The storage engine runs inside the application's process and thread context:

```text
application process
  -> business logic
  -> RocksDB / LevelDB library
       -> memtables and caches in process memory
       -> flush and compaction background threads

local data directory
  -> WAL files
  -> SSTables
  -> MANIFEST and supporting metadata
```

`db.Put()` and `db.Get()` are ordinary in-process function calls, not network requests to a separate database service. The engine is still persistent: its WAL, SSTables, and metadata live in a configured filesystem directory, normally on local SSD. They survive process restarts. Whether an acknowledged recent write survives a full machine crash depends on the selected WAL and synchronization settings.

Scaling happens at different boundaries:

- **Inside one process:** concurrent application threads, caches, column families, and background flush/compaction workers.
- **Inside one machine:** more CPU, RAM, storage bandwidth, larger disks, or multiple embedded database instances.
- **Across machines:** the surrounding system must add sharding, replication, routing, failover, or consensus.

RocksDB does not become distributed merely because the application runs on several servers. Each instance owns local state; the application or a higher-level database coordinates those instances. RocksDB explicitly documents that replication belongs above the engine, while multiple local databases can share process-level thread pools, block caches, and rate limiters. See the official [RocksDB overview](https://github.com/facebook/rocksdb/wiki/RocksDB-Overview).

> **Memory hook:** *embedded means “the database engine lives inside your process.” Its files are durable locally; distribution, replication, and cross-machine scaling belong to the surrounding system.*

### Why keep keys sorted—and when is the sorting work paid?

**Question: sorting improves reads and compaction, but does maintaining sorted keys make every write slow? Is the memtable a hash table that gets sorted only during flush?**

For LevelDB and default RocksDB, the memtable is <span style="color:#ff8bd2"><strong>not a hash table</strong></span>. It is a sorted skip list. Each write performs an in-memory ordered insertion, typically expected `O(log n)`, after the WAL append:

```text
PUT(k, v)
  -> append to WAL
  -> insert into sorted skip-list memtable
```

That costs more CPU than an expected `O(1)` hash-table insertion, but it happens in RAM and buys several properties at once:

- `GET` can search the memtable efficiently.
- Range scans can iterate keys in order.
- Flush can walk the memtable directly in sorted order instead of sorting the entire buffer.
- SSTable indexes can address ordered data blocks.
- Compaction can merge sorted inputs in one linear pass.

The engine therefore pays a small ordering cost on each default memtable write so that flush and merge remain predictable:

```text
sorted memtable
  -> sequentially write sorted SSTable

sorted SSTable A + sorted SSTable B
  -> linear merge into sorted output
```

RocksDB also provides alternative memtables for specialized workloads. A vector memtable appends cheaply and sorts when it flushes. Hash-based memtables optimize selected prefix lookups, but fully ordered scans and flushing become more expensive. The choice is a tradeoff about <span style="color:#ffff99"><strong>when to pay for order</strong></span>, not whether sorted SSTables are useful.

See the official RocksDB documentation for the [default skip-list and alternative memtables](https://github.com/facebook/rocksdb/wiki/MemTable), and LevelDB's table builder, which writes entries by iterating its already ordered input: [LevelDB table construction](https://github.com/google/leveldb/blob/main/db/builder.cc).

> **Memory hook:** *sorted keys move work from random disk operations into cheap in-memory comparisons. The default skip list maintains order during writes; other memtables may defer sorting until flush.*

### Why can't a tombstone be removed immediately?

**Question: once `DELETE(k)` has hidden every older value, why not erase the tombstone during the next compaction?**

Because the compaction may not include every physical copy of `k`. Consider:

```text
Level 1 input:  k -> TOMBSTONE
Level 4 file:   k -> old value
```

If compaction removes the tombstone while the older value remains in Level 4, a later `GET(k)` can expose that value again. The deleted key has been <span style="color:#ff8a8a"><strong>resurrected</strong></span>.

Snapshots add a second constraint. A snapshot created before the delete may still need to read the old value:

```text
sequence 80:  PUT k, v1
snapshot:     sequence 90
sequence 100: DELETE k
```

The latest view should return not-found, while the snapshot at sequence 90 should still return `v1`. Compaction must retain enough history to satisfy both views.

LevelDB drops a tombstone only when it is older than the oldest active snapshot and the engine knows no copy of that key exists in lower, unexamined levels. Until both conditions hold, the tombstone is a <span style="color:#ffff99"><strong>correctness record</strong></span>, not disposable garbage. See LevelDB's official [compaction drop conditions](https://github.com/google/leveldb/blob/main/db/db_impl.cc).

> **Memory hook:** *remove a tombstone too early and an older value can reappear. Keep it until no snapshot needs the old history and no lower level can contain a hidden copy.*

### What happens if the process crashes halfway through flush or compaction?

**Question: compaction creates new SSTables and removes old ones. What prevents a crash halfway through from leaving the database with half the old state and half the new state?**

The engine separates <span style="color:#93c5fd"><strong>building files</strong></span> from <span style="color:#ffff99"><strong>installing files</strong></span>.

During flush or compaction, it first writes new SSTables under new file numbers. LevelDB finishes each table, synchronizes and closes it, and verifies that the table can be opened. The old SSTables still remain part of the current database version during this work.

Only after all required outputs are ready does the engine append one version edit to the MANIFEST. That edit says, conceptually:

```text
add:     new-output-1.sst, new-output-2.sst
remove:  old-input-a.sst, old-input-b.sst
```

The MANIFEST transition makes the new file set visible as one consistent version. If the process crashes before that installation, recovery continues to use the old version; incomplete or unreferenced outputs can be cleaned up. If it crashes after installation, recovery uses the new version, and obsolete input files can be removed later.

A flush crash is also protected by the WAL. If the new SSTable was never successfully installed, recovery replays the relevant log records and rebuilds the memtable. `CURRENT` points to the active MANIFEST, whose version edits reconstruct the last known consistent file set.

See LevelDB's [table building and synchronization](https://github.com/google/leveldb/blob/main/db/builder.cc), [compaction installation](https://github.com/google/leveldb/blob/main/db/db_impl.cc), and RocksDB's official [MANIFEST description](https://github.com/facebook/rocksdb/wiki/MANIFEST).

> **Memory hook:** *write and verify new files first; publish one MANIFEST edit second; delete old files later. A crash sees either the old installed version or the new installed version—not a half-installed merge.*

### What happens when compaction cannot keep up with writes?

**Question: foreground writes can arrive faster than flush and compaction can reorganize them. Why not keep accepting writes and let the background work catch up later?**

The debt accumulates physically:

- Immutable memtables queue up waiting to flush.
- Level-0 files accumulate and increase read amplification.
- Pending compaction bytes consume disk space.
- Future compactions need progressively more I/O to recover.

Continuing indefinitely risks severe read latency and eventually a full disk. RocksDB therefore applies <span style="color:#ff8a8a"><strong>write backpressure</strong></span>. It first delays writes, reducing ingestion toward the rate the storage device can sustain. At harder limits it stops writes until flush or compaction makes progress.

The main triggers are too many immutable memtables, too many Level-0 SSTables, and too many estimated pending compaction bytes. Normal application writes can block during a stall. Callers that set `no_slowdown` can instead receive an incomplete status rather than waiting.

The real sustainable write rate is therefore not “how fast can RAM accept inserts?” It is approximately “how fast can the whole system flush and compact those inserts over time?” See the official RocksDB [write-stall documentation](https://github.com/facebook/rocksdb/wiki/Write-Stalls).

> **Memory hook:** *RAM absorbs bursts; compaction determines sustained throughput. When background work falls behind, backpressure protects read latency and disk space by slowing or stopping writers.*

### What does “durable” mean: `write()`, page cache, or `fsync()`?

**Question: if the WAL append succeeded, is the write already safe from every kind of crash?**

Not necessarily. There are several durability boundaries:

```text
application / RocksDB buffer
  -> operating-system page cache
  -> storage device
  -> persistent media
```

A successful `write()` commonly means the kernel accepted the bytes into its page cache. That usually survives an application-process crash, because the kernel is still running. It may not survive a machine crash or power loss.

An `fsync()` asks the operating system and storage stack to persist the file's pending data before returning. In RocksDB, `WriteOptions.sync = true` synchronizes the WAL before acknowledging the write. With the default non-sync mode, the WAL is not crash-safe against a machine failure even though it can still recover from a process restart after the kernel writes those pages.

Durability does not require one separate `fsync()` for every operation. <span style="color:#ff8bd2"><strong>Group commit</strong></span> combines compatible concurrent writes into one WAL write and one synchronization, then acknowledges the whole group. This preserves the durability boundary while amortizing expensive I/O.

Hardware, filesystems, and storage configuration still matter: the guarantee is only as strong as the lower layers' implementation of flush and ordering commands. See RocksDB's official [WAL performance and sync-mode documentation](https://github.com/facebook/rocksdb/wiki/WAL-Performance).

> **Memory hook:** *`write()` usually reaches the OS; `fsync()` requests persistence before acknowledgement. Group commit lets many durable writes share one synchronization.*

### How do range scans work across several sorted sources?

**Question: `GET(k)` looks for one key, but a scan such as `[k100, k200)` may cross the memtables and many SSTables. How does the engine return one ordered, duplicate-free view?**

Each source exposes a sorted iterator:

```text
active memtable iterator
immutable memtable iterator
SSTable iterator A
SSTable iterator B
...
```

A merging iterator keeps the current key from each child in a heap and repeatedly emits the smallest internal key. This produces one globally sorted stream without materializing the complete result first.

That internal stream can contain several versions and tombstones for the same user key. A database iterator applies the read's snapshot sequence, returns the newest visible value, suppresses older versions, and skips keys whose newest visible record is a tombstone:

```text
internal stream:
  k1 @ 9 -> PUT v2
  k1 @ 4 -> PUT v1
  k2 @ 8 -> DELETE
  k2 @ 3 -> PUT old

user-visible scan:
  k1 -> v2
```

Sorted keys make this streaming merge possible, but scans can still have read amplification: the iterator may need children from multiple levels, and long-lived iterators can pin the file version and blocks they reference. See RocksDB's official [iterator implementation](https://github.com/facebook/rocksdb/wiki/Iterator-Implementation) and LevelDB's [version-filtering database iterator](https://github.com/google/leveldb/blob/main/db/db_iter.cc).

> **Memory hook:** *a range scan heap-merges every relevant sorted source, then filters the internal stream by snapshot, version, and tombstone to expose one ordered user view.*

### What are read, write, and space amplification?

**Question: LSM tuning repeatedly mentions “amplification.” What is being amplified, and why can't one configuration minimize all three dimensions?**

Amplification compares physical work or storage with the application's logical request:

| Amplification | Meaning | Typical LSM cause |
| --- | --- | --- |
| **Read amplification** | Physical files, blocks, or bytes examined per logical read. | The key may have candidates in several runs or levels. |
| **Write amplification** | Total bytes written by WAL, flush, and compaction per byte written by the application. | Compaction rewrites existing data while merging new data downward. |
| **Space amplification** | Physical storage occupied relative to the current logical dataset. | Old versions, tombstones, overlapping runs, and temporary compaction outputs coexist. |

The compaction strategy moves cost among these dimensions. Leveled compaction maintains fewer overlapping runs, usually improving reads and steady-state space, but it can rewrite data many times. Tiered or universal compaction delays those rewrites and lowers write amplification, but permits more sorted runs and greater temporary space usage.

There is no universal best setting because workload goals conflict:

```text
fewer runs        -> cheaper reads, more merge rewriting
more runs         -> cheaper writes, more read and space overhead
larger fanout     -> fewer levels, larger individual compactions
smaller fanout    -> more levels, more frequent transitions
```

Measure amplification in production alongside latency and throughput; ratios without workload context can mislead. See RocksDB's official [compaction tradeoff overview](https://github.com/facebook/rocksdb/wiki/Compaction) and [leveled compaction documentation](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction).

> **Memory hook:** *an LSM does not eliminate work—it moves it. Compaction policy decides whether you pay more in reads, rewritten bytes, or temporary disk space.*

### When does a Bloom filter become ineffective or too expensive?

**Question: if Bloom filters avoid unnecessary SSTable reads, why not allocate an enormous filter for every file and use it for every query?**

Bloom filters help most when the workload performs point lookups for keys that are absent from many candidate SSTables. A definite **NO** avoids the data lookup. They help less when:

- Most queried keys actually exist, because a positive result still requires the real lookup.
- The workload is dominated by broad range scans; whole-key filters do not prove that a range is empty.
- Too few bits per key produce enough false positives that many unnecessary reads remain.
- Filters and indexes consume memory that would have produced more value as cached data blocks.
- Constructing very large filters adds flush or compaction CPU and temporary memory pressure.

More bits reduce false positives, but returns diminish. RocksDB's documented example is roughly 9.9 bits per key for a 1% false-positive rate and 15.5 bits per key for 0.1%. The extra memory should be justified by the I/O it saves.

RocksDB can skip filters on the last level for workloads dominated by successful lookups, cache index and filter blocks, partition large filters, or use prefix filters for selected iterator seeks. The right filter policy therefore depends on hit rate, query shape, cache budget, and storage latency. See the official [RocksDB Bloom filter guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter).

> **Memory hook:** *Bloom filters buy fewer negative probes with RAM. They are valuable for absent-key point reads, less valuable for hits and broad scans, and oversized filters can steal memory from the data cache.*

### Why does compaction produce multiple output files instead of one?

**Question: compaction already merges several inputs into one sorted stream. Why not write that entire stream into one enormous SSTable?**

The merged stream is logically one sorted run, but the engine splits it into multiple physical SSTables:

```text
sorted merged stream
  -> output-1.sst  [a ... f]
  -> output-2.sst  [g ... m]
  -> output-3.sst  [n ... z]
```

Bounded output files provide several practical benefits:

- File metadata can route a read to a narrow key range.
- Indexes and filters remain independently cacheable.
- Later compactions can rewrite only overlapping ranges instead of one enormous file.
- File creation, verification, movement, and deletion remain manageable units.
- Different files can participate in parallel reads or later background work.

LevelDB also cuts an output when it reaches the configured maximum output size or when continuing would create too much overlap with “grandparent” files in the next level. Limiting that overlap prevents one new file from forcing an excessively large future compaction.

All output files from the compaction are prepared first and then installed together through one version edit. They are multiple physical files but one logical state transition. See LevelDB's official [`MaxOutputFileSize` and `ShouldStopBefore` contract](https://github.com/google/leveldb/blob/main/db/version_set.h) and [compaction output installation](https://github.com/google/leveldb/blob/main/db/db_impl.cc).

> **Memory hook:** *compaction creates one sorted run but several bounded files. Smaller physical units improve routing, caching, and future range-local compaction while the MANIFEST installs them as one logical result.*
