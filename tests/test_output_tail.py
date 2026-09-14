from ratel.clan.output import OutputTail


def test_large_unbroken_output_is_bounded():
    tail = OutputTail(characters=65536)
    for _ in range(2000):
        tail.append('x' * 4096)
        assert tail.size <= 65536 and len(tail.parts) <= 40
    tail.append('final failure')
    assert tail.text().endswith('final failure') and tail.truncated


def test_line_tail_keeps_the_failure_excerpt():
    tail = OutputTail(lines=3)
    for i in range(10):
        tail.append(str(i) + '\n')
    assert tail.text() == '7\n8\n9\n'
