"""The same six steps as tiny_rag.py, but with real components.

tiny_rag.py fakes two things so it can run with no install and no key: it uses
word-counting instead of a real embedding model, and a string template instead
of a language model. This file shows what those two steps actually look like.

It is deliberately short and deliberately incomplete. It does NOT run as-is:
you supply `embed()` and a populated store. The point is that swapping the fake
parts for real ones changes almost nothing about the shape of the pipeline.
The plumbing is the same. That is the article's argument.

Requires: pip install anthropic, plus ANTHROPIC_API_KEY (or `ant auth login`).
"""

import re

import anthropic

client = anthropic.Anthropic()

MODEL = "claude-opus-5"


# ---------------------------------------------------------------------------
# STEPS 1 AND 2: turn text into numbers, find the nearest stored passages.
#
# Anthropic has no first-party embeddings endpoint, so this half of a Claude
# based RAG system always comes from somewhere else: a hosted embedding API, or
# a model you run yourself. Whichever you pick, the rule from the article holds.
# The SAME model must embed your documents and your queries. Record which model
# produced each stored vector (the `model_name` column in the data model
# diagram) so that the day you switch, you know what has to be re-indexed.
# ---------------------------------------------------------------------------
def embed(text: str) -> list[float]:
    """Return a normalized vector for `text`. Supply your own implementation."""
    raise NotImplementedError("wire in your embedding model here")


def search(query_vector: list[float], k: int = 5) -> list[dict]:
    """Return the k nearest stored chunks as {"id", "text", "score"} dicts.

    In production this is an approximate nearest-neighbour index rather than a
    scan, because a scan does not survive scale. It can live in a dedicated
    vector database or inside a relational database you already run.
    """
    raise NotImplementedError("wire in your vector store here")


# ---------------------------------------------------------------------------
# STEP 3: rerank. A cross-encoder reads the question and one passage together
# and scores the pair, which is a much better judge of relevance than comparing
# two vectors that never met. Budget tens to hundreds of milliseconds per 100
# passages. That cost is why this is the step that quietly disappears under
# load, and why answers get worse in ways nobody attributes to a deploy.
# ---------------------------------------------------------------------------
def rerank(question: str, chunks: list[dict], k: int = 3) -> list[dict]:
    """Reorder `chunks` by a cross-encoder score, keep the top k. Yours to supply."""
    raise NotImplementedError("wire in your reranker here")


# ---------------------------------------------------------------------------
# STEPS 4 AND 5: paste the passages into the prompt, generate the answer.
# ---------------------------------------------------------------------------
SYSTEM = """You answer questions using only the passages provided below.

Rules:
- Use only what is in the passages. Do not use anything you know from training.
- Every factual sentence must end with its source marker, like [doc_142].
- If the passages do not contain the answer, reply exactly: "I don't know."
  Saying you don't know is a correct answer. Guessing is not."""


def answer(question: str, k: int = 5) -> str:
    chunks = search(embed(question), k=k)

    # Nothing retrieved means nothing to ground an answer in. Refuse here
    # rather than sending an empty context and hoping the model declines.
    # Fail closed: an unanswerable question is a fine outcome, an ungrounded
    # answer is not.
    if not chunks:
        return "I don't know."

    chunks = rerank(question, chunks)

    passages = "\n\n".join(f"[{c['id']}] {c['text']}" for c in chunks)

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"PASSAGES:\n{passages}\n\nQUESTION: {question}",
        }],
        # Adaptive thinking: the model decides how much reasoning this needs.
        # Note there is no `temperature` here. Current Claude models reject a
        # non-default temperature outright, and setting it to 0 never bought
        # the determinism people believed it did.
        thinking={"type": "adaptive"},
        # Server-side fallback. A safety classifier can decline a request and
        # return stop_reason "refusal"; this re-runs it on a suitable fallback
        # model inside the same call instead of handing your users nothing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    # Always check stop_reason before reading content. A refusal comes back as
    # HTTP 200 with no usable text, so this does not raise on its own.
    if response.stop_reason == "refusal":
        return "I don't know."

    text = "".join(b.text for b in response.content if b.type == "text")

    # -----------------------------------------------------------------------
    # STEP 6: the CHEAP HALF of the check, and it is important to be honest
    # about which half this is.
    #
    # What is NOT enough: asking the same model, or another model, whether the
    # answer is good. That is a second opinion, not a check.
    #
    # What this does: confirms every citation marker names a chunk that really
    # was in this request's context. It catches a fabricated citation, which is
    # common.
    #
    # What this does NOT catch is the failure this article opens with: a real,
    # retrieved citation sitting underneath a sentence it does not support. For
    # that you need coverage of the sentence against the retrieved text, which
    # tiny_rag.verify() implements in about 30 lines, or a trained entailment
    # model, which is what a production system should use.
    #
    # Note it fails CLOSED and returns a refusal rather than raising: an
    # unverifiable answer is a "don't know", not a crash.
    # -----------------------------------------------------------------------
    retrieved_ids = {c["id"] for c in chunks}
    cited_ids = set(re.findall(r"\[([^\]]+)\]", text))
    fabricated = cited_ids - retrieved_ids
    if fabricated:
        return "I don't know."

    return text


if __name__ == "__main__":
    print(__doc__)
