import stardag as sd
import httpx

class Download(sd.Task[str]):
    url: str

    async def run_aio(self):
        async with httpx.AsyncClient() as client:
            response = await client.get(self.url)
            response.raise_for_status()
            self._save(response.text)

urls = [f"https://www.example.com/{i}" for i in range(5)]

# All async tasks run concurrently via asyncio
sd.build([Download(url=url) for url in urls])
