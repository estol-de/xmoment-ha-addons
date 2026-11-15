"""estol XMoment Connect – Home-Assistant-Add-on.

Findet XMoment-MQTT-Geraete im Heimnetz (mDNS _xmoment._tcp, dazu feste Adressen aus den Optionen) und gibt
ihnen den Zugang zum Mosquitto-Broker von Home Assistant. Ab dann melden die Geraete ihre Leuchten selbst
per MQTT-Discovery an. Das Add-on ist nur Einrichtungshilfe: faellt es aus, laufen die Geraete weiter.

Automatisch verbunden werden nur Geraete OHNE Broker (Erst-Einrichtung). Ein Geraet, das an einem anderen
Broker haengt, wird nur auf Knopfdruck uebernommen.
"""
import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

DATA = Path(os.environ.get("XC_DATA", "/data"))
WEB = Path(__file__).resolve().parent / "web"
SUPERVISOR = os.environ.get("XC_SUPERVISOR_URL", "http://supervisor")
PORT = int(os.environ.get("XC_PORT", "8099"))
ALLOW_IPS = {ip for ip in os.environ.get("XC_ALLOW_IPS", "172.30.32.2").split(",") if ip}
SERVICE = "_xmoment._tcp.local."
POLL = int(os.environ.get("XC_POLL", "20"))
OFFLINE_AFTER = 90  # s ohne Antwort -> als offline zeigen

log = logging.getLogger("xmoment.connect")


