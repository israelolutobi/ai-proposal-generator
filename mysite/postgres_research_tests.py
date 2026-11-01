"""Concurrent snapshot imports on the explicitly isolated PostgreSQL cluster."""
import os
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase
from .test_research_intelligence import ResearchImportTests
from proposal_ai.models import ResearchDataset


@unittest.skipUnless(os.environ.get('PROPOSALQ_POSTGRES_TESTS') == '1', 'Disposable PostgreSQL only.')
class ResearchConcurrencyTests(TransactionTestCase):
    def test_concurrent_imports_have_one_atomic_active_snapshot(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertTrue(connection.settings_dict['NAME'].startswith('test_proposalq_task4b'))
        with connection.cursor() as cursor:
            cursor.execute('SHOW data_directory')
            self.assertEqual(Path(cursor.fetchone()[0]).resolve(), Path(os.environ['PROPOSALQ_TEST_PG_DATA_DIR']).resolve())
        fixture = ResearchImportTests('test_import_indexes_and_preserves_private_distinctions')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        with TemporaryDirectory() as directory:
            paths = [Path(directory)/f'snapshot{n}.xlsx' for n in range(2)]
            for n, path in enumerate(paths): path.write_bytes(f'synthetic-{n}'.encode())
            ready = threading.Barrier(2)
            errors = []
            def run(path):
                try:
                    connections.close_all()
                    ready.wait(10)
                    call_command('import_research_workbook', str(path), stdout=StringIO())
                except BaseException as error:
                    errors.append(type(error).__name__)
                finally:
                    connections.close_all()
            threads = [threading.Thread(target=run,args=(path,)) for path in paths]
            for thread in threads: thread.start()
            for thread in threads: thread.join(20)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(ResearchDataset.objects.count(), 2)
            self.assertEqual(ResearchDataset.objects.filter(active=True).count(), 1)
            for dataset in ResearchDataset.objects.all():
                self.assertEqual(dataset.cases.count(), 1)
                self.assertTrue(dataset.cases.get().terms.exists())
                self.assertEqual(dataset.notes.count(), 1)
