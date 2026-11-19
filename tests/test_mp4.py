
import os
import struct
import subprocess
from io import BytesIO
from tempfile import mkstemp

import pytest

from tests import TestCase, DATA_DIR, get_temp_copy
from mutagen.mp4 import (MP4, MP4Tags, MP4Info, delete, MP4Cover,
                         MP4MetadataError, MP4FreeForm, error, AtomDataType,
                         _item_sort_key, MP4StreamInfoError)
from mutagen.mp4._atom import Atom, Atoms, AtomError
from mutagen.mp4._util import parse_full_atom
from mutagen.mp4._as_entry import AudioSampleEntry, ASEntryError
from mutagen._util import cdata


class TAtom(TestCase):

    def test_no_children(self):
        fileobj = BytesIO(b"\x00\x00\x00\x08atom")
        atom = Atom(fileobj)
        self.failUnlessRaises(KeyError, atom.__getitem__, "test")

    def test_length_1(self):
        fileobj = BytesIO(b"\x00\x00\x00\x01atom"
                          b"\x00\x00\x00\x00\x00\x00\x00\x10" + b"\x00" * 16)
        atom = Atom(fileobj)
        self.failUnlessEqual(atom.length, 16)
        self.failUnlessEqual(atom.datalength, 0)

    def test_length_64bit_less_than_16(self):
        fileobj = BytesIO(b"\x00\x00\x00\x01atom"
                          b"\x00\x00\x00\x00\x00\x00\x00\x08" + b"\x00" * 8)
        self.assertRaises(AtomError, Atom, fileobj)

    def test_length_less_than_8(self):
        fileobj = BytesIO(b"\x00\x00\x00\x02atom")
        self.assertRaises(AtomError, Atom, fileobj)

    def test_truncated(self):
        self.assertRaises(AtomError, Atom, BytesIO(b"\x00"))
        self.assertRaises(AtomError, Atom, BytesIO(b"\x00\x00\x00\x01atom"))

    def test_render_too_big(self):
        class TooBig(bytes):
            def __len__(self):
                return 1 << 32
        data = TooBig(b"test")
        try:
            len(data)
        except OverflowError:
            # Py_ssize_t is still only 32 bits on this system.
            self.failUnlessRaises(OverflowError, Atom.render, b"data", data)
        else:
            data = Atom.render(b"data", data)
            self.failUnlessEqual(len(data), 4 + 4 + 8 + 4)

    def test_non_top_level_length_0_is_invalid(self):
        data = BytesIO(struct.pack(">I4s", 0, b"whee"))
        self.assertRaises(AtomError, Atom, data, level=1)

    def test_length_0(self):
        fileobj = BytesIO(b"\x00\x00\x00\x00atom" + 40 * b"\x00")
        atom = Atom(fileobj)
        self.failUnlessEqual(fileobj.tell(), 48)
        self.failUnlessEqual(atom.length, 48)
        self.failUnlessEqual(atom.datalength, 40)

    def test_length_0_container(self):
        data = BytesIO(struct.pack(">I4s", 0, b"moov") +
                       Atom.render(b"data", b"whee"))
        atom = Atom(data)
        self.failUnlessEqual(len(atom.children), 1)
        self.failUnlessEqual(atom.length, 20)
        self.failUnlessEqual(atom.children[-1].length, 12)

    def test_read(self):
        payload = 8 * b"\xff"
        fileobj = BytesIO(b"\x00\x00\x00\x10atom" + payload)
        atom = Atom(fileobj)
        ok, data = atom.read(fileobj)
        self.assertTrue(ok)
        self.assertEqual(data, payload)

        payload = 7 * b"\xff"
        fileobj = BytesIO(b"\x00\x00\x00\x10atom" + payload)
        atom = Atom(fileobj)
        ok, data = atom.read(fileobj)
        self.assertFalse(ok)
        self.assertEqual(data, payload)


class TAtoms(TestCase):
    filename = os.path.join(DATA_DIR, "has-tags.m4a")

    def setUp(self):
        with open(self.filename, "rb") as h:
            self.atoms = Atoms(h)

    def test_getitem(self):
        self.failUnless(self.atoms[b"moov"])
        self.failUnless(self.atoms[b"moov.udta"])
        self.failUnlessRaises(KeyError, self.atoms.__getitem__, b"whee")

    def test_contains(self):
        self.failUnless(b"moov" in self.atoms)
        self.failUnless(b"moov.udta" in self.atoms)
        self.failUnless(b"whee" not in self.atoms)

    def test_name(self):
        self.failUnlessEqual(self.atoms.atoms[0].name, b"ftyp")

    def test_children(self):
        self.failUnless(self.atoms.atoms[2].children)

    def test_no_children(self):
        self.failUnless(self.atoms.atoms[0].children is None)

    def test_extra_trailing_data(self):
        data = BytesIO(Atom.render(b"data", b"whee") + b"\x00\x00")
        self.failUnless(Atoms(data))

    def test_repr(self):
        repr(self.atoms)


class TMP4Info(TestCase):

    def test_no_soun(self):
        self.failUnlessRaises(
            error, self.test_mdhd_version_1, b"vide")

    def test_mdhd_version_1(self, soun=b"soun"):
        mdhd = Atom.render(b"mdhd", (b"\x01\x00\x00\x00" + b"\x00" * 16 +
                                     b"\x00\x00\x00\x02" +  # 2 Hz
                                     b"\x00\x00\x00\x00\x00\x00\x00\x10"))
        hdlr = Atom.render(b"hdlr", b"\x00" * 8 + soun)
        mdia = Atom.render(b"mdia", mdhd + hdlr)
        trak = Atom.render(b"trak", mdia)
        moov = Atom.render(b"moov", trak)
        fileobj = BytesIO(moov)
        atoms = Atoms(fileobj)
        info = MP4Info(atoms, fileobj)
        self.failUnlessEqual(info.length, 8)

    def test_multiple_tracks(self):
        hdlr = Atom.render(b"hdlr", b"\x00" * 8 + b"whee")
        mdia = Atom.render(b"mdia", hdlr)
        trak1 = Atom.render(b"trak", mdia)
        mdhd = Atom.render(b"mdhd", (b"\x01\x00\x00\x00" + b"\x00" * 16 +
                                     b"\x00\x00\x00\x02" +  # 2 Hz
                                     b"\x00\x00\x00\x00\x00\x00\x00\x10"))
        hdlr = Atom.render(b"hdlr", b"\x00" * 8 + b"soun")
        mdia = Atom.render(b"mdia", mdhd + hdlr)
        trak2 = Atom.render(b"trak", mdia)
        moov = Atom.render(b"moov", trak1 + trak2)
        fileobj = BytesIO(moov)
        atoms = Atoms(fileobj)
        info = MP4Info(atoms, fileobj)
        self.failUnlessEqual(info.length, 8)

    def test_no_tracks(self):
        moov = Atom.render(b"moov", b"")
        fileobj = BytesIO(moov)
        atoms = Atoms(fileobj)
        with self.assertRaises(MP4StreamInfoError):
            MP4Info(atoms, fileobj)


