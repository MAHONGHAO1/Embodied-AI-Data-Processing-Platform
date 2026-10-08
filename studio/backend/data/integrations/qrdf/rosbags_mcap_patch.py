"""rosbags MCAP encoding patch (derived from droid_dataset, compatible with Atom MCAP with encoding=unknown)."""

from __future__ import annotations

from collections import defaultdict
from io import BytesIO


def apply_mcap_schema_encoding_patch() -> None:
    """Call once before creating AnyReader."""
    from rosbags.interfaces import (
        Connection,
        ConnectionExtRosbag2,
        MessageDefinition,
        MessageDefinitionFormat,
        Qos,
    )
    from rosbags.rosbag2 import storage_mcap as mcap_mod
    from rosbags.rosbag2.errors import ReaderError
    from rosbags.rosbag2.metadata import parse_qos

    McapReader = mcap_mod.McapReader
    Statistics = mcap_mod.Statistics
    deserialize_uint32 = mcap_mod.deserialize_uint32
    deserialize_uint64 = mcap_mod.deserialize_uint64
    read_sized = mcap_mod.read_sized
    read_string = mcap_mod.read_string

    if not getattr(mcap_mod, "_quicdata_read_string_patch", False):

        def read_string_safe(bio):
            length = deserialize_uint32(bio.read(4))[0]
            raw = bio.read(length)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")

        mcap_mod.read_string = read_string_safe
        mcap_mod._quicdata_read_string_patch = True
        read_string = read_string_safe

    if getattr(McapReader, "_quicdata_mcap_encoding_patch", False):
        return

    def open_patched(self: McapReader) -> None:
        try:
            self.bio = self.path.open("rb")
        except OSError as err:
            msg = f"Could not open file {str(self.path)!r}: {err.strerror}."
            raise ReaderError(msg) from err

        magic = self.bio.read(8)
        if not magic:
            msg = f"File {str(self.path)!r} seems to be empty."
            raise ReaderError(msg)

        if magic != b"\x89MCAP0\r\n":
            msg = "File magic is invalid."
            raise ReaderError(msg)

        op_ = ord(self.bio.read(1))
        if op_ != 0x01:
            msg = "Unexpected record."
            raise ReaderError(msg)

        recio = BytesIO(read_sized(self.bio))
        profile = read_string(recio)
        # Atom / certain devices MCAP profile may be empty or non-ros2; still attempt to decode as ROS2 messages
        if profile not in ("ros2", ""):
            import logging

            logging.getLogger(__name__).warning(
                "MCAP profile=%r，非标准 ros2，按兼容模式继续读取", profile
            )
        self.data_start = self.bio.tell()

        _ = self.bio.seek(-37, 2)
        footer_start = self.bio.tell()
        data = self.bio.read()
        magic = data[-8:]
        if magic != b"\x89MCAP0\r\n":
            msg = "File end magic is invalid."
            raise ReaderError(msg)

        assert len(data) == 37
        assert data[0:9] == b"\x02\x14\x00\x00\x00\x00\x00\x00\x00", data[0:9]

        (summary_start,) = deserialize_uint64(data[9:17])
        if summary_start:
            self.data_end = summary_start
            self.read_index()
            if self.statistics:
                if not self.schemas:
                    self.meta_scan()
            elif self.chunks:
                message_count = sum(sum(x.channel_count.values()) for x in self.chunks)
                start_time = min(x.message_start_time for x in self.chunks)
                end_time = max(x.message_end_time for x in self.chunks)
                cstats: dict[int, int] = defaultdict(int)
                for chunk in self.chunks:
                    for cid, count in chunk.channel_count.items():
                        cstats[cid] += count

                self.statistics = Statistics(
                    message_count,
                    len(self.schemas),
                    len(self.channels),
                    0,
                    0,
                    len(self.chunks),
                    start_time,
                    end_time,
                    cstats,
                )
            else:
                self.meta_scan()
        else:
            self.data_end = footer_start
            self.meta_scan()

        def get_msgdef(name: str) -> MessageDefinition:
            fmtmap = {
                "ros2msg": MessageDefinitionFormat.MSG,
                "ros2idl": MessageDefinitionFormat.IDL,
                "omgidl": MessageDefinitionFormat.IDL,
                "": MessageDefinitionFormat.NONE,
                "unknown": MessageDefinitionFormat.MSG,
            }
            if msgtype := next((x for x in self.schemas.values() if x.name == name), None):
                fmt = fmtmap.get(msgtype.encoding)
                if fmt is None:
                    fmt = MessageDefinitionFormat.MSG
                return MessageDefinition(fmt, msgtype.data)
            return MessageDefinition(MessageDefinitionFormat.NONE, "")

        def get_qos(metadata: bytes) -> list[Qos]:
            bio = BytesIO(metadata)
            while bio.tell() < len(metadata):
                try:
                    key = read_string(bio)
                    value = read_string(bio)
                except Exception:
                    break
                if key == "offered_qos_profiles":
                    try:
                        return parse_qos(value)
                    except Exception:
                        return []
            return []

        assert self.statistics
        self.connections = [
            Connection(
                x.id,
                x.topic,
                x.schema,
                get_msgdef(x.schema),
                "",
                self.statistics.channel_message_counts.get(x.id, 0),
                ConnectionExtRosbag2(
                    x.message_encoding,
                    get_qos(x.metadata),
                ),
                self,
            )
            for x in self.channels.values()
        ]

        message_count = self.statistics.message_count
        start_time = self.statistics.start_time
        end_time = self.statistics.end_time
        duration = end_time - start_time

        self.metadata = self.metadata._replace(
            duration=duration + 1,
            start_time=start_time,
            end_time=end_time + 1,
            message_count=message_count,
        )

    McapReader.open = open_patched
    McapReader._quicdata_mcap_encoding_patch = True
