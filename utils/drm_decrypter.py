import struct
from typing import Optional, Union

from Crypto.Cipher import AES
from collections import namedtuple
import array

try:
    # OpenSSL-backed CTR is materially faster for the multi-sample fMP4
    # segments used by the MPD->HLS compatibility path.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - exercised only in minimal installs
    Cipher = algorithms = modes = None

CENCSampleAuxiliaryDataFormat = namedtuple("CENCSampleAuxiliaryDataFormat", ["is_encrypted", "iv", "sub_samples"])


class MP4Atom:
    """
    Represents an MP4 atom, which is a basic unit of data in an MP4 file.
    Each atom contains a header (size and type) and data.
    """

    __slots__ = ("atom_type", "size", "data")

    def __init__(self, atom_type: bytes, size: int, data: Union[memoryview, bytearray]):
        """
        Initializes an MP4Atom instance.

        Args:
            atom_type (bytes): The type of the atom.
            size (int): The size of the atom.
            data (Union[memoryview, bytearray]): The data contained in the atom.
        """
        self.atom_type = atom_type
        self.size = size
        self.data = data

    def __repr__(self):
        return f"<MP4Atom type={self.atom_type}, size={self.size}>"

    def pack(self):
        """
        Packs the atom into binary data.

        Returns:
            bytes: Packed binary data with size, type, and data.
        """
        return struct.pack(">I", self.size) + self.atom_type + self.data


class MP4Parser:
    """
    Parses MP4 data to extract atoms and their structure.
    """

    def __init__(self, data: memoryview):
        """
        Initializes an MP4Parser instance.

        Args:
            data (memoryview): The binary data of the MP4 file.
        """
        self.data = data
        self.position = 0

    def read_atom(self) -> Optional[MP4Atom]:
        """
        Reads the next atom from the data.

        Returns:
            Optional[MP4Atom]: MP4Atom object or None if no more atoms are available.
        """
        pos = self.position
        if pos + 8 > len(self.data):
            return None

        size, atom_type = struct.unpack_from(">I4s", self.data, pos)
        pos += 8

        if size == 1:
            if pos + 8 > len(self.data):
                return None
            size = struct.unpack_from(">Q", self.data, pos)[0]
            pos += 8

        if size < 8 or pos + size - 8 > len(self.data):
            return None

        atom_data = self.data[pos : pos + size - 8]
        self.position = pos + size - 8
        return MP4Atom(atom_type, size, atom_data)

    def list_atoms(self) -> list[MP4Atom]:
        """
        Lists all atoms in the data.

        Returns:
            list[MP4Atom]: List of MP4Atom objects.
        """
        atoms = []
        original_position = self.position
        self.position = 0
        while self.position + 8 <= len(self.data):
            atom = self.read_atom()
            if not atom:
                break
            atoms.append(atom)
        self.position = original_position
        return atoms

    def _read_atom_at(self, pos: int, end: int) -> Optional[MP4Atom]:
        if pos + 8 > end:
            return None

        size, atom_type = struct.unpack_from(">I4s", self.data, pos)
        pos += 8

        if size == 1:
            if pos + 8 > end:
                return None
            size = struct.unpack_from(">Q", self.data, pos)[0]
            pos += 8

        if size < 8 or pos + size - 8 > end:
            return None

        atom_data = self.data[pos : pos + size - 8]
        return MP4Atom(atom_type, size, atom_data)




