"""FastAPI service: loads the champion pipeline, caches in Redis (in-memory fallback), rate-limits, logs feedback."""
import hashlib, json, logging, os, time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ghrec.pipeline import Pipeline

log = logging.getLogger("feedback")


class Store:
    """Redis if REDIS_URL is set, else a process-local dict (single replica / dev only)."""

    def __init__(self):
        url = os.environ.get("REDIS_URL")
        self.r = None
        if url:
            import redis
            self.r = redis.Redis.from_url(url)
        self.d: dict = {}

    def get(self, k):
        if self.r:
            return self.r.get(k)
        v = self.d.get(k)
        return v[1] if v and v[0] > time.time() else None

    def set(self, k, v, ttl):
        if self.r:
            self.r.set(k, v, ex=ttl)
        else:
            self.d[k] = (time.time() + ttl, v)

    def incr(self, k, ttl):
        if self.r:
            n = self.r.incr(k)
            if n == 1:
                self.r.expire(k, ttl)
            return n
        exp, n = self.d.get(k, (0, 0))
        if exp <= time.time():
            exp, n = time.time() + ttl, 0
        self.d[k] = (exp, n + 1)
        return n + 1


@asynccontextmanager
async def lifespan(app):
    app.state.pipe = Pipeline.load(os.environ.get("MODEL_PATH", "artifacts/champion.pkl"))
    app.state.store = Store()
    yield


app = FastAPI(lifespan=lifespan)
LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT", "60"))


class RecReq(BaseModel):
    likes: list[str] = Field(default_factory=list, max_length=100)
    session_id: str = Field(min_length=8, max_length=64)
    k: int = Field(10, ge=1, le=50)


class Fb(BaseModel):
    session_id: str
    request_id: str
    repo: str
    event: str = Field(pattern="^(impression|click)$")


@app.post("/recommend")
def recommend(req: RecReq, request: Request):
    s = app.state.store
    ip = request.client.host if request.client else "?"
    if max(s.incr(f"rl:s:{req.session_id}", 60), s.incr(f"rl:ip:{ip}", 60)) > LIMIT_PER_MIN:
        raise HTTPException(429, "rate limit exceeded")
    key = "rec:" + hashlib.sha1(json.dumps([sorted(req.likes), req.k]).encode()).hexdigest()
    if (hit := s.get(key)) is not None:
        items = json.loads(hit)
    else:
        items = app.state.pipe.recommend(req.likes, req.k)
        s.set(key, json.dumps(items), 600)
    return {"items": items, "request_id": hashlib.sha1(f"{req.session_id}{time.time()}".encode()).hexdigest()[:12]}


@app.get("/trending")
def trending(k: int = 10):
    return {"items": app.state.pipe.recommend([], min(k, 50))}


@app.post("/feedback")
def feedback(fb: Fb):
    log.info(json.dumps(fb.model_dump()))  # shortcut: stdout -> Log Analytics; swap for Event Hubs when analysis needs it
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok", "repos": len(app.state.pipe.repos)}