class TMP4Tags(TestCase):

    def wrap_ilst(self, data):
        ilst = Atom.render(b"ilst", data)
        meta = Atom.render(b"meta", b"\x00" * 4 + ilst)
        data = Atom.render(b"moov", Atom.render(b"udta", meta))
        fileobj = BytesIO(data)
        return MP4Tags(Atoms(fileobj), fileobj)

    def test_parse_multiple_atoms(self):
        # while we don't write multiple values as multiple atoms
        # still read them
        # https://github.com/quodlibet/mutagen/issues/165
        data = Atom.render(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"foo")
        grp1 = Atom.render(b"\xa9grp", data)
        data = Atom.render(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"bar")
        grp2 = Atom.render(b"\xa9grp", data)
        tags = self.wrap_ilst(grp1 + grp2)
        self.assertEqual(tags["\xa9grp"], [u"foo", u"bar"])

    def test_purl(self):
        # purl can have 0 or 1 flags (implicit or utf8)
        data = Atom.render(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"foo")
        purl = Atom.render(b"purl", data)
        tags = self.wrap_ilst(purl)
        self.failUnlessEqual(tags["purl"], ["foo"])

        data = Atom.render(b"data", b"\x00\x00\x00\x00" + b"\x00" * 4 + b"foo")
        purl = Atom.render(b"purl", data)
        tags = self.wrap_ilst(purl)
        self.failUnlessEqual(tags["purl"], ["foo"])

        # invalid flag
        data = Atom.render(b"data", b"\x00\x00\x00\x03" + b"\x00" * 4 + b"foo")
        purl = Atom.render(b"purl", data)
        tags = self.wrap_ilst(purl)
        self.assertFalse("purl" in tags)

        self.assertTrue("purl" in tags._failed_atoms)

        # invalid utf8
        data = Atom.render(
            b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"\xff")
        purl = Atom.render(b"purl", data)
        tags = self.wrap_ilst(purl)
        self.assertFalse("purl" in tags)

    def test_genre(self):
        data = Atom.render(b"data", b"\x00" * 8 + b"\x00\x01")
        genre = Atom.render(b"gnre", data)
        tags = self.wrap_ilst(genre)
        self.failIf("gnre" in tags)
        self.failUnlessEqual(tags["\xa9gen"], ["Blues"])

    def test_empty_cpil(self):
        cpil = Atom.render(b"cpil", Atom.render(b"data", b"\x00" * 8))
        tags = self.wrap_ilst(cpil)
        self.assertFalse("cpil" in tags)

    def test_genre_too_big(self):
        data = Atom.render(b"data", b"\x00" * 8 + b"\x01\x00")
        genre = Atom.render(b"gnre", data)
        tags = self.wrap_ilst(genre)
        self.failIf("gnre" in tags)
        self.failIf("\xa9gen" in tags)

    def test_strips_unknown_types(self):
        data = Atom.render(b"data", b"\x00" * 8 + b"whee")
        foob = Atom.render(b"foob", data)
        tags = self.wrap_ilst(foob)
        self.failIf(tags)

    def test_strips_bad_unknown_types(self):
        data = Atom.render(b"datA", b"\x00" * 8 + b"whee")
        foob = Atom.render(b"foob", data)
        tags = self.wrap_ilst(foob)
        self.failIf(tags)

    def test_bad_covr(self):
        data = Atom.render(
            b"foob", b"\x00\x00\x00\x0E" + b"\x00" * 4 + b"whee")
        covr = Atom.render(b"covr", data)
        tags = self.wrap_ilst(covr)
        self.assertFalse(tags)

    def test_covr_blank_format(self):
        data = Atom.render(
            b"data", b"\x00\x00\x00\x00" + b"\x00" * 4 + b"whee")
        covr = Atom.render(b"covr", data)
        tags = self.wrap_ilst(covr)
        self.failUnlessEqual(
            MP4Cover.FORMAT_JPEG, tags["covr"][0].imageformat)

    def test_render_bool(self):
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_bool('pgap', True),
            b"\x00\x00\x00\x19pgap\x00\x00\x00\x11data"
            b"\x00\x00\x00\x15\x00\x00\x00\x00\x01"
        )
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_bool('pgap', False),
            b"\x00\x00\x00\x19pgap\x00\x00\x00\x11data"
            b"\x00\x00\x00\x15\x00\x00\x00\x00\x00"
        )

    def test_render_integer_min_size(self):
        render_int = MP4Tags()._MP4Tags__render_integer

        data = render_int('stik', [42], 1)
        tags = self.wrap_ilst(data)
        assert tags['stik'] == [42]

        assert len(render_int('stik', [42], 2)) == len(data) + 1
        assert len(render_int('stik', [42], 4)) == len(data) + 3
        assert len(render_int('stik', [42], 8)) == len(data) + 7

    def test_render_text(self):
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_text(
                'purl', ['http://foo/bar.xml'], 0),
            b"\x00\x00\x00*purl\x00\x00\x00\"data\x00\x00\x00\x00\x00\x00"
            b"\x00\x00http://foo/bar.xml"
        )
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_text(
                'aART', [u'\u0041lbum Artist']),
            b"\x00\x00\x00$aART\x00\x00\x00\x1cdata\x00\x00\x00\x01\x00\x00"
            b"\x00\x00\x41lbum Artist"
        )
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_text(
                'aART', [u'Album Artist', u'Whee']),
            b"\x00\x00\x008aART\x00\x00\x00\x1cdata\x00\x00\x00\x01\x00\x00"
            b"\x00\x00Album Artist\x00\x00\x00\x14data\x00\x00\x00\x01\x00"
            b"\x00\x00\x00Whee"
        )

    def test_render_data(self):
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_data('aART', 0, 1, [b'whee']),
            b"\x00\x00\x00\x1caART"
            b"\x00\x00\x00\x14data\x00\x00\x00\x01\x00\x00\x00\x00whee"
        )
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_data('aART', 0, 2, [b'whee', b'wee']),
            b"\x00\x00\x00/aART"
            b"\x00\x00\x00\x14data\x00\x00\x00\x02\x00\x00\x00\x00whee"
            b"\x00\x00\x00\x13data\x00\x00\x00\x02\x00\x00\x00\x00wee"
        )

    def test_bad_text_data(self):
        data = Atom.render(b"datA", b"\x00\x00\x00\x01\x00\x00\x00\x00whee")
        data = Atom.render(b"aART", data)
        tags = self.wrap_ilst(data)
        self.assertFalse(tags)

    def test_bad_cprt(self):
        data = Atom.render(b"cprt", b"\x00\x00\x00#data\x00")
        tags = self.wrap_ilst(data)
        self.assertFalse(tags)

    def test_parse_tmpo(self):
        for d, v in [(b"\x01", 1), (b"\x01\x02", 258),
                     (b"\x01\x02\x03", 66051), (b"\x01\x02\x03\x04", 16909060),
                     (b"\x01\x02\x03\x04\x05\x06\x07\x08", 72623859790382856)]:
            data = Atom.render(
                b"data", b"\x00\x00\x00\x15" + b"\x00\x00\x00\x00" + d)
            tmpo = Atom.render(b"tmpo", data)
            tags = self.wrap_ilst(tmpo)
            assert tags["tmpo"][0] == v

    def test_write_back_bad_atoms(self):
        # write a broken atom and try to load it
        data = Atom.render(b"datA", b"\x00\x00\x00\x01\x00\x00\x00\x00wheeee")
        data = Atom.render(b"aART", data)
        tags = self.wrap_ilst(data)
        self.assertFalse(tags)

        # save it into an existing mp4
        original = os.path.join(DATA_DIR, "has-tags.m4a")
        filename = get_temp_copy(original)
        try:
            delete(filename)

            # it should still end up in the file
            tags.save(filename)
            with open(filename, "rb") as h:
                self.assertTrue(b"wheeee" in h.read())

            # if we define our own aART throw away the broken one
            tags["aART"] = ["new"]
            tags.save(filename)
            with open(filename, "rb") as h:
                self.assertFalse(b"wheeee" in h.read())

            # add the broken one back and delete all tags including
            # the broken one
            del tags["aART"]
            tags.save(filename)
            with open(filename, "rb") as h:
                self.assertTrue(b"wheeee" in h.read())
            delete(filename)
            with open(filename, "rb") as h:
                self.assertFalse(b"wheeee" in h.read())
        finally:
            os.unlink(filename)

    def test_render_freeform(self):
        data = (
            b"\x00\x00\x00a----"
            b"\x00\x00\x00\"mean\x00\x00\x00\x00net.sacredchao.Mutagen"
            b"\x00\x00\x00\x10name\x00\x00\x00\x00test"
            b"\x00\x00\x00\x14data\x00\x00\x00\x01\x00\x00\x00\x00whee"
            b"\x00\x00\x00\x13data\x00\x00\x00\x01\x00\x00\x00\x00wee"
        )

        key = '----:net.sacredchao.Mutagen:test'
        self.failUnlessEqual(
            MP4Tags()._MP4Tags__render_freeform(key, [b'whee', b'wee']), data)

    def test_parse_freeform(self):
        double_data = (
            b"\x00\x00\x00a----"
            b"\x00\x00\x00\"mean\x00\x00\x00\x00net.sacredchao.Mutagen"
            b"\x00\x00\x00\x10name\x00\x00\x00\x00test"
            b"\x00\x00\x00\x14data\x00\x00\x00\x01\x00\x00\x00\x00whee"
            b"\x00\x00\x00\x13data\x00\x00\x00\x01\x00\x00\x00\x00wee"
        )

        key = '----:net.sacredchao.Mutagen:test'
        double_atom = \
            MP4Tags()._MP4Tags__render_freeform(key, [b'whee', b'wee'])

        tags = self.wrap_ilst(double_data)
        self.assertTrue(key in tags)
        self.assertEqual(tags[key], [b'whee', b'wee'])

        tags2 = self.wrap_ilst(double_atom)
        self.assertEqual(tags, tags2)

    def test_multi_freeform(self):
        # merge multiple freeform tags with the same key
        mean = Atom.render(b"mean", b"\x00" * 4 + b"net.sacredchao.Mutagen")
        name = Atom.render(b"name", b"\x00" * 4 + b"foo")

        data = Atom.render(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"bar")
        result = Atom.render(b"----", mean + name + data)
        data = Atom.render(
            b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"quux")
        result += Atom.render(b"----", mean + name + data)
        tags = self.wrap_ilst(result)
        values = tags["----:net.sacredchao.Mutagen:foo"]
        self.assertEqual(values[0], b"bar")
        self.assertEqual(values[1], b"quux")

    def test_bad_freeform(self):
        mean = Atom.render(b"mean", b"net.sacredchao.Mutagen")
        name = Atom.render(b"name", b"empty test key")
        bad_freeform = Atom.render(b"----", b"\x00" * 4 + mean + name)
        tags = self.wrap_ilst(bad_freeform)
        self.assertFalse(tags)

    def test_pprint_non_text_list(self):
        tags = MP4Tags()
        tags["tmpo"] = [120, 121]
        tags["trkn"] = [(1, 2), (3, 4)]
        tags.pprint()

    def test_freeform_data(self):
        # https://github.com/quodlibet/mutagen/issues/103
        key = "----:com.apple.iTunes:Encoding Params"
        value = (b"vers\x00\x00\x00\x01acbf\x00\x00\x00\x01brat\x00\x01\xf4"
                 b"\x00cdcv\x00\x01\x05\x04")

        data = (b"\x00\x00\x00\x1cmean\x00\x00\x00\x00com.apple.iTunes\x00\x00"
                b"\x00\x1bname\x00\x00\x00\x00Encoding Params\x00\x00\x000data"
                b"\x00\x00\x00\x00\x00\x00\x00\x00vers\x00\x00\x00\x01acbf\x00"
                b"\x00\x00\x01brat\x00\x01\xf4\x00cdcv\x00\x01\x05\x04")

        tags = self.wrap_ilst(Atom.render(b"----", data))
        v = tags[key][0]
        self.failUnlessEqual(v, value)
        self.failUnlessEqual(v.dataformat, AtomDataType.IMPLICIT)

        data = MP4Tags()._MP4Tags__render_freeform(key, v)
        v = self.wrap_ilst(data)[key][0]
        self.failUnlessEqual(v.dataformat, AtomDataType.IMPLICIT)

        data = MP4Tags()._MP4Tags__render_freeform(key, value)
        v = self.wrap_ilst(data)[key][0]
        self.failUnlessEqual(v.dataformat, AtomDataType.UTF8)


