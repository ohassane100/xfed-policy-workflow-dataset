from copy import deepcopy
from pathlib import Path

from xfed.common import schema, validate


def classify_chunk(chunk, client, root=Path('.'), context=(), unresolved=()):
    from jsonschema import Draft202012Validator
    decision_schema = schema(3, root)['properties']['chunks']['items']['properties']['classification']
    prompt = (Path(root) / 'prompts/relevance_classifier.md').read_text(encoding='utf-8')
    source = {k: v for k, v in chunk.items() if k != 'classification'}
    result = client.generate(prompt, dict(clause=source, context=list(context),
                                         unresolved_references=list(unresolved)), decision_schema)
    Draft202012Validator(decision_schema).validate(result)
    return result


def classify(document, client, root=Path('.')):
    validate(document, 2, root)
    output = deepcopy(document)
    for chunk in output['chunks']:
        chunk['classification'] = classify_chunk(chunk, client, root)
    return validate(output, 3, root)
