import asyncio
import aiohttp

BASE_URL = "https://api.awakening.wiki/items"
LIMIT = 1000

async def fetch_all_items():
    offset = 0
    all_items = []

    async with aiohttp.ClientSession() as session:
        while True:
            params = {
                "limit": LIMIT,
                "offset": offset,
                "shuffle": 0,
                "fields": "Id,name,item_tags,short_description"
            }

            async with session.get(BASE_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            items = data["list"]

            if not items:
                break

            all_items.extend(items)

            print(f"Fetched {len(items)} items (offset={offset})")

            if len(items) < LIMIT:
                break

            offset += LIMIT

    return all_items


if __name__ == "__main__":
    items = asyncio.run(fetch_all_items())
    print(f"\nTotal items fetched: {len(items)}")