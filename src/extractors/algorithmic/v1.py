"""Conservative deontic candidate extraction with exact source evidence."""
import argparse
import hashlib
from pathlib import Path
import re
from src.policy.parse_policy_md import Policy, render

MODAL = re.compile(r'\b(may\s+not|shall\s+not|must\s+not|not\s+(?:be\s+)?(?:permitted|allowed)|prohibited|may|shall|must|required|permitted|allowed)\b',re.I)


def segments(text: str) -> list[tuple[str,str]]:
    result = []
    for page_no, page in enumerate(text.split('\f'),1):
        current_clause = f'page {page_no}; clause not identified'
        for block in re.split(r'\n[ \t]*\n',page):
            for sentence in re.split(r'(?<=[.!?;])(?=\s+[A-Z])',block):
                sentence = sentence.strip()
                number = re.match(r'^(\d+(?:\.\d+)*|\([a-z]\))[.)]?\s+',sentence)
                if number:
                    current_clause = number[1]
                if sentence:
                    result.append((current_clause,sentence))
    return result


def extract(text: str, contract_id: str, drafted_by: str = 'algorithmic_v1') -> str:
    rules = []
    seen = set()
    for clause, sentence in segments(text):
        match = MODAL.search(sentence)
        if not match:
            continue
        modal = ' '.join(match[0].lower().split())
        effect = 'deny' if 'not' in modal or modal == 'prohibited' else 'allow' if modal in {'may','permitted','allowed'} else 'require'
        # Preserve complete evidence including qualifiers, rather than inventing fields.
        description = ' '.join(sentence.split())
        identifier = 'contract_'+hashlib.sha256((clause+'|'+description).encode()).hexdigest()[:12]
        if identifier in seen:
            continue
        seen.add(identifier)
        rules.append(dict(effect=effect,rule_id=identifier,description=description,
                          applies_to='Scope as stated in the quoted clause; review required',
                          source_clause=clause, source_text=sentence))
    metadata = {'Policy ID':'policy-'+contract_id,'Project ID':'contract-policy-research',
                'Company A':'Not resolved; see source agreement','Company B':'Not resolved; see source agreement',
                'Drafted by':drafted_by,'Source':'uploaded_contract_text','Source Contract':contract_id,'Status':'candidate'}
    return render(Policy(metadata,rules))


def run(contract: Path, run_name: str = 'v1') -> Path:
    from src.utils.runs import save_run
    text = (contract/'source/contract.txt').read_text(encoding='utf-8')
    return save_run(contract,'algorithmic',run_name,extract(text,contract.name),
                    {'extractor':'algorithmic','version':'v1','modal_pattern':MODAL.pattern})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract',type=Path)
    parser.add_argument('--run-name',default='v1')
    args = parser.parse_args()
    print(run(args.contract,args.run_name))


if __name__ == '__main__':
    main()
