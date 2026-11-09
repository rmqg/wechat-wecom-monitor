"""Public JSON query must remain UTF-8 under Windows-style pipe encodings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from archive_store import ArchiveStore


class CollectorCliTests(unittest.TestCase):
    def test_chinese_query_is_utf8_even_when_parent_requests_cp1252(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            archive=root/'messages.sqlite3'
            with ArchiveStore(archive) as store:
                store.ingest([{'source':'synthetic','id':'one','kind':'text','text':'彩叶的中文消息 🍰','links':[],'attachments':[]}],observed_at=1)
            config=root/'config.json'
            config.write_text(json.dumps({'data_dir':str(root/'account'/'Data'),'archive_file':str(archive)}),encoding='utf-8')
            environment={**os.environ,'PYTHONIOENCODING':'cp1252','PYTHONUTF8':'0'}
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('collect-wecom.py')),'--config',str(config),'query','--since','0'],env=environment,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode('ascii',errors='replace'))
            records=json.loads(result.stdout.decode('utf-8'))
            self.assertEqual(records[0]['text'],'彩叶的中文消息 🍰')
            self.assertEqual(records[0]['id'],'one')


if __name__=='__main__':unittest.main()
