# Building a Word Dictionary Without a Database

**Question: you have to serve a 1 TB dictionary — 170,000 words, each with a meaning — and the one rule is that you can't use a traditional database. No MySQL, no Postgres, no Redis. How do you store it, look words up fast, ship it as one portable file, and update it every week without ever serving a bad read?** The honest path runs straight through file layout, indexing, and the same trick a real database uses internally — and by the end you've essentially hand-built a tiny read-optimized storage engine on top of plain files.

## The brief

Before designing anything, pin down what we're actually being asked for. The constraints are unusually shaped, and each one steers a decision later.

<img src="../assets/word-dictionary/requirements.svg" alt="The brief: no traditional database (get creative); about 1 TB of data and 170,000 words; every lookup is a single word; updated weekly through a changelog; must stay portable; scale storage and API servers; response time can be high (latency is relaxed). And four dimensions any design has to get right: Storage (where do 1 TB of bytes live without a DB?), Querying (how do we find one word fast?), Portability (can we hand someone the whole dictionary as one file?), Seamless updates (apply the weekly changelog with zero bad reads). No database means we own storage, lookup, updates, and portability ourselves." width="1000">

A few of these are gifts. <span style="color:#8aff8a"><strong>Every lookup is a single word</strong></span> — never a range, never a fuzzy search — so we never need a query planner or a B-tree of ranges; we need exact-match key lookup and nothing more. And <span style="color:#9aa6b2"><strong>response time can be high</strong></span> — latency is relaxed — so we're allowed to make a network hop per request instead of holding all 1 TB in memory.

The rest are the work. <span style="color:#ff8a8a"><strong>No traditional database</strong></span> means we own storage and lookup ourselves. <span style="color:#93c5fd"><strong>Portability</strong></span> means "the dictionary" has to be something you can hand to a colleague as one artifact. And <span style="color:#ff8bd2"><strong>weekly updates</strong></span> arrive as a changelog and must apply without users ever seeing a half-updated, garbled answer.

> **Memory hook:** *single-word lookups + relaxed latency means we can keep bytes on cheap storage and fetch one at a time — the hard parts are portability and clean weekly updates.*

---

## Section 1 — Where the Bytes Live: One Shared Store, Not N Copies

**Question: where do a terabyte of bytes even live? The first instinct is "on the API server" — so do we just keep the whole 1 TB file on each server's local disk?**

On a single server, that's fine. But the moment you scale out, it breaks on two fronts. <span style="color:#ff8a8a"><strong>Duplication</strong></span>: if heavy traffic needs 20 servers, you're now storing the same 1 TB twenty times — 20 TB of disk to hold 1 TB of dictionary. And <span style="color:#ff8a8a"><strong>update fan-out</strong></span>: every weekly change has to be pushed to all 20 disks, and any server that lags or misses one answers from a different version — <span style="color:#ff8a8a"><strong>inconsistency</strong></span> across the fleet.

<img src="../assets/word-dictionary/storage-local-vs-nas.svg" alt="Where the bytes live: N local copies vs. one shared store. Left, the anti-pattern: copy the full 1 TB onto each API server's local disk. With 20 servers that is 20 TB of duplicated storage, every weekly update must reach all 20 disks, and the result is wasteful with redundancy and inconsistency risk. Right, the fix: one shared store every server reads. Several stateless API servers all read from a single central store (NAS, or S3 in the cloud) that holds the 1 TB exactly once. Store the 1 TB once and update it once; API servers stay stateless so you can add as many as you like. This is what network-attached storage (NAS) is, and S3 is its managed cloud version." width="1000">

The fix is to stop copying the data and keep it in **one place every server shares**. A central store that all the API servers read over the network is exactly what <span style="color:#ffff99"><strong>network-attached storage (NAS)</strong></span> is for: store the 1 TB once, update it once, and let the servers stay <span style="color:#8aff8a"><strong>stateless</strong></span>. In the cloud, the managed version of that shared store is <span style="color:#ffff99"><strong>S3</strong></span> — durable, effectively unbounded, and read by every server at once. So the bytes live in S3, and the API servers keep nothing big locally.

