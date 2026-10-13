import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import disclosure_back_up_scheduler as backup


class DisclosureBackupTest(unittest.TestCase):
    def test_all_feeds_archive_old_data_and_preserve_recent_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            today = datetime.now(backup.KST).replace(hour=12, minute=0, second=0, microsecond=0)
            items = [dict(title=str(age), timestamp=(today - timedelta(days=age)).timestamp()) for age in (0, 1, 3)]
            items.append({'title': 'unknown date'})
            files, caches = {}, {}
            for key in ('stream', 'filtered', 'blacklisted'):
                files[key] = str(root / f'{key}.json')
                Path(files[key]).write_text(json.dumps(items))
                caches[key] = list(items)
            refs = dict(caches)
            with patch.object(backup, 'BACKUP_DIR', root / 'archives'):
                for _ in range(2):
                    backup.execute_midnight_processing(threading.Lock(), files, caches)
                for key in files:
                    self.assertIs(refs[key], caches[key])
                    self.assertEqual(caches[key], [items[0], items[1], items[3]])
                    self.assertEqual(json.loads(Path(files[key]).read_text()), caches[key])
                    for age in (1, 3):
                        date = (today - timedelta(days=age)).strftime('%y%m%d')
                        archived = root / 'archives' / f'disclosure_news_list_{key}_backup_{date}.json'
                        self.assertEqual(json.loads(archived.read_text()), [items[1 if age == 1 else 2]])

    def test_backup_write_failure_preserves_source_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'stream.json'
            items = [{'timestamp': (datetime.now(backup.KST) - timedelta(days=3)).timestamp()}]
            source.write_text(json.dumps(items))
            cache = list(items)
            with patch.object(backup, 'BACKUP_DIR', Path(directory) / 'archives'), patch.object(backup, '_write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    backup.execute_midnight_processing(threading.Lock(), {'stream': str(source)}, {'stream': cache})
            self.assertEqual(json.loads(source.read_text()), items)
            self.assertEqual(cache, items)


if __name__ == '__main__':
    unittest.main()
