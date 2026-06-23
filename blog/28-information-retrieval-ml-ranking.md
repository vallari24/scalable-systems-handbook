# Machine Learning in Information Retrieval: Learning to Rank, Embeddings, and Neural Search

The [classical search engine](27-information-retrieval-search-engine.md) is a beautiful machine — an inverted index, sorted-list merges, BM25 — but it has a ceiling, and the ceiling is that **it matches words, not meaning, and it ranks by formulas a human tuned by hand.** This post is about the layer that breaks through that ceiling: the machine learning that powers modern relevance. It's the half of information retrieval that turns "documents containing your words, scored by BM25" into "documents that *answer* you, ranked by a model trained on what people actually found useful." We'll build it in the order it actually slots into a real engine: the **retrieve-then-rerank funnel** that makes expensive models affordable; **learning to rank**, where the ranking function stops being a formula and becomes a trained model; **embeddings**, where words and documents become vectors of *meaning* so "car" finds "automobile"; **dense retrieval** with bi-encoders and cross-encoders; the **approximate-nearest-neighbor** infrastructure that makes vector search fast; and **hybrid** search that fuses the lexical and the semantic. By the end you should understand not just *what* each piece does but *why* it sits where it does in the pipeline.

**Question: classical search matches the literal words you typed and orders results with a fixed formula (BM25 plus a few hand-weighted signals). But "cheap flights to the big apple" should find a page about *inexpensive airfare to New York City* that may share almost no words with the query, and the *right* way to combine hundreds of ranking signals isn't a formula anyone can hand-tune. So how does machine learning enter a search engine — where exactly does it plug in, what does it learn from, and why is the architecture a two-stage funnel rather than one big neural net scoring everything?** The answer is a single, repeating shape: **a cheap, recall-oriented stage narrows billions of documents to a few hundred candidates, and an expensive, precision-oriented learned model re-scores just those.** Every ML technique in this post — learning to rank, cross-encoders, dense retrieval — lives at a specific point on that funnel, and understanding *why each is cheap or expensive* is understanding the whole design.

This post is the second half of an information-retrieval pair. The first, [How a Text Search Engine Works](27-information-retrieval-search-engine.md), built the classical engine — inverted index, crawling, the read path, TF-IDF/cosine/BM25, the forgiveness layer, and the NDCG-family metrics — and ended by naming the three seams where the classical machine strains: lexical-only matching, hand-tuned re-ranking, and an empty second stage in the funnel. Those three seams are exactly what this post fills. If the relevance math or the metrics here feel unfamiliar, that post is the prerequisite.

> **Memory hook:** *ML in IR fills three seams in the classical engine: lexical matching (→ embeddings for meaning), hand-tuned ranking (→ learning to rank), and the funnel's second stage (→ neural re-rankers). The unifying shape: cheap recall-oriented retrieval narrows to a candidate set, expensive learned precision re-ranks it.*

---

## The retrieve-then-rerank funnel: where ML plugs in

**Question: why not just run one powerful neural model that reads every document and scores it for the query? Why split the work into stages at all?**

Because the math doesn't allow it. A modern relevance model — a transformer reading the query and a document *together* — can take milliseconds to tens of milliseconds per document, depending on model size, document length, hardware, batching, and serving tricks. Run that across a billion-document index, per query, and you've blown the latency budget by many orders of magnitude. So search is built as a **funnel**, and ML enters at a specific tier:

- <span style="color:#8aff8a"><strong>Stage 1 — Retrieval (cheap, recall-oriented).</strong></span> Out of *all* documents, cheaply pull a **candidate set** (the top few hundred to few thousand) that are *plausibly* relevant. The job here is recall: don't miss the good ones. Classical [BM25 over an inverted index](27-information-retrieval-search-engine.md) does this; so does a **dense retriever** (later in this post). The defining property is that it can use an index or precomputed representation so it does not linearly score every document.
- <span style="color:#b79bff"><strong>Stage 2 — Re-ranking (expensive, precision-oriented).</strong></span> Now run the heavy, accurate model on *only* the few hundred survivors. The job here is precision: get the order of the top results exactly right, because that's all the user sees. This is where **learning-to-rank models** and **cross-encoders** live — they're far too slow to run on the whole corpus, but on a few hundred candidates they're cheap.