> **Memory hook:** *don't put 1 TB on every server — 20 servers means 20 TB and 20 update targets; keep one shared copy (NAS, or S3 in the cloud) and let stateless servers read it.*

---

## Section 2 — Storage: Treat S3 as a Raw File System

**Question: S3 holds our bytes, but it's just a store of objects with no opinion about layout. How should we organize 170,000 words inside it?**

S3 behaves like a raw file system we control: it stores objects durably and leaves the structure entirely to us. Being creative just means putting that structure on top ourselves — and the first idea writes itself.

<img src="../assets/word-dictionary/approach-1-file-per-word.svg" alt="Approach 1: one file per word on S3, treating S3 as a raw network-attached file system. Inside S3, a folder per letter: s3://word-dictionary/a/apple.txt, a/america.txt, a/automatic.txt, ... z/zoo.txt. Each word.txt holds that word's full meaning; 170,000 words become 170,000 separate objects, and the first letter is the folder so the path is computable. Lookup get_word(w): build the path s3://word-dictionary/a/apple.txt, read that one object from S3, return its contents as the meaning — no scan, no index, the word itself is the address. Request path: user, LB, API, S3. But it breaks a major requirement: portability — the dictionary is now 170,000 scattered objects, not one artifact you can hand over, copy, or version as a whole." width="1000">

**Approach 1: one file per word.** Store each word as its own object, in a folder named for its first letter: `s3://word-dictionary/a/apple.txt`. Lookup is delightful — `get_word(w)` <span style="color:#8aff8a"><strong>computes the path</strong></span> from the word itself (`a` → `a/apple.txt`), reads that one object, and returns it. No scanning, no index; the word *is* the address. Behind a load balancer, a fleet of stateless API servers each do exactly this.

So why isn't this the answer? It quietly breaks <span style="color:#93c5fd"><strong>portability</strong></span>. "The dictionary" is now <span style="color:#ff8a8a"><strong>170,000 scattered objects</strong></span>. You can't hand a colleague a single file, copy the dictionary somewhere else in one move, or version it as a coherent whole. Great for lookups, bad for moving the data around — and portability was a hard requirement.

> **Memory hook:** *S3 is just a file system you rent; one-file-per-word makes lookup trivial but shatters the dictionary into 170,000 pieces you can't hand over as one thing.*

---

## Section 3 — One Big File, Made Fast With an Index

**Question: so put everything in one file instead. But if the dictionary is a single 1 TB file, how do you find one word in it without reading the whole thing?**

Start with the simplest one-file format: a <span style="color:#ffff99"><strong>CSV</strong></span>, one `word, meaning` per line, sorted. That fixes portability instantly — it's one file you can copy and version. But now lookup is the problem: to find `able`, the naive approach <span style="color:#ff8a8a"><strong>scans the file</strong></span> until it hits the line. On 1 TB, that's far too slow and too expensive to do per request. We traded a lookup problem for a scan problem.

<img src="../assets/word-dictionary/indexing.svg" alt="Making lookups fast by splitting into an index and a data file. Approach 2 is one big CSV file of about 1 TB with rows from 'a' to 'zoo'; the problem is that a lookup means scanning the file, which is too slow and too expensive. The fix: index.dat, a separate small sorted file mapping word to offset and length — for example 'a : 0 : 127', 'abandon : 127 : 130', 'ability : 257 : 100', 'able : 357 : 150', down to 'zoo : 1023895 : 196'. Look up the word in the index to get its offset and length, then seek straight to those bytes in data.dat (the 1 TB of meanings, byte-addressed) — for 'able', seek to offset 357 and read 150 bytes, no scan. The universal trick is indexing: keep bulk data in data.dat and a small sorted index.dat mapping each word to its byte offset and length." width="1000">

The fix is the single most universal trick in storage: **indexing**. Split the one file into two.

- <span style="color:#ffff99"><strong>`data.dat`</strong></span> holds the bulk — every meaning, concatenated, ~1 TB. It's just bytes, addressed by position.
- <span style="color:#8aff8a"><strong>`index.dat`</strong></span> is a small, sorted file mapping each word to *where its meaning lives*: `word : offset : length`. For example, `able : 357 : 150` means "able's meaning starts at byte 357 and is 150 bytes long."

