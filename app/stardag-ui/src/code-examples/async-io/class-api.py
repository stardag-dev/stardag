"""Completion checks already happen concurrently via asyncio. Implement 
`run_aio` for async concurrent task execution in build."""
import httpx
import stardag as sd

class Download(sd.Task[str]):
    url: str

    async def run_aio(self):
        async with httpx.AsyncClient() as client:
            response = await client.get(self.url)
            response.raise_for_status()
            self._save(response.text)

urls = [f"https://www.example.com/{i}" for i in range(5)]
tasks = [Download(url=url) for url in urls]

# All async tasks run concurrently via asyncio
sd.build(tasks)

# Or in an async context
async def main():
    await sd.build_aio(tasks)