class TMP4(TestCase):

    def setUp(self):
        self.filename = get_temp_copy(self.original)
        self.audio = MP4(self.filename)

    def tearDown(self):
        os.unlink(self.filename)


class TMP4Mixin(object):

    def faad(self):
        if not have_faad:
            return
        self.assertEqual(call_faad("-w", self.filename), 0)

    def test_set_inval(self):
        self.assertRaises(TypeError, self.audio.__setitem__, "\xa9nam", 42)

    def test_score(self):
        fileobj = open(self.filename, "rb")
        header = fileobj.read(128)
        self.failUnless(MP4.score(self.filename, fileobj, header))
        fileobj.close()

    def test_channels(self):
        self.failUnlessEqual(self.audio.info.channels, 2)

    def test_sample_rate(self):
        self.failUnlessEqual(self.audio.info.sample_rate, 44100)

    def test_bits_per_sample(self):
        self.failUnlessEqual(self.audio.info.bits_per_sample, 16)

    def test_bitrate(self):
        self.failUnlessEqual(self.audio.info.bitrate, 2914)

    def test_length(self):
        self.failUnlessAlmostEqual(3.7, self.audio.info.length, 1)

    def test_kind(self):
        self.assertEqual(self.audio.info.codec, u'mp4a.40.2')

    def test_padding(self):
        self.audio["\xa9nam"] = u"wheeee" * 10
        self.audio.save()
        size1 = os.path.getsize(self.audio.filename)
        self.audio["\xa9nam"] = u"wheeee" * 11
        self.audio.save()
        size2 = os.path.getsize(self.audio.filename)
        self.failUnless(size1, size2)

    def test_padding_2(self):
        self.audio["\xa9nam"] = u"wheeee" * 10
        self.audio.save()

        # Reorder "free" and "ilst" atoms
        with open(self.audio.filename, "rb+") as fileobj:
            atoms = Atoms(fileobj)
            meta = atoms[b"moov", b"udta", b"meta"]
            meta_length1 = meta.length
            ilst = meta[b"ilst", ]
            free = meta[b"free", ]
            self.failUnlessEqual(ilst.offset + ilst.length, free.offset)
            fileobj.seek(ilst.offset)
            ilst_data = fileobj.read(ilst.length)
            fileobj.seek(free.offset)
            free_data = fileobj.read(free.length)
            fileobj.seek(ilst.offset)
            fileobj.write(free_data + ilst_data)

        with open(self.audio.filename, "rb+") as fileobj:
            atoms = Atoms(fileobj)
            meta = atoms[b"moov", b"udta", b"meta"]
            ilst = meta[b"ilst", ]
            free = meta[b"free", ]
            self.failUnlessEqual(free.offset + free.length, ilst.offset)

        # Save the file
        self.audio["\xa9nam"] = u"wheeee" * 11
        self.audio.save()

        # Check the order of "free" and "ilst" atoms
        with open(self.audio.filename, "rb+") as fileobj:
            atoms = Atoms(fileobj)

        meta = atoms[b"moov", b"udta", b"meta"]
        ilst = meta[b"ilst", ]
        free = meta[b"free", ]
        self.failUnlessEqual(meta.length, meta_length1)
        self.failUnlessEqual(ilst.offset + ilst.length, free.offset)

    def set_key(self, key, value, result=None, faad=True):
        self.audio[key] = value
        self.audio.save()
        audio = MP4(self.audio.filename)
        self.failUnless(key in audio)
        self.failUnlessEqual(audio[key], result or value)
        if faad:
            self.faad()

    def test_unicode(self):
        try:
            self.set_key('\xa9nam', [b'\xe3\x82\x8a\xe3\x81\x8b'],
                         result=[u'\u308a\u304b'])
        except TypeError:
            pass

    def test_preserve_freeform(self):
        self.set_key('----:net.sacredchao.Mutagen:test key',
                     [MP4FreeForm(b'woooo', 142, 42)])

    def test_invalid_text(self):
        self.assertRaises(
            TypeError, self.audio.__setitem__, '\xa9nam', [b'\xff'])

    def test_save_text(self):
        self.set_key('\xa9nam', [u"Some test name"])

    def test_save_texts(self):
        self.set_key('\xa9nam', [u"Some test name", u"One more name"])

    def test_freeform(self):
        self.set_key('----:net.sacredchao.Mutagen:test key', [b"whee"])

    def test_freeform_2(self):
        self.set_key(
            '----:net.sacredchao.Mutagen:test key', b"whee", [b"whee"])

    def test_freeforms(self):
        self.set_key(
            '----:net.sacredchao.Mutagen:test key', [b"whee", b"uhh"])

    def test_freeform_bin(self):
        self.set_key('----:net.sacredchao.Mutagen:test key', [
            MP4FreeForm(b'woooo', AtomDataType.UTF8),
            MP4FreeForm(b'hoooo', AtomDataType.IMPLICIT),
            MP4FreeForm(b'boooo'),
        ])

    def test_tracknumber(self):
        self.set_key('trkn', [(1, 10)])
        self.set_key('trkn', [(1, 10), (5, 20)], faad=False)
        self.set_key('trkn', [])

    def test_disk(self):
        self.set_key('disk', [(18, 0)])
        self.set_key('disk', [(1, 10), (5, 20)], faad=False)
        self.set_key('disk', [])

    def test_tracknumber_too_small(self):
        self.failUnlessRaises(ValueError, self.set_key, 'trkn', [(-1, 0)])
        self.failUnlessRaises(
            ValueError, self.set_key, 'trkn', [(2 ** 18, 1)])

    def test_disk_too_small(self):
        self.failUnlessRaises(ValueError, self.set_key, 'disk', [(-1, 0)])
        self.failUnlessRaises(
            ValueError, self.set_key, 'disk', [(2 ** 18, 1)])

    def test_tracknumber_wrong_size(self):
        self.failUnlessRaises(ValueError, self.set_key, 'trkn', (1,))
        self.failUnlessRaises(ValueError, self.set_key, 'trkn', (1, 2, 3,))
        self.failUnlessRaises(ValueError, self.set_key, 'trkn', [(1,)])
        self.failUnlessRaises(ValueError, self.set_key, 'trkn', [(1, 2, 3,)])

    def test_disk_wrong_size(self):
        self.failUnlessRaises(ValueError, self.set_key, 'disk', [(1,)])
        self.failUnlessRaises(ValueError, self.set_key, 'disk', [(1, 2, 3,)])

    def test_tempo(self):
        self.set_key('tmpo', [150])
        self.set_key('tmpo', [])
        self.set_key('tmpo', [0])
        self.set_key('tmpo', [cdata.int16_min])
        self.set_key('tmpo', [cdata.int32_min])
        self.set_key('tmpo', [cdata.int64_min])
        self.set_key('tmpo', [cdata.int16_max])
        self.set_key('tmpo', [cdata.int32_max])
        self.set_key('tmpo', [cdata.int64_max])

    def test_various_int(self):
        keys = [
            "stik", "hdvd", "rtng", "plID", "cnID", "geID", "atID",
            "sfID", "cmID", "akID", "tvsn", "tves",
        ]

        for key in keys:
            self.set_key(key, [])
            self.set_key(key, [0])
            self.set_key(key, [1])
            self.set_key(key, [cdata.int64_max])

    def test_movements(self):
        self.set_key('shwm', [1])
        self.set_key('\xa9mvc', [42])
        self.set_key('\xa9mvi', [24])
        self.set_key('\xa9mvn', [u"movement"])
        self.set_key('\xa9wrk', [u"work"])

    def test_tempos(self):
        self.set_key('tmpo', [160, 200], faad=False)

    def test_tempo_invalid(self):
        for badvalue in [
                [cdata.int64_max + 1], [cdata.int64_min - 1], 10, "foo"]:
            self.failUnlessRaises(ValueError, self.set_key, 'tmpo', badvalue)

    def test_compilation(self):
        self.set_key('cpil', True)

    def test_compilation_false(self):
        self.set_key('cpil', False)

    def test_gapless(self):
        self.set_key('pgap', True)

    def test_gapless_false(self):
        self.set_key('pgap', False)

    def test_podcast(self):
        self.set_key('pcst', True)

    def test_podcast_false(self):
        self.set_key('pcst', False)

    def test_cover(self):
        self.set_key('covr', [b'woooo'])

    def test_cover_png(self):
        self.set_key('covr', [
            MP4Cover(b'woooo', MP4Cover.FORMAT_PNG),
            MP4Cover(b'hoooo', MP4Cover.FORMAT_JPEG),
        ])

    def test_podcast_url(self):
        self.set_key('purl', ['http://pdl.warnerbros.com/wbie/'
                              'justiceleagueheroes/audio/JLH_EA.xml'])

    def test_episode_guid(self):
        self.set_key('catg', ['falling-star-episode-1'])

    def test_pprint(self):
        self.failUnless(self.audio.pprint())
        self.assertTrue(isinstance(self.audio.pprint(), str))

    def test_pprint_binary(self):
        self.audio["covr"] = [b"\x00\xa9garbage"]
        self.failUnless(self.audio.pprint())

    def test_pprint_pair(self):
        self.audio["cpil"] = (1, 10)
        self.failUnless("cpil=(1, 10)" in self.audio.pprint())

    def test_delete(self):
        self.audio.delete()
        audio = MP4(self.audio.filename)
        self.failIf(audio.tags)
        self.faad()

    def test_module_delete(self):
        delete(self.filename)
        audio = MP4(self.audio.filename)
        self.failIf(audio.tags)
        self.faad()

    def test_reads_unknown_text(self):
        self.set_key("foob", [u"A test"])

    def __read_offsets(self, filename):
        fileobj = open(filename, 'rb')
        atoms = Atoms(fileobj)
        moov = atoms[b'moov']
        samples = []
        for atom in moov.findall(b'stco', True):
            fileobj.seek(atom.offset + 12)
            data = fileobj.read(atom.length - 12)
            fmt = ">%dI" % cdata.uint_be(data[:4])
            offsets = struct.unpack(fmt, data[4:])
            for offset in offsets:
                fileobj.seek(offset)
                samples.append(fileobj.read(8))
        for atom in moov.findall(b'co64', True):
            fileobj.seek(atom.offset + 12)
            data = fileobj.read(atom.length - 12)
            fmt = ">%dQ" % cdata.uint_be(data[:4])
            offsets = struct.unpack(fmt, data[4:])
            for offset in offsets:
                fileobj.seek(offset)
                samples.append(fileobj.read(8))
        try:
            for atom in atoms[b"moof"].findall(b'tfhd', True):
                data = fileobj.read(atom.length - 9)
                flags = cdata.uint_be(b"\x00" + data[:3])
                if flags & 1:
                    offset = cdata.ulonglong_be(data[7:15])
                    fileobj.seek(offset)
                    samples.append(fileobj.read(8))
        except KeyError:
            pass
        fileobj.close()
        return samples

    def test_update_offsets(self):
        aa = self.__read_offsets(self.original)
        self.audio["\xa9nam"] = "wheeeeeeee"
        self.audio.save()
        bb = self.__read_offsets(self.filename)
        for a, b in zip(aa, bb):
            self.failUnlessEqual(a, b)

    def test_mime(self):
        self.failUnless("audio/mp4" in self.audio.mime)

    def test_set_init_padding_zero(self):
        if self.audio.tags is None:
            self.audio.add_tags()
        self.audio.save(padding=lambda x: 0)
        self.assertEqual(MP4(self.audio.filename)._padding, 0)

    def test_set_init_padding_large(self):
        if self.audio.tags is None:
            self.audio.add_tags()
        self.audio.save(padding=lambda x: 5000)
        self.assertEqual(MP4(self.audio.filename)._padding, 5000)

    def test_set_various_padding(self):
        if self.audio.tags is None:
            self.audio.add_tags()
        for i in [0, 1, 2, 3, 1024, 983, 5000, 0, 1]:
            self.audio.save(padding=lambda x: i)
            self.assertEqual(MP4(self.audio.filename)._padding, i)
            self.faad()


