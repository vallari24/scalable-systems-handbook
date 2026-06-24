# Designing Remote File Sync: Chunking, Content Hashing, and Resumable Uploads

This post builds the engine behind a sync service like **Dropbox or Google Drive**: you have a file on your machine, and you want it to land in remote storage durably and completely, even though the network between you and that storage is slow, flaky, and prone to dying halfway through a 2 GB upload. The naive version ("`PUT` the whole file") works until the Wi-Fi drops at 95%, and then it makes you start over. The whole post is about the small set of ideas that turn that fragile transfer into one you can interrupt, resume, deduplicate, and version — without the client having to remember fragile upload progress.

**Question: a user drops a 14 MB `video.avi` into their synced folder. The upload reaches 12 MB and the connection dies. When it comes back, how much should we re-send — and who is responsible for knowing what already made it across?** The tempting answer is "the client remembers how far it got and resumes from there." That works until the client crashes, or the user opens the same file on a second laptop, or two devices edit it at once. The better answer flips the responsibility: **the server is the source of truth for which bytes already exist**, the file is broken into independently-addressable pieces, and the client simply *asks* the server what's missing before sending anything. The client holds no fragile upload-progress state at all.

This post sits next to a few we've already built. We pushed bytes straight into [S3](20-high-throughput-system-s3.md) with a pre-signed URL in the [Instagram upload post](09-social-network-instagram.md), and we'll lean on blob storage the same way here. The new ideas are three, and they're worth stating up front because the rest of the post is just earning them:

> **Memory hook:** *the three pillars of file sync — (1) **resumable** uploads and downloads, achieved by chunking; (2) a core intuition built entirely around **identifying what changed**; and (3) **multi-versioning**, so every save is a new version, never a destructive overwrite.*

We start with **Part 1**, which builds the upload path end to end — chunking, hashing, the two metadata tables, the commit handshake, and how a tiny edit syncs cheaply. **Part 2** then pushes changes back down to every device. Conflict handling and the sync daemon come later.

---

## Section 1 — The naive upload, and why it betrays you

**Question: what's wrong with just `PUT`-ting the whole file in one request?**

Nothing — until something interrupts it. Picture the 14 MB `video.avi` going up as one HTTP request. At 12 MB the connection drops. The request failed, so the server has *nothing* (a half-written object is no object), and the client's only move is to start the entire 14 MB again. On a phone on cellular, uploading a few hundred MB, this is the difference between "syncs in the background" and "never finishes."

Two things are broken here, and they're separable:

- **Granularity.** The unit of work is the whole file. A failure anywhere throws away *all* progress. We want a failure to cost us one small piece, not everything.
- **Responsibility.** If we tried to fix resumption by having the client remember "I got to byte 12,582,912," we'd be trusting the most fragile, least-available part of the system — a client that can crash, lose its database, or be one of *several* devices syncing the same account — to hold the truth about server state.

Both problems point at the same fix. Make the unit of work small, and make the *server* — which is durable and shared — the place that knows what exists.

