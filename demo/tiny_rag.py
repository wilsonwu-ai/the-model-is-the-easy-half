#!/usr/bin/env python3
"""
tiny_rag.py -- the load-bearing skeleton of a retrieval pipeline, no model in it.

Run it:   python3 demo/tiny_rag.py
Test it:  python3 -m unittest discover -s demo -v      (the "-s demo" is required)
Needs:    nothing. No pip, no API key, no network. Python 3 standard library only.

People assume the language model is the hard part of an AI system. It is not.
Everything that decides whether the system is right or wrong happens before and
after it: how the facts get cut up, how the right ones get found, and how you
check the answer is really made out of them. This file does all of that WITHOUT
a model, so you can watch the machinery alone. Where a real system would call a
model, this uses something deliberately dumb and says so on the line doing it.

WHAT IS NOT HERE, and a real system needs: keyword (BM25) scoring blended with
the vectors, query rewriting, metadata and permission filters (a handbook has
pages the asker may not be allowed to read), deduplication, and an evaluation
set. This is the skeleton, not the whole animal.

The THREE FAILURES at the end are the point of the exercise, not bugs to fix.
"""

import hashlib
import math
import re
import textwrap

# ---------------------------------------------------------------------------
# THE CORPUS -- "corpus" just means the pile of text the system may answer from.
# ---------------------------------------------------------------------------
# An invented staff handbook. Nine short documents. In a real system this is a
# wiki, a support inbox, a folder of PDFs -- messy, and much larger. The shape
# of the problem does not change: text arrives, and it has to become findable.

CORPUS = [
    {"doc_id": "HB-01", "title": "VPN passwords", "text":
     "To reset your VPN password, open the Access Portal and choose Reset Password. "
     "The new password takes effect within ten minutes. "
     "VPN passwords expire every ninety days and cannot be reused."},

    {"doc_id": "HB-02", "title": "VPN access requests", "text":
     "New staff request VPN access through the Access Portal. "
     "Your manager must approve the request before the VPN account is created."},

    {"doc_id": "HB-03", "title": "Remote work", "text":
     "Remote work is available two days per week for most teams. "
     "Staff must be reachable on chat during core hours, which run from ten in the morning until four in the afternoon. "
     "Remote days are agreed with your manager and recorded in the team calendar. "
     "Teams that serve customers directly keep one fixed office day."},

    {"doc_id": "HB-04", "title": "Remote work equipment", "text":
     "The company provides a laptop, a monitor, and a home network router for remote work. "
     "Equipment requests go to the Facilities team and ship within ten business days."},

    {"doc_id": "HB-05", "title": "Travel meals", "text":
     "Meal costs during business travel are reimbursed at cost. "
     "Submit receipts in the Expense Portal within thirty days of the trip."},

    {"doc_id": "HB-06", "title": "Spending approval", "text":
     "Any expense above five hundred dollars requires written approval from your manager before you spend the money. "
     "Approval is requested in the Expense Portal and is usually returned by your manager the same working day. "
     "Purchases made without prior approval can be refused at reimbursement."},

    {"doc_id": "HB-07", "title": "Parental leave", "text":
     "Parental leave is sixteen weeks at full pay for all parents. "
     "Leave must begin within one year of the birth or adoption."},

    {"doc_id": "HB-08", "title": "Returning from leave", "text":
     "Staff returning from parental leave can work a reduced schedule for eight weeks at full pay. "
     "The reduced schedule is arranged with your manager before the return date."},

    {"doc_id": "HB-09", "title": "Approval limits (quick reference)", "text":
     "Approval limits: five hundred dollars for an expense, five thousand dollars for equipment. "
     "Approval limits are reviewed each January."},
]

# Words carrying no topic information, dropped everywhere -- in the vectors, the
# reranker and the checker -- so all three argue about the same thing.
#
# Note what is deliberately NOT here: "not", "no", "never", "without". A checker
# that throws away the negation cannot tell a rule from its opposite. Every word
# that IS here remains a hole in that checker: it cannot see "with", so
# "purchases made with prior approval" passes against a chunk saying "without".
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "before", "but", "by",
    "can", "do", "does", "each", "for", "from", "has", "have", "how", "i",
    "if", "in", "is", "it", "its", "may", "most", "must", "my", "need",
    "of", "on", "or", "so", "that", "the", "their", "this", "to", "until",
    "was", "we", "what", "when", "where", "which", "who", "will", "with",
    "you", "your",
}

