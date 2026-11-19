
import os
import struct
from io import BytesIO

import mutagen

from mutagen.wave import WAVE
from mutagen._iff import InvalidChunk
from tests import TestCase, DATA_DIR, get_temp_copy


def _chunk(id_, data, size=None):
    if size is None:
        size = len(data)
    # The padding is always based on the actual payload length, the
    # declared size might be a placeholder.
    return id_ + struct.pack('<I', size) + data + \
        (b'\x00' if len(data) % 2 else b'')


def make_rf64_file(root_id=b'RF64', data_size=48000 * 5 * 6, id3_data=None,
                   table_ids=(), data_placeholder=True):
    """Create a minimal RF64/BW64/WAVE file.

    ``table_ids`` lists the chunk IDs which are stored with a 32-bit
    placeholder and an entry in the ds64 chunk table. The ``data`` chunk
    always uses a placeholder header and its size is stored in the
    ``dataSize`` field of the ds64 chunk.
    """

    fmt = struct.pack('<HHLLHH', 1, 2, 48000, 48000 * 6, 6, 24)
    sizes = {b'fmt ': len(fmt)}
    body = b'fmt ' + struct.pack(
        '<I', 0xFFFFFFFF if b'fmt ' in table_ids else len(fmt)) + fmt
    if id3_data is not None:
        body += _chunk(b'id3 ', id3_data)
    if data_placeholder:
        body += b'data' + struct.pack('<I', 0xFFFFFFFF) + \
            bytes(data_size) + (b'\x00' if data_size % 2 else b'')
    else:
        body += _chunk(b'data', bytes(data_size))

    table_entries = b''
    for id_ in table_ids:
        table_entries += struct.pack('<4sQ', id_, sizes[id_])

    ds64_len = 28 + 12 * len(table_ids)
    riff_size = 4 + 8 + ds64_len + len(body)
    ds64 = struct.pack('<QQQI', riff_size, data_size, data_size // 6,
                       len(table_ids)) + table_entries
    data = (root_id + struct.pack('<I', 0xFFFFFFFF) + b'WAVE' +
            _chunk(b'ds64', ds64) + body)
    return data


def parse_rf64(data):
    """Parse an RF64/BW64 file, resolving the ds64 sizes."""

    root_id, root_size = struct.unpack('<4sI', data[:8])
    assert data[8:12] == b'WAVE'
    assert data[12:16] == b'ds64'
    ds64_size = struct.unpack('<I', data[16:20])[0]
    ds64 = struct.unpack('<QQQI', data[20:48])
    table = {}
    for i in range(ds64[3]):
        id_, size = struct.unpack('<4sQ', data[48 + 12 * i:60 + 12 * i])
        table[id_] = size
    off = 12 + 8 + ds64_size + (ds64_size % 2)
    chunks = []
    while off + 8 <= len(data):
        id_ = data[off:off + 4]
        raw_size = struct.unpack('<I', data[off + 4:off + 8])[0]
        size = table.get(id_, raw_size)
        if size == 0xFFFFFFFF:
            size = ds64[1] if id_ == b'data' else len(data) - off - 8
        chunks.append((id_, raw_size, size, off))
        off += 8 + size + (size % 2)
    return {
        'root_id': root_id,
        'root_size': root_size,
        'ds64': ds64,
        'chunks': chunks,
    }