> **Memory hook:** *the naive whole-file `PUT` fails on two axes — granularity (one failure wastes everything) and responsibility (the fragile client should not be the source of truth for what's uploaded). Chunking fixes the first; a server-authoritative handshake fixes the second.*

---

## Section 2 — Chunking makes it resumable

**Question: how do you make an upload survive a dropped connection without re-sending everything?**

Break the file into fixed-size pieces and upload them independently. A 14 MB file splits into four chunks — 4 MB, 4 MB, 4 MB, and a final 2 MB. Each fixed-size chunk is called a <span style="color:#ff8bd2"><strong>block</strong></span>. Now a dropped connection costs you, at most, the *one block* that was in flight — everything already accepted stays accepted. The same idea runs in reverse for **downloads**: you fetch block by block (4, 4, 4, 2), and an interrupted download resumes at the next missing block instead of restarting.

This is the entire reason chunking exists: **the block, not the file, is the unit of transfer and the unit of retry.** A 14 MB upload that dies at 12 MB has already banked three blocks; only the fourth is repeated.

But chunking alone isn't enough. If a block is just "bytes 8 MB through 12 MB of `video.avi`," how does the server tell one block apart from another? How does it know whether it has *already seen* this exact block? For that, each block needs a stable identity — and that identity comes from its content.

> **Memory hook:** *chunk the file into fixed-size blocks (e.g. 4 MB). The block is the unit of transfer and retry, so an interrupted upload or download costs one block, not the whole file — that's what makes it resumable.*

---

## Section 3 — Content hashing gives each block a name

**Question: what's the most useful possible name for a block — one that lets the server instantly know if it already has those exact bytes?**

Name the block by a **hash of its content.** Pass each block through a hash function and you get a short, fixed-length identifier — `h1 = hash(block1)`, `h2 = hash(block2)`, and so on. A good hash has the property we need: identical bytes always produce the identical hash, and different bytes (in practice) never collide. So the hash *is* the block's identity.

<img src="../assets/remote-file-sync/chunking-and-hashing.svg" alt="Chunking and content hashing. A 14 MB file named video.avi is split into four fixed-size blocks: block 1 (4 MB), block 2 (4 MB), block 3 (4 MB), and block 4 (2 MB); each fixed-size chunk is called a block. Each block is passed through a hash function, producing the hashes h1, h2, h3, and h4 — a hash uniquely identifies a block. The file then becomes an ordered list of those block hashes, called the blocklist: video.avi maps to [h1, h2, h3, h4]. Blocks are stored in blob storage (S3), keyed by their own hash — content-addressed — at a path like s3://my-dropbox/<account_id>/<block_hash>. Because identical bytes produce an identical hash, identical blocks are stored only once." width="1180">

This unlocks two things at once.

First, **a file is now just an ordered list of block hashes.** We call that list the <span style="color:#8aff8a"><strong>blocklist</strong></span>: `video.avi → [h1, h2, h3, h4]`. The blocklist is a complete, compact recipe for reconstructing the file — fetch each block in order, concatenate, done. It's a few hundred bytes describing a 14 MB file.

Second, **the hash is a deduplication key for free.** Store each block in blob storage under a key derived from its hash — this is <span style="color:#93c5fd"><strong>content-addressed storage</strong></span>, a path shaped like `s3://my-dropbox/<account_id>/<block_hash>`. Two files that share a block (or the same file uploaded twice) reference the same hash and therefore the same stored object. The bytes are stored once.

> **Memory hook:** *hash every block; the hash is its identity. A file becomes an ordered list of hashes — the blocklist. Store blocks content-addressed (`s3://.../<account_id>/<block_hash>`) so identical bytes are stored once and the server can recognize a block it already has just by its hash.*

---

## Section 4 — Where the bytes live, and where the truth lives

**Question: do the blocks go in a database or in files? And what does the server need to remember about each file?**

These are two different storage problems, and conflating them is the classic mistake.

The **blocks themselves are large, opaque, immutable byte-blobs.** A relational database is the wrong home for 4 MB binary blobs — it's expensive, it bloats backups, and you gain nothing from putting bytes that you only ever fetch whole into a query engine. Blocks belong in **blob storage (S3)**, exactly the [object store we designed earlier](20-high-throughput-system-s3.md), addressed by hash.

The **metadata is small, structured, and queried constantly** — "what files does this account have?", "what's the blocklist for `/video.avi`?", "does this account already have block `h3`?". That belongs in a database. We split it into two tables, each answering one question.

<img src="../assets/remote-file-sync/two-databases.svg" alt="Two database tables for file sync. On the left, the Blocks DB answers 'does this block exist in this account, and where is it in object storage'. It has three columns: namespace_id (the account_id), hash (the block hash), and object_key (the S3 object pointer, such as s3://.../<hash>). On the right, the File Metadata DB is the logical view that says a file is a list of block hashes. Its columns are namespace_id (account_id), relative_path (for example /video.avi), blocklist (for example [h1, h2, h3, h4]), and version_id (monotonically increasing). A dashed arrow shows that each hash in a file's blocklist resolves to a row in the Blocks DB, which points to the bytes in S3. The metadata DB is the source of truth for what a file is; the Blocks DB is the source of truth for which byte blobs exist. To render the 'here are your files' UI you read only the File Metadata DB, touching no bytes." width="1180">

The <span style="color:#93c5fd"><strong>Blocks DB</strong></span> is the existence and object-location index. Keyed by `(namespace_id, hash)` — where namespace is the account — it answers exactly one question: *does this account already have a block with this hash, and where is it?* If yes, the row points to the bytes in S3; if no, the client must upload them. This one tiny table is what lets the server look at an incoming blocklist and say "I already have three of these four blocks."

The <span style="color:#ffff99"><strong>File Metadata DB</strong></span> is the logical view of a file: `namespace_id`, the `relative_path` (`/video.avi`), the `blocklist` (`[h1, h2, h3, h4]`), and a `version_id`. This is what you read to render the "here are your files" UI — no bytes are touched, you're just listing rows. And the `version_id` is the seed of multi-versioning; it's a <span style="color:#8aff8a">monotonically increasing</span> number per account, and it earns its own discussion later because it's what makes the whole scheme safe.

The division of labor is the thing to remember: **the File Metadata DB is the source of truth for what a file *is*; the Blocks DB is the source of truth for which byte blobs *exist*; S3 holds the bytes.**

> **Memory hook:** *blocks (big, opaque, immutable) → blob storage (S3), content-addressed. Metadata (small, structured, queried) → a DB, split in two: **Blocks DB** `(namespace, hash, object_key)` answers "does this block exist, and where is it?", and **File Metadata DB** `(namespace, relative_path, blocklist, version)` answers "what is this file?". Listing a user's files reads only metadata.*

---

## Section 5 — The upload handshake: commit first, ask what's missing

Now we can answer the original question — who tracks progress, and what happens on failure — and the answer is intentionally boring: **almost nothing is tracked, and retry is just doing the same thing again.**

**Question: how does the client upload `video.avi` without ever having to remember how far it got?**

It doesn't upload first and bookkeep second. It **commits first** — declares its intent to the metaserver — and lets the server tell it what's actually needed.

<img src="../assets/remote-file-sync/upload-flow.svg" alt="An upload sequence diagram with three lifelines: client, meta server, and blocks server. Step 1: the client sends commit /video.avi with the blocklist [h1, h2, h3, h4] to the meta server. The meta server checks the Blocks DB and finds none of them exist. Step 2: the meta server replies 'need h1, h2, h3, h4'. Step 3: the client sends store (h1,b1) and (h2,b2) to the blocks server, two blocks at a time. Step 4: the blocks server replies OK. Step 5: the client sends store (h3,b3) and (h4,b4). Step 6: the blocks server replies OK. Step 7: the client re-sends commit /video.avi with the same blocklist; now all blocks are present so the meta server saves the file. Step 8: the meta server replies OK — file saved as version 1. A note explains that if the connection dies after the first OK, the retried commit re-asks which blocks are needed, so already-stored blocks are never re-sent." width="1180">

Walk the handshake:

1. **Commit.** The client computes the blocklist locally and sends `commit /video.avi [h1, h2, h3, h4]` to the <span style="color:#93c5fd"><strong>metaserver</strong></span>. Note it sends the *recipe*, not the bytes.
2. **The server diffs against truth.** The metaserver checks each hash in the Blocks DB. For a brand-new file, none exist, so it replies `need h1, h2, h3, h4`. This response *is* the progress state — computed fresh from the server's own records, never trusted from the client.
3. **Upload the needed blocks.** The client sends the missing blocks to the <span style="color:#ffff99"><strong>blocks server</strong></span> (here, two at a time), waiting for an `OK` after each batch. Each accepted block is written to S3 first and then recorded in the Blocks DB.
4. **Re-commit.** The client fires *the exact same* `commit /video.avi [h1, h2, h3, h4]` again. This time every block exists, so the metaserver writes the File Metadata DB row and replies `OK` — the file is saved as version 1.

Now answer all the sub-questions this design quietly resolves:

- **"How much is uploaded — does the client track it?"** No. The Blocks DB tracks it implicitly. The client never persists a byte offset or a "blocks done" list; it just re-commits and re-asks.
- **"What happens on failure? What retry mechanism?"** The connection dies mid-upload? The client retries by re-committing. The server re-diffs and asks only for what's *still* missing — already-stored blocks are never re-sent. The whole flow is **idempotent**: committing a file whose blocks all exist just saves it; uploading a block that already exists is a no-op (same hash, same object).
- **"Do we need a complicated workflow engine for this?"** No — and that's the point. There's no saga, no state machine to coordinate. The protocol is *commit → diff → upload missing → re-commit*, and its correctness comes entirely from the server being the source of truth and every step being safely repeatable.

> **Memory hook:** *upload = commit the blocklist first, server diffs it against the Blocks DB and replies with the missing hashes, client uploads only those, then re-commits to save. The client persists no progress; the Blocks DB is the implicit ledger. Retry = re-commit. Everything is idempotent, so no workflow engine is needed.*

---

## Section 6 — When a file changes, only the diff moves

**Question: the user edits a few bytes in the middle of `video.avi`. How much has to travel to the server?**

For the simple in-place case, one block. This is where chunking-plus-hashing pays off.

An edit changes the bytes of whichever block(s) it touches. Re-chunk and re-hash the file, and the unchanged blocks produce the **same** hashes as before — only the touched block gets a new hash. Say the edit lands in block 3: the new blocklist is `[h1, h2, h3', h4]`, where `h3'` is the only newcomer.

<img src="../assets/remote-file-sync/change-and-versioning.svg" alt="What changes during a simple in-place file edit. Before the edit, the blocklist is [h1, h2, h3, h4]. After editing a few bytes, only block 3 differs, so the new blocklist is [h1, h2, h3', h4] — h1, h2, and h4 are unchanged and h3' is highlighted as new. A mini sequence between client and meta server follows: the client sends commit /video.avi [h1, h2, h3', h4]; the meta server replies 'need h3'' — only the changed block; the client uploads block b3' and re-commits; the meta server replies OK, saved as version 2. A note observes that only one 4 MB block crosses the wire, not the full 14 MB. At the bottom, the File Metadata DB gains a new row rather than overwriting: version 1 has namespace vallari_mehta, path /video.avi, blocklist [h1, h2, h3, h4]; version 2 has the same namespace and path but blocklist [h1, h2, h3', h4]." width="1180">

The handshake is identical to a fresh upload, and the server's diff does the rest: the client commits `[h1, h2, h3', h4]`, the metaserver checks the Blocks DB, finds `h1, h2, h4` already present and only `h3'` missing, and replies `need h3'`. The client uploads that single 4 MB block and re-commits. **One block crosses the wire, not the whole 14 MB file** — and we never had to send a separate "here is what changed" message. The client hashes the new file into a blocklist; the server compares those hashes against what it already stores. No byte-by-byte diff protocol is needed because the diff falls out of content-addressing.

One boundary to keep honest: this example assumes a small in-place edit that does not shift every later fixed-size boundary. If the user inserts or deletes bytes near the beginning of a file, fixed-size chunking can make many later blocks get new hashes. Production systems can reduce that boundary-shift cost with content-defined chunking or rolling hashes, but the core idea stays the same: compare content-derived block names, not byte offsets.

Now the `version_id` does its job. Saving the edit does **not** overwrite the old metadata row. It writes a **new row** with an incremented version: version 1 still points at `[h1, h2, h3, h4]`, and version 2 points at `[h1, h2, h3', h4]`. Both versions' blocks still exist in S3, so both versions remain fully reconstructable. A brand-new file simply creates its first row at version 1. This append-only history is <span style="color:#8aff8a"><strong>multi-versioning</strong></span> — the third pillar — and it's why a sync service can offer "restore previous version" and why a botched edit is never destructive. How versions are pruned, and how the monotonic counter coordinates across devices, comes later.

> **Memory hook:** *for an in-place edit, only the touched block's hash changes; the server diffs the new blocklist against the Blocks DB and asks only for that changed block, so no byte-by-byte diff is sent. Fixed-size insert/delete edits can shift later boundaries (content-defined chunking reduces that cost), and saving still appends a new version row instead of overwriting: that's multi-versioning.*

---

## Section 7 — The same handshake everywhere: Git, registries, and LinkedIn

**Question: is "commit first, let the server tell you what's missing" special to file sync — or did we just stumble onto something more general?**

It's general, and recognizing it is worth as much as the file-sync design itself. Strip away the file vocabulary and what's left is a pattern:

> **Declare the desired end state by its content-derived names; let the server diff that against what it already has; transfer only the gap.**

Two reusable ingredients make it work. **Content-addressing** (name a thing by the hash of its bytes) lets the server recognize what it already holds without the client describing it. **A monotonic version/cursor** lets the server answer "what changed since you last synced?" cheaply. Once you see this shape, it's everywhere.

<img src="../assets/remote-file-sync/commit-need-everywhere.svg" alt="The commit/need handshake generalized beyond files. A banner states the abstract pattern: declare the desired state by content-derived names, the server diffs it against what it already has, and only the gap is transferred. Four system cards follow. Git: the unit is an object (blob, tree, or commit) addressed by its SHA; the client declares the refs it wants to push, and the server replies with the set of missing objects — this is pack negotiation. Container registries (Docker): the unit is an image layer addressed by digest; the client pushes the manifest, and the registry asks only for the layer digests it does not already store. LinkedIn (highlighted): two uses — media dedup, where the same company logo, profile photo, or shared document is content-addressed and stored once across millions of users; and delta sync, where the mobile app holds a sync cursor and the server returns only what changed since that cursor. Backup and build caches: the unit is a chunk or action result addressed by hash; the client asks 'do you have this digest?' and uploads only the misses. At the bottom, a LinkedIn delta-sync sequence: a mobile client tells the feed service 'sync, I have cursor 42'; the service diffs and replies with items 43 through 57 plus a new cursor 57 — the same commit/need shape, with the version_id pillar generalized to a sync cursor." width="1180">

**Git** is the purest example. Every object — a file blob, a directory tree, a commit — is named by the SHA-1/SHA-256 of its content; a commit is, in effect, a versioned blocklist of trees and blobs. When you `git push`, the client and server run *exactly* our handshake: the client says "I want to advance these refs," the server diffs against the objects it already has, and only the missing objects travel in the pack. Unchanged files in a 10,000-file repo cost nothing to push because their object hashes already exist on the other side. Commit/need, by another name.

**Container registries** (Docker, OCI) do the same for image layers. Each layer is content-addressed by a digest, and `docker push` sends the manifest first — the layer list — so the registry can reply with only the digests it's missing. Pushing a new app version that changed one layer uploads one layer; the shared base-OS layers are already there. That's our "edit changes one block" story applied to images.

### Where it pays off inside LinkedIn

LinkedIn isn't a file-storage product, but the same two pillars solve several of its real problems:

- **Media deduplication.** A company logo, a popular shared slide deck, a profile background — the same bytes get uploaded by thousands of users. Content-address every uploaded media block and the identical bytes are stored **once**, no matter how many members reference them. The Blocks DB's "does this hash exist?" check is the whole mechanism.
- **Resumable uploads for large media.** Video posts and document attachments are large and often uploaded on mobile. Chunk-and-commit makes those uploads survive a dropped connection — the member's phone re-commits and re-sends only the missing blocks, exactly as in Section 5.
- **Delta sync to the mobile app — the version pillar's biggest payoff.** Your feed, your connection graph, your message threads: the app doesn't re-download them on every launch. It holds a **sync cursor** (a monotonic version, just like our `version_id`) and tells the server "I'm at cursor 42." The server diffs and returns *only* what changed since cursor 42, plus the new cursor. This is commit/need with the roles softened — the client declares where it is, the server computes the gap — and it's why the app reopens instantly on a train with one bar of signal.
- **Messaging attachments.** A deck forwarded into fifty conversations is one set of content-addressed blocks referenced fifty times, not fifty copies.

**Backup tools and build caches** round out the pattern. Restic, Borg, and Time Machine content-chunk your disk and back up only chunks they haven't seen — incremental backups for free. CI systems (Bazel's remote cache, layer caches) key build outputs by the hash of their inputs and ask "do you already have this action result?" before recomputing or uploading. Same question, same savings.