class TMP4HasTagsMixin(TMP4Mixin):
    def test_save_simple(self):
        self.audio.save()
        self.faad()

    def test_shrink(self):
        self.audio.clear()
        self.audio.save()
        audio = MP4(self.audio.filename)
        self.failIf(audio.tags)

    def test_too_short(self):
        fileobj = open(self.audio.filename, "rb")
        try:
            atoms = Atoms(fileobj)
            ilst = atoms[b"moov.udta.meta.ilst"]
            # fake a too long atom length
            ilst.children[0].length += 10000000
            self.failUnlessRaises(MP4MetadataError, MP4Tags, atoms, fileobj)
        finally:
            fileobj.close()

    def test_has_tags(self):
        self.failUnless(self.audio.tags)

    def test_not_my_file(self):
        # should raise something like "Not a MP4 file"
        self.failUnlessRaisesRegexp(
            error, "MP4", MP4, os.path.join(DATA_DIR, "empty.ogg"))

    def test_delete_remove_padding(self):
        self.audio.clear()
        self.audio.tags['foob'] = u"foo"
        self.audio.save(padding=lambda x: 0)
        filesize = os.path.getsize(self.audio.filename)
        self.audio.delete()
        self.assertTrue(os.path.getsize(self.audio.filename) < filesize)


