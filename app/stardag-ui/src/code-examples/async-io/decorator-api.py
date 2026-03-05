import stardag as sd
import httpx

@sd.task
async def download(url: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

urls = [f"https://www.example.com/{i}" for i in range(5)]

# All async tasks run concurrently via asyncio
sd.build([download(url=url) for url in urls])
