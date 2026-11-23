"""Request building shared by the judgment tools (jev, vela): systemone bodies and images.

A body is {"model", "state", "questions"} with optional "images" (file paths or image data
URIs, converted here to base64 data URIs). Refusals raise ToolError before any request.
"""
import base64
import binascii
import json
import mimetypes
import os
import re
from io import BytesIO
from pathlib import Path

from tools import ToolError

# A name with an upstream prefix (e.g. "<prefix>/model") is refused locally; CPA chooses the upstream.
UPSTREAM_PREFIX = re.compile(r'\s*jev-[^/]*/', re.I)


def add_arguments(parser):
    parser.add_argument('--model', help='model id for lines without "model"; no default, the served models '
                        'change (take an id from --list-models)')
    parser.add_argument('--max-side', type=int, metavar='PIXELS',
                        help='resize input images so the longest side is at most PIXELS (requires Pillow)')


def check_args(args):
    if args.max_side is not None and args.max_side < 1:
        raise ToolError('--max-side must be positive')


def check_questions(questions):
    if not isinstance(questions, dict) or not questions:
        raise ToolError('needs a nonempty object "questions"')


def judgment_body(item, args, state, questions):
    """Return (model, body) for one judgment. The caller has checked state and questions."""
    model = item['model'] if 'model' in item else args.model
    if model is None:
        raise ToolError('has no "model" and --model is not set; take an id from --list-models')
    if not isinstance(model, str) or not model.strip():
        raise ToolError('has an invalid model')
    if UPSTREAM_PREFIX.match(model):
        raise ToolError('names model %r with an upstream prefix; write the model only (the id from '
                        '--list-models), CPA chooses the upstream' % model)
    body = {'model': model, 'state': state, 'questions': questions}
    if 'images' in item:
        images = item['images']
        if not isinstance(images, list):
            raise ToolError('"images" must be a list')
        body['images'] = [image_to_data_uri(image, args.max_side) for image in images]
    return model, body


def image_bytes_to_data_uri(data, mime, max_side=None):
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ToolError('images must be PNG, JPEG, or WebP')
    if max_side is not None:
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise ToolError('Pillow (PIL) is required for --max-side; install Pillow and retry')
        try:
            image = Image.open(BytesIO(data))
            image = ImageOps.exif_transpose(image)
            image.load()
        except Exception as e:
            raise ToolError('cannot decode image for --max-side: %s' % e)
        width, height = image.size
        longest = max(width, height)
        if longest > max_side:
            scale = max_side / float(longest)
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            resampling = getattr(Image, 'Resampling', Image).LANCZOS
            image = image.resize(size, resampling)
            fmt = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}[mime]
            if fmt == 'JPEG' and image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            output = BytesIO()
            image.save(output, format=fmt)
            data = output.getvalue()
    return 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))


def image_to_data_uri(value, max_side=None):
    if not isinstance(value, str) or not value:
        raise ToolError('each images entry must be a nonempty file path or data URI')
    if value.startswith('data:'):
        match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,(.*)', value, re.DOTALL)
        if not match:
            raise ToolError('image data URI must be base64 PNG, JPEG, or WebP')
        if max_side is None:
            return value
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            raise ToolError('image data URI has invalid base64')
        return image_bytes_to_data_uri(data, match.group(1), max_side)

    path = Path(os.path.expanduser(value))
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ToolError('cannot read image file %s: %s' % (path, e))
    mime = mimetypes.guess_type(str(path))[0]
    if mime == 'image/jpg':
        mime = 'image/jpeg'
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ToolError('unsupported image file type: %s' % path.suffix)
    return image_bytes_to_data_uri(data, mime, max_side)


def run_judgment(client, path, prepared, args):
    """POST one judgment and return its record; the answers object is required on success."""
    reply = client.call('POST', path, json_body=prepared['body'], attempts=args.attempts, timeout=args.timeout)
    record = {'id': prepared['id'], 'ok': False, 'answers': None, 'usage': None,
              'model': prepared['model'], 'status': reply.status, 'attempts': reply.attempts,
              'error': reply.error, '_rate_limits': reply.rate_limits}
    if reply.error is None:
        try:
            response = json.loads(reply.raw)
        except (ValueError, UnicodeDecodeError):
            record['error'] = 'HTTP %s: response is not valid JSON' % reply.status
        else:
            if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
                record['error'] = 'HTTP %s: response is missing an answers object' % reply.status
            else:
                observed = response.get('model')
                record.update(ok=True, answers=response['answers'], usage=response.get('usage'),
                              model=observed or prepared['model'], error=None)
                record['_observed'] = observed
    return record
