"""Word handling shared by the checks that read the question: the words that
carry no content, the words that name a period, crude stems, phrase matching.

Deliberately small and deterministic: these decide only whether a word the
question used is carried by something the query uses, never what it means.
"""

from __future__ import annotations

from services.cube.lens_view import norm, toks

# Words that carry no content of their own: function words, ranking and
# comparison words, the words every analytic question shares, and the
# glossing words people wrap a question in ("I want to know", "what does it
# mean", "can you tell me where", "through"). None of them names anything in
# a model, so a ledger that checked them stopped on every polite sentence.
# Operator words (average, rate, share, growth) are NOT here: they ask for an
# operation over the named measures and are checked as such (``OPERATORS``).
STOP = set(
    """a an the of in on at for to from by with and or vs versus compared compare comparing
comparison than what what's whats which who whom whose how many much is are was were be been
being did do does done have has had we our ours us you your i me my it its this that these those
there their them they get got go went come came coming per each every all any some so far as up
out into over between within since about show list give tell please number count total amount
figure figures breakdown broken down split top most least highest lowest
worst best biggest largest smallest more less fewer greater higher lower why actually right now
currently current overall whole same time period periods there's vs. not just only also still yet
ever
means mean meant meaning want wants wanted need needs needed would could should can might
where through send please explain explained exactly really like kind sort thing things something
anything someone anyone know knows knew wondering wonder think thinks thought looking look see
check checking find found based wise terms term regarding re""".split()
)

# Operator words: each asks for an operation over the measures the question
# names, never for a thing of its own. The word check passes one when the
# queries carry the operation (an average measure; a ratio measure, or two
# measures composed; a comparison window or a growth measure) and otherwise
# asks, naming the candidate measures, instead of reading it as an unbound
# noun or letting it through unchecked.
OPERATORS: dict[str, str] = {
    **dict.fromkeys("average avg mean median".split(), "average"),
    **dict.fromkeys(
        "rate rates share shares percentage percent pct ratio ratios proportion conversion".split(),
        "ratio",
    ),
    **dict.fromkeys(
        """growth grow grew grown growing change changed changes increase increased decrease
        decreased delta difference diff fall fell drop dropped jump jumped spike spiked""".split(),
        "change",
    ),
}
# The words a member's own text carries the operation by.
OPERATOR_TEXT: dict[str, tuple[str, ...]] = {
    "average": ("average", "avg", "mean", "median"),
    "ratio": ("rate", "share", "ratio", "percent", "pct", "percentage", "proportion", "conversion"),
    "change": ("growth", "change", "delta", "increase", "decrease", "difference", "diff"),
}

# Words that name a period: covered by any window the query reads.
PERIOD = set(
    """today yesterday week weeks weekly month months monthly quarter quarters quarterly year years
yearly annual ytd day days daily last next previous prior before after ago so-far since date dates
january february march april may june july august september october november december jan feb mar
apr jun jul aug sep sept oct nov dec q1 q2 q3 q4 h1 h2 this""".split()
)


def stem(word: str) -> str:
    w = norm(word).strip("'").removesuffix("'s")
    for suffix in ("ies", "ing", "es", "ed", "s"):
        if len(w) > 4 + len(suffix) - 1 and w.endswith(suffix):
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def has_stem(text: str, st: str) -> bool:
    """Some word of ``text`` has the stem ``st`` (or one is a prefix of the
    other, both at least four letters)."""
    for t in toks(text):
        s = stem(t)
        if s == st or (len(st) >= 4 and s.startswith(st)) or (len(s) >= 4 and st.startswith(s)):
            return True
    return False


def _light(word: str) -> str:
    # Plural only: "contracting" is not "contract".
    return word.removesuffix("'s").removesuffix("s") if len(word) > 3 else word


def phrase_in(text: str, phrase: str) -> bool:
    """The words of ``phrase`` appear consecutively in ``text``."""
    t = [_light(x) for x in toks(text)]
    p = [_light(x) for x in toks(phrase)]
    return bool(p) and any(t[i : i + len(p)] == p for i in range(len(t) - len(p) + 1))
