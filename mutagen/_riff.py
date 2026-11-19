# Copyright (C) 2017  Borewit
# Copyright (C) 2019-2020  Philipp Wolfer
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

"""Resource Interchange File Format (RIFF).

This also covers the RF64 and BW64 variants of RIFF which are used for
files that can exceed the 4 GiB limit of the 32-bit RIFF size fields.
"""

import struct
from struct import pack

from mutagen._iff import (
    IffChunk,
    IffContainerChunkMixin,
    IffFile,
    InvalidChunk,
)

#: Root chunk IDs of the 64 bit RIFF variants (RF64 / BW64)
RF64_IDS = (u'RF64', u'BW64')

#: Placeholder used in 32-bit size fields of RF64/BW64 files. The real
#: size is stored in the mandatory 'ds64' chunk in this case.
DS64_PLACEHOLDER_SIZE = 0xFFFFFFFF

#: Placeholder used in 64-bit size fields of the ds64 chunk.
DS64_PLACEHOLDER_SIZE_64 = 0xFFFFFFFFFFFFFFFF


class RiffChunk(IffChunk):
    """Generic RIFF chunk"""

    @classmethod
    def parse_header(cls, header):
        return struct.unpack('<4sI', header)

    @classmethod
    def get_class(cls, id):
        if id in (u'LIST', u'RIFF'):
            return RiffListChunk
        elif id in RF64_IDS:
            return Riff64RootChunk
        else:
            return cls

    def write_new_header(self, id_, size):
        self._fileobj.write(pack('<4sI', id_, size))

    def write_size(self):
        size = self.data_size
        if getattr(self, 'rf64_placeholder', False):
            # The real size is stored in the ds64 chunk. Keep the
            # placeholder in the chunk header so the file stays valid.
            size = DS64_PLACEHOLDER_SIZE
        self._fileobj.write(pack('<I', size))


class RiffListChunk(RiffChunk, IffContainerChunkMixin):
    """A RIFF chunk containing other chunks.
    This is either a 'LIST' or 'RIFF'
    """

    _container_ids = (u'RIFF', u'LIST')

    def parse_next_subchunk(self):
        return RiffChunk.parse(self._fileobj, self)

    def __init__(self, fileobj, id, data_size, parent_chunk):
        if id not in self._container_ids:
            raise InvalidChunk('Expected RIFF or LIST chunk, got %s' % id)

        RiffChunk.__init__(self, fileobj, id, data_size, parent_chunk)
        self.init_container()


