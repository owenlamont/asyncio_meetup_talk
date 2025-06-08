# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "anyio",
# ]
# ///

import asyncio
from pathlib import Path
import tempfile
import time

from anyio import Path as APath


async def read_file_async(path: APath) -> str:
    return await path.read_text()


def read_file_sync(path: Path) -> str:
    return path.read_text()


async def main():
    # Create temporary files
    temp_dir = tempfile.TemporaryDirectory()
    file_paths = []
    file_count = 10
    file_size = 100_000

    content = "5" * file_size

    for i in range(file_count):
        file_path = Path(temp_dir.name) / f"file_{i}.txt"
        file_path.write_text(content)
        file_paths.append(file_path)

    # Synchronous read
    start_sync = time.perf_counter()
    sync_contents = [read_file_sync(p) for p in file_paths]
    end_sync = time.perf_counter()
    sync_duration = end_sync - start_sync

    # Asynchronous read
    start_async = time.perf_counter()
    async_contents = await asyncio.gather(*(read_file_async(APath(p)) for p in file_paths))
    end_async = time.perf_counter()
    async_duration = end_async - start_async

    print(f"Reading {file_count} files of size {file_size} bytes")
    print(f"Sync duration:   {sync_duration:.6f} seconds")
    print(f"Async duration:  {async_duration:.6f} seconds")

    # Simple correctness check
    assert sync_contents == async_contents

    # Performance check with margin
    if async_duration < sync_duration:
        print("✅ Async was faster")
    else:
        print("⚠️ Async was slower")

    temp_dir.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
