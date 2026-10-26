from copy import deepcopy
from pathlib import Path
import json
import re

from xfed.common import map_chunks, schema, validate


def classification_request(items, client, root=Path('.')):
    """Build the actual prompt, evidence and response schema for a group of clauses."""
    decision = schema(3, root)['properties']['chunks']['items']['properties']['classification']
    prompt = (Path(root) / 'prompts/relevance_classifier.md').read_text(encoding='utf-8')
    if getattr(client, 'relevance_compact', False):
        # Source pages and block IDs remain in the output; inference needs the contractual text.
        fields = ('clause_id', 'parent_clause_id', 'heading', 'text')
        context_pool, context_ids, clauses = [], {}, []
        for position, item in enumerate(items):
            references = []
            for entry in item['context']:
                evidence = {key: entry[key] for key in fields if key in entry}
                key = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
                if key not in context_ids:
                    context_ids[key] = len(context_pool)
                    context_pool.append(evidence)
                references.append(dict(ref=context_ids[key], relationship=entry.get('relationship', 'other')))
            clauses.append(dict(id=str(position),
                                clause={key: item['clause'][key] for key in fields if key in item['clause']},
                                context=references, unresolved_references=item['unresolved_references']))
        keys = [str(position) for position in range(len(items))]
        response_schema = dict(type='object', additionalProperties=False, required=['labels'],
                               properties=dict(labels=dict(type='object', additionalProperties=False,
                                   required=keys, properties={key: decision['properties']['label'] for key in keys})))
        prompt = prompt.replace('label and an optional concise reason.',
                                "a labels object mapping each entry's id field to its label, without reasons.")
        prompt = prompt.replace('otherwise keep NEEDS_CONTEXT and explain\nwhat is missing.',
                                'otherwise keep NEEDS_CONTEXT.')
        prompt += (
            '\nClassify each entry in clauses independently. Its context entries reference the zero-based '
            'context_pool array by ref, with the relationship given in that entry. Use only that clause and '
            'its listed context; unlisted pool entries and other clauses are not its evidence. '
            'Return exactly one label for every supplied id in labels. Use compact single-line JSON. '
            'Do not generate reasons or repeat text.'
        )
        return prompt, dict(context_pool=context_pool, clauses=clauses), response_schema
    if len(items) == 1:
        item = items[0]
        return prompt, item, decision
    identifiers = [item['clause']['chunk_id'] for item in items]
    response_schema = dict(type='object', additionalProperties=False, required=['classifications'],
                          properties=dict(classifications=dict(
                              type='array', minItems=len(items), maxItems=len(items),
                              items=dict(type='object', additionalProperties=False,
                                         required=['chunk_id', 'classification'],
                                         properties=dict(chunk_id=dict(type='string', enum=identifiers),
                                                         classification=decision)))))
    prompt = prompt.replace(
        'Return only the JSON classification object constrained by the supplied schema:',
        'Return only the JSON batch object constrained by the supplied schema. Each classification contains:'
    )
    prompt += (
        '\nClassify every entry in clauses independently. Each entry has its own clause, context, '
        'and unresolved references. Do not borrow evidence from another entry. Return classifications '
        'with exactly one chunk_id and classification object per supplied clause. Keep reasons concise.'
    )
    return prompt, dict(clauses=items), response_schema


def request_bytes(items, client, root=Path('.')):
    prompt, value, response_schema = classification_request(items, client, root)
    options = dict(ensure_ascii=False)
    if getattr(client, 'relevance_compact', False):
        options['separators'] = (',', ':')
    return len((prompt + json.dumps(value, **options) + json.dumps(response_schema)).encode('utf-8'))


def output_limit(items, client):
    return getattr(client, 'relevance_num_predict', getattr(client, 'num_predict', 4096))


def fits(items, client, root, num_ctx, limit=None):
    # Conservative estimate only; truncate=False makes the server reject actual token overflow.
    limit = output_limit(items, client) if limit is None else limit
    return request_bytes(items, client, root) <= max(0, num_ctx - limit - 1024) * 2.5