Now a lookup is two cheap steps instead of one terabyte scan: find the word in the index to get its <span style="color:#8aff8a"><strong>offset and length</strong></span>, then <span style="color:#8aff8a"><strong>seek directly</strong></span> to those bytes in `data.dat` and read exactly that slice.

And notice *why* "seek to byte 357, read 150 bytes" is even possible: reading *L bytes starting at offset S* is a primitive the storage layer already gives you — <span style="color:#8aff8a"><strong>`read(offset, length)`</strong></span>. Every filesystem exposes it, and S3 exposes it as a ranged `GET`. Our index needs no database machinery; it just rides on a read primitive the storage already provides. This is precisely how a relational database's index finds a row, too — look up the key, get a location, read from there.

> **Memory hook:** *one file is portable but unscannable; add a small sorted index of `word → offset, length` and a lookup becomes a `read(offset, length)` — a seek, not a scan.*

---

## Section 4 — The Read Path: Keep the Index in Memory

**Question: the index still lives on S3. Do we really do *two* S3 reads per lookup — one for the index, one for the data? Can we make the index part free?**

We can, because the index is astonishingly small. Run the numbers: ~171,476 words, and each index entry is the word (~4.7 bytes on average) plus a separator and newline (3 bytes) plus two 4-byte numbers for offset and length (8 bytes) — about <span style="color:#8aff8a"><strong>15.7 bytes per entry</strong></span>. Times 171,476 words is roughly **2.6 MB**. The index for a 1 TB dictionary fits in memory hundreds of thousands of times over.

<img src="../assets/word-dictionary/read-path.svg" alt="The read path: index in memory, data on S3. A user sends GET /meaning?w=able through a load balancer to a fleet of API servers; each server holds the index in RAM. On boot, each server loads index.dat (about 2.6 MB, sorted) from S3 into memory. S3 is the durable store holding index.dat and data.dat (about 1 TB, byte-addressed meanings). A lookup, step by step: 1, the word comes in and is found in the in-memory index; 2, the index returns its offset and length; 3, one ranged read of data.dat on S3 at that offset; 4, return the bytes as the meaning. Why the index fits in RAM: 171,476 words times about 15.7 bytes per entry (4.7 for the word, 3 for separator and newline, 8 for two 4-byte numbers) is about 2.69 MB, roughly 2.6 MB, which trivially fits in every server's memory." width="1000">

So the read path is: on <span style="color:#93c5fd"><strong>boot</strong></span>, each API server pulls `index.dat` from S3 once and holds it in <span style="color:#8aff8a"><strong>RAM</strong></span>. From then on, a request does an <span style="color:#8aff8a"><strong>in-memory index lookup</strong></span> (instant), gets the offset and length, and issues exactly **one** <span style="color:#8aff8a"><strong>ranged read</strong></span> of `data.dat` on S3 — a `GET` for just those bytes, not the whole object. One memory hit plus one small S3 read per word.

This is the whole steady state, and it scales the obvious way: the servers are stateless and identical, so you put them behind a load balancer and add more when traffic grows. The 1 TB stays on cheap S3; only the 2.6 MB index is duplicated into each server's memory.

> **Memory hook:** *the index is ~2.6 MB, so load it into every server's RAM on boot; then each lookup is one memory hit and one ranged S3 read.*

---

## Section 5 — A Sorting Recap: Why Sorted Data Is Gold

**Question: the changelog arrives as a CSV of changed and new words. To apply it, each entry has to land in its correct place inside a 1 TB file. Can we just loop over the dictionary and patch it in place? And more fundamentally — which sorting algorithm even applies when the data is far bigger than memory?**

Rule out the obvious first. A `for` loop that walks the dictionary asking "is this word here?" assumes the dictionary is *in memory* — but it's a 1 TB file on S3, so a loop means streaming a terabyte per update. Load it all into RAM and patch there? That asks for <span style="color:#ff8a8a"><strong>1 TB of RAM</strong></span>. Both ignore the one fact that rescues us: the dictionary is **already sorted by word**, and the changelog arrives sorted too. So before we exploit that, here's the three-sort refresher — and why only one idea fits our constraints.

