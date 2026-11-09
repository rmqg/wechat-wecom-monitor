"""Behavioral wire decoding and safe media association tests; synthetic data only."""
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

from media import MediaIndex
from messages import conversation_meta, decode_payload, normalize_url


def vint(value):
    encoded=bytearray()
    while value>127:
        encoded.append((value&127)|128)
        value>>=7
    encoded.append(value)
    return bytes(encoded)


def data(number,value):
    if isinstance(value,str):value=value.encode()
    return vint(number*8+2)+vint(len(value))+value


def number(field,value):
    return vint(field*8)+vint(value)


def text_payload(value):
    return data(1,number(1,0)+data(2,data(1,value)))


def image_bytes(size,format):
    image=Image.new('RGB',size,(23,71,149))
    stream=io.BytesIO()
    image.save(stream,format=format)
    return stream.getvalue()


class MessageMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.account=self.root/'account'
        self.cache=self.account/'Cache'/'Image'
        self.cache.mkdir(parents=True)
        self.snapshots=self.root/'snapshots'
        self.snapshots.mkdir()
        self.output=self.root/'archive'/'media'
        self.cache_db=self.snapshots/'cache-fixture.db'
        db=sqlite3.connect(self.cache_db)
        db.execute('CREATE TABLE mapping(type INTEGER,key TEXT,file_name TEXT,file_md5 TEXT)')
        db.commit();db.close()

    def tearDown(self):self.temporary.cleanup()

    def mapping(self,key,path,digest):
        db=sqlite3.connect(self.cache_db)
        db.execute('INSERT INTO mapping VALUES (1,?,?,?)',(key,str(path),digest))
        db.commit();db.close()

    def payload(self,original,name='photo.png'):
        digest=hashlib.md5(original).hexdigest()
        return data(1,'synthetic-file-id')+data(2,name)+number(4,len(original))+number(5,64)+number(6,48)+data(10,digest)

    def test_text_rich_text_and_link_card_preserve_visible_content(self):
        normal='彩叶，链接在这里 https://example.test/a?x=1&y=2'
        value=decode_payload(2,text_payload(normal),b'',None)
        self.assertEqual(value['text'],normal)
        self.assertEqual(value['links'],[{'url':'https://example.test/a?x=1&y=2'}])
        self.assertEqual(decode_payload(0,text_payload('多行\n富文本'),b'',None)['text'],'多行\n富文本')
        card=data(1,'标题')+data(2,'说明')+data(3,'https://example.test/?one=1&two=2')
        self.assertEqual(decode_payload(38,card,b'',None)['links'],[{'url':'https://example.test/?one=1&two=2','title':'标题','description':'说明'}])

    def test_repeated_text_and_all_member_mention_segments_keep_order(self):
        mention=data(1,number(1,5)+data(2,number(1,0)+data(2,'所有人')))
        payload=mention+text_payload(' 请看通知\n第二行')
        self.assertEqual(decode_payload(2,payload,b'',None)['text'],'@所有人 请看通知\n第二行')

    def test_inline_emoticon_label_preserves_segment_order(self):
        label=data(1,number(1,3)+data(2,data(1,'[微笑]')))
        payload=text_payload('前文')+label+text_payload('后文')
        self.assertEqual(decode_payload(2,payload,b'',None)['text'],'前文[微笑]后文')

    def test_cached_reencoding_is_not_reported_as_original_or_wrong_format(self):
        original=image_bytes((64,48),'PNG')
        cached=image_bytes((16,12),'JPEG')
        path=self.cache/'converted.jpg';path.write_bytes(cached)
        self.mapping('synthetic-file-id','Image/converted.jpg',hashlib.md5(original).hexdigest())
        result=MediaIndex(self.snapshots,self.account,self.output).resolve('image',self.payload(original))
        self.assertEqual((result['status'],result['original'],result['variant']),('ready',False,'client_cache'))
        self.assertEqual(result['dimensions'],[16,12])
        self.assertEqual(result['original_md5'],hashlib.md5(original).hexdigest())
        self.assertEqual(result['md5'],hashlib.md5(cached).hexdigest())
        self.assertEqual(Path(result['path']).suffix,'.jpg')
        self.assertEqual(result['verification'],'cache_reference_and_decoded_dimensions')

    def test_available_original_wins_over_earlier_cached_variant(self):
        original=image_bytes((64,48),'PNG')
        (self.cache/'converted.jpg').write_bytes(image_bytes((16,12),'JPEG'))
        (self.cache/'original.png').write_bytes(original)
        digest=hashlib.md5(original).hexdigest()
        self.mapping('synthetic-file-id','Image/converted.jpg',digest)
        self.mapping('synthetic-file-id','Image/original.png',digest)
        result=MediaIndex(self.snapshots,self.account,self.output).resolve('image',self.payload(original))
        self.assertEqual((result['status'],result['original'],result['md5']),('ready',True,digest))
        self.assertEqual(result['verification'],'source_md5_and_size')
        self.assertEqual(result['dimensions'],[64,48])

    def test_mixed_message_keeps_text_and_media_together(self):
        original=image_bytes((64,48),'PNG')
        (self.cache/'original.png').write_bytes(original)
        self.mapping('synthetic-file-id','Image/original.png',hashlib.md5(original).hexdigest())
        payload=data(1,number(5,2)+data(101,text_payload('附图说明')))+data(1,number(5,14)+data(103,self.payload(original)))
        result=decode_payload(123,payload,b'',MediaIndex(self.snapshots,self.account,self.output))
        self.assertEqual(result['kind'],'mixed')
        self.assertEqual(result['text'],'附图说明')
        self.assertEqual([(item['kind'],item['status']) for item in result['attachments']],[('image','ready')])

    def test_file_mismatch_cannot_use_image_variant_relaxation(self):
        original=image_bytes((64,48),'PNG')
        (self.cache/'wrong.png').write_bytes(image_bytes((16,12),'PNG'))
        self.mapping('synthetic-file-id','Image/wrong.png',hashlib.md5(original).hexdigest())
        result=MediaIndex(self.snapshots,self.account,self.output).resolve('file',self.payload(original))
        self.assertEqual(result['status'],'pending_download')
        self.assertIn('size_mismatch',result['reason'])
        self.assertFalse(any(self.output.glob('*')))

    def test_mapping_cannot_read_outside_the_declared_cache(self):
        outside=self.root/'private.txt';outside.write_bytes(b'not a chat attachment')
        self.mapping('synthetic-file-id',outside,hashlib.md5(outside.read_bytes()).hexdigest())
        payload=data(1,'synthetic-file-id')+data(2,'private.txt')+number(4,outside.stat().st_size)+data(10,hashlib.md5(outside.read_bytes()).hexdigest())
        result=MediaIndex(self.snapshots,self.account,self.output).resolve('file',payload)
        self.assertEqual((result['status'],result['reason']),('pending_download','not_in_local_cache'))
        self.assertFalse(any(self.output.glob('*')))