def classify_chunk(chunk, client, root=Path('.'), context=(), unresolved=()):
    item = dict(clause={key: value for key, value in chunk.items() if key != 'classification'},
                context=list(context), unresolved_references=list(unresolved))
    return classify_batch([item], client, root)[0]


def classify_batch(items, client, root=Path('.'), num_ctx=None):
    from jsonschema import Draft202012Validator, ValidationError

    if not items:
        return []
    identifiers = [item['clause']['chunk_id'] for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('Batch input chunk IDs must be unique.')
    # Only metadata-confirmed short headings qualify; substantive merged text
    # and sentence fragments still need the model and their source context.
    headings = [bool(item['clause'].get('heading'))
                and item['clause']['text'].strip() == item['clause']['heading'].strip()
                and len(item['clause']['text'].split()) <= 14
                and not re.search(r'\b(?:shall|must|may|means|is|are)\b|[;:]\s*\S',
                                  item['clause']['text'], re.I)
                for item in items]
    if any(headings):
        remaining = iter(classify_batch([item for item, heading in zip(items, headings) if not heading],
                                        client, root, num_ctx))
        return [dict(label='NOT_RELEVANT', reason='Standalone heading; retained as supporting context.')
                if heading else next(remaining) for heading in headings]
    prompt, value, response_schema = classification_request(items, client, root)
    maximum_context = getattr(client, 'num_ctx', 32768)
    num_ctx = num_ctx or getattr(client, 'relevance_num_ctx', 0) or maximum_context
    limit = output_limit(items, client)
    options = dict(truncate=False, compact_json=getattr(client, 'relevance_compact', False))
    if getattr(client, 'relevance_num_batch', None) is not None:
        options['num_batch'] = client.relevance_num_batch
    thinking = getattr(client, 'relevance_think', None)
    if thinking == 'auto':
        thinking = getattr(client, 'auto_relevance_think', None)
    if thinking is not None:
        options['think'] = thinking
    try:
        # Only retry when a larger context can fit the same complete evidence.
        for _ in range(2):
            if not fits(items, client, root, num_ctx, limit):
                num_ctx = maximum_context
                if not fits(items, client, root, num_ctx, limit):
                    raise ValueError('Clause and supporting context exceed the safe input budget; increase context tokens.')
            try:
                answer = client.generate(prompt, value, response_schema, num_predict=limit, num_ctx=num_ctx, **options)
                break
            except ValueError as error:
                message = str(error).lower()
                if 'context limit reached' in message and num_ctx < maximum_context:
                    num_ctx = maximum_context
                    continue
                raise
        else:
            raise ValueError('Classification could not complete within the context and output limits.')
        Draft202012Validator(response_schema).validate(answer)
        if getattr(client, 'relevance_compact', False):
            return [dict(label=answer['labels'][str(position)]) for position in range(len(items))]
        if len(items) == 1:
            return [answer]
        decisions = {row['chunk_id']: row['classification'] for row in answer['classifications']}
        if len(decisions) != len(identifiers) or set(decisions) != set(identifiers):
            raise ValueError('Batch response must classify each supplied chunk exactly once.')
        return [decisions[identifier] for identifier in identifiers]
    except (ValueError, ValidationError) as error:
        if hasattr(client, 'discard_last_response'):
            client.discard_last_response()
        if 'output token limit reached' in str(error).lower():
            raise ValueError(f'Classification failed: {error} '
                             'Adjust Classification thinking or Classification output tokens (1.3). '
                             'The request was not repeated or split after reaching the token limit.') from error
        if len(items) == 1:
            if any(text in str(error).lower() for text in ('context limit reached', 'safe input budget')):
                return [dict(label='NEEDS_CONTEXT', reason=str(error))]
            raise
        # Keep valid small batches instead of falling straight back to N individual requests.
        middle = len(items) // 2
        return (classify_batch(items[:middle], client, root, num_ctx) +
                classify_batch(items[middle:], client, root, num_ctx))


def classification_batches(document, client, root=Path('.'), chunk_ids=None, include_references=False, blocks=()):
    from xfed.enrichment import ContextIndex

    validate(document, 2, root)
    index = ContextIndex(document['chunks'], blocks)
    batch_size = getattr(client, 'relevance_batch_size', 1)
    if type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise ValueError('relevance_batch_size must be between 1 and 16.')
    items = []
    for chunk in document['chunks']:
        if chunk_ids is not None and chunk['chunk_id'] not in chunk_ids:
            continue
        context, missing = index.context_for(chunk, include_references=include_references)
        source = {key: value for key, value in chunk.items() if key != 'classification'}
        item = dict(clause=source, context=list(context), unresolved_references=list(missing))
        items.append(item)
    maximum_context = getattr(client, 'num_ctx', 32768)
    # Match the run allocation by default, as in the original one-clause pipeline.
    num_ctx = getattr(client, 'relevance_num_ctx', 0) or maximum_context
    groups, current = [], []
    for item in items:
        if current and (len(current) >= batch_size or not fits([*current, item], client, root, num_ctx)):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return [dict(chunk_id=group[0]['clause']['chunk_id'],
                 chunk_ids=[item['clause']['chunk_id'] for item in group], items=group, num_ctx=num_ctx)
            for group in groups]


def classify(document, client, root=Path('.'), progress=None, blocks=()):
    output = deepcopy(document)
    if progress:
        progress(0, len(output['chunks']), 'Preparing classification requests')
    work = classification_batches(output, client, root, blocks=blocks)
    lengths = {group['chunk_id']: len(group['items']) for group in work}
    completed = 0
    def report(done, total, message):
        nonlocal completed
        if done:
            completed += lengths[message]
        if progress:
            progress(completed, len(output['chunks']), message)
    decisions = map_chunks(work, lambda group, worker: classify_batch(group['items'], worker, root, group['num_ctx']),
                           client, report if progress else None)
    for chunk, decision in zip(output['chunks'], (value for group in decisions for value in group)):
        chunk['classification'] = decision
    # Resolve genuine uncertainty with additional source evidence, not another blind retry.
    initial = {item['clause']['chunk_id']: item for group in work for item in group['items']}
    pending = {chunk['chunk_id'] for chunk in output['chunks']
               if chunk['classification']['label'] == 'NEEDS_CONTEXT'
               and 'safe input budget' not in chunk['classification'].get('reason', '').lower()
               and 'context limit reached' not in chunk['classification'].get('reason', '').lower()}
    if pending:
        retry = classification_batches(document, client, root, pending, include_references=True, blocks=blocks)
        retry = [dict(group, items=[item for item in group['items']
                                   if item['context'] != initial[item['clause']['chunk_id']]['context']])
                 for group in retry]
        retry = [dict(group, chunk_ids=[item['clause']['chunk_id'] for item in group['items']],
                      chunk_id=group['items'][0]['clause']['chunk_id']) for group in retry if group['items']]
        if retry:
            extra = sum(len(group['items']) for group in retry)
            if progress:
                progress(len(output['chunks']), len(output['chunks']) + extra, 'Resolving uncertain clauses with referenced evidence')
            lengths = {group['chunk_id']: len(group['items']) for group in retry}
            completed = len(output['chunks'])
            def report_retry(done, total, message):
                nonlocal completed
                if done:
                    completed += lengths[message]
                if progress:
                    progress(completed, len(output['chunks']) + extra, 'Resolving referenced context: ' + message)
            resolved = map_chunks(retry, lambda group, worker: classify_batch(
                group['items'], worker, root, group['num_ctx']), client, report_retry if progress else None)
            by_id = {item['clause']['chunk_id']: decision for group, values in zip(retry, resolved)
                     for item, decision in zip(group['items'], values)}
            for chunk in output['chunks']:
                if chunk['chunk_id'] in by_id:
                    chunk['classification'] = by_id[chunk['chunk_id']]
    if progress:
        progress(len(output['chunks']), len(output['chunks']), 'Complete')
    return validate(output, 3, root)
