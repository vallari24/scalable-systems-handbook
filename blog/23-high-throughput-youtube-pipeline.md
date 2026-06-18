# Designing YouTube's Video Pipeline: Orchestrating Work with Airflow

This post builds the **processing pipeline** behind a video platform like YouTube: the machinery that takes a freshly uploaded video and turns it into something watchable, safe, and discoverable — transcoded into multiple resolutions, copyright-checked, scanned for nudity, captioned, and given a thumbnail — before (and after) it's allowed to go live. Upload itself is the easy part; we've already built it. The hard part is that "process this video" is not one job but **a dozen jobs with tangled dependencies**, some of which gate publishing and some of which don't. The whole post is about how you *coordinate* that graph of work — and why the obvious answer (a queue like Kafka) is the wrong tool, while a **workflow management tool** (Apache Airflow) is the right one.

**Question: a creator uploads a video. Before it can appear on the site you must transcode it to several resolutions, run an expensive copyright scan, run a nudity scan, generate captions, and make a thumbnail — and these steps depend on each other in awkward ways (you can't copyright-check a file you haven't transcoded yet, and you don't want to block publishing on the slow 4K transcode). Some steps must finish *before* the video goes live; others can finish *after*. How do you model and run this graph so that the right things block, the slow things don't, failures retry cleanly, and you can answer "where is video X stuck?" at 3 a.m.?** The honest answer is not "throw it on a queue and wire up consumers." It's that this is a **dependency graph**, not a stream — and the tool built for dependency graphs is a workflow orchestrator built around a **DAG**.

This post sits in a small arc. We built [Instagram's media upload](09-social-network-instagram.md) and learned to push bytes straight to [S3](20-high-throughput-system-s3.md) with a pre-signed URL; we built [ETL and Change Data Capture](16-storage-engine-etl-cdc.md) on [Kafka](16-storage-engine-etl-cdc.md) and learned what an event stream is *good* at. This post is where we learn what a stream is *bad* at — fan-in, dependencies, "do E only after C and D both finish" — and reach for orchestration instead.

> **Memory hook:** *a video pipeline isn't a stream of events, it's a graph of dependent jobs. Kafka moves events forward; it has no idea when a *set* of jobs has all finished. A workflow tool (Airflow) models the graph as a DAG, tracks every task's state, and fires the next task only when its dependencies are done.*

---

## The brief: three stages, one hard one

**Question: before drawing anything — what are the big pieces of a video platform, and which one is actually difficult?**

<img src="../assets/youtube-pipeline/pipeline-overview.svg" alt="The three stages of a video platform, drawn left to right as a pipeline. Stage 1, UPLOAD (pink): a creator pushes the raw video file straight into S3 via a pre-signed URL, exactly like the Instagram photo-upload flow — the API server never touches the bytes. Stage 2, PROCESSING (blue, boxed and highlighted as 'the hard part'): once the raw file lands in S3, a graph of dependent jobs runs — transcode to multiple resolutions, copyright check, nudity check, auto-captioning, thumbnail generation — and a subset of those jobs gates whether the video may be published. Stage 3, DISTRIBUTION (green): the finished renditions are served to viewers through a CDN, the same read-heavy delivery problem as Instagram photo delivery. A caption underneath: upload and distribution are solved problems we've built before; this post is about orchestrating the middle stage, where a dozen jobs with awkward dependencies must run in the right order with the right things blocking." width="1100">

A video platform is three stages:

- <span style="color:#ff8bd2"><strong>Upload</strong></span> — get the raw bytes from the creator into durable storage. This is the [Instagram upload problem](09-social-network-instagram.md) almost exactly: a pre-signed URL, bytes go straight to [S3](20-high-throughput-system-s3.md), the API server never relays the file.
- <span style="color:#93c5fd"><strong>Processing</strong></span> — turn that one raw file into everything the platform needs: multiple resolutions, safety checks, captions, a thumbnail. **This is the hard part, and the whole post.**
- <span style="color:#8aff8a"><strong>Distribution</strong></span> — serve the finished renditions to millions of viewers through a CDN. This is the [read-heavy delivery problem](12-social-network-image-optimization-and-tagging.md) from the image posts, scaled to video.

Upload and distribution we've essentially built before. So this post spends one short section on upload (because it really is just Instagram), and the rest on the orchestration of stage 2 — the part nobody draws correctly the first time.

> **Memory hook:** *upload = Instagram-to-S3; distribution = CDN read path; processing = a dependency graph of jobs. Only the middle one is new, and it's where the design lives.*

---

## Section 1 — Upload: it's just the Instagram flow

**Question: a video is bigger than a photo, but is the upload *architecturally* different?**

No. A typical user upload is a single small-to-medium file — a phone clip, a few hundred MB — not a multi-hour 4K master. So we use the exact pattern from the [Instagram post](09-social-network-instagram.md): the client asks our API for a <span style="color:#ffff99"><strong>pre-signed URL</strong></span>, then `PUT`s the raw bytes **directly to S3**. The bytes never flow through our servers, so we don't pay double bandwidth or melt an upload tier relaying gigabytes.

We deliberately **do not chunk** the typical upload. Chunking matters for huge files and flaky networks (resumable multipart upload), but for the common case — a small video over a decent connection — a single `PUT` is simpler and good enough. (If you later need resumable uploads for 90-minute 4K masters, S3 multipart upload is the lever; it doesn't change anything downstream.)

The one new wrinkle is **what happens the instant the raw file lands.** For a photo you might just store it. For a video, the landing of the raw object in S3 is the **trigger** for an entire processing pipeline — and that pipeline is what saves us bandwidth and compute, because we process the file *once*, centrally, instead of asking every viewer's device to deal with a 2 GB raw upload. The raw file in S3 is the input; everything after this is stage 2.

> **Memory hook:** *video upload = pre-signed URL, bytes straight to S3, no chunking for the common case — identical to Instagram. The new thing is that the raw object landing in S3 kicks off the processing graph.*

---

## Section 2 — Transcoding: make one arbitrary upload playable everywhere

**Question: the creator's original file already plays on their phone. Why can't we store it and serve that same file to every viewer?**

Suppose an iPhone uploads `clip.mov`: a 4K HEVC video stream, an AAC audio stream, and some metadata packaged together. The creator's phone can decode that exact combination. A browser, an older Android phone, or a low-power TV may not. Even a compatible device may be on a 3 Mbps cellular connection that cannot keep up with the source file's bitrate.

So the platform cannot make the uploader's recording choices every viewer's minimum requirements. It must turn one arbitrary source into a small, controlled set of outputs that cover different devices and network conditions. That conversion is <span style="color:#93c5fd"><strong>transcoding</strong></span>.

### Container, codec, resolution, and bitrate are different knobs

A video file is not one undifferentiated blob. Its container can carry a video stream, one or more audio streams, subtitles, timing information, and metadata.

| Term | What it controls | Examples |
| --- | --- | --- |
| **Container** | Packages the streams and keeps them synchronized | MOV, MP4, WebM, AVI |
| **Video codec** | Compresses and decodes the video stream | H.264/AVC, HEVC/H.265, VP9, AV1 |
| **Audio codec** | Compresses and decodes the audio stream | AAC, Opus |
| **Resolution** | Number of pixels in each frame | 360p, 480p, 720p, 1080p, 4K |
| **Bitrate** | Approximate number of bits delivered per second | 800 Kbps, 2.5 Mbps, 8 Mbps |

The `.mov` or `.mp4` extension names the **container**, not the video codec inside it. MOV is not universally unplayable on Android, and MP4 is not automatically compatible everywhere. Playback depends on whether the client supports the complete container–codec combination, often with a hardware decoder.

Resolution and bitrate are related, but they are not the same thing. Two 720p files can use different bitrates and look very different. Raising bitrate usually preserves more detail, but it also consumes more storage, network bandwidth, and mobile-radio energy. Resolution, frame rate, codec complexity, and hardware support drive most of the decoding CPU and battery cost. High-quality renditions tend to raise several of those costs together.

### FFmpeg turns the source into a rendition ladder

A transcoding worker reads the source container, decodes its audio and video streams, scales the video, and encodes new outputs with combinations the platform has chosen to support. [FFmpeg](https://ffmpeg.org/) is a widely used open-source tool for doing this work on a server.

For example, a worker can create a 360p H.264/AAC rendition with a command shaped like this:

```bash
ffmpeg -i upload.mov \
  -vf "scale=-2:360" \
  -c:v libx264 -b:v 800k \
  -c:a aac -b:a 96k \
  output/360p/video.mp4
```

FFmpeg can also select individual streams: `-vn` creates an audio-only output, while `-an` creates a video-only output. If we only move already-encoded streams into a different container with `-c copy`, that cheaper operation is **remuxing**. Transcoding means decoding and re-encoding at least one stream.

The real pipeline runs several encode jobs and produces a <span style="color:#93c5fd"><strong>rendition ladder</strong></span>: perhaps 360p at a low bitrate, 720p at a medium bitrate, and 1080p at a high bitrate. It does not blindly create every resolution. There is no value in upscaling a 480p upload to 4K, and every extra rung costs compute and storage.

<img src="../assets/youtube-pipeline/transcoding-ladder.svg" alt="How one arbitrary video upload becomes an adaptive streaming ladder. A creator uploads a MOV container holding a 4K HEVC video stream, AAC audio, and subtitle or metadata streams. An FFmpeg worker decodes the source, scales it, re-encodes target codec and bitrate combinations, and packages the outputs into segments. S3 stores a master manifest plus separate 360p, 720p, and 1080p folders, each with a rendition manifest and sequential media segments. A player fetches the manifest, estimates network throughput, buffer, and device capability, then requests the next segment from the appropriate rendition. It can switch from 1080p to 360p at a segment boundary before its buffer empties." width="1280">

### Segment the ladder so the player can adapt

The platform does not usually serve each rendition as one giant file. It packages each rendition for **HLS or DASH**, splitting the timeline into short, aligned media segments:

```text
video-123/
  master.m3u8
  360p/
    index.m3u8
    0001.m4s
    0002.m4s
  720p/
    index.m3u8
    0001.m4s
    0002.m4s
  1080p/
    index.m3u8
    0001.m4s
    0002.m4s
```

The **master manifest** tells the client which renditions, codecs, and bitrates exist. Each rendition manifest lists its ordered segments. The player downloads a few segments ahead into a buffer, measures throughput, and chooses the next segment from the highest rendition it believes can arrive before the buffer empties.

If Wi-Fi becomes weak, the player can request segment `0042` from the 360p folder instead of the 1080p folder. When bandwidth recovers, it switches back up at a later segment boundary. This is <span style="color:#8aff8a"><strong>adaptive bitrate streaming</strong></span>: preserve continuous playback first, then maximize quality.

For a deeper treatment of containers, codecs, manifests, HLS/DASH, and adaptive playback, [HowVideo.works](https://howvideo.works/) is an excellent visual resource.

At scale, we still avoid unlimited work. We eagerly generate the low rendition needed for publishing, generate common HD renditions asynchronously, and may defer rare or unusually expensive outputs. CDN caching is naturally demand-driven too: edge locations cache only the manifests and segments viewers actually request.

> **Memory hook:** *the container packages streams; codecs compress them; transcoding creates a controlled resolution-and-bitrate ladder; manifests and segments let the player switch rungs without stopping playback.*

---

## Section 3 — What "processing" actually means: a fan-out of jobs

**Question: the raw file is in S3. What has to happen to it before it's a real YouTube video — and are these one job or many?**

Many. The single raw upload fans out into a handful of *different kinds* of work, each producing a different artifact:

<img src="../assets/youtube-pipeline/processing-fanout.svg" alt="The fan-out of processing jobs from one raw uploaded video. Center-left: a pink box 'raw video in S3' is the single input. Five arrows fan out to five job groups. One, TRANSCODING (blue): generate multiple resolution renditions — 360p, 480p, 720p, 1080p — each a separate encode of the source; these are the playable files. Two, COPYRIGHT CHECK (yellow, marked 'expensive'): fingerprint the audio/video and match against a rights database (Content ID) — flagged as the most compute-heavy and slowest job. Three, NUDITY / SAFETY CHECK (yellow): an ML classifier scans frames for disallowed content. Four, AUTO-CAPTIONING (blue): speech-to-text generates subtitle tracks. Five, THUMBNAIL (blue): either extract candidate frames from the video, or accept a custom image the creator uploaded. A caption: one upload becomes many artifacts produced by many jobs — and crucially, these jobs are not independent and are not equally urgent, which is what the next sections untangle." width="1120">

- <span style="color:#93c5fd"><strong>Transcoding</strong></span> — the source has one container, codec, resolution, and bitrate combination; viewers need a controlled compatibility ladder. We generate renditions such as <span style="color:#93c5fd"><strong>360p, 480p, 720p, 1080p</strong></span> (and up). Each rendition is its own encode job, and higher resolutions take dramatically longer.
- <span style="color:#ffff99"><strong>Copyright check</strong></span> — fingerprint the audio and video and match it against a rights database (this is YouTube's *Content ID*). This is the **most expensive** job: heavy compute, slow, and it can block monetization or publishing entirely.
- <span style="color:#ffff99"><strong>Nudity / safety check</strong></span> — an ML classifier scans frames for disallowed content. Also gating: you cannot publish unsafe content.
- <span style="color:#93c5fd"><strong>Auto-captioning</strong></span> — speech-to-text produces subtitle tracks. Nice to have, not safety-critical.
- <span style="color:#93c5fd"><strong>Thumbnail</strong></span> — either **extract** candidate frames from the video automatically, or accept a **custom** image the creator uploaded.

Stare at this list and two facts jump out, and they drive the entire rest of the design:

1. **These jobs are not independent.** You can't run a copyright check on a rendition that doesn't exist yet. Captioning needs decoded audio. The graph has *edges*.
2. **These jobs are not equally urgent.** A safety check *must* finish before the video is public. The 4K transcode and the caption track absolutely do not — they can land minutes later. So some jobs **gate publishing** and some don't.

The naïve instinct is "run all five in parallel, wait for all of them, then publish." That's wrong on both counts: it blocks publishing on the slowest non-critical job, and it ignores the dependencies. Let's fix the *what-blocks-what* question first, then the *how-do-we-run-it* question.

> **Memory hook:** *one upload fans out into transcoding (360/480/720/1080), copyright, nudity, captioning, thumbnail. Two truths: the jobs depend on each other (you can't check a rendition that doesn't exist), and they're not equally urgent (safety gates publish; 4K and captions don't).*

---

## Section 4 — The publish decision: what blocks, what doesn't

**Question: the creator hits "publish." What is the *minimum* set of jobs that must be finished before the video can legally and safely go live — and what should we let finish in the background afterward?**

This is a **product decision disguised as an engineering one**, and getting it right is the whole game. If you block publishing on everything, creators wait 40 minutes for a 4K transcode that 2% of viewers will ever use. If you block on nothing, you publish copyrighted or unsafe content. The answer is to split the jobs into a **blocking gate** and an **async tail**.

<img src="../assets/youtube-pipeline/publish-gate.svg" alt="The publish decision, drawn as two lanes feeding a gate. Left: the raw video. The jobs split into two groups. TOP LANE — BLOCKING (yellow, must finish before publish): generate a low-resolution rendition (at least 360p so there's something playable), copyright check, and nudity/safety check. These three flow into a yellow diamond gate labeled 'allow to publish?'. If copyright or nudity fails, the gate routes to 'reject / hold' (red). If all pass and a playable rendition exists, the gate routes to 'PUBLISH' (green) — the video goes live. BOTTOM LANE — NON-BLOCKING / ASYNC (blue, finishes after publish): higher resolutions (720p, 1080p, 4K), auto-captioning, and auto-extracted thumbnails. These run after the video is already live and attach to it as they complete; a custom thumbnail the creator uploaded can be applied immediately since it needs no processing. A caption: publish as soon as there's one safe, playable rendition; let quality and extras stream in afterward. Key insight box: you pick a small set of resolutions (e.g. 360p, then 720p) as the publish bar — not the full ladder." width="1180">

The **blocking gate** — everything that must be green before the video is public:

- <span style="color:#ffff99"><strong>At least one playable, low resolution.</strong></span> You need *something* to serve. We pick a small bar — say **360p** (and maybe 720p) — and require it. We do **not** wait for the full ladder. The full 1080p/4K renditions can arrive later; YouTube literally shows "HD will be available shortly" for exactly this reason.
- <span style="color:#ffff99"><strong>Copyright check.</strong></span> Publishing copyrighted material is a legal liability, so this gates. (The next section is entirely about making this expensive check *fast enough* to gate on.)
- <span style="color:#ffff99"><strong>Nudity / safety check.</strong></span> Publishing unsafe content is unacceptable, so this gates too.

The **async tail** — everything that attaches *after* the video is already live:

- <span style="color:#93c5fd"><strong>Higher resolutions</strong></span> (720p, 1080p, 4K). They stream in and the player offers them as they appear.
- <span style="color:#93c5fd"><strong>Auto-captioning.</strong></span> Subtitles showing up a few minutes after publish is fine — it does **not** block.
- <span style="color:#93c5fd"><strong>Thumbnails.</strong></span> A *custom* thumbnail the creator uploaded needs no processing and can apply instantly; *auto-extracted* thumbnails can be generated after publish and swapped in.

So the rule is: **publish the moment there is one safe, playable rendition; let quality and extras stream in behind it.** This is the same instinct as a [progressive image load](12-social-network-image-optimization-and-tagging.md) — show something correct fast, refine later — applied to an entire publishing workflow.

> **Memory hook:** *split jobs into a blocking gate and an async tail. Gate = one low rendition (360p) + copyright + nudity. Tail = HD/4K, captions, auto-thumbnails. Publish on a safe playable rendition; quality and extras catch up after.*

---

## Section 5 — Ordering for speed: check the 360p, not the 4K

**Question: copyright and nudity checks are expensive, and they gate publishing — so they sit on the critical path to "live." How do we make the creator's wait as short as possible without skipping the checks?**

Here's the subtle, important move. The safety and copyright checks need *a decoded rendition* to run on — they don't run on the raw container directly, and they certainly don't need the 4K master. So **which** rendition do you check?

If you run the copyright check on the **1080p/4K** rendition, you've now serialized two slow things: the slow HD transcode *and then* the slow check on a huge file. The creator waits for both. But if you run the checks on the **360p** rendition — which is the *fastest and cheapest to produce* and the smallest file to scan — the whole gate clears far sooner.

<img src="../assets/youtube-pipeline/ordering-360-first.svg" alt="The ordering optimization, drawn as a timeline DAG. Left: raw video. Step 1 (blue, fast): transcode to 360p first — the cheapest, quickest encode. Step 2 (yellow): as soon as 360p exists, run copyright check and nudity check ON the 360p rendition, in parallel — checking the small low-res file is much faster than checking a 4K file. Step 3 (yellow diamond → green): both checks pass on 360p, so the publish gate opens and the video goes LIVE serving 360p. Step 4 (blue, runs in the background AFTER publish): the higher renditions — 720p, 1080p, 4K — transcode and attach as they finish; captions and auto-thumbnail also run here. A contrasting grayed-out 'slow path' is shown above for comparison: if you had waited to transcode 4K first and then checked the 4K file, the gate would open much later. Caption: do the cheapest thing that satisfies the gate first — a 360p check unblocks publish, then let HD catch up asynchronously. The shape is fan-out, then fan-in at the gate, then an async tail." width="1180">

The optimal ordering falls right out:

1. <span style="color:#93c5fd"><strong>Transcode 360p first.</strong></span> It's the cheapest encode and produces the smallest file.
2. <span style="color:#ffff99"><strong>Run copyright + nudity on the 360p</strong></span>, in parallel. Checking a small low-res file is far faster than checking a 4K one, and the *result is the same* — a copyright match or a safety violation is detectable at 360p.
3. <span style="color:#8aff8a"><strong>Gate opens → publish</strong></span> as soon as both checks pass on the 360p. The video is live, serving 360p.
4. <span style="color:#93c5fd"><strong>Everything else runs after</strong></span>: 720p/1080p/4K transcodes, captions, auto-thumbnails — all in the background, attaching to the now-live video.

Notice the *shape* this creates. From the raw file we **fan out** (the checks depend on the 360p transcode), then **fan in** at the publish gate (publish needs copyright *and* nudity *and* 360p to all be done), then a long **async tail**. That fan-in — "do the publish step only when *all* of these other steps have succeeded" — is the exact pattern that, in the next section, breaks a naïve queue and demands a real orchestrator.

> **Memory hook:** *run the gating checks on the cheapest rendition (360p), not the 4K. Transcode 360p → check copyright+nudity on it → publish → let HD/captions/thumbnails catch up. Fan-out to checks, fan-in at the gate, async tail after.*

---

## Section 6 — Two ways to run a graph of jobs: events vs orchestration

**Question: we have a graph of dependent jobs. Broadly, what are our two architectural options for executing it — and which family does each tool belong to?**

There are two fundamentally different paradigms for "run a bunch of jobs," and choosing the wrong one is the most common mistake in this design:

| | **Event-driven** | **Workflow management** |
| --- | --- | --- |
| Tools | Kafka, SQS, Pub/Sub | **Airflow**, Luigi, Dagster, Prefect, Temporal |
| Unit | a **message/event** flowing forward | a **task** in a dependency graph |
| Mental model | "when X happens, react to it" | "task E runs only after C *and* D succeed" |
| Knows about dependencies? | **No** — each consumer sees its own messages | **Yes** — the whole DAG is declared up front |
| Knows when a *set* of jobs finished? | **No** | **Yes** — that's its whole job |
| Great for | high-volume streaming, decoupling producers/consumers, async fan-out with no join | multi-step jobs with dependencies, fan-in, retries, scheduling, visibility |

<span style="color:#93c5fd"><strong>Event-driven</strong></span> systems like [Kafka](16-storage-engine-etl-cdc.md) are superb at *moving events forward* and decoupling producers from consumers. A producer drops a message; some consumer reacts. Nobody waits for anybody. That's the strength — and, for our pipeline, the fatal weakness.

<span style="color:#ffff99"><strong>Workflow management</strong></span> tools like <span style="color:#ffff99"><strong>Apache Airflow</strong></span> (and Luigi, Dagster, Prefect) are built around the opposite idea: you **declare the dependency graph** — the DAG — up front, and the tool's entire purpose is to track which tasks have finished and fire the next ones only when their dependencies are satisfied.

Our pipeline is full of "do this only after those finish" — publish only after copyright *and* nudity *and* 360p. That's a **dependency graph**, not a stream. So the question becomes: *why exactly* does the event-driven approach fall apart here?

> **Memory hook:** *two paradigms — event-driven (Kafka: react to messages, no notion of dependencies) and workflow management (Airflow: declare a DAG, run a task only when its dependencies finish). Our pipeline is a dependency graph, so it's a workflow-management problem.*

---

## Section 7 — Why Kafka doesn't work here: the fan-in problem

**Question: we already know Kafka. Why not just push the video to a topic, have a consumer transcode it, push a "done" event, have the next consumer react, and chain it all together? Where does that actually break?**

Let's try it honestly and watch it fall apart. The naïve event-driven design: the upload drops a message on a topic; a transcode consumer picks it up; when it's done it emits a `transcoded` event; a copyright consumer reacts to that; emits `copyright-ok`; and so on.

<img src="../assets/youtube-pipeline/kafka-fanin.svg" alt="Why an event-driven queue struggles with this pipeline, in two panels. LEFT PANEL — the simple chain that looks fine: a producer (the upload) drops a message onto a Kafka topic (drawn as a horizontal tube), and a few consumers (boxes) each react to messages and emit new events. This works for a straight line A → B → C. RIGHT PANEL — the fan-in that breaks it (red): the publish step E must run only AFTER both copyright (C) and nudity (D) have finished. But C and D are independent consumers emitting independent 'done' events onto the bus. Nothing in Kafka knows that E needs BOTH. So you are forced to hand-build a join: some consumer must listen for both events, store partial state ('C done, waiting for D…') in an external database, correlate them by video id, handle the case where D arrives before C, handle a duplicate event, handle a missing event that never arrives, and time out. A red callout lists the burdens you've reinvented: who signals completion? where is the join state stored? how do you handle out-of-order and duplicate events? what about retries and 'where is video X stuck?'. The bottom caption: with hundreds of these dependencies, you end up rebuilding a workflow engine badly on top of a queue — fan-in is the thing a stream cannot express." width="1180">

The straight-line part works fine. The trouble is the **fan-in**. Recall the publish step `E` must run **only after both copyright `C` and nudity `D` finish.** But in Kafka, `C` and `D` are independent consumers emitting independent "done" events onto the bus. **Nothing in the system knows that `E` needs both.** So you're forced to build the join yourself, by hand:

- **Who signals completion?** You invent a convention where each consumer emits a `done` event. Now every job must reliably publish its own completion, and you must trust it.
- **Where does the join state live?** Some new consumer has to listen for *both* `copyright-ok` and `nudity-ok`, remember "C is done, still waiting on D…" in an **external database keyed by video id**, and only then trigger `E`. You just built a state machine in a side table.
- **Out-of-order and duplicates.** `D` might arrive before `C`. An event might be delivered twice (Kafka is at-least-once). Your join logic must be idempotent and order-independent.
- **The event that never comes.** If the nudity consumer crashes and never emits `nudity-ok`, `E` waits forever. You need timeouts, dead-letter handling, and alerting — all hand-rolled.
- **Retries and visibility.** When the copyright check fails transiently, who retries it, how many times, with what backoff? And when a creator asks "why isn't my video live?", you have *no single place* that knows the state of the whole graph — you're grepping across topics and a side table.

Now multiply this by **hundreds of dependencies** across the real pipeline. You haven't built a video pipeline; you've built a **buggy, ad-hoc workflow engine on top of a queue** — reinventing dependency tracking, join state, retries, timeouts, and observability, badly. The event stream has no concept of "this *set* of tasks is complete," and that concept is exactly what we need.

The lesson isn't that Kafka is bad — it's *excellent* at what it's for (high-volume decoupled streaming, and we'd happily use it as the *transport* under the hood). It's that **modeling a dependency graph as a stream of events is the wrong abstraction.** When the core requirement is "run E after C and D both succeed, with retries and visibility," you want a tool whose native vocabulary is exactly that.

> **Memory hook:** *Kafka has no notion of "all of these jobs finished." Fan-in (run E only after C and D) forces you to hand-build a join: completion events, external join state, dedup, out-of-order handling, timeouts, retries, and a status view. Times hundreds of dependencies, that's a workflow engine reinvented badly. Wrong abstraction — use orchestration.*

---

## Section 8 — The workflow management tool and the DAG

**Question: what does a workflow tool give us that the queue couldn't — and what is the one data structure at its heart?**

The heart of every workflow tool is the <span style="color:#ffff99"><strong>DAG — Directed Acyclic Graph.</strong></span> You declare your jobs as **nodes** and their dependencies as **directed edges**; "acyclic" means no job can (transitively) depend on itself, so the graph always has a valid execution order. The orchestrator reads this graph and does the one thing Kafka couldn't: it runs a task **only when all of its upstream tasks have succeeded.**

<img src="../assets/youtube-pipeline/workflow-dag.svg" alt="The video processing pipeline expressed as a DAG in a workflow tool, in two parts. TOP — the abstract fan-in rule that motivates everything: a small DAG with a root, branching to nodes C and D, which both point into node E, with a highlighted key callout 'Do E only when both C and D are done.' This is the dependency the orchestrator enforces natively. BOTTOM — the real video-processing DAG drawn top-down: root node 'video processing' → 'validation' (check the upload is a real, intact video) → fans out to four branches: (1) 'transcoding', which itself fans out to 'generate 360', 'generate 720', 'generate 1080'; (2) 'copyright'; (3) 'thumbnail'; (4) 'captioning'. Edges show that copyright depends on a rendition existing (generate 360 → copyright), and a final 'publish' node depends on the gating tasks (generate 360 + copyright + nudity) — the fan-in — while the non-gating branches (1080, captioning, auto-thumbnail) point to publish with a dashed 'non-blocking' edge meaning they may finish after. A caption: the DAG declares the whole graph once; the orchestrator tracks each task's state and fires downstream tasks exactly when their upstream dependencies succeed — no hand-built joins, no side tables." width="1180">

Compare this directly to the Kafka nightmare. The fan-in that forced us to build an external join table is now **one line of dependency declaration**: `publish` depends on `[generate_360, copyright, nudity]`, and the orchestrator *natively* waits for all three. Everything we hand-rolled in Section 7 is built in:

- **Dependencies & fan-in** — declared as edges; "run E after C and D" is the tool's native vocabulary.
- **Who polls / who tracks state** — the orchestrator's scheduler does, continuously, in its own metadata database. There is one authoritative place that knows every task's state.
- **Retries, timeouts, backoff** — per-task configuration, not bespoke code.
- **Visibility** — a UI shows the whole DAG for video X, green/red per task, so "where is it stuck?" is a glance.
- **Passing data downstream** — the output of one task (e.g. the S3 path of the 360p rendition) can be handed to the next task as input.

This is what the user-facing pipeline becomes: `video processing → validation → {transcoding → 360/720/1080, copyright, thumbnail, captioning} → publish`, with the gating subset feeding the publish fan-in and the non-gating branches allowed to finish late. Now let's express our pipeline in the most common such tool — Apache Airflow.

> **Memory hook:** *a workflow tool is built on a DAG: jobs as nodes, dependencies as directed edges, no cycles. "Run E after C and D" is one edge declaration, not a hand-built join. The orchestrator natively tracks state, handles retries/timeouts, shows a status UI, and passes one task's output to the next.*

---

## Section 9 — Orchestrating the pipeline with Apache Airflow

**Question: we've chosen a workflow orchestrator. Concretely, what does our pipeline look like in the most common one — Apache Airflow — and how does it express the fan-in that broke Kafka?**

Apache Airflow is the workhorse workflow orchestrator. At a glance it's four cooperating pieces: a <span style="color:#93c5fd"><strong>scheduler</strong></span> (the brain that decides which tasks are ready to run), a <span style="color:#ffff99"><strong>metadata database</strong></span> (the single source of truth for every task's state), a fleet of <span style="color:#93c5fd"><strong>workers</strong></span> (that run the task code), and a <span style="color:#8aff8a"><strong>webserver / UI</strong></span> (that shows the whole DAG's state — the "where is video X stuck?" view). You declare the DAG in a Python file; the scheduler runs each task the moment its upstream dependencies all succeed.

Here's the gating part of our pipeline as Airflow code:

```python
from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime

with DAG(
    dag_id="video_processing",
    start_date=datetime(2026, 1, 1),
    schedule=None,            # triggered per-upload, not on a clock
    catchup=False,
) as dag:

    validate     = PythonOperator(task_id="validate",     python_callable=validate_upload)
    gen_360      = PythonOperator(task_id="gen_360",      python_callable=transcode_360)
    copyright    = PythonOperator(task_id="copyright",    python_callable=copyright_check)
    nudity       = PythonOperator(task_id="nudity",       python_callable=nudity_check)
    publish      = PythonOperator(task_id="publish",      python_callable=go_live)

    # the async tail
    gen_720      = PythonOperator(task_id="gen_720",      python_callable=transcode_720)
    gen_1080     = PythonOperator(task_id="gen_1080",     python_callable=transcode_1080)
    captions     = PythonOperator(task_id="captions",     python_callable=auto_caption)

    # dependencies: the gate
    validate >> gen_360 >> [copyright, nudity] >> publish
    #   ^ checks run on the 360p rendition, then fan in to publish

    # dependencies: the async tail (runs after publish, doesn't block it)
    validate >> [gen_720, gen_1080, captions]
```

Read the dependency lines and the whole design from Section 5 is right there: `validate >> gen_360 >> [copyright, nudity] >> publish` says *validate first, then transcode 360p, then run copyright and nudity in parallel on it, then — once both succeed — publish.* The `[gen_720, gen_1080, captions]` branch hangs off `validate` and runs independently, never blocking `publish`.

### Trigger rules: the knob behind fan-in

By default, a task runs only when **all** its upstream tasks succeeded — Airflow's `trigger_rule="all_success"`. That default *is* our fan-in: `publish` waits for both `copyright` and `nudity`. But the trigger rule is configurable, and the alternatives matter:

| `trigger_rule` | `publish` runs when… | Use for |
| --- | --- | --- |
| `all_success` (default) | every upstream succeeded | the publish gate — copyright **and** nudity must pass |
| `all_done` | every upstream finished (success *or* fail) | a cleanup task that must run regardless |
| `one_success` | any one upstream succeeded | "thumbnail ready from *either* custom upload *or* auto-extract" |
| `one_failed` | any upstream failed | fire an alert / quarantine the video the moment a check fails |
| `none_failed` | all succeeded or were skipped | proceed past optional branches that were skipped |

So "publish only if copyright **and** nudity pass" is `all_success` (the default). "Alert the moment any safety check fails" is a separate `one_failed` task wired to the same checks. The fan-in semantics that cost us a side table in Kafka are a single keyword here.

### What else you get for free

Everything we hand-rolled on Kafka is built in. Passing one task's output to the next — the 360p rendition's S3 path from `gen_360` into `copyright` — rides <span style="color:#ffff99"><strong>XCom</strong></span>. **Retries, timeouts, and backoff** are per-task config (`retries=3, retry_delay=…`), so a flaky copyright API recovers on its own. A <span style="color:#93c5fd"><strong>sensor</strong></span> can wait for the raw upload to land in S3 before `validate` runs. And the DAG is **triggered per upload** (`schedule=None`) — one run per video. Best of all, the scheduler tracks every task's state in the metadata DB, so "where is video X stuck?" is a glance at the UI — not a grep across Kafka topics and a side table.

> The *how* — Airflow's scheduler loop, executor model, task state machine, and how it stays durable and scales as a distributed system — is a substantial topic in its own right. It's the next post: **[Inside Apache Airflow](24-high-throughput-airflow.md)**.

> **Memory hook:** *Airflow models the pipeline as a DAG declared in Python; `validate >> gen_360 >> [copyright, nudity] >> publish` is the gate, and the default `all_success` trigger rule IS the fan-in. A scheduler runs each task when its upstreams succeed, tracking state in a metadata DB; XCom passes outputs, and retries/sensors/per-upload triggering are built in. The full internals are their own post (24).*

---

## Where this leaves us: the complete pipeline

We started from one raw file in S3 and a deceptively simple request — "process this video" — and discovered it was a **graph of dependent jobs**, not a stream. We split the jobs into a blocking gate (one safe playable rendition + copyright + nudity, all checked on the cheap 360p) and an async tail (HD, captions, thumbnails). We tried to run the graph on Kafka and watched fan-in force us to hand-build a join, retries, timeouts, and a status view — a workflow engine reinvented badly. So we used the right tool: a workflow orchestrator whose native vocabulary is the **DAG** — Apache Airflow, whose scheduler, metadata DB, and workers run our graph with built-in dependencies, retries, data-passing, and visibility. (How Airflow does that internally — its scheduler loop, executor, state machine, and scaling — is its [own post](24-high-throughput-airflow.md).)

<img src="../assets/youtube-pipeline/final-map.svg" alt="The complete YouTube video pipeline in one map, left to right. STAGE 1 UPLOAD (pink): creator → pre-signed URL → raw video lands in S3 (yellow cylinder). The landing triggers the orchestrator. STAGE 2 PROCESSING, orchestrated by Airflow (the center, with Airflow's scheduler + metadata DB + workers shown as the engine running the DAG): an S3 sensor → validate → gen_360 (blue) → copyright + nudity checks run on the 360p (yellow) → fan-in to the publish gate (yellow diamond). If checks fail → reject/hold (red). If they pass → PUBLISH → video goes LIVE serving 360p (green). The async tail (blue, dashed, runs after publish): gen_720, gen_1080, captions, auto-thumbnail — each attaches to the live video as it finishes. STAGE 3 DISTRIBUTION (green): the renditions in S3 are served to viewers through a CDN. A legend ties the colors to upload/write (pink), storage and gate (yellow), async processing/control (blue), serve path (green), failure (red). Caption: one upload → orchestrated DAG with a gate and an async tail → CDN distribution; Airflow tracks every task's state so the gate fan-in, retries, and the 'where is it stuck' view are all built in." width="1320">

The one idea to carry away: **a pipeline of dependent jobs is a DAG, and a DAG wants an orchestrator, not a queue.** Kafka moves events forward and never knows when a set of jobs is done; Airflow declares the whole graph, tracks each task's state in one metadata database, and fires each task exactly when its upstream dependencies succeed — turning fan-in, retries, data-passing, scheduling, and "where is video X stuck?" from things you hand-build into things you configure. Upload is Instagram, distribution is a CDN, but the processing in the middle — the part that's genuinely hard — is a workflow-orchestration problem, and naming it that is most of the solution.

> **Memory hook:** *one upload → S3 → Airflow-orchestrated DAG (validate → 360p → copyright+nudity → publish gate; HD/captions/thumbnails as an async tail) → CDN. A dependency graph is a DAG; a DAG wants an orchestrator, not a queue. Airflow's scheduler + metadata DB make fan-in, retries, data-passing, and visibility built-in, not hand-built.*