<img src="../assets/word-dictionary/sorting-recap.svg" alt="Three sorting algorithms and when to use each, under the constraint that we can't hold 1 TB in RAM and can't insert into the middle of a file. Insertion sort takes each next item and slots it into the sorted part — good when data is small or nearly sorted (O(n) there), but O(n squared) in general, and slotting into the middle means rewriting every byte after it, which is impossible on a 1 TB disk file, so you can't insert into the middle of the dictionary in place. Merge sort splits the list in half, sorts each half, and merges them back — O(n log n), stable, and works on data too big for RAM via external sort because you stream rather than random-access; the key insight is that the naive version still buffers in memory but its merge step needs only two cursors, so if the inputs are already sorted that merge step is all we need. Quick sort picks a pivot and partitions around it — a great general in-memory array sort (O(n log n) average, in-place), but it needs random access and the whole array in memory, which we don't have, and it's pointless here because our data is already sorted. The punchline: both inputs are already sorted, so we never sort; we run only merge sort's merge step, walking both with two pointers line by line, emitting the smaller each time — O(n), one streaming pass, constant memory. This is exactly why databases are obsessed with keeping data sorted." width="1000">

- <span style="color:#ffffff"><strong>Insertion sort</strong></span> walks the list and slots each item into the sorted part. Lovely on small or nearly-sorted data (O(n) there), but O(n²) in general — and "slot it in" means inserting into the *middle*, which on a 1 TB file means rewriting every byte that follows. <span style="color:#ff8a8a"><strong>You can't insert into the middle of the dictionary in place.</strong></span>
- <span style="color:#ffffff"><strong>Quick sort</strong></span> picks a pivot and partitions around it — the usual in-memory default (O(n log n) average, in-place). But it needs random access and the whole array in memory, and we have <span style="color:#ff8a8a"><strong>neither</strong></span>. It's also moot here: our data is already sorted.
- <span style="color:#8aff8a"><strong>Merge sort</strong></span> splits, sorts each half, and merges them back. Its outer recursion still buffers in memory in the naive form — but its <span style="color:#8aff8a"><strong>merge step</strong></span> is the gold: combining two *already-sorted* lists needs only two cursors, no random access, and works on data far bigger than RAM (this is how *external* sorting handles terabytes).

That last point is the whole trick. We don't need to *sort* anything — both inputs are already sorted — so we run merge sort's merge step alone: walk the dictionary and the changelog together with <span style="color:#93c5fd"><strong>two pointers</strong></span>, reading each <span style="color:#93c5fd"><strong>line by line</strong></span>, always emitting the smaller word next.

<img src="../assets/word-dictionary/two-pointer-merge.svg" alt="Merging two sorted lists with two pointers, reading each list line by line so neither is ever fully loaded. List A is 1, 3, 5, 7, 9 with cursor i; List B is 2, 4, 6, 8, 10 with cursor j. At each step you compare the two cursors, emit the smaller, and advance that cursor: 1 vs 2 emit 1 advance i; 3 vs 2 emit 2 advance j; 3 vs 4 emit 3 advance i; 5 vs 4 emit 4 advance j; 5 vs 6 emit 5 advance i; 7 vs 6 emit 6 advance j; 7 vs 8 emit 7 advance i; 9 vs 8 emit 8 advance j; 9 vs 10 emit 9 advance i and A is done; then drain B to emit 10. When one list runs out you copy the rest of the other because it's already sorted. The merged output is the single sorted list 1 through 10, built top to bottom. It runs in O(n), constant memory, one streaming pass — no list is ever fully in RAM, you hold only the two current lines, and this is the exact mechanic used to fold the changelog into the 1 TB dictionary." width="1000">