class Riff64RootChunk(RiffListChunk):
    """Root chunk of a RF64 or BW64 file.

    RF64/BW64 files store the sizes which don't fit into the 32-bit size
    fields as 0xFFFFFFFF placeholders. The real 64-bit sizes live in a
    mandatory 'ds64' chunk which must be the first chunk of the file.
    """

    _container_ids = RF64_IDS

    def __init__(self, fileobj, id, data_size, parent_chunk):
        RiffListChunk.__init__(self, fileobj, id, data_size, parent_chunk)

        self._load_ds64()
        # The size in the root chunk header is a placeholder, the real
        # size is stored in the ds64 chunk.
        self.data_size = self._ds64_riff_size
        self._calculate_size()

    def _calculate_size(self):
        # Don't assert on even sizes here, the sizes stored in ds64
        # chunks are not necessarily even in broken files.
        self.size = self.HEADER_SIZE + self.data_size + self.padding()

    def _file_size(self):
        fileobj = self._fileobj
        fileobj.seek(0, 2)
        return fileobj.tell()

    def _load_ds64(self):
        fileobj = self._fileobj
        # The ds64 chunk has to be the first chunk, directly after the
        # 'WAVE' container name.
        fileobj.seek(self.data_offset + 4)
        ds64 = RiffChunk.parse(fileobj, self)
        if ds64.id != u'ds64':
            raise InvalidChunk("RF64/BW64 file is missing the ds64 chunk")

        data = ds64.read()
        if len(data) < 28:
            raise InvalidChunk("Invalid ds64 chunk size")

        riff_size, data_size, sample_count, table_length = \
            struct.unpack('<QQQI', data[:28])
        if len(data) < 28 + 12 * table_length:
            raise InvalidChunk("Invalid ds64 chunk table")

        if riff_size in (0, DS64_PLACEHOLDER_SIZE_64):
            # Some writers leave the RIFF size unknown, fall back to
            # the actual file size in that case.
            riff_size = max(0, self._file_size() - self.HEADER_SIZE)
        else:
            # Never trust a size which reaches beyond the actual file,
            # otherwise parsing would run past the end of the file.
            riff_size = min(riff_size,
                            max(0, self._file_size() - self.HEADER_SIZE))

        # The data chunk size is always stored in the ds64 chunk.
        sizes = {}
        if data_size != DS64_PLACEHOLDER_SIZE_64:
            sizes[u'data'] = data_size
        table_ids = []
        for i in range(table_length):
            entry = data[28 + 12 * i:40 + 12 * i]
            id_, size = struct.unpack('<4sQ', entry)
            try:
                id_ = id_.decode('ascii').rstrip()
            except UnicodeDecodeError as e:
                raise InvalidChunk(e)
            if size != DS64_PLACEHOLDER_SIZE_64:
                sizes[id_] = size
            table_ids.append(id_)

        self.ds64_chunk = ds64
        self._ds64_riff_size = riff_size
        self._ds64_data_size = data_size
        self._ds64_sample_count = sample_count
        self._ds64_table_ids = table_ids
        self._ds64_sizes = sizes
        # Current sizes of the chunks as known from the ds64 chunk and
        # kept in sync while writing.
        self._chunk_sizes = dict(sizes)

    def parse_next_subchunk(self):
        chunk = RiffChunk.parse(self._fileobj, self)
        if chunk.data_size == DS64_PLACEHOLDER_SIZE:
            # The real size is stored in the ds64 chunk.
            chunk.rf64_placeholder = True
            size = self._ds64_sizes.get(chunk.id)
            if size is None and chunk.id == u'data':
                size = self._ds64_data_size
            if size is not None and size != DS64_PLACEHOLDER_SIZE_64:
                chunk.data_size = size
            else:
                # Unknown size, assume the chunk spans the rest of the
                # file so that parsing can stop cleanly.
                chunk.data_size = max(
                    0, self._file_size() - chunk.data_offset)
            chunk._calculate_size()
        elif chunk.id == u'data' and u'data' in self._ds64_sizes:
            # The ds64 chunk is authoritative for the data chunk size.
            # This is also what the stream duration is calculated from.
            chunk.data_size = self._ds64_sizes[u'data']
            chunk._calculate_size()
        # Remember the size of the chunk in case it couldn't be read from
        # the ds64 chunk but has to be written back on save.
        self._chunk_sizes.setdefault(chunk.id, chunk.data_size)
        return chunk

    def sync_ds64(self):
        """Update the real sizes stored in the ds64 chunk.

        This keeps the ds64 chunk in sync with the actual chunk sizes
        after chunks have been added, resized or removed.
        """
        ds64 = self.ds64_chunk
        data = bytearray(ds64.read())
        struct.pack_into('<Q', data, 0, self.data_size)
        data_size = self._chunk_sizes.get(u'data', self._ds64_data_size)
        struct.pack_into('<Q', data, 8, data_size)
        for index, id_ in enumerate(self._ds64_table_ids):
            size = self._chunk_sizes.get(id_)
            if size is not None:
                struct.pack_into('<Q', data, 28 + 12 * index + 4, size)
        self._fileobj.seek(ds64.data_offset)
        self._fileobj.write(data)
        self._ds64_data_size = data_size

    def write_size(self):
        # The root chunk size field of RF64/BW64 files is always a
        # placeholder, the size is stored in the ds64 chunk.
        self._fileobj.write(pack('<I', DS64_PLACEHOLDER_SIZE))

    def _update_size(self, size_diff, changed_subchunk=None):
        if changed_subchunk is not None:
            self._chunk_sizes[changed_subchunk.id] = \
                changed_subchunk.data_size
        super()._update_size(size_diff, changed_subchunk)
        self.sync_ds64()

    def _remove_subchunk(self, chunk):
        # The base implementation updates the size of this chunk before
        # removing it from the chunk list, so sync once more afterwards.
        super()._remove_subchunk(chunk)
        if chunk.id in self._chunk_sizes:
            self._chunk_sizes[chunk.id] = 0
        self.sync_ds64()


class RiffFile(IffFile):
    """Representation of a RIFF file"""

    def __init__(self, fileobj):
        super().__init__(RiffChunk, fileobj)

        if self.root.id not in (u'RIFF',) + RF64_IDS:
            raise InvalidChunk(
                "Root chunk must be a RIFF, RF64 or BW64 chunk, got %s"
                % self.root.id)

        self.file_type = self.root.name
