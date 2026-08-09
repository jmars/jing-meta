"""Shared text-processing constants.

Used by the dreamer's Python tokenizer and the Soufflé pipeline's token
CSV export so both agree on token sets, keeping overlap-count semantics
consistent.
"""

STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "it", "this", "that", "from", "by",
    "at", "as", "its", "his", "her", "their", "we", "you", "they", "not", "no",
    "has", "have", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "into", "via", "through", "using", "used", "use", "more", "most",
})