# Length of every vector. Two words can land in the same bucket -- a collision --
# and the vector then cannot tell them apart. This corpus has some; step 2 counts
# them at run time rather than asserting a number here that would rot the moment
# the handbook is edited. Real systems using this trick use far FEWER buckets
# than words and accept many more collisions as the price of a fixed-size
# vector. It is a knob, and it has a wrong setting.
DIMENSIONS = 1024
TOP_K = 3          # how many chunks retrieval hands to the next stage


# ---------------------------------------------------------------------------
# STEP 1 -- CHUNK
# ---------------------------------------------------------------------------
# A whole document is the wrong unit to search: too big and the one relevant
# sentence drowns, too small and it loses the context that made it mean
# anything. So documents get cut into chunks, and the chunks OVERLAP -- each
# shares a sentence with the next, so a fact spanning a boundary is not sliced
# in half and lost by both sides. Cheapest reliability win in the pipeline.

def split_sentences(text):
    """Cut text at sentence endings. Crude, and good enough for a handbook."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = []
    for part in parts:
        if part.strip():
            kept.append(part.strip())
    return kept


def chunk_document(doc, sentences_per_chunk=2, overlap=1):
    """Slide a window of sentences across one document."""
    # With an overlap as big as the window, the window never advances and this
    # loop runs forever, quietly eating memory. Both are public arguments, so
    # refuse the setting rather than hang on it.
    if overlap >= sentences_per_chunk:
        raise ValueError("overlap must be smaller than sentences_per_chunk")
    sentences = split_sentences(doc["text"])
    step = sentences_per_chunk - overlap
    chunks = []
    start = 0
    index = 0
    while start < len(sentences):
        window = sentences[start:start + sentences_per_chunk]
        chunks.append({
            "chunk_id": "%s#%d" % (doc["doc_id"], index),
            "doc_id": doc["doc_id"],
            "title": doc["title"],
            "text": " ".join(window),
        })
        index += 1
        if start + sentences_per_chunk >= len(sentences):
            break          # this window already reached the end
        start += step
    return chunks


def chunk_corpus(corpus):
    """Chunk everything. Returns (chunks, ids of documents that produced none).

    An empty document -- a PDF whose text extraction silently failed -- makes no
    chunks and is then simply absent from the index, with no error and no
    warning. Counting the losses is the only defense, so the count comes back
    with the chunks instead of being dropped.
    """
    chunks, produced_nothing = [], []
    for doc in corpus:
        made = chunk_document(doc)
        if not made:
            produced_nothing.append(doc["doc_id"])
        for chunk in made:
            chunks.append(chunk)
    return chunks, produced_nothing


# ---------------------------------------------------------------------------
# STEP 2 -- EMBED
# ---------------------------------------------------------------------------
# Computers cannot compare sentences, only numbers. So every chunk becomes a
# fixed-length list of numbers -- a vector -- and similar text is supposed to
# produce similar vectors.
#
# The real thing is a trained neural network that maps MEANING into those
# numbers. THIS IS NOT THAT; it keeps the industry name only because that is the
# name you will meet everywhere else. It counts words and drops each into a
# numbered bucket. Nothing is trained, and nothing here knows what a word means.
# Failure A is that limitation, out loud.

def tokenize(text):
    """Lowercase, keep letters and digits, drop the words that carry no topic.

    ASCII-only, so text in a non-Latin script tokenises to nothing, embeds to
    all zeros and becomes permanently unretrievable without raising anything.
    """
    raw = re.findall(r"[a-z0-9]+", text.lower())
    tokens = []
    for word in raw:
        if word not in STOPWORDS:
            tokens.append(word)
    return tokens


def bucket_of(token, dimensions=DIMENSIONS):
    """Map a word to one of the numbered buckets.

    Uses hashlib, NOT Python's built-in hash(). hash() on strings is randomized
    per process (PYTHONHASHSEED), so the same word would land in a different
    bucket on every run and every machine. sha256 is fixed forever, and an
    embedding you cannot reproduce is an embedding you cannot debug.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dimensions


