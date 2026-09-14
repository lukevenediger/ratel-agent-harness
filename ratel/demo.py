"""Synthetic board data without credentials, provider calls or clan processes."""
from pathlib import Path

from .bus import Bus
from .schema import validate_name


def seed(home, channel='harbor-demo'):
    validate_name(channel)
    root = Path(home).expanduser().absolute()
    # A new home is deliberate: no overwrite/reset path and no ambient default.
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    bus = Bus(root, channel)
    plan = bus.channel_dir / 'plans' / 'demo.md'
    plan.write_text('''# Harbor demo

This is synthetic data. No agents are running and no provider credentials are needed.

- Review the pinned plan.
- Open a reply thread.
- Search for `health` or filter mentions of `stakeholder`.

```mermaid
flowchart LR
    Plan --> Build --> Review
```
''')
    (bus.files_dir / 'demo-plan.md').write_text(plan.read_text())
    note = bus.files_dir / 'review.md'
    note.write_text('# Review notes\n\nThe health endpoint returns **200 OK**.\n')
    top = bus.post('orchestrator', 'Welcome to the demo. @developer add a health endpoint.', pin=True,
                   attachments=[{'type': 'tasks', 'ref': 'plans/demo.md', 'items': [
                       {'text': 'Implement health endpoint', 'who': 'developer', 'done': True},
                       {'text': 'Review response contract', 'who': 'reviewer', 'done': False}]}])
    bus.post('developer', 'The health endpoint is ready for review.', parent=top['id'],
             attachments=[{'type': 'code', 'lang': 'python', 'body': 'def health():\n    return {"status": "ok"}\n'}])
    bus.post('reviewer', '@developer Please add an explicit response-status test.', parent=top['id'])
    bus.post('developer', 'Added the test; the local checks pass.', parent=top['id'])
    bus.post('reviewer', '@stakeholder Review notes are ready.', attachments=[
        {'type': 'file', 'ref': 'files/review.md', 'name': 'Review notes', 'mime': 'text/markdown'}])
    bus.post('orchestrator', 'The demo plan includes a diagram.', attachments=[
        {'type': 'file', 'ref': 'files/demo-plan.md', 'name': 'Demo plan', 'mime': 'text/markdown'}])
    return {'home': str(root), 'channel': channel, 'messages': bus.summary()['count'], 'thread': top['id']}