| System | The "block" (content-addressed unit) | "commit" = declare | "need" = server's diff |
| --- | --- | --- | --- |
| **File sync** (this post) | 4 MB file block, by hash | commit the blocklist | missing block hashes |
| **Git** | object (blob/tree/commit), by SHA | push refs | missing objects (pack negotiation) |
| **Docker / OCI** | image layer, by digest | push the manifest | absent layer digests |
| **LinkedIn media** | media block, by hash | upload referencing hashes | blocks not yet stored |
| **LinkedIn feed/messages** | item, by monotonic cursor | "I'm at cursor 42" | items since 42 + new cursor |
| **Backup / CI cache** | chunk / action result, by hash | "store this snapshot" | chunks/results not seen |

> **Memory hook:** *commit/need + content-addressing is a general pattern: name by content, declare the desired state, server transfers only the gap. Git push (objects by SHA), Docker push (layers by digest), LinkedIn (media dedup, resumable uploads, and feed/message **delta sync** where the version_id becomes a sync cursor), and backup/CI caches are all the same handshake. The version pillar generalizes to "what changed since cursor N?"*

---

## Where this leaves us (Part 1)

We started from a fragile whole-file `PUT` that wasted all progress on any failure and forced the client to bookkeep, and we replaced it with four ideas that compose cleanly:

- **Chunk** the file into fixed-size blocks so transfer and retry happen one small piece at a time — that's what makes uploads and downloads resumable.
- **Hash** each block so its content *is* its name, giving us a compact blocklist recipe and free deduplication.
- **Split storage** so big opaque blocks live content-addressed in S3, while two small tables — the Blocks DB ("does this block exist, and where is it?") and the File Metadata DB ("what is this file?") — hold the truth.
- **Commit-then-diff**, so the server (not the client) tracks what exists; uploads send only missing blocks, an edit moves only the changed block, retry is just re-committing, and every save appends a new version instead of overwriting.

The whole design rests on one inversion of responsibility: **the durable, shared server is the source of truth for which blocks exist, and the client merely asks.** That single decision is what dissolves the hard problems — progress tracking, resumption, deduplication, and "what changed" — into a short, idempotent handshake with no workflow engine in sight.

> **Memory hook:** *file sync = chunk → hash → blocklist → commit/diff handshake, with blocks in S3 and truth in two metadata tables. The server is the source of truth; the client only asks what's missing. Resumability, dedup, cheap edits, and versioning all fall out of content-addressing plus a server-authoritative commit.*

---

# Part 2 — Pushing changes back down to every device

Part 1 got bytes *up*: the client commits a blocklist, the server says what's missing, only the gap travels. But sync is bidirectional. Once `/video.avi` reaches version 2 from your laptop, your phone — which still shows version 1 — has to find out and catch up. That raises three questions, and we'll take them in the order the design demands:

1. **What's the data structure underneath all of this?** It turns out the File Metadata DB has been a *log* all along, and seeing that is the foundation for everything else.
2. **How does a device learn something changed?** Push or pull — and the answer is dictated by one stubborn fact about clients.
3. **How does multi-versioning let us go *backward*** — restore an old version — and why is that nearly free?

---

## Section 8 — The version log: the foundation (and it looks oddly like Kafka)

**Question: in the File Metadata DB, is `version_id` a number *per file*, or something bigger?**

Bigger — and this is the insight the rest of Part 2 hangs on. The `version_id` is a single **monotonically increasing counter per namespace** (per account). *Every* update to *any* file in the namespace bumps the same counter and appends a row. So the File Metadata DB isn't really a table you overwrite — it's an **append-only log**, ordered by version, of "at version N, file X became this blocklist."

A delete is just another row in the same log. If the user deletes `/photo.jpg`, the metaserver appends version 7 for that path with a <span style="color:#ff8a8a"><strong>tombstone</strong></span> marker like `deleted = true` instead of a blocklist. A client at version 5 later asks "anything after 5?", receives the delete row, removes the local file, and advances its cursor. The old blocks are not deleted immediately; version history and garbage collection decide later when no retained version needs them.

<img src="../assets/remote-file-sync/version-log.svg" alt="The version log: one monotonically increasing version_id per namespace. The File Metadata DB is shown as an append-only log with columns version, namespace, relative_path, and blocklist, all in the namespace vallari_mehta. Version 1 sets /video.avi to [h1, h2, h3, h4]. Version 2 sets /video.avi to [h1, h2, h3', h4] (block 3 changed). Version 3 sets /photo.jpg to [h6]. Version 4 sets /notes.txt to [h2, h8]. Version 5 sets /photo.jpg to [h6']. Version 6 sets /video.avi back to [h1, h2, h3, h4] — the same blocklist as version 1, a revert, which is just another commit. Every update to any file appends a row and the version_id is the offset, exactly like a Kafka log. A callout notes it looks oddly like Kafka: a consumer keeps an offset, and here each device keeps a version cursor. Two device cursors are shown: a mobile device at cursor v2 and a Mac at cursor v5. An example query: the mobile device asks 'namespace vallari_mehta, I'm at version 2, anything after?' and the server returns rows 3, 4, 5, 6 — the device replays the log forward from its committed offset. A final note explains why a version number is used instead of a timestamp: clocks drift, jump on NTP sync, and can be changed by the client, so a server-assigned monotonic version is the single tamper-proof ordering truth." width="1180">

