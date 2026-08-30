#!/usr/bin/env python3
"""
Tests for tiny_rag.py. Standard library unittest only.

Run from the repo root:  python3 -m unittest discover -s demo -v
The "-s demo" is required. Without it, discovery finds nothing, reports
"Ran 0 tests", and exits non-zero -- a success-shaped failure.

Most of these are ordinary correctness tests. Three groups are not:

  * TestKnownLimitations asserts that the demo FAILS. Those failures are the
    article's argument, so they are pinned like any other behavior.

  * TestCheckerCanBeBeaten pins the sentences that beat the checker. If a
    future change starts catching them, the file's own honesty comments are
    out of date and must be rewritten -- so those tests failing is a signal.

  * test_reranker_swaps_the_documented_case pins the exact before-and-after
    ordering, so the demo can never print a swap that did not happen.
"""

import unittest

import tiny_rag as rag


MAIN_QUERY = "do I need approval for an expense above five hundred dollars"
SYNONYM_QUERY = "how do I recover my network credentials"


def build_index():
    chunks, _ = rag.chunk_corpus(rag.CORPUS)
    vectors = []
    for chunk in chunks:
        vectors.append(rag.embed(chunk["text"]))
    return chunks, vectors


class TestEmbedding(unittest.TestCase):

    def test_vectors_are_l2_normalised(self):
        for text in ["VPN password reset", "parental leave", rag.CORPUS[5]["text"]]:
            vector = rag.embed(text)
            total = 0.0
            for value in vector:
                total += value * value
            self.assertAlmostEqual(total ** 0.5, 1.0, places=9)

    def test_embedding_is_deterministic_across_two_calls(self):
        self.assertEqual(rag.embed("VPN password reset"), rag.embed("VPN password reset"))

    def test_embedding_is_stable_for_a_known_input(self):
        """Pin the actual numbers.

        The demo's whole claim is that it produces identical output on every
        machine and every run. If someone swaps sha256 for something else, or
        changes DIMENSIONS or the stopword list, these exact values move and
        this test is what notices.
        """
        self.assertEqual(rag.DIMENSIONS, 1024)
        self.assertEqual(rag.bucket_of("password"), 152)
        self.assertEqual(rag.bucket_of("vpn"), 239)

        vector = rag.embed("VPN password reset")
        filled = {}
        for i in range(rag.DIMENSIONS):
            if vector[i] != 0.0:
                filled[i] = round(vector[i], 10)
        # three distinct words, so three buckets each at 1/sqrt(3)
        self.assertEqual(filled, {152: 0.5773502692, 187: 0.5773502692, 239: 0.5773502692})

    def test_unknown_words_give_an_honest_zero_vector(self):
        vector = rag.embed("the and of to")          # stopwords only
        self.assertEqual(sum(vector), 0.0)

    def test_negations_are_content_words_not_stopwords(self):
        """The checker is blind to every word tokenize() drops, so negation
        must survive it. Failure C depends on this."""
        for word in ["not", "no", "never", "without"]:
            self.assertNotIn(word, rag.STOPWORDS)
            self.assertEqual(rag.tokenize(word), [word])

    def test_non_latin_text_silently_embeds_to_nothing(self):
        """A documented sharp edge, pinned so it stays documented."""
        self.assertEqual(rag.tokenize("日本語のクエリ"), [])
        self.assertEqual(sum(rag.embed("日本語のクエリ")), 0.0)


class TestCosine(unittest.TestCase):

    def test_cosine_with_itself_is_one(self):
        vector = rag.embed("parental leave is sixteen weeks at full pay")
        self.assertAlmostEqual(rag.cosine(vector, vector), 1.0, places=9)

    def test_cosine_of_disjoint_vocabularies_is_zero(self):
        a = rag.embed("parental leave adoption")
        b = rag.embed("laptop monitor router")
        self.assertAlmostEqual(rag.cosine(a, b), 0.0, places=9)

    def test_cosine_refuses_mismatched_lengths(self):
        """Silently truncating to the shorter vector would return a plausible
        number from an impossible comparison."""
        with self.assertRaises(ValueError):
            rag.cosine(rag.embed("vpn", dimensions=8), rag.embed("vpn", dimensions=1024))
        with self.assertRaises(ValueError):
            rag.cosine(rag.embed("vpn", dimensions=1024), rag.embed("vpn", dimensions=8))


