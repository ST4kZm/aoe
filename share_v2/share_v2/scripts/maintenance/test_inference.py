"""Mocked transport tests; never send API requests."""
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from scripts.maintenance.pipeline import collect_responses
from scripts.maintenance.test_fixtures import dataset
from scripts.maintenance.test_resource_dag import response
from scripts.maintenance.resource_dag import parse_response
from scripts.utils.llms_definitions import GPTModel


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def chunk(text=None, finish=None):
    return NS(choices=[NS(delta=NS(content=text), finish_reason=finish)])


class TransportTests(unittest.TestCase):
    def test_unique_complete_suffix_recovered_without_changing_predictions(self):
        data = dataset([1, 1])
        expected = response(data)
        prefix = '{"timeline":{"summary":"broken\n'
        raw = prefix + json.dumps(expected)
        audit = {}
        with self.assertWarns(RuntimeWarning):
            actual = parse_response(data, raw, recovery_info=audit)
        self.assertEqual(actual, expected)
        self.assertEqual(audit["start_char"], len(prefix))

    def test_multiple_complete_answers_and_incomplete_suffix_rejected(self):
        data = dataset([1, 1])
        raw = json.dumps(response(data))
        with self.assertRaisesRegex(ValueError, "unique complete"):
            parse_response(data, raw + raw)
        with self.assertRaisesRegex(ValueError, "unique complete"):
            parse_response(data, raw[:40])

    def test_recovery_revalidates_coverage_and_writes_audit_on_resume(self):
        data = dataset([1, 1])
        invalid = response(data)
        invalid["pairs"] = []
        with self.assertRaisesRegex(ValueError, "Missing"):
            parse_response(data, 'incomplete{' + json.dumps(invalid))
        client = Mock()
        client.run_model.return_value = 'incomplete{' + json.dumps(response(data))
        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            output = str(Path(tmp) / 'graph.json')
            result = collect_responses(data, 'gpt', 'test-model', 1, output, factory=lambda: client)
            self.assertEqual(result, [response(data)])
            audit = json.loads((Path(tmp) / 'graph.runs/run_1.recovery.json').read_text())
            self.assertEqual(audit['start_char'], len('incomplete{'))
            self.assertIn('raw_sha256', audit)

    def test_stream_joins_text_ignores_empty_chunks_and_closes(self):
        stream = FakeStream([NS(choices=[]), chunk(), chunk('{"ok":'), chunk('true}'), chunk(finish="stop")])
        client = Mock()
        client.chat.completions.create.return_value = stream
        observed = []
        value = GPTModel.run_gpt(client, "test-model", [], stream=True, on_chunk=observed.append)
        self.assertEqual(value, '{"ok":true}')
        self.assertTrue(client.chat.completions.create.call_args.kwargs["stream"])
        self.assertTrue(stream.closed)
        self.assertEqual(len(observed), 2)

    def test_truncated_stream_is_not_success(self):
        stream = FakeStream([chunk('{"unfinished":')])
        client = Mock()
        client.chat.completions.create.return_value = stream
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            GPTModel.run_gpt(client, "test-model", [], stream=True)
        self.assertTrue(stream.closed)

    def test_resume_skips_successful_calls_after_524(self):
        data = dataset([1, 1])
        raw = json.dumps(response(data))
        error = RuntimeError("test gateway failure")
        error.status_code = 524
        client = Mock()
        client.run_model.side_effect = [raw, error]
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "graph.json")
            with self.assertRaisesRegex(RuntimeError, "524"):
                collect_responses(data, "gpt", "test-model", 2, output, factory=lambda: client)
            self.assertEqual(client.run_model.call_count, 2)
            self.assertTrue((Path(tmp) / "graph.runs/run_1.json").exists())
            self.assertFalse((Path(tmp) / "graph.runs/run_2.json").exists())
            resumed = Mock()
            resumed.run_model.return_value = raw
            result = collect_responses(data, "gpt", "test-model", 2, output, resume=True, factory=lambda: resumed)
            self.assertEqual(len(result), 2)
            self.assertEqual(resumed.run_model.call_count, 1)
            with self.assertRaisesRegex(ValueError, "changed"):
                collect_responses(data, "gpt", "different-model", 2, output, resume=True)

    def test_partial_stream_saved_and_not_used_as_complete(self):
        data = dataset([1, 1])
        client = Mock()
        def fail(prompt, on_chunk):
            on_chunk('{"unfinished":')
            raise RuntimeError("connection lost")
        client.run_model.side_effect = fail
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "graph.json")
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                collect_responses(data, "gpt", "test-model", 1, output, factory=lambda: client)
            folder = Path(tmp) / "graph.runs"
            self.assertEqual((folder / "run_1.attempt_1.partial.txt").read_text(), '{"unfinished":')
            self.assertFalse((folder / "run_1.txt").exists())
            self.assertFalse((folder / "run_1.json").exists())


if __name__ == "__main__":
    unittest.main()
