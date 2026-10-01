import io
from tempfile import TemporaryDirectory
import unittest

from quadringent.object_store import FileObjectStore, S3ObjectStore


class BoundedObjectTests(unittest.TestCase):
    def test_file_limit_accepts_exact_size_and_rejects_overflow(self):
        with TemporaryDirectory() as directory:
            store = FileObjectStore(directory)
            store.put_once('metadata', b'12345')
            self.assertEqual(store.get_bounded('metadata', 5), b'12345')
            with self.assertRaises(ValueError):
                store.get_bounded('metadata', 4)

    def test_s3_body_is_read_with_budget_and_closed(self):
        class Body(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 6:
                    raise AssertionError('unbounded transport read')
                return super().read(size)

        class Client:
            def get_object(self, **kwargs):
                return {'Body': body}

        for payload in (b'12345', b'123456789'):
            with self.subTest(payload=payload):
                body = Body(payload)
                store = S3ObjectStore('bucket', client=Client())
                if len(payload) == 5:
                    self.assertEqual(store.get_bounded('metadata', 5), payload)
                else:
                    with self.assertRaises(ValueError):
                        store.get_bounded('metadata', 5)
                self.assertTrue(body.closed)
