import asyncio
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import bcrypt
import docker
import jwt
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db as database
from . import eggs as eggmod

SECRET = os.environ.get("PANEL_SECRET")
if not SECRET:
    raise RuntimeError("PANEL_SECRET is not set (see /etc/mypanel/panel.env)")
DATA = Path(os.environ.get("PANEL_DATA", "/srv/mypanel/servers"))
HERE = Path(__file__).parent
LABEL = "mypanel.managed"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
TEMPLATES = json.loads((HERE / "templates.json").read_text())
EGGS = eggmod.load_eggs()
STATE_DIR = Path(database.DB_PATH).parent
INSTALL_LOGS = STATE_DIR / "install-logs"
INSTALL_TMP = STATE_DIR / "install-tmp"  # must be a real host dir: dockerd bind-mounts from it
STARTING: Dict[str, int] = {}  # server name -> start generation, while an egg server is "starting"
LAST_ERROR: Dict[str, str] = {}
_gen = 0

app = FastAPI(title="MyPanel", docs_url=None, redoc_url=None)


class _LazyDocker:
    """Connect to Docker on first use so the panel can start even if dockerd is still booting."""
    _client = None

    def __getattr__(self, name):
        if _LazyDocker._client is None:
            _LazyDocker._client = docker.from_env()
        return getattr(_LazyDocker._client, name)


dk = _LazyDocker()
bearer = HTTPBearer(auto_error=False)


# ---------------- auth ----------------
def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def check_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except ValueError:
        return False


def make_token(uid: int) -> str:
    return jwt.encode({"uid": uid, "exp": int(time.time()) + 8 * 3600}, SECRET, "HS256")


def user_from_token(token: str) -> dict:
    try:
        uid = jwt.decode(token, SECRET, algorithms=["HS256"])["uid"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    with database.db() as c:
        row = c.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "User not found")
    return dict(row)


def current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    if not cred:
        raise HTTPException(401, "Not authenticated")
    return user_from_token(cred.credentials)


def admin_only(u: dict = Depends(current_user)) -> dict:
    if u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return u


_fails: Dict[str, list] = {}


def _recent_fails(key: str) -> list:
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < 300]
    return _fails[key]


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login, request: Request):
    key = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    if len(_recent_fails(key)) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    with database.db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not row or not check_pw(body.password, row["pw_hash"]):
        _recent_fails(key).append(time.time())
        raise HTTPException(401, "Wrong username or password")
    return {"token": make_token(row["id"]), "username": row["username"], "role": row["role"]}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(u: dict = Depends(current_user)):
    return u


class PwChange(BaseModel):
    current: str
    new: str = Field(min_length=8)


@app.post("/api/me/password")
def change_password(body: PwChange, u: dict = Depends(current_user)):
    with database.db() as c:
        row = c.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
        if not check_pw(body.current, row["pw_hash"]):
            raise HTTPException(400, "Current password is wrong")
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(body.new), u["id"]))
    return {"ok": True}


# ---------------- users (admin) ----------------
class NewUser(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = "user"


@app.get("/api/users")
def list_users(_: dict = Depends(admin_only)):
    with database.db() as c:
        return [dict(r) for r in c.execute("SELECT id, username, role FROM users ORDER BY id")]


@app.post("/api/users")
def create_user(body: NewUser, _: dict = Depends(admin_only)):
    if not USER_RE.match(body.username) or body.role not in ("admin", "user"):
        raise HTTPException(400, "Invalid username or role")
    try:
        with database.db() as c:
            c.execute(
                "INSERT INTO users(username, pw_hash, role) VALUES(?,?,?)",
                (body.username, hash_pw(body.password), body.role),
            )
    except Exception:
        raise HTTPException(409, "Username already exists")
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, u: dict = Depends(admin_only)):
    if uid == u["id"]:
        raise HTTPException(400, "You cannot delete yourself")
    with database.db() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("UPDATE servers SET owner_id=NULL WHERE owner_id=?", (uid,))
    return {"ok": True}


# ---------------- servers ----------------
def server_row(name: str, user: dict):
    with database.db() as c:
        r = c.execute("SELECT * FROM servers WHERE name=?", (name,)).fetchone()
    if not r or (user["role"] != "admin" and r["owner_id"] != user["id"]):
        raise HTTPException(404, "Server not found")
    return r


def container(name: str):
    try:
        return dk.containers.get(f"mp_{name}")
    except NotFound:
        raise HTTPException(404, "Container is missing")