class TestChunking(unittest.TestCase):

    def test_consecutive_chunks_actually_overlap(self):
        """Overlap is the cheapest reliability win in the pipeline, so prove it."""
        doc = None
        for candidate in rag.CORPUS:
            if candidate["doc_id"] == "HB-01":
                doc = candidate
        chunks = rag.chunk_document(doc)
        self.assertGreater(len(chunks), 1)
        for i in range(len(chunks) - 1):
            first = set(rag.split_sentences(chunks[i]["text"]))
            second = set(rag.split_sentences(chunks[i + 1]["text"]))
            self.assertTrue(first & second, "chunks %d and %d share no sentence" % (i, i + 1))

    def test_every_sentence_survives_chunking(self):
        for doc in rag.CORPUS:
            covered = set()
            for chunk in rag.chunk_document(doc):
                for sentence in rag.split_sentences(chunk["text"]):
                    covered.add(sentence)
            self.assertEqual(covered, set(rag.split_sentences(doc["text"])), doc["doc_id"])

    def test_overlap_at_least_the_window_is_refused_not_hung(self):
        """step <= 0 means the window never advances: an infinite loop that also
        eats memory. Both are public arguments, so it must raise."""
        doc = {"doc_id": "X", "title": "t", "text": "A one. B two. C three."}
        for window, overlap in [(1, 1), (2, 2), (2, 3)]:
            with self.assertRaises(ValueError):
                rag.chunk_document(doc, sentences_per_chunk=window, overlap=overlap)

    def test_an_empty_document_is_reported_not_silently_dropped(self):
        corpus = list(rag.CORPUS) + [{"doc_id": "HB-99", "title": "blank", "text": "   \n "}]
        chunks, produced_nothing = rag.chunk_corpus(corpus)
        self.assertEqual(produced_nothing, ["HB-99"])
        for chunk in chunks:
            self.assertNotEqual(chunk["doc_id"], "HB-99")


class TestRetrieval(unittest.TestCase):

    def setUp(self):
        self.chunks, self.vectors = build_index()

    def test_results_are_sorted_by_descending_score(self):
        results = rag.retrieve(MAIN_QUERY, self.chunks, self.vectors, k=len(self.chunks))
        for i in range(len(results) - 1):
            self.assertGreaterEqual(results[i][0], results[i + 1][0])

    def test_retrieval_respects_k(self):
        for k in [1, 3, 5]:
            self.assertEqual(len(rag.retrieve(MAIN_QUERY, self.chunks, self.vectors, k=k)), k)

    def test_ties_do_not_depend_on_corpus_order(self):
        """Ties break on chunk_id, so reordering the handbook cannot silently
        change which zero-scoring chunk surfaces."""
        shuffled = list(reversed(rag.CORPUS))
        chunks, _ = rag.chunk_corpus(shuffled)
        vectors = []
        for chunk in chunks:
            vectors.append(rag.embed(chunk["text"]))
        original = [c["chunk_id"] for _, c in rag.retrieve(SYNONYM_QUERY, self.chunks, self.vectors, k=len(self.chunks))]
        reordered = [c["chunk_id"] for _, c in rag.retrieve(SYNONYM_QUERY, chunks, vectors, k=len(chunks))]
        self.assertEqual(original, reordered)

    def test_a_query_with_no_known_words_still_returns_k_zeroes(self):
        """Documented sharp edge: nothing separates 'no match' from 'match'."""
        results = rag.retrieve("the and of to", self.chunks, self.vectors, k=rag.TOP_K)
        self.assertEqual(len(results), rag.TOP_K)
        for score, _ in results:
            self.assertEqual(score, 0.0)