class TMP4Datatypes(TMP4, TMP4HasTagsMixin):
    original = os.path.join(DATA_DIR, "has-tags.m4a")

    def test_has_freeform(self):
        key = "----:com.apple.iTunes:iTunNORM"
        self.failUnless(key in self.audio.tags)
        ff = self.audio.tags[key]
        self.failUnlessEqual(ff[0].dataformat, AtomDataType.UTF8)
        self.failUnlessEqual(ff[0].version, 0)

    def test_has_covr(self):
        self.failUnless('covr' in self.audio.tags)
        covr = self.audio.tags['covr']
        self.failUnlessEqual(len(covr), 2)
        self.failUnlessEqual(covr[0].imageformat, MP4Cover.FORMAT_PNG)
        self.failUnlessEqual(covr[1].imageformat, MP4Cover.FORMAT_JPEG)

    def test_pprint(self):
        text = self.audio.tags.pprint().splitlines()
        self.assertTrue(u"©ART=Test Artist" in text)

    def test_get_padding(self):
        self.assertEqual(self.audio._padding, 1634)


class TMP4CovrWithName(TMP4, TMP4Mixin):
    # http://bugs.musicbrainz.org/ticket/5894
    original = os.path.join(DATA_DIR, "covr-with-name.m4a")

    def test_has_covr(self):
        self.failUnless('covr' in self.audio.tags)
        covr = self.audio.tags['covr']
        self.failUnlessEqual(len(covr), 2)
        self.failUnlessEqual(covr[0].imageformat, MP4Cover.FORMAT_PNG)
        self.failUnlessEqual(covr[1].imageformat, MP4Cover.FORMAT_JPEG)


class TMP4HasTags64Bit(TMP4, TMP4HasTagsMixin):
    original = os.path.join(DATA_DIR, "truncated-64bit.mp4")

    def test_has_covr(self):
        pass

    def test_bitrate(self):
        self.failUnlessEqual(self.audio.info.bitrate, 128000)

    def test_length(self):
        self.failUnlessAlmostEqual(0.325, self.audio.info.length, 3)

    def faad(self):
        # This is only half a file, so FAAD segfaults. Can't test. :(
        pass


class TMP4NoTagsM4A(TMP4, TMP4Mixin):
    original = os.path.join(DATA_DIR, "no-tags.m4a")

    def test_no_tags(self):
        self.failUnless(self.audio.tags is None)

    def test_add_tags(self):
        self.audio.add_tags()
        self.failUnlessRaises(error, self.audio.add_tags)


class TMP4NoTags3G2(TMP4, TMP4Mixin):
    original = os.path.join(DATA_DIR, "no-tags.3g2")

    def test_no_tags(self):
        self.failUnless(self.audio.tags is None)

    def test_sample_rate(self):
        self.failUnlessEqual(self.audio.info.sample_rate, 22050)

    def test_bitrate(self):
        self.failUnlessEqual(self.audio.info.bitrate, 32000)

    def test_length(self):
        self.failUnlessAlmostEqual(15, self.audio.info.length, 1)


class TMP4UpdateParents64Bit(TestCase):
    original = os.path.join(DATA_DIR, "64bit.mp4")

    def setUp(self):
        self.filename = get_temp_copy(self.original)

    def test_update_parents(self):
        with open(self.filename, "rb") as fileobj:
            atoms = Atoms(fileobj)
            self.assertEqual(77, atoms.atoms[0].length)
            self.assertEqual(61, atoms.atoms[0].children[0].length)
            tags = MP4Tags(atoms, fileobj)
            tags['pgap'] = True
            tags.save(self.filename, padding=lambda x: 0)

        with open(self.filename, "rb") as fileobj:
            atoms = Atoms(fileobj)
            # original size + 'pgap' size + padding
            self.assertEqual(77 + 25 + 8, atoms.atoms[0].length)
            self.assertEqual(61 + 25 + 8, atoms.atoms[0].children[0].length)

    def tearDown(self):
        os.unlink(self.filename)


class TMP4ALAC(TestCase):
    original = os.path.join(DATA_DIR, "alac.m4a")

    def setUp(self):
        self.audio = MP4(self.original)

    def test_channels(self):
        self.failUnlessEqual(self.audio.info.channels, 2)

    def test_sample_rate(self):
        self.failUnlessEqual(self.audio.info.sample_rate, 44100)

    def test_bits_per_sample(self):
        self.failUnlessEqual(self.audio.info.bits_per_sample, 16)

    def test_length(self):
        self.failUnlessAlmostEqual(3.7, self.audio.info.length, 1)

    def test_bitrate(self):
        self.assertEqual(self.audio.info.bitrate, 2764)

    def test_kind(self):
        self.assertEqual(self.audio.info.codec, u'alac')


class TMP4Misc(TestCase):

    def test_no_audio_tracks(self):
        data = Atom.render(b"moov", Atom.render(b"udta", b""))
        fileobj = BytesIO(data)
        audio = MP4(fileobj)
        assert audio.info
        assert audio.pprint()
        info = audio.info
        assert isinstance(info.bitrate, int)
        assert isinstance(info.length, float)
        assert isinstance(info.channels, int)
        assert isinstance(info.sample_rate, int)
        assert isinstance(info.bits_per_sample, int)
        assert isinstance(info.codec, str)
        assert isinstance(info.codec_description, str)

    def test_parse_full_atom(self):
        p = parse_full_atom(b"\x01\x02\x03\x04\xff")
        self.assertEqual(p, (1, 131844, b'\xff'))

        self.assertRaises(ValueError, parse_full_atom, b"\x00\x00\x00")

    def test_sort_items(self):
        items = [
            ("\xa9nam", ["foo"]),
            ("gnre", ["fo"]),
            ("----", ["123"]),
            ("----", ["1234"]),
        ]

        sorted_items = sorted(items, key=lambda kv: _item_sort_key(*kv))
        self.assertEqual(sorted_items, items)