def egg_of(r):
    """The egg for a server row, or None for old template-based servers."""
    if not r["egg"]:
        return None
    egg = EGGS.get(r["egg"])
    if not egg:
        raise HTTPException(500, f"Egg '{r['egg']}' is not installed on this panel")
    return egg


def row_env(r) -> Dict[str, str]:
    return json.loads(r["variables"] or "{}")


def docker_status(name: str) -> Optional[str]:
    try:
        return dk.containers.get(f"mp_{name}").status
    except NotFound:
        return None


def server_status(r, docker_st: Optional[str]) -> str:
    if not r["egg"]:  # old template servers: plain docker status
        return docker_st or "missing"
    if r["state"] in ("installing", "install_failed"):
        return r["state"]
    if r["name"] in STARTING:
        return "starting"
    return "running" if docker_st == "running" else "offline"


def set_state(name: str, state: str) -> None:
    with database.db() as c:
        c.execute("UPDATE servers SET state=? WHERE name=?", (state, name))


def get_row(name: str):
    with database.db() as c:
        return c.execute("SELECT * FROM servers WHERE name=?", (name,)).fetchone()


def spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def own_for_container(paths) -> None:
    for p in paths:
        try:
            os.chown(p, eggmod.CONTAINER_UID, eggmod.CONTAINER_GID)
        except OSError:
            pass


@app.get("/api/templates")
def templates(_: dict = Depends(current_user)):
    return TEMPLATES


@app.get("/api/eggs")
def list_eggs(_: dict = Depends(admin_only)):
    return [eggmod.public_egg(e) for e in EGGS.values()]


@app.get("/api/servers")
def list_servers(u: dict = Depends(current_user)):
    with database.db() as c:
        if u["role"] == "admin":
            rows = c.execute(
                "SELECT s.*, u.username AS owner FROM servers s LEFT JOIN users u ON u.id=s.owner_id ORDER BY s.name"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT s.*, u.username AS owner FROM servers s LEFT JOIN users u ON u.id=s.owner_id "
                "WHERE s.owner_id=? ORDER BY s.name",
                (u["id"],),
            ).fetchall()
    status = {c.name: c.status for c in dk.containers.list(all=True, filters={"label": f"{LABEL}=1"})}
    return [
        {
            "name": r["name"],
            "image": r["image"],
            "egg": EGGS[r["egg"]]["name"] if r["egg"] in EGGS else r["egg"],
            "owner": r["owner"],
            "status": server_status(r, status.get(f"mp_{r['name']}")),
        }
        for r in rows
    ]


@app.get("/api/servers/{name}")
def server_detail(name: str, u: dict = Depends(current_user)):
    r = server_row(name, u)
    egg = egg_of(r)
    out = {
        "name": r["name"],
        "image": r["image"],
        "egg": r["egg"],
        "egg_name": egg["name"] if egg else None,
        "state": r["state"],
        "memory_mb": r["memory_mb"],
        "cpus": r["cpus"],
        "port": r["port"],
        "variables": [],
    }
    if egg:
        env = row_env(r)
        for v in egg["variables"]:
            if not v["user_viewable"] and u["role"] != "admin":
                continue
            out["variables"].append(
                {
                    "name": v["name"],
                    "description": v["description"],
                    "env_variable": v["env_variable"],
                    "value": env.get(v["env_variable"], v["default_value"]),
                    "secret": not v["user_viewable"],
                    "editable": v["user_editable"] or u["role"] == "admin",
                }
            )
    return out


class VarUpdate(BaseModel):
    variables: Dict[str, str]


@app.put("/api/servers/{name}/variables")
def update_variables(name: str, body: VarUpdate, u: dict = Depends(current_user)):
    r = server_row(name, u)
    egg = egg_of(r)
    if not egg:
        raise HTTPException(400, "This server was not created from an egg")
    try:
        env = eggmod.validate_variables(egg, body.variables, base=row_env(r), is_admin=u["role"] == "admin")
    except ValueError as e:
        raise HTTPException(400, str(e))
    with database.db() as c:
        c.execute("UPDATE servers SET variables=? WHERE name=?", (json.dumps(env), name))
    return {"ok": True, "note": "Restart the server to apply the changes"}


class NewServer(BaseModel):
    name: str
    image: Optional[str] = None
    command: Optional[str] = None
    memory_mb: int = Field(512, ge=64, le=65536)
    cpus: float = Field(1.0, gt=0, le=64)
    port: Optional[int] = Field(None, ge=1024, le=65535)
    container_port: Optional[int] = Field(None, ge=1, le=65535)
    env: Dict[str, str] = {}
    owner: Optional[str] = None
    egg: Optional[str] = None  # egg id; when set, `variables` are the egg variables
    variables: Dict[str, str] = {}


