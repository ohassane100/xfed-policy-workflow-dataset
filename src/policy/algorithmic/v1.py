from __future__ import annotations

import re
import json
from functools import lru_cache
from hashlib import sha1


RELEVANT_TERMS = (
    "access", "background", "confidential", "information", "data",
    "project result", "publish", "publication", "disclose", "disclosure",
    "store", "storage", "use", "utilisation", "utilization", "transfer",
    "third party", "personal data", "research", "commercial"
)

CLAUSE_RE = re.compile(
    r"^\s*(\d+(?:\s*\.\s*\d+)+|\d+[.)]|\d+(?=\s+[A-Z]))"
    r"(?:\.)?\s+(\S.*)$"
)
MODAL_RE = re.compile(
    r"\b(?:shall\s+not|must\s+not|may\s+not|(?:is|are)\s+not\s+permitted|"
    r"(?:is|are)\s+prohibited|prohibited|not\s+entitled|"
    r"shall\s+have\s+access|(?:have|has)\s+access|"
    r"(?:is|are)\s+(?:required|permitted|entitled)\s+to|"
    r"permitted|entitled|shall|must|may|required|obligation\s+to)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def sentencizer():
    try:
        import spacy
    except ImportError:
        return None
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    return nlp


def split_sentences(text: str) -> list[str]:
    nlp = sentencizer()
    if nlp is not None:
        return [s.text.strip() for s in nlp(text).sents if s.text.strip()]
    # Keep semicolons and their exceptions together; avoid splitting clause
    # references and common abbreviations in the regex fallback.
    return [s.strip() for s in re.split(
        r"(?<!\bMr\.)(?<!\bDr\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bNo\.)"
        r"(?<=[.!?])\s+(?=[A-Z])", text
    ) if s.strip()]


def split_numbered_clauses(text: str) -> list[tuple[str, str]]:
    # Some PDFs put a line break inside a clause number ("5\n.2.4").
    text = re.sub(r"(?m)^(\d+)\s*\n\s*(?=\.\d)", r"\1", text)
    clauses = []
    clause_id = "unnumbered"
    lines = []

    def flush():
        nonlocal clause_id, lines
        if clause_id and lines:
            body = re.sub(r"\s+", " ", " ".join(lines)).strip()
            if body:
                clauses.append((clause_id, body))

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Repeated page furniture is not part of the legal sentence.
        if re.fullmatch(r"Side\s+\d+\s+av\s+\d+", line):
            continue
        if line.startswith("«") and "project no." in line:
            continue
        if re.fullmatch(r"\d+(?:\s*\.\s*\d+)*", line):
            # Detached numbers can be page furniture or displaced labels.
            # Do not attribute subsequent text to the preceding clause.
            flush()
            clause_id, lines = "unresolved (detached numbering)", []
            continue

        match = CLAUSE_RE.match(line)
        if match:
            flush()
            clause_id = re.sub(r"\s+", "", match.group(1)).rstrip(".)")
            lines = [match.group(2)]
        elif clause_id:
            lines.append(line)

    flush()
    return clauses


def classify(sentence: str) -> str | None:
    effects = set()
    for match in MODAL_RE.finditer(sentence):
        phrase = re.sub(r"\s+", " ", match.group().lower())
        if "not" in phrase or "prohibited" in phrase:
            effects.add("deny")
        elif any(word in phrase for word in ("may", "permitted", "entitled", "access")):
            effects.add("allow")
        else:
            effects.add("require")
    # A single V1 effect cannot safely represent mixed permissions/obligations.
    return effects.pop() if len(effects) == 1 else None


def relevant(sentence: str) -> bool:
    lower = sentence.lower()
    return any(term in lower for term in RELEVANT_TERMS)


def rough_parts(sentence: str) -> tuple[str, str, str] | None:
    """Return only directly observed actor, first action word and remainder."""
    matches = list(MODAL_RE.finditer(sentence))
    if len(matches) != 1:
        return None
    modal = matches[0]
    actor = sentence[:modal.start()].strip()
    tail = sentence[modal.end():].strip().removeprefix("to ")
    parts = tail.split(maxsplit=1)
    if (not actor or len(parts) != 2 or
            re.search(r"[,;:]|\b(if|unless|where|when|provided|subject)\b", actor, re.I)):
        return None
    return actor, parts[0], parts[1]


def rough_action(sentence: str) -> str:
    lower = sentence.lower()

    markers = [
        "shall not", "must not", "may not", "shall", "must", "may",
        "is required to", "are required to", "is permitted to",
        "are permitted to", "is entitled to", "are entitled to"
    ]

    for marker in markers:
        idx = lower.find(marker)
        if idx >= 0:
            tail = sentence[idx + len(marker):].strip(" ,:")
            if tail:
                return tail

    return sentence.strip()


def rule_fields(sentence: str) -> tuple[str, str, str]:
    """Separate observed subject, recipient and resource for simple active rules.

    Complex/passive constructions remain unresolved rather than assigning the
    grammatical subject (which may be data) as an obligated party.
    """
    unknown = "Unresolved; see source text"
    parts = rough_parts(sentence)
    if parts is None:
        return unknown, unknown, unknown
    actor, action, remainder = parts
    if action.lower() not in {
        "share", "disclose", "transfer", "send", "provide", "release",
        "use", "access", "process", "store", "retain", "delete", "destroy",
        "protect", "publish", "keep", "return", "export", "analyse", "analyze",
    }:
        return unknown, unknown, unknown
    # Pronouns need antecedent resolution, which V1 does not implement.
    if actor.lower() in {"it", "they", "he", "she", "this", "that"}:
        actor = unknown
    # Qualifiers remain in the full description and source quotation.
    core = re.split(
        r"[,;]|\b(?:if|unless|except|provided|subject to|when|before|after|"
        r"solely|only|for|under|in accordance with|without|within)\b",
        remainder, maxsplit=1, flags=re.I,
    )[0].strip().rstrip(".")
    recipient = "Not specified"
    resource = core
    if action.lower() in {"share", "disclose", "transfer", "send", "provide",
                          "release", "return", "export"}:
        transfer = re.fullmatch(r"(.+?)\s+(?:to|with)\s+(.+)", core, re.I)
        if transfer:
            resource, target = transfer.groups()
            # Do not label a location/system as a recipient party.
            if re.search(r"\b(?:part(?:y|ies)|recipient|partner|company|companies|"
                         r"researcher|authority|owner)\b", target, re.I):
                recipient = target
            else:
                recipient = unknown
    resource = re.sub(r"^(?:the|a|an)\s+", "", resource, flags=re.I)
    if not re.search(r"\b(?:data|information|results?|background|records?|"
                     r"dataset|outputs?|files?|materials?|software|workflow)\b",
                     resource, re.I):
        resource = unknown
    return actor, recipient, resource or unknown


def rule_id(effect: str, sentence: str) -> str:
    digest = sha1(sentence.encode("utf-8")).hexdigest()[:6]
    words = re.findall(r"[a-z0-9]+", rough_action(sentence).lower())[:5]
    base = "_".join(words) or effect
    return f"{effect}_{base}_{digest}"


def extract_parties(contract_text: str) -> list[str]:
    """Read registered parties from an explicit agreement party list.

    Keep names, stated roles and registration numbers together. Do not infer
    parties from organisations merely mentioned elsewhere in the agreement.
    Unrecognised list formats are left for manual review.
    """
    block = re.search(
        r"entered\s+into\s+between\s*:(.*?)hereinafter\s+collectively",
        contract_text, re.IGNORECASE | re.DOTALL,
    )
    if block is None:
        return []
    # PDF extraction may detach the numbered column from the party names.
    text = re.sub(r"(?m)^\s*\d+[.)]?\s*$", "", block.group(1))
    text = re.sub(r"(?m)^\s*\d+[.)]?\s+(?=\D)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    entries = list(re.finditer(
        r"([^,;]+,\s*org\.?\s*nr\.?\s*(?:\d\s*){9})(?=[,;]|$)",
        text, re.IGNORECASE,
    ))
    # Avoid silently presenting a partially recognised list as complete.
    remainder = text
    for entry in reversed(entries):
        remainder = remainder[:entry.start()] + remainder[entry.end():]
    if remainder.strip(" ,;"):
        return []
    return list(dict.fromkeys(entry.group(1).strip() for entry in entries))