class TestReranking(unittest.TestCase):

    def setUp(self):
        self.chunks, self.vectors = build_index()

    def test_reranker_swaps_the_documented_case(self):
        """The swap must be real, not narrated.

        Word counting puts the quick-reference line (HB-09#0) first because it
        repeats 'five hundred dollars'. The reranker sees the query's phrase in
        the query's order inside the real policy sentence (HB-06#0) and pulls it
        up. Both orderings are pinned exactly: if the corpus drifts and the two
        stages start agreeing, the demo would print a swap that did not happen,
        and this test fails first.
        """
        retrieved = rag.retrieve(MAIN_QUERY, self.chunks, self.vectors, k=rag.TOP_K)
        before = [chunk["chunk_id"] for _, chunk in retrieved]
        self.assertEqual(before, ["HB-09#0", "HB-06#0", "HB-06#1"])

        after = [chunk["chunk_id"] for _, chunk in rag.rerank(MAIN_QUERY, retrieved)]
        self.assertEqual(after, ["HB-06#0", "HB-09#0", "HB-06#1"])

        self.assertEqual(before[0], after[1])             # the two really swapped,
        self.assertEqual(before[1], after[0])             # not just "rank 1 moved"

    def test_reranker_rewards_exact_phrase_order(self):
        in_order = rag.rerank_score("five hundred dollars", "any expense above five hundred dollars")
        scrambled = rag.rerank_score("five hundred dollars", "dollars five limits hundred")
        self.assertGreater(in_order, scrambled)

    def test_reranker_is_as_blind_to_synonyms_as_the_vectors(self):
        """It shares the tokenizer, so it cannot rescue failure A. The comment
        in step 4 says so; this is the check behind the comment."""
        target = "To reset your VPN password, open the Access Portal and choose Reset Password."
        self.assertEqual(rag.rerank_score(SYNONYM_QUERY, target), 0.0)


class TestKnownLimitations(unittest.TestCase):

    def setUp(self):
        self.chunks, self.vectors = build_index()

    def test_synonym_query_shares_no_content_words_with_its_answer(self):
        """The premise of the failure demo, checked rather than assumed."""
        question = set(rag.tokenize(SYNONYM_QUERY))
        answer = set(rag.tokenize("To reset your VPN password, open the Access Portal "
                                  "and choose Reset Password."))
        self.assertTrue(question)
        self.assertEqual(question & answer, set())

    def test_known_limitation_synonym_query_retrieves_the_wrong_document(self):
        """A PASSING TEST THAT ASSERTS A FAILURE. This is intended.

        Asked for a password reset in different words, the toy retrieves the
        equipment policy and scores the correct chunk at exactly zero. That is
        not a bug in this file, it is the demonstration: bag-of-words vectors
        match spellings, and a trained embedding model matches meanings. That
        gap is the capability you are paying an embedding provider for.

        It is pinned as a test so nobody 'fixes' it by accident. If a future
        change makes this pass on merit, the demo's FAILURE A narration is
        lying and must be rewritten -- so this test failing is a real signal,
        not a nuisance.
        """
        results = rag.retrieve(SYNONYM_QUERY, self.chunks, self.vectors, k=rag.TOP_K)
        top_ids = [chunk["chunk_id"] for _, chunk in results]
        self.assertEqual(top_ids[0], "HB-04#0")           # the router, not the password

        everything = rag.retrieve(SYNONYM_QUERY, self.chunks, self.vectors, k=len(self.chunks))
        zeros = 0
        for score, chunk in everything:
            if chunk["chunk_id"] == "HB-01#0":
                self.assertEqual(score, 0.0)
            if score == 0.0:
                zeros += 1
        self.assertGreater(zeros, 1)                      # so its rank is meaningless


class TestGeneration(unittest.TestCase):

    def setUp(self):
        self.chunks, self.vectors = build_index()

    def test_generated_answer_carries_citations(self):
        ranked = rag.rerank(MAIN_QUERY, rag.retrieve(MAIN_QUERY, self.chunks, self.vectors))
        answer = rag.generate(MAIN_QUERY, ranked)
        self.assertTrue(answer)
        for sentence in answer:
            self.assertRegex(sentence, r"\[HB-\d\d\]$")

    def test_an_out_of_domain_question_gets_no_answer_at_all(self):
        """Without this, best_sentence() hands back the first sentence of
        whatever ranked top -- cited, confident, and about the wrong subject --
        and the checker passes it, because it really was copied from a chunk.
        """
        question = "what is the refund policy for cancelled flights"
        ranked = rag.rerank(question, rag.retrieve(question, self.chunks, self.vectors))
        self.assertEqual(rag.generate(question, ranked), [])