Compare the word under each cursor: emit the smaller, advance that cursor, repeat; when one side runs out, copy the tail of the other (it's already sorted). Nothing is ever fully loaded — which is exactly why this survives the "no 1 TB in RAM" constraint that killed the other approaches. One linear pass, <span style="color:#8aff8a"><strong>O(n)</strong></span>, only the two current lines in memory.

This is why databases are *obsessed* with keeping data sorted: sorted inputs turn what looks like an expensive re-sort into a single linear streaming pass — the same reason their indexes, log-structured merge trees, and compactions all keep records in sorted order.

> **Memory hook:** *can't loop a 1 TB file and can't fit it in RAM — but both inputs are sorted, so skip sorting and run merge sort's merge step: two pointers, line by line, O(n), constant memory.*

---

## Section 6 — Weekly Updates: Applying the Changelog

**Question: we now know the mechanic — merge two sorted lists with two pointers. So concretely, what does applying a week's changelog produce, and what happens when a changelog word already exists versus when it's brand new?**

Every week the dictionary changes — some meanings rewritten, some words added — and the changes arrive as a sorted <span style="color:#ff8bd2"><strong>changelog</strong></span> CSV. Because both the dictionary and the changelog are already sorted, we don't re-sort anything; we run the merge step from the last section, walking both with two pointers.

<img src="../assets/word-dictionary/changelog-merge.svg" alt="Weekly updates as merging two sorted lists. The current dictionary, sorted by word: a to a', b to b', c to c', d to d', f to f', g to g', h to h'. Plus this week's changelog, also sorted: c to c'' (rewritten), e to e' (brand new), f to f'' (rewritten). Merging them in one O(n) pass produces a new dictionary, still sorted: a to a', b to b', c to c'' (override applied), d to d', e to e' (new word inserted in order), f to f'' (override), g to g', h to h'. Why O(n): both lists are sorted, so this is the merge step of merge sort — one linear walk. The rebuild job: spin up a worker, pull the dictionary plus changelog, merge into a new dictionary and index, upload to S3. Because both the dictionary and the index are kept sorted, applying a week of changes is a single linear merge, not a re-sort — cheap to run on a throwaway worker every week." width="1000">

At each step, compare the word under the dictionary cursor with the word under the changelog cursor and decide — this is the "check if it's already present" rule:

- **Already present** in the dictionary → take the changelog's <span style="color:#ff8bd2"><strong>updated meaning</strong></span> and skip the old one (an override, like `c → c''` replacing `c → c'`).
- **Brand-new word** (it sorts before the current dictionary word) → <span style="color:#ff8bd2"><strong>insert</strong></span> it here, in order (like `e → e'` slotting between `d` and `f`).
- **Otherwise** → carry the existing dictionary entry forward unchanged.

One pass, <span style="color:#8aff8a"><strong>O(n)</strong></span>, and the output is a fresh dictionary that's *still sorted* — so we rebuild `index.dat` in the very same pass, since the entries come out in order.

The update job itself is a throwaway worker: spin one up, pull the current dictionary and the changelog, merge into a new `data.dat` + `index.dat`, and <span style="color:#ff8bd2"><strong>upload</strong></span> the results to S3. Nothing runs continuously; the weekly cadence just kicks off a batch job that produces the next version of the two files.

One thing this rebuild must *not* do is take the dictionary offline while it swaps in the new files. **We want zero downtime** — reads keep flowing the whole time the new version publishes. That requirement is exactly where the next section's trap hides.

> **Memory hook:** *applying the changelog is one merge pass — present word → override, new word → insert in order, else carry forward — producing a fresh sorted dictionary + index on a disposable worker, with no downtime.*

---

## Section 7 — The Trap: Don't Overwrite In Place

**Question: the worker produced new files. Where does it put them? The obvious answer is "the same path, overwriting the old ones" — and that obvious answer corrupts live traffic. Why?**

Because the index and the data are coupled by <span style="color:#ffff99"><strong>byte offsets</strong></span>, and the running servers are holding the *old* index in memory. The merge shifted where every meaning sits in the file. The instant you overwrite `data.dat` at the same path, the offsets in the old in-memory index point into the new file at the wrong places.

<img src="../assets/word-dictionary/transition-problem.svg" alt="The trap: overwriting in place corrupts live lookups. An API server holds the index in RAM with the entry 'apple : offset 100 : length 1024', built from the old dictionary on boot and never re-read, so the server keeps trusting offset 100. In data.dat (old) at the same S3 path, offset 100 correctly lands on 'apple, a fruit' — offsets still match, shown with a check. But data.dat (new) overwrote the old file in place, and because the merge shifted every offset, offset 100 now lands mid-entry on garbage like 'le, a fruit\\nzo...' — shown with an X. Same offset 100, wrong bytes. The user sees a random response during the swap. The index and the data are coupled by offsets, so the real question is how to publish a new version without breaking servers mid-flight." width="1000">

Concretely: a server's index says `apple : offset 100 : length 1024`. Against the <span style="color:#8aff8a"><strong>old file</strong></span>, byte 100 is the start of apple's meaning — correct. Against the <span style="color:#ff8a8a"><strong>new file</strong></span>, byte 100 now lands in the *middle* of some other entry, and the server happily returns 1024 bytes of <span style="color:#ff8a8a"><strong>garbage</strong></span>. The user sees a random response, and it lasts until every server reloads.

The lesson is general: when an index and its data move together, you cannot swap one underneath a reader. So the real question becomes **how do we publish a new version without breaking servers mid-flight?**

> **Memory hook:** *offsets in the cached index only make sense against the file they were built for — overwrite that file in place and every live server reads garbage.*

---

## Section 8 — Three Ways to Swap In a New Version Safely

**Question: given that in-place overwrites are poison, how do we cut over from the old dictionary to the new one with zero bad reads?**

There are three answers, increasing in safety. The first two manage the *timing* of reloads; the third removes the hazard entirely by never overwriting anything.

<img src="../assets/word-dictionary/transition-strategies.svg" alt="Three ways to swap in a new dictionary safely. 1, Periodic refresh: each server pulls the index on a timer; the timeline shows pull ticks, but when index.dat changes on S3 there is a stale window until the next pull and reload, during which reads are stale — it is dead simple to build but has stale reads between pulls, shortened with a graceful terminate and reload. 2, Reactive pub/sub: when S3 changes, a change event is published to a Redis pub/sub bus, which tells all API servers to reload now — low lag, but one more system to run; S3 holds index.dat (new) and data.dat (new). 3, Parallel setup plus meta.json, the safest, an atomic version flip: new files are uploaded to a new versioned path — s3://word-dictionary/002/index.dat and 002/data.dat — while 001 stays in place; a meta.json file points 'index' and 'data' at the 002 paths, flipping 001 to 002 atomically. Old servers keep serving 001, a new server reads meta.json and serves 002, and no file is ever overwritten." width="1000">

**1 · Periodic refresh.** Every server re-pulls the index on a timer. Dead simple, but there's a <span style="color:#ff8a8a"><strong>stale window</strong></span> between when the new file lands on S3 and when each server next refreshes — during which a server may read across mismatched versions. You can shrink the window by tying refresh to a graceful terminate-and-reload, but you can't fully close it this way.

**2 · Reactive pub/sub.** Instead of polling, push. When S3 changes, publish a <span style="color:#93c5fd"><strong>change event</strong></span> to a bus (Redis pub/sub) that every API server subscribes to, and they reload immediately. Much lower lag than polling — but you've added a pub/sub system to run, and reloads still happen *into* the same servers, so you must be careful they don't mix old offsets with new data mid-reload.

**3 · Parallel setup with `meta.json`.** The clean answer: never overwrite. Write each new build to a **new versioned path** — `s3://word-dictionary/002/index.dat` and `002/data.dat` — leaving `001/` untouched. A small <span style="color:#93c5fd"><strong>`meta.json`</strong></span> pointer says which version is current. Publishing is: upload the new files, then <span style="color:#ff8bd2"><strong>flip `meta.json`</strong></span> from `001` to `002` in one atomic write. <span style="color:#9aa6b2"><strong>Old servers</strong></span> keep serving `001` (still intact); a <span style="color:#8aff8a"><strong>new server</strong></span> reads `meta.json` on boot and serves `002`. Each version's index and data always match, because no file is ever modified after it's written.

> **Memory hook:** *don't manage reload timing — remove the hazard: write each build to a new versioned path and flip a `meta.json` pointer atomically, so index and data are never mismatched.*

---

## Section 9 — Portability: Fold Index and Data Into One File

**Question: we now have two files per version, `index.dat` and `data.dat`. But portability wanted *one* artifact. How do we merge them back into a single file without losing the ability to tell where the index ends and the data begins?**

Concatenate them — `[ Index | Data ]` — and the immediate problem is finding the boundary. Your first instinct, a separator byte between the two sections, is a <span style="color:#ff8a8a"><strong>bad choice</strong></span>: the data is arbitrary text, so any separator you pick could occur naturally inside a meaning, and you'd split in the wrong place.

Is there a smarter separator? You could try to pick a byte that "can't" appear, but you can never guarantee that against arbitrary text. So drop the separator idea entirely and steal the trick that **every file format in the world already uses**: a <span style="color:#ffff99"><strong>fixed-width header</strong></span> at the front.

<img src="../assets/word-dictionary/portability-header.svg" alt="One portable file: a fixed-width header plus index plus data. The merged file is a 16-byte Header, then the Index, then the Data. Zooming into the 16-byte header shows four fields: 6 bytes of magic string (DICEDB) that names the format, a 4-byte integer word count (170,000), a 4-byte integer index size in bytes (e.g. 3563214), and 2 bytes of version or reserved — totaling 16 bytes, fixed. Why a magic header and not a separator: a separator byte can occur inside the arbitrary-text data so you would split in the wrong place; instead every real file format starts with a fixed-width header whose first bytes are a magic identifier (PNG, ZIP, class files all do this), so reading the first 6 bytes gives DICEDB and tells you the format and that the header is 16 bytes wide. The offsets fall out of the header: index starts at 16, index ends at 16 plus index_size, data starts where the index ends — nothing extra to store. Boot: read the 16-byte header, load the index, serve. Update: the whole file is recreated as a new version with header, index, and data rebuilt together." width="1000">

The header's first bytes are a <span style="color:#93c5fd"><strong>magic identifier</strong></span> that names the format — exactly how PNG, ZIP, and Java class files announce themselves. A reader grabs those bytes, recognizes the format, and knows precisely how wide the header is. For our dictionary, the header is **16 bytes**, laid out for this use case:

- **6 bytes** — magic string `DICEDB`. Read these and you know the format (and that the header is 16 bytes).
- **4 bytes** — the <span style="color:#ffff99"><strong>word count</strong></span> as a binary integer (170,000).
- **4 bytes** — the <span style="color:#ffff99"><strong>index size</strong></span> in bytes, as a binary integer.
- **2 bytes** — version / reserved for later use.

That's everything, because the section boundaries now *derive* from those numbers instead of being stored or marked. The <span style="color:#8aff8a"><strong>index starts at byte 16</strong></span> (right after the header), it <span style="color:#8aff8a"><strong>ends at `16 + index_size`</strong></span>, and the data begins immediately after. No separator, no ambiguity — just a fixed width plus two sizes.

Put numbers on it. Say the index is `index_size = 3,563,214` bytes. Then the index occupies bytes `16 … 3,563,230`, and the **data section begins at byte `16 + 3,563,214 = 3,563,230`**. Now look up `apple`: its index entry reads `offset 1024, length 47` — and those are positions *within the data section*. The absolute read against the single file is `3,563,230 + 1024 = 3,564,254`, length `47`. One <span style="color:#8aff8a"><strong>`read(offset, length)`</strong></span> returns apple's meaning — index, header, and data all live in one file, and the math is just additions of sizes the header handed us.

Boot gets one short step longer: read the fixed 16-byte header, use it to load the index into memory, then serve. And since the dictionary is already rebuilt as a whole each week, an <span style="color:#ff8bd2"><strong>update</strong></span> simply recreates the entire file — header, index, and data together — as a new version. "The dictionary" is now genuinely one portable, self-describing file, and it slots right into the versioned-path scheme from the last section.

> **Memory hook:** *don't trust a separator that might appear in the data — start the file with a fixed-width header (magic bytes + section sizes); the magic names the format and the sizes derive every offset.*

---

## Section 10 — Where This Shows Up in the Real World

**Question: this looks like a puzzle answer. But is "bulk data as files on cheap storage, with an index to query it" a real pattern — or just a workaround for a no-database rule?**

It's a real, widely-used pattern, usually under the name <span style="color:#ffff99"><strong>multi-tiered storage</strong></span>. The exact tension we navigated — cheap bulk storage versus the ability to look data up — is how production systems handle data that grows without bound.

<img src="../assets/word-dictionary/multitier.svg" alt="Where this shows up: multi-tiered storage. A user querying orders goes through a query router that splits recent versus historical. Recent queries go to MySQL holding the latest orders — the hot tier, fast and fully queryable. Historical and archived queries go to S3 holding historical orders — the cold tier, cheap object storage archived as files, with Athena running SQL directly over the files. Same idea as the dictionary: the dictionary taught us to put bulk data as files on S3 and keep an index to query it; real systems do the same with cold data, moving it to S3 to save cost but keeping it queryable via Athena, partitions, and a metastore. Cheap, still queryable." width="1000">

The canonical example is order history. Keep the <span style="color:#8aff8a"><strong>latest orders</strong></span> in a database like MySQL — the hot tier, where queries are fast and rich. Age <span style="color:#ffff99"><strong>historical orders</strong></span> out to <span style="color:#ffff99"><strong>S3</strong></span> as files — the cold tier, dramatically cheaper. The catch is the same one we solved: moving data to files normally costs you queryability. Systems recover it with tools like <span style="color:#93c5fd"><strong>Athena</strong></span> that run SQL directly over files in S3, plus partitions and a metastore to avoid scanning everything — the production-grade version of our hand-built index.

So the dictionary wasn't a trick question. "Put the bulk on cheap object storage, keep a small index so you can still find any one record" is exactly how you store cold data at scale without losing the ability to query it.

> **Memory hook:** *hot data in a database, cold data as files on S3 with an index/metastore over it — the dictionary is multi-tiered storage in miniature.*

---

## Where this leaves us

We were forbidden a database and built one anyway — the read half of one. Bulk data went onto cheap <span style="color:#ffff99"><strong>S3</strong></span> as a single file; a small sorted <span style="color:#8aff8a"><strong>index</strong></span> of `word → offset, length` turned a 1 TB scan into a seek; and because that index is only ~2.6 MB, every API server holds it in <span style="color:#8aff8a"><strong>memory</strong></span> and answers a lookup with one in-RAM hit plus one <span style="color:#8aff8a"><strong>ranged read</strong></span>.

<img src="../assets/word-dictionary/final-map.svg" alt="The complete picture. Read path: a user goes through a load balancer to an API server fleet, each holding the index in RAM (about 2.6 MB). On boot a server (1) reads meta.json to learn the current version, (2) loads that version's index on boot, and (3) does a ranged read of the data per lookup from S3, the durable store. S3 holds meta.json (current points to 002/), the 002/ current snapshot as one portable file of header, index, and data, and the 001/ previous snapshot which is never overwritten. Weekly rebuild: a weekly changelog of sorted updates plus the current dictionary pulled from S3 feed a merge worker that merges two sorted lists in O(n) into a new snapshot, then publishes 002/ and flips meta.json. Legend: white is user request, blue is control for version and boot load, green is read for ranged data fetch, pink is the weekly write for merge and publish." width="1000">

The weekly <span style="color:#ff8bd2"><strong>changelog</strong></span> folds in as an O(n) merge of two sorted lists, and we learned the hard lesson that you can't overwrite an indexed file under a live reader — so each build lands at a <span style="color:#93c5fd"><strong>new versioned path</strong></span> and a <span style="color:#93c5fd"><strong>`meta.json`</strong></span> pointer flips atomically, with a fixed-length <span style="color:#ffff99"><strong>header</strong></span> making each version one self-describing, portable file. That same shape — bulk on object storage, an index to query it — is exactly how real systems tier cold data onto S3 without losing the ability to query it.

> **Memory hook:** *a dictionary with no database is just a storage engine you build yourself: index in RAM, data on S3, weekly merges published as immutable versioned snapshots behind an atomic `meta.json` flip.*
