import asyncio, json
from collections import deque

class EventBus:
    def __init__(self):
        self.clients=set()
        self.history=deque(maxlen=100)
    async def publish(self, event: str, data: dict):
        payload={"event":event,"data":data}
        self.history.append(payload)
        dead=[]
        for q in list(self.clients):
            try: q.put_nowait(payload)
            except asyncio.QueueFull: dead.append(q)
        for q in dead: self.clients.discard(q)
    def subscribe(self):
        q=asyncio.Queue(maxsize=200); self.clients.add(q); return q
    def unsubscribe(self,q): self.clients.discard(q)
    @staticmethod
    def encode(payload):
        return f"event: {payload['event']}\ndata: {json.dumps(payload['data'], default=str)}\n\n"

bus=EventBus()
