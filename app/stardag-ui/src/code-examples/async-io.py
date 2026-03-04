import stardag as sd
import httpx

@sd.task
async def download(url: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

# Or with Class API:
class Download(sd.Task[str]):
    url: str

    async def run_aio(self):
        async with httpx.AsyncClient() as client:
            response = await client.get(self.url)
            response.raise_for_status()
            self._save(response.text)

urls = [f"https://www.example.com/{i}" for i in range(5)]
sd.build([download(url=url) for url in urls])