def _run_install(name: str) -> None:
    """Background job: egg install script. Moves the server to 'ready' or 'install_failed'."""
    ok = False
    log_path = INSTALL_LOGS / f"{name}.log"
    try:
        r = get_row(name)
        egg = EGGS.get(r["egg"]) if r else None
        if not egg:
            raise RuntimeError("egg not found")
        ok = eggmod.install_server(dk, name, egg, row_env(r), DATA / name, log_path, INSTALL_TMP)
    except Exception as e:
        LAST_ERROR[name] = str(e)
        try:
            with open(log_path, "a") as f:
                f.write(f"\r\n[panel] Install crashed: {e}\r\n")
        except OSError:
            pass
    set_state(name, "ready" if ok else "install_failed")


@app.post("/api/servers")
def create_server(s: NewServer, u: dict = Depends(admin_only)):
    if not NAME_RE.match(s.name):
        raise HTTPException(400, "Name must be 2-32 chars: lowercase letters, digits, dashes")
    with database.db() as c:
        if c.execute("SELECT 1 FROM servers WHERE name=?", (s.name,)).fetchone():
            raise HTTPException(409, "A server with that name already exists")
        owner_id = u["id"]
        if s.owner:
            o = c.execute("SELECT id FROM users WHERE username=?", (s.owner,)).fetchone()
            if not o:
                raise HTTPException(400, "Owner not found")
            owner_id = o["id"]

    # ---- egg based server: record it, then run the egg's install script in the background
    if s.egg:
        egg = EGGS.get(s.egg)
        if not egg:
            raise HTTPException(400, "Unknown egg")
        image = (s.image or "").strip() or next(iter(egg["docker_images"].values()))
        if image not in egg["docker_images"].values():
            raise HTTPException(400, "That docker image is not offered by this egg")
        try:
            env = eggmod.validate_variables(egg, s.variables)
        except ValueError as e:
            raise HTTPException(400, str(e))
        data_dir = DATA / s.name
        data_dir.mkdir(parents=True, exist_ok=True)
        own_for_container([data_dir])
        with database.db() as c:
            c.execute(
                "INSERT INTO servers(name, owner_id, image, created, egg, variables, memory_mb, cpus, port, state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (s.name, owner_id, image, int(time.time()), s.egg, json.dumps(env), s.memory_mb, s.cpus, s.port, "installing"),
            )
        spawn(_run_install, s.name)
        return {"ok": True, "status": "installing"}

    # ---- old template / custom image server (unchanged behaviour)
    if not s.image or not s.image.strip():
        raise HTTPException(400, "Image is required")
    data_dir = DATA / s.name
    data_dir.mkdir(parents=True, exist_ok=True)
    ports = {f"{s.container_port}/tcp": s.port} if s.port and s.container_port else None
    try:
        dk.containers.run(
            s.image.strip(),
            s.command or None,
            name=f"mp_{s.name}",
            detach=True,
            tty=True,
            stdin_open=True,
            labels={LABEL: "1"},
            environment=s.env,
            mem_limit=f"{s.memory_mb}m",
            nano_cpus=int(s.cpus * 1e9),
            ports=ports,
            volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
            working_dir="/data",
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            cap_add=["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"],
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
        )
    except (APIError, ImageNotFound) as e:
        shutil.rmtree(data_dir, ignore_errors=True)
        raise HTTPException(400, getattr(e, "explanation", None) or str(e))
    with database.db() as c:
        c.execute(
            "INSERT INTO servers(name, owner_id, image, created) VALUES(?,?,?,?)",
            (s.name, owner_id, s.image.strip(), int(time.time())),
        )
    return {"ok": True}


def _run_start(name: str, gen: int, stop_first: bool = False) -> None:
    """Background job: (optionally stop,) then create + start the egg container."""

    def ready() -> None:
        if STARTING.get(name) == gen:
            STARTING.pop(name, None)

    try:
        r = get_row(name)
        egg = EGGS.get(r["egg"]) if r else None
        if not egg:
            raise RuntimeError("egg not found")
        if stop_first:
            try:
                eggmod.stop_server(dk, name, egg)
            except NotFound:
                pass
        eggmod.start_server(
            dk, name, egg, row_env(r), r["image"], DATA / name,
            r["memory_mb"] or 512, r["cpus"] or 1.0, r["port"], LABEL, ready,
        )
    except Exception as e:
        LAST_ERROR[name] = getattr(e, "explanation", None) or str(e)
        if STARTING.get(name) == gen:
            STARTING.pop(name, None)


