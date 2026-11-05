"""Phase H: состояние потока чата — каждый кусок ровно один раз."""

import unittest

from lamcore.chat import (ChatTranscript, EventSubscription, StreamAssembler,
                          StreamStateError, split_stream_lines)


class StreamAssemblerTest(unittest.TestCase):
    def test_identical_chunks_stay_two_chunks(self):
        assembler = StreamAssembler()
        assembler.feed("hello")
        assembler.feed("hello")
        self.assertEqual(assembler.text(), "hellohello")
        self.assertEqual(assembler.chunks, 2)

    def test_unicode_chunks(self):
        assembler = StreamAssembler()
        for chunk in ("Привет", ", мир", "!", " Ещё 2 + 2 = 4."):
            assembler.feed(chunk)
        self.assertEqual(assembler.text(), "Привет, мир! Ещё 2 + 2 = 4.")

    def test_feed_after_close_is_an_error(self):
        assembler = StreamAssembler()
        assembler.feed("a")
        assembler.close()
        with self.assertRaises(StreamStateError):
            assembler.feed("b")


class TranscriptTest(unittest.TestCase):
    def test_streaming_renders_only_the_answer(self):
        transcript = ChatTranscript()
        transcript.add_user("hi")
        transcript.begin_stream()
        transcript.feed("hello")
        blocks = transcript.blocks()
        self.assertEqual(blocks[-1], ("assistant", "hello"))
        self.assertNotIn("МОДЕЛЬ", "".join(text for _role, text in blocks))
        self.assertNotIn("MODEL", "".join(text for _role, text in blocks))

    def test_placeholder_before_first_chunk(self):
        transcript = ChatTranscript()
        transcript.begin_stream()
        self.assertEqual(transcript.blocks()[-1], ("assistant", ChatTranscript.PLACEHOLDER))
        transcript.feed("x")
        self.assertEqual(transcript.blocks()[-1], ("assistant", "x"))

    def test_each_chunk_appears_exactly_once(self):
        transcript = ChatTranscript()
        transcript.begin_stream()
        for chunk in ("hel", "lo", "hello", "!"):
            transcript.feed(chunk)
        self.assertEqual(transcript.blocks()[-1], ("assistant", "hellohello!"))
        finished = transcript.finish()
        self.assertEqual(finished, "hellohello!")
        self.assertEqual(transcript.blocks()[-1], ("assistant", "hellohello!"))
        self.assertEqual(sum(1 for role, _ in transcript.blocks() if role == "assistant"), 1)

    def test_finish_twice_raises(self):
        transcript = ChatTranscript()
        transcript.begin_stream()
        transcript.feed("a")
        transcript.finish()
        with self.assertRaises(StreamStateError):
            transcript.finish()

    def test_history_keeps_complete_pairs_and_adds_system(self):
        transcript = ChatTranscript()
        transcript.add_user("one")
        transcript.add_assistant("two")
        transcript.add_user("three")
        payload = transcript.history_for_api(system="sys")
        self.assertEqual(payload[0], {"role": "system", "content": "sys"})
        self.assertEqual([item["role"] for item in payload[1:]], ["user", "assistant", "user"])

    def test_history_drops_leading_assistant_and_errors(self):
        transcript = ChatTranscript()
        transcript.add_assistant("stale")
        transcript.add_error("boom")
        transcript.add_user("q")
        transcript.add_assistant("a")
        payload = transcript.history_for_api()
        self.assertEqual(payload, [{"role": "user", "content": "q"},
                                   {"role": "assistant", "content": "a"}])

    def test_cancel_keeps_partial_text(self):
        transcript = ChatTranscript()
        transcript.begin_stream()
        transcript.feed("part")
        partial = transcript.cancel()
        self.assertEqual(partial, "part")
        self.assertFalse(transcript.streaming)
        self.assertEqual(transcript.messages[0].content, "part")
        self.assertEqual(transcript.messages[-1].role, "error")

    def test_feed_without_stream_is_an_error(self):
        with self.assertRaises(StreamStateError):
            ChatTranscript().feed("x")


class FakeWidget:
    def __init__(self):
        self.calls = []

    def bind(self, sequence, callback, add=None):
        self.calls.append((sequence, callback, add))


class EventSubscriptionTest(unittest.TestCase):
    def test_same_handler_is_bound_once(self):
        widget = FakeWidget()
        binder = EventSubscription()
        callback = lambda _event: None
        self.assertTrue(binder.bind(widget, "<<Modified>>", callback, key="modified"))
        self.assertFalse(binder.bind(widget, "<<Modified>>", callback, key="modified"))
        self.assertEqual(len(widget.calls), 1)
        self.assertEqual(len(binder), 1)

    def test_different_sequences_are_kept(self):
        widget = FakeWidget()
        binder = EventSubscription()
        binder.bind(widget, "<<A>>", lambda _e: None, key="a")
        binder.bind(widget, "<<B>>", lambda _e: None, key="b")
        self.assertEqual(len(widget.calls), 2)

    def test_forget_widget_clears_its_bindings(self):
        widget = FakeWidget()
        binder = EventSubscription()
        binder.bind(widget, "<<A>>", lambda _e: None, key="a")
        binder.forget_widget(widget)
        self.assertEqual(len(binder), 0)
        self.assertTrue(binder.bind(widget, "<<A>>", lambda _e: None, key="a"))


class SplitStreamLinesTest(unittest.TestCase):
    def test_incomplete_tail_is_not_parsed_twice(self):
        lines, tail = split_stream_lines(b'data: {"a":1}\n\ndata: {"b"')
        self.assertEqual(lines, ['data: {"a":1}', ""])
        self.assertEqual(tail, b'data: {"b"')

    def test_literal_newline_keeps_tail(self):
        lines, tail = split_stream_lines(b"first\nsecond")
        self.assertEqual(lines, ["first"])
        self.assertEqual(tail, b"second")


if __name__ == "__main__":
    unittest.main()