class MP4Decrypter:
    """
    Class to handle the decryption of CENC encrypted MP4 segments.

    Attributes:
        key_map (dict[bytes, bytes]): Mapping of track IDs to decryption keys.
        current_key (Optional[bytes]): Current decryption key.
        trun_sample_sizes (array.array): Array of sample sizes from the 'trun' box.
        current_sample_info (list): List of sample information from the 'senc' box.
        encryption_overhead (int): Total size of encryption-related boxes.
    """

    def __init__(self, key_map: dict[bytes, bytes]):
        """
        Initializes the MP4Decrypter with a key map.

        Args:
            key_map (dict[bytes, bytes]): Mapping of track IDs to decryption keys.
        """
        self.key_map = key_map
        self.current_key = None
        self.trun_sample_sizes = array.array("I")
        self.current_sample_info = []
        self.encryption_overhead = 0
        # Bytes removed from the current moof before mdat.  The trun data
        # offset and any following sidx reference must account for them.
        self._moof_removed_overhead = 0
        self.track_kid_map = {}  # track_id -> KID (bytes) from tenc box
        self.track_iv_size_map = {}  # track_id -> per-sample IV size (0, 8, 16) from tenc box
        self.track_constant_iv_map = {}  # track_id -> constant IV (bytes) from tenc box
        self.track_default_sample_size_map = {}  # track_id -> default_sample_size from mvex/trex
        self._last_extracted_kid = None  # temp storage during moov processing
        self._last_extracted_iv_size = None
        self._last_extracted_constant_iv = b""
        self._default_sample_size = 0  # from tfhd/trex, used when trun lacks sample-size-present

    def decrypt_segment(self, combined_segment: bytes, init_length: int = 0) -> bytes:
        """
        Decrypts a combined MP4 segment.

        Args:
            combined_segment (bytes): Combined initialization and media segment.
            init_length (int): Length of the initialization segment. If > 0, all atoms
                               belonging to the initialization segment are skipped in the output.

        Returns:
            bytes: Decrypted segment content.
        """
        data = memoryview(combined_segment)
        parser = MP4Parser(data)
        atoms = parser.list_atoms()

        processed_by_index = {}
        for idx, atom in enumerate(atoms):
            if atom.atom_type == b"moov":
                processed_by_index[idx] = self._process_moov(atom)

        pending_sidx = []
        for idx, atom in enumerate(atoms):
            if atom.atom_type == b"sidx":
                pending_sidx.append((idx, atom))
            elif atom.atom_type == b"moof":
                processed_by_index[idx] = self._process_moof(atom)
                for sidx_idx, sidx_atom in pending_sidx:
                    processed_by_index[sidx_idx] = self._process_sidx(sidx_atom)
                pending_sidx.clear()
            elif atom.atom_type == b"mdat":
                processed_by_index[idx] = self._decrypt_mdat(atom)

        for sidx_idx, sidx_atom in pending_sidx:
            processed_by_index[sidx_idx] = self._process_sidx(sidx_atom)

        result = bytearray()
        current_offset = 0
        for idx, atom in enumerate(atoms):
            is_init_atom = current_offset < init_length
            current_offset += atom.size
            if init_length > 0 and is_init_atom:
                continue
            processed_atom = processed_by_index.get(idx, atom)
            result.extend(processed_atom.pack())

        return bytes(result)

    def _process_atom(self, atom_type: bytes, atom: MP4Atom) -> MP4Atom:
        """
        Processes an MP4 atom based on its type.

        Args:
            atom_type (bytes): Type of the atom.
            atom (MP4Atom): The atom to process.

        Returns:
            MP4Atom: Processed atom.
        """
        if atom_type == b"moov":
            return self._process_moov(atom)
        elif atom_type == b"moof":
            return self._process_moof(atom)
        elif atom_type == b"sidx":
            return self._process_sidx(atom)
        elif atom_type == b"mdat":
            return self._decrypt_mdat(atom)
        else:
            return atom

    def _process_moov(self, moov: MP4Atom) -> MP4Atom:
        """
        Processes the 'moov' (Movie) atom, which contains metadata about the entire presentation.
        This includes information about tracks, media data, and other movie-level metadata.

        Args:
            moov (MP4Atom): The 'moov' atom to process.

        Returns:
            MP4Atom: Processed 'moov' atom with updated track information.
        """
        parser = MP4Parser(moov.data)
        new_moov_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"trak":
                new_trak = self._process_trak(atom)
                new_moov_data.extend(new_trak.pack())
            elif atom.atom_type == b"mvex":
                mvex_parser = MP4Parser(atom.data)
                for mvex_atom in iter(mvex_parser.read_atom, None):
                    if mvex_atom.atom_type == b"trex" and len(mvex_atom.data) >= 20:
                        trex_track_id = struct.unpack_from(">I", mvex_atom.data, 4)[0]
                        trex_default_size = struct.unpack_from(">I", mvex_atom.data, 16)[0]
                        if trex_default_size:
                            self.track_default_sample_size_map[trex_track_id] = trex_default_size
                new_moov_data.extend(atom.pack())
            elif atom.atom_type != b"pssh":
                # Skip PSSH boxes as they are not needed in the decrypted output
                new_moov_data.extend(atom.pack())

        return MP4Atom(b"moov", len(new_moov_data) + 8, new_moov_data)

    def _process_moof(self, moof: MP4Atom) -> MP4Atom:
        """
        Processes the 'moov' (Movie) atom, which contains metadata about the entire presentation.
        This includes information about tracks, media data, and other movie-level metadata.

        Args:
            moov (MP4Atom): The 'moov' atom to process.

        Returns:
            MP4Atom: Processed 'moov' atom with updated track information.
        """
        parser = MP4Parser(moof.data)
        new_moof_data = bytearray()
        # Some CMAF/DASH origins repeat Widevine/PlayReady PSSH boxes in
        # every media moof, not only in the initialization segment.  Once
        # ClearKey decryption has completed, those boxes must not reach the
        # HLS/fMP4 player or it may open an unsupported DRM session again.
        self._moof_removed_overhead = sum(
            atom.size for atom in parser.list_atoms() if atom.atom_type == b"pssh"
        )

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"pssh":
                continue
            if atom.atom_type == b"traf":
                new_traf = self._process_traf(atom)
                new_moof_data.extend(new_traf.pack())
            else:
                new_moof_data.extend(atom.pack())

        return MP4Atom(b"moof", len(new_moof_data) + 8, new_moof_data)

    @staticmethod
    def _parse_tfhd_default_sample_size(tfhd: MP4Atom) -> int:
        if len(tfhd.data) < 8:
            return 0
        flags = struct.unpack_from(">I", tfhd.data, 0)[0] & 0xFFFFFF
        offset = 8
        if flags & 0x000001:
            offset += 8
        if flags & 0x000002:
            offset += 4
        if flags & 0x000008:
            offset += 4
        if flags & 0x000010:
            if offset + 4 <= len(tfhd.data):
                return struct.unpack_from(">I", tfhd.data, offset)[0]
        return 0

    def _process_traf(self, traf: MP4Atom) -> MP4Atom:
        parser = MP4Parser(traf.data)
        new_traf_data = bytearray()
        sample_count = 0
        sample_info = []

        atoms = parser.list_atoms()
        tfhd = next((a for a in atoms if a.atom_type == b"tfhd"), None)
        tfhd_track_id = (
            struct.unpack_from(">I", tfhd.data, 4)[0]
            if tfhd is not None and len(tfhd.data) >= 8
            else None
        )
        tfhd_sample_size = self._parse_tfhd_default_sample_size(tfhd) if tfhd is not None else 0
        self._default_sample_size = (
            tfhd_sample_size
            or (self.track_default_sample_size_map.get(tfhd_track_id, 0) if tfhd_track_id is not None else 0)
        )

        self.encryption_overhead = self._moof_removed_overhead + sum(
            a.size for a in atoms if a.atom_type in {b"senc", b"saiz", b"saio", b"sbgp", b"sgpd"}
        )

        for atom in atoms:
            if atom.atom_type == b"tfhd":
                new_traf_data.extend(atom.pack())
            elif atom.atom_type == b"trun":
                sample_count = self._process_trun(atom)
                new_trun = self._modify_trun(atom)
                new_traf_data.extend(new_trun.pack())
            elif atom.atom_type == b"senc":
                track_iv_size = self.track_iv_size_map.get(tfhd_track_id) if tfhd_track_id is not None else None
                track_const_iv = self.track_constant_iv_map.get(tfhd_track_id, b"") if tfhd_track_id is not None else b""
                sample_info = self._parse_senc(
                    atom,
                    sample_count,
                    iv_size=track_iv_size,
                    constant_iv=track_const_iv,
                )
            elif atom.atom_type not in {b"saiz", b"saio", b"sbgp", b"sgpd"}:
                new_traf_data.extend(atom.pack())

        if tfhd_track_id is not None:
            self.current_key = self._get_key_for_track(tfhd_track_id)
            self.current_sample_info = sample_info

        return MP4Atom(b"traf", len(new_traf_data) + 8, new_traf_data)

    def _decrypt_mdat(self, mdat: MP4Atom) -> MP4Atom:
        """
        Decrypts the 'mdat' (Media Data) atom, which contains the actual media data (audio, video, etc.).
        The decryption is performed using the current decryption key and sample information.

        Args:
            mdat (MP4Atom): The 'mdat' atom to decrypt.

        Returns:
            MP4Atom: Decrypted 'mdat' atom with decrypted media data.
        """
        if not self.current_key or not self.current_sample_info:
            return mdat  # Return original mdat if we don't have decryption info

        decrypted_samples = bytearray()
        mdat_data = mdat.data
        position = 0

        for i, info in enumerate(self.current_sample_info):
            if position >= len(mdat_data):
                break  # No more data to process

            sample_size = self.trun_sample_sizes[i] if i < len(self.trun_sample_sizes) else len(mdat_data) - position
            sample = mdat_data[position : position + sample_size]
            position += sample_size
            decrypted_sample = self._process_sample(sample, info, self.current_key)
            decrypted_samples.extend(decrypted_sample)

        return MP4Atom(b"mdat", len(decrypted_samples) + 8, decrypted_samples)

    @staticmethod
    def _parse_senc_entries(
        data: memoryview,
        position: int,
        sample_count: int,
        flags: int,
        iv_size: int,
        constant_iv: bytes = b"",
    ) -> tuple[list[CENCSampleAuxiliaryDataFormat], int, bool]:
        sample_info = []
        for _ in range(sample_count):
            if iv_size == 0:
                iv = constant_iv
            else:
                if position + iv_size > len(data):
                    return sample_info, position, False
                iv = data[position : position + iv_size].tobytes()
                position += iv_size

            sub_samples = []
            if flags & 0x000002:
                if position + 2 > len(data):
                    return sample_info, position, False
                subsample_count = struct.unpack_from(">H", data, position)[0]
                position += 2
                if position + subsample_count * 6 > len(data):
                    return sample_info, position, False
                for _ in range(subsample_count):
                    clear_bytes, encrypted_bytes = struct.unpack_from(">HI", data, position)
                    position += 6
                    sub_samples.append((clear_bytes, encrypted_bytes))

            sample_info.append(CENCSampleAuxiliaryDataFormat(True, iv, sub_samples))
        return sample_info, position, position == len(data) and len(sample_info) == sample_count

    def _parse_senc(
        self,
        senc: MP4Atom,
        sample_count: int,
        iv_size: Optional[int] = None,
        constant_iv: bytes = b"",
    ) -> list[CENCSampleAuxiliaryDataFormat]:
        """
        Parses the 'senc' (Sample Encryption) atom, which contains encryption information for samples.
        This includes initialization vectors (IVs) and sub-sample encryption data.
        """
        data = memoryview(senc.data)
        if len(data) < 4:
            return []
        version_flags = struct.unpack_from(">I", data, 0)[0]
        version, flags = version_flags >> 24, version_flags & 0xFFFFFF
        position = 4

        if version == 0:
            if len(data) < 8:
                return []
            sample_count = struct.unpack_from(">I", data, position)[0]
            position += 4

        if iv_size in (0, 8, 16):
            entries, _, _ = self._parse_senc_entries(
                data, position, sample_count, flags, iv_size, constant_iv
            )
            return entries

        entries_8, _, exact_8 = self._parse_senc_entries(
            data, position, sample_count, flags, 8, constant_iv
        )
        if exact_8:
            return entries_8
        entries_16, _, exact_16 = self._parse_senc_entries(
            data, position, sample_count, flags, 16, constant_iv
        )
        if exact_16:
            return entries_16
        return entries_8

    def _get_key_for_track(self, track_id: int) -> bytes:
        """
        Retrieves the decryption key for a given track ID from the key map.
        
        Uses the real KID extracted from the tenc box (stored in track_kid_map
        during moov processing) to look up the correct key from key_map.
        This supports any number of KID:KEY pairs correctly.

        Args:
            track_id (int): The track ID.

        Returns:
            bytes: The decryption key for the specified track ID.
        """
        # Best: look up the actual KID for this track from the tenc box
        if track_id in self.track_kid_map:
            kid = self.track_kid_map[track_id]
            if kid in self.key_map:
                return self.key_map[kid]
        
        # Single key: use it directly
        if len(self.key_map) == 1:
            return next(iter(self.key_map.values()))
        
        # Last resort fallback: index-based (for segments without moov/tenc info)
        keys_list = list(self.key_map.values())
        key_index = (track_id - 1) % len(keys_list)
        return keys_list[key_index]

    @staticmethod
    def _process_sample(
        sample: memoryview, sample_info: CENCSampleAuxiliaryDataFormat, key: bytes
    ) -> Union[memoryview, bytearray, bytes]:
        """
        Processes and decrypts a sample using the provided sample information and decryption key.
        This includes handling sub-sample encryption if present.

        Args:
            sample (memoryview): The sample data.
            sample_info (CENCSampleAuxiliaryDataFormat): The sample auxiliary data format with encryption information.
            key (bytes): The decryption key.

        Returns:
            Union[memoryview, bytearray, bytes]: The decrypted sample.
        """
        if not sample_info.is_encrypted:
            return sample

        # pad IV to 16 bytes
        iv = sample_info.iv + b"\x00" * (16 - len(sample_info.iv))
        if Cipher is not None:
            cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
            decrypt = cipher.update
            finalize = cipher.finalize
        else:
            cipher = AES.new(key, AES.MODE_CTR, initial_value=iv, nonce=b"")
            decrypt = cipher.decrypt
            finalize = lambda: b""

        if not sample_info.sub_samples:
            # If there are no sub_samples, decrypt the entire sample
            result = decrypt(sample)
            finalize()
            return result

        result = bytearray()
        offset = 0
        for clear_bytes, encrypted_bytes in sample_info.sub_samples:
            result.extend(sample[offset : offset + clear_bytes])
            offset += clear_bytes
            result.extend(decrypt(sample[offset : offset + encrypted_bytes]))
            offset += encrypted_bytes

        # If there's any remaining data, treat it as encrypted
        if offset < len(sample):
            result.extend(decrypt(sample[offset:]))

        finalize()

        return result

    def _process_trun(self, trun: MP4Atom) -> int:
        """
        Processes the 'trun' (Track Fragment Run) atom, which contains information about the samples in a track fragment.
        This includes sample sizes, durations, flags, and composition time offsets.

        Args:
            trun (MP4Atom): The 'trun' atom to process.

        Returns:
            int: The number of samples in the 'trun' atom.
        """
        trun_flags, sample_count = struct.unpack_from(">II", trun.data, 0)
        data_offset = 8

        if trun_flags & 0x000001:
            data_offset += 4
        if trun_flags & 0x000004:
            data_offset += 4

        self.trun_sample_sizes = array.array("I")
        default_size = self._default_sample_size

        for _ in range(sample_count):
            if trun_flags & 0x000100:
                data_offset += 4
            if trun_flags & 0x000200:
                sample_size = struct.unpack_from(">I", trun.data, data_offset)[0]
                self.trun_sample_sizes.append(sample_size)
                data_offset += 4
            else:
                self.trun_sample_sizes.append(default_size)
            if trun_flags & 0x000400:  # sample-flags-present flag
                data_offset += 4
            if trun_flags & 0x000800:  # sample-composition-time-offsets-present flag
                data_offset += 4

        return sample_count

    def _modify_trun(self, trun: MP4Atom) -> MP4Atom:
        """
        Modifies the 'trun' (Track Fragment Run) atom to update the data offset.
        This is necessary to account for the encryption overhead.

        Args:
            trun (MP4Atom): The 'trun' atom to modify.

        Returns:
            MP4Atom: Modified 'trun' atom with updated data offset.
        """
        trun_data = bytearray(trun.data)
        current_flags = struct.unpack_from(">I", trun_data, 0)[0] & 0xFFFFFF

        # If the data-offset-present flag is set, update the data offset to account for encryption overhead
        if current_flags & 0x000001:
            current_data_offset = struct.unpack_from(">i", trun_data, 8)[0]
            struct.pack_into(">i", trun_data, 8, current_data_offset - self.encryption_overhead)

        return MP4Atom(b"trun", len(trun_data) + 8, trun_data)

    def _process_sidx(self, sidx: MP4Atom) -> MP4Atom:
        """
        Processes the 'sidx' (Segment Index) atom, which contains indexing information for media segments.
        This includes references to media segments and their durations.

        Args:
            sidx (MP4Atom): The 'sidx' atom to process.

        Returns:
            MP4Atom: Processed 'sidx' atom with updated segment references.
        """
        sidx_data = bytearray(sidx.data)
        if len(sidx_data) < 28:
            return MP4Atom(b"sidx", len(sidx_data) + 8, sidx_data)

        has_box_header = sidx_data[4:8] == b"sidx"
        version = sidx_data[8 if has_box_header else 0]
        ref_offset = (32 if version == 0 else 40) if has_box_header else (24 if version == 0 else 32)
        if (
            ref_offset == 24
            and len(sidx_data) >= 36
            and struct.unpack_from(">I", sidx_data, 24)[0] == 0
            and struct.unpack_from(">I", sidx_data, 32)[0] != 0
        ):
            ref_offset = 32
        if len(sidx_data) < ref_offset + 4:
            return MP4Atom(b"sidx", len(sidx_data) + 8, sidx_data)

        current_size = struct.unpack_from(">I", sidx_data, ref_offset)[0]
        reference_type = current_size >> 31
        current_referenced_size = current_size & 0x7FFFFFFF

        # Remove encryption overhead from referenced size
        new_referenced_size = max(0, current_referenced_size - self.encryption_overhead)
        new_size = (reference_type << 31) | new_referenced_size
        struct.pack_into(">I", sidx_data, ref_offset, new_size)

        return MP4Atom(b"sidx", len(sidx_data) + 8, sidx_data)

    def _process_trak(self, trak: MP4Atom) -> MP4Atom:
        """
        Processes the 'trak' (Track) atom, which contains information about a single track in the movie.
        This includes track header, media information, and other track-level metadata.

        Args:
            trak (MP4Atom): The 'trak' atom to process.

        Returns:
            MP4Atom: Processed 'trak' atom with updated track information.
        """
        parser = MP4Parser(trak.data)
        atoms = parser.list_atoms()
        new_trak_data = bytearray()

        # Extract track_id from tkhd
        track_id = None
        for atom in atoms:
            if atom.atom_type == b"tkhd":
                if len(atom.data) > 20:
                    version = atom.data[0] if isinstance(atom.data[0], int) else ord(atom.data[0])
                    if version == 1:
                        track_id = struct.unpack_from(">I", atom.data, 20)[0]
                    else:
                        track_id = struct.unpack_from(">I", atom.data, 12)[0]
                break

        self._last_extracted_kid = None
        self._last_extracted_iv_size = None
        self._last_extracted_constant_iv = b""

        for atom in atoms:
            if atom.atom_type == b"mdia":
                new_mdia = self._process_mdia(atom)
                new_trak_data.extend(new_mdia.pack())
            else:
                new_trak_data.extend(atom.pack())

        # Store KID and IV mapping for this track (extracted from tenc inside sinf)
        if track_id is not None:
            if self._last_extracted_kid is not None:
                self.track_kid_map[track_id] = self._last_extracted_kid
            if self._last_extracted_iv_size is not None:
                self.track_iv_size_map[track_id] = self._last_extracted_iv_size
            if self._last_extracted_constant_iv:
                self.track_constant_iv_map[track_id] = self._last_extracted_constant_iv

        return MP4Atom(b"trak", len(new_trak_data) + 8, new_trak_data)

    def _process_mdia(self, mdia: MP4Atom) -> MP4Atom:
        """
        Processes the 'mdia' (Media) atom, which contains media information for a track.
        This includes media header, handler reference, and media information container.

        Args:
            mdia (MP4Atom): The 'mdia' atom to process.

        Returns:
            MP4Atom: Processed 'mdia' atom with updated media information.
        """
        parser = MP4Parser(mdia.data)
        new_mdia_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"minf":
                new_minf = self._process_minf(atom)
                new_mdia_data.extend(new_minf.pack())
            else:
                new_mdia_data.extend(atom.pack())

        return MP4Atom(b"mdia", len(new_mdia_data) + 8, new_mdia_data)

    def _process_minf(self, minf: MP4Atom) -> MP4Atom:
        """
        Processes the 'minf' (Media Information) atom, which contains information about the media data in a track.
        This includes data information, sample table, and other media-level metadata.

        Args:
            minf (MP4Atom): The 'minf' atom to process.

        Returns:
            MP4Atom: Processed 'minf' atom with updated media information.
        """
        parser = MP4Parser(minf.data)
        new_minf_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stbl":
                new_stbl = self._process_stbl(atom)
                new_minf_data.extend(new_stbl.pack())
            else:
                new_minf_data.extend(atom.pack())

        return MP4Atom(b"minf", len(new_minf_data) + 8, new_minf_data)

    def _process_stbl(self, stbl: MP4Atom) -> MP4Atom:
        """
        Processes the 'stbl' (Sample Table) atom, which contains information about the samples in a track.
        This includes sample descriptions, sample sizes, sample times, and other sample-level metadata.

        Args:
            stbl (MP4Atom): The 'stbl' atom to process.

        Returns:
            MP4Atom: Processed 'stbl' atom with updated sample information.
        """
        parser = MP4Parser(stbl.data)
        new_stbl_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stsd":
                new_stsd = self._process_stsd(atom)
                new_stbl_data.extend(new_stsd.pack())
            elif atom.atom_type not in {b"sbgp", b"sgpd"}:
                new_stbl_data.extend(atom.pack())

        return MP4Atom(b"stbl", len(new_stbl_data) + 8, new_stbl_data)

    def _process_stsd(self, stsd: MP4Atom) -> MP4Atom:
        """
        Processes the 'stsd' (Sample Description) atom, which contains descriptions of the sample entries in a track.
        This includes codec information, sample entry details, and other sample description metadata.

        Args:
            stsd (MP4Atom): The 'stsd' atom to process.

        Returns:
            MP4Atom: Processed 'stsd' atom with updated sample descriptions.
        """
        parser = MP4Parser(stsd.data)
        entry_count = struct.unpack_from(">I", parser.data, 4)[0]
        new_stsd_data = bytearray(stsd.data[:8])

        parser.position = 8  # Move past version_flags and entry_count

        for _ in range(entry_count):
            sample_entry = parser.read_atom()
            if not sample_entry:
                break

            processed_entry = self._process_sample_entry(sample_entry)
            new_stsd_data.extend(processed_entry.pack())

        return MP4Atom(b"stsd", len(new_stsd_data) + 8, new_stsd_data)

    def _process_sample_entry(self, entry: MP4Atom) -> MP4Atom:
        """
        Processes a sample entry atom, which contains information about a specific type of sample.
        This includes codec-specific information and other sample entry details.

        Args:
            entry (MP4Atom): The sample entry atom to process.

        Returns:
            MP4Atom: Processed sample entry atom with updated information.
        """
        # Determine the size of fixed fields based on sample entry type
        if entry.atom_type in {b"mp4a", b"enca"}:
            fixed_size = 28  # 8 bytes for size, type and reserved, 20 bytes for fixed fields in Audio Sample Entry.
        elif entry.atom_type in {b"mp4v", b"encv", b"avc1", b"hev1", b"hvc1"}:
            fixed_size = 78  # 8 bytes for size, type and reserved, 70 bytes for fixed fields in Video Sample Entry.
        else:
            fixed_size = 16  # 8 bytes for size, type and reserved, 8 bytes for fixed fields in other Sample Entries.

        new_entry_data = bytearray(entry.data[:fixed_size])
        parser = MP4Parser(entry.data[fixed_size:])
        codec_format = None

        for atom in iter(parser.read_atom, None):
            if atom.atom_type in {b"sinf", b"schi", b"tenc", b"schm"}:
                if atom.atom_type == b"sinf":
                    codec_format = self._extract_codec_format(atom)
                    kid = self._extract_kid_from_sinf(atom)
                    if kid is not None:
                        self._last_extracted_kid = kid
                continue  # Skip encryption-related atoms
            new_entry_data.extend(atom.pack())

        # Replace the atom type with the extracted codec format
        new_type = codec_format if codec_format else entry.atom_type
        return MP4Atom(new_type, len(new_entry_data) + 8, new_entry_data)

    def _extract_codec_format(self, sinf: MP4Atom) -> Optional[bytes]:
        """
        Extracts the codec format from the 'sinf' (Protection Scheme Information) atom.
        This includes information about the original format of the protected content.

        Args:
            sinf (MP4Atom): The 'sinf' atom to extract from.

        Returns:
            Optional[bytes]: The codec format or None if not found.
        """
        parser = MP4Parser(sinf.data)
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"frma":
                return atom.data
        return None

    def _extract_kid_from_sinf(self, sinf: MP4Atom) -> Optional[bytes]:
        """
        Extracts the default KID from the 'tenc' box inside sinf -> schi -> tenc.
        
        tenc layout: version(1) + flags(3) + reserved(2) + isProtected(1) + perSampleIVSize(1) + default_KID(16)
        So KID is at offset 8..24 of tenc data.

        Args:
            sinf (MP4Atom): The 'sinf' atom.

        Returns:
            Optional[bytes]: The 16-byte KID or None.
        """
        parser = MP4Parser(sinf.data)
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"schi":
                schi_parser = MP4Parser(atom.data)
                for schi_atom in iter(schi_parser.read_atom, None):
                    if schi_atom.atom_type == b"tenc" and len(schi_atom.data) >= 24 and schi_atom.data[0] in (0, 1):
                        iv_size = schi_atom.data[7]
                        self._last_extracted_iv_size = iv_size
                        if iv_size == 0 and len(schi_atom.data) >= 25:
                            const_iv_size = schi_atom.data[24]
                            if len(schi_atom.data) >= 25 + const_iv_size:
                                self._last_extracted_constant_iv = bytes(schi_atom.data[25 : 25 + const_iv_size])
                        return bytes(schi_atom.data[8:24])
        return None