def embed(text, dimensions=DIMENSIONS):
    """Count words into buckets, then scale the vector to length 1."""
    vector = [0.0] * dimensions
    for token in tokenize(text):
        vector[bucket_of(token, dimensions)] += 1.0

    # L2-normalize: divide every count by the list's own length. After this,
    # comparing two vectors measures only which MIX of words each text used and
    # not how much text there was. Without it, long documents win everything.
    length = 0.0
    for value in vector:
        length += value * value
    length = math.sqrt(length)
    if length == 0.0:
        return vector          # no known words: an honest all-zero vector
    for i in range(dimensions):
        vector[i] = vector[i] / length
    return vector


def vocabulary(texts):
    """Every distinct content word across a list of texts."""
    words = set()
    for text in texts:
        for token in tokenize(text):
            words.add(token)
    return words


# ---------------------------------------------------------------------------
# STEP 3 -- INDEX AND RETRIEVE
# ---------------------------------------------------------------------------

def cosine(a, b):
    """Dot product. It equals the cosine ONLY because both inputs came out of
    embed() already scaled to length 1, which this does not check. 1.0 = same
    mix of words, 0.0 = none in common -- except against an all-zero vector,
    where 0.0 really means undefined and merely reads as "no match".
    """
    if len(a) != len(b):
        raise ValueError("vectors must be the same length")
    total = 0.0
    for i in range(len(a)):
        total += a[i] * b[i]
    return total


def retrieve(query, chunks, vectors, k=TOP_K):
    """Score the query against EVERY chunk and keep the best k.

    Brute force: one comparison per chunk. It finds the true top k FOR THIS
    SCORE -- a different thing from finding the right answer, as failure A
    shows. Production swaps it for an approximate index (HNSW, IVF) not because
    brute force is wrong but because it does not survive scale: at a hundred
    million chunks you buy a nearly-right answer that arrives in milliseconds.
    That trade is what a vector database is actually selling.

    A query with no known content words scores 0.0 against everything and still
    returns k confident-looking rows; nothing here separates "no match" from
    "match". Ties break on chunk_id, so reordering the handbook cannot silently
    change what comes back.
    """
    query_vector = embed(query)
    scored = []
    for i in range(len(chunks)):
        scored.append((cosine(query_vector, vectors[i]), chunks[i]))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
    return scored[:k]


# ---------------------------------------------------------------------------
# STEP 4 -- RERANK
# ---------------------------------------------------------------------------
# Retrieval had to be cheap because it looked at everything. Reranking only
# looks at the handful that survived, so it can afford to be fussy. The vector
# in step 2 is a BAG of words: it knows which words appear and threw the order
# away, so "five hundred dollars for an expense" and "expense above five hundred
# dollars" look nearly identical to it. This stage puts order back.
#
# In production it is a CROSS-ENCODER: a different model reading query and chunk
# together, which the embedding never does. Here it is arithmetic over the SAME
# tokenizer and stopword list as step 2 -- so it is lexical too, and cheaper
# than the embedding rather than dearer. It fixes word ORDER mistakes; it cannot
# fix a synonym miss, and failure A's question is exactly as invisible to it. It
# also only sees the TOP_K retrieval already liked, so anything ranked fourth or
# worse is gone for good however good this stage gets: recall lost at step 3
# cannot be recovered at step 4, the commonest way a real RAG system fails
# without anyone noticing.

def ngrams(tokens, n):
    grams = []
    for i in range(len(tokens) - n + 1):
        grams.append(tuple(tokens[i:i + n]))
    return grams


def rerank_score(query, text):
    query_tokens = tokenize(query)
    text_tokens = tokenize(text)
    text_set = set(text_tokens)

    # How many of the query's distinct words appear at all.
    distinct = set(query_tokens)
    hits = 0
    for token in distinct:
        if token in text_set:
            hits += 1
    coverage = hits / len(distinct) if distinct else 0.0

    # Exact phrase matches -- the thing the bag of words could not see.
    text_pairs = set(ngrams(text_tokens, 2))
    text_triples = set(ngrams(text_tokens, 3))
    pair_hits = 0
    for gram in ngrams(query_tokens, 2):
        if gram in text_pairs:
            pair_hits += 1
    triple_hits = 0
    for gram in ngrams(query_tokens, 3):
        if gram in text_triples:
            triple_hits += 1

    # Proximity: are the matching words bunched together, or spread across the
    # whole chunk by accident? Crude measure, honest signal.
    positions = []
    for i in range(len(text_tokens)):
        if text_tokens[i] in distinct:
            positions.append(i)
    if len(positions) >= 2:
        span = positions[-1] - positions[0] + 1
        proximity = len(positions) / span
    else:
        proximity = 0.0

    return 3.0 * triple_hits + 2.0 * pair_hits + 1.0 * coverage + 0.5 * proximity