class TMP4Freeform(TestCase):

    def test_cmp(self):
        self.assertReallyEqual(
            MP4FreeForm(b'woooo', 142, 42), MP4FreeForm(b'woooo', 142, 42))
        self.assertReallyNotEqual(
            MP4FreeForm(b'woooo', 142, 43), MP4FreeForm(b'woooo', 142, 42))
        self.assertReallyNotEqual(
            MP4FreeForm(b'woooo', 143, 42), MP4FreeForm(b'woooo', 142, 42))
        self.assertReallyNotEqual(
            MP4FreeForm(b'wooox', 142, 42), MP4FreeForm(b'woooo', 142, 42))

    def test_cmp_bytes(self):
        self.assertReallyEqual(MP4FreeForm(b'woooo'), b"woooo")
        self.assertReallyNotEqual(MP4FreeForm(b'woooo'), b"foo")


class TMP4Cover(TestCase):

    def test_cmp(self):
        self.assertReallyEqual(
            MP4Cover(b'woooo', 142), MP4Cover(b'woooo', 142))
        self.assertReallyNotEqual(
            MP4Cover(b'woooo', 143), MP4Cover(b'woooo', 142))
        self.assertReallyNotEqual(
            MP4Cover(b'woooo', 142), MP4Cover(b'wooox', 142))

    def test_cmp_bytes(self):
        self.assertReallyEqual(MP4Cover(b'woooo'), b"woooo")
        self.assertReallyNotEqual(MP4Cover(b'woooo'), b"foo")


class TMP4AudioSampleEntry(TestCase):

    def test_alac(self):
        # an exampe where the channel count in the alac cookie is right
        # but the SampleEntry is wrong
        atom_data = (
            b'\x00\x00\x00Halac\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00\x00\x00\x00\x1f@\x00'
            b'\x00\x00\x00\x00$alac\x00\x00\x00\x00\x00\x00\x10\x00\x00\x10'
            b'(\n\x0e\x01\x00\xff\x00\x00P\x01\x00\x00\x00\x00\x00\x00\x1f@')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)
        self.assertEqual(entry.bitrate, 0)
        self.assertEqual(entry.channels, 1)
        self.assertEqual(entry.codec, "alac")
        self.assertEqual(entry.codec_description, "ALAC")
        self.assertEqual(entry.sample_rate, 8000)

    def test_alac_2(self):
        # an example where the samplerate is only correct in the cookie,
        # also contains a bitrate
        atom_data = (
            b'\x00\x00\x00Halac\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x18\x00\x00\x00\x00X\x88\x00'
            b'\x00\x00\x00\x00$alac\x00\x00\x00\x00\x00\x00\x10\x00\x00\x18'
            b'(\n\x0e\x02\x00\xff\x00\x00F/\x00%2\xd5\x00\x01X\x88')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)
        self.assertEqual(entry.bitrate, 2437845)
        self.assertEqual(entry.channels, 2)
        self.assertEqual(entry.codec, "alac")
        self.assertEqual(entry.codec_description, "ALAC")
        self.assertEqual(entry.sample_rate, 88200)

    def test_pce(self):
        atom_data = (
            b'\x00\x00\x00dmp4a\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00\x00\x00\x00\xbb\x80'
            b'\x00\x00\x00\x00\x00@esds\x00\x00\x00\x00\x03\x80\x80\x80/\x00'
            b'\x00\x00\x04\x80\x80\x80!@\x15\x00\x15\x00\x00\x03\xed\xaa\x00'
            b'\x03k\x00\x05\x80\x80\x80\x0f+\x01\x88\x02\xc4\x04\x90,\x10\x8c'
            b'\x80\x00\x00\xed@\x06\x80\x80\x80\x01\x02')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)

        self.assertEqual(entry.bitrate, 224000)
        self.assertEqual(entry.channels, 8)
        self.assertEqual(entry.codec_description, "AAC LC+SBR")
        self.assertEqual(entry.codec, "mp4a.40.2")
        self.assertEqual(entry.sample_rate, 48000)
        self.assertEqual(entry.sample_size, 16)

    def test_sbr_ps_sig_1(self):
        atom_data = (
            b"\x00\x00\x00\\mp4a\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00\x00\x00\x00\xbb\x80\x00"
            b"\x00\x00\x00\x008esds\x00\x00\x00\x00\x03\x80\x80\x80'\x00\x00"
            b"\x00\x04\x80\x80\x80\x19@\x15\x00\x03\x00\x00\x00\xe9j\x00\x00"
            b"\xda\xc0\x05\x80\x80\x80\x07\x13\x08V\xe5\x9dH\x80\x06\x80\x80"
            b"\x80\x01\x02")

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)

        self.assertEqual(entry.bitrate, 56000)
        self.assertEqual(entry.channels, 2)
        self.assertEqual(entry.codec_description, "AAC LC+SBR+PS")
        self.assertEqual(entry.codec, "mp4a.40.2")
        self.assertEqual(entry.sample_rate, 48000)
        self.assertEqual(entry.sample_size, 16)

        self.assertTrue(isinstance(entry.codec, str))
        self.assertTrue(isinstance(entry.codec_description, str))

    def test_als(self):
        atom_data = (
            b'\x00\x00\x00\x9dmp4a\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x00\x10\x00\x00\x00\x00\x07'
            b'\xd0\x00\x00\x00\x00\x00yesds\x00\x00\x00\x00\x03k\x00\x00\x00'
            b'\x04c@\x15\x10\xe7\xe6\x00W\xcbJ\x00W\xcbJ\x05T\xf8\x9e\x00\x0f'
            b'\xa0\x00ALS\x00\x00\x00\x07\xd0\x00\x00\x0c\t\x01\xff$O\xff\x00'
            b'g\xff\xfc\x80\x00\x00\x00,\x00\x00\x00\x00RIFF$$0\x00WAVEfmt '
            b'\x10\x00\x00\x00\x01\x00\x00\x02\xd0\x07\x00\x00\x00@\x1f\x00'
            b'\x00\x04\x10\x00data\x00$0\x00\xf6\xceF+\x06\x01\x02')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)

        self.assertEqual(entry.bitrate, 5753674)
        self.assertEqual(entry.channels, 512)
        self.assertEqual(entry.codec_description, "ALS")
        self.assertEqual(entry.codec, "mp4a.40.36")
        self.assertEqual(entry.sample_rate, 2000)
        self.assertEqual(entry.sample_size, 16)

    def test_ac3(self):
        atom_data = (
            b'\x00\x00\x00/ac-3\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00\x00\x00\x00V"\x00\x00'
            b'\x00\x00\x00\x0bdac3R\t\x00')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)

        self.assertEqual(entry.bitrate, 128000)
        self.assertEqual(entry.channels, 1)
        self.assertEqual(entry.codec_description, "AC-3")
        self.assertEqual(entry.codec, "ac-3")
        self.assertEqual(entry.sample_rate, 22050)
        self.assertEqual(entry.sample_size, 16)

        self.assertTrue(isinstance(entry.codec, str))
        self.assertTrue(isinstance(entry.codec_description, str))

    def test_samr(self):
        # parsing not implemented, values are wrong but at least it loads.
        # should be Mono 7.95kbps 8KHz
        atom_data = (
            b'\x00\x00\x005samr\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00\x00\x00\x00\x1f@\x00'
            b'\x00\x00\x00\x00\x11damrFFMP\x00\x81\xff\x00\x01')

        fileobj = BytesIO(atom_data)
        atom = Atom(fileobj)
        entry = AudioSampleEntry(atom, fileobj)

        self.assertEqual(entry.bitrate, 0)
        self.assertEqual(entry.channels, 2)
        self.assertEqual(entry.codec_description, "SAMR")
        self.assertEqual(entry.codec, "samr")
        self.assertEqual(entry.sample_rate, 8000)
        self.assertEqual(entry.sample_size, 16)

        self.assertTrue(isinstance(entry.codec, str))
        self.assertTrue(isinstance(entry.codec_description, str))

    def test_error(self):
        fileobj = BytesIO(b"\x00" * 20)
        atom = Atom(fileobj)
        self.assertRaises(ASEntryError, AudioSampleEntry, atom, fileobj)


