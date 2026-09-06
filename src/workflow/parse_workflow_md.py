"""Deterministic parser for the supplied WORKFLOW.md template."""
from dataclasses import dataclass

META = ('Workflow ID','Project ID','Requested by','Host Company','Status','Signature Status')
SECTIONS = ('Stage','Purpose','Inputs','Shared Outputs','Stays Private','Dataset','Expected Result')


@dataclass
class Workflow:
    metadata: dict[str,str]
    sections: dict[str,str]


def parse(text: str) -> Workflow:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '# WORKFLOW.MD':
        raise ValueError('Expected # WORKFLOW.MD')
    metadata, sections = {}, {}
    current = None
    for line in lines[1:]:
        if line.startswith('## '):
            current = line[3:].strip()
            if current not in SECTIONS or current in sections:
                raise ValueError('Unknown/duplicate workflow section')
            sections[current] = []
        elif current:
            sections[current].append(line)
        elif line.strip():
            key, sep, value = line.partition(': ')
            if not sep or key not in META or key in metadata:
                raise ValueError('Invalid/duplicate workflow metadata')
            metadata[key] = value.strip()
    return Workflow(metadata,{k:'\n'.join(v).strip() for k,v in sections.items()})


def render(workflow: Workflow) -> str:
    lines = ['# WORKFLOW.MD','']+[f'{key}: {workflow.metadata[key]}' for key in META]
    for section in SECTIONS:
        lines += ['',f'## {section}','',workflow.sections[section]]
    return '\n'.join(lines)+'\n'