class TRf64Wave(TestCase):

    ROOT_IDS = (b'RF64', b'BW64')

    def test_score(self):
        for root_id in self.ROOT_IDS:
            header = root_id + b'\xff\xff\xff\xffWAVE'
            with self.subTest(root_id=root_id):
                self.assertEqual(
                    WAVE.score('audio', BytesIO(header), header), 2)
                self.assertEqual(
                    WAVE.score('audio.wav', BytesIO(header), header), 3)
                self.assertEqual(
                    WAVE.score('audio', BytesIO(), b""), 0)

    def test_stream_info(self):
        for root_id in self.ROOT_IDS:
            for table_ids in ((), (b'fmt ',)):
                data = make_rf64_file(root_id, table_ids=table_ids)
                with self.subTest(root_id=root_id, table_ids=table_ids):
                    info = WAVE(BytesIO(data)).info
                    self.assertEqual(info.channels, 2)
                    self.assertEqual(info.sample_rate, 48000)
                    self.assertEqual(info.bits_per_sample, 24)
                    self.assertAlmostEqual(info.length, 5.0, 2)
                    self.assertEqual(info._number_of_samples, 240000)

    def test_stream_info_from_ds64_not_32bit(self):
        # The 32 bit data size must not be used, the ds64 size is
        # authoritative for the duration.
        data = bytearray(make_rf64_file(data_size=48000 * 5 * 6))
        # data chunk header directly follows fmt, which follows ds64
        data_off = data.index(b'data')
        data[data_off + 4:data_off + 8] = struct.pack('<I', 0)
        wav = WAVE(BytesIO(bytes(data)))
        self.assertAlmostEqual(wav.info.length, 5.0, 2)

    def test_riff_size_is_placeholder(self):
        for root_id in self.ROOT_IDS:
            data = make_rf64_file(root_id)
            with self.subTest(root_id=root_id):
                parsed = parse_rf64(data)
                self.assertEqual(parsed['root_size'], 0xFFFFFFFF)
                self.assertEqual(parsed['ds64'][0], len(data) - 8)

    def test_save_and_delete_id3_after_data(self):
        import io
        import tempfile
        from mutagen.id3 import ID3, TIT2

        tag = ID3()
        tag.add(TIT2(encoding=3, text='Existing'))
        bio = io.BytesIO()
        tag.save(bio, v2_version=3)

        for root_id in self.ROOT_IDS:
            for table_ids in ((), (b'fmt ',)):
                with self.subTest(root_id=root_id, table_ids=table_ids):
                    data = make_rf64_file(
                        root_id, id3_data=bio.getvalue(),
                        table_ids=table_ids)
                    before = parse_rf64(data)
                    audio_before = self._chunk_data(data, before, b'data')

                    fd, name = tempfile.mkstemp(suffix='.wav')
                    try:
                        os.write(fd, data)
                        os.close(fd)

                        wav = WAVE(name)
                        self.assertEqual(wav.tags['TIT2'], ['Existing'])
                        wav.tags.add(TIT2(encoding=3, text='Updated'))
                        wav.tags.save(name)

                        with open(name, 'rb') as f:
                            saved = f.read()
                        after = parse_rf64(saved)
                        self.assertEqual(after['root_id'], root_id)
                        self.assertEqual(after['root_size'], 0xFFFFFFFF)
                        self.assertEqual(after['ds64'][0], len(saved) - 8)
                        self.assertEqual(after['ds64'][1],
                                         len(audio_before))
                        # Placeholders must be preserved
                        original_raw = {
                            c[0]: c[1] for c in before['chunks']}
                        for id_, raw_size, size, off in after['chunks']:
                            if original_raw.get(id_) == 0xFFFFFFFF:
                                self.assertEqual(raw_size, 0xFFFFFFFF)
                        self.assertEqual(
                            self._chunk_data(saved, after, b'data'),
                            audio_before)

                        reread = WAVE(name)
                        self.assertEqual(reread.tags['TIT2'], ['Updated'])
                        self.assertEqual(reread.info.channels, 2)
                        self.assertEqual(reread.info.sample_rate, 48000)
                        self.assertEqual(reread.info.bits_per_sample, 24)
                        self.assertAlmostEqual(reread.info.length, 5.0, 2)

                        # Now delete the tag and verify the file again
                        reread.delete()
                        with open(name, 'rb') as f:
                            deleted_data = f.read()
                    finally:
                        os.unlink(name)

                    deleted_parsed = parse_rf64(deleted_data)
                    self.assertEqual(deleted_parsed['root_size'], 0xFFFFFFFF)
                    self.assertEqual(deleted_parsed['ds64'][0],
                                     len(deleted_data) - 8)
                    self.assertEqual(
                        self._chunk_data(deleted_data, deleted_parsed,
                                         b'data'),
                        audio_before)
                    self.assertNotIn(
                        b'id3 ', [c[0] for c in deleted_parsed['chunks']])
                    reread2 = WAVE(BytesIO(deleted_data))
                    self.assertIsNone(reread2.tags)
                    self.assertAlmostEqual(reread2.info.length, 5.0, 2)

    def test_missing_ds64(self):
        data = (b'RF64' + struct.pack('<I', 0xFFFFFFFF) + b'WAVE' +
                _chunk(b'fmt ', struct.pack('<HHLLHH', 1, 2, 48000,
                                            48000 * 6, 6, 24)) +
                _chunk(b'data', b'\x00' * 100))
        self.assertRaises(InvalidChunk, WAVE, BytesIO(data))

    def test_invalid_ds64(self):
        data = (b'RF64' + struct.pack('<I', 0xFFFFFFFF) + b'WAVE' +
                _chunk(b'ds64', b'\x00' * 10))
        self.assertRaises(InvalidChunk, WAVE, BytesIO(data))

    def test_rf64_non_wave(self):
        data = (b'RF64' + struct.pack('<I', 0xFFFFFFFF) + b'AVI ' +
                _chunk(b'ds64', struct.pack('<QQQI', 0, 0, 0, 0)))
        self.assertRaises(Exception, WAVE, BytesIO(data))

    def test_plain_wav_still_works(self):
        # Make sure normal RIFF/WAVE files are not treated as RF64.
        data = (b'RIFF' + struct.pack('<I', 36 + 64) + b'WAVE' +
                _chunk(b'fmt ', struct.pack('<HHLLHH', 1, 1, 8000,
                                            8000, 1, 8)) +
                _chunk(b'data', b'\x80' * 64))
        wav = WAVE(BytesIO(data))
        self.assertEqual(wav.info.channels, 1)
        self.assertAlmostEqual(wav.info.length, 64 / 8000.0)

    def _chunk_data(self, data, parsed, id_):
        for chunk_id, raw_size, size, off in parsed['chunks']:
            if chunk_id == id_:
                return data[off + 8:off + 8 + size]
        raise KeyError(id_)