def egg_power(name: str, r, action: str):
    egg = egg_of(r)
    if r["state"] != "ready":
        raise HTTPException(409, f"Server is not ready (state: {r['state']})")
    st = docker_status(name)
    global _gen
    if action in ("start", "restart"):
        if action == "start" and (name in STARTING or st == "running"):
            raise HTTPException(409, "Server is already running")
        _gen += 1
        LAST_ERROR.pop(name, None)
        STARTING[name] = _gen
        spawn(_run_start, name, _gen, action == "restart")
        return {"status": "starting"}
    if action == "stop":
        if st is None:
            if name in STARTING:
                raise HTTPException(409, "Server is still being created, try again in a moment")
            return {"status": "offline"}
        STARTING.pop(name, None)
        try:
            eggmod.stop_server(dk, name, egg)
        except APIError as e:
            raise HTTPException(400, getattr(e, "explanation", None) or str(e))
        return {"status": "offline"}
    if action == "kill":
        STARTING.pop(name, None)
        if st == "running":
            try:
                container(name).kill()
            except APIError as e:
                raise HTTPException(400, getattr(e, "explanation", None) or str(e))
        return {"status": "offline"}
    raise HTTPException(400, "Unknown action")


@app.post("/api/servers/{name}/power/{action}")
def power(name: str, action: str, u: dict = Depends(current_user)):
    r = server_row(name, u)
    if r["egg"]:
        return egg_power(name, r, action)
    c = container(name)
    try:
        if action == "start":
            c.start()
        elif action == "stop":
            c.stop(timeout=15)
        elif action == "restart":
            c.restart(timeout=15)
        elif action == "kill":
            c.kill()
        else:
            raise HTTPException(400, "Unknown action")
    except APIError as e:
        raise HTTPException(400, getattr(e, "explanation", None) or str(e))
    c.reload()
    return {"status": c.status}


@app.post("/api/servers/{name}/reinstall")
def reinstall(name: str, u: dict = Depends(admin_only)):
    """Run the egg install script again (files are kept, like Pterodactyl's reinstall)."""
    r = server_row(name, u)
    if not r["egg"]:
        raise HTTPException(400, "This server was not created from an egg")
    if r["state"] == "installing":
        raise HTTPException(409, "Already installing")
    if docker_status(name) == "running" or name in STARTING:
        raise HTTPException(400, "Stop the server first")
    set_state(name, "installing")
    spawn(_run_install, name)
    return {"ok": True}


@app.delete("/api/servers/{name}")
def delete_server(name: str, purge: bool = False, u: dict = Depends(admin_only)):
    server_row(name, u)
    for cname in (f"mp_{name}", f"mp_install_{name}"):
        try:
            dk.containers.get(cname).remove(force=True)
        except NotFound:
            pass
    STARTING.pop(name, None)
    LAST_ERROR.pop(name, None)
    with database.db() as c:
        c.execute("DELETE FROM servers WHERE name=?", (name,))
    (INSTALL_LOGS / f"{name}.log").unlink(missing_ok=True)
    if purge:
        shutil.rmtree(DATA / name, ignore_errors=True)
    return {"ok": True}


@app.get("/api/servers/{name}/stats")
def stats(name: str, u: dict = Depends(current_user)):
    r = server_row(name, u)
    out = {"cpu_percent": 0, "mem_mb": 0, "mem_limit_mb": 0}
    if r["egg"]:
        st = docker_status(name)
        out["status"] = server_status(r, st)
        out["error"] = LAST_ERROR.get(name)
        if st != "running":
            return out
        c = container(name)
    else:
        c = container(name)
        if c.status != "running":
            out["status"] = c.status
            return out
        out["status"] = c.status
    s = c.stats(stream=False)
    cpu = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
    sysd = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
    n = s["cpu_stats"].get("online_cpus", 1)
    out.update(
        cpu_percent=round(cpu / sysd * n * 100, 1) if sysd > 0 else 0,
        mem_mb=round(s["memory_stats"].get("usage", 0) / 1048576, 1),
        mem_limit_mb=round(s["memory_stats"].get("limit", 0) / 1048576, 1),
    )
    return out


