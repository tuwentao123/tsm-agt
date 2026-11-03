"""Exercise actual body reads, not merely equal cached return values."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from tests.test_read_result_reuse import BODY, exchange, reader, results
from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.builtin.file_read_cache import FileReadCache, file_version
from tsm_agt.core.read_result_reuse import project_read_results


@dataclass
class BodyReads:
    counts: Counter = field(default_factory=Counter)
    before_read: object = None
    lock: object = field(default_factory=threading.Lock)


@pytest.fixture
def body_reads(monkeypatch):
    reads = BodyReads()
    original_open = Path.open

    class ObservedStream:
        def __init__(self, stream, path):
            self.stream, self.path = stream, path

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, *args):
            with reads.lock:
                reads.counts[self.path] += 1
            if reads.before_read:
                reads.before_read(self.path)
            return self.stream.read(*args)

    def observed_open(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return ObservedStream(stream, path.resolve()) if mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', observed_open)
    return reads


@pytest.fixture
def fast_reader(reader, tmp_path):
    if file_version((tmp_path / 'source.py').stat()) is None:
        pytest.skip('filesystem does not expose a usable change-time version')
    return reader


def test_repeated_reads_skip_body_io_but_keep_complete_canonical_results(fast_reader, body_reads, tmp_path):
    pairs = [fast_reader(name) for name in ('first', 'second', 'third')]
    assert body_reads.counts[tmp_path / 'source.py'] == 1
    assert [result.data['content'] for _, result in pairs] == [BODY] * 3
    assert [result.call_id for _, result in pairs] == ['first', 'second', 'third']
    assert [result.meta['read_cache_hit'] for _, result in pairs] == [False, True, True]
    canonical = tuple(message for pair in pairs for message in exchange(*pair))
    projection = project_read_results(canonical, 'task')
    assert projection.stats.references == (('second', 'first'), ('third', 'first'))
    # After history drops the old anchor, a surviving full result is usable.
    recovered = project_read_results(canonical[2:], 'task')
    assert results(recovered.messages)[0].data['content'] == BODY
    assert results(recovered.messages)[1].data['reused_from_call_id'] == 'second'


def test_full_file_reuses_larger_limits_and_canonical_path_aliases(fast_reader, body_reads, tmp_path):
    fast_reader('first', max_lines=200)
    second = fast_reader('second', path='./source.py', max_lines=1000)[1]
    assert second.data['content'] == BODY
    assert body_reads.counts[tmp_path / 'source.py'] == 1


def test_new_ranges_and_tasks_have_their_own_reads(fast_reader, body_reads, tmp_path):
    fast_reader('first', max_lines=10)
    fast_reader('other-range', start_line=11, max_lines=10)
    fast_reader('other-task', max_lines=10, task='another-task')
    assert body_reads.counts[tmp_path / 'source.py'] == 3
    fast_reader('same-range', max_lines=10)
    assert body_reads.counts[tmp_path / 'source.py'] == 3


@pytest.mark.parametrize('change', ['preserved_mtime', 'atomic_replace'])
def test_external_changes_invalidate_body_cache(fast_reader, body_reads, tmp_path, change):
    path = tmp_path / 'source.py'
    first = fast_reader('first')[1]
    stamp = path.stat()
    updated = BODY.replace('value_000', 'value_NEW')
    if change == 'atomic_replace':
        other = tmp_path / 'replacement.py'
        other.write_text(updated)
        os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        other.replace(path)
    else:
        path.write_text(updated)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = fast_reader('second')[1]
    assert second.data['content'] == updated
    assert first.data['sha256'] != second.data['sha256']
    assert body_reads.counts[path] == 2


def test_a_hit_still_checks_current_open_permission(fast_reader, monkeypatch, tmp_path):
    fast_reader('first')
    original = Path.open

    def denied(path, mode='r', *args, **kwargs):
        if path == tmp_path / 'source.py' and mode == 'rb':
            raise PermissionError('read access revoked')
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', denied)
    result = fast_reader('denied')[1]
    assert not result.ok
    assert 'read access revoked' in result.message


def test_unknown_file_versions_fall_back_to_real_reads(reader, body_reads, monkeypatch, tmp_path):
    monkeypatch.setattr('tsm_agt.adapters.builtin.core_tools.file_version', lambda _: None)
    reader('first')
    reader('second')
    assert body_reads.counts[tmp_path / 'source.py'] == 2


def test_racing_write_does_not_bind_old_bytes_to_a_new_version(fast_reader, body_reads, tmp_path):
    original = CoreReadOnlyToolProvider._read_file_page
    path = tmp_path / 'source.py'
    updated = BODY.replace('value_000', 'value_NEW')

    def after_read(*args):
        page = original(*args)
        path.write_text(updated)
        return page

    with patch.object(CoreReadOnlyToolProvider, '_read_file_page', side_effect=after_read):
        first = fast_reader('first')[1]
    second = fast_reader('second')[1]
    assert first.data['content'] == BODY
    assert second.data['content'] == updated
    assert body_reads.counts[path] == 2


def test_parallel_duplicate_reads_share_body_io(fast_reader, body_reads, tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(lambda i: fast_reader(f'parallel-{i}'), range(8)))
    assert all(result.ok and result.data['content'] == BODY for _, result in pairs)
    assert body_reads.counts[tmp_path / 'source.py'] == 1


def test_different_files_can_read_in_parallel(fast_reader, body_reads, tmp_path):
    (tmp_path / 'other.py').write_text(BODY)
    barrier = threading.Barrier(2)
    body_reads.before_read = lambda _: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fast_reader, path, path=path) for path in ['source.py', 'other.py']]
        assert all(future.result(timeout=10)[1].ok for future in futures)


def test_cache_eviction_byte_budget_and_clear_restore_real_reads():
    cache = FileReadCache(max_pages=2, max_content_bytes=6)
    version = (1,)
    key = lambda name: (('task', '/root', name), 1, 1, 200)
    page = lambda text: {'content': text, 'next_cursor': None, 'total_lines': 1}
    cache.put(key('a'), version, page('aaa'))
    cache.put(key('b'), version, page('bbb'))
    assert cache.get(key('a'), version)
    cache.put(key('c'), version, page('ccc'))
    assert cache.get(key('b'), version) is None  # Least-recently used.
    cache.put(key('large'), version, page('too large'))
    assert cache.get(key('large'), version) is None
    cache.clear()
    assert cache.get(key('a'), version) is None


def test_cached_page_cursors_cannot_be_mutated_by_a_caller():
    cache = FileReadCache()
    key = (('task', '/root', 'file'), 1, 1, 200)
    cache.put(key, (1,), {'content': 'page', 'next_cursor': {'start_line': 2}, 'total_lines': 3})
    first = cache.get(key, (1,))
    first['next_cursor']['start_line'] = 100
    assert cache.get(key, (1,))['next_cursor'] == {'start_line': 2}