def rerank(query, retrieved):
    scored = []
    for _, chunk in retrieved:
        scored.append((rerank_score(query, chunk["text"]), chunk))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
    return scored


# ---------------------------------------------------------------------------
# STEP 5 -- GENERATE
# ---------------------------------------------------------------------------
# THIS IS NOT A LANGUAGE MODEL. It picks the best-matching sentence out of each
# retrieved chunk and staples them together with a citation. It cannot rephrase,
# summarize or reason -- and it cannot invent, which matters in step 6: every
# sentence is copied verbatim out of a chunk that is also the evidence, so the
# checker CANNOT reject this generator's real output. That clean pass proves
# nothing, and failure B has to splice a lie in by hand to give the checker
# anything to catch. A real model would read better and would change nothing
# else in the pipeline. The fluency is the part that got cheap.

def best_sentence(query, text):
    """The sentence with the most words in common with the question, or None.

    None when nothing overlaps at all. Without that, an out-of-domain question
    is handed the first sentence of whatever ranked top, cited and confident --
    and the checker waves it through, because it really was copied from a chunk.
    A system that cannot return nothing will always return something.
    """
    query_set = set(tokenize(query))
    best, best_hits = None, 0
    for sentence in split_sentences(text):
        hits = 0
        for token in tokenize(sentence):
            if token in query_set:
                hits += 1
        if hits > best_hits:
            best, best_hits = sentence, hits
    return best


def generate(query, ranked, max_sentences=2):
    """Stitch retrieved sentences into an answer, one per source document."""
    answer, used = [], set()
    for _, chunk in ranked:
        if chunk["doc_id"] in used:
            continue
        sentence = best_sentence(query, chunk["text"])
        if sentence is None:
            continue
        answer.append("%s [%s]" % (sentence, chunk["doc_id"]))
        used.add(chunk["doc_id"])
        if len(answer) >= max_sentences:
            break
    return answer


# ---------------------------------------------------------------------------
# STEP 6 -- VERIFY
# ---------------------------------------------------------------------------
# The last stage, and the one most systems skip. Every sentence of the answer is
# checked back against the text that was actually retrieved. A sentence is
# COVERED only if some single retrieved chunk contains every one of its content
# words. The industry word for this stage is "grounding" -- but that word means
# ENTAILED, and this tests nothing of the kind, so the printed label says only
# what was measured.
#
# It FAILS CLOSED: anything it cannot confirm is rejected, never waved through.
# Give it no context and it grounds nothing.
#
# WHAT IT CATCHES: a sentence using a word that appears in no retrieved chunk --
# the invented "fourteen" of failure B, and the commonest hallucination shape.
#
# WHAT IT CANNOT CATCH, and no tuning will fix:
#   * Recombination. This is a SET test: order, repetition and which number
#     belongs to which noun are thrown away, so "five thousand dollars for an
#     expense, five hundred for equipment" passes against a chunk saying the
#     reverse. Failure C runs exactly that.
#   * Any word the tokenizer discarded. See STOPWORDS.
#   * Truth. Nothing here consults the world, so a sentence can be COVERED and
#     false -- which is what failure C is.
# It is a NECESSARY condition wearing a green label, not a sufficient one, and
# it is not a security boundary: anyone who can see the retrieved chunks can
# write a passing falsehood out of their vocabulary in a minute. A real system
# puts an entailment model here and inherits that model's error rate instead.

def verify(answer_sentences, context_chunks):
    """Returns (sentence, covered, closest chunk, words missing from it)."""
    results = []
    for sentence in answer_sentences:
        clean = re.sub(r"\[[^\]]*\]", " ", sentence)     # drop the citation tag
        tokens = tokenize(clean)

        if not tokens:
            results.append((sentence, False, None, []))  # nothing to check = not proven
            continue

        best_chunk, missing = None, list(tokens)
        for chunk in context_chunks:
            chunk_tokens = set(tokenize(chunk["text"]))
            gaps = []
            for token in tokens:
                if token not in chunk_tokens:
                    gaps.append(token)
            if best_chunk is None or len(gaps) < len(missing):
                best_chunk, missing = chunk, gaps

        covered = best_chunk is not None and len(missing) == 0
        results.append((sentence, covered, best_chunk, missing))
    return results


