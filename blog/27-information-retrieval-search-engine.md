# How a Text Search Engine Works: From a Linear Scan to a Ranked Inverted Index

This post builds a **text search engine** from first principles — the kind of system that takes a few words you type and finds, out of billions of documents, the handful you actually wanted, in a few hundred milliseconds. We start from the most naive thing that could possibly work (read every document and check if it matches), watch it fall over, and rebuild it the way real engines are built: an **inverted index** at the core, a **crawl-and-index pipeline** feeding it, a **query path** that merges sorted lists instead of scanning, and a **ranking** layer that turns "documents that match" into "documents ranked by how well they match." Along the way we make the relevance math concrete — TF-IDF, cosine similarity, Jaccard, BM25 — and the messy-but-essential bag of tricks that makes real search forgiving of typos, bad spelling, and missing spaces. By the end you should be able to design a search engine on a whiteboard and defend every component. The *learning-to-rank and neural* half — embeddings, dense retrieval, the ML that powers modern relevance — is its own post: [Machine Learning in Information Retrieval](28-information-retrieval-ml-ranking.md).

**Question: you have a corpus — JSON records, web pages, text documents — and a user with an *information need* expressed as a few words: "fast scalable search." Your job is to return the *most relevant* documents from the corpus, ranked best-first, fast enough that it feels instant, and forgiving enough that "fast scalabe serch" still works. The naive answer — loop over every document and check if it contains the words — is correct and completely unusable at scale. So what does the real machine look like, and why is almost every piece of it the way it is?** The single idea the whole field turns on is this: **don't search the documents at query time — search a structure you built *from* the documents ahead of time.** That structure is the inverted index, and once you have it, every other decision (how documents get in, how queries are parsed, how results are ranked and scored, how you even *measure* "good") falls into place around it. This post is the walk from the naive scan to that machine, one component at a time.

This post sits in a small arc on information retrieval. We built a [recent-searches feed](25-information-retrieval-recent-searches.md) and a [live commentary system](26-information-retrieval-cricbuzz-commentary.md) and learned to pick components by how a workload actually behaves; we hand-built a [word dictionary as a read-optimized file](18-storage-engine-word-dictionary.md) and learned exact-match lookup (the thing a search engine deliberately is *not*); and we built [multi-tiered storage](21-high-throughput-multi-tiered-db.md), [LSM-trees](22-high-throughput-lsm-trees.md), and a [CDC/ETL pipeline](16-storage-engine-etl-cdc.md) whose ideas — immutable segments, background merges, a write path decoupled from a read path — turn out to be exactly how a search index is built. This is where they assemble into one system.

> **Memory hook:** *a search engine never searches documents at query time — it searches an inverted index built from them beforehand. Information need → ranked relevant documents. Everything else (crawl, parse, rank, evaluate) is built around that one inversion.*

---

## The naive search, and why it forces an index

**Question: the simplest possible search — take the query `q`, walk every document one by one, and check whether it "matches." What's wrong with it?**

Two things, and both are fatal at scale.

- <span style="color:#ff8a8a"><strong>It's O(n) in the corpus.</strong></span> Every query reads *every* document. Ten documents, fine. Ten billion web pages, and a single search would take longer than the user's patience by many orders of magnitude. Worse, the cost grows with the corpus — exactly backwards from what you want, since a bigger corpus is supposed to be *more* useful, not slower.
- <span style="color:#ff8a8a"><strong>"Matches" is doing a lot of hidden work.</strong></span> A substring check (`document.contains("search")`) is itself linear in the document's length, finds "research" when you wanted "search," and tells you *nothing* about which of the matching documents is the *best* one.

The naive scan answers the wrong question. We don't want "which documents contain this word" computed *now*; we want it *precomputed*, so the query is a lookup, not a scan.

**How do we make lookup faster?** The same way a database makes a `WHERE` clause faster, the same way the [word dictionary post](18-storage-engine-word-dictionary.md) made word lookup faster: **indexing**. We build, once, a structure that maps *from the thing we search by* (a word) *to the thing we want* (the documents containing it). For search, that structure has a name, and it is the core and crux of the entire system.

<img src="../assets/information-retrieval-search-engine/naive-to-inverted.svg" alt="The journey from naive search to the inverted index, in three panels. Panel 1, 'The information need': a user with a query 'fast scalable search' points at a corpus (JSON, web pages, text docs); the goal labelled 'find the MOST RELEVANT documents from the corpus.' Panel 2, 'Simplest search' (rejected, red): the query goes through documents one-by-one checking 'does it match?' via substring check; annotated 'O(n) in the corpus — re-scans every doc per query, not scalable; substring finds research when you wanted search; no notion of which match is best.' Panel 3, 'The fix: indexing' (green, labelled 'core and crux'): an inverted index drawn as a dictionary of terms, each term w_i pointing to a posting list [d_a, d_b, d_c, ...] of the documents that contain it; caption 'word → documents that contain the word. Built once, ahead of time. Now a search is a lookup, not a scan.' A banner across the bottom: don't search the documents at query time — search a structure built from them beforehand." width="1100">

> **Memory hook:** *naive search is O(n) per query (re-scans the whole corpus) and substring matching can't rank. The fix is to precompute, once, a map from word → documents containing it, so a query becomes a lookup instead of a scan.*

---

## The inverted index — the core and crux

**Question: what exactly is this structure, what does each entry store, and how do raw documents become it?**

A normal ("forward") index maps **document → the words it contains**. That's the natural layout and it's useless for search — to answer "who has the word *search*?" you'd read every document's word list. So we **invert** it:

```
forward:   d1 → {fast, scalable, search}        (doc → its words)
inverted:  search    → [d1, d4, d7, d9, ...]    (word → docs that contain it)
           scalable  → [d1, d2, d4, ...]
           fast      → [d1, d5, ...]
```

The <span style="color:#ffff99"><strong>inverted index</strong></span> has two parts:

- A <span style="color:#93c5fd"><strong>term dictionary</strong></span> — every distinct term in the corpus, each pointing to its list. Lookup here is effectively O(1) (hash) or O(log V) (sorted/FST), where V is the vocabulary size, *not* the corpus size. This is the whole win: query cost decoupled from corpus size.
- A <span style="color:#8aff8a"><strong>posting list</strong></span> per term — the documents that contain it. Each element is a **posting**, and a posting stores more than just a document id:
  - the <span style="color:#8aff8a"><strong>document id</strong></span>,
  - the <span style="color:#ffd27f"><strong>term frequency</strong></span> (how many times the term appears in that document — we'll need it for ranking),
  - the <span style="color:#b79bff"><strong>positions</strong></span> (the offsets where it appears — needed for *phrase* queries like `"scalable search"` and proximity scoring),
  - optionally per-**field** info (did it appear in the title or the body?) for weighted scoring later.

Crucially, **posting lists are kept sorted by document id.** That single invariant is what makes query-time merging fast (we'll see two-pointer intersection and skip pointers below). A search is now: look up each query term in the dictionary, grab its sorted posting list, and merge.

### From a document to postings: the analysis pipeline

You don't index raw text — you index *normalized tokens*, and the query gets the **exact same treatment** so the terms line up. The pipeline, per document:

1. <span style="color:#93c5fd"><strong>Parse</strong></span> — pull text (and structure: title, body, anchor text) out of HTML/JSON.
2. <span style="color:#93c5fd"><strong>Tokenize</strong></span> — split the text into tokens (usually on word boundaries).
3. <span style="color:#93c5fd"><strong>Normalize</strong></span> — lowercase, fold accents/Unicode, standardize punctuation, so "Search", "search", and "SEARCH" become one term.
4. <span style="color:#ffd27f"><strong>Remove stop words</strong></span> (optionally) — "the", "a", "of" are in almost every document, so they carry little signal and bloat posting lists. (Modern engines often keep them and let IDF down-weight them instead.)
5. <span style="color:#8aff8a"><strong>Stem or lemmatize</strong></span> — collapse a word to its root so "running", "runs", "ran" all match a query for "run". **Stemming** is the crude, fast version: chop suffixes by rule (Porter stemmer: "running" → "run", "scalable" → "scalabl"). **Lemmatization** is the linguistically correct version: map to the dictionary headword ("better" → "good", "ran" → "run"). Stemming is cheaper and usually good enough.
6. <span style="color:#8aff8a"><strong>Emit postings</strong></span> — for each surviving term, append (this doc id, frequency, positions) to that term's posting list.

Run the *identical* pipeline on the query at search time and the comparison is apples-to-apples: a query for "Searching" becomes the term `search`, which is exactly what's in the index.

<img src="../assets/information-retrieval-search-engine/inverted-index.svg" alt="The inverted index and the analysis pipeline that builds it. Top half, the analysis pipeline as a left-to-right flow for one document: raw doc 'The Fast, Scalable Search Engines' → Parse (extract title/body) → Tokenize (split into words) → Normalize (lowercase, fold accents) → Remove stop words (drop 'the') → Stem/lemmatize (engines→engine, scalable→scalabl) → emit terms. A note: 'the query runs the SAME pipeline so terms line up.' Bottom half, the resulting inverted index: a term dictionary column (fast, scalable, search, engine) each with an arrow to its posting list. The 'search' posting list is expanded to show three postings, each a little record: docID=d1, tf=2, pos=[3,9]; docID=d4, tf=1, pos=[7]; docID=d7, tf=5, pos=[1,4,8,…]. A caption notes: posting lists are kept SORTED by docID (this is what makes query-time merging fast); a posting stores docID + term frequency + positions (+ field). Stemming vs lemmatization called out: stemming = fast rule-based suffix chop (running→run); lemmatization = dictionary headword (better→good)." width="1100">

> **Memory hook:** *invert document→words into word→sorted-list-of-docs. A posting stores docID + term frequency + positions (+ field), sorted by docID. Build it via parse→tokenize→normalize→stop-words→stem/lemmatize, and run the SAME pipeline on the query so terms match. Lookup cost depends on vocabulary size, not corpus size.*

---

## Crawling and indexing: how documents get in (the write loop)

**Question: the index is the heart, but something has to *feed* it. Where do the documents come from, and how is that ingest path kept from slowing down user queries?**

A search engine is really **two loops that meet at the index**, and separating them is the single most important architectural decision:

- The <span style="color:#ff8bd2"><strong>ingest loop</strong></span> (crawl → analyze → index) — heavy, batchy, asynchronous. It can take seconds or minutes per document and nobody is waiting on it.
- The <span style="color:#8aff8a"><strong>query loop</strong></span> (parse → retrieve → rank → return) — latency-critical, sub-second, a user is staring at a spinner.

This is [CQRS](16-storage-engine-etl-cdc.md) in spirit: a write path and a read path that share *data* but not *code paths*, so a giant indexing job never makes a search slow. The index is the seam between them.

### Crawling (for a web engine)

If your corpus is a web you don't own, you have to go get it. The <span style="color:#ff8bd2"><strong>crawler</strong></span> (spider/bot) downloads a page, extracts its text and its outbound links, and follows those links to discover more pages — a graph traversal of the web starting from **seed URLs**. The interesting part isn't the fetch; it's the bookkeeping:

- <span style="color:#ffd27f"><strong>URL frontier</strong></span> — the crawler's prioritized to-do queue. It decides *which URL next* (priority: important, fresh, frequently-changing pages first) and *when* (politeness). It's a scheduler, not just a queue — priority and politeness collide here.
- <span style="color:#93c5fd"><strong>Politeness</strong></span> — rate-limit per host so you don't hammer one server into the ground; honor `robots.txt` (which paths you're allowed to crawl) and any `Crawl-delay`. Fetch and cache `robots.txt` per host rather than re-fetching it constantly.
- <span style="color:#b79bff"><strong>Deduplication, two kinds.</strong></span> *URL dedup* — "have I already queued this link?" — typically a [Bloom filter](18-storage-engine-word-dictionary.md) for a fast probabilistic reject, then an exact check for the few that pass. *Content dedup* — many different URLs serve near-identical pages — uses a **SimHash** fingerprint (a locality-sensitive hash where *similar pages get similar fingerprints*); near-duplicates show up as a small Hamming distance between fingerprints. (Google's published figure: 64-bit fingerprints, near-dup if within 3 bits, across an 8-billion-page repository.)
- <span style="color:#ffd27f"><strong>Freshness / recrawl</strong></span> — a news homepage changes by the minute; an old article never changes. The crawler learns each page's change frequency and sets a recrawl interval accordingly, spending its limited **crawl budget** where content actually moves.
- <span style="color:#93c5fd"><strong>Distributed</strong></span> — partition the frontier across many machines *by host*, which conveniently also keeps per-host politeness correct (one machine owns all of `example.com`, so it alone enforces that host's rate limit).

If you *own* the corpus (a product catalog, a document store), you skip crawling entirely and feed documents straight in — often via a [change-data-capture stream](16-storage-engine-etl-cdc.md) so every insert/update/delete in the source database flows into the index.

### Indexing at scale: immutable segments

A web-scale index is far too big for one machine, and it's constantly being added to. The trick — and it's the *same* trick as [LSM-trees](22-high-throughput-lsm-trees.md) — is **immutable segments**. New documents are written into a new, small, self-contained mini-index (a segment); existing segments are never modified in place. Updates and deletes are tombstones; the space is reclaimed later by a background **merge** that compacts small segments into bigger ones. Immutability buys lock-free concurrent reads, cache-friendliness, and trivial replication. Then the whole thing is **sharded** across servers so a query can fan out and run in parallel. Inside a segment, the term dictionary is often a compact FST, and posting lists are **delta-encoded** (store gaps between sorted doc ids, which are small integers, instead of the ids themselves) to shrink the index dramatically.

<img src="../assets/information-retrieval-search-engine/crawl-index-loop.svg" alt="The ingest loop: crawling and indexing, drawn as the CQRS write side. Left, the crawl sub-loop: seed URLs feed a URL Frontier (labelled 'prioritized to-do queue: which URL next + politeness'); the Frontier hands a URL to the Crawler/Renderer, which fetches the page, checks a cached robots.txt, extracts text and outbound links; new links pass through a dedup gate (URL dedup via Bloom filter; content dedup via SimHash fingerprint, near-dup if Hamming distance ≤ 3) and the fresh ones loop back into the Frontier. A freshness note: recrawl interval set per page by how often it changes (crawl budget). Middle, the page content flows into the Indexer running the analysis pipeline (tokenize→normalize→stem). Right, the output: an INVERTED INDEX stored as immutable segments (new docs → new segment; deletes are tombstones; background merge compacts them — same idea as LSM-trees), delta-encoded and sharded across many servers. A divider line labels the left+middle as the asynchronous WRITE/INGEST loop and notes 'the index is the seam: writes append segments, reads query them — CQRS, so indexing never slows queries.'" width="1180">

> **Memory hook:** *a search engine is two loops meeting at the index: an async ingest loop (crawl→analyze→index) and a latency-critical query loop (CQRS). Crawling = frontier (priority+politeness) + robots.txt + dedup (Bloom for URLs, SimHash for content) + freshness recrawl, partitioned by host. Index at scale = immutable segments + background merge + delta-encoded posting lists + sharding — the LSM-tree pattern applied to search.*

---

## Querying: the read path that merges instead of scans

**Question: a query arrives — `scalable AND search`. Walk the read path from the raw string to a ranked page of results. Where does the speed come from?**

1. <span style="color:#93c5fd"><strong>Parse and analyze the query.</strong></span> Run the same analysis pipeline as indexing (tokenize, normalize, stem) so `Searching` → `search`. Do spell correction and query expansion here too (next section). Build a small query tree: `scalable AND search`.
2. <span style="color:#93c5fd"><strong>Fan out to shards.</strong></span> In a sharded index, send the query to every shard; each finds its local best results and a coordinator merges them at the end.
3. <span style="color:#8aff8a"><strong>Look up posting lists.</strong></span> One dictionary lookup per term — O(1)-ish — gives you each term's sorted posting list. No documents have been touched yet.
4. <span style="color:#8aff8a"><strong>Merge the posting lists.</strong></span> This is the engine of the engine:
   - **AND (intersection)** — both lists are sorted by doc id, so walk them with **two pointers**: advance whichever points at the smaller id; when they're equal, emit and advance both. O(m + n), no scanning of documents. **Start with the shortest list** — the intersection can't be bigger than it.
   - **OR (union)** — merge the sorted lists, dedup.
   - **Skip pointers** — posting lists embed forward pointers so intersection can *jump over* runs that can't match, pushing the cost from O(n) toward roughly O(√n). On a 50-million-document posting list, that's the difference between 50M steps and a few thousand.
5. <span style="color:#ffd27f"><strong>Score and keep the top-k.</strong></span> The surviving documents get a relevance score (next section); a small top-k heap keeps just the best ones rather than sorting millions.
6. <span style="color:#8aff8a"><strong>Merge shards, paginate, return.</strong></span> The coordinator merges each shard's top-k into a global top-k, applies pagination (page 1 = top-k; deeper pages continue from a cursor), and returns.

The headline: **raw documents are never re-scanned at query time.** All the expensive work — parsing, link analysis, computing term frequencies — happened once, at index time. Query latency is just *dictionary lookup + merge of sorted lists + score the survivors.*

<img src="../assets/information-retrieval-search-engine/query-read-path.svg" alt="The query read path for 'scalable AND search', as the CQRS read side. Step 1: the raw query string runs through the same analysis pipeline as indexing (normalize, stem) producing terms [scalable, search] and a query tree 'scalable AND search'; a note: spell-correct and expand here. Step 2: fan out to shards. Step 3: dictionary lookup fetches each term's sorted posting list — scalable → [d1,d2,d4,d9,...], search → [d1,d4,d7,d9,...] — 'no documents touched yet.' Step 4, the centerpiece: a two-pointer intersection of the two sorted lists, showing pointers walking and emitting the common ids [d1, d4, d9]; annotations 'both lists sorted by docID → walk with two pointers, O(m+n), advance the smaller; start with the SHORTEST list' and 'skip pointers let intersection jump over non-matches → ~O(√n), e.g. √n hops on a 50M list.' Step 5: the survivors get scored and a top-k heap keeps the best. Step 6: a coordinator merges per-shard top-k, paginates, returns. Bottom banner: raw documents are NEVER re-scanned at query time — latency = dictionary lookup + merge sorted lists + score top-k; everything expensive was precomputed at index time." width="1180">

> **Memory hook:** *read path = analyze query (same pipeline) → fan out to shards → dictionary lookup per term → merge sorted posting lists (AND = two-pointer intersection O(m+n), start with the shortest; skip pointers → ~O(√n)) → score survivors into a top-k heap → merge shards, paginate. Documents are never re-scanned; the index was built for exactly this.*

---

## Ranking: from "matches" to "best match"

**Question: intersection gives you the *set* of documents that contain the query terms — often thousands of them. The user sees ten. Which ten, in what order? How do you turn a match into a *score*?**

Boolean matching ("contains all the terms") is necessary but not sufficient — it gives you a bag of candidates with no order. **Ranking** is the function that scores each candidate so the best float to the top. Let's build the scoring math from the ground up.

### TF-IDF: reward terms frequent *here* but rare *everywhere*

The foundational intuition has two halves:

- <span style="color:#8aff8a"><strong>Term frequency (TF)</strong></span> — a document that uses the query term *more* is probably more about it. But not linearly: the 10th occurrence of "search" matters far less than the 1st, so we damp it with a log: `wf = 1 + log₁₀(tf)`.
- <span style="color:#ffd27f"><strong>Inverse document frequency (IDF)</strong></span> — a term that appears in *almost every* document ("the") tells you nothing; a term in *few* documents is highly discriminating. So weight each term by how rare it is across the corpus: `idf = log₁₀(N / df)`, where N is the total number of documents and df is how many contain the term.

The weight of a term in a document is the product: <span style="color:#ffff99"><strong>tf-idf = (1 + log₁₀ tf) × log₁₀(N / df)</strong></span>.

**Worked example.** Corpus of N = 4 documents; the term is "network", appearing in 3 of them (df = 3).

```
idf("network") = log₁₀(4 / 3) = 0.1249

d1: tf = 3  →  wf = 1 + log₁₀(3) = 1.4771  →  tf-idf = 1.4771 × 0.1249 = 0.1845
d4: tf = 7  →  wf = 1 + log₁₀(7) = 1.8451  →  tf-idf = 1.8451 × 0.1249 = 0.2305
d3: tf = 1  →  wf = 1 + log₁₀(1) = 1.0000  →  tf-idf = 1.0000 × 0.1249 = 0.1249
```

Notice the damping: d4 has **2.3×** the raw term count of d1 (7 vs 3) but only **~25%** more weight. That's the log doing its job — preventing keyword stuffing from dominating.

### The vector space model + cosine similarity

Now picture each document and the query as a **vector** in a space with one dimension per vocabulary term, each component being that term's tf-idf weight. Most components are zero (a document has few of the millions of possible words). Relevance becomes **geometry**: how aligned is the document's vector with the query's?

The right measure is the angle between them, via <span style="color:#8aff8a"><strong>cosine similarity</strong></span>:

```
cos(q, d) = (q · d) / (|q| × |d|)
```

**Worked example.** Three terms [gpu, memory, latency]. Query q = (1, 1, 0), document d = (3, 0, 4).

```
q · d = (1×3) + (1×0) + (0×4) = 3
|q|   = √(1 + 1 + 0) = 1.4142
|d|   = √(9 + 0 + 16) = 5
cos   = 3 / (1.4142 × 5) = 0.4243
```

Why cosine and not the raw dot product? Because the raw dot product **rewards long documents** — more terms means bigger weights means a bigger dot product, regardless of actual relevance. Dividing by the vector magnitudes is **length normalization**: it compares *direction*, not *size*, so a short on-topic document and a long on-topic document score the same. This is the single most important correction in classical ranking.

### Jaccard similarity: the cheap set-overlap measure (and its limits)

A simpler measure treats query and document as **sets** of terms and asks how much they overlap:

```
J(A, B) = |A ∩ B| / |A ∪ B|
```

**Worked example.** Query A = {fast, scalable, search}, document B = {scalable, search, engine, index}.

```
A ∩ B = {scalable, search}                          → 2
A ∪ B = {fast, scalable, search, engine, index}     → 5
J     = 2 / 5 = 0.4
```

Jaccard is great for **near-duplicate detection** and set-overlap problems (it's the basis of shingling/MinHash for crawl dedup). But it's a poor *relevance ranker*, for three reasons, and naming them cements why TF-IDF/cosine win: (1) it ignores **term frequency** — a word appearing once counts the same as 50 times; (2) it ignores **IDF** — a match on "the" counts as much as a match on "scalable", so stop-word overlap inflates the score; (3) it's thrown off by **length disparity**. Cosine over tf-idf vectors fixes all three.

### BM25: the scorer everyone actually uses

<span style="color:#b79bff"><strong>BM25</strong></span> is the modern default (it's what Lucene/Elasticsearch/OpenSearch use out of the box) because it keeps TF-IDF's two good instincts but fixes TF-IDF's two flaws — unbounded term frequency and ad-hoc length handling — *inside the formula*:

```
BM25(d, q) = Σ  idf(t) × [ f(t,d) × (k₁ + 1) ] / [ f(t,d) + k₁ × (1 − b + b × |d|/avgdl) ]
            t∈q
```

Two knobs do the work:

- <span style="color:#ffd27f"><strong>TF saturation (k₁)</strong></span> — as term frequency grows, the score approaches a ceiling instead of rising forever. The first occurrence helps a lot; the twentieth barely moves the needle. (Typical k₁ ≈ 1.2–2.0.)
- <span style="color:#93c5fd"><strong>Length normalization (b)</strong></span> — the `(1 − b + b·|d|/avgdl)` factor penalizes documents longer than the corpus average, so a long document can't win just by containing the term more times. (Typical b ≈ 0.75.)

**Worked intuition.** Take k₁ = 1.2 and an average-length document. The term-frequency factor `2.2·f / (f + 1.2)` evaluates to:

```
f = 1   →  1.00
f = 2   →  1.375
f = 5   →  1.774
f = 20  →  2.075   (approaching the ceiling k₁+1 = 2.2)
```

Going from 1 to 2 occurrences adds 0.375; going from 5 to 20 (fifteen more occurrences!) adds only ~0.30. That diminishing return is exactly the saturation plain TF-IDF lacks. Multiply by IDF, sum over the query terms, and you have a robust score.

<img src="../assets/information-retrieval-search-engine/scoring.svg" alt="The ranking math built up in four linked panels. Panel 1, TF-IDF: two gauges — 'TF: more occurrences = more relevant, but log-damped (1 + log10 tf)' and 'IDF: rarer term = more discriminating, log10(N/df)' — multiplied together; the worked example for term network in N=4 docs (df=3, idf=0.1249) showing d1 tf3→0.1845, d4 tf7→0.2305, d3 tf1→0.1249, with the note 'd4 has 2.3x the raw count of d1 but only ~25% more weight — log damping.' Panel 2, Vector space + cosine: query and doc drawn as two arrows from the origin in a term-space, the angle between them highlighted; formula cos = q·d/(|q||d|); worked example q=(1,1,0), d=(3,0,4) → 0.4243; caption 'cosine compares DIRECTION not size = length normalization, so a long doc cannot win just by being long.' Panel 3, Jaccard: two overlapping circles (query set, doc set), J = |∩|/|∪|; worked example {fast,scalable,search} vs {scalable,search,engine,index} = 2/5 = 0.4; a red note lists its three blind spots: ignores term frequency, ignores IDF, hurt by length — 'good for dedup, weak for ranking.' Panel 4, BM25 (highlighted as 'the production default'): the formula with k1 and b annotated — k1 = TF saturation (curve flattening to a ceiling), b = length normalization; the saturation table f=1→1.00, f=2→1.375, f=5→1.774, f=20→2.075 toward the ceiling 2.2; caption 'keeps TF-IDF's instincts, fixes its flaws (unbounded tf, ad-hoc length) inside the formula.'" width="1180">

> **Memory hook:** *ranking turns a match into a score. TF-IDF = (1+log tf) × log(N/df): frequent-here but rare-everywhere. Make docs/queries tf-idf vectors and score by COSINE (angle, not size = length normalization). Jaccard = |∩|/|∪| is cheap set overlap, great for dedup but a weak ranker (no tf, no idf, length-sensitive). BM25 is the production default: TF-IDF plus built-in tf saturation (k₁) and length normalization (b).*

---

## Making search forgiving: relevance techniques

**Question: real users type "fast scalabe serch", forget spaces in "mcdonalds", and spell "Schwarzenegger" six different ways. Exact term matching against the index would return nothing. What's the bag of techniques that turns a brittle exact-match index into something that feels like it *understands* you?**

These sit mostly in the query-analysis stage, and each one is a different failure of "the query terms exactly equal the index terms."

- <span style="color:#8aff8a"><strong>Weighted field scoring (boosting).</strong></span> A match in the **title** means more than a match in the **body**. So index per-field and give fields different weights — a hit in the title contributes, say, 3× a hit in the body. (This is why posting lists carry field info.) The same idea boosts recent or popular documents.
- <span style="color:#ffd27f"><strong>Fuzzy search via edit distance and BK-trees.</strong></span> "serch" isn't a term, but "search" is one edit away. <span style="color:#ffff99"><strong>Levenshtein distance</strong></span> counts the single-character insertions, deletions, and substitutions to turn one word into another — `lat → cat` is distance 1 (substitute l→c), `lat → late` is distance 1 (insert e). To find *all dictionary words within distance k* without comparing against the entire vocabulary, you use a <span style="color:#93c5fd"><strong>BK-tree</strong></span>: a tree keyed on edit distance that exploits the **triangle inequality**. At a node `u`, you compute `d = dist(query, u)` and then only need to descend into children whose edge label lies in `[d−k, d+k]` — pruning away most of the dictionary. This is how a search box offers "did you mean…" fast.
- <span style="color:#ffd27f"><strong>Spell correction.</strong></span> "HOUPE" → "HOUSE", then search the *corrected* term. Generate candidates within edit distance 1–2, then pick the best by a simple probabilistic model: `P(correction) × P(typo | correction)` — i.e. favor corrections that are *common words* and *likely typos* of what was typed (Norvig's classic spell-corrector). The correction happens before index lookup, so the user never sees the miss.
- <span style="color:#8aff8a"><strong>Synonyms / query expansion.</strong></span> "HOME" and "HOUSE" should match each other; "car" should find "automobile". Maintain a synonym dictionary and **expand** the query so it matches documents containing *any* of the synonyms. (This patches the "vocabulary mismatch" problem lexically; embeddings in [the ML post](28-information-retrieval-ml-ranking.md) solve it statistically.)
- <span style="color:#b79bff"><strong>Phonetic matching (Soundex, Metaphone).</strong></span> Here's the deep one. People often **don't know the spelling but remember the sound**, so their typos resemble the *pronunciation*: "vedio/video/vdio", "Arnold Swarzeneger/Swazeneger/Schwarzenegger". Edit distance *struggles* with these — the misspellings diverge too many characters from the correct spelling to be within a small `k`. The fix is to map every word to a **phonetic code** so words that *sound alike collide*. <span style="color:#ffff99"><strong>Soundex</strong></span> keeps the first letter, encodes the consonants by sound-group (b/f/p/v → 1; c/g/j/k/q/s/x/z → 2; d/t → 3; l → 4; m/n → 5; r → 6), drops vowels, collapses duplicates, and truncates to a letter + 3 digits — so "Robert" and "Rupert" both become **R163** and match. <span style="color:#ffff99"><strong>Metaphone</strong></span> / Double Metaphone are more accurate models of English pronunciation. You index the phonetic code alongside the term; a query that *sounds* right finds the document even when every letter is wrong. **This is the case fuzzy search cannot solve, because it's solving for sound, not spelling.**
- <span style="color:#8aff8a"><strong>Query segmentation.</strong></span> People forget spaces: "mcdonalds" vs "mc donalds", "newyork" vs "new york". The query processor re-segments — splitting or joining tokens — so the index lookup matches regardless. (This is why query analysis is its own step, not just "lowercase and split.")

The throughline: **every technique repairs a specific mismatch between what the user typed and what's in the index** — wrong field weight, a typo, a misspelling, a different word, a phonetic guess, a missing space — *before* the lookup, so the brittle exact-match core gets a clean, normalized, forgiving query. (Christopher Manning's *Introduction to Information Retrieval* is the canonical deep reference for all of these.)

<img src="../assets/information-retrieval-search-engine/relevance-techniques.svg" alt="Six relevance techniques, each shown as 'the mismatch it repairs', arranged around the query-analysis stage. 1) Weighted field scoring: a title-hit and a body-hit on a scale, title weighted 3x heavier; 'a match in the title means more than in the body.' 2) Fuzzy search / BK-tree: the word 'serch' one edit from 'search' (Levenshtein: lat→cat = 1 substitution, lat→late = 1 insertion); a small BK-tree keyed on edit distance with the note 'triangle inequality: at a node compute d, descend only into children with label in [d−k, d+k] — prunes the dictionary; powers did-you-mean.' 3) Spell correction: HOUPE → HOUSE before lookup, 'pick candidate by P(word)×P(typo|word) — Norvig.' 4) Synonyms / query expansion: HOME = HOUSE, car = automobile; expand query to match docs with EITHER word. 5) Phonetic (Soundex/Metaphone), drawn largest as the key insight: 'people don't know the spelling but remember the SOUND, so typos resemble the sound'; the examples Swarzeneger/Swazeneger/Schwarzenegger and vedio/video/vdio all collapsing to one phonetic code; a Soundex worked example Robert→R163 and Rupert→R163 (match!); a red note 'fuzzy/edit-distance CANNOT solve this — the spellings diverge too far; phonetic codes collapse them by sound.' 6) Query segmentation: 'mcdonalds' ↔ 'mc donalds', 'newyork' ↔ 'new york' — re-split tokens so lookup matches. Bottom banner: every technique repairs a specific query↔index mismatch BEFORE lookup, so the brittle exact-match core sees a clean, forgiving query." width="1180">

> **Memory hook:** *forgiveness lives in query analysis, each technique fixing one query↔index mismatch: weighted fields (title>body), fuzzy/BK-tree+Levenshtein (typos: serch→search), spell correction (HOUPE→HOUSE, Norvig P(word)×P(typo)), synonyms/expansion (home=house), phonetic Soundex/Metaphone (people remember the SOUND — Robert & Rupert → R163; the case fuzzy can't solve), query segmentation (mc donalds → mcdonalds). Repair before lookup.*

---

## Evaluation: how do you know the search is any good?

**Question: you changed the ranking and it "feels better." Prove it. How do you put a number on search quality so you can compare two rankers?**

You need labeled data — a set of queries, and for each, human judgments of which documents are relevant (binary: relevant/not, or graded: 0–3). Then you measure the ranking against those judgments. The metrics fall into two families.

### Set-based (did we return the right documents at all?)

For a query with TP relevant-and-returned, FP irrelevant-and-returned, FN relevant-but-missed:

- <span style="color:#8aff8a"><strong>Precision</strong></span> = TP / (TP + FP) — of what we returned, how much was right.
- <span style="color:#ffd27f"><strong>Recall</strong></span> = TP / (TP + FN) — of all the right answers, how many we found.
- <span style="color:#93c5fd"><strong>F1</strong></span> = harmonic mean of the two = 2PR / (P + R).

**Worked example.** You return 5 documents; 3 are relevant (TP=3, FP=2), and 3 relevant ones were missed (FN=3). Then P = 3/5 = **0.60**, R = 3/6 = **0.50**, F1 = 2(0.6)(0.5)/(1.1) = **0.5455**. **Precision@k** and **Recall@k** restrict this to the top k — what's on page one.

### Rank-aware (did we put the best documents *highest*?)

Set metrics ignore *order*, but order is everything in search. These reward putting good results near the top:

- <span style="color:#ffd27f"><strong>MRR (Mean Reciprocal Rank)</strong></span> — for tasks with one right answer (known-item, QA), score each query by 1/(rank of the first relevant result), and average. First-relevant at ranks 1, 3, 2 across three queries → (1 + 1/3 + 1/2)/3 = **0.611**. *How high is the first good hit?*
- <span style="color:#ffd27f"><strong>MAP (Mean Average Precision)</strong></span> — average the precision computed at each rank where a relevant document appears, then average over queries. For a ranked relevance pattern [1, 0, 1, 1, 0, 1] with 4 relevant docs: precision at the relevant ranks (1, 3, 4, 6) is (1.000 + 0.667 + 0.750 + 0.667), averaged over 4 = **0.7708**.
- <span style="color:#8aff8a"><strong>NDCG (Normalized Discounted Cumulative Gain)</strong></span> — the metric for *graded* relevance, and the one modern ranking optimizes. **DCG** sums each result's relevance discounted by a log of its position (a great result at rank 1 counts fully; at rank 6 it's discounted), then **normalize** by the best-possible ordering (IDCG) so the score is 0–1 and comparable across queries.

**NDCG worked example.** A ranking with graded relevances [3, 2, 3, 0, 1, 2], using `DCG = Σ relᵢ / log₂(i+1)`:

```
DCG  = 3/log₂2 + 2/log₂3 + 3/log₂4 + 0/log₂5 + 1/log₂6 + 2/log₂7
     = 3.000 + 1.262 + 1.500 + 0.000 + 0.387 + 0.713  = 6.861

Ideal order [3,3,2,2,1,0]:
IDCG = 3.000 + 1.893 + 1.000 + 0.861 + 0.387 + 0.000   = 7.141

NDCG = 6.861 / 7.141 = 0.961
```

A 0.96 says this ranking is close to ideal. (Caveat worth knowing: there are two DCG conventions — the linear `relᵢ` above, and an exponential `2^relᵢ − 1` that weights highly-relevant documents far more heavily; libraries differ, so always state which.)

<img src="../assets/information-retrieval-search-engine/evaluation-metrics.svg" alt="Search evaluation metrics in two groups. Left group, set-based (order-blind): a confusion-style diagram with TP/FP/FN; Precision = TP/(TP+FP) 'of what we returned, how much was right', Recall = TP/(TP+FN) 'of all right answers, how many we found', F1 = harmonic mean; worked example return 5, TP=3 FP=2 FN=3 → P=0.60, R=0.50, F1=0.5455; note 'Precision@k / Recall@k = restrict to top-k (page one).' Right group, rank-aware (order matters), shown as a ranked list of results colored by relevance: MRR = 1/rank of first relevant, example ranks 1,3,2 → 0.611, 'one-right-answer tasks'; MAP = average precision at the relevant ranks, example [1,0,1,1,0,1] → 0.7708; NDCG highlighted as 'the modern default for graded relevance' — a ranked list with grades [3,2,3,0,1,2], each grade discounted by log2(position+1) to give DCG=6.861, divided by the ideal ordering [3,3,2,2,1,0] IDCG=7.141 → NDCG=0.961, with a small bar chart showing the position discount shrinking with rank. Bottom note: set metrics ask 'right documents?', rank-aware metrics ask 'best documents highest?' — search lives and dies on the latter. Caveat: two DCG conventions (linear rel vs exponential 2^rel−1) — state which." width="1180">

> **Memory hook:** *to prove search quality you need judged queries. Set-based (order-blind): Precision=TP/(TP+FP), Recall=TP/(TP+FN), F1=harmonic mean; @k = top-k. Rank-aware (order matters): MRR=1/rank-of-first-hit (one-answer tasks), MAP=avg precision at relevant ranks, NDCG=position-discounted graded relevance normalized to ideal (the modern default, ~0.96 = near-ideal). Search lives on the rank-aware family.*

---

## The full architecture

**Question: assemble the whole machine. Trace a page from the crawler all the way to a user's ranked results.**

<img src="../assets/information-retrieval-search-engine/full-architecture.svg" alt="The complete text search engine architecture, split into a write loop and a read loop meeting at the index. Top (pink, the ingest/WRITE loop): seed URLs → URL Frontier (priority + politeness) → Crawler/Renderer (robots.txt cache, dedup via Bloom filter + SimHash) → extracted links loop back to the Frontier; page content → Indexer running the analysis pipeline (parse→tokenize→normalize→stop-words→stem) → INVERTED INDEX in the center, drawn as sharded immutable segments (term dictionary → sorted posting lists with docID+tf+positions; delta-encoded; background merge). A label: 'CQRS seam — writes append segments, reads query them.' Bottom (green, the query/READ loop): user query → Query Service (same analysis pipeline + spell correction + synonym expansion + query segmentation + phonetic) → fan out to shards → dictionary lookup + merge posting lists (two-pointer AND, skip pointers) → Stage A retrieve with BM25 → top-k candidates → Stage B re-rank (link analysis/PageRank, field boosts, freshness; full ML re-ranking deferred to the ML post) → coordinator merges per-shard top-k, paginates → ranked results to the user. A right-side strip maps each concern to its mechanism: SPEED = inverted index + sorted-list merge + skip pointers; FORGIVENESS = spell/fuzzy/synonym/phonetic/segmentation in query analysis; RELEVANCE = TF-IDF→cosine→BM25 then re-rank; QUALITY MEASURED BY = precision/recall, MAP, MRR, NDCG; SCALE = sharded immutable segments + crawl budget. Legend: pink = ingest/write path, green = query/read path, yellow = the index." width="1200">

Read it as the two loops meeting at the index:

- **Ingest (pink).** The crawler, driven by the frontier, fetches pages politely, dedups them, and hands their text to the indexer, which runs the analysis pipeline and appends postings to **immutable, sharded segments** of the inverted index. Asynchronous; nobody waits on it.
- **Query (green).** A user's query runs the *same* analysis pipeline plus the forgiveness layer (spell correction, synonyms, phonetic, segmentation), fans out to the shards, looks up and merges posting lists with two-pointer intersection and skip pointers, scores candidates with **BM25 (Stage A retrieve)**, re-ranks the top few with richer signals like PageRank and field boosts **(Stage B)**, and the coordinator merges, paginates, and returns. Sub-second.
- **The index is the seam.** Writes append; reads query; the two never block each other. That CQRS split, plus the inverted index, plus precomputing everything expensive at index time, is *why* a query over billions of documents returns in a few hundred milliseconds.

Every choice traces back to the opening idea: **search a structure built from the documents, not the documents themselves** — and spend all your cleverness at index time so the query path stays cheap.

> **Memory hook:** *full system — ingest loop (crawl→dedup→analyze→append to sharded immutable segments) and query loop (analyze+forgive→fan out→lookup+merge→BM25 retrieve→re-rank→paginate) meet at the inverted index (the CQRS seam). Speed from the index + sorted-list merge; forgiveness from query analysis; relevance from TF-IDF/cosine/BM25 + re-rank; quality measured by NDCG & friends. Cleverness at index time, cheap queries.*

---

## Where this leaves us — and the ML half

You can now design a classical search engine end to end and defend every component: why the inverted index exists, what a posting holds, how crawling feeds it without slowing queries, how the read path merges sorted lists instead of scanning, how TF-IDF → cosine → BM25 turn a match into a ranked score, how the forgiveness layer rescues real-world queries, and how NDCG and friends let you *prove* one ranker beats another.

This is not "old search" in the sense of being obsolete. It is the **production floor**. If you were building a serious search system today, you would still start with the inverted index, analyzers, filters, facets, access control, and BM25 because that layer is fast, debuggable, and excellent at exact terms, IDs, names, product codes, and structured filtering. The ML half does not replace this machine; it sits on top of it.

But notice the seams where the classical machine strains:

- **BM25 is lexical.** It matches *words*, so "car" and "automobile" are strangers unless a human wrote a synonym rule. The deeper fix is **embeddings** — representing meaning as vectors so semantically-close queries and documents land near each other even with zero shared words.
- **Re-ranking was hand-waved.** "Combine PageRank, freshness, field boosts" — *how*, exactly, and with what weights? That's not a formula you tune by hand across hundreds of signals; it's a model you *train*. That's **learning to rank**.
- **The two-stage funnel begs for a second stage.** Cheap BM25 retrieval narrowing to a small candidate set, then an expensive, accurate model re-scoring just those — the expensive model is increasingly a **neural cross-encoder**.

All three are the **machine-learning layer of information retrieval**, and they're where modern search actually gets its quality. The production answer is not "BM25 or vectors?" It is usually **BM25 + dense retrieval in parallel, fused, then re-ranked**. That's the next post: [Machine Learning in Information Retrieval](28-information-retrieval-ml-ranking.md) — learning-to-rank (pointwise/pairwise/listwise, LambdaMART), word and document embeddings, dense retrieval with bi-encoders and cross-encoders, approximate-nearest-neighbor vector search, hybrid lexical-plus-semantic ranking, and the production blueprint for what you would build today.

> **Memory hook:** *the classical engine is the production floor, not a toy: inverted index + analyzers + filters + BM25 are still the dependable base. It strains at three seams — lexical-only matching (→ embeddings), hand-tuned re-ranking (→ learning to rank), and the second stage of the funnel (→ neural cross-encoders). Modern production search usually keeps BM25 and adds dense retrieval, fusion, and reranking on top.*
