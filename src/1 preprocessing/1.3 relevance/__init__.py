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
    maximum_output = getattr(client, 'num_predict', 512)
    initial_output = min(maximum_output, getattr(client, 'relevance_num_predict', 512))
    options = {}
    if getattr(client, 'relevance_think', None) is not None:
        options['think'] = client.relevance_think
    # Classification is short; a bounded retry preserves complete answers when needed.
    for output_limit in dict.fromkeys((initial_output, maximum_output)):
        try:
            result = client.generate(prompt, value, decision_schema, num_predict=output_limit, **options)
        except ValueError as error:
            message = str(error).lower()
            if 'context limit reached' in message:
                return dict(label='NEEDS_CONTEXT', reason='Model context limit reached; no truncated clause was classified.')
            if 'output token limit reached' in message and output_limit < maximum_output:
                continue
            raise
        Draft202012Validator(decision_schema).validate(result)
        return result


def classify_batch(items, client, root=Path('.')):
    from jsonschema import Draft202012Validator, ValidationError

    if len(items) == 1:
        item = items[0]
        return [classify_chunk(item['clause'], client, root, item['context'], item['unresolved_references'])]
    decision = schema(3, root)['properties']['chunks']['items']['properties']['classification']
    identifiers = [item['clause']['chunk_id'] for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('Batch input chunk IDs must be unique.')
    response_schema = dict(type='object', additionalProperties=False, required=['classifications'],
                          properties=dict(classifications=dict(
                              type='array', minItems=len(items), maxItems=len(items),
                              items=dict(type='object', additionalProperties=False,
                                         required=['chunk_id', 'classification'],
                                         properties=dict(chunk_id=dict(type='string', enum=identifiers),
                                                         classification=decision)))))
    prompt = (Path(root) / 'prompts/relevance_classifier.md').read_text(encoding='utf-8')
    prompt = prompt.replace(
        'Return only the JSON classification object constrained by the supplied schema:',
        'Return only the JSON batch object constrained by the supplied schema. Each classification contains:'
    )
    prompt += (
        '\nClassify every entry in clauses independently. Each entry has its own clause, context, '
        'and unresolved references. Do not borrow evidence from another entry. Return classifications '
        'with exactly one chunk_id and classification object per supplied clause. Keep reasons concise.'
    )
    output_limit = min(getattr(client, 'num_predict', 512),
                       len(items) * getattr(client, 'relevance_num_predict', 512))
    options = dict(num_predict=output_limit)
    if getattr(client, 'relevance_think', None) is not None:
        options['think'] = client.relevance_think
    try:
        answer = client.generate(prompt, dict(clauses=items), response_schema, **options)
        Draft202012Validator(response_schema).validate(answer)
        decisions = {row['chunk_id']: row['classification'] for row in answer['classifications']}
        if len(decisions) != len(identifiers) or set(decisions) != set(identifiers):
            raise ValueError('Batch response must classify each supplied chunk exactly once.')
        return [decisions[identifier] for identifier in identifiers]
    except (ValueError, ValidationError):
        # Never accept missing, duplicated, truncated, or invalid batch decisions.
        return [classify_chunk(item['clause'], client, root, item['context'], item['unresolved_references'])
                for item in items]


def classify(document, client, root=Path('.'), progress=None):
    from xfed.enrichment import ContextIndex

    validate(document, 2, root)
    output = deepcopy(document)
    index = ContextIndex(output['chunks'])
    batch_size = getattr(client, 'relevance_batch_size', 1)
    if type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise ValueError('relevance_batch_size must be between 1 and 16.')
    prompt = (Path(root) / 'prompts/relevance_classifier.md').read_text(encoding='utf-8')
    # Reserve extra space for the batch instructions and structured response.
    budget = max(0, (getattr(client, 'num_ctx', 32768) - getattr(client, 'num_predict', 512) - 1024)
                 * 2.5 - len(prompt) - 512)
    groups, current = [], []
    for chunk in output['chunks']:
        context, missing = index.context_for(chunk, include_references=False)
        source = {key: value for key, value in chunk.items() if key != 'classification'}
        item = dict(clause=source, context=list(context), unresolved_references=list(missing))
        if current and (len(current) >= batch_size or
                        len(json.dumps(dict(clauses=[*current, item]), ensure_ascii=False)) > budget):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    work = [dict(chunk_id=group[0]['clause']['chunk_id'],
                 chunk_ids=[item['clause']['chunk_id'] for item in group], items=group)
            for group in groups]
    lengths = {group['chunk_id']: len(group['items']) for group in work}
    completed = 0
    def report(done, total, message):
        nonlocal completed
        if done:
            completed += lengths[message]
        if progress:
            progress(completed, len(output['chunks']), message)
    decisions = map_chunks(work, lambda group, worker: classify_batch(group['items'], worker, root),
                           client, report if progress else None)
    for chunk, decision in zip(output['chunks'], (value for group in decisions for value in group)):
        chunk['classification'] = decision
    return validate(output, 3, root)