class TWave(TestCase):
    def test_score_requires_wave_form(self):
        for form, filename in [(b"WEBP", "cover.webp"), (b"AVI ", "video.avi")]:
            header = b"RIFF\x08\x00\x00\x00" + form
            with self.subTest(form=form):
                self.assertEqual(WAVE.score(filename, BytesIO(header), header), 0)
                self.assertIsNone(mutagen.File(BytesIO(header)))

    def test_score_wave_header_and_extensions(self):
        header = b"RIFF\x08\x00\x00\x00WAVE"
        self.assertEqual(WAVE.score("audio", BytesIO(header), header), 2)
        for filename in ("audio.wav", "audio.WAVE"):
            with self.subTest(filename=filename):
                self.assertEqual(WAVE.score(filename, BytesIO(header), header), 3)
                self.assertEqual(WAVE.score(filename, BytesIO(), b""), 1)
        for header in (b"", b"RIFF", b"RIFF\x08\x00\x00\x00WA",
                       b"xxxx\x00\x00\x00\x00WAVE"):
            with self.subTest(header=header):
                self.assertEqual(WAVE.score("audio", BytesIO(header), header), 0)

    def setUp(self):
        fn_wav_pcm_2s_16000_08_id3v23 = \
            os.path.join(DATA_DIR, "silence-2s-PCM-16000-08-ID3v23.wav")
        self.wav_pcm_2s_16000_08_ID3v23 = \
            WAVE(fn_wav_pcm_2s_16000_08_id3v23)

        self.tmp_fn_pcm_2s_16000_08_ID3v23 = \
            get_temp_copy(fn_wav_pcm_2s_16000_08_id3v23)
        self.tmp_wav_pcm_2s_16000_08_ID3v23 = \
            WAVE(self.tmp_fn_pcm_2s_16000_08_ID3v23)

        self.fn_wav_pcm_2s_16000_08_notags = \
            os.path.join(DATA_DIR, "silence-2s-PCM-16000-08-notags.wav")
        self.wav_pcm_2s_16000_08_notags = \
            WAVE(self.fn_wav_pcm_2s_16000_08_notags)

        self.tmp_fn_pcm_2s_16000_08_notag = \
            get_temp_copy(self.fn_wav_pcm_2s_16000_08_notags)
        self.tmp_wav_pcm_2s_16000_08_notag = \
            WAVE(self.tmp_fn_pcm_2s_16000_08_notag)

        fn_wav_pcm_2s_44100_16_id3v23 = \
            os.path.join(DATA_DIR, "silence-2s-PCM-44100-16-ID3v23.wav")
        self.wav_pcm_2s_44100_16_ID3v23 = WAVE(fn_wav_pcm_2s_44100_16_id3v23)

    def test_channels(self):
        self.failUnlessEqual(self.wav_pcm_2s_16000_08_ID3v23.info.channels, 2)
        self.failUnlessEqual(self.wav_pcm_2s_44100_16_ID3v23.info.channels, 2)

    def test_sample_rate(self):
        self.failUnlessEqual(self.wav_pcm_2s_16000_08_ID3v23.info.sample_rate,
                             16000)
        self.failUnlessEqual(self.wav_pcm_2s_44100_16_ID3v23.info.sample_rate,
                             44100)

    def test_number_of_samples(self):
        self.failUnlessEqual(self.wav_pcm_2s_16000_08_ID3v23.
                             info._number_of_samples, 32000)
        self.failUnlessEqual(self.wav_pcm_2s_44100_16_ID3v23.
                             info._number_of_samples, 88200)

    def test_bits_per_sample(self):
        self.failUnlessEqual(self.wav_pcm_2s_16000_08_ID3v23.
                             info.bits_per_sample, 8)
        self.failUnlessEqual(self.wav_pcm_2s_44100_16_ID3v23.
                             info.bits_per_sample, 16)

    def test_bitrate(self):
        self.failUnlessEqual(self.wav_pcm_2s_16000_08_ID3v23.
                             info.bitrate, 256000)
        self.failUnlessEqual(self.wav_pcm_2s_44100_16_ID3v23.
                             info.bitrate, 1411200)

    def test_length(self):
        self.failUnlessAlmostEqual(self.wav_pcm_2s_16000_08_ID3v23.info.length,
                                   2.0, 2)
        self.failUnlessAlmostEqual(self.wav_pcm_2s_44100_16_ID3v23.info.length,
                                   2.0, 2)

    def test_not_my_file(self):
        self.failUnlessRaises(
            InvalidChunk, WAVE, os.path.join(DATA_DIR, "empty.ogg"))

    def test_pprint(self):
        self.wav_pcm_2s_44100_16_ID3v23.pprint()

    def test_mime(self):
        self.failUnless("audio/wav" in self.wav_pcm_2s_44100_16_ID3v23.mime)
        self.failUnless("audio/wave" in self.wav_pcm_2s_44100_16_ID3v23.mime)

    def test_id3_tags(self):
        id3 = self.wav_pcm_2s_44100_16_ID3v23.tags
        self.assertEquals(id3["TALB"], "Quod Libet Test Data")
        self.assertEquals(id3["TCON"], "Silence")
        self.assertEquals(id3["TIT2"], "Silence")
        self.assertEquals(id3["TPE1"], ["piman / jzig"])

    def test_id3_tags_uppercase_chunk(self):
        id3 = self.wav_pcm_2s_16000_08_ID3v23
        self.assertEquals(id3["TALB"], "Quod Libet Test Data")
        self.assertEquals(id3["TCON"], "Silence")
        self.assertEquals(id3["TIT2"], "Silence")
        self.assertEquals(id3["TPE1"], ["piman / jzig"])

    def test_delete(self):
        self.tmp_wav_pcm_2s_16000_08_ID3v23.delete()

        self.failIf(self.tmp_wav_pcm_2s_16000_08_ID3v23.tags)
        self.failUnless(WAVE(self.tmp_fn_pcm_2s_16000_08_ID3v23).tags is None)

    def test_save_no_tags(self):
        self.tmp_wav_pcm_2s_16000_08_ID3v23.tags = None
        self.tmp_wav_pcm_2s_16000_08_ID3v23.save()
        self.assertTrue(self.tmp_wav_pcm_2s_16000_08_ID3v23.tags is None)

    def test_add_tags_already_there(self):
        self.failUnless(self.tmp_wav_pcm_2s_16000_08_ID3v23.tags)
        self.failUnlessRaises(Exception,
                              self.tmp_wav_pcm_2s_16000_08_ID3v23.add_tags)

    def test_roundtrip(self):
        self.failUnlessEqual(self.tmp_wav_pcm_2s_16000_08_ID3v23["TIT2"],
                             ["Silence"])
        self.tmp_wav_pcm_2s_16000_08_ID3v23.save()
        new = WAVE(self.tmp_wav_pcm_2s_16000_08_ID3v23.filename)
        self.failUnlessEqual(new["TIT2"], ["Silence"])

    def test_save_tags(self):
        from mutagen.id3 import TIT1
        tags = self.tmp_wav_pcm_2s_16000_08_ID3v23.tags
        tags.add(TIT1(encoding=3, text="foobar"))
        tags.save()

        new = WAVE(self.tmp_wav_pcm_2s_16000_08_ID3v23.filename)
        self.failUnlessEqual(new["TIT1"], ["foobar"])

    """" Simulate the way Picard writes and update tags """
    def test_picard_lifecycle(self):
        path_tmp_wav_file = \
            get_temp_copy(self.fn_wav_pcm_2s_16000_08_notags)
        from mutagen.id3 import ID3
        wav = WAVE(path_tmp_wav_file)
        id3 = wav.tags
        """" Picard WaveFile._get_tags: """
        self.assertIsNone(id3, "Ensure ID3-tag-header does not exist")
        """" Picard WaveFile._get_tags: initialize tags """
        wav.add_tags()
        id3 = wav.tags
        self.assertIsInstance(id3, ID3)
        """ ID3v2.3 separator """
        separator = '/'
        """ Initialize Picard like metadata tags """
        self.__init_id3_tags(id3, major=3)
        """ Write the Picard like metadata to the empty WAVE-file """
        id3.save(path_tmp_wav_file, v23_sep=separator)
        """ Tags (metadata) have been added; now load the file again """
        wav = WAVE(path_tmp_wav_file)
        id3 = wav.tags
        self.assertIsInstance(id3, ID3)
        self.assertEquals(id3["TRCK"], "1/10")
        self.assertEquals(id3["TPOS"], "1/1")
        self.assertEquals(id3["TXXX:MusicBrainz Release Group Id"],
                          "e00305af-1c72-469b-9a7c-6dc665ca9adc")
        self.assertEquals(id3["TXXX:MusicBrainz Album Artist Id"], [
                          "3fe817fc-966e-4ece-b00a-76be43e7e73c",
                          "984f8239-8fe1-4683-9c54-10ffb14439e9"])
        self.assertEquals(id3["TXXX:CATALOGNUMBER"], ["PRAR931391"])
        self.assertEquals(id3["TSRC"], ["NLB931100460", "USMH51100098"])

    @staticmethod
    def __init_id3_tags(id3, major=3):
        """
        Attributes:
            id3 ID3 Tag object
            major ID3 major version, e.g.: 3 for ID3v2.3
        """
        from mutagen.id3 import TRCK, TPOS, TXXX, TPUB, TALB, UFID, TPE2, \
            TSO2, TMED, TIT2, TPE1, TSRC, IPLS, TORY, TDAT, TYER
        id3.add(TRCK(encoding=major, text="1/10"))
        id3.add(TPOS(encoding=major, text="1/1"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Release Group Id",
                     text="e00305af-1c72-469b-9a7c-6dc665ca9adc"))
        id3.add(TXXX(encoding=major, desc="originalyear", text="2011"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Album Type",
                     text="album"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Album Id",
                     text="e7050302-74e6-42e4-aba0-09efd5d431d8"))
        id3.add(TPUB(encoding=major, text="J&R Adventures"))
        id3.add(TXXX(encoding=major, desc="CATALOGNUMBER", text="PRAR931391"))
        id3.add(TALB(encoding=major, text="Don\'t Explain"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Album Status",
                     text="official"))
        id3.add(TXXX(encoding=major, desc="SCRIPT", text="Latn"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Album Release Country",
                     text="US"))
        id3.add(TXXX(encoding=major, desc="BARCODE", text="804879313915"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Album Artist Id",
                     text=[
                        "3fe817fc-966e-4ece-b00a-76be43e7e73c",
                        "984f8239-8fe1-4683-9c54-10ffb14439e9"]))
        id3.add(TPE2(encoding=major, text="Beth Hart & Joe Bonamassa"))
        id3.add(TSO2(encoding=major, text="Hart, Beth & Bonamassa, Joe"))
        id3.add(TXXX(encoding=major, desc="ASIN", text="B005NPEUB2"))
        id3.add(TMED(encoding=major, text="CD"))
        id3.add(UFID(encoding=major, owner="http://musicbrainz.org",
                     data=b"f151cb94-c909-46a8-ad99-fb77391abfb8"))
        id3.add(TIT2(encoding=major, text="Sinner's Prayer"))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Artist Id",
                     text=[
                        "3fe817fc-966e-4ece-b00a-76be43e7e73c",
                        "984f8239-8fe1-4683-9c54-10ffb14439e9"]))
        id3.add(TPE1(encoding=major, text=["Beth Hart & Joe Bonamassa"]))
        id3.add(TXXX(encoding=major, desc="Artists",
                     text=["Beth Hart", "Joe Bonamassa"]))
        id3.add(TSRC(encoding=major, text=["NLB931100460", "USMH51100098"]))
        id3.add(TXXX(encoding=major, desc="MusicBrainz Release Track Id",
                     text="d062f484-253c-374b-85f7-89aab45551c7"))
        id3.add(IPLS(encoding=major, people=[
            ["engineer", "James McCullagh"],
            ["engineer", "Jared Kvitka"],
            ["arranger", "Jeff Bova"],
            ["producer", "Roy Weisman"],
            ["piano", "Beth Hart"],
            ["guitar", "Blondie Chaplin"],
            ["guitar", "Joe Bonamassa"],
            ["percussion", "Anton Fig"],
            ["drums", "Anton Fig"],
            ["keyboard", "Arlan Schierbaum"],
            ["bass guitar", "Carmine Rojas"],
            ["orchestra", "The Bovaland Orchestra"],
            ["vocals", "Beth Hart"],
            ["vocals", "Joe Bonamassa"]])),
        id3.add(TORY(encoding=major, text="2011"))
        id3.add(TYER(encoding=major, text="2011"))
        id3.add(TDAT(encoding=major, text="2709"))
