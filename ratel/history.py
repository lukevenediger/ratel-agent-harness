"""Board history pages in append order, with literal text and mention filters."""
from .schema import validate_limit, validate_name
from .ulid import is_ulid


def matches(msg, query='', mention='', operator=False):
    return ((not query or query.lower() in msg['text'].lower())
            and (not mention or mention in msg['mentions'])
            and (not operator or msg['from'] == 'stakeholder' or 'stakeholder' in msg['mentions']))


def validate_page(before=None, limit=100, query='', mention='', operator=False):
    validate_limit(limit)
    if limit > 200:
        raise ValueError('limit must be at most 200')
    if before is not None and not is_ulid(before):
        raise ValueError('before must be a message ID')
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError('search must be at most 200 characters')
    if mention:
        validate_name(mention, 'mention')
    if not isinstance(operator, bool):
        raise ValueError('operator must be a boolean')


def page_result(rows, limit, tip):
    return {'messages': list(reversed(rows[:limit])),
            'next_before': rows[limit - 1]['id'] if len(rows) > limit else None, 'tip': tip}


def clan_heads(messages):
    heads = {'proposed': None, 'approved': None}
    for msg in messages:  # newest first
        for att in msg.get('attachments', []):
            if att.get('type') != 'clan':
                continue
            if msg['from'] == 'orchestrator' and att.get('status') == 'proposed' and heads['proposed'] is None:
                heads['proposed'] = msg['id']
            if msg['from'] == 'stakeholder' and att.get('status') == 'approved' and heads['approved'] is None:
                heads['approved'] = att.get('supersedes')
        if all(heads.values()):
            break
    return heads