# ---------------------------------------------------------------------------
# NARRATION
# ---------------------------------------------------------------------------

def wrapped(label, text, indent=4, width=78):
    """Print label + text, folded so nothing overflows a README code block."""
    prefix = " " * indent + label
    body = " " * (indent + len(label))
    lines = textwrap.wrap(text, width=width - len(prefix)) or [""]
    print("%s%s" % (prefix, lines[0]))
    for line in lines[1:]:
        print("%s%s" % (body, line))


def header(label, title):
    print()
    print("=" * 78)
    print("  %s  %s" % (label, title))
    print("=" * 78)


def show_ranking(rows, score_label):
    """Rank shows 'tie' on a zero score: a chunk that matched nothing has no
    rank, and its place in the list is an accident of sort order."""
    print("  %-4s %-10s %-9s %s" % ("rank", "chunk", score_label, "text"))
    for i in range(len(rows)):
        score, chunk = rows[i]
        text = chunk["text"]
        if len(text) > 50:
            text = text[:47] + "..."
        rank = "%d" % (i + 1) if score > 0.0 else "tie"
        print("  %-4s %-10s %-9.4f %s" % (rank, chunk["chunk_id"], score, text))


def show_verdicts(results):
    print("  %-12s %-8s %s" % ("verdict", "source", "sentence"))
    covered_count = 0
    for sentence, covered, chunk, _ in results:
        source = chunk["chunk_id"] if (covered and chunk) else "--"
        text = sentence if len(sentence) <= 52 else sentence[:49] + "..."
        print("  %-12s %-8s %s" % ("COVERED" if covered else "NOT COVERED", source, text))
        if covered:
            covered_count += 1
    return covered_count


def sentence_with(text, word, width=74):
    """The sentence that actually contains a matched word, so the demo can SHOW
    why something matched instead of asserting it. Cut on a word boundary."""
    for sentence in split_sentences(text):
        if word in tokenize(sentence):
            if len(sentence) <= width:
                return sentence
            return sentence[:sentence.rfind(" ", 0, width - 3)] + "..."
    return ""


def detail(label, items, indent=16, width=78):
    """One indented evidence line under a verdict, truncated rather than folded."""
    body = "%s%s%s" % (" " * indent, label, ", ".join(items) if items else "(none)")
    print(body if len(body) <= width else body[:width - 3] + "...")


