"""Offline tests. Does not access Gofile or download any remote data."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import io

import gofile_bulk as g


class FakeClient:
    token = 'test-token'
    def __init__(self):
        self.data = {
            'root': ({'type': 'folder', 'name': '../root'}, {
                'child': {'id': 'child', 'type': 'folder', 'name': 'nested'},
                'f1': {'id': 'f1', 'type': 'file', 'name': 'a?.txt',
                       'size': 3, 'link': 'https://store1.gofile.io/download/one'},
                'f2': {'id': 'f2', 'type': 'file', 'name': 'a*.txt',
                       'size': 4, 'link': 'https://store1.gofile.io/download/two'},
            }),
            'child': ({'type': 'folder', 'name': 'nested'}, {
                'f3': {'id': 'f3', 'type': 'file', 'name': '../../outside.txt',
                       'size': 1, 'link': 'https://store1.gofile.io/download/three'},
            }),
        }

    def list_children(self, cid):
        return self.data[cid]


class FakeResponse:
    def __init__(self, status, content, headers=None):
        self.status_code = status
        self.data = content
        self.headers = headers or {}
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def iter_content(self, chunk_size):
        yield self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class DownloaderTests(unittest.TestCase):
    def test_safe_filename_and_host(self):
        self.assertEqual(g.safe_name('../../evil', 'id'), '_.._evil')
        self.assertEqual(g.safe_name('CON.txt', 'id'), '_CON.txt')
        self.assertEqual(g.share_id('https://gofile.io/d/HcFO9mbF'), 'HcFO9mbF')
        for url in ['http://gofile.io/d/ok', 'https://evil.example/d/ok',
                    'https://gofile.io.evil.example/download/test']:
            with self.assertRaises(g.DownloadError):
                g.validate_gofile_url(url)

    def test_manifest_nested_and_collisions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = [x.path for x in g.build_manifest(FakeClient(), 'root', root)]
            self.assertEqual(len(paths), 3)
            self.assertEqual(len({str(p).lower() for p in paths}), 3)
            self.assertTrue(all(p.resolve().is_relative_to(root.resolve()) for p in paths))
            self.assertTrue(any(p.name == '_.._outside.txt' for p in paths))
            self.assertEqual(sorted(p.name for p in paths if p.parent.name == '_root'),
                             ['a___f1.txt', 'a___f2.txt'])

    def test_preexisting_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'root').symlink_to('/tmp', target_is_directory=True)
            with self.assertRaises(g.DownloadError):
                g.build_manifest(FakeClient(), 'root', root)

    def test_pagination(self):
        obj = object.__new__(g.Gofile)
        page1 = {'data': {'type': 'folder', 'children': {'a': {'type': 'file'}}},
                 'metadata': {'totalPages': 2}}
        page2 = {'data': {'type': 'folder', 'children': {'b': {'type': 'file'}}},
                 'metadata': {'totalPages': 2}}
        obj._get_page = lambda cid, page: page1 if page == 1 else page2
        _, results = obj.list_children('test')
        self.assertEqual(set(results), {'a', 'b'})
        page2['data']['children'] = {'a': {'type': 'file'}}
        with self.assertRaises(g.DownloadError):
            obj.list_children('test')

    def test_resume_206_and_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / 'abc.bin'
            part = root / 'abc.bin.part'
            part.write_bytes(b'hello')
            item = g.DownloadItem('https://store1.gofile.io/download/test', target, 11,
                                  hashlib.md5(b'hello world').hexdigest())
            sess = FakeSession([FakeResponse(206, b' world', {
                'Content-Range': 'bytes 5-10/11'
            })])
            with patch.object(g.requests, 'Session', return_value=sess):
                self.assertIn('OK', g.download(item, 'token', root))
            self.assertEqual(target.read_bytes(), b'hello world')
            self.assertFalse(part.exists())
            self.assertEqual(sess.requests[0][1]['headers']['Range'], 'bytes=5-')

    def test_resume_server_ignores_range(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / 'abc.bin'
            (root / 'abc.bin.part').write_bytes(b'bad')
            item = g.DownloadItem('https://store1.gofile.io/download/test', target, 5, None)
            sess = FakeSession([FakeResponse(200, b'fresh')])
            with patch.object(g.requests, 'Session', return_value=sess):
                g.download(item, 'token', root)
            self.assertEqual(target.read_bytes(), b'fresh')

    def test_never_overwrite_existing_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / 'test.txt'
            path.write_bytes(b'existing')
            item = g.DownloadItem('https://store1.gofile.io/download/test', path, 500, None)
            with self.assertRaises(g.DownloadError):
                g.download(item, 'token', root)
            self.assertEqual(path.read_bytes(), b'existing')


    def test_id_or_full_url_input(self):
        expected = ('https://gofile.io/d/kaHyq5zp', 'kaHyq5zp')
        for value in (
            'kaHyq5zp',
            ' https://gofile.io/d/kaHyq5zp ',
            'gofile.io/d/kaHyq5zp',
            'https://www.gofile.io/d/kaHyq5zp/',
        ):
            self.assertEqual(g.normalize_share(value), expected)
        for value in ('', 'https://example.com/d/kaHyq5zp',
                      'https://gofile.io/d/../../oops', 'http://gofile.io/d/kaHyq5zp',
                      'kaHyq5zp/anything'):
            with self.assertRaises(g.DownloadError):
                g.normalize_share(value)

    def test_interactive_inputs_and_media_menu(self):
        with patch('builtins.input', return_value='kaHyq5zp, https://gofile.io/d/other'):
            self.assertEqual(g.prompt_shares(), ['kaHyq5zp', 'https://gofile.io/d/other'])
        with patch('builtins.input', side_effect=['wrong', '2']), redirect_stdout(io.StringIO()):
            self.assertEqual(g.prompt_media(), 'photos')
        with patch('builtins.input', return_value=''):
            self.assertEqual(g.prompt_media(), 'all')
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(g.DownloadError):
                g.prompt_shares()

    def test_media_classification_mime_and_extension(self):
        self.assertTrue(g.matches_media({'mimeType': 'video/mp4'}, 'no-extension', 'videos'))
        self.assertTrue(g.matches_media({}, 'MOVIE.MKV', 'videos'))
        self.assertFalse(g.matches_media({'mimeType': 'image/jpeg'}, 'wrong.mp4', 'videos'))
        self.assertTrue(g.matches_media({'mimeType': 'image/jpeg'}, 'wrong.mp4', 'photos'))
        self.assertTrue(g.matches_media({}, 'image.HEIC', 'photos'))
        self.assertFalse(g.matches_media({}, 'document.pdf', 'photos'))
        self.assertFalse(g.matches_media({'mimetype': 'image/svg+xml'}, 'image.svg', 'photos'))
        self.assertTrue(g.matches_media({}, 'document.pdf', 'all'))

    def test_recursive_media_filter_skips_nonmatching_without_link(self):
        fake = FakeClient()
        fake.data['root'][1]['f1'].update({'name': 'video.mp4'})
        fake.data['root'][1]['f2'].update({'name': 'image.jpg'})
        fake.data['child'][1]['f3'].update({'name': 'nested_video.MKV'})
        # No download URL on this unmatched document: it must not block video-only mode.
        fake.data['child'][1]['f4'] = {'id': 'f4', 'type': 'file', 'name': 'readme.txt'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            videos = g.build_manifest(fake, 'root', root, 'videos')
            photos = g.build_manifest(fake, 'root', root, 'photos')
            all_names = {p.path.name for p in videos}
            self.assertEqual(all_names, {'video.mp4', 'nested_video.MKV'})
            self.assertEqual([p.path.name for p in photos], ['image.jpg'])
            with self.assertRaises(g.DownloadError):
                g.build_manifest(fake, 'root', root, 'all')

    def test_main_interactive_id_and_selection(self):
        fake = FakeClient()
        fake.data['root'][1]['f1']['name'] = 'video.mp4'
        fake.data['root'][1]['f2']['name'] = 'photo.png'
        fake.data['child'][1]['f3']['name'] = 'notes.txt'
        with tempfile.TemporaryDirectory() as td:
            with (patch('builtins.input', side_effect=['root', '1']),
                  patch.object(g, 'Gofile', return_value=fake),
                  redirect_stdout(io.StringIO()) as output):
                status = g.main(['--dry-run', '-o', td])
            self.assertEqual(status, 0)
            self.assertIn('Found 1 matching files', output.getvalue())
            self.assertIn('video.mp4', output.getvalue())
            self.assertNotIn('photo.png', output.getvalue())

    def test_cli_share_and_media_noninteractive(self):
        fake = FakeClient()
        fake.data['root'][1]['f1']['name'] = 'video.mp4'
        fake.data['root'][1]['f2']['name'] = 'photo.jpg'
        with tempfile.TemporaryDirectory() as td:
            with (patch.object(g, 'Gofile', return_value=fake),
                  redirect_stdout(io.StringIO()) as output):
                result = g.main(['root', '--media', 'photos', '--dry-run', '-o', td])
            self.assertEqual(result, 0)
            self.assertIn('Found 1 matching files', output.getvalue())
            self.assertIn('photo.jpg', output.getvalue())
            self.assertNotIn('video.mp4', output.getvalue())


    def test_parse_shares_multiline_commas_and_duplicates(self):
        self.assertEqual(g.parse_shares('''kaHyq5zp
https://gofile.io/d/kaHyq5zp, https://gofile.io/d/Second; Third'''),
                         [('https://gofile.io/d/kaHyq5zp', 'kaHyq5zp'),
                          ('https://gofile.io/d/Second', 'Second'),
                          ('https://gofile.io/d/Third', 'Third')])
        for value in ('   ', 'https://evil.example/d/a', 'https://gofile.io/d/x  https://bad.test/d/y'):
            with self.assertRaises(g.DownloadError):
                g.parse_shares(value)

    def test_preview_manifest_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'not-yet-created'
            items = g.build_manifest(FakeClient(), 'root', root, create_directories=False)
            self.assertEqual(len(items), 3)
            self.assertFalse(root.exists(), 'Preview must not write to disk')

    def test_cancel_keeps_partial_file_without_finalizing(self):
        from threading import Event
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = g.DownloadItem('https://store1.gofile.io/download/test',
                                  root / 'movie.mp4', 11, None)
            event = Event()
            response = FakeResponse(200, b'', headers={})

            def chunks(chunk_size):
                yield b'hello'
                event.set()
                yield b' world'

            response.iter_content = chunks
            sess = FakeSession([response])
            with patch.object(g.requests, 'Session', return_value=sess):
                with self.assertRaises(g.DownloadCancelled):
                    g.download(item, 'token', root, cancel_event=event)
            self.assertFalse(item.path.exists())
            self.assertEqual((root / 'movie.mp4.part').read_bytes(), b'hello')

    def test_cancel_before_download_makes_no_requests(self):
        from threading import Event
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = g.DownloadItem('https://store1.gofile.io/download/test',
                                  root / 'video.mp4', 5, None)
            event = Event()
            event.set()
            with patch.object(g.requests, 'Session') as session:
                with self.assertRaises(g.DownloadCancelled):
                    g.download(item, 'token', root, cancel_event=event)
            session.assert_not_called()

    def test_entrypoint_uses_gui_without_args_and_cli_with_args(self):
        with (patch.object(g.sys, 'argv', ['gofile_bulk.py']),
              patch.object(g, 'run_gui', return_value=0) as gui,
              patch.object(g, 'main', return_value=0) as cli):
            self.assertEqual(g.entrypoint(), 0)
            gui.assert_called_once_with()
            cli.assert_not_called()
        with (patch.object(g.sys, 'argv', ['gofile_bulk.py', 'test-id', '--media', 'videos']),
              patch.object(g, 'run_gui') as gui,
              patch.object(g, 'main', return_value=0) as cli):
            self.assertEqual(g.entrypoint(), 0)
            cli.assert_called_once_with()
            gui.assert_not_called()

if __name__ == '__main__':
    unittest.main()
