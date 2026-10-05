"""Pterodactyl-style eggs for MyPanel.

This mirrors what Pterodactyl (panel + Wings) does with a PTDL_v2 egg:

  install_server()  Runs the egg's installation script in a throw-away container
                    (the egg's install image, server files mounted at /mnt/server,
                    egg variables passed as environment).

  start_server()    Creates the server container from one of the egg's docker images,
                    passes the egg variables plus STARTUP as environment, mounts the
                    server files at /home/container and lets the image's entrypoint
                    run the startup command (that is how the parkervcp/yolks images
                    work). Then watches the console for the egg's "done" string so the
                    server goes  starting -> running.

  stop_server()     Uses the egg's stop setting: "^C" = SIGINT, "^SIGTERM" = that signal,
                    anything else is typed into the console as a command.

Eggs are plain JSON files in panel/eggs/*.json.
"""
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from docker.errors import NotFound

EGG_DIR = Path(__file__).parent / "eggs"

# The parkervcp/yolks images run as user "container" (uid/gid 1000).
CONTAINER_UID = 1000
CONTAINER_GID = 1000
SERVER_MOUNT = "/home/container"  # where server files live in the server container
INSTALL_MOUNT = "/mnt/server"  # where server files live in the install container

# If an egg's "done" string never shows up, flip to running after this many seconds
# (Pterodactyl would stay on "Starting" forever).
STARTUP_GRACE = int(os.environ.get("PANEL_STARTUP_GRACE", "30"))
STOP_TIMEOUT = int(os.environ.get("PANEL_STOP_TIMEOUT", "20"))

