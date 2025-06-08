# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx",
# ]
# ///

import asyncio
from datetime import datetime, timedelta, UTC
import time

import httpx


BASE_URL = "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA"
FILENAME_TEMPLATE = "PUBLIC_DISPATCHSCADA_{date:%Y%m%d}.zip"
MAX_CONNECTIONS = 6


def get_safe_recent_dates(n: int = 10, offset_days: int = 3) -> list[datetime]:
    """Generate list of `n` dates starting from `offset_days` ago, in YYYYMMDD format."""
    start_date = datetime.now(tz=UTC) - timedelta(days=offset_days)
    return [(start_date - timedelta(days=i)) for i in range(n)]


def build_urls(dates: list[str]) -> list[str]:
    return [f"{BASE_URL}/{FILENAME_TEMPLATE.format(date=d)}" for d in dates]


def download_sync(urls: list[str]) -> list[bytes]:
    contents = []
    with httpx.Client() as client:
        for url in urls:
            response = client.get(url)
            response.raise_for_status()
            contents.append(response.content)
    return contents


async def download_one_async(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


async def download_async(urls: list[str]) -> list[bytes]:
    # This limits stops the AEMO web server from blocking us due to too many requests
    limits = httpx.Limits(max_connections=MAX_CONNECTIONS)
    async with httpx.AsyncClient(limits=limits) as client:
        return await asyncio.gather(*[download_one_async(client, url) for url in urls])


async def main():
    file_count = 20
    dates = get_safe_recent_dates(n=file_count, offset_days=3)
    urls = build_urls(dates)

    print(f"▶ Downloading {file_count} files synchronously...")
    start_sync = time.perf_counter()
    sync_contents = download_sync(urls)
    end_sync = time.perf_counter()
    sync_duration = end_sync - start_sync

    print(f"▶ Downloading {file_count} files asynchronously...")
    start_async = time.perf_counter()
    async_contents = await download_async(urls)
    end_async = time.perf_counter()
    async_duration = end_async - start_async

    print("\n📊 Results:")
    print(f"  Sync duration:  {sync_duration:.2f} seconds")
    print(f"  Async duration: {async_duration:.2f} seconds")

    assert sync_contents == async_contents, "Mismatch in file contents downloaded"
    print("✅ All files downloaded successfully.")

    if async_duration < sync_duration:
        print("🚀 Async download was faster!")
    else:
        print("⚠️ Async was not faster (may be due to network conditions or small file sizes)")


if __name__ == "__main__":
    asyncio.run(main())
