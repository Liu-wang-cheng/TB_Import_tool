#!/usr/bin/env python3
"""
AI缺陷分析工具 - DRC日志解析器
DRC Log Parser - Python equivalent of Tool_DrcToLog for LDROBOT AutopackDebugger format.
Parses .drc files containing AABBCC00-framed log records.
"""
import argparse
import re
import sys
from pathlib import Path

FRAME_MAGIC = b'\x00\xcc\xbb\xaa'
HEADER_SIZE = 28
LZ4_BLOCK_SIZE = 4 * 1024 * 1024

LOG_HEAD = re.compile(
    rb'\d{1,2}-\d{1,2}\s+\d{1,2}:\d{1,2}:\d{1,2}\.\d{3}/[A-Z]{2,}\s+[DIWEF]/[\w/_.-]+:\d+\s'
)


def extract_frames(data: bytes) -> list:
    """Find all AABBCC00-delimited frames."""
    positions = []
    pos = 0
    while True:
        pos = data.find(FRAME_MAGIC, pos)
        if pos == -1:
            break
        positions.append(pos)
        pos += 4
    return positions


def _scan_log_lines(data: bytes) -> list:
    """Scan binary data for log lines matching LOG_HEAD, merging multi-line entries."""
    logs = []
    matches = list(LOG_HEAD.finditer(data))
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(data)
        chunk = data[start:end]
        parts = []
        for seg_idx, segment in enumerate(chunk.split(b'\n')):
            s = 0
            while s < len(segment) and not (0x20 <= segment[s] <= 0x7E):
                s += 1
            e = s
            while e < len(segment) and 0x20 <= segment[e] <= 0x7E:
                e += 1
            # First segment (main log line): include any length
            # Continuation segments: require >= 8 chars to filter binary noise
            min_len = 2 if seg_idx == 0 else 8
            if e - s >= min_len:
                parts.append(segment[s:e].decode('ascii').strip())
        merged = ' '.join(parts)
        if len(merged) > 30:
            logs.append(merged)
    return logs


def extract_logs_from_frame(frame_data: bytes) -> list:
    """Extract individual log lines from a frame's data payload."""
    return _scan_log_lines(frame_data)


def _decompress_lz4(data: bytes) -> bytes:
    """Decompress LZ4-compressed DRC data (.drc.save format)."""
    import lz4.block
    return lz4.block.decompress(data, uncompressed_size=LZ4_BLOCK_SIZE)


def extract_logs(data: bytes) -> list:
    """Extract all log lines from raw DRC binary data (auto-detects LZ4 format)."""
    # LZ4-compressed .drc.save: 8-byte header (type=6) + LZ4 block
    if len(data) >= 8:
        import struct
        type_val = struct.unpack('<Q', data[:8])[0]
        if type_val == 6:
            data = _decompress_lz4(data[8:])

    # Try AABBCC00 frame-based extraction first
    frames = extract_frames(data)
    if frames:
        logs = []
        for i, start in enumerate(frames):
            end = frames[i + 1] if i + 1 < len(frames) else len(data)
            chunk = data[start:end]
            if len(chunk) < HEADER_SIZE:
                continue
            logs.extend(extract_logs_from_frame(chunk[HEADER_SIZE:]))
        return logs

    # Fallback: direct regex extraction for non-framed data
    return _scan_log_lines(data)


def parse_drc(input_path: Path, output_path: Path = None) -> int:
    with open(input_path, 'rb') as f:
        data = f.read()

    logs = extract_logs(data)
    if not logs:
        print(f'No log data found in {input_path}', file=sys.stderr)
        return 1

    if output_path is None:
        output_path = input_path.with_suffix('.txt')

    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        for line in logs:
            f.write(line + '\n')

    print(f'Input : {input_path} ({len(data)} bytes)')
    print(f'Logs  : {len(logs)} lines')
    print(f'Output: {output_path}')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Parse LDROBOT .drc log files to text')
    parser.add_argument('input', type=Path, help='Input .drc file')
    parser.add_argument('-o', '--output', type=Path, default=None, help='Output .txt file')
    args = parser.parse_args()

    if not args.input.exists():
        print(f'Error: file not found: {args.input}', file=sys.stderr)
        sys.exit(1)

    sys.exit(parse_drc(args.input, args.output))


if __name__ == '__main__':
    main()