The candidate-set size N is the **dial that trades latency for quality**: bigger N means the good documents are less likely to be missed in Stage 1, but more work for Stage 2. The entire art is making Stage 1 recall-complete enough that the right answer is *in* the candidate set, and Stage 2 accurate enough to lift it to the top.

<img src="../assets/information-retrieval-ml-ranking/retrieve-rerank-funnel.svg" alt="The retrieve-then-rerank funnel drawn as a downward-narrowing funnel. At the wide top, 'Billions of documents.' Stage 1 (green, labelled 'Retrieval — cheap, high recall'): a wide band narrowing to a few hundred candidates, methods listed 'BM25 over inverted index, OR dense retriever (bi-encoder + ANN)'; annotation 'uses an index and avoids a full scan; job = do not miss the good ones.' Stage 2 (purple, labelled 'Re-ranking — expensive, high precision'): the few-hundred candidates narrowing to a ranked top-10, methods listed 'learning-to-rank model (LambdaMART), neural cross-encoder (BERT)'; annotation 'runs only on the survivors; job = make the top order as accurate as possible.' A vertical dial on the side labelled 'candidate-set size N = latency vs quality trade-off.' Caption at the bottom: you cannot run an expensive model on a billion docs per query — so narrow cheaply, then re-score precisely. Every ML technique in this post lives at one of these two tiers." width="1100">

> **Memory hook:** *search is a funnel because you can't run a heavy model on a billion docs per query. Stage 1 = cheap recall-oriented retrieval (BM25 or dense) narrows to a few hundred candidates; Stage 2 = expensive precision-oriented re-ranking (learning-to-rank, cross-encoder) re-scores only those. Candidate-set size N is the latency↔quality dial. Every ML method sits at one tier.*

---

## Learning to rank: when the ranking function is trained, not written

**Question: classical ranking combines signals — BM25, PageRank, freshness, field boosts — but *how*? With what weights? Hand-tuning weights across a handful of signals is already hard; across the hundreds of signals a real engine uses it's impossible. What replaces the hand-tuned formula?**

A **trained model**. Learning to rank (LTR) treats ranking as a supervised ML problem: learn, from examples of good and bad rankings, the function that orders documents for a query.

### What it learns from, and on what

- <span style="color:#ffd27f"><strong>Training data</strong></span> — query–document pairs with **relevance labels**. Labels come from human judges (often graded, such as 0–4), or, at scale, from **implicit feedback** — clicks, dwell time, skips, reformulations — treated as noisy relevance signals.
- <span style="color:#93c5fd"><strong>Features</strong></span> — each query–document pair is turned into a feature vector: *query–document* features (BM25 score, TF-IDF, count of matched terms, are they in the title?), *document* features (PageRank, length, freshness, spam score), and *query* features (length, has-an-entity, predicted intent). A production model can have **hundreds** of these.
- <span style="color:#8aff8a"><strong>Objective</strong></span> — the model should produce an ordering that scores well on a ranking metric, and the natural target is [NDCG](27-information-retrieval-search-engine.md) — graded relevance, position-discounted.

The interesting design question is *what you train the model to optimize*, and there are three families.

### Pointwise, pairwise, listwise