class TestVerifier(unittest.TestCase):

    def setUp(self):
        self.chunks, self.vectors = build_index()
        retrieved = rag.retrieve(MAIN_QUERY, self.chunks, self.vectors, k=rag.TOP_K)
        self.ranked = rag.rerank(MAIN_QUERY, retrieved)
        self.context = []
        for _, chunk in self.ranked:
            self.context.append(chunk)
        self.answer = rag.generate(MAIN_QUERY, self.ranked)

    def test_every_generated_sentence_is_covered(self):
        """Note what this does NOT prove.

        generate() copies sentences verbatim out of chunks that are also the
        evidence, so this assertion is true by construction and can never fail.
        It pins the plumbing (citations stripped correctly, the right chunk
        named) and nothing about the checker's power. TestCheckerCanBeBeaten
        below is where the checker is actually put under load.
        """
        for _, covered, chunk, missing in rag.verify(self.answer, self.context):
            self.assertTrue(covered)
            self.assertIsNotNone(chunk)
            self.assertEqual(missing, [])

    def test_verifier_flags_the_ungrounded_sentence(self):
        poisoned = "Written approval above five hundred dollars is returned within fourteen days. [HB-06]"
        results = rag.verify(self.answer + [poisoned], self.context)

        for _, covered, _, _ in results[:-1]:
            self.assertTrue(covered, "a real sentence was wrongly rejected")

        _, covered, _, missing = results[-1]
        self.assertFalse(covered)
        self.assertIn("fourteen", missing)                # the invented number

    def test_failure_b_narration_is_true_of_the_corpus(self):
        """The demo prints 'within' and 'days' as missing from the retrieved
        chunks but present in the handbook, and only 'fourteen' as invented.
        Both halves of that claim are checked here, because the earlier version
        of this file printed 'not in any chunk' and was wrong about two words.
        """
        handbook = rag.vocabulary([doc["text"] for doc in rag.CORPUS])
        retrieved = rag.vocabulary([chunk["text"] for chunk in self.context])
        for word in ["within", "days"]:
            self.assertIn(word, handbook)
            self.assertNotIn(word, retrieved)
        self.assertNotIn("fourteen", handbook)

    def test_verifier_fails_closed_on_empty_context(self):
        """No evidence must mean no grounding -- never a free pass."""
        for _, covered, chunk, missing in rag.verify(self.answer, []):
            self.assertFalse(covered)
            self.assertIsNone(chunk)
            self.assertTrue(missing)

    def test_verifier_fails_closed_on_a_contentless_sentence(self):
        for _, covered, _, _ in rag.verify(["The and of to. [HB-06]"], self.context):
            self.assertFalse(covered)


class TestCheckerCanBeBeaten(unittest.TestCase):
    """The limits of the check, pinned as tests.

    FAILURE C prints these two results. If either flips, the honesty comments
    in step 6 and the FAILURE C narration are lying and must be rewritten.
    """

    def setUp(self):
        self.chunks, self.vectors = build_index()
        ranked = rag.rerank(MAIN_QUERY, rag.retrieve(MAIN_QUERY, self.chunks, self.vectors))
        self.context = []
        for _, chunk in ranked:
            self.context.append(chunk)

    def test_negation_is_caught_because_not_is_a_content_word(self):
        sentence = "Approval is not requested in the Expense Portal. [HB-06]"
        _, covered, _, missing = rag.verify([sentence], self.context)[0]
        self.assertFalse(covered)
        self.assertEqual(missing, ["not"])

    def test_recombination_is_NOT_caught_and_cannot_be(self):
        """Every content word is present in HB-09#0; the limits are swapped.
        A set test has no way to see that, which is the whole point."""
        sentence = ("Approval limits: five thousand dollars for an expense, "
                    "five hundred dollars for equipment. [HB-09]")
        _, covered, chunk, missing = rag.verify([sentence], self.context)[0]
        self.assertTrue(covered)
        self.assertEqual(chunk["chunk_id"], "HB-09#0")
        self.assertEqual(missing, [])

    def test_a_discarded_stopword_is_a_hole_in_the_check(self):
        """'with' is a stopword, so a sentence saying the opposite of a chunk
        that says 'without' passes. Documented in STOPWORDS, checked here."""
        sentence = "Purchases made with prior approval can be refused at reimbursement. [HB-06]"
        context = []
        for chunk in self.chunks:
            if chunk["chunk_id"] == "HB-06#1":
                context.append(chunk)
        _, covered, _, _ = rag.verify([sentence], context)[0]
        self.assertTrue(covered)


if __name__ == "__main__":
    unittest.main()
