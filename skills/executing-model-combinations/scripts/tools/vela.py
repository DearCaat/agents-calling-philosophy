"""Tool vela: judgment (systemone) for the vela models, which accept object states and span questions.

Input line: {"id", "state": "<text>" or {"context": "...", "answer": "..."}, "questions": {...},
"model": "<id>" (or --model), "images"?: [<path or data URI>]}. Offsets of span answers are
character offsets into answer for an object state, into the string otherwise.
"""
from tools import ToolError
from tools import systemone

NAME = 'vela'
DESCRIPTION = 'judgment for the vela models; object states and span questions allowed'
PATH = '/vela/systemone'
DEFAULT_CONCURRENCY = 64
DEFAULT_TIMEOUT = 160.0


def add_arguments(parser):
    systemone.add_arguments(parser)


def check_args(args):
    systemone.check_args(args)


def prepare(item, args):
    state = item.get('state')
    if isinstance(state, dict):
        if not (isinstance(state.get('context'), str) and isinstance(state.get('answer'), str)):
            raise ToolError('an object state needs string "context" and "answer"')
    elif not isinstance(state, str) or not state.strip():
        raise ToolError('needs a nonempty string or {"context", "answer"} "state"')
    questions = item.get('questions')
    systemone.check_questions(questions)
    model, body = systemone.judgment_body(item, args, state, questions)
    return {'model': model, 'body': body, 'preview': body}


def run_one(client, prepared, args):
    return systemone.run_judgment(client, PATH, prepared, args)