- <span style="color:#93c5fd"><strong>Pointwise.</strong></span> Treat each document independently: predict an absolute relevance score (regression) or class, then sort by it. Simple — it's just regression/classification — but it learns nothing about *order*; it never sees two documents side by side, so it can't directly learn "this one should come *above* that one."
- <span style="color:#ffd27f"><strong>Pairwise.</strong></span> Look at *pairs*: given two documents for a query where one is more relevant, learn to score the better one higher. The classic is **RankNet** — model `P(dᵢ ranks above dⱼ) = sigmoid(sᵢ − sⱼ)` and minimize cross-entropy against the true pair order. This optimizes *relative* order, which is what ranking actually is, and it's a big step up from pointwise. Its flaw: it treats every mis-ordered pair as equally bad, when in truth swapping the #1 and #2 results matters far more than swapping #500 and #501.
- <span style="color:#8aff8a"><strong>Listwise / metric-aware.</strong></span> Optimize the quality of the *whole ranked list*, or use pairwise gradients weighted by the change in a list metric such as NDCG. The long-running workhorse is [**LambdaMART**](https://www.microsoft.com/en-us/research/publication/from-ranknet-to-lambdarank-to-lambdamart-an-overview/): it scales each pairwise gradient (the "lambda") by **how much NDCG would change if you swapped that pair**, so the model spends its effort where it moves the metric — the top of the list. LambdaMART = these lambda-gradients on top of **MART** (gradient-boosted regression trees); an ensemble of LambdaMART rankers won Track 1 of the 2010 Yahoo Learning-to-Rank Challenge. (ListNet/ListMLE are neural listwise cousins that model a probability distribution over orderings.)

The progression is a clean story: **pointwise ignores order, pairwise learns relative order, listwise or metric-aware methods optimize closer to the thing you actually measure** — each fixes the previous one's blind spot. In practice, strong production systems usually combine this ranking objective with feature engineering, fast serving constraints, and online experiments.

<img src="../assets/information-retrieval-ml-ranking/learning-to-rank.svg" alt="Learning to rank shown as training data feeding three approaches. Left: the setup — a query plus a list of documents, each turned into a feature vector (BM25 score, PageRank, freshness, title-match, ...) with a relevance label (graded 0–4 from human judges or from clicks); objective labelled 'optimize NDCG.' Right: three stacked panels comparing the families. Pointwise (blue): each document scored independently by a regression model, then sorted; tag 'simple, but never compares two docs → learns nothing about order.' Pairwise (yellow): two documents fed as a pair, RankNet learns P(di>dj)=sigmoid(si−sj) with cross-entropy; tag 'learns relative order, but treats a top-of-list swap the same as a bottom-of-list swap.' Metric-aware LambdaMART (green, highlighted as 'common workhorse'): pairwise gradients are scaled by ΔNDCG of swapping the pair, on gradient-boosted trees (MART); tag 'spends effort where it moves the metric — the top; won the 2010 Yahoo LTR challenge.' Bottom banner: pointwise ignores order → pairwise learns relative order → metric-aware ranking targets the actual metric; each fixes the prior blind spot." width="1180">

> **Memory hook:** *learning to rank replaces the hand-tuned formula with a model trained on query-doc pairs labelled by judges or clicks, over hundreds of features, optimizing NDCG. Three families: pointwise (regression per doc, ignores order), pairwise (RankNet, learns relative order via sigmoid(sᵢ−sⱼ) but weights all swaps equally), listwise (LambdaMART scales each gradient by ΔNDCG → focuses on the top; the workhorse).*

---

## Embeddings: representing meaning so "car" finds "automobile"

**Question: BM25 and the inverted index match *tokens*. "car" and "automobile" share zero characters, so to a lexical engine they're unrelated — the dreaded *vocabulary mismatch*. Synonym lists patch a few cases by hand. What's the principled fix that makes the machine understand that two different words mean the same thing?**

Represent words (and sentences, and documents) as **dense vectors of real numbers — embeddings — positioned so that things with similar *meaning* sit close together in the vector space.** Meaning becomes geometry, and "close in meaning" becomes "close in cosine distance," whether or not the words share any letters.

- <span style="color:#b79bff"><strong>Word embeddings (word2vec, GloVe).</strong></span> Train a model on a huge text corpus on a self-supervised task: [**word2vec**](https://arxiv.org/abs/1301.3781) slides a window over text and either predicts a word from its neighbors (CBOW) or the neighbors from the word (skip-gram). Words that appear in similar contexts get similar vectors — so "car", "automobile", and "vehicle" tend to cluster together because they're used in related contexts. [**GloVe**](https://nlp.stanford.edu/projects/glove/) reaches a similar place by factorizing a global word co-occurrence matrix. The classic `vec("king") − vec("man") + vec("woman") ≈ vec("queen")` example is an intuition pump, not a guarantee: embedding spaces encode many relationships, but the exact arithmetic depends on the model and corpus.
- <span style="color:#93c5fd"><strong>Sentence and document embeddings.</strong></span> The same idea scales up: encode a whole query or document into a single vector. Modern encoders are often transformer-based, such as [Sentence-BERT](https://arxiv.org/abs/1908.10084). Now "cheap flights to the big apple" and "inexpensive airfare to NYC" — barely any shared words — can land *near each other* in the space, and a nearest-neighbor lookup can find one from the other.

This is the principled cure for vocabulary mismatch: instead of enumerating synonyms by hand, you *learn* a geometry where synonyms, paraphrases, and related concepts are automatically neighbors. It's what makes **semantic search** possible — searching by meaning rather than by string.

<img src="../assets/information-retrieval-ml-ranking/embeddings-semantic.svg" alt="Embeddings and semantic search in two panels. Left, the problem: a lexical engine with 'car' and 'automobile' as two unrelated tokens (zero shared characters), an X marking 'vocabulary mismatch — BM25 sees no overlap'; a hand-written synonym list patching a few cases, tagged 'doesn't scale.' Right, the fix: a 2-D vector space (embedding space) with clusters — 'car / automobile / vehicle' sitting close together, 'cheap flights to the big apple' and 'inexpensive airfare to NYC' as two points near each other despite different words, and a separate distant cluster 'banana / fruit'; arrows show cosine distance = closeness of meaning. An inset illustrates the analogy vec(king) − vec(man) + vec(woman) ≈ vec(queen) as vector arithmetic. Caption: train word2vec (skip-gram/CBOW) or GloVe on a huge corpus — words used in similar contexts get similar vectors; meaning becomes geometry, so 'close in meaning' = 'close in the space' even with no shared letters. This is semantic search: search by meaning, not by string." width="1180">

> **Memory hook:** *embeddings fix vocabulary mismatch ('car' vs 'automobile') by mapping words/sentences/docs to dense vectors positioned by meaning — similar meaning = near in cosine space. word2vec (skip-gram/CBOW) and GloVe learn this self-supervised from context; the king−man+woman≈queen analogy shows the space encodes relationships. Sentence/doc embeddings enable semantic search: search by meaning, not string.*

---

## Dense retrieval: bi-encoders and cross-encoders

**Question: embeddings give you vectors of meaning — but how do you actually *retrieve* with them, and where does the heavy neural model go? There seem to be two ways to score a (query, document) pair with a transformer, and they have wildly different costs.**

Exactly two, and the difference between them *is* the architecture of modern neural search.

- <span style="color:#8aff8a"><strong>Bi-encoder (dual encoder) — for retrieval.</strong></span> Encode the query and each document **separately**, into independent vectors, and score by their cosine/dot-product similarity. The crucial property: because the encoders are independent, you can **embed every document offline, ahead of time**, and store the vectors in an index. At query time you embed only the query (once) and find its nearest document vectors. This is **dense retrieval** — [DPR (Dense Passage Retrieval)](https://arxiv.org/abs/2004.04906) is a canonical example. It's fast and can scale to very large corpora because the expensive document encoding is precomputed; it's trained with a contrastive loss that pulls a query toward its relevant documents and pushes it from irrelevant ones.
- <span style="color:#b79bff"><strong>Cross-encoder — for re-ranking.</strong></span> Feed the query and the document **together** into one transformer, such as [BERT](https://arxiv.org/abs/1810.04805) — `[CLS] query [SEP] document [SEP]` — and let every query token attend to every document token, then read a relevance score off the top. This *joint* attention is usually more accurate for fine relevance judgments, because the model sees how the query and document interact term by term. But there's no shortcut: the document's representation depends on the query, so **nothing can be precomputed** — you must run the full transformer for *every* (query, document) pair at query time. That's affordable on a few hundred candidates, but not viable across a billion-document corpus per query.

And now the two-stage funnel snaps into focus: <span style="color:#ffff99"><strong>retrieve with the bi-encoder (fast, over everything), re-rank with the cross-encoder (accurate, over the survivors).</strong></span> The bi-encoder's precomputability buys recall over the whole corpus; the cross-encoder's joint attention buys precision over the short list. Google publicly described [BERT in Search](https://blog.google/products-and-platforms/products/search/search-language-understanding-bert/) in 2019 as a language-understanding improvement for ranking and featured snippets, and later said BERT was used in almost every English query. [**ColBERT**](https://arxiv.org/abs/2004.12832) is a notable middle ground — it stores *per-token* embeddings and does a cheap "late interaction" max-similarity at query time, recovering much of the cross-encoder's token-level interaction while keeping most of the bi-encoder's precomputability.

<img src="../assets/information-retrieval-ml-ranking/bi-vs-cross-encoder.svg" alt="Bi-encoder versus cross-encoder, side by side, mapped onto the funnel. Left, Bi-encoder (green, 'for RETRIEVAL'): two separate encoder towers — one encodes the query into a vector, one encodes each document into a vector — with a similarity (cosine/dot) computed between the two output vectors; a clock icon shows 'all document vectors precomputed OFFLINE and indexed'; at query time only the query is encoded then nearest-neighbor searched. Tags: 'fast, can scale very large because doc encoding is precomputed; less accurate (query and doc never interact). DPR.' Right, Cross-encoder (purple, 'for RE-RANKING'): a single encoder tower taking [CLS] query [SEP] document [SEP] together, every query token attending to every document token, outputting one relevance score. Tags: 'high accuracy from full joint interaction; nothing can be precomputed — must run the transformer per (query,doc) pair at query time → only affordable on a few hundred candidates. BERT.' Center arrow ties them together: 'retrieve with bi-encoder over everything → re-rank with cross-encoder over the survivors.' A small footnote box: ColBERT = late-interaction middle ground (per-token embeddings + MaxSim)." width="1180">

> **Memory hook:** *two ways to score (query,doc) with a transformer. Bi-encoder: encode query and doc separately → similarity of vectors; docs precomputed offline → fast, can scale very large, used for RETRIEVAL (DPR). Cross-encoder: encode query+doc jointly so tokens interact → high accuracy but nothing precomputable, must run per pair → only on a few hundred, used for RE-RANKING (BERT). The funnel = bi-encoder retrieve + cross-encoder rerank. ColBERT = late-interaction middle ground.*

---

## Vector search infrastructure: approximate nearest neighbors

**Question: dense retrieval needs to find a query vector's nearest neighbors among *billions* of document vectors. Comparing against every vector is another O(n) scan — the exact thing the inverted index saved us from. How do you make vector search sublinear?**

You give up *exactness* for speed — **approximate nearest neighbor (ANN)** search, which tries to find near-enough neighbors while avoiding most vector comparisons. The recall/latency tradeoff is tunable, and different indexes make different tradeoffs. Two widely used families:

- <span style="color:#93c5fd"><strong>Graph-based — HNSW.</strong></span> [Hierarchical Navigable Small World](https://arxiv.org/abs/1603.09320) graphs connect each vector to its near neighbors in a multi-layer graph; a search greedily "walks" toward the query, dropping through layers from coarse to fine. HNSW is widely used because it gives strong recall/latency behavior in many practical workloads.
- <span style="color:#ffd27f"><strong>Partition-based — IVF (+ PQ).</strong></span> Inverted File clusters the vectors and, at query time, only searches the few nearest clusters (an *inverted index for vectors* — the same idea, one level up). **Product Quantization** compresses each vector into a few bytes so billions fit in RAM, trading a little accuracy for a huge memory saving.

These live in **vector databases / libraries** — [FAISS](https://arxiv.org/abs/1702.08734), HNSWlib, Milvus, Weaviate, pgvector — which are to dense retrieval what the inverted index is to lexical search: the structure that lets Stage-1 retrieval avoid a full scan. The conceptual parallel is exact: *both* the inverted index and the ANN index exist so that Stage-1 retrieval does less work than "compare the query with everything."

> **Memory hook:** *dense retrieval needs the nearest of billions of vectors without an O(n) scan — so use approximate nearest neighbor (ANN): HNSW (multi-layer proximity graph, greedy walk) or IVF+PQ (cluster, search nearest clusters, quantize to compress). Vector DBs (FAISS, Milvus, pgvector) are to dense retrieval what the inverted index is to lexical search — the structure that lets Stage 1 avoid a full scan.*

---

## Hybrid search: fuse the lexical and the semantic

**Question: dense retrieval understands meaning but can fumble exact tokens — a product code "RTX-4090", a rare surname, an exact quoted phrase — precisely where BM25 shines. BM25 nails exact terms but is blind to meaning. Why choose? Can you have both?**

You can, and you usually should — **hybrid search** runs both retrievers and **fuses** their results:

- <span style="color:#8aff8a"><strong>Lexical (BM25)</strong></span> is strongest on exact, rare, out-of-vocabulary tokens — IDs, names, codes, exact phrases — where there's no "meaning" to embed, just a string that must match.
- <span style="color:#b79bff"><strong>Dense (bi-encoder)</strong></span> is strongest on paraphrase and concept — the "cheap flights to the big apple" case — where the words differ but the meaning is identical.

They fail in *opposite* situations, which is exactly why combining them often beats either alone. A simple fusion method is <span style="color:#ffff99"><strong>Reciprocal Rank Fusion (RRF)</strong></span>: score each document by `Σ 1 / (k + rankᵢ)` across the two result lists (`k = 60` in the original [SIGIR 2009 paper](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)), rewarding documents that rank highly in *either* retriever. It needs no score calibration between the two systems — it uses only ranks — which is why it's a common baseline/default. The fused candidate set then goes to the cross-encoder re-ranker.

<img src="../assets/information-retrieval-ml-ranking/hybrid-vector-architecture.svg" alt="Hybrid search architecture. A query splits into two parallel retrieval paths. Top path (green), Lexical: query analysis → inverted index → BM25 → a ranked candidate list; labelled 'wins on exact/rare tokens: IDs, names, codes, exact phrases.' Bottom path (purple), Dense: query encoded by a bi-encoder into a vector → ANN index (HNSW / IVF+PQ over precomputed document vectors in a vector DB) → a ranked candidate list; labelled 'wins on meaning/paraphrase: car=automobile, big apple=NYC.' The two candidate lists meet at a Fusion box (yellow): Reciprocal Rank Fusion, score = Σ 1/(k+rank), k≈60, 'rewards docs ranking high in EITHER list, needs no score calibration.' The fused candidate set flows into a Cross-encoder re-ranker (the Stage-2 box) → final ranked top results. A side note: the two retrievers fail in opposite situations, so fusing them beats either alone. Legend ties back to the funnel: green+purple = Stage-1 retrieval (run in parallel), fusion + cross-encoder = the bridge into Stage-2." width="1180">

> **Memory hook:** *hybrid search runs lexical (BM25, wins on exact/rare tokens — IDs, names, phrases) and dense (bi-encoder, wins on meaning/paraphrase) in parallel and fuses them, usually with Reciprocal Rank Fusion (Σ 1/(k+rank), k≈60, rank-only so no calibration). They fail in opposite cases, so the union beats either; the fused candidates then hit the cross-encoder re-ranker.*

---

## The rest of the ML stack, and the modern (LLM) turn

**Question: re-ranking and retrieval are the big two — but ML touches more of the pipeline. What else, and how does all this connect to the LLM era?**

A few more places ML quietly runs the show:

- <span style="color:#93c5fd"><strong>Query understanding.</strong></span> Before retrieval, models classify **intent** (navigational? transactional? a question?), tag **entities** ("apple" the company vs the fruit), do **neural spelling correction**, and **expand** the query. Better query understanding lifts every downstream stage.
- <span style="color:#ffd27f"><strong>Learning from clicks.</strong></span> Human relevance judgments matter, but no large web engine can rely only on judged labels for every query and page. So rankers also learn from **implicit feedback**: clicks, skips, dwell time, reformulations. The catch is **position bias** (people click the top result because it's on top, not because it's best), which is corrected with click models and counterfactual learning before the signal is trusted as a relevance label. This is the flywheel: serve results → log behavior → retrain the ranker → serve better results.
- <span style="color:#b79bff"><strong>Personalization.</strong></span> User and session embeddings nudge ranking toward what *this* user, *right now*, is likely to want.

And the modern turn: **retrieval-augmented generation (RAG)** — the pattern behind LLM systems that answer from your documents — is *built on exactly these blocks*. The original [RAG paper](https://arxiv.org/abs/2005.11401) combined a parametric language model with non-parametric memory retrieved from an external index. In production systems, that "retrieval" step is often a bi-encoder embedding the question, an ANN index finding the nearest document chunks, often hybridized with BM25 and re-ranked with a cross-encoder — the precise funnel of this post — with an LLM added at the end to read the retrieved passages and write an answer. The embeddings-plus-ANN machinery you build for semantic search *is* the retrieval layer of the LLM stack. Information retrieval didn't get replaced by LLMs; it became their foundation.

<img src="../assets/information-retrieval-ml-ranking/full-ml-stack.svg" alt="The full ML-in-IR stack as an end-to-end pipeline, showing where every technique sits. Left: a user query enters Query Understanding (ML: intent classification, entity tagging, neural spell-correct, expansion). It flows into Stage-1 Retrieval drawn as the hybrid funnel — lexical BM25 over the inverted index AND dense bi-encoder over an ANN vector index, fused by Reciprocal Rank Fusion into a few-hundred candidate set. That feeds Stage-2 Re-ranking — a learning-to-rank model (LambdaMART) and/or a neural cross-encoder (BERT) scoring the candidates against hundreds of features, optimizing NDCG — producing the final ranked results. Below, a feedback loop: served results → logged clicks/dwell (with position-bias correction) → retrain the rankers, drawn as a cycle arrow back to the models (the relevance flywheel). On the right, a panel 'Same blocks power RAG / LLM search': the question → bi-encoder + ANN (+ BM25 hybrid) + cross-encoder rerank → retrieved passages → LLM writes the answer, with a caption 'IR became the retrieval foundation of the LLM stack.' A legend maps colors: blue = query understanding, green/purple = retrieval, purple = re-ranking, orange = the click-feedback flywheel." width="1200">

> **Memory hook:** *beyond retrieve+rerank, ML also runs query understanding (intent, entities, neural spell, expansion) and learns from clicks/dwell (correcting position bias) in a retrain flywheel; user/session embeddings personalize. The modern turn: RAG/LLM search is this exact funnel — bi-encoder + ANN (+BM25 hybrid) + cross-encoder rerank → passages → LLM. IR became the retrieval foundation of the LLM stack.*

---

## Current research: where ML search is headed

**Question: if the retrieve-then-rerank funnel is the stable shape, what is changing now?**

The direction is not "one giant neural network scans the web." The direction is **more neural work at every stage, but still inside a staged retrieval system**: better query understanding before retrieval, better candidate generation, better re-ranking, and now answer synthesis or agents on top. The index still matters. Latency still matters. Grounding still matters.

- <span style="color:#8aff8a"><strong>Neural retrieval is becoming multi-stage.</strong></span> DPR showed that a dual encoder can retrieve relevant passages with dense vectors. ColBERT showed that you can keep more token-level interaction without paying full cross-encoder cost. Current systems often look like a cascade: BM25 + dense retrieval → fusion → late-interaction or cross-encoder re-rank → final ranker. Research keeps pushing the same question: how much semantic interaction can you afford before latency breaks?
- <span style="color:#93c5fd"><strong>Transformers are the language-understanding layer.</strong></span> The [Transformer](https://arxiv.org/abs/1706.03762) made attention the core primitive: every token can condition on other tokens, which is exactly what search needs for query intent, passage relevance, and answer generation. BERT made this practical for bidirectional language understanding; later encoder, decoder, and encoder-decoder models stretched the same idea into retrieval, ranking, summarization, and multimodal search.
- <span style="color:#ffff99"><strong>RAG turns retrieval into a grounding system.</strong></span> The retrieval layer no longer ends at a ranked list. It feeds evidence into an LLM, and the generated answer should be constrained by retrieved sources. This shifts research toward **source selection, citation quality, freshness, factuality, and evaluation**, not just NDCG on ten blue links.
- <span style="color:#ff8bd2"><strong>Search is becoming more multimodal and agentic.</strong></span> Queries can be text, images, video, files, voice, or a session history. Some searches are no longer one-shot: the system decomposes the task, issues multiple subqueries, keeps context, and may monitor the web or take an action. That is still IR at the core, but with planning and tool use around it.

So what does current Google-style search look like from public research and Google documentation? Be careful here: Google does not publish its full internal architecture. The accurate public model is an inferred product-and-systems map:

```text
classic Search:
  query
    -> language understanding + ranking systems
       (BERT, RankBrain, passage ranking, freshness, spam/reliability, helpfulness)
    -> retrieve and rank web results
    -> snippets, features, knowledge panels, local/shopping/news modules

AI Overview / AI Mode:
  query or follow-up
    -> model plans / expands the information need
    -> query fan-out across subtopics and data sources
       (web index, Knowledge Graph, real-time sources, shopping/product data, etc.)
    -> retrieve + rank + synthesize
    -> generated answer with links, or fallback to web results when confidence is low

AI Search direction:
  multimodal input
    -> conversational follow-up
    -> information agents / task agents
    -> generated UI, summaries, monitoring, and actions
```

Google's public docs line up with the funnel in this post. Its [ranking systems guide](https://developers.google.com/search/docs/appearance/ranking-systems-guide) names systems such as RankBrain and passage ranking; its [2019 BERT post](https://blog.google/products-and-platforms/products/search/search-language-understanding-bert/) says BERT helped Search understand query context for ranking and featured snippets; its [2020 Search update](https://blog.google/products-and-platforms/products/search/search-on/) says BERT was used in almost every English query and describes neural spell correction, passage understanding, and subtopic understanding. On the generative side, Google's 2025 AI Mode post describes [query fan-out](https://blog.google/products-and-platforms/products/search/ai-mode-search/) across subtopics and data sources, and its 2026 Search post describes [AI Mode, multimodal input, follow-ups from AI Overviews, and information agents](https://blog.google/products-and-platforms/products/search/search-io-2026/).

The important design lesson: **modern search is no longer just "rank documents."** It is becoming:

```text
retrieve evidence -> rank evidence -> synthesize an answer -> expose sources -> continue or act
```

But the old engineering constraint did not disappear. The generated answer is only as good as the retrieval and grounding underneath it. If Stage 1 misses the right evidence, the LLM cannot reliably invent it back. If the source selection is weak, the answer may sound fluent and still be wrong. So current ML research in search is headed toward better retrievers, cheaper interaction models, grounded generation, multimodal inputs, and agentic workflows — while still depending on the same staged retrieval architecture.

> **Memory hook:** *current ML search is not one giant model scanning everything. It is the funnel made deeper: lexical+dense retrieval, fusion, late interaction, cross-encoder/ranker, then RAG-style synthesis or agents. Transformers power language understanding and generation; retrieval keeps the system grounded. Public Google Search has moved in the same direction: BERT/RankBrain/passage ranking for classic results, and AI Mode/AI Overviews using query fan-out, Gemini, links, follow-ups, multimodal input, and agents.*

---

## Where this leaves us

Put the two posts together and you can design a modern search engine top to bottom. The [classical half](27-information-retrieval-search-engine.md) gives you the skeleton — inverted index, crawl-and-index pipeline, the merge-based read path, BM25, the forgiveness layer, and the metrics to prove quality. This half gives you the intelligence that rides on top, and it all hangs off one shape:

- **The funnel** — cheap recall-oriented Stage-1 retrieval, expensive precision-oriented Stage-2 re-ranking — is the frame that makes everything else affordable.
- **Learning to rank** turns the ranking function from a hand-tuned formula into a model trained on judgments and clicks, optimizing NDCG, best done listwise (LambdaMART).
- **Embeddings** turn words and documents into vectors of meaning, curing vocabulary mismatch and enabling semantic search.
- **Dense retrieval** splits the transformer two ways — bi-encoder to retrieve over everything (precomputed, ANN-indexed), cross-encoder to re-rank the survivors (joint, accurate) — which is *why* the funnel has the shape it does.
- **Hybrid + fusion** unite the lexical and the semantic because they fail in opposite cases.
- And the whole assembly is now the **retrieval foundation of the LLM and AI Search era**: answers, agents, and generated interfaces still depend on finding the right evidence first.

The deepest takeaway is the same one the classical post ended on, now upgraded: spend your cleverness where it's affordable. Precompute meaning into vectors and postings offline; retrieve cheaply over the whole corpus; and reserve the expensive, learned, accurate models for the short list the user will actually see. Get that funnel right and every modern relevance technique has a natural home in it.

> **Memory hook:** *the two posts together = a full modern search engine. ML hangs off one shape — the funnel (cheap recall retrieve → expensive precision rerank): learning to rank (LambdaMART, optimize NDCG) for the ranking function, embeddings for meaning, bi-encoder/cross-encoder splitting retrieve from rerank, hybrid+RRF fusing lexical and semantic, and the whole thing now the retrieval layer under RAG, AI Overviews, AI Mode, and agents. Precompute meaning offline, retrieve cheaply, reserve learned models for the short list, and ground generated answers in retrieved evidence.*