# ---------------- file manager ----------------
def safe(name: str, rel: str):
    base = (DATA / name).resolve()
    target = (base / rel.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(400, "Invalid path")
    return base, target


@app.get("/api/servers/{name}/files")
def files(name: str, path: str = "", u: dict = Depends(current_user)):
    server_row(name, u)
    _, t = safe(name, path)
    if not t.is_dir():
        raise HTTPException(404, "Not a directory")
    out = []
    for p in sorted(t.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            out.append({"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0})
        except OSError:
            continue
    return out


@app.get("/api/servers/{name}/file")
def read_file(name: str, path: str, u: dict = Depends(current_user)):
    server_row(name, u)
    _, t = safe(name, path)
    if not t.is_file():
        raise HTTPException(404, "File not found")
    if t.stat().st_size > 1_000_000:
        raise HTTPException(413, "File too large to edit here (1 MB max)")
    return {"content": t.read_text(errors="replace")}


class FileBody(BaseModel):
    content: str


@app.put("/api/servers/{name}/file")
def write_file(name: str, path: str, body: FileBody, u: dict = Depends(current_user)):
    server_row(name, u)
    base, t = safe(name, path)
    if t == base:
        raise HTTPException(400, "Invalid path")
    missing = []
    p = t.parent
    while p != base and not p.exists():
        missing.append(p)
        p = p.parent
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(body.content)
    if get_row(name)["egg"]:  # the bot runs as uid 1000, the panel as root
        own_for_container([*missing, t])
    return {"ok": True}


@app.delete("/api/servers/{name}/file")
def delete_file(name: str, path: str, u: dict = Depends(current_user)):
    server_row(name, u)
    base, t = safe(name, path)
    if t == base:
        raise HTTPException(400, "Cannot delete the server root")
    if t.is_dir():
        shutil.rmtree(t)
    elif t.exists():
        t.unlink()
    return {"ok": True}


# ---------------- live console ----------------
async def _stream_install_log(ws: WebSocket, name: str) -> None:
    path = INSTALL_LOGS / f"{name}.log"
    pos = 0
    while True:
        row = get_row(name)
        installing = bool(row) and row["state"] == "installing"
        if path.exists():
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
            pos += len(data)
            if data:
                await ws.send_text(data.decode(errors="replace"))
        if not installing:
            return
        await asyncio.sleep(0.5)


@app.websocket("/api/servers/{name}/console")
async def console(ws: WebSocket, name: str, token: str):
    try:
        u = user_from_token(token)
        r = server_row(name, u)
    except HTTPException:
        await ws.close(code=4401)
        return
    loop = asyncio.get_running_loop()

    if r["egg"]:
        await ws.accept()
        if r["state"] in ("installing", "install_failed"):
            try:
                await _stream_install_log(ws, name)
                await ws.close()
            except Exception:
                pass
            return
        c = None
        for _ in range(120):  # wait up to 60s while the image is pulled / container is created
            try:
                c = dk.containers.get(f"mp_{name}")
                break
            except NotFound:
                if name not in STARTING:
                    break
                await asyncio.sleep(0.5)
        if c is None:
            await ws.send_text("\r\n[server is offline - press Start]\r\n")
            await ws.close()
            return
    else:
        try:
            c = container(name)
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()

    c.reload()
    if c.status != "running":
        await ws.send_text("\r\n[container is not running - press Start]\r\n")
        await ws.close()
        return
    tail = c.logs(tail=200).decode(errors="replace").replace("\n", "\r\n")
    await ws.send_text(tail)
    try:
        await loop.run_in_executor(None, lambda: c.resize(28, 100))
    except Exception:
        pass
    sock = c.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
    raw = sock._sock
    raw.setblocking(False)

    async def pump():
        while True:
            try:
                data = await loop.sock_recv(raw, 4096)
            except Exception:
                break
            if not data:
                break
            await ws.send_text(data.decode(errors="replace"))
        try:
            await ws.close()
        except Exception:
            pass

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_text()
            await loop.sock_sendall(raw, msg.encode())
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        task.cancel()
        try:
            sock.close()
        except Exception:
            pass


# ---------------- startup + static ----------------
@app.on_event("startup")
def startup():
    DATA.mkdir(parents=True, exist_ok=True)
    INSTALL_LOGS.mkdir(parents=True, exist_ok=True)
    database.init()
    with database.db() as c:
        c.execute("UPDATE servers SET state='install_failed' WHERE state='installing'")
    with database.db() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            user = os.environ.get("PANEL_ADMIN_USER", "admin")
            pw = os.environ.get("PANEL_ADMIN_PASSWORD")
            if pw:
                c.execute(
                    "INSERT INTO users(username, pw_hash, role) VALUES(?,?, 'admin')",
                    (user, hash_pw(pw)),
                )


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