Once you see it as a log, the **Kafka** resemblance is direct, and it's the right mental model. In Kafka, a topic is an ordered log and each consumer remembers an **offset** — "I've read up to position N, give me everything after." Our version log is the topic; the `version_id` is the offset. A client that synced up to version 2 just says *"I'm at 2 — what's after?"* and replays the log forward. This is the same log-structured idea behind the [LSM-tree storage engine](22-high-throughput-lsm-trees.md) elsewhere in this handbook: **an ordered, append-only sequence is a natural structure to catch up against.**

This also answers a tempting wrong turn. **Question: why not just track a *timestamp* — "give me everything modified after 3:42 PM"?** Because clocks are weak ordering sources. Two metaservers can disagree by milliseconds; NTP can nudge a clock backward mid-day; a client can set its clock to last Tuesday. A **server-assigned monotonic version** sidesteps all of it: there is exactly one counter, it only ever goes up, and "after version 5" means the same thing on every device forever. (It's the same reason distributed systems lean on monotonic [ID generators](08-distributed-id-generators.md) rather than wall-clock time whenever they need a dependable order.)

> **Memory hook:** *`version_id` is one monotonic counter per namespace, not per file. That gives every client one cursor: "I have applied version 42; send me everything after 42." Create, edit, restore, and delete all append rows, so the client catches up across all files in order. Use a server counter, not a timestamp — clocks drift, jump, and can be tampered with.*

---

## Section 9 — How a client learns what changed: pull, not push

**Question: when `/video.avi` becomes version 6, how does your phone — sitting in your pocket — find out?**

There are two shapes, and the choice isn't a toss-up.

- **Push.** The server proactively notifies the device over a live connection (WebSocket): "something changed, here it is." Low latency when it works — but it rests on the device being **online and reachable** at the moment of the change. A phone in a pocket, a laptop with its lid shut, a machine behind a flaky network: all unreachable. To push reliably you'd have to buffer per-device backlogs and handle every reconnection, and you'd *still* need a catch-up path for the device that was offline. Push can't be the backbone.
- **Pull.** Each device **periodically asks** "I'm at version N — anything after?" This works no matter how long the device was dark. Offline for a week? On reconnect it asks once and the log hands back everything it missed in order. The mechanism that handles the *steady state* is the same one that handles *catching up* — there's no separate path.

So the backbone is **pull**, and the reason is a single stubborn fact: **devices go offline, and you cannot push to a device that isn't there.** Each device stores exactly one piece of sync state — **its own cursor**, the last version it has fully applied. Nothing more. Your phone might sit at version 2 while your Mac is at version 5; they're just two independent consumers reading the same log at their own pace, exactly like two Kafka consumers with independent offsets.

Here's the sync loop for a device sitting at version 5:

<img src="../assets/remote-file-sync/pull-sync.svg" alt="Syncing down: a device pulls only what changed. A sequence diagram with three lifelines — Mac at cursor v5, meta server, and blocks server. Step 1: the Mac asks the meta server 'changed in vallari_mehta after v5?' and the server reads the log past offset 5. Step 2: the meta server replies 'v6: /video.avi maps to [h1, h2, h3, h4]'. Step 3: locally the Mac determines it already has h1, h2, and h4, so only h3 is missing. Step 4: the Mac asks the blocks server to fetch h3. Step 5: the blocks server returns block b3. Step 6: the Mac reconstructs /video.avi from [h1, h2, h3, h4] and advances its cursor to v6. A note emphasizes pull, not push: a device can be offline for a week and still catch up from its cursor, and a WebSocket push is only a 'pull now' nudge layered on top, never the source of correctness. A second note observes that v6 is a revert to v1's blocklist, so syncing a restore looks identical to syncing any edit." width="1180">

1. **Ask from the cursor.** The Mac asks the metaserver: *"namespace `vallari_mehta`, I'm at version 5 — what's after?"*
2. **Server replays the log.** It returns the rows past offset 5 — here, version 6: `/video.avi → [h1, h2, h3, h4]`.
3. **Diff locally by hash.** The Mac looks at the new blocklist and checks which hashes it doesn't already have on disk. It still has `h1, h2, h4` from before; only `h3` is missing. **This is the commit/need trick from Part 1, run in the download direction** — diff by content hash, fetch only the gap.
4. **Fetch only the gap.** It pulls just block `h3` from the blocks server.
5. **Reconstruct and advance.** It rebuilds `/video.avi` from `[h1, h2, h3, h4]`, writes it to disk, and **advances its cursor to 6.**

Push still has a place — as an *optimization*, not the foundation. A WebSocket can send a tiny "there's something new, pull now" nudge to shrink latency for online devices. But correctness lives entirely in the pull loop, so an offline device loses nothing but freshness.

> **Memory hook:** *learning what changed is **pull-based**, because you can't push to an offline device. Each device stores only its own **cursor** (last applied version) and periodically asks "anything after N?"; the same loop serves both steady-state sync and week-long catch-up. It then diffs the new blocklist by hash and downloads only missing blocks — commit/need in reverse. Push over WebSocket is just a low-latency "pull now" nudge on top.*

---

## Section 10 — Multi-versioning, and why going *backward* is nearly free

**Question: the user wants `/video.avi` back the way it was at version 1. How much work is that?**

Almost none. Going backward is not a special "copy the old file back" operation. It is just another version row.

A version row is a small recipe: "for this path, the file is this ordered blocklist." Because old version rows are kept, version 1 still says `/video.avi → [h1, h2, h3, h4]` even after version 2 changes the file to `[h1, h2, h3', h4]`.

So restore is simple:

```text
v1: /video.avi -> [h1, h2, h3,  h4]
v2: /video.avi -> [h1, h2, h3', h4]

restore to v1:
v6: /video.avi -> [h1, h2, h3,  h4]   # new row, old recipe
```

That new `v6` row is important. It is how every other device learns something changed. A phone sitting at cursor 5 asks, "anything after 5?", receives `v6`, and now knows `/video.avi` should point back to `[h1, h2, h3, h4]`.

The phone still does not download the whole file. It compares the target blocklist with the blocks it already has locally. If it currently has version 2, it already has `h1`, `h2`, `h3'`, and `h4`. To reconstruct version 1, it only needs the original `h3`. So it fetches `h3`, reassembles `[h1, h2, h3, h4]`, and advances its cursor to 6.

<img src="../assets/remote-file-sync/multi-version-pool.svg" alt="Multi-versioning shown as a namespace-version log. A table lists six rows: v1 /video.avi maps to [h1, h2, h3, h4]; v2 /video.avi maps to [h1, h2, h3', h4]; v3 /photo.jpg maps to [h6]; v4 /notes.txt maps to [h2, h8]; v5 /photo.jpg maps to [h6']; and v6 /video.avi maps back to [h1, h2, h3, h4]. The v6 row is highlighted as a restore: it is a new row with an old recipe and requires zero uploads. Below the table, a client catch-up box shows a client at cursor v5 asking for anything after 5, receiving v6, fetching only missing h3, and advancing to v6. A storage rule box says version rows are small recipes over shared blocks; the edit stores h3', the restore stores no new bytes, and garbage collection deletes blocks only when no retained version references them." width="1180">

The storage cost follows the same rule. The first upload stores four blocks. The edit stores only `h3'`. The restore stores no new bytes if the old `h3` is still retained; it only appends the new version row. Later, a garbage collector can delete blocks that no retained version references anymore.

This is why **Google Docs, Dropbox, and friends offer deep version history so cheaply.** They are not storing a full copy per save; they store one small recipe per version over a pool of shared, deduplicated blocks.

> **Memory hook:** *a restore/revert is a new version row, not a rewrite. That row tells every client "this path changed," and the client downloads only the blocks it does not already have. Version history is cheap because versions are recipes over shared content-addressed blocks, not full file copies.*

---

## Section 11 — Anywhere you need "updates"

Section 7 showed the commit/need *handshake* recurring in Git, registries, and LinkedIn. The Part 2 machinery — **an append-only version log plus a per-consumer cursor** — recurs just as widely, because it's the general answer to *"catch me up on what changed since I last looked."* A few of the apps you use every day:

- **Google Drive** — multi-versioning is this design almost verbatim: file revision history, "restore previous version," and per-device sync are exactly the version log plus device cursors.
- **WhatsApp and other messaging** — keeping track of which messages a device has is log-shaped storage on the DB side: each message is a log entry, and a device that's been offline says "I have up to message N, send the rest." The per-chat sequence number *is* the cursor; delivering missed messages *is* replaying the log forward.
- **Slack, Teams, and similar** — "catch up on the channels you missed while you were away" is the same log-and-cursor replay, per channel.

For *live collaborative editing* — two people typing in the same paragraph at once — the basic log isn't enough on its own, and that's where the heavier cousins come in: **Differential Synchronization** and **Operational Transformation** (and CRDTs) reconcile concurrent fine-grained edits rather than whole-file versions. They're a Part 3 topic. But notice the backbone underneath them is unchanged: *anywhere we need to ship "updates" to clients that drift in and out, the shape is an append-only log of changes plus a cursor that says how far each consumer has read.*

> **Memory hook:** *the version-log + cursor pattern is everywhere "updates" must reach intermittent clients: Google Drive version history, WhatsApp/Slack/Teams message catch-up (a per-chat sequence number is the cursor, missed-message delivery is log replay). Live co-editing needs more — Differential Synchronization / Operational Transformation / CRDTs — but they sit on the same log-and-cursor backbone.*

---

## Where this leaves us (Part 2)

Part 1 made uploads resumable and edits cheap by inverting responsibility onto the server. Part 2 turned the single idea that made that work — content-addressing — into a *bidirectional* sync engine by noticing the metadata store was a log all along:

- The File Metadata DB is an **append-only version log**, one monotonic counter per namespace — a Kafka-like topic, ordered by a counter rather than a fragile timestamp.
- Devices learn what changed by **pulling from their own cursor**, because you can't push to a device that's offline; the same loop handles steady-state sync and week-long catch-up, and it reuses the diff-by-hash trick to download only missing blocks.
- **Multi-versioning makes restore nearly free** — every version is a small recipe over shared blocks, and a revert is just another commit that flows down the ordinary pull loop.

The through-line across both parts is one decision and its consequences: **name things by their content, keep the truth in a server-side append-only log, and let clients converge by asking "what's missing / what's after my cursor?"** Upload, download, edit, restore, and multi-device sync all collapse into that one question asked in different directions.

> **Memory hook:** *Part 2 = the metadata store is an append-only **version log** (one monotonic counter per namespace = Kafka offset); devices **pull from a cursor** because offline devices can't be pushed to; **restore is free** because versions are recipes over shared content-addressed blocks. One idea across both parts: name by content, keep truth in a server-side log, let clients ask "what's after my cursor?"*

*Part 3 will tackle concurrency head-on: what happens when two devices commit different edits to the same file at the same version — conflict detection, resolution, and the collaborative-editing techniques (Differential Synchronization, Operational Transformation, CRDTs) named above.*
