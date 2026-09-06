"""Optional dependency-parser extractor; requires an explicitly installed spaCy model."""
import argparse
from pathlib import Path
from src.extractors.algorithmic.v1 import extract
from src.policy.parse_policy_md import parse, render
from src.utils.runs import save_run


def run(contract: Path, model: str, run_name: str = 'v2') -> Path:
    try:
        import spacy
    except ImportError as exc:
        raise RuntimeError('V2 needs the optional spaCy dependency and a trained dependency model') from exc
    nlp = spacy.load(model)
    if 'parser' not in nlp.pipe_names:
        raise ValueError('The selected NLP model has no dependency parser')
    text = (contract/'source/contract.txt').read_text(encoding='utf-8')
    output = parse(extract(text,contract.name,'algorithmic_v2'))
    for rule in output.rules:
        doc = nlp(rule['description'])
        subjects = [token.subtree for token in doc if token.dep_ in {'nsubj','nsubjpass'}]
        objects = [token.subtree for token in doc if token.dep_ in {'dobj','obj','pobj'}]
        fields = []
        if subjects:
            fields.append('Actor: '+'; '.join(' '.join(t.text for t in span) for span in subjects))
        if objects:
            fields.append('Data/object: '+'; '.join(' '.join(t.text for t in span) for span in objects))
        actions = [t.lemma_ for t in doc if t.pos_ == 'VERB']
        if actions:
            fields.append('Action: '+', '.join(actions))
        # Qualifiers and defined terms stay verbatim in evidence; no unsupported resolution.
        if fields:
            rule['applies_to'] = ' | '.join(fields)
    return save_run(contract,'algorithmic',run_name,render(output),
                    {'version':'v2','nlp_model':model,'nlp_model_version':nlp.meta.get('version'),
                     'spacy_version':spacy.__version__,'qualifiers':'retained verbatim; not semantically resolved'})


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract',type=Path); parser.add_argument('--model',required=True)
    parser.add_argument('--run-name',default='v2')
    args=parser.parse_args(); print(run(args.contract,args.model,args.run_name))
