from bot.render import ThinkStripper, chunk_text, extract_prompt, strip_thinking


def stream(text: str, size: int) -> str:
    s = ThinkStripper()
    out = "".join(s.feed(text[i : i + size]) for i in range(0, len(text), size))
    return (out + s.finish()).strip()


def test_think_removed_whole():
    assert strip_thinking("<think>plan it</think>Hello") == "Hello"


def test_think_tags_split_across_chunks():  # M1.2-T09
    text = "<think>reasoning here</think>\n\nThe answer is 42."
    for size in (1, 2, 3, 5, 7):
        assert stream(text, size) == "The answer is 42."


def test_char_by_char_equals_whole():
    text = "a <b> c <thin d <think>x</think> done"
    assert stream(text, 1) == strip_thinking(text)


def test_unclosed_think_with_no_other_output_is_not_swallowed():  # M1.2-T10
    assert strip_thinking("<think>only thoughts") == "only thoughts"


def test_unclosed_think_after_answer_drops_thoughts():
    assert strip_thinking("Answer. <think>dangling") == "Answer."


def test_show_thinking_passthrough():
    s = ThinkStripper(show=True)
    assert s.feed("<think>x</think>y") == "<think>x</think>y"


def test_chunk_short_and_empty():
    assert chunk_text("hi") == ["hi"]
    assert chunk_text("   ") == []


def test_chunk_5000_chars_plain():  # M1.4-T07
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(70))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_single_3000_char_line():  # M1.4-T08
    chunks = chunk_text("y" * 3000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == "y" * 3000


def test_chunk_keeps_code_fences_balanced():
    code = "\n".join(f"print({i})  # " + "z" * 40 for i in range(80))
    text = "Intro\n```python\n" + code + "\n```\nOutro"
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 2000
        assert c.count("```") % 2 == 0
    assert chunks[1].startswith("```python")


def test_extract_prompt():
    assert extract_prompt("<@123> hello <@!123>", 123) == "hello"
    assert extract_prompt("<@999> hi", 123) == "<@999> hi"
    assert extract_prompt("   ", 123) == ""
