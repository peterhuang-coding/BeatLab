import pathlib,tempfile,json,threading,http.client,unittest
from examples import album_audition as module

class IndependentHTTP(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name)
        (self.root/'audio').mkdir();(self.root/'audio/test.wav').write_bytes(b'0123456789')
        self.track=dict(id='test',number=1,title='测试',direction='fixture',version='v1',mix_sha256='a'*64,bpm=90,duration_seconds=50,change_point_seconds=25,
                        audio='audio/test.wav',original_audio='audio/test.wav',editable_path='/tmp/fixture',source_summary=[])
        (self.root/'collection.json').write_text(json.dumps(dict(id='test',title='test',subtitle='test',tracks=[self.track])))
        self.server=module.create_server(self.root,port=0);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def req(self,path,method='GET',body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        try:
            c.request(method,path,body=body,headers=headers or {});r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
        finally:c.close()
    def test_corrupt_feedback_returns_json_and_preserves_file(self):
        p=self.root/'feedback.json';p.write_text('{broken')
        status,headers,body=self.req('/api/feedback')
        self.assertEqual(status,500);self.assertIn('error',json.loads(body));self.assertEqual(p.read_text(),'{broken')
    def test_exact_range_and_head(self):
        status,h,b=self.req('/media/audio/test.wav',headers={'Range':'bytes=2-4'})
        self.assertEqual((status,b),(206,b'234'));self.assertEqual(h['Content-Range'],'bytes 2-4/10')
        status,h,b=self.req('/media/audio/test.wav',method='HEAD');self.assertEqual((status,b),(200,b''));self.assertEqual(h['Content-Length'],'10')
    def test_same_feedback_idempotent_and_stale_refused(self):
        payload=dict(track_id='test',version='v1',mix_sha256='a'*64,ratings={'groove':4,'sample':None,'variation':None,'clarity':None,'rap_space':None},keep='revise',notes='fixture only',timestamp_notes=[{'seconds':2.5,'text':'fixture'}])
        self.assertEqual(self.req('/api/feedback','POST',json.dumps(payload))[0],200)
        before=(self.root/'feedback.json').read_bytes()
        self.assertEqual(self.req('/api/feedback','POST',json.dumps(payload))[0],200)
        self.assertEqual((self.root/'feedback.json').read_bytes(),before)
        payload['mix_sha256']='b'*64
        self.assertEqual(self.req('/api/feedback','POST',json.dumps(payload))[0],409)
        self.assertEqual((self.root/'feedback.json').read_bytes(),before)
    def test_declared_symlink_escape_rejected(self):
        p=self.root/'audio/test.wav';p.unlink();p.symlink_to(self.root.parent/'not-exposed')
        self.assertIn(self.req('/media/audio/test.wav')[0],(403,404))

if __name__=='__main__':unittest.main()
