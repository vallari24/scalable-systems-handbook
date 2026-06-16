# Building a Superfast Key-Value Database

This post builds a Bitcask-style key-value storage engine from first principles. It starts with the hardware constraint that durable data must land on disk, then shows how append-only writes, length-framed records, CRC checksums, an in-memory hash index, file rotation, and background compaction combine into a persistent store with O(1) reads, writes, and deletes.

**Question: you have to build a key-value store where reads, writes, and deletes are all about as fast as the hardware will physically allow — and the data has to survive a reboot. No "just use Redis," no "just use RocksDB." You own the file format, the index, and the crash recovery yourself. What's the smallest design that gets you O(1) on every operation *and* persistence?** The honest path runs straight through the storage hierarchy, a single append-only file, a handful of bytes of framing, and one in-memory hash table — and by the end you've hand-built [Bitcask](https://riak.com/assets/bitcask-intro.pdf), the storage engine that backs Riak in production.

## The brief

Before designing anything, pin down what we're actually being asked for. The requirements are short, but each word steers a decision later.

<img src="../assets/fast-kv-db/requirements.svg" alt="The brief: a superfast key-value database. Requirements: superfast reads, superfast writes, superfast deletes, and persistence (data survives a crash or reboot). The API is tiny — PUT(k, v), GET(k), DEL(k) — and that smallness is the gift: no ranges, no scans, no joins, every operation touches exactly one key. Four dimensions any design has to get right: Write path (how do we make a write as cheap as the disk allows?), Read path (how do we find one key in O(1)?), Durability (how does data survive a crash mid-write?), and Space (how do we stop the files growing without bound?). The hard constraint underneath all four: persistence means the bytes must land on disk, and disk is slow in exactly one way we must design around." width="1000">

The API is tiny — `PUT(k, v)`, `GET(k)`, `DEL(k)` — and that smallness is a gift. <span style="color:#8aff8a"><strong>Every operation touches exactly one key</strong></span>: no ranges, no prefix scans, no joins. So we never need a sorted tree or a query planner; we need exact-match key access and nothing more.

The hard word is <span style="color:#ffff99"><strong>persistence</strong></span>. "Superfast" alone would let us keep everything in RAM and call it a day. But the data has to survive a crash, which means the bytes must reach <span style="color:#ffff99"><strong>disk</strong></span> — and disk is slow in exactly one way that will shape the entire design. So before we write a single `PUT`, we have to understand the hardware we're writing to.

> **Memory hook:** *the API is just PUT/GET/DEL on single keys — the only hard requirement is persistence, which drags us onto disk, and disk has one weakness the whole design is built to dodge.*

---

## Section 1 — The Storage Hierarchy: Persistence Means Disk, and Disk Hates Seeks

**Question: "superfast" and "survives a reboot" pull in opposite directions — the fast memory is volatile, and the durable memory is slow. Where on the hardware do our bytes actually have to live, and what exactly makes that level slow?**

Computer storage is a ladder, and every rung trades speed for durability and cost. At the top, <span style="color:#8aff8a"><strong>CPU caches and RAM</strong></span> are blisteringly fast but <span style="color:#ff8a8a"><strong>volatile</strong></span> — pull the power and they're empty. At the bottom, <span style="color:#ffff99"><strong>magnetic disk and tape</strong></span> are durable and cheap but slow. Persistence forces our hand: the authoritative copy of the data *must* live on a durable rung, which in practice means disk.

<img src="../assets/fast-kv-db/storage-hierarchy.svg" alt="The storage hierarchy as a ladder from fastest/most-expensive/volatile at the top to slowest/cheapest/durable at the bottom. Rungs: CPU registers and cache (about 1 nanosecond, volatile), RAM (about 100 nanoseconds, volatile), SSD (about 100 microseconds, durable), magnetic disk / HDD (about 10 milliseconds, durable), tape (seconds, durable, archival). A callout on the HDD rung: those 10 milliseconds are not transfer time, they are seek time — the read/write head physically moving across the spinning platter to the right track. Random access pays this seek on every operation; sequential access pays it once and then streams. The lesson: persistence forces our authoritative bytes onto a durable rung (disk), and on disk the enemy is not reading or writing bytes, it is the seek between them. Design to avoid seeks and even a slow HDD becomes fast." width="1000">

Now zoom into the rung that hurts. A magnetic disk stores data on a spinning platter, and a physical head has to move to the right track before it can read or write. That movement — the <span style="color:#ff8a8a"><strong>seek</strong></span> — is the slow part, roughly <span style="color:#ff8a8a"><strong>10 milliseconds</strong></span>, which is *millions* of times slower than a RAM access. Crucially, the slowness isn't in moving bytes; it's in *repositioning the head between* bytes that live in different places.

#### A look inside the disk: what a "seek" physically is

Why is moving the head so slow? Because it's a *mechanical* motion in a world where everything else is electronic. A magnetic disk is a spinning platter whose surface is carved into concentric <span style="color:#8aff8a"><strong>tracks</strong></span>, each divided into <span style="color:#ff8bd2"><strong>sectors</strong></span> — the small blocks where bytes actually live. A read/write <span style="color:#ff8bd2"><strong>head</strong></span> rides on a swinging arm above the surface, and to touch any one sector, three things have to happen — and only the last one moves data.

<img src="../assets/fast-kv-db/magnetic-disk.svg" alt="Inside a magnetic disk: where the 10 milliseconds go. Left: a top-down view of a spinning platter. The surface is divided into concentric circular tracks and into pie-slice sectors by radial dividers; one track is highlighted in green and labelled 'a track'. At the center is the spindle (the axis the platter spins around). A cluster of pink dots sitting on the highlighted track is labelled 'one sector — a block of bytes'. A read/write head, drawn as a small block on the end of a long swinging arm pinned outside the platter, hovers over the highlighted track; a red double-headed arrow near the head is labelled 'seek: swing to a track', and a blue arrow below the platter shows it spinning. Right: reading or writing one sector takes three steps. Step 1, SEEK (~10 ms, mechanical, drawn in red as the slow step): the arm physically swings the head to the target track — this moving part is the entire 10 ms. Step 2, ROTATIONAL LATENCY (yellow): wait for the platter to spin the target sector around under the head. Step 3, TRANSFER (green, fast): bytes stream under the head as the platter turns — actually moving data is cheap. The punchline callout: the cost is in REACHING the data, not moving it. A sequential write lands in the next sector on the same track, already rolling under the head, so there is no new seek and it runs at full speed; a random write lands on a far track, so it pays the seek (step 1) every single time." width="1000">

So a disk access is two mechanical waits — the **seek** to the right track and the **rotational latency** until the right sector spins under the head — followed by one quick **transfer**. The 10 ms lives almost entirely in those first two. Touch the *next* sector on the *same* track and there's nothing to wait for; jump to a far track and you pay the seek all over again.

That single fact is the hinge of the whole design. <span style="color:#ff8a8a"><strong>Random access</strong></span> — touching scattered locations — pays a seek every single time. <span style="color:#8aff8a"><strong>Sequential access</strong></span> — reading or writing one continuous stretch — pays the seek *once* and then streams at full speed. So the design rule writes itself: if we can arrange for every write to land at the end of one continuous file, we sidestep the disk's only real weakness, and even a cheap HDD starts to look fast.

> **Memory hook:** *persistence forces us onto disk, and disk's one weakness is the seek — so never scatter writes; keep them sequential and the slow rung stops being slow.*

---

## Section 2 — Log-Structured Storage: Append, Never Overwrite

**Question: a normal database, to update a row, seeks to where that row lives and overwrites it in place — a seek per write. If seeks are the enemy, what if we simply refused to ever go back and overwrite anything?**

That refusal is the entire idea behind <span style="color:#ffff99"><strong>log-structured storage</strong></span>. The data file is treated as an <span style="color:#ff8bd2"><strong>append-only</strong></span> log: writes only ever go to the *end*, never into the middle. No random updates, no overwrites — just one growing file, written <span style="color:#8aff8a"><strong>sequentially</strong></span>.

<img src="../assets/fast-kv-db/append-vs-random.svg" alt="Two ways to write, side by side. Left, update-in-place (the classic approach): to write keys, the disk head seeks to each key's existing location and overwrites it — arrows jump back and forth across the platter, one seek per write, drawn in red as the slow anti-pattern. Right, append-only log: every write goes to the current end of one file, the head never moves backward, writes stream sequentially in green. Below: what we get — high write throughput even on a spinning HDD, because we deleted the seek from the write path entirely; the note says roughly a 5000x gain over random writes on an HDD. A second note: an SSD has no moving head, so it does not get the same dramatic gain from going sequential — but append-only still wins on an SSD for a different reason (fewer write-amplification rewrites and simpler crash recovery). The punchline: turning random writes into sequential appends is the single biggest lever for write speed on durable storage." width="1000">

What does that buy us? <span style="color:#8aff8a"><strong>High write throughput</strong></span>, even on a spinning HDD, because we removed the seek from the write path entirely. The gain over random in-place writes on a magnetic disk is enormous — on the order of <span style="color:#ff8bd2"><strong>thousands of times faster</strong></span>. The disk that was our problem in Section 1 is now writing at its full sequential speed.

One honest caveat: an <span style="color:#93c5fd"><strong>SSD</strong></span> has no moving head, so it does *not* see the same dramatic speedup from going sequential — its random access was never that slow to begin with. Append-only still wins on an SSD (less write amplification, dead-simple crash recovery), just for subtler reasons. The headline 5000× number is an HDD story. Either way, append-only is the right shape — so let's design the simplest possible store on top of it.

**This is the main point, and it generalizes far beyond our toy store.** Log-structured storage is, first and foremost, a bet on <span style="color:#ff8bd2"><strong>write throughput</strong></span>: it trades a little read and space complexity (you now need an index, and stale records pile up) for writes that are nothing but cheap sequential appends. So whenever a system has to absorb writes *fast and in volume*, it almost always turns out to be log-structured underneath. Bitcask here, the <span style="color:#ffff99"><strong>LSM-trees</strong></span> inside RocksDB, LevelDB, Cassandra and HBase, the <span style="color:#ffff99"><strong>write-ahead log</strong></span> every relational database flushes to first, even Kafka's commit log — all the same move: if you want to ingest writes at the speed of the disk, you stop overwriting in place and start appending to a log. If your workload is write-heavy, this is the shape to reach for.

> **Memory hook:** *log-structured = append-only: writes go only to the end of one file, never overwrite in place — which deletes the seek from the write path and makes even an HDD scream. It's the default design for any high-write-throughput system (LSM-trees, WALs, Kafka) for exactly this reason.*

---

## Section 3 — The Simplest Design: One Append-Only File of Key-Value Pairs

**Question: we've committed to append-only. So what is the dumbest thing that could possibly work — and does "append-only" even let us express an update or a delete, when those sound like things that *change* existing data?**

Start with the simplest store imaginable: a single file, and every `PUT(k, v)` <span style="color:#ff8bd2"><strong>appends</strong></span> one `key, value` record to the end of it. That's the whole write path. It's a <span style="color:#ff8bd2"><strong>lightning-fast</strong></span> sequential append — exactly the operation Section 2 made cheap.

<img src="../assets/fast-kv-db/append-log.svg" alt="One append-only file holding a sequence of operations, growing top to bottom. The operations issued, in order: put k1 v1, put k2 v2, put k3 v3, put k1 v1' (an update to k1), put k4 v4, put k2 v2' (an update to k2), del k3. The file contents, in append order: (k1,v1), (k2,v2), (k3,v3), (k1,v1'), (k4,v4), (k2,v2'), (k3, tombstone). Three annotations. One, an UPDATE is just another append — k1 appears twice, and the rule is the newest record for a key wins, so v1' overrides v1; the old (k1,v1) is now a stale entry still sitting in the file. Two, a DELETE is also just an append — DEL(k3) is written as PUT(k3, tombstone), a special marker value (drawn as -1); we never erase the old record, we append a gravestone that says 'k3 is gone'. Three, both update and delete are therefore the same lightning-fast sequential append as a normal write. The cost we are quietly accepting: the file accumulates stale and tombstoned records that waste space — a problem we will pay off later with compaction." width="1000">

Now the clever part: append-only seems to forbid updates and deletes, but it doesn't — it just expresses them differently.

- **Update** `PUT(k1, v1')` after `PUT(k1, v1)`? We don't go back and edit the old record. We simply <span style="color:#ff8bd2"><strong>append a new one</strong></span>. The key `k1` now appears twice in the file, and we adopt one rule: <span style="color:#8aff8a"><strong>the newest record for a key wins</strong></span> (`v1'` overrides `v1`). The old record is now *stale*, but it sits there harmlessly.
- **Delete** `DEL(k3)`? There's nothing to erase in an append-only world. Instead we append a <span style="color:#ff8bd2"><strong>tombstone</strong></span> — a special marker value (think `-1`) that means "k3 is gone." A delete is just a `PUT(k3, tombstone)`: the same fast append as any other write.

So all three mutations — write, update, delete — collapse into the *one* operation we already made fast. The price we're quietly accepting is that the file now fills with stale and tombstoned records that waste space. Hold that thought; Section 9 pays it off. First, two problems are closer: how do we even read one record back, and how do we survive a crash mid-write?

> **Memory hook:** *one append-only file; PUT appends, UPDATE appends again (newest wins), DELETE appends a tombstone — every mutation is the same cheap append, at the cost of stale records piling up.*

---

## Section 4 — Anatomy of an Entry: Framing Variable-Length Records

**Question: we're appending records to a file. Later we'll want to read exactly one of them back. When the reader lands at the start of a record, how does it know where that record *ends* — how many bytes to read?**

The tempting answer is "read until a newline," the way a text file works. But that breaks immediately: a <span style="color:#ffff99"><strong>value can contain any bytes</strong></span>, including newlines, so there's no delimiter we can trust to mark the boundary. And keys and values are <span style="color:#ffff99"><strong>variable length</strong></span> — `k1` might be 2 bytes, the next key 40 — so the reader can't assume a fixed size either.

<img src="../assets/fast-kv-db/entry-format.svg" alt="How one entry in the file is framed, built up in two steps. Step one, the naive layout: [ K | V ] — just the key bytes followed by the value bytes. The problem: when the reader lands here it has no idea where K ends and V begins, and cannot read 'until newline' because the value may contain any bytes including newlines, and both fields are variable length. Step two, the fix — a length-prefixed (framed) record: [ KSZ | VSZ | K | V ], where KSZ and VSZ are fixed-width 4-byte integers giving the key size and value size in bytes. The read procedure, shown as numbered steps: 1, read the first 4 bytes to get KSZ; 2, read the next 4 bytes to get VSZ; 3, now you know the exact lengths, so read KSZ bytes for the key; 4, read VSZ bytes for the value. Because the two sizes sit at fixed offsets at the front of the record, the reader always knows exactly how far to read — no delimiter needed, and the value can hold arbitrary bytes. The takeaway: prefix variable-length data with its length; this is how almost every binary format frames records." width="1000">

The fix is the universal trick for variable-length data: <span style="color:#ffff99"><strong>prefix each field with its length</strong></span>. Lay the record out as `[ KSZ | VSZ | K | V ]`, where `KSZ` and `VSZ` are fixed-width 4-byte integers holding the key size and value size. Now reading one record is fully deterministic:

1. Read the first **4 bytes** → that's `KSZ`.
2. Read the next **4 bytes** → that's `VSZ`.
3. You now know the lengths, so read exactly `KSZ` bytes → the key.
4. Read exactly `VSZ` bytes → the value.

Because the two sizes always sit at the same offsets at the front of every record, the reader *always* knows precisely how far to read, and the value is free to contain any bytes at all. No delimiter, no ambiguity — just two small numbers that frame the rest. This is how essentially every binary format on disk works.

> **Memory hook:** *you can't delimit binary values with a newline — prefix each record with fixed-width key-size and value-size, then read exactly that many bytes.*

---

## Section 5 — Crash Safety: Detecting Torn Writes with a Checksum

**Question: persistence was the whole point — but what happens if the machine crashes *while* we're in the middle of appending a record? We get half a record on disk. How does the reader later tell a good record from a corrupt one?**

Picture a write in flight: we've written `KSZ`, `VSZ`, the key, and the first chunk of the value, and then the power dies. The file now ends in a <span style="color:#ff8a8a"><strong>torn record</strong></span> — the length prefix promises (say) 200 bytes of value, but only 110 made it to disk. A naive reader trusts `VSZ`, reads past the real data into garbage, and silently returns a corrupt value. Persistence that hands back corruption is worse than no persistence.

<img src="../assets/fast-kv-db/crc-integrity.svg" alt="Crash safety for the append-only record, in two parts. Top: the torn-write problem. A record being appended — [ KSZ | VSZ | K | V... ] — where VSZ claims a 200-byte value but the machine crashed while writing the value, so only 110 bytes actually reached disk (drawn as a jagged broken edge mid-value). A reader that blindly trusts VSZ would read past the real bytes into garbage and return a corrupt value, silently. Bottom: the fix — add a CRC checksum and a timestamp to the front of every record, giving the final layout [ CRC | TS | KSZ | VSZ | K | V ]. CRC (cyclic redundancy check) is a small number computed from the record's bytes and written first; on read, the reader recomputes the CRC over the bytes it read and compares — if they differ, the record is corrupt (a torn write) and is discarded, so corruption is detected, never returned. TS is a timestamp written into each record, used to resolve conflicts and decide which write is newer when the same key appears more than once. Note that CRC is written first / flushed first so that if it is present the rest of the record is known to be intact. The takeaway: a checksum turns 'silently return garbage' into 'detect and skip the broken record', which is what makes the persistence trustworthy." width="1000">

The fix is a <span style="color:#ffff99"><strong>checksum</strong></span>. Before each record we store a <span style="color:#ffff99"><strong>CRC</strong></span> (cyclic redundancy check) — a small number computed from the record's bytes. On write, we compute the CRC and put it at the very front. On read, we recompute the CRC over the bytes we just read and compare it to the stored one. If they <span style="color:#ff8a8a"><strong>don't match</strong></span>, the record is torn or corrupt, and we discard it instead of returning it. Corruption is now *detected*, never silently served.

While we're adding header fields, we add one more: a <span style="color:#ffff99"><strong>timestamp (TS)</strong></span>. When the same key appears in multiple records — or across multiple files later — the timestamp is the tiebreaker that decides which write is genuinely the newest. The final on-disk record is:

```
[ CRC | TS | KSZ | VSZ | K | V ]
```

`CRC` first (so its presence vouches for everything after it), then `TS`, then the framing from Section 4, then the data. That's the complete, crash-safe entry format. Now we can write durably and read records back — but reading still means scanning the file to *find* a key. Let's fix that.

> **Memory hook:** *a crash mid-append leaves a torn record — store a CRC up front and recompute it on read to detect and skip corruption, and a timestamp to settle which write is newest.*

---

## Section 6 — Fast GET: The In-Memory Hash Index

**Question: our file is now a durable, well-framed log. But to answer `GET(k)`, do we really scan the entire log looking for the latest record of `k`? On a large file that's O(n) per read — the opposite of superfast. How do we get to O(1)?**

Scanning is hopeless at scale, so we add the same thing every database adds: an <span style="color:#ff8bd2"><strong>index</strong></span>. And because the keys are small, we can afford the fastest possible index — an <span style="color:#8aff8a"><strong>in-memory hash table</strong></span> mapping each key to the <span style="color:#8aff8a"><strong>byte offset of its latest record</strong></span> in the file.

<img src="../assets/fast-kv-db/inmem-index.svg" alt="Fast GET via an in-memory hash index. Center: an in-memory hash table mapping each key to the byte offset of that key's most recent record in the data file: k1 to offset o1, k2 to offset o2, k3 to offset o3. The data file on disk on the right holds the actual records in append order (k1, k2, k1 again as an update, k3, k1 again, k3 again), and the index always points at the latest occurrence of each key. The GET(k) path, shown as three numbered steps and labelled a 'pointed query': 1, hash-table lookup in memory to get the offset (O(1), instant); 2, one disk seek to that offset; 3, one disk read of the record. So a GET costs one in-memory lookup plus exactly one disk seek and one disk read — no scan. On every PUT we also update the hash table to point at the new offset, and on DEL we remove the key from the table. The limitation, called out in red: the index lives in RAM, so all the keys must fit in memory (the values stay on disk, only the keys plus small offsets are in RAM). The takeaway: keep values on cheap disk, keep a tiny key-to-offset map in fast RAM, and every read becomes a pointed O(1) query." width="1000">

Now `GET(k)` becomes a <span style="color:#8aff8a"><strong>pointed query</strong></span> instead of a scan, in three cheap steps:

1. <span style="color:#8aff8a"><strong>Hash-table lookup</strong></span> in memory → get the offset. O(1), instant.
2. One <span style="color:#8aff8a"><strong>disk seek</strong></span> to that offset.
3. One <span style="color:#8aff8a"><strong>disk read</strong></span> of the record.

One memory hit plus a single seek-and-read — no matter how big the file grows. And the index stays correct for free: every `PUT` also updates the hash table to point at the new offset (which is *why* "newest wins" just works — the index only ever remembers the latest), and every `DEL` removes the key from the table.

There's a price, and it's the defining limitation of this whole design: the index lives in <span style="color:#ff8a8a"><strong>RAM</strong></span>, so **all the keys must fit in memory**. The *values* stay on cheap disk — only the keys and their small offsets are held in RAM — but if you have more distinct keys than memory can hold, this design doesn't fit. For the enormous number of workloads where keys do fit, you get O(1) reads, writes, and deletes.

> **Memory hook:** *don't scan to read — keep an in-memory hash table of key → latest byte offset, so GET is one memory lookup plus one seek-and-read; the catch is every key must fit in RAM.*

---

## Section 7 — When the File Grows Too Big: Rotation, Active and Immutable

**Question: a single append-only file grows forever, and an infinitely large file is awkward to manage, back up, and compact. The obvious fix is "use multiple files" — but if writes can land in any file, we've reintroduced random writes. How do we split the file without losing the append-only property?**

The trick is to keep appending to *one* file at a time, but to <span style="color:#ff8bd2"><strong>rotate</strong></span> it once it reaches some size threshold `t`. When the current file crosses `t` bytes, we close it, freeze it, and start a fresh one. Writes still only ever append to a single file — we've just made that file finite.

> **Note — how the swap happens cleanly:** a <span style="color:#93c5fd"><strong>symlink</strong></span> (symbolic link) is just a file whose contents are a *pathname* — a pointer that says "I actually mean that other file." The writer always appends to a fixed name like `active` that's really a symlink; to rotate, we create the new file, then atomically repoint the `active` symlink from the old file to the new one. Writers keep opening `active` and never notice — they just follow the link to wherever it now points.

<img src="../assets/fast-kv-db/file-rotation.svg" alt="File rotation: from one unbounded file to many bounded files with a single writable one. Top: the rule — when the current file grows past a threshold of t bytes, rotate: create a brand-new file to append to, and freeze the old file as immutable (read-only, never written again). Bottom: the resulting layout, a row of files. Four older files on the left are drawn in white/grey and braced together with the label 'Immutable — available for reads only'; they will never change again. One file on the right is drawn highlighted in yellow with the label 'Active'; an upward arrow into it is labelled 'all writes go here'. Only the single active file ever receives appends, so the append-only, sequential-write property is preserved — we did NOT scatter writes across files. Reads can come from any file, active or immutable, by following the in-memory index to whichever file holds a key's latest record. Two consequences noted: immutable files never change so they are trivially safe to back up and to compact in the background, and because old files are read-only there is never a writer and a reader contending on the same file. The takeaway: rotate the active file at a size threshold so writes stay sequential to one file while the data set spreads across many immutable files." width="1000">

So instead of one ever-growing file, we have many files, but at any moment exactly **one is `ACTIVE`** — the file all writes append to. Every other file is <span style="color:#93c5fd"><strong>immutable</strong></span>: closed, frozen, never written again, and available only for reads. Writes never scatter — they still go to a single file sequentially — so we kept the append-only property completely intact.

This split quietly unlocks two big wins. Because immutable files <span style="color:#93c5fd"><strong>never change</strong></span>, they're trivially safe to <span style="color:#8aff8a"><strong>back up</strong></span> (copy a file that can't change underneath you) and trivially safe to reorganize in the background (Section 9). And there's never a writer and a reader fighting over the same file, because only the lone active file is ever written. The data set now spans many files — which means our index needs to say not just *where* in a file a key lives, but *which* file.

> **Memory hook:** *one unbounded file is unmanageable — rotate at a size threshold so there's always exactly one ACTIVE file taking writes and many frozen, immutable files for reads, backups, and compaction.*

---

## Section 8 — The Richer Index Entry: Pointing Across Many Files

**Question: with the data spread across many files, an offset alone is no longer enough — offset 357 in *which* file? What does each entry in our in-memory index need to hold now?**

The index keeps the same shape — key → location — but the location grows from a bare offset into a small record that pinpoints the value across the whole file set. Each entry now holds four fields.

<img src="../assets/fast-kv-db/index-entry.svg" alt="The richer in-memory index entry, now that data spans many files. The in-memory index still maps a key to a location, but the location is now a four-field record. Shown: key k1 points to an entry [ File ID | VSZ | VPOS | TS ], and key k2 points to its own such entry, each arrow landing in a different data file among several drawn on disk (one of them highlighted as the active file). The four fields, labelled: File ID — which file holds the latest record for this key; VSZ (value size) — how many bytes the value is, so the reader knows exactly how much to read; VPOS (value position) — the byte offset of the value within that file; TS (timestamp) — when this record was written, used to decide which record is newest when resolving conflicts. So a GET(k) is: look up k in memory to get [File ID, VSZ, VPOS, TS], open that file, seek to VPOS, and read VSZ bytes — still one in-memory lookup plus one seek-and-read, just now addressed by (file, position) instead of position alone. The takeaway: across many files the index entry must name the file, the position, and the length — file id plus value position plus value size — and carry a timestamp to settle which write wins." width="1000">

- <span style="color:#8aff8a"><strong>File ID</strong></span> — which file holds the latest record for this key.
- <span style="color:#8aff8a"><strong>VPOS</strong></span> — the byte offset of the value *within* that file.
- <span style="color:#8aff8a"><strong>VSZ</strong></span> — the value's size, so the reader knows exactly how many bytes to read.
- <span style="color:#ffff99"><strong>TS</strong></span> — the timestamp, the tiebreaker for which record is newest.

`GET(k)` is unchanged in spirit: look up `k` in memory to get `[File ID, VPOS, VSZ, TS]`, open that file, seek to `VPOS`, read `VSZ` bytes. Still one in-memory lookup plus one seek-and-read — only now the address is a `(file, position)` pair instead of a lone offset. The index grew by a few bytes per key and bought us the ability to read any key out of any file in the set.

> **Memory hook:** *across many files, an offset isn't enough — each index entry holds File ID + value position + value size (+ timestamp), so a read still costs one lookup and one seek-and-read.*

---

## Section 9 — Merge and Compaction: Reclaiming the Wasted Space

**Question: every update left a stale record behind, and every delete left a tombstone plus the dead record it shadows. Across many immutable files, that's a lot of garbage taking up disk. How do we reclaim it without ever blocking reads or writes?**

This is the bill from Section 3 coming due. The fix is <span style="color:#ff8bd2"><strong>merge and compaction</strong></span>: a background job that walks the <span style="color:#93c5fd"><strong>immutable</strong></span> files and rewrites them into a smaller set, keeping only what matters.

<img src="../assets/fast-kv-db/merge-compaction.svg" alt="Merge and compaction reclaiming wasted disk space. Left: a sawtooth graph of disk usage over time — it climbs steadily as appends accumulate stale and tombstoned records, then drops sharply each time a compaction runs, then climbs again: a repeating saw pattern that keeps total disk bounded instead of growing forever. Right: what compaction does. It takes all the immutable files and merges them into a new, smaller set of compacted files, applying two skip rules: stale entries are skipped (for a key with several versions, only the newest record survives — the older overwritten versions are dropped), and deleted entries are skipped (a tombstone and the dead record it shadows are both dropped once compaction passes, so the space is finally reclaimed). The active file is left untouched because it is still being written; only frozen immutable files are compacted, so reads and writes never block. Bottom: the critical consequence — because records are rewritten into new files, every surviving key's File ID and value position change, so the in-memory index must be updated to the new locations, and that index swap must happen atomically so a concurrent GET never sees a half-updated index pointing at a file that no longer holds the record. The takeaway: compaction folds many garbage-filled immutable files into a few dense ones by keeping only the newest live record per key, then atomically repoints the index — bounding disk use without pausing the database." width="1000">

Compaction reads the immutable files and writes out a new, dense set, applying two skip rules:

- <span style="color:#ff8a8a"><strong>Stale entries skipped.</strong></span> If a key has several versions, only the <span style="color:#8aff8a"><strong>newest</strong></span> survives; the overwritten older records are dropped.
- <span style="color:#ff8a8a"><strong>Deleted entries skipped.</strong></span> A tombstone and the dead record it shadows are *both* dropped once compaction passes them — that's the moment the space for a deleted key is finally reclaimed.

Critically, compaction only ever touches <span style="color:#93c5fd"><strong>immutable</strong></span> files — the active file keeps taking writes untouched — so reads and writes never block. Plotted over time, disk usage becomes a <span style="color:#ff8bd2"><strong>sawtooth</strong></span>: it climbs as garbage accumulates, then drops each time compaction runs, staying bounded forever instead of growing without limit.

But compaction moves records into brand-new files, so every surviving key's `File ID` and `VPOS` <span style="color:#ff8a8a"><strong>change</strong></span>. The in-memory index must be repointed to the new locations — and that swap has to be <span style="color:#ffff99"><strong>atomic</strong></span>. If a `GET` ran against a half-updated index, it could follow a stale pointer into a file that no longer holds the record. So compaction finishes by <span style="color:#ffff99"><strong>atomically updating the index</strong></span> to the new layout, and only then are the old files deleted.

> **Memory hook:** *merge the immutable files, keeping only the newest live record per key and dropping tombstoned ones — disk use becomes a bounded sawtooth — then swap the index to the new offsets atomically.*

---

## Section 10 — What We Built: Bitcask

**Question: we started from "make it fast and persistent" and derived a whole storage engine one constraint at a time. Step back — what are this design's real strengths and its one hard limit, and is it a real thing people run?**

Let's name the tradeoffs honestly. The single <span style="color:#ff8a8a"><strong>limitation</strong></span> is the one from Section 6: **all keys must fit in memory**, because the index is an in-RAM hash table. That's the price of admission. In exchange, the <span style="color:#8aff8a"><strong>strengths</strong></span> are exactly what the brief asked for:

- <span style="color:#8aff8a"><strong>O(1) reads, writes, and deletes</strong></span> — every operation is a hash lookup plus at most one seek-and-read.
- <span style="color:#8aff8a"><strong>High throughput, low latency</strong></span> — writes are sequential appends; the design will happily saturate the disk's I/O.
- <span style="color:#8aff8a"><strong>Easy backups</strong></span> — immutable files never change, so you copy them with zero coordination.

<img src="../assets/fast-kv-db/bitcask-realworld.svg" alt="What we built and where it runs. Top: a summary card. Limitation — all keys must fit in memory. Strengths — O(1) reads/writes/deletes, high throughput and low latency, saturates disk I/O, easy backups. A label states: what we just designed is Bitcask, a real log-structured key-value storage engine. Bottom: where Bitcask is used in production. A client connects to a proxy, which routes to several Riak database nodes drawn as a horizontal row; each Riak node has its own instance of Bitcask running as its storage backend (each node box contains a small Bitcask box). Notes: Bitcask is one of the most efficient embedded KV stores, it was created for Riak (a distributed key-value database), and each Riak node runs an independent Bitcask instance to store that node's data on local disk. The takeaway: the design we derived from first principles — append-only log, in-memory hash index, framed records with CRC, rotation, and compaction — is exactly Bitcask, and it ships inside Riak in real distributed deployments." width="1000">

This isn't a toy. What we just derived from first principles *is* <span style="color:#ff8bd2"><strong>Bitcask</strong></span> — a real, production log-structured key-value store. It was built as the storage backend for <span style="color:#93c5fd"><strong>Riak</strong></span>, a distributed key-value database: a client talks to the cluster through a proxy, requests route to one of several Riak nodes, and <span style="color:#93c5fd"><strong>each node runs its own independent Bitcask instance</strong></span> to store that node's slice of the data on local disk. Every design decision we made — append-only log, in-memory hash index, length-framed records with a CRC, file rotation, background compaction — is a real part of how Bitcask works.

> **Memory hook:** *what we derived is Bitcask: append-only log + in-RAM hash index + CRC-framed records + rotation + compaction — O(1) everything, easy backups, one rule (keys fit in memory), shipping inside Riak.*

---

## Where this leaves us

We were asked for a key-value store that's fast on every operation and survives a reboot, and we built one by following the hardware. Persistence forced us onto <span style="color:#ffff99"><strong>disk</strong></span>; the disk's hatred of <span style="color:#ff8a8a"><strong>seeks</strong></span> forced us into an <span style="color:#ff8bd2"><strong>append-only log</strong></span>; the log made writes, updates (newest wins), and deletes (tombstones) all the same cheap append.

<img src="../assets/fast-kv-db/final-map.svg" alt="The complete picture of the key-value engine, three flows in one map. Write path (pink): PUT/UPDATE/DEL all append one CRC-framed record [CRC|TS|KSZ|VSZ|K|V] to the single ACTIVE file on disk, and update the in-memory hash index to point at the new location. Read path (green): GET(k) does an O(1) lookup in the in-memory hash index to get [File ID, VPOS, VSZ], then one seek-and-read into whichever file (active or immutable) holds the latest record. Storage layer (center): one ACTIVE file taking all appends, plus many IMMUTABLE files that are read-only, rotated off once the active file passes t bytes. Maintenance path (blue/background): a compaction job merges the immutable files, skipping stale and tombstoned records to reclaim space (disk usage as a bounded sawtooth), then atomically repoints the in-memory index to the new offsets. The in-memory hash index sits between the API and the files, mapping key to [File ID, VPOS, VSZ, TS], and is the single source of truth for where each key's latest value lives — the reason every read is O(1) and the reason it must be updated atomically on both write and compaction. Legend: green is the read path, pink is the write path, yellow is durable storage and the atomic index swap, blue is the background maintenance plane. This is Bitcask." width="1180">

Then we made reads O(1) with an <span style="color:#8aff8a"><strong>in-memory hash index</strong></span> of key → location, kept the records readable and crash-safe with <span style="color:#ffff99"><strong>length framing and a CRC</strong></span>, kept the files manageable by <span style="color:#ff8bd2"><strong>rotating</strong></span> one active file against many immutable ones, and kept disk bounded with background <span style="color:#93c5fd"><strong>compaction</strong></span> behind an atomic index swap. The one limitation — keys must fit in RAM — is the deliberate trade that buys all the speed. That complete shape is Bitcask, and it's exactly how a real engine turns a slow disk into a superfast key-value database.

> **Memory hook:** *a superfast KV store is just an append-only log on disk with a hash index in RAM: writes append CRC-framed records to the one active file, reads are O(1) lookups + one seek, and background compaction keeps disk bounded — that's Bitcask.*