def decrypt_segment(init_segment: bytes, segment_content: bytes, key_id: str, key: str, skip_init: bool = False) -> bytes:
    """
    Decrypts a CENC encrypted MP4 segment.

    Args:
        init_segment (bytes): Initialization segment data.
        segment_content (bytes): Encrypted segment content.
        key_id (str): Key ID(s) in hexadecimal format. Supports comma-separated for multi-key: "KID1,KID2"
        key (str): Key(s) in hexadecimal format. Supports comma-separated for multi-key: "KEY1,KEY2"
        skip_init (bool): If True, all atoms belonging to the initialization segment are excluded from the output.
    
    Returns:
        bytes: Decrypted segment content.
    
    Raises:
        ValueError: If the number of key_ids doesn't match the number of keys.
    """
    # Support multi-key: "KID1,KID2" and "KEY1,KEY2"
    kid_list = [k.strip() for k in key_id.split(',')]
    key_list = [k.strip() for k in key.split(',')]
    
    if len(kid_list) != len(key_list):
        raise ValueError(f"Mismatched key_id/key count: {len(kid_list)} key_ids vs {len(key_list)} keys")
    
    # Build key_map with all key pairs
    key_map = {}
    for kid, k in zip(kid_list, key_list):
        key_map[bytes.fromhex(kid)] = bytes.fromhex(k)
    
    decrypter = MP4Decrypter(key_map)
    init_length = len(init_segment) if skip_init else 0
    decrypted_content = decrypter.decrypt_segment(init_segment + segment_content, init_length=init_length)
    return decrypted_content




