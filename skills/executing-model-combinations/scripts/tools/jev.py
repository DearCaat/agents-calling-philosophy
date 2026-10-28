"""Tool jev: judgment (systemone) through the jev-* gateways, for models that are not vela models.

Input line: {"id", "state": "<text>", "questions": {...}, "model"?: "<id>", "images"?: [<path or data URI>]}.
Object states and span questions are refused here; the vela tool serves them.
"""
from tools import ToolError
from tools import systemone

NAME = 'jev'
DESCRIPTION = 'judgment through the jev-* gateways (models that are not vela models)'
PATH = '/jev/systemone'
DEFAULT_CONCURRENCY = 1024
DEFAULT_TIMEOUT = 160.0


def add_arguments(parser):
    systemone.add_arguments(parser)


def check_args(args):
    systemone.check_args(args)


def prepare(item, args):
    state = item.get('state')
    if isinstance(state, dict):
        raise ToolError('object states (context/answer) are supported only by the vela tool; '
                        'send these items with: tool-call.py vela')
    if not isinstance(state, str) or not state.strip():
        raise ToolError('needs a nonempty string "state"')
    questions = item.get('questions')
    systemone.check_questions(questions)
    if any(isinstance(q, dict) and q.get('type') == 'span' for q in questions.values()):
        raise ToolError('span questions are supported only by the vela tool; '
                        'send these items with: tool-call.py vela')
    model, body = systemone.judgment_body(item, args, state, questions)
    return {'model': model, 'body': body, 'preview': body}


def run_one(client, prepared, args):
    return systemone.run_judgment(client, PATH, prepared, args)
