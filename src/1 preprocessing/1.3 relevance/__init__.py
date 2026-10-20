from copy import deepcopy
from pathlib import Path
import json

from xfed.common import map_chunks, schema, validate


def classify_chunk(chunk, client, root=Path('.'), context=(), unresolved=()):
    from jsonschema import Draft202012Validator
    decision_schema = schema(3, root)['properties']['chunks']['items']['properties']['classification']
    prompt = (Path(root) / 'prompts/relevance_classifier.md').read_text(encoding='utf-8')
    source = {k: v for k, v in chunk.items() if k != 'classification'}
    value = dict(clause=source, context=list(context), unresolved_references=list(unresolved))
    budget = max(0, (getattr(client, 'num_ctx', 32768) - getattr(client, 'num_predict', 512) - 1024) * 2.5 - len(prompt))
    if len(json.dumps(value, ensure_ascii=False)) > budget:
        return dict(label='NEEDS_CONTEXT', reason='Clause and supporting context exceed the safe input budget; increase context tokens.')
    try:
        result = client.generate(prompt, value, decision_schema)
    except ValueError as error:
        if 'context limit reached' not in str(error).lower():
            raise
        return dict(label='NEEDS_CONTEXT', reason='Model context limit reached; no truncated clause was classified.')
    Draft202012Validator(decision_schema).validate(result)
    return result


def classify(document, client, root=Path('.'), progress=None):
    from xfed.enrichment import ContextIndex

    validate(document, 2, root)
    output = deepcopy(document)
    index = ContextIndex(output['chunks'])
    def decide(chunk):
        # Resolve structural dependencies first; full cross-references belong to enrichment.
        context, missing = index.context_for(chunk, include_references=False)
        return classify_chunk(chunk, client, root, context, missing)
    decisions = map_chunks(output['chunks'], decide, client, progress)
    for chunk, decision in zip(output['chunks'], decisions):
        chunk['classification'] = decision
    return validate(output, 3, root)