def local_ip_towards(host: str) -> str:
    """Eigene Adresse, unter der das Geraet Home Assistant erreicht (Route zum Geraet)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 80))
        return s.getsockname()[0]


class Device:
    def __init__(self, node: str, host: str, port: int = 80) -> None:
        self.node, self.host, self.port = node, host, port
        self.info: dict = {}
        self.seen = 0.0
        self.message = ""
        self.busy = False
        self.auto = True  # False, sobald jemand von Hand getrennt hat -> nie wieder automatisch verbinden
        self.auto_at = 0.0    # naechster automatischer Erstverbindungs-Versuch
        self.relink_at = 0.0  # naechste automatische Passwort-Erneuerung

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}" if self.port != 80 else f"http://{self.host}"

    def view(self, broker_hosts: set) -> dict:
        m = self.info.get("mqtt") or {}
        if not self.seen or time.monotonic() - self.seen > OFFLINE_AFTER:
            state = "offline"
        elif not m.get("enabled"):
            state = "free"
        elif m.get("host") in broker_hosts:
            state = "linked" if m.get("connected") else "linking"
        else:
            state = "foreign"
        return {"node": self.node, "host": self.host, "url": self.url, "state": state,
                "network": self.info.get("network") or "", "lights": self.info.get("lights", 0),
                "ble": bool(self.info.get("connected")), "version": self.info.get("version", ""),
                "broker": m.get("host", ""), "error": m.get("error", ""), "message": self.message, "busy": self.busy}


class Connect:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.candidates: dict[str, int] = {}  # Adresse -> Port, aus mDNS, Optionen und gemerkten Geraeten
        self.options: dict = {}
        self.broker: dict = {}
        self.broker_error = ""
        self.http: aiohttp.ClientSession | None = None
        self.wake = asyncio.Event()
        self.known_file = DATA / "devices.json"

    # ------------------------------------------------------------ Supervisor

    async def supervisor(self, path: str) -> dict:
        headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}
        async with self.http.get(f"{SUPERVISOR}{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise RuntimeError(f"Supervisor {path}: {r.status}")
            return (await r.json()).get("data") or {}

    async def load_broker(self) -> None:
        try:
            data = await self.supervisor("/services/mqtt")
            self.broker = {"port": int(data.get("port") or 1883), "user": data.get("username") or "",
                           "password": data.get("password") or "", "tls": bool(data.get("ssl"))}
            self.broker_error = ""
        except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError) as e:
            self.broker, self.broker_error = {}, "Kein MQTT-Broker – bitte das Mosquitto-Add-on installieren und starten"
            log.warning("MQTT-Dienst nicht verfuegbar: %s", e)

    def broker_hosts(self) -> set:
        """Alle Adressen dieses Rechners, unter denen ein Geraet den Broker eingetragen haben koennte."""
        hosts = set()
        for d in self.devices.values():
            try:
                hosts.add(local_ip_towards(d.host))
            except OSError:
                pass
        return hosts

    # ------------------------------------------------------------ Geraete finden

    def remember(self) -> None:
        known = {n: {"host": d.host, "port": d.port, "auto": d.auto} for n, d in self.devices.items()}
        tmp = self.known_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(known, indent=2), encoding="utf-8")
        tmp.replace(self.known_file)

    def load_known(self) -> None:
        try:
            for node, d in json.loads(self.known_file.read_text(encoding="utf-8")).items():
                self.devices[node] = Device(node, d["host"], int(d.get("port", 80)))
                self.devices[node].auto = bool(d.get("auto", True))
                self.candidates[d["host"]] = int(d.get("port", 80))
        except (OSError, ValueError, KeyError):
            pass
        for entry in self.options.get("devices") or []:
            host, _, port = str(entry).strip().partition(":")
            if host:
                self.candidates[host] = int(port or 80)

    def on_mdns(self, zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        asyncio.ensure_future(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000):
            return
        for addr in info.parsed_addresses():
            if ipaddress.ip_address(addr).version == 4:
                if addr not in self.candidates:
                    log.info("XMoment im Netz gefunden: %s (%s)", name.split(".")[0], addr)
                self.candidates[addr] = info.port or 80
                self.wake.set()
                return

    async def probe(self, host: str, port: int) -> None:
        url = f"http://{host}:{port}/api/info"
        try:
            async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                info = await r.json() if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return
        if not info or info.get("product") != "XMoment MQTT" or not info.get("node"):
            return
        node = str(info["node"])
        dev = self.devices.get(node)
        if dev is None:
            dev = self.devices[node] = Device(node, host, port)
            self.remember()
        elif (dev.host, dev.port) != (host, port):  # neue Adresse (DHCP) -> mitziehen
            dev.host, dev.port = host, port
            self.remember()
        dev.info, dev.seen = info, time.monotonic()

    # ------------------------------------------------------------ Verbinden

    async def link(self, dev: Device, reason: str) -> None:
        if not self.broker:
            dev.message = self.broker_error
            return
        dev.busy, dev.message = True, ""
        try:
            body = {"host": local_ip_towards(dev.host), "port": self.broker["port"], "user": self.broker["user"],
                    "password": self.broker["password"], "tls": self.broker["tls"]}
            async with self.http.post(f"{dev.url}/api/mqtt", json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    dev.message = f"Gerät lehnt ab ({r.status})"
                    return
            log.info("%s an Home Assistant angebunden (%s)", dev.info.get("network") or dev.node, reason)
            dev.message = "Verbunden – die Leuchten erscheinen gleich unter MQTT"
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            dev.message = f"Gerät nicht erreichbar ({type(e).__name__})"
        finally:
            dev.busy = False
            await self.probe(dev.host, dev.port)

    async def unlink(self, dev: Device) -> None:
        dev.busy = True
        try:
            async with self.http.delete(f"{dev.url}/api/mqtt", json={}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                dev.message = "Getrennt – die Leuchten wurden aus Home Assistant entfernt" if r.status == 200 \
                    else f"Gerät lehnt ab ({r.status})"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            dev.message = f"Gerät nicht erreichbar ({type(e).__name__})"
        finally:
            dev.busy = False
            await self.probe(dev.host, dev.port)

    async def reconcile(self) -> None:
        hosts = self.broker_hosts()
        for dev in list(self.devices.values()):
            v = dev.view(hosts)
            if dev.busy or v["state"] == "offline":
                continue
            now = time.monotonic()
            if v["state"] == "free" and dev.auto and self.options.get("auto_connect", True) and now >= dev.auto_at:
                dev.auto_at = now + 600  # lehnt das Geraet ab, nicht im Sekundentakt nachhaken
                await self.link(dev, "automatisch, Erst-Einrichtung")
            elif v["state"] == "linking" and "Passwort" in v["error"] and now >= dev.relink_at:
                dev.relink_at = now + 600  # hoechstens alle 10 min, kein Dauerfeuer
                await self.link(dev, "Zugangsdaten erneuert")  # Mosquitto-Passwort hat sich geaendert

    async def loop(self) -> None:
        while True:
            await self.load_broker()
            await asyncio.gather(*(self.probe(h, p) for h, p in list(self.candidates.items())))
            await self.reconcile()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), POLL)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ Web (Ingress-Panel)

    def view(self) -> dict:
        hosts = self.broker_hosts()
        return {"broker": bool(self.broker), "broker_error": self.broker_error,
                "auto_connect": self.options.get("auto_connect", True),
                "devices": sorted((d.view(hosts) for d in self.devices.values()),
                                  key=lambda v: (v["state"] == "offline", v["network"].lower(), v["host"]))}

    def device(self, req) -> Device:
        dev = self.devices.get(req.match_info["node"])
        if dev is None:
            raise web.HTTPNotFound()
        return dev


connect = Connect()


@web.middleware
async def only_ingress(req, handler):
    if ALLOW_IPS and req.remote not in ALLOW_IPS:  # host_network: Port waere sonst im ganzen LAN offen
        raise web.HTTPForbidden()
    return await handler(req)


async def index(_req):
    return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


async def api_devices(_req):
    return web.json_response(connect.view())


async def _json_only(req) -> dict:
    if req.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    body = await req.json()
    return body if isinstance(body, dict) else {}


async def api_link(req):
    await _json_only(req)
    dev = connect.device(req)
    dev.auto = True
    connect.remember()
    await connect.link(dev, "von Hand")
    return web.json_response(connect.view())


async def api_unlink(req):
    await _json_only(req)
    dev = connect.device(req)
    dev.auto = False
    connect.remember()
    await connect.unlink(dev)
    return web.json_response(connect.view())


async def api_scan(req):
    """Adresse von Hand pruefen (falls mDNS im Netz gesperrt ist)."""
    body = await _json_only(req)
    host, _, port = str(body.get("host") or "").strip().partition(":")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = (await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET))[0][4][0]
        except (OSError, IndexError):
            return web.json_response({"error": "Adresse nicht gefunden"}, status=400)
    before = set(connect.devices)
    await connect.probe(host, int(port or 80))
    if set(connect.devices) == before and not any(d.host == host for d in connect.devices.values()):
        return web.json_response({"error": "Unter dieser Adresse antwortet kein XMoment-Gerät"}, status=404)
    connect.candidates[host] = int(port or 80)
    connect.wake.set()
    return web.json_response(connect.view())


async def start(app):
    connect.http = aiohttp.ClientSession()
    connect.load_known()
    app["loop"] = asyncio.create_task(connect.loop())
    if os.environ.get("XC_NO_MDNS") != "1":
        app["zc"] = AsyncZeroconf()
        app["browser"] = AsyncServiceBrowser(app["zc"].zeroconf, SERVICE, handlers=[connect.on_mdns])


async def stop(app):
    if "browser" in app:
        await app["browser"].async_cancel()
        await app["zc"].async_close()
    app["loop"].cancel()
    await connect.http.close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[only_ingress])
    app.router.add_get("/", index)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/devices/{node}/link", api_link)
    app.router.add_post("/api/devices/{node}/unlink", api_unlink)
    app.router.add_post("/api/scan", api_scan)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


def main() -> None:
    connect.options = json.loads((DATA / "options.json").read_text(encoding="utf-8"))
    logging.basicConfig(level=connect.options.get("log_level", "info").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    log.info("XMoment Connect startet (automatisch verbinden: %s)", "ja" if connect.options.get("auto_connect", True)
             else "nein")
    web.run_app(make_app(), port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