ALLOWED_SIGNALS = {"SIGINT", "SIGTERM", "SIGKILL", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2"}


# ---------------------------------------------------------------- loading
def _loads(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except ValueError:
        return {}


def parse_egg(raw: dict, egg_id: str) -> dict:
    images = raw.get("docker_images") or {}
    if not images:
        raise ValueError("egg has no docker_images")
    if not raw.get("startup"):
        raise ValueError("egg has no startup command")
    cfg = raw.get("config") or {}
    inst = (raw.get("scripts") or {}).get("installation") or {}
    variables = []
    for v in raw.get("variables") or []:
        variables.append(
            {
                "name": v["name"],
                "description": v.get("description") or "",
                "env_variable": v["env_variable"],
                "default_value": "" if v.get("default_value") is None else str(v["default_value"]),
                "user_viewable": bool(v.get("user_viewable", True)),
                "user_editable": bool(v.get("user_editable", True)),
                "rules": v.get("rules") or "",
            }
        )
    return {
        "id": egg_id,
        "name": raw.get("name") or egg_id,
        "description": raw.get("description") or "",
        "docker_images": dict(images),  # label -> image
        "startup": raw["startup"],
        "done": _loads(cfg.get("startup")).get("done") or None,
        "stop": cfg.get("stop") or "^C",
        "install_script": inst.get("script") or "",
        "install_image": inst.get("container") or "debian:bullseye-slim",
        "install_entrypoint": inst.get("entrypoint") or "bash",
        "variables": variables,
    }


def load_eggs(directory: Path = EGG_DIR) -> Dict[str, dict]:
    """Load every *.json in the egg directory. 'egg-python-bot.json' gets the id 'python-bot'."""
    eggs: Dict[str, dict] = {}
    for p in sorted(directory.glob("*.json")):
        egg_id = re.sub(r"^egg-", "", p.stem)
        try:
            eggs[egg_id] = parse_egg(json.loads(p.read_text(encoding="utf-8")), egg_id)
        except (ValueError, KeyError, OSError) as e:
            print(f"[mypanel] skipping egg {p.name}: {e}")
    return eggs


def public_egg(egg: dict) -> dict:
    """What the browser needs to build the 'new server' form (no install script)."""
    return {k: egg[k] for k in ("id", "name", "description", "docker_images", "variables")}


# ---------------------------------------------------------------- variables
def _check_rules(var: dict, val: str) -> None:
    label = var["name"]
    rules = [r for r in var["rules"].split("|") if r]
    if "\x00" in val:
        raise ValueError(f"{label} contains invalid characters")
    if val == "":
        if "required" in rules:
            raise ValueError(f"{label} is required")
        return
    numeric = "numeric" in rules or "integer" in rules
    if "integer" in rules and not re.fullmatch(r"-?\d+", val):
        raise ValueError(f"{label} must be a whole number")
    if "numeric" in rules and not re.fullmatch(r"-?\d+(\.\d+)?", val):
        raise ValueError(f"{label} must be a number")
    for r in rules:
        if r.startswith(("max:", "min:")):
            kind, _, n = r.partition(":")
            try:
                limit = float(n)
            except ValueError:
                continue
            measure = float(val) if numeric else len(val)
            unit = "" if numeric else " characters"
            if kind == "max" and measure > limit:
                raise ValueError(f"{label} must be at most {n}{unit}")
            if kind == "min" and measure < limit:
                raise ValueError(f"{label} must be at least {n}{unit}")
        elif r.startswith("in:"):
            allowed = r[3:].split(",")
            if val not in allowed:
                raise ValueError(f"{label} must be one of: {', '.join(allowed)}")


def validate_variables(
    egg: dict,
    given: Dict[str, str],
    base: Optional[Dict[str, str]] = None,
    is_admin: bool = True,
) -> Dict[str, str]:
    """Merge user-supplied values over `base` (or egg defaults) and check them against the egg rules.

    Raises ValueError with a readable message. Non-admins can only change user_editable variables.
    """
    known = {v["env_variable"]: v for v in egg["variables"]}
    unknown = set(given) - set(known)
    if unknown:
        raise ValueError(f"Unknown variable: {', '.join(sorted(unknown))}")
    out: Dict[str, str] = {}
    for key, var in known.items():
        current = (base or {}).get(key, var["default_value"])
        if key in given:
            val = "" if given[key] is None else str(given[key])
            if not is_admin and not var["user_editable"] and val != current:
                raise ValueError(f"{var['name']} cannot be edited")
        else:
            val = current
        _check_rules(var, val)
        out[key] = val
    return out


# ---------------------------------------------------------------- docker helpers
def chown_tree(path, uid: int = CONTAINER_UID, gid: int = CONTAINER_GID) -> None:
    """Hand the server files to the container user (the panel runs as root)."""
    path = str(path)
    try:
        os.lchown(path, uid, gid)
    except OSError:
        pass
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(root, n), uid, gid)
            except OSError:
                pass


def _remove(dk, container_name: str) -> None:
    try:
        dk.containers.get(container_name).remove(force=True)
    except NotFound:
        pass


def ensure_image(dk, image: str, say: Callable[[str], None]) -> None:
    try:
        dk.images.get(image)
        return
    except NotFound:
        pass
    say(f"[panel] Pulling image {image} (first use, this can take a minute)...")
    dk.images.pull(image)
    say("[panel] Image pulled.")


def _err(e: Exception) -> str:
    return getattr(e, "explanation", None) or str(e)


# ---------------------------------------------------------------- function 1: install
def install_server(
    dk,
    name: str,
    egg: dict,
    env: Dict[str, str],
    data_dir: Path,
    log_path: Path,
    tmp_root: Path,
    memory_mb: int = 1024,
) -> bool:
    """Run the egg's install script. Output is written to log_path (the panel streams it to the console).

    Returns True when the script exited 0.
    tmp_root must be a real directory on the host (not a systemd PrivateTmp) because dockerd mounts it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=tmp_root))
    ok = False
    with open(log_path, "w", encoding="utf-8", buffering=1, newline="") as log:

        def say(msg: str) -> None:
            log.write(msg + "\r\n")

        try:
            script = (egg["install_script"] or "").replace("\r\n", "\n")
            if not script.strip():
                say("[panel] This egg has no install script, nothing to install.")
                ok = True
            else:
                (script_dir / "install.sh").write_text(script)
                os.chmod(script_dir / "install.sh", 0o755)
                image = egg["install_image"]
                say(f"[panel] Installing '{name}' with egg '{egg['name']}' using {image}")
                ensure_image(dk, image, say)
                _remove(dk, f"mp_install_{name}")
                c = dk.containers.create(
                    image,
                    command=["/mnt/install/install.sh"],
                    entrypoint=egg["install_entrypoint"],
                    name=f"mp_install_{name}",
                    environment=dict(env),
                    volumes={
                        str(data_dir): {"bind": INSTALL_MOUNT, "mode": "rw"},
                        str(script_dir): {"bind": "/mnt/install", "mode": "ro"},
                    },
                    working_dir=INSTALL_MOUNT,
                    mem_limit=f"{memory_mb}m",
                    labels={"mypanel.install": "1"},  # NOT mypanel.managed, so it never shows in the server list
                    security_opt=["no-new-privileges"],
                )
                try:
                    c.start()
                    for chunk in c.logs(stream=True, follow=True):
                        text = chunk.decode("utf-8", "replace").replace("\r\n", "\n").replace("\n", "\r\n")
                        log.write(text)
                    code = c.wait()["StatusCode"]
                    ok = code == 0
                    if not ok:
                        say(f"[panel] Install script exited with code {code}.")
                finally:
                    try:
                        c.remove(force=True)
                    except Exception:
                        pass
        except Exception as e:  # docker / filesystem problems
            say(f"[panel] Install error: {_err(e)}")
            ok = False
        finally:
            shutil.rmtree(script_dir, ignore_errors=True)
        if ok:
            chown_tree(data_dir)
            say("[panel] Installation complete. Press Start.")
        else:
            say("[panel] Installation FAILED. Fix the variables / egg, then use Reinstall.")
    return ok


# ---------------------------------------------------------------- function 2: start
def start_server(
    dk,
    name: str,
    egg: dict,
    env: Dict[str, str],
    image: str,
    data_dir: Path,
    memory_mb: int,
    cpus: float,
    port: Optional[int],
    label: str,
    on_ready: Callable[[], None],
):
    """Create and start the server container, Pterodactyl style.

    A fresh container is created on every start so edited variables always apply.
    on_ready() is called once the egg's "done" string appears (or after STARTUP_GRACE
    seconds, or when the container exits).
    """
    _remove(dk, f"mp_{name}")
    ensure_image(dk, image, lambda _m: None)
    data_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(env)
    environment.update(
        {
            "STARTUP": egg["startup"],  # the yolks entrypoint turns {{VAR}} into ${VAR} and evals it
            "SERVER_MEMORY": str(memory_mb),
            "SERVER_IP": "0.0.0.0",
        }
    )
    if port:
        environment["SERVER_PORT"] = str(port)
    c = dk.containers.run(
        image,
        name=f"mp_{name}",
        detach=True,
        tty=True,
        stdin_open=True,
        labels={label: "1"},
        environment=environment,
        mem_limit=f"{memory_mb}m",
        memswap_limit=f"{memory_mb}m",  # no swap
        nano_cpus=int(cpus * 1e9),
        ports={f"{port}/tcp": port} if port else None,
        volumes={str(data_dir): {"bind": SERVER_MOUNT, "mode": "rw"}},
        working_dir=SERVER_MOUNT,
        security_opt=["no-new-privileges"],
        cap_drop=["ALL"],
    )
    _watch_startup(dk, name, egg.get("done"), on_ready)
    return c


def _watch_startup(dk, name: str, done: Optional[str], on_ready: Callable[[], None]) -> None:
    if not done:
        on_ready()
        return

    def run() -> None:
        timer = threading.Timer(STARTUP_GRACE, on_ready)
        timer.daemon = True
        timer.start()
        try:
            c = dk.containers.get(f"mp_{name}")
            tail = ""
            for chunk in c.logs(stream=True, follow=True):
                tail = (tail + chunk.decode("utf-8", "replace"))[-(len(done) + 4096):]
                if done in tail:
                    break
        except Exception:
            pass
        finally:
            timer.cancel()
            on_ready()

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- stop / console
def stop_signal(stop: str) -> Optional[str]:
    """'^C' -> SIGINT, '^SIGTERM' / '^TERM' -> SIGTERM, anything else -> None (it is a console command)."""
    if not stop.startswith("^"):
        return None
    s = stop[1:].strip().upper()
    if s == "C":
        return "SIGINT"
    if not s.startswith("SIG"):
        s = "SIG" + s
    return s if s in ALLOWED_SIGNALS else "SIGTERM"


def send_command(container, text: str) -> None:
    sock = container.attach_socket(params={"stdin": 1, "stream": 1})
    try:
        sock._sock.sendall((text + "\n").encode())
    finally:
        sock.close()


def stop_server(dk, name: str, egg: dict) -> None:
    """Gracefully stop using the egg's stop setting, kill if it takes longer than STOP_TIMEOUT."""
    c = dk.containers.get(f"mp_{name}")  # NotFound propagates
    c.reload()
    if c.status != "running":
        return
    sig = stop_signal(egg["stop"])
    try:
        if sig:
            c.kill(signal=sig)
        else:
            send_command(c, egg["stop"])
        c.wait(timeout=STOP_TIMEOUT)
    except Exception:
        try:
            c.kill()
        except Exception:
            pass