def test_weird_descriptor_size():
    path = os.path.join(DATA_DIR, "ep7.m4b")
    t = MP4(path)
    assert t.info.length == pytest.approx(2.02, 2)
    assert t.info.bitrate == 125591
    assert t.info.sample_rate == 44100
    assert t.info.codec_description == "AAC LC"

    path = os.path.join(DATA_DIR, "ep9.m4b")
    t = MP4(path)
    assert t.info.length == pytest.approx(2.02, 2)
    assert t.info.bitrate == 61591
    assert t.info.sample_rate == 44100
    assert t.info.codec_description == "AAC LC"


class TMP4Chapters(TMP4):
    original = os.path.join(DATA_DIR, "nero-chapters.m4b")

    def test_has_chapters(self):
        self.failUnless(hasattr(self.audio, 'chapters'))
        chapters = self.audio.chapters
        self.failUnlessEqual(len(chapters), 112)
        for i, c in enumerate(chapters):
            self.failUnlessEqual(c.title, str(i + 1).zfill(3))

    def test_falls_back_to_chpl_when_track_unusable(self):
        # nero-chapters.m4b references a text chapter track, but this test
        # file only carries metadata: the track's sample table points at
        # data which isn't in the file. The 'chpl' box must still work.
        from mutagen.mp4 import MP4Chapters
        with open(self.filename, "rb") as fileobj:
            atoms = Atoms(fileobj)
            chapters = MP4Chapters(atoms, fileobj)
        self.failUnlessEqual(len(chapters), 112)
        self.failUnlessAlmostEqual(chapters[1].start, 17.507, 3)
        self.failUnlessAlmostEqual(chapters[-1].start, 168998.359, 3)
        for i, c in enumerate(chapters):
            self.failUnlessEqual(c.title, str(i + 1).zfill(3))

    def test_chpl_named_free_still_opens(self):
        # Renaming the 'chpl' box away leaves no readable chapter source;
        # the file must still open and simply report no chapters.
        with open(self.filename, "rb") as fileobj:
            data = bytearray(fileobj.read())
        index = data.index(b"chpl") - 4
        data[index + 4:index + 8] = b"free"
        filename = _write_temp(bytes(data))
        try:
            audio = MP4(filename)
        finally:
            os.unlink(filename)
        self.failUnlessEqual(len(audio.chapters), 0)

    def test_chapters_survive_save(self):
        before = [(c.start, c.title) for c in self.audio.chapters]
        self.audio["\xa9nam"] = ["New Title"]
        self.audio.save()
        after = [(c.start, c.title) for c in MP4(self.filename).chapters]
        self.failUnlessEqual(before, after)

    def test_starts_are_monotonic(self):
        starts = [c.start for c in self.audio.chapters]
        self.failUnlessEqual(starts, sorted(starts))

    def test_pprint(self):
        text = self.audio.pprint()
        self.failUnless("chapters=" in text)
        self.failUnless("112" in text)


def _mp4_box(name, payload):
    return struct.pack(">I4s", len(payload) + 8, name) + payload


def _mp4_full_box(name, version, flags, payload):
    return _mp4_box(name, struct.pack(">B3s", version,
                                     flags.to_bytes(3, "big")) + payload)


def _qt_text_sample(text, encoding=b"\x00\x00\x01\x00"):
    """Render a QuickTime text sample with a trailing 'encd' box."""

    if isinstance(text, str):
        text = text.encode("utf-8")
    body = struct.pack(">H", len(text)) + text
    if encoding is not None:
        body += _mp4_box(b"encd", encoding)
    return body


def _build_chapter_mp4(samples, *, chunk_layout=None, timescale=1000,
                       deltas=None, use_co64=False, chpl_entries=None,
                       tref_ids=(2,), elst_entries=None, uniform=None,
                       incomplete_stts=False, missing_stco=False):
    """Build a minimal MP4 with one audio track and a chapter text track.

    The audio track has no sample table; only the chapter track is
    complete enough to be read by MP4Chapters.
    """

    sizes = [len(s) for s in samples]
    if chunk_layout is None:
        chunk_layout = [[i] for i in range(len(samples))]
    if deltas is None:
        deltas = [timescale] * len(samples)

    stts_runs = []
    for delta in deltas:
        if stts_runs and stts_runs[-1][1] == delta:
            stts_runs[-1][0] += 1
        else:
            stts_runs.append([1, delta])
    if incomplete_stts and len(stts_runs) > 1:
        stts_runs = stts_runs[:-1]

    stsc_runs = []
    for index, chunk in enumerate(chunk_layout, 1):
        count = len(chunk)
        if not stsc_runs or stsc_runs[-1][1] != count:
            stsc_runs.append([index, count, 1])

    def stts(data):
        payload = struct.pack(">I", len(data))
        for count, delta in data:
            payload += struct.pack(">II", count, delta)
        return _mp4_full_box(b"stts", 0, 0, payload)

    def stsc(data):
        payload = struct.pack(">I", len(data))
        for first, count, desc in data:
            payload += struct.pack(">III", first, count, desc)
        return _mp4_full_box(b"stsc", 0, 0, payload)

    def stsz(data):
        if uniform is not None and len(set(data)) == 1:
            payload = struct.pack(">II", uniform, len(data))
        else:
            payload = struct.pack(">II", 0, len(data))
            payload += b"".join(struct.pack(">I", size) for size in data)
        return _mp4_full_box(b"stsz", 0, 0, payload)

    def offsets(data):
        payload = struct.pack(">I", len(data))
        if use_co64:
            payload += b"".join(struct.pack(">Q", o) for o in data)
            return _mp4_full_box(b"co64", 0, 0, payload)
        payload += b"".join(struct.pack(">I", o) for o in data)
        return _mp4_full_box(b"stco", 0, 0, payload)

    def elst(entries):
        payload = struct.pack(">I", len(entries))
        for seg, media_time, rate in entries:
            payload += struct.pack(">IiI", seg, media_time, rate)
        return _mp4_full_box(b"elst", 0, 0, payload)

    def stsd(entry_name):
        # 28 byte sample entry fields plus an empty trailing box so that
        # AudioSampleEntry can parse it.
        entry = struct.pack(">I4s", 44, entry_name) + b"\x00" * 28 + \
            struct.pack(">I4s", 8, b"btrt")
        return _mp4_full_box(b"stsd", 0, 0, struct.pack(">I", 1) + entry)

    def tkhd(track_id, duration):
        payload = struct.pack(">IIIII", 0, 0, track_id, 0, duration)
        payload += b"\x00" * 60
        return _mp4_full_box(b"tkhd", 0, 7, payload)

    def mdhd(scale, duration):
        payload = struct.pack(">IIII", 0, 0, scale, duration) + b"\x00" * 4
        return _mp4_full_box(b"mdhd", 0, 0, payload)

    def hdlr(handler):
        return _mp4_full_box(b"hdlr", 0, 0, b"\x00" * 4 + handler +
                             b"\x00" * 12 + b"Handler\x00")

    def trak(track_id, handler, stbl_boxes, duration, edts=None,
             refs=None):
        children = [tkhd(track_id, duration)]
        if edts is not None:
            children.append(_mp4_box(b"edts", elst(edts)))
        if refs is not None:
            children.append(_mp4_box(b"tref", _mp4_box(
                b"chap", b"".join(struct.pack(">I", r) for r in refs))))
        children.append(_mp4_box(b"mdia", b"".join([
            mdhd(timescale if handler == b"text" else 44100, duration),
            hdlr(handler),
            _mp4_box(b"minf", _mp4_box(b"stbl", b"".join(stbl_boxes))),
        ])))
        return _mp4_box(b"trak", b"".join(children))

    total_duration = sum(deltas)
    text_boxes = [stsd(b"text"), stsz(sizes), stts(stts_runs),
                  stsc(stsc_runs)]
    if not missing_stco:
        text_boxes.append(offsets([0] * len(chunk_layout)))
    text_trak = trak(2, b"text", text_boxes, total_duration,
                     edts=elst_entries)
    audio_trak = trak(1, b"soun", [stsd(b"mp4a")], total_duration,
                      refs=tref_ids)

    udta = b""
    if chpl_entries is not None:
        payload = struct.pack(">B3sI", 1, b"\x00\x00\x00", 0)
        payload += struct.pack(">B", len(chpl_entries))
        for start, title in chpl_entries:
            payload += struct.pack(">Q", int(round(start * 10000000)))
            raw = title.encode("utf-8")
            payload += struct.pack(">B", len(raw)) + raw
        udta = _mp4_box(b"udta", _mp4_box(b"chpl", payload))

    mvhd = _mp4_full_box(b"mvhd", 0, 0, struct.pack(
        ">IIII", 0, 0, 1000, total_duration) + b"\x00" * 80)

    ftyp = _mp4_box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    mdat_payload = b"".join(samples)

    def make_moov(mdat_offset):
        children = [mvhd, audio_trak, text_trak, udta]
        moov = _mp4_box(b"moov", b"".join(children))
        # Repoint the text chunk offsets at the sample data in mdat.
        base = mdat_offset + 8
        chunk_offsets = []
        index = 0
        for chunk in chunk_layout:
            chunk_offsets.append(base + sum(sizes[:index]))
            index += len(chunk)
        old = offsets([0] * len(chunk_layout))
        new = offsets(chunk_offsets)
        assert len(old) == len(new)
        return moov.replace(old, new, 1)

    # The size of the moov with zero offsets is stable, so it can be laid
    # out before mdat.
    placeholder = make_moov(0)
    mdat_offset = len(ftyp) + len(placeholder)
    moov = make_moov(mdat_offset)
    return ftyp + moov + _mp4_box(b"mdat", mdat_payload)


