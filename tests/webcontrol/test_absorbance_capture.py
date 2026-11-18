import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import urlopen

from webcontrol.absorbance_capture import save_absorbance
from webcontrol.spectrometer import Spectrometer
from webcontrol.spectrometer_server import SpectrometerServer


class AbsorbanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.record={'pixel':[0,1,2], 'wavelength_nm':[650,550,450],
                     'intensity':[50,0,110], 't_wall':'2026-11-17T12:00:00+08:00','seq':42,
                     'photometry':{'active':True,'reference_id':'ref',
                                   'absorbance':[.30103,None,-.04139],'transmittance':[.5,None,1.1]}}
        self.reference={'id':'ref','record':{'intensity':[100,100,100]}}

    def save(self,name='BTB_pH7.00'):
        return save_absorbance(self.directory,self.record,self.reference,name=name)

    def test_names_data_and_no_overwrite(self):
        first=self.save('缓冲液 BTB_pH7.00 #1%')
        self.assertEqual(first['name'],'缓冲液 BTB_pH7.00 #1%')
        data=json.loads(Path(first['files']['json']).read_text(encoding='utf8'))
        self.assertEqual(data['record'],self.record)
        self.assertEqual(data['reference'],self.reference)
        rows=list(csv.DictReader(io.StringIO(Path(first['files']['csv']).read_text(encoding='utf-8-sig'))))
        self.assertEqual(rows[1]['absorbance'],'')
        self.assertEqual(rows[2]['absorbance'],'-0.04139')
        self.assertTrue(Path(first['files']['png']).read_bytes().startswith(b'\x89PNG'))
        second=self.save(first['name'])
        self.assertEqual(second['name'],first['name']+'-01')
        self.assertRegex(self.save('  ')['name'],r'^absorbance-\d{8}-\d{6}-\d{3}$')

    def test_invalid_names_and_measurements(self):
        for name in ['../escape','CON','aux.csv','x.','a/b','a\\b','a:b','x'*81]:
            with self.assertRaises(ValueError):self.save(name)
        self.record['photometry']['active']=False
        with self.assertRaises(ValueError):self.save()
        self.record['photometry']['active']=True
        self.record['photometry']['absorbance']=[None]*3
        with self.assertRaises(ValueError):self.save()
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_partial_failure_cleans_only_own_files(self):
        existing=self.save()
        original=Path.open
        def fail(path,*args,**kwargs):
            if path.suffix=='.png' and args and args[0]=='xb':
                raise OSError('disk full')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',fail):
            with self.assertRaises(OSError):self.save()
        self.assertEqual({p.name for p in self.directory.iterdir()}, {Path(p).name for p in existing['files'].values()})

    def test_encoded_downloads(self):
        self.directory=self.directory/'captures'
        capture=self.save('BTB 中文_pH7.00 #1%')
        service=Spectrometer(demo=True,data_dir=self.tmp.name)
        server=SpectrometerServer(service,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for path in capture['files'].values():
                with urlopen('http://127.0.0.1:'+str(server.server_port)+'/api/spectrometer-file/'+quote(Path(path).name)) as response:
                    self.assertEqual(response.read(),Path(path).read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__=='__main__':unittest.main()