def main():
    print()
    print("THE MODEL IS THE EASY HALF -- a retrieval pipeline with no model in it")
    print("Six stages, then three demonstrations of it failing. No network, no model.")

    # --- STEP 1 -------------------------------------------------------------
    header("STEP 1", "CHUNK -- cut the documents into searchable pieces")
    chunks, empty_docs = chunk_corpus(CORPUS)
    print("  %d documents became %d overlapping chunks (2 sentences each, 1 shared)." % (len(CORPUS), len(chunks)))
    print("  Documents that produced no chunks: %d. An empty one -- a PDF whose text" % len(empty_docs))
    print("  extraction quietly failed -- would vanish here with no error at all.")
    print()
    print("  One chunk in full. A 'chunk' is just a short passage, kept whole:")
    print("    id    : %s" % chunks[0]["chunk_id"])
    print("    source: %s (%s)" % (chunks[0]["title"], chunks[0]["doc_id"]))
    wrapped("text  : ", chunks[0]["text"], indent=4)
    print()
    print("  The overlap made visible -- the next chunk repeats the last sentence, so a")
    print("  fact sitting on the boundary is not cut in half and lost by both sides:")
    wrapped("%s: " % chunks[1]["chunk_id"], chunks[1]["text"], indent=4)

    # --- STEP 2 -------------------------------------------------------------
    header("STEP 2", "EMBED -- turn each chunk into %d numbers, by counting words" % DIMENSIONS)
    print("  These are NOT semantic embeddings and nothing here was trained. A 'vector'")
    print("  is just a fixed-length list of numbers; each of these records WHICH words a")
    print("  chunk contains and nothing about what they mean, so two sentences with the")
    print("  same meaning and no shared words come out unrelated. Failure A is that.")
    print()
    vectors = []
    for chunk in chunks:
        vectors.append(embed(chunk["text"]))
    sample = vectors[0]
    filled = []
    for i in range(DIMENSIONS):
        if sample[i] != 0.0:
            filled.append(i)
    print("  Chunk %s uses %d of the %d buckets. The rest are zero." % (chunks[0]["chunk_id"], len(filled), DIMENSIONS))
    print("  First filled buckets: %s" % ", ".join("[%d]=%.3f" % (i, sample[i]) for i in filled[:4]))
    print()

    # Work the arithmetic by hand, so none of the numbers above are magic.
    counts = {}
    for token in tokenize(chunks[0]["text"]):
        counts[token] = counts.get(token, 0) + 1
    by_count = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    raw_length = 0.0
    for count in counts.values():
        raw_length += count * count
    raw_length = math.sqrt(raw_length)
    top_word, top_n = by_count[0]
    next_word, next_n = by_count[1]
    singles = len(counts) - 2
    print("  Worked by hand, so none of that is magic:")
    print("    \"%s\" appears %d times, \"%s\" %d times, %d other words once each." % (
        top_word, top_n, next_word, next_n, singles))
    print("    Counted straight, bucket [%d]=%d and bucket [%d]=%d." % (
        bucket_of(top_word), top_n, bucket_of(next_word), next_n))
    print("    The list's own length is sqrt(%d*%d + %d*%d + %d ones) = %.3f." % (
        top_n, top_n, next_n, next_n, singles, raw_length))
    print("    Divide each count by %.3f: [%d]=%.3f and [%d]=%.3f. That division is" % (
        raw_length, bucket_of(top_word), top_n / raw_length, bucket_of(next_word), next_n / raw_length))
    print("    'normalizing', and it is what lets a two-sentence chunk and a")
    print("    two-hundred-page document be compared at all: only the MIX of words")
    print("    survives, not how much text there was.")
    print("  That leaves length 1 for any chunk with a content word in it, and exactly 0")
    print("  for a chunk with none: embed() returns an honest all-zero vector rather than")
    print("  dividing by zero, so the length is not 'always 1'.")
    print()

    # Collisions: counted, not claimed.
    words = vocabulary([chunk["text"] for chunk in chunks])
    by_bucket = {}
    for word in sorted(words):
        by_bucket.setdefault(bucket_of(word), []).append(word)
    collided = []
    for bucket in sorted(by_bucket):
        if len(by_bucket[bucket]) > 1:
            collided.append((bucket, by_bucket[bucket]))
    print("  %d distinct content words into %d buckets: %d collision(s)." % (len(words), DIMENSIONS, len(collided)))
    for bucket, pair in collided:
        print("    %s share bucket %d -- the vector cannot tell them apart." % (" / ".join(pair), bucket))
    print("  Collisions are the price of a fixed-size vector, and real systems use far")
    print("  FEWER buckets than words and accept many more of them. 'password' always")
    print("  lands in bucket %d while DIMENSIONS is %d -- on this machine, on yours," % (bucket_of("password"), DIMENSIONS))
    print("  and in ten years. That is hashlib; Python's own hash() is randomized per")
    print("  run and would reshuffle every number above on each launch.")

    # --- STEPS 3-6 on one real question ------------------------------------
    query = "do I need approval for an expense above five hundred dollars"

    header("STEP 3", "RETRIEVE -- compare the question to all %d chunks" % len(chunks))
    print('  Question: "%s"' % query)
    print()
    retrieved = retrieve(query, chunks, vectors, k=TOP_K)
    show_ranking(retrieved, "cosine")
    print()
    print("  'Cosine' is the comparison: 1.0000 means the two texts use the same mix of")
    print("  words, 0.0000 means they share none. It is not a chance of being right.")
    print("  Brute force, %d comparisons, one per chunk. It finds the true top %d FOR THIS" % (len(chunks), TOP_K))
    print("  SCORE, which is not the same as the right answer -- failure A shows the gap.")
    print("  Real systems swap it for an approximate index, a precomputed shortcut,")
    print("  because %d comparisons are free and a hundred million are not." % len(chunks))

    header("STEP 4", "RERANK -- look again, more carefully, at only the top %d" % TOP_K)
    reranked = rerank(query, retrieved)
    print("  BEFORE (word counting, word order thrown away):")
    show_ranking(retrieved, "cosine")
    print()
    print("  AFTER (phrases and word proximity put back). This score has its own scale:")
    print("  higher is better, and it is NOT comparable to the cosines above.")
    show_ranking(reranked, "rerank")
    print()
    swapped = (retrieved[0][1]["chunk_id"] == reranked[1][1]["chunk_id"]
               and reranked[0][1]["chunk_id"] == retrieved[1][1]["chunk_id"])
    if swapped:
        winner, loser = reranked[0][1], retrieved[0][1]
        won, lost = [], []
        for gram in ngrams(tokenize(query), 2):
            if gram in set(ngrams(tokenize(winner["text"]), 2)):
                won.append(" ".join(gram))
            if gram in set(ngrams(tokenize(loser["text"]), 2)):
                lost.append(" ".join(gram))
        print("  Ranks 1 and 2 swapped -- the permutation is checked, not narrated.")
        wrapped("%s has %d of the question's word pairs in order: " % (winner["chunk_id"], len(won)),
                ", ".join(won), indent=2)
        wrapped("%s has %d: " % (loser["chunk_id"], len(lost)),
                ", ".join(lost) if lost else "(none)", indent=2)
        print("  The first pass could see none of that. It deleted word order to be cheap,")
        print("  so a quick-reference line that merely repeats the words won instead.")
    else:
        print("  No swap this run. The narration is computed, so it says nothing instead.")

    header("STEP 5", "GENERATE -- stitch the retrieved sentences into an answer")
    context = []
    for _, chunk in reranked:
        context.append(chunk)
    answer = generate(query, reranked)
    print("  NOT a language model. This copies sentences and adds a citation. It cannot")
    print("  rephrase, reason, or invent. A real model would read better and would change")
    print("  nothing else in this file -- which is the whole argument.")
    print()
    for sentence in answer:
        wrapped("- ", sentence, indent=4)

    header("STEP 6", "VERIFY -- check every sentence's words against the retrieved text")
    print("  The rule, in one sentence: a sentence is COVERED when some single retrieved")
    print("  chunk contains every one of its content words. The industry calls this")
    print("  'grounding', but that word means ENTAILED and this is only word coverage,")
    print("  so the label says exactly what was measured and no more.")
    print()
    results = verify(answer, context)
    covered = show_verdicts(results)
    print()
    if covered == len(results):
        print("  Answer accepted: %d of %d sentences had every content word present in one" % (covered, len(results)))
        print("  retrieved chunk. Note what that does NOT prove: the generator above copies")
        print("  sentences verbatim out of those same chunks, so it CANNOT fail this check.")
        print("  A checker that never rejects anything is decoration, so the next two")
        print("  sections attack it on purpose.")
    else:
        print("  Answer rejected: %d of %d sentences failed word coverage." % (len(results) - covered, len(results)))

    # --- FAILURE A ----------------------------------------------------------
    header("FAILURE A", "THE SYNONYM MISS -- the same question in different words")
    bad_query = "how do I recover my network credentials"
    target = None
    for chunk in chunks:
        if chunk["chunk_id"] == "HB-01#0":
            target = chunk
    query_words = sorted(set(tokenize(bad_query)))
    target_words = sorted(set(tokenize(target["text"])))
    shared = sorted(set(query_words) & set(target_words))

    print('  Question: "%s"' % bad_query)
    wrapped("The right answer is in %s: " % target["chunk_id"], target["text"], indent=2)
    print()
    wrapped("Content words in the question: ", ", ".join(query_words), indent=2)
    wrapped("Content words in the answer  : ", ", ".join(target_words), indent=2)
    print("  Words in common              : %s" % (", ".join(shared) if shared else "(none -- checked, not assumed)"))
    print()
    bad_hits = retrieve(bad_query, chunks, vectors, k=TOP_K)
    show_ranking(bad_hits, "cosine")
    print()

    everything = retrieve(bad_query, chunks, vectors, k=len(chunks))
    target_score, zeros = 0.0, 0
    for score, chunk in everything:
        if chunk["chunk_id"] == target["chunk_id"]:
            target_score = score
        if score == 0.0:
            zeros += 1
    top_chunk = bad_hits[0][1]
    overlap = sorted(set(tokenize(bad_query)) & set(tokenize(top_chunk["text"])))
    print("  Rank 1 came back %s, %s -- score %.4f." % (top_chunk["chunk_id"], top_chunk["title"], bad_hits[0][0]))
    if overlap:
        print("  Its only word in common with the question is '%s', in this sentence:" % overlap[0])
        print("    %s" % sentence_with(top_chunk["text"], overlap[0]))
    else:
        print("  It shares NO word with the question: that score is a bucket collision.")
    print("  The right chunk %s scored %.4f, and so did %d of %d chunks. It is not" % (
        target["chunk_id"], target_score, zeros, len(chunks)))
    print("  ranked second, it is UNRANKED -- which is why the table prints 'tie'.")
    print()
    print("  A person sees one question asked twice. The word counter sees no overlap at")
    print("  all, so the right answer scores exactly zero, cannot be ranked above")
    print("  anything, and nothing signals that the system missed. This is the capability")
    print("  a trained embedding model is SOLD on -- mapping MEANING into the numbers so")
    print("  'credentials' and 'password' land close. This file has no model and does not")
    print("  test that claim; test it on your own documents before believing it. And note")
    print("  what a better model does NOT fix: chunking, reranking, citation, checking.")

    # --- FAILURE B ----------------------------------------------------------
    header("FAILURE B", "THE UNGROUNDED SENTENCE -- fluent, confident, invented")
    poisoned = list(answer)
    poisoned.insert(1, "Written approval above five hundred dollars is returned within fourteen days. [HB-06]")
    print("  The generator above CANNOT produce the extra sentence below -- it only")
    print("  copies. It was typed by hand to stand in for what a real model does, and the")
    print("  check does not know that. Try to spot it:")
    print()
    for sentence in poisoned:
        wrapped("- ", sentence, indent=4)
    print()
    print("  Same tone, same citation format, same confidence. No stylistic tell, because")
    print("  there never is one. Now run the check:")
    print()
    handbook_words = vocabulary([doc["text"] for doc in CORPUS])
    print("  %-12s %-8s %s" % ("verdict", "source", "sentence"))
    rejected = 0
    for sentence, covered_flag, chunk, missing in verify(poisoned, context):
        source = chunk["chunk_id"] if (covered_flag and chunk) else "--"
        text = sentence if len(sentence) <= 52 else sentence[:49] + "..."
        print("  %-12s %-8s %s" % ("COVERED" if covered_flag else "NOT COVERED", source, text))
        if not covered_flag:
            rejected += 1
            nowhere = []
            for token in missing:
                if token not in handbook_words:
                    nowhere.append(token)
            detail("missing from closest chunk (%s): " % (chunk["chunk_id"] if chunk else "none"), missing)
            detail("of those, absent from the whole handbook: ", nowhere)
    print()
    if rejected:
        print("  ANSWER REJECTED. %d of %d sentences failed the check." % (rejected, len(poisoned)))
    else:
        print("  ANSWER ACCEPTED -- the check did not catch it. All %d passed." % len(poisoned))
    print("  Those two lines are not the same claim. 'within' and 'days' ARE in the")
    print("  handbook; the check only ever compares against the chunks that were")
    print("  RETRIEVED, and none of the three had them. Only 'fourteen' was invented")
    print("  outright, and one unfamiliar word was enough. Good answer and poisoned")
    print("  answer are equally fluent and confident. Only the check separates them.")

    # --- FAILURE C ----------------------------------------------------------
    header("FAILURE C", "THE LIE THE CHECK CANNOT CATCH -- both of these are false")
    attacks = [
        "Approval is not requested in the Expense Portal. [HB-06]",
        "Approval limits: five thousand dollars for an expense, five hundred dollars for equipment. [HB-09]",
    ]
    for sentence in attacks:
        wrapped("- ", sentence, indent=4)
    print()
    print("  The first inverts the policy. The second swaps the two limits, so anyone")
    print("  acting on it overspends by ten times. Neither is in the handbook. Same")
    print("  check, same three chunks of evidence:")
    print()
    show_verdicts(verify(attacks, context))
    print()
    print("  The first is caught, and only because 'not' was deliberately kept OUT of the")
    print("  stopword list: a checker that deletes the negation cannot tell a rule from")
    print("  its opposite. The second passes, and it has to. This is a SET test -- word")
    print("  order, repetition, and which number belongs to which noun are all thrown")
    print("  away before it runs, and every word of that sentence really is in HB-09.")
    print()
    print("  So the check catches invented facts and misses invented relationships. It is")
    print("  a correctness filter, not a security boundary: anyone who can see the")
    print("  retrieved chunks can write a passing falsehood out of their vocabulary in a")
    print("  minute, and a real system puts an entailment model here instead. What makes")
    print("  even this version worth having is that it FAILS CLOSED -- what it cannot")
    print("  confirm is rejected, never waved through.")

    print()
    print("=" * 78)
    print("  Generation is cheap now. Verification is not. That is the whole article.")
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
