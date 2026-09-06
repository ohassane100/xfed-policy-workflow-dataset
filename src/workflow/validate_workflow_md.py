import argparse
from pathlib import Path
from src.workflow.parse_workflow_md import META, SECTIONS, Workflow, parse


def validate(text: str) -> Workflow:
    result = parse(text)
    if set(result.metadata) != set(META) or set(result.sections) != set(SECTIONS):
        raise ValueError('Missing workflow metadata or sections')
    if any(not value.strip() for value in [*result.metadata.values(), *result.sections.values()]):
        raise ValueError('Workflow fields cannot be empty; use explicit None where appropriate')
    for section in ('Inputs','Shared Outputs','Stays Private'):
        if any(not line.startswith('- ') or not line[2:].strip() for line in result.sections[section].splitlines()):
            raise ValueError(f'{section} must contain Markdown bullet items')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('workflow',type=Path)
    args = parser.parse_args()
    validate(args.workflow.read_text(encoding='utf-8'))
    print('Valid WORKFLOW.md')