if __name__=='__main__':unittest.main()


class _FakeMedia:
    def resolve(self, kind, payload, local_extra=b''):
        return {'kind': kind, 'status': 'pending_download', 'reason': 'synthetic'}

    def resolve_metadata(self, **kwargs):
        return {'kind': kwargs.get('kind'), 'name': kwargs.get('name'),
                'size': kwargs.get('size'), 'md5': kwargs.get('digest'),
                'status': 'pending_download', 'reason': 'synthetic'}


class _FakeFileIndex:
    def __init__(self, entries):
        self.entries = entries

    def for_message(self, message_id):
        return list(self.entries)


class ExtendedDecoderTests(unittest.TestCase):
    def test_new_link_card_is_parsed_and_cleaned(self):
        payload = (data(1, 'https://example.test/doc，后文')
                   + data(3, '主席团竞选问卷') + data(4, '收集表'))
        value = decode_payload(13, payload, b'', None)
        self.assertEqual(value['kind'], 'link')
        self.assertEqual(value['links'][0]['url'], 'https://example.test/doc')
        self.assertEqual(value['links'][0]['title'], '主席团竞选问卷')
        self.assertEqual(value['text'], '主席团竞选问卷\n收集表')

    def test_text_url_never_swallows_chinese_punctuation_or_prose(self):
        value = decode_payload(2, text_payload('详见 https://example.test/a?x=1，谢谢'), b'', None)
        self.assertEqual(value['links'], [{'url': 'https://example.test/a?x=1'}])

    def test_file_card_uses_file_db_entries_for_every_file(self):
        entries = [
            {'name': '一.docx', 'size': 10, 'md5': 'a' * 32, 'file_index': 0},
            {'name': '二.pdf', 'size': 20, 'md5': 'b' * 32, 'file_index': 1},
        ]
        value = decode_payload(36, b'', b'', _FakeMedia(), file_index=_FakeFileIndex(entries), message_id=90)
        self.assertEqual(value['kind'], 'file')
        self.assertEqual([item['name'] for item in value['attachments']], ['一.docx', '二.pdf'])
        self.assertFalse(value['unsupported'])

    def test_document_edit_and_group_system_text_are_kept(self):
        self.assertEqual(decode_payload(1011, '你编辑了表格'.encode(), b'', None)['text'], '你编辑了表格')
        self.assertEqual(decode_payload(1022, data(1, '已经成为新的群主'), b'', None)['text'], '已经成为新的群主')

    def test_call_record_keeps_visible_duration(self):
        value = decode_payload(40, data(3, '通话时长00:14'), b'', None)
        self.assertEqual((value['kind'], value['text']), ('call', '通话时长00:14'))

    def test_member_event_is_unsupported_but_keeps_member_ids(self):
        value = decode_payload(1002, b'2000010000000001;2000010000000002', b'', None)
        self.assertEqual(value['kind'], 'system')
        self.assertTrue(value['unsupported'])
        self.assertEqual(value['member_id'], '2000010000000001')
        self.assertEqual(value['member_ids'], ['2000010000000001', '2000010000000002'])

    def test_mini_program_card_extracts_target_url(self):
        path = 'pages/preview/preview.html?checkLink=https%3A%2F%2Fexample.test%2Fform'
        payload = data(107, data(3, path) + data(7, '接龙') + data(10, '问卷星'))
        value = decode_payload(78, payload, b'', None)
        self.assertEqual(value['links'][0]['url'], 'https://example.test/form')
        self.assertEqual(value['text'], '接龙\n问卷星')

    def test_conversation_meta_distinguishes_group_and_private(self):
        users = {'2000012': '示例联系人'}
        group = conversation_meta('R:123', '24级群', users)
        self.assertEqual((group['conversation_type'], group['is_group']), ('group', True))
        direct = conversation_meta('S:2000012_2000018', '', users)
        self.assertEqual((direct['conversation_type'], direct['peer_id'], direct['conversation_name']),
                         ('direct', '2000012', '示例联系人'))
        self.assertEqual(conversation_meta('FILEASSIST', 'File Transfer', users)['conversation_type'], 'self')
        self.assertEqual(conversation_meta('O:1', '统一身份认证', users)['conversation_type'], 'official')

    def test_normalize_url_rejects_non_http_and_cleans_trailing(self):
        self.assertEqual(normalize_url('javascript:alert(1)'), '')
        self.assertEqual(normalize_url('https://example.test/b.'), 'https://example.test/b')
        self.assertEqual(normalize_url('https://example.test/c）提交申请。'), 'https://example.test/c')
        self.assertEqual(normalize_url('https://example.test/d#rd'), 'https://example.test/d#rd')

    def test_unknown_type_still_preserves_printable_strings(self):
        value = decode_payload(9999, data(2, '可见文字'), b'', None)
        self.assertEqual(value['text'], '可见文字')
        self.assertTrue(value['unsupported'])