def extract_policy_markdown(contract_text: str, contract_id: str) -> str:
    rules = []
    seen = set()

    for clause_id, clause_text in split_numbered_clauses(contract_text):
        for sentence in split_sentences(clause_text):
            sentence = re.sub(r"\s+", " ", sentence).strip()

            if not sentence or sentence in seen or not relevant(sentence):
                continue

            effect = classify(sentence)
            if effect is None:
                continue

            # Conservative V1: preserve original legal sentence meaning.
            description = sentence
            actor, recipient, scope = rule_fields(sentence)
            rid = rule_id(effect, sentence)
            rules.append((effect, rid, description, clause_id, actor, recipient, scope))
            seen.add(sentence)

    lines = [
        "# POLICY.MD",
        "",
        f"Policy ID: policy-{contract_id}-algorithmic-v1",
        f"Contract ID: {contract_id}",
        "Project ID: Unresolved",
        "Generated By: algorithmic_v1",
        "Source: source/contract.txt",
        "Status: candidate",
        "",
        "## Parties",
        "",
    ]
    parties = extract_parties(contract_text)
    if parties:
        lines.extend(f"- {party}" for party in parties)
    else:
        lines.append("_Unresolved; identify the parties from the source agreement._")
    lines.extend(["", "## Enforcement Rules", ""])

    if not rules:
        lines.append("_No candidate enforcement rules detected._")
    else:
        for effect, rid, description, clause_id, actor, recipient, scope in rules:
            lines.append(f"- `{effect}` **{rid}** — {description}")
            lines.append(f"  - Actor: {actor}")
            lines.append(f"  - Counterparty/Recipient: {recipient}")
            lines.append(f"  - Applies to: {scope}")
            lines.append(f"  - Source clause: {clause_id}")
            lines.append(f"  - Source text: {json.dumps(description, ensure_ascii=False)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
