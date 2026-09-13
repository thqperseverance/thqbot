import asyncio
class LoopProxy:
    def __getattr__(self, name):
        return getattr(asyncio.get_event_loop(), name)

loop = LoopProxy()
def main():
    async def foo():
        print("Hello")
    loop.run_until_complete(foo())

main()
