"""A golden test over the board's `md()`.

`md()` lives inside board.html and is exercised nowhere else in the suite: the
page's own coverage is string needles, which cannot tell "still renders task
lists" from "renders them the same way". This runs the real function — sliced
out of the page at test time, never copied, because a copy drifts and a drifted
golden test is worse than none — over a corpus that exercises every construct it
supports plus the malformed neighbours that surround them.

Needs `node`; skipped with a reason when it is absent, the same way the zellij
tests are.
"""
import json
import subprocess
from pathlib import Path

import pytest

import ratel.board as board_mod

pytestmark = pytest.mark.node

BOARD_HTML = Path(board_mod.__file__).parent / "static" / "board.html"

# `colour` and `chipBg` reach exactly one place: the inline style on a mention
# chip. They never decide what tags md() emits or how lines group, so stubbing
# them keeps the goldens stable across the palette without weakening the test.
HARNESS = """
const colour = () => "#c01";
const chipBg = () => "#c02";
%(esc)s
%(md)s
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(c => md(c.text, c.opts))));
"""


def _slice(src, start, end="\n}\n"):
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


def render(cases, tmp_path):
    """Run the page's real md() over `cases` under node."""
    src = BOARD_HTML.read_text()
    esc = src[src.index("const ESC = {"):src.index("\n", src.index("const esc = s =>")) + 1]
    script = tmp_path / "md_harness.js"
    # from md()'s module-level constants through the function itself: they are
    # contiguous, and md() cannot run without them
    script.write_text(HARNESS % {"esc": esc, "md": _slice(src, "const MD_BLOCK = ")})
    payload = json.dumps([c if isinstance(c, dict) else {"text": c} for c in cases])
    p = subprocess.run(["node", str(script)], input=payload, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# Every construct md() supports, and the malformed neighbours around each one:
# a bullet marker with nothing after it, a checkbox spelling that is not a task,
# unclosed emphasis and code, raw HTML, a lone fence.
CORPUS = [
    "plain text",
    "**bold** and *not*",
    "a `inline code` b",
    "```\nplain fence\n```",
    "```js\nconst x = 1;\n```",
    "```mermaid\ngraph TD;\nA-->B;\n```",
    # the source stays inside the block, escaped, and `--&gt;` decodes back to
    # `-->` when the renderer reads it through dataset
    "```MERMAID\ngraph LR;\nA-->B;\n```",
    "```mermaid\n%%{init: {\"flowchart\":{\"defaultRenderer\":\"elk\"}}}%%\ngraph TD;\nA-->B;\n```",
    "```mermaidjs\nnot mermaid\n```",
    "```js title=x.js\nconst y = 2;\n```",
    "```JS\nupper\n```",
    "```c++\nint main(){}\n```",
    "```a_b-c+d\nweird info\n```",
    "```const x = 1```",
    "```code```",
    "``` \nspace after fence\n```",
    "text before\n```py\nbody\n```\ntext after",
    "- one\n- two",
    "- [ ] todo\n- [x] done\n- [X] DONE",
    "- bullet\n- [ ] task\n- bullet again",
    "- a\n\n- b",
    "[link](https://example.com/x)",
    "bare https://example.com/y here",
    "@orchestrator hi @worker-a",
    "@notamention.tail and a@b",
    "- @orchestrator does `x` **now**",
    "- [ ] @worker-a: see https://example.com/z",
    "```\n@orchestrator inside a fence\n```",
    "`@orchestrator inline`",
    "line1\nline2\n\nline4",
    "<script>alert(1)</script>",
    "**bold with <b>tag</b>**",
    "- [ ] a\n```\nfence after list\n```\n- [ ] b",
    "trailing newline in fence\n```\nx\n\n```",
    "https://example.com/a?b=c&d=e#f",
    "[a](https://x.example/1) and [b](https://y.example/2)",
    "",
    "\n",
    "- ",
    "```",
    "- [y] not a task",
    "**unclosed",
    "`unclosed",
    "</ul>",
    "  - indented",
    "\t- tab",
    # inline emphasis, which IS on for message text
    "*italic* and _under_ and ~~strike~~",
    "snake_case_name stays plain",
    "a *multi\nline* is not italic",
    # the link ordering: a marker inside a URL must survive
    "https://x.example/a_b_c_d",
    "https://x.example/a**b**c",
    "[**bold** label](https://x.example/1)",
    # block constructs, with blocks OFF: every one of these stays literal text,
    # which is what protects an append-only bus of existing messages
    "# heading",
    "###### six",
    "> quoted",
    "1. first\n2. second",
    "| a | b |\n| --- | --- |\n| 1 | 2 |",
    "---",
    "***",
    "___",
]

# The same constructs with blocks ON — what the modal (step 8) renders. Its
# container must carry class="mdblocks" or none of this is styled.
BLOCK_CORPUS = [
    "# h1\n## h2\n### h3\n#### h4\n##### h5\n###### h6",
    "####### seven is not a heading",
    "#nospace",
    "# heading\ntext under it",
    "1. first\n2. second\n3) third",
    "1. one\n- bullet\n1. two",
    "&gt; not a quote in source",          # the source really contains &gt;
    "> quoted\n> second line",
    ">",
    "> quote\ntext after",
    "---",
    "***",
    "___",
    "- a\n---\n- b",
    "- a\n- b\n\n---",
    "| a | b |\n| --- | --- |\n| 1 | 2 |",
    "| l | c | r |\n| :-- | :-: | --: |\n| 1 | 2 | 3 |",
    "| head |\n| --- |",
    "| a | b |\n| --- | --- |\n| 1 | 2 |\ntext after the table",
    "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |",
    "| not | a table |\nno delimiter row",
    "| a |\n| --- |\n| `code` and **bold** |",
    "# title\n\n- [ ] task\n\n| a |\n| --- |\n| 1 |\n\n> note\n\n---",
    "```\n# not a heading\n> not a quote\n---\n```",
    "text\n\n# heading\n\ntext",
]



# The goldens: what md() renders today, one entry per corpus case.
GOLDEN = [
    'plain text',
    '<b>bold</b> and <i>not</i>',
    'a <code>inline code</code> b',
    '<pre><code>plain fence</code></pre>',
    '<pre><code class="lang-js">const x = 1;</code></pre>',
    '<div class="mermaid-block" data-src="graph TD;\nA--&gt;B;"><pre><code class="lang-mermaid">graph TD;\nA--&gt;B;</code></pre></div>',
    '<div class="mermaid-block" data-src="graph LR;\nA--&gt;B;"><pre><code class="lang-MERMAID">graph LR;\nA--&gt;B;</code></pre></div>',
    '<div class="mermaid-block" data-src="%%{init: {&quot;flowchart&quot;:{&quot;defaultRenderer&quot;:&quot;elk&quot;}}}%%\ngraph TD;\nA--&gt;B;"><pre><code class="lang-mermaid">%%{init: {&quot;flowchart&quot;:{&quot;defaultRenderer&quot;:&quot;elk&quot;}}}%%\ngraph TD;\nA--&gt;B;</code></pre></div>',
    '<pre><code class="lang-mermaidjs">not mermaid</code></pre>',
    '<pre><code class="lang-js">const y = 2;</code></pre>',
    '<pre><code class="lang-JS">upper</code></pre>',
    '<pre><code class="lang-c++">int main(){}</code></pre>',
    '<pre><code class="lang-a_b-c+d">weird info</code></pre>',
    '<pre><code class="lang-const"> x = 1</code></pre>',
    '<pre><code class="lang-code"></code></pre>',
    '<pre><code>space after fence</code></pre>',
    'text before<pre><code class="lang-py">body</code></pre>text after',
    '<ul><li>one</li><li>two</li></ul>',
    '<ul class="checks"><li><input type="checkbox" disabled><span>todo</span></li><li><input type="checkbox" disabled checked><span>done</span></li><li><input type="checkbox" disabled checked><span>DONE</span></li></ul>',
    '<ul><li>bullet</li></ul><ul class="checks"><li><input type="checkbox" disabled><span>task</span></li></ul><ul><li>bullet again</li></ul>',
    '<ul><li>a</li></ul><ul><li>b</li></ul>',
    '<a href="https://example.com/x" target="_blank" rel="noopener">link</a>',
    'bare <a href="https://example.com/y" target="_blank" rel="noopener">https://example.com/y</a> here',
    '<span class="mention" style="color:#c01;background:#c02">@orchestrator</span> hi <span class="mention" style="color:#c01;background:#c02">@worker-a</span>',
    '<span class="mention" style="color:#c01;background:#c02">@notamention</span>.tail and a@b',
    '<ul><li><span class="mention" style="color:#c01;background:#c02">@orchestrator</span> does <code>x</code> <b>now</b></li></ul>',
    '<ul class="checks"><li><input type="checkbox" disabled><span><span class="mention" style="color:#c01;background:#c02">@worker-a</span>: see <a href="https://example.com/z" target="_blank" rel="noopener">https://example.com/z</a></span></li></ul>',
    '<pre><code>@orchestrator inside a fence</code></pre>',
    '<code>@orchestrator inline</code>',
    'line1<br>line2<br><br>line4',
    '&lt;script&gt;alert(1)&lt;/script&gt;',
    '<b>bold with &lt;b&gt;tag&lt;/b&gt;</b>',
    '<ul class="checks"><li><input type="checkbox" disabled><span>a</span></li></ul><pre><code>fence after list</code></pre><ul class="checks"><li><input type="checkbox" disabled><span>b</span></li></ul>',
    'trailing newline in fence<pre><code>x\n</code></pre>',
    '<a href="https://example.com/a?b=c&amp;d=e#f" target="_blank" rel="noopener">https://example.com/a?b=c&amp;d=e#f</a>',
    '<a href="https://x.example/1" target="_blank" rel="noopener">a</a> and <a href="https://y.example/2" target="_blank" rel="noopener">b</a>',
    '',
    '<br>',
    '<ul><li></li></ul>',
    '```',
    '<ul><li>[y] not a task</li></ul>',
    '**unclosed',
    '`unclosed',
    '&lt;/ul&gt;',
    '<ul><li>indented</li></ul>',
    '<ul><li>tab</li></ul>',
    '<i>italic</i> and <i>under</i> and <s>strike</s>',
    'snake_case_name stays plain',
    'a *multi<br>line* is not italic',
    '<a href="https://x.example/a_b_c_d" target="_blank" rel="noopener">https://x.example/a_b_c_d</a>',
    '<a href="https://x.example/a**b**c" target="_blank" rel="noopener">https://x.example/a**b**c</a>',
    '<a href="https://x.example/1" target="_blank" rel="noopener"><b>bold</b> label</a>',
    '# heading',
    '###### six',
    '&gt; quoted',
    '1. first<br>2. second',
    '| a | b |<br>| --- | --- |<br>| 1 | 2 |',
    '---',
    '***',
    '___',
]

BLOCK_GOLDEN = [
    '<h1>h1</h1><h2>h2</h2><h3>h3</h3><h4>h4</h4><h5>h5</h5><h6>h6</h6>',
    '####### seven is not a heading',
    '#nospace',
    '<h1>heading</h1>text under it',
    '<ol><li>first</li><li>second</li><li>third</li></ol>',
    '<ol><li>one</li></ol><ul><li>bullet</li></ul><ol><li>two</li></ol>',
    '&amp;gt; not a quote in source',
    '<blockquote>quoted<br>second line</blockquote>',
    '<blockquote></blockquote>',
    '<blockquote>quote</blockquote>text after',
    '<hr>',
    '<hr>',
    '<hr>',
    '<ul><li>a</li></ul><hr><ul><li>b</li></ul>',
    '<ul><li>a</li><li>b</li></ul><hr>',
    '<div class="scrollx"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>',
    '<div class="scrollx"><table><thead><tr><th style="text-align:left">l</th><th style="text-align:center">c</th><th style="text-align:right">r</th></tr></thead><tbody><tr><td style="text-align:left">1</td><td style="text-align:center">2</td><td style="text-align:right">3</td></tr></tbody></table></div>',
    '<div class="scrollx"><table><thead><tr><th>head</th></tr></thead></table></div>',
    '<div class="scrollx"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>text after the table',
    '<div class="scrollx"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td><td>3</td></tr></tbody></table></div>',
    '| not | a table |<br>no delimiter row',
    '<div class="scrollx"><table><thead><tr><th>a</th></tr></thead><tbody><tr><td><code>code</code> and <b>bold</b></td></tr></tbody></table></div>',
    '<h1>title</h1><ul class="checks"><li><input type="checkbox" disabled><span>task</span></li></ul><div class="scrollx"><table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></div><blockquote>note</blockquote><hr>',
    '<pre><code># not a heading\n&gt; not a quote\n---</code></pre>',
    'text<br><h1>heading</h1><br>text',
]


def test_md_golden(tmp_path):
    """Any change to md() that moves one of these changes how every message on
    every board renders — it has to be deliberate."""
    got = render(CORPUS, tmp_path)
    assert len(got) == len(GOLDEN) == len(CORPUS)
    for src, want, have in zip(CORPUS, GOLDEN, got):
        assert have == want, src


def test_md_block_golden(tmp_path):
    """The same constructs with { blocks: true } — the modal's rendering."""
    cases = [{"text": t, "opts": {"blocks": True}} for t in BLOCK_CORPUS]
    got = render(cases, tmp_path)
    assert len(got) == len(BLOCK_GOLDEN) == len(BLOCK_CORPUS)
    for src, want, have in zip(BLOCK_CORPUS, BLOCK_GOLDEN, got):
        assert have == want, src


def test_block_constructs_are_off_by_default(tmp_path):
    """The bus is append-only: `---`, `# ` and `> ` in the years of messages
    already on it must keep rendering as the literal text they always were."""
    off = render(BLOCK_CORPUS, tmp_path)
    for src, out in zip(BLOCK_CORPUS, off):
        for tag in ("<h1", "<h2", "<h6", "<table", "<ol>", "<blockquote>", "<hr>"):
            assert tag not in out, (src, tag)
