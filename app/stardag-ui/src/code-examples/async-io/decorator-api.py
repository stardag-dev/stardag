"""Completion checks already happen concurrently via asyncio. Decorate 
async functions to get async concurrent task execution in build."""
import httpx
import stardag as sd

@sd.task
async def download(url: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

urls = [f"https://www.example.com/{i}" for i in range(5)]
tasks = [download(url=url) for url in urls]

# All async tasks run concurrently via asyncio
sd.build(tasks)

# Or in an async context
async def main():
    await sd.build_aio(tasks)