def _write_temp(data, suffix=".m4b"):
    fd, filename = mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fileobj:
        fileobj.write(data)
    return filename


class TMP4QuickTimeChapters(TestCase):

    def load(self, data):
        filename = _write_temp(data)
        try:
            return MP4(filename)
        finally:
            os.unlink(filename)

    def test_text_track_only(self):
        audio = self.load(_build_chapter_mp4([
            _qt_text_sample("Intro"),
            _qt_text_sample("Part Two")]))
        chapters = audio.chapters
        self.failUnlessEqual(len(chapters), 2)
        self.failUnlessEqual(chapters[0].title, "Intro")
        self.failUnlessEqual(chapters[1].title, "Part Two")
        self.failUnlessAlmostEqual(chapters[0].start, 0.0, 6)
        self.failUnlessAlmostEqual(chapters[1].start, 1.0, 6)

    def test_both_sources_text_track_wins(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("Track One"), _qt_text_sample("Track Two")],
            chpl_entries=[(0, "Nero One"), (5, "Nero Two")]))
        titles = [c.title for c in audio.chapters]
        self.failUnlessEqual(titles, ["Track One", "Track Two"])

    def test_chpl_only(self):
        audio = self.load(_build_chapter_mp4(
            [], tref_ids=(), chpl_entries=[(0, "A"), (3.5, "B")]))
        chapters = audio.chapters
        self.failUnlessEqual([c.title for c in chapters], ["A", "B"])
        self.failUnlessAlmostEqual(chapters[1].start, 3.5, 6)

    def test_multi_sample_chunks_and_co64(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("A"), _qt_text_sample("BB"),
             _qt_text_sample("CCC"), _qt_text_sample("DDDD"),
             _qt_text_sample("EEEEE")],
            chunk_layout=[[0, 1, 2], [3, 4]], use_co64=True))
        chapters = audio.chapters
        self.failUnlessEqual(
            [c.title for c in chapters], ["A", "BB", "CCC", "DDDD", "EEEEE"])
        self.failUnlessEqual(
            [round(c.start, 6) for c in chapters], [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_timescale_scaled(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("A"), _qt_text_sample("B"), _qt_text_sample("C")],
            timescale=44100, deltas=[44100, 22050, 44100]))
        starts = [c.start for c in audio.chapters]
        self.failUnlessAlmostEqual(starts[0], 0.0, 6)
        self.failUnlessAlmostEqual(starts[1], 1.0, 6)
        self.failUnlessAlmostEqual(starts[2], 1.5, 6)

    def test_uniform_sample_size(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("AAA"), _qt_text_sample("BBB")], uniform=16))
        self.failUnlessEqual([c.title for c in audio.chapters],
                             ["AAA", "BBB"])

    def test_utf16_bom(self):
        title = u"Café 测试"
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample(title.encode("utf-16"))]))
        self.failUnlessEqual(audio.chapters[0].title, title)

    def test_declared_utf16be(self):
        title = u"测试"
        raw = struct.pack(">H", len(title.encode("utf-16-be"))) + \
            title.encode("utf-16-be") + _mp4_box(
                b"encd", struct.pack(">I", 0x10000100))
        audio = self.load(_build_chapter_mp4([raw]))
        self.failUnlessEqual(audio.chapters[0].title, title)

    def test_empty_edit_shifts_starts(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("A"), _qt_text_sample("B")],
            elst_entries=[(3000, -1, 0x10000)]))
        starts = [c.start for c in audio.chapters]
        self.failUnlessAlmostEqual(starts[0], 3.0, 6)
        self.failUnlessAlmostEqual(starts[1], 4.0, 6)

    def test_broken_reference_falls_back_to_chpl(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("Track")], tref_ids=(99,),
            chpl_entries=[(0, "Fallback")]))
        self.failUnlessEqual([c.title for c in audio.chapters], ["Fallback"])

    def test_broken_reference_no_fallback(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("Track")], tref_ids=(99,)))
        self.failUnlessEqual(len(audio.chapters), 0)

    def test_incomplete_stts_falls_back_to_chpl(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("One"), _qt_text_sample("Two")],
            deltas=[1000, 2000], incomplete_stts=True,
            chpl_entries=[(0, "Fallback A"), (9, "Fallback B")]))
        self.failUnlessEqual([c.title for c in audio.chapters],
                             ["Fallback A", "Fallback B"])

    def test_missing_chunk_offsets(self):
        audio = self.load(_build_chapter_mp4(
            [_qt_text_sample("One")], missing_stco=True))
        self.failUnlessEqual(len(audio.chapters), 0)

    def test_truncated_tref_does_not_break_opening(self):
        data = _build_chapter_mp4([_qt_text_sample("One")])
        # Corrupt the 'chap' box size so the reference can't be parsed.
        index = data.index(b"chap")
        broken = data[:index - 4] + b"\xff\xff\xff\xff" + data[index:]
        filename = _write_temp(broken)
        try:
            audio = MP4(filename)
        finally:
            os.unlink(filename)
        self.failUnless(not audio.chapters)

    def test_no_chapters(self):
        audio = self.load(_build_chapter_mp4([], tref_ids=()))
        self.failUnlessEqual(audio.chapters, None)


def call_faad(*args):
    with open(os.devnull, 'wb') as null:
        return subprocess.call(
            ["faad"] + list(args),
            stdout=null, stderr=subprocess.STDOUT)

have_faad = True
try:
    call_faad()
except OSError:
    have_faad = False
    print("WARNING: Skipping FAAD reference tests.")
